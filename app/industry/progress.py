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
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import Depends

from app.db import get_connection
from app.esi import require_context
from app.industry._router import router

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


def queue_progress(context_id: int) -> dict:
    """Per-type and per-order progress for the account's current build queue."""
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
        return {"empty": True}
    # use_stock=False: progress must measure against the FULL requirement. With stock netted off,
    # the denominator would shrink as you acquire materials and the bar could never fill.
    res = _run_queue_plan(context_id, QueuePlanRequest(use_stock=False))
    if res.get("empty"):
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

    types = []
    tot = {"required": 0, "done": 0, "running": 0, "waiting": 0}
    for req in res.get("requirements", []):
        tid = int(req["type_id"])
        need = int(req["runs"])
        oq = int(req["output_qty"]) or 1
        from_stock = int(owned.get(tid, 0) // oq)      # whole runs' worth sitting in the hangar
        d = min(max(completed.get(tid, 0), from_stock), need)   # cap at what the plan asked for
        r = min(running.get(tid, 0), max(0, need - d))
        w = max(0, need - d - r)
        types.append({
            "type_id": tid, "name": req["name"], "activity": req["activity"],
            "required_runs": need, "done_runs": d, "running_runs": r, "waiting_runs": w,
            "output_qty": req["output_qty"],
            "in_stock": int(owned.get(tid, 0)),
            "pct": round(100.0 * d / need, 1) if need else 0.0,
        })
        tot["required"] += need
        tot["done"] += d
        tot["running"] += r
        tot["waiting"] += w

    by_type = {t["type_id"]: t for t in types}
    order_rows = []
    for o in orders:
        tid = int(o["product_type_id"])
        t = by_type.get(tid)
        oq = (t or {}).get("output_qty") or 1
        want = int(o["quantity"])
        have = max(completed.get(tid, 0) * oq, int(owned.get(tid, 0)))
        done_units = min(have, want)
        run_units = min(running.get(tid, 0) * oq, max(0, want - done_units))
        status = ("complete" if done_units >= want
                  else "building" if run_units > 0 or done_units > 0
                  else "waiting")
        order_rows.append({
            "id": o["id"], "name": o["name"], "label": o["label"], "product_type_id": tid, "quantity": want,
            "done_units": done_units, "running_units": run_units,
            "pct": round(100.0 * done_units / want, 1) if want else 0.0,
            "status": status,
        })

    return {
        "empty": False,
        "since": since,
        "totals": tot,
        "pct": round(100.0 * tot["done"] / tot["required"], 1) if tot["required"] else 0.0,
        "types": types,
        "orders": order_rows,
    }


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
        return {"empty": True}
    res = _run_queue_plan(context_id, QueuePlanRequest(use_stock=False))
    if res.get("empty"):
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
    tot = {"required": 0, "done": 0, "running": 0, "waiting": 0}
    for tid, r in reqs.items():
        need = int(r["runs"])
        d = min(done_runs.get(tid, 0), need)
        run = min(running_runs.get(tid, 0), max(0, need - d))
        w = max(0, need - d - run)
        types.append({"type_id": tid, "name": r["name"], "activity": r["activity"],
                      "required_runs": need, "done_runs": d, "running_runs": run,
                      "waiting_runs": w, "output_qty": r["output_qty"],
                      "in_stock": 0,
                      "pct": round(100.0 * d / need, 1) if need else 0.0})
        tot["required"] += need
        tot["done"] += d
        tot["running"] += run
        tot["waiting"] += w

    by_type = {t["type_id"]: t for t in types}
    order_rows = []
    for o in orders:
        tid = int(o["product_type_id"])
        t = by_type.get(tid)
        oq = (t or {}).get("output_qty") or 1
        want = int(o["quantity"])
        done_units = min((t or {}).get("done_runs", 0) * oq, want)
        run_units = min((t or {}).get("running_runs", 0) * oq, max(0, want - done_units))
        status = ("complete" if done_units >= want
                  else "building" if run_units > 0 or done_units > 0 else "waiting")
        order_rows.append({"id": o["id"], "name": o["name"], "label": o["label"],
                           "product_type_id": tid,
                           "quantity": want, "done_units": done_units, "running_units": run_units,
                           "pct": round(100.0 * done_units / want, 1) if want else 0.0,
                           "status": status})

    return {"empty": False, "simulated": True, "simulated_pct": pct, "since": None,
            "totals": tot,
            "pct": round(100.0 * tot["done"] / tot["required"], 1) if tot["required"] else 0.0,
            "types": types, "orders": order_rows}


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
