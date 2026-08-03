"""Queue progress from real ESI industry jobs.

Answers "what's started, what's done, what's still waiting" for the build queue by matching observed
industry jobs against the queue plan's per-type requirements.

Two things shape the design:

* **ESI jobs cannot be tagged.** There is no field on an industry job we can stamp with our order
  id, so attribution has to be inferred from the product type.
* **The queue aggregates demand on purpose.** One batch of an intermediate can serve several orders,
  so binding a job to a single order is ambiguous *by construction*. Progress is therefore tracked
  per TYPE and only rolled up to orders afterwards, via each order's own end product.

Nothing here changes how jobs are fetched. Completed work is read from the two existing forward-only
ledgers (`pp_industry_completions`, `pp_reaction_completions`) and running work from the two existing
per-character job caches, so this module adds no ESI traffic and no new scope.

**Marking a job done by hand is the third signal.** Inference is right most of the time and wrong in
ways the user can see and we can't: a batch built on a character that never granted the jobs scope,
work done before the account was connected, a component acquired by trade or contract instead of
built, or an ESI job cache that simply hasn't caught up. When the screen says a job is outstanding
and the player knows it isn't, the only honest fix is to let them say so. A manual mark is stored
per TYPE (the same grain everything else here is tracked at) and folded in as one more "done"
signal, never overriding a higher measured one.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from fastapi import Depends
from pydantic import BaseModel

from app.db import get_connection
from app.sde import ensure_once
from app.esi import require_context
from app.industry._router import router

# Stored runs value meaning "all of it, whatever the plan currently asks for". A concrete number
# would go stale the moment the queue is re-planned (change a quantity and a job marked complete
# would silently become partly outstanding again), which is the opposite of what ticking it means.
_ALL = -1

# Statuses that mean "a slot is busy with this right now". `ready` is finished-but-undelivered — the
# work is done but it hasn't been collected, so it counts as in-progress, not done (the completions
# ledgers only record a job once it's actually delivered).
_RUNNING = ("active", "paused", "ready")

MANUFACTURING_ACTIVITY_ID = 1
REACTION_ACTIVITY_ID = 9


def _ts(value) -> float | None:
    """ESI dates are ISO-8601 (`2026-07-25T12:00:00Z`); our ledgers store unix seconds. Normalise to
    unix seconds so the two can be compared against the same epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _running_by_type(context_id: int, since: float) -> dict[int, int]:
    """Runs currently in progress per product type, across BOTH job pools, for jobs started at or
    after `since`. Reads the existing per-character caches — a stale cache under-reports rather than
    lying, which is the safe direction."""
    out: dict[int, int] = {}
    con = get_connection()
    try:
        for table, activity in (("pp_char_manufacturing_jobs", MANUFACTURING_ACTIVITY_ID),
                                ("pp_char_industry_jobs", REACTION_ACTIVITY_ID)):
            try:
                rows = con.execute(
                    f"SELECT j.jobs_json FROM {table} j "
                    "JOIN pp_characters c ON c.character_id = j.character_id "
                    "WHERE c.context_id = ?",
                    (context_id,),
                ).fetchall()
            except Exception:
                continue                      # table not created yet on a fresh install
            for r in rows:
                try:
                    jobs = json.loads(r["jobs_json"] or "[]")
                except Exception:
                    continue
                for j in jobs:
                    # The manufacturing cache is pre-filtered to activity 1 and drops the field;
                    # the reaction cache keeps raw ESI jobs, so only filter when it's present.
                    if j.get("activity_id") not in (None, activity):
                        continue
                    if j.get("status") not in _RUNNING:
                        continue
                    started = _ts(j.get("start_date"))
                    if started is not None and started < since:
                        continue
                    tid = j.get("product_type_id")
                    if tid is None:
                        continue
                    out[int(tid)] = out.get(int(tid), 0) + int(j.get("runs") or 0)
    finally:
        con.close()
    return out


def _done_by_type(context_id: int, since: float) -> dict[int, int]:
    """Runs delivered per product type since `since`, from the two completion ledgers."""
    out: dict[int, int] = {}
    con = get_connection()
    try:
        for table in ("pp_industry_completions", "pp_reaction_completions"):
            try:
                rows = con.execute(
                    f"SELECT product_type_id, runs FROM {table} "
                    "WHERE context_id = ? AND completed_at >= ?",
                    (context_id, since),
                ).fetchall()
            except Exception:
                continue
            for r in rows:
                tid = r["product_type_id"]
                if tid is None:
                    continue
                out[int(tid)] = out.get(int(tid), 0) + int(r["runs"] or 0)
    finally:
        con.close()
    return out


@ensure_once
def ensure_manual_done_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_manual_done (
                context_id INTEGER NOT NULL,
                type_id    INTEGER NOT NULL,
                runs       INTEGER NOT NULL DEFAULT -1,
                marked_at  REAL NOT NULL,
                PRIMARY KEY (context_id, type_id)
            )
        """)
        con.commit()
    finally:
        con.close()


def _manual_by_type(context_id: int, since: float) -> dict[int, int]:
    """Runs the user has marked done by hand, per type.

    Epoch-gated like the completion ledgers, and for the same reason: re-queueing restarts the
    clock, so a tick left over from a build you finished last month can't read as progress on a new
    one. `_ALL` is resolved against the plan's own requirement by the caller.
    """
    ensure_manual_done_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT type_id, runs FROM pp_industry_manual_done "
            "WHERE context_id = ? AND marked_at >= ?",
            (context_id, since),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(r["type_id"]): int(r["runs"]) for r in rows}


def set_manual_done(context_id: int, type_id: int, runs: int | None) -> None:
    """Mark (`runs=None` → all of it, or a run count) or clear (`runs=0`) a type as done by hand."""
    ensure_manual_done_table()
    con = get_connection()
    try:
        if runs is not None and runs <= 0:
            con.execute("DELETE FROM pp_industry_manual_done WHERE context_id=? AND type_id=?",
                        (context_id, type_id))
        else:
            con.execute(
                "INSERT INTO pp_industry_manual_done (context_id, type_id, runs, marked_at) "
                "VALUES (?,?,?,?) ON CONFLICT(context_id, type_id) DO UPDATE SET "
                "runs=excluded.runs, marked_at=excluded.marked_at",
                (context_id, type_id, _ALL if runs is None else int(runs), time.time()))
        con.commit()
    finally:
        con.close()


def resolve_done(need: int, completed: int, from_stock: int, manual: int) -> int:
    """The three "it's done" signals combined into one run count.

    Each of them under-reports on its own — the ledger only knows the current epoch, the hangar goes
    to zero once the output is consumed by the next stage, and a hand mark is only as complete as
    the user bothered to be — so the highest wins, capped at what the plan actually asked for. Taking
    the max is also what keeps a manual tick honest: it can raise the count, never hide observed work.
    """
    return min(max(completed, from_stock, manual), need)


def _epoch(context_id: int) -> float:
    """Only work started after the queue was set up counts toward it — otherwise an unrelated build
    of the same item from last week would read as queue progress. The oldest still-queued order is
    the epoch; re-queueing legitimately restarts the clock."""
    con = get_connection()
    try:
        row = con.execute(
            "SELECT MIN(created_at) AS t FROM pp_industry_orders WHERE context_id = ?",
            (context_id,),
        ).fetchone()
    finally:
        con.close()
    return float((row and row["t"]) or 0.0)


def _queue_snapshot(context_id: int):
    """(orders, plan) for the account's queue, or (None, None) if there's nothing to report.

    Shared by the live and simulated progress paths: both need the same order rows in the same
    queue order, and the same full-requirement plan to measure against.
    """
    from app.industry.orders import QueuePlanRequest, _run_queue_plan

    con = get_connection()
    try:
        orders = con.execute(
            "SELECT id, product_type_id, name, quantity, COALESCE(label, '') AS label FROM pp_industry_orders "
            "WHERE context_id = ? ORDER BY priority DESC, id",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    if not orders:
        return None, None
    # use_stock=False: progress must measure against the FULL requirement. With stock netted off,
    # the denominator would shrink as you acquire materials and the bar could never fill.
    res = _run_queue_plan(context_id, QueuePlanRequest(use_stock=False))
    if res.get("empty"):
        return None, None
    return orders, res


def _type_row(tid: int, req, need: int, done: int, running: int, in_stock: int,
              manual: int = 0) -> dict:
    """One per-type progress row. `req` is the plan requirement (name/activity/output_qty).

    `manual_runs` is reported separately from `done_runs` so the UI can show that this one was
    ticked by hand rather than observed — and offer to untick it.
    """
    return {
        "type_id": tid, "name": req["name"], "activity": req["activity"],
        "required_runs": need, "done_runs": done, "running_runs": running,
        "waiting_runs": max(0, need - done - running),
        "output_qty": req["output_qty"], "in_stock": in_stock, "manual_runs": manual,
        "pct": round(100.0 * done / need, 1) if need else 0.0,
    }


def _totals(types: list[dict]) -> dict:
    """Roll the per-type run counts up to the queue-wide totals the progress bar reads."""
    keys = (("required", "required_runs"), ("done", "done_runs"),
            ("running", "running_runs"), ("waiting", "waiting_runs"))
    return {k: sum(int(t[src]) for t in types) for k, src in keys}


def _order_rows(orders, types: list[dict], units) -> list[dict]:
    """One row per queued order: how many UNITS of that product are done / in flight.

    `units(tid, type_row, output_qty, want) -> (done_units, running_units)` is the only thing that
    differs between real progress (measured from completions + what's in the hangar) and simulated
    progress (derived from the invented run counts), so it's the only thing passed in.
    """
    by_type = {t["type_id"]: t for t in types}
    rows = []
    for o in orders:
        tid = int(o["product_type_id"])
        t = by_type.get(tid)
        oq = (t or {}).get("output_qty") or 1
        want = int(o["quantity"])
        done_units, run_units = units(tid, t, oq, want)
        rows.append({
            "id": o["id"], "name": o["name"], "label": o["label"], "product_type_id": tid,
            "quantity": want, "done_units": done_units, "running_units": run_units,
            "pct": round(100.0 * done_units / want, 1) if want else 0.0,
            "status": ("complete" if done_units >= want
                       else "building" if run_units > 0 or done_units > 0
                       else "waiting"),
        })
    return rows


def _progress_payload(types: list[dict], order_rows: list[dict], **extra) -> dict:
    tot = _totals(types)
    return {
        "empty": False,
        "totals": tot,
        "pct": round(100.0 * tot["done"] / tot["required"], 1) if tot["required"] else 0.0,
        "types": types,
        "orders": order_rows,
        **extra,
    }


def queue_progress(context_id: int) -> dict:
    """Per-type and per-order progress for the account's current build queue."""
    orders, res = _queue_snapshot(context_id)
    if orders is None:
        return {"empty": True}

    since = _epoch(context_id)
    completed = _done_by_type(context_id, since)
    running = _running_by_type(context_id, since)
    # Two independent "done" signals, combined by taking whichever is higher:
    #   * OWNING the output proves it's done, needs no epoch, and survives a re-queue — but it goes
    #     to zero once the stuff is consumed by the next stage up.
    #   * The completion ledgers still know you built it, but only within the queue's epoch.
    # Either alone under-reports; the max of the two is right in both directions.
    from app.industry.assets import owned_quantities
    owned = owned_quantities(context_id)
    # The third signal: what the user told us is done. Same max() treatment — a hand mark can only
    # ever raise the count, so it can't hide work that the ledgers or the hangar prove is still
    # outstanding, and it doesn't need to be right about the exact number to be useful.
    marked = _manual_by_type(context_id, since)

    def _manual_runs(tid: int, need: int) -> int:
        m = marked.get(tid)
        return 0 if m is None else (need if m == _ALL else min(m, need))

    types = []
    for req in res.get("requirements", []):
        tid = int(req["type_id"])
        need = int(req["runs"])
        oq = int(req["output_qty"]) or 1
        from_stock = int(owned.get(tid, 0) // oq)      # whole runs' worth sitting in the hangar
        man = _manual_runs(tid, need)
        d = resolve_done(need, completed.get(tid, 0), from_stock, man)
        r = min(running.get(tid, 0), max(0, need - d))
        types.append(_type_row(tid, req, need, d, r, in_stock=int(owned.get(tid, 0)), manual=man))

    def units(tid, t, oq, want):
        # A product ticked done is done in UNITS too — otherwise the order chip would still read
        # "not started" next to a build the user just marked complete. `_ALL` means the whole thing
        # regardless of what the plan says, which also covers a product that has no requirement row
        # left (already netted off by stock).
        if marked.get(tid) == _ALL:
            return want, 0
        man_units = _manual_runs(tid, (t or {}).get("required_runs") or 0) * oq
        have = max(completed.get(tid, 0) * oq, int(owned.get(tid, 0)), man_units)
        done_units = min(have, want)
        return done_units, min(running.get(tid, 0) * oq, max(0, want - done_units))

    return _progress_payload(types, _order_rows(orders, types, units), since=since)


def simulated_progress(context_id: int, pct: float) -> dict:
    """The same payload `queue_progress` returns, but with the state invented at `pct` complete.

    Purpose: someone who hasn't started manufacturing yet sees nothing but zeroes, so the live views
    (stage counters, card badges, queue bars) can't be judged at all. This drives the REAL rendering
    path with plausible state instead of a mock-up, so what you see is exactly what real jobs will
    produce.

    Deliberately READ-ONLY — it never touches the completion ledgers or job caches, because those
    feed lifetime turnover and profit, and seeding them with fiction to preview a UI would corrupt
    real numbers permanently. Every response is tagged `simulated` so it can't be mistaken for real.
    """
    orders, res = _queue_snapshot(context_id)
    if orders is None:
        return {"empty": True}

    reqs = {int(r["type_id"]): r for r in res.get("requirements", [])}
    if not reqs:
        return {"empty": True}

    # Completion follows the SCHEDULE's own order, so a preview shows early stages finishing before
    # later ones — the shape real progress actually takes — rather than a uniform fill.
    order: list[int] = []
    for w in (res.get("schedule") or {}).get("waves", []):
        for t in w.get("tasks", []):
            tid = int(t["type_id"])
            if tid in reqs and tid not in order:
                order.append(tid)
    for tid in reqs:                                   # anything the schedule didn't mention
        if tid not in order:
            order.append(tid)

    total_runs = sum(int(r["runs"]) for r in reqs.values())
    budget = total_runs * max(0.0, min(100.0, pct)) / 100.0

    done_runs: dict[int, int] = {}
    running_runs: dict[int, int] = {}
    for tid in order:
        need = int(reqs[tid]["runs"])
        if budget >= need:
            done_runs[tid] = need
            budget -= need
        elif budget > 0:
            d = int(budget)
            done_runs[tid] = d
            running_runs[tid] = max(1, min(need - d, max(1, need // 3)))
            budget = 0
        else:
            # The first untouched type gets a couple of jobs in flight, so "running" is represented.
            if not running_runs and need:
                running_runs[tid] = max(1, min(need, 2))
            done_runs.setdefault(tid, 0)

    types = []
    for tid, r in reqs.items():
        need = int(r["runs"])
        d = min(done_runs.get(tid, 0), need)
        run = min(running_runs.get(tid, 0), max(0, need - d))
        types.append(_type_row(tid, r, need, d, run, in_stock=0))

    def units(_tid, t, oq, want):
        done_units = min((t or {}).get("done_runs", 0) * oq, want)
        return done_units, min((t or {}).get("running_runs", 0) * oq, max(0, want - done_units))

    return _progress_payload(types, _order_rows(orders, types, units),
                             simulated=True, simulated_pct=pct, since=None)


@router.get("/api/industry/progress")
def industry_progress(simulate: float | None = None, ctx: int = Depends(require_context)):
    """Live progress of the build queue: per-type done/running/waiting run counts and a per-order
    roll-up. Own-account scoped.

    `simulate=0..100` returns invented state at that completion for previewing the UI. Read-only —
    it writes nothing, so it can never leak into the real ledgers.
    """
    if simulate is not None:
        return simulated_progress(ctx, float(simulate))
    return queue_progress(ctx)


class MarkDone(BaseModel):
    type_id: int
    runs: int | None = None      # None = all of it; 0 = clear the mark


@router.post("/api/industry/progress/done")
def industry_mark_done(req: MarkDone, ctx: int = Depends(require_context)):
    """Mark a build step done (or un-mark it) by hand, and return the refreshed progress.

    Deliberately does NOT write to the completion ledgers: those feed lifetime turnover and profit,
    and a tick is a statement about this queue's progress, not evidence of an ISK-bearing job. Same
    rule the simulated-progress preview follows.
    """
    set_manual_done(ctx, int(req.type_id), req.runs)
    # Customer links cache their payload for a minute. A mark moves their progress bar too, so
    # without this the builder ticks a step done, looks at the link they handed over, and sees it
    # unchanged — which is precisely the doubt a status link exists to remove. Per ACCOUNT, not per
    # order: a mark is per type, and one type can feed several customers' orders.
    try:
        from app.industry.shares import invalidate_context_shares
        invalidate_context_shares(ctx)
    except Exception:
        pass                      # a stale customer page for a minute must not fail the mark
    return queue_progress(ctx)
