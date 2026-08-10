"""Personal reaction-job tracking, plan slots and orphan handling — the 'what is running / what am
I committed to' layer. Reads live ESI industry jobs (opt-in scope), matches them against the
persistent plan (pp_reaction_assignments), levels that plan's run counts, and values everything via
the graph layer. Depends on settings + graph; advisor (the suggestion engine) and orders build on
top of this, so this must not import either."""
import json as _json
import logging
import math
import time as _time
from datetime import datetime

log = logging.getLogger(__name__)


from app import esi_http
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.db import add_columns
from app.markets import resolve_market_data
from app.cache import cache_invalidate, charlist_key
from app.esi import require_context, _get_valid_token
from app import completions

from app.reactions._router import router
from app.reactions.settings import effective_reaction_settings
from app.reactions.graph import (
    _load_goo_and_reached, _value_reaction_batch, _ordered_chain_tiers, reaction_stock_pool,
    _shopping_roots, tier_ranks, tidy_runs, request_memo, _TIDY_STEPS,
)


# ── Personal reaction-job tracking (opt-in scope, see app.esi.INDUSTRY_JOBS_SCOPES) ────────────
# Cache-at-fetch, not live-fetch-on-every-page-load (same shape as app.pi_sim's colony state):
# ESI already reports start_date/end_date directly for a job, so there's no decay/rate math to
# simulate forward — just cache the raw filtered job list with a fetched_at timestamp, refreshed
# on demand (a "Refresh" button, same UX as the existing planet rescan) rather than polling.

@ensure_once
def ensure_industry_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_industry_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


_structure_name_cache: dict[int, str] = {}  # structure names don't change — cache for process lifetime


def _resolve_structure_name(structure_id: int, access_token: str) -> str:
    if structure_id in _structure_name_cache:
        return _structure_name_cache[structure_id]
    name = f"Structure #{structure_id}"
    try:
        resp = esi_http.get(f"universe/structures/{structure_id}/", token=access_token, timeout=10)
        resp.raise_for_status()
        name = resp.json().get("name") or name
    except Exception:
        pass  # best-effort — an unresolvable structure just shows its raw ID, never blocks the fetch
    _structure_name_cache[structure_id] = name
    return name


# EVE industry activity id for Reactions is 9 (Manufacturing=1, ME research=4, …). Verified
# against live corp/character industry-jobs responses — a reaction-heavy corp's jobs are all
# activity 9, and there is no activity 11 at all. An earlier value of 11 here was wrong but never
# caught, because the jobs table was never populated (the refresh was unwired) so the filter never
# actually ran against real data.
REACTION_ACTIVITY_ID = 9

LEDGER = "pp_reaction_completions"


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Fetch this character's reaction jobs (activity_id 9) from ESI, resolving each distinct
    facility to a readable name. Best-effort: returns [] on any failure rather than raising —
    a refresh failing for one character must not block the others."""
    try:
        resp = esi_http.get(f"characters/{character_id}/industry/jobs/",
                            token=access_token, timeout=10)
        resp.raise_for_status()
        jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == REACTION_ACTIVITY_ID]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


def fetch_corp_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """This character's reaction jobs installed FOR CORPORATION (a shared corp hangar/reactor,
    not the character's personal jobs) — a real, confirmed gap: these never appear via the
    per-character endpoint fetch_industry_jobs uses, only via the corp one, and only when the
    character holds Factory_Manager/Director. Best-effort like the rest of this module: any
    failure (missing role, no corp, network) returns [] rather than raising — one character's
    corp-jobs lookup failing must never block their own personal jobs or any other character's
    refresh. Filtered to `installer_id == character_id` — this reads the WHOLE corp's job queue
    over ESI, but only this specific character's own installs are what a "my jobs" view should
    show, not every corpmate's."""
    try:
        with esi_http.client(timeout=10) as client:
            pub = esi_http.get(f"characters/{character_id}/", client=client).json()
            corp_id = pub.get("corporation_id")
            if not corp_id:
                return []
            resp = esi_http.get(f"corporations/{corp_id}/industry/jobs/",
                                client=client, token=access_token)
            resp.raise_for_status()
            jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == REACTION_ACTIVITY_ID and j.get("installer_id") == character_id]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


# ESI's industry-jobs endpoint caches ~5 min; a plain tab-open refresh must not re-hit ESI more
# often than that (both to respect CCP's cache and to keep the Reactions tab snappy). The manual
# "Refresh jobs" button passes force=1 to bypass this and pull immediately.
_JOBS_CACHE_TTL = 300


@router.post("/api/reactions/jobs/refresh")
def refresh_industry_jobs(force: int = 0, context_id: int = Depends(require_context)):
    """Refresh the caller's own characters' cached reaction-job list from ESI — only characters
    that have actually granted the industry-jobs scope (opted in via ?reactions=1 login) are
    fetched; others are silently skipped, not an error, since most PI-planner accounts never
    opt into this. Called on Reactions tab-open (respecting ESI's ~5min cache via _JOBS_CACHE_TTL)
    and by the manual "Refresh jobs" button (force=1, bypasses the staleness guard)."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        # Last-fetch time per character, read up front (one connection, closed before the ESI loop
        # below) so the staleness guard doesn't re-hit ESI for a character refreshed seconds ago.
        fetched_at_by_char = {r["character_id"]: r["fetched_at"] for r in con.execute(
            "SELECT character_id, fetched_at FROM pp_char_industry_jobs"
        )}
    finally:
        con.close()

    now = _time.time()
    skipped = 0
    fetched: list[tuple] = []          # (character_id, jobs_json, fetched_at) — written in one batch below
    for c in chars:
        scopes = c["scopes"] or ""
        if "read_character_jobs" not in scopes:
            continue
        prev = fetched_at_by_char.get(c["character_id"])
        if not force and prev is not None and (now - prev) < _JOBS_CACHE_TTL:
            skipped += 1  # still within ESI's own cache window — a re-fetch would return the same data
            continue
        token = _get_valid_token(c["character_id"])
        if not token:
            continue
        jobs = fetch_industry_jobs(c["character_id"], token)
        # Only characters that re-authorised after the corp-jobs scope was added carry it —
        # already-connected characters keep working (personal jobs only) until they reconnect,
        # no forced re-auth. job_id is unique across BOTH endpoints (it's ESI's own job
        # identifier), so a plain concat can't double-count even in the (shouldn't-happen) case
        # of a job appearing in both responses.
        if "read_corporation_jobs" in scopes:
            jobs = jobs + fetch_corp_industry_jobs(c["character_id"], token)
        fetched.append((c["character_id"], _json.dumps(jobs), _time.time()))

    # One connection for all the writes, after the (slow) ESI fetches are done — not one per char.
    if fetched:
        con = get_connection()
        try:
            con.executemany(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                fetched,
            )
            con.commit()
        finally:
            con.close()
    refreshed = len(fetched)
    # The Characters tab shows each toon's running reaction jobs (see list_characters), served
    # from the same Redis-cached charlist payload — so a refresh that actually pulled new jobs must
    # bust that cache or the tab keeps showing stale/absent jobs until the next colony rescan.
    if refreshed:
        cache_invalidate(charlist_key(context_id))
    return {"ok": True, "characters_refreshed": refreshed, "characters_skipped": skipped}


def reaction_slots(character_row: dict) -> int:
    """1 base slot + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at
    the game's real max of 11 (5+5+1)."""
    return min(11, 1 + (character_row.get("mass_reactions") or 0) + (character_row.get("advanced_mass_reactions") or 0))


def reaction_capable(character_row: dict) -> tuple[bool, str]:
    """(counts toward reaction capacity, why not).

    Two conditions, both required:

    * **Job-tracking scope** — without it we can't see what's already running, so we'd have no idea
      how loaded the character is. (Also excludes a wallet-only character outright.)
    * **A reaction slot skill actually trained** — every character has one free base slot, but a
      character that has never trained Mass Reactions isn't running reactions. Counting those base
      slots inflated capacity by one per idle alt, and the optimizer sizes its suggestions against
      that ceiling, so it proposed work for characters that were never going to run it.

    Matches app/industry/slots.py's `_eligibility` so the two tools report the same capacity.
    """
    if "read_character_jobs" not in (character_row["scopes"] or ""):
        return False, "not connected for job tracking"
    trained = (character_row["mass_reactions"] or 0) > 0 or (character_row["advanced_mass_reactions"] or 0) > 0
    if not trained:
        return False, "no reaction slot skills trained"
    return True, ""


@ensure_once
def ensure_reaction_assignments_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_assignments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                runs         INTEGER NOT NULL,
                input_cost   REAL NOT NULL,
                reward       REAL NOT NULL,
                created_at   REAL NOT NULL
            )
        """)
        con.commit()
        # tier_order: 0 = the deepest intermediate reaction (react first — a real chain, e.g.
        # goo -> Ferrofluid -> Nonlinear Metamaterials, needs the intermediate done before the
        # top-level reaction can even start), ascending up to the top-level product itself
        # (highest number in the group = react last). Existing single-tier assignments default
        # to 0, unaffected — additive migration, matches this codebase's convention.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN tier_order INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except Exception:
            pass
        # order_id: tags every row (top-level AND its chain-tier rows) created on behalf of a
        # fixed-unit customer order (see ensure_reaction_orders_table below) — NULL for every
        # assignment created the normal way (manual-assign, suggest-and-assign), no behavior
        # change there. Lets the dashboard label which slots are committed to a client job.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN order_id INTEGER")
            con.commit()
        except Exception:
            pass
    finally:
        con.close()


# ── Fixed-unit customer orders ──────────────────────────────────────────────────────────────
# A different framing from the day-cadence/profit-maximizing wizard above: sometimes another
# player asks for a FIXED number of finished units (a one-off job), not an ongoing weekly
# routine. An order is persistent (tracked across sessions, not a one-shot calculator) and
# committing to it occupies real reaction slots the same way the suggestion/manual-assign flow
# does — see _allocate_and_insert below, which reuses the exact slot-spreading logic
# _suggest_reactions already has.

@ensure_once
def ensure_reaction_orders_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id     INTEGER NOT NULL,
                type_id        INTEGER NOT NULL,
                name           TEXT NOT NULL,
                target_qty     REAL NOT NULL,
                top_level_runs INTEGER NOT NULL,
                assigned_runs  INTEGER NOT NULL DEFAULT 0,
                client_name    TEXT,
                notes          TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                created_at     REAL NOT NULL
            )
        """)
        con.commit()
        # What the CLIENT pays for the whole order. The one number this tool cannot derive: an
        # order's revenue is whatever was negotiated, not a market price. Without it an order's
        # profit is unknowable, and the dashboard was reporting that as 0 — asserting no profit
        # rather than admitting it did not know. NULL stays "not told", which reads as unknown
        # everywhere and never as zero.
        add_columns(con, "pp_reaction_orders", "client_price DOUBLE PRECISION")
    finally:
        con.close()


@ensure_once
def ensure_reaction_completions_table():
    """Forward-only ledger of FINISHED reaction jobs — one row per ESI job_id, so a completion is
    recorded exactly once however many times the sweep sees it. Shape and machinery are shared with
    manufacturing (app.completions); only the valuation below is reaction-specific."""
    completions.ensure_ledger(LEDGER, "idx_rxcomp_ctx")


def log_reaction_completions(context_id: int) -> int:
    """Record reaction jobs for this context that FINISHED since the last sweep. Scan, dedupe and
    insert are shared (app.completions); this supplies the reaction VALUATION.

    A reachable product is valued through the full recipe roll-up (materials + job fees) via
    `_value_reaction_batch`. An unreachable one — goo the price sheet doesn't cover — still
    contributes turnover from raw output x sell price, with net left at 0 rather than overstated
    from an input cost we can't actually compute."""
    ensure_industry_jobs_table()
    ensure_reaction_completions_table()
    pending = completions.pending_completions(
        context_id, "pp_char_industry_jobs", LEDGER, ("active", "paused", "ready"))
    if not pending:
        return 0

    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    pi = load_pi_data()
    types = loaded[4] if loaded else pi["types"]
    settings = effective_reaction_settings(context_id)
    con = get_connection()
    try:
        out_qty_by_type = {r["output_type_id"]: r["output_qty"]
                           for r in con.execute("SELECT output_type_id, output_qty FROM reactions")}
    finally:
        con.close()
    prod_ids = list({tid for _, _, tid, _, _ in pending if tid})
    market = resolve_market_data(context_id, prod_ids) if prod_ids else {}

    valued = []
    for jid, cid, tid, runs, end_ts in pending:
        node = reached.get(tid)
        sell = (market.get(tid) or {}).get("sell_price", 0.0) or 0.0
        vol = (types.get(tid, {}) or {}).get("volume", 0.0) or 0.0
        if node and node.get("via") and runs > 0:
            total_out = runs * node["via"]["output_qty"]
            v = _value_reaction_batch(node, total_out, sell_price=sell, volume=vol, settings=settings)
            out_val, inp_cost = v["output_value"], v["input_cost"] + v["job_cost"]
        else:
            out_val = (runs * out_qty_by_type.get(tid, 0.0) * sell) if runs > 0 else 0.0
            inp_cost = 0.0
        valued.append((jid, cid, tid, runs, end_ts, out_val, inp_cost))
    return completions.record_completions(LEDGER, context_id, valued)


def log_all_reaction_completions() -> int:
    """Sweep every context that tracks reaction jobs. Scheduled every 15 min alongside the
    notification check; per-context failures are isolated."""
    ensure_industry_jobs_table()
    return completions.sweep_all("pp_char_industry_jobs", log_reaction_completions, "reaction")


@router.get("/api/reactions/lifetime")
def reactions_lifetime(context_id: int = Depends(require_context)):
    """This account's lifetime reaction ledger: turnover (Σ produced output value), net profit
    (Σ output − materials − job fees), job count, and the earliest logged completion."""
    ensure_reaction_completions_table()
    return completions.lifetime(LEDGER, context_id)


def _tidy_runs_on(context_id: int) -> bool:
    """Gated: rounding buys a little surplus intermediate with real ISK, and it changes the numbers
    on a plan people are already used to reading."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("reactions_tidy_runs", context_id)
    except Exception:
        return False


def _level_runs_on(context_id: int) -> bool:
    """Gated: levelling a product across characters is the only pass that CREATES and DELETES plan
    rows, so it changes slot counts as well as numbers. Off, `level_stage_runs` still levels within
    a character with the row count fixed."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("reactions_level_runs", context_id)
    except Exception:
        return False


def _insert_assignment_rows(con, character_id: int, type_id: int, name: str, runs: float,
                             job_count: int, input_cost: float, reward: float, tier_order: int,
                             now: float, order_id: int | None = None, tidy: bool = False) -> None:
    """One product's worth of a job commitment, split into `job_count` separate assignment rows
    (one per actual in-game job install — see assign_reaction's own docstring for why). Shared by
    assign_reaction (order_id always None there — no behavior change) and the customer-order
    assign flow (_allocate_and_insert, order_id set) so the row-insertion shape can't drift
    between the two callers."""
    job_count = max(1, job_count)
    runs_per_job = math.ceil(runs / job_count)
    # `tidy` is passed for INTERMEDIATE steps only. Their output is consumed by the stage above, so
    # a little surplus is stock rather than waste — whereas the top-level product's run count is
    # what the batch's cost, output and profit were all computed from, and moving it would make
    # every one of those figures a lie. See `tidy_runs`.
    if tidy:
        runs_per_job = tidy_runs(runs_per_job)
    for _ in range(job_count):
        con.execute(
            "INSERT INTO pp_reaction_assignments "
            "(character_id, type_id, name, runs, input_cost, reward, created_at, tier_order, order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (character_id, type_id, name, runs_per_job, input_cost / job_count, reward / job_count,
             now, tier_order, order_id),
        )


class ChainTier(BaseModel):
    type_id: int
    name: str
    runs: int
    job_count: int = 1
    # Which STAGE this step belongs to — steps sharing one have no dependency on each other and run
    # side by side. Optional so an older client (or a cached suggestion) still assigns; absent, the
    # server derives it from the graph, and only failing that falls back to list position, which is
    # what used to be the only rule and is what serialised siblings.
    tier: int | None = None


def restage_plan_rows(context_id: int) -> int:
    """Re-derive `tier_order` for plan rows written under the old "position in the list is the
    stage" rule. Returns how many rows moved.

    Every insert path used `enumerate(...)`, so three steps that run at the same time were stamped
    stages 0/1/2 — the dashboard greyed two of them out as "wait for the one above", and
    `_concurrent_load` counted three simultaneous jobs as one reactor. The rows already in the
    table say that, and nobody should have to clear their plan to get the truth back.

    Idempotent and cheap after the first pass: a row is only rewritten when the graph disagrees
    with what is stored, so a repaired account does no writes and the graph is only loaded when
    there is a row that could be wrong. No graph (unpriced/unreachable) means no repair — the
    stored value stands rather than being replaced by a guess.
    """
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT a.id, a.character_id, a.type_id, a.created_at, COALESCE(a.tier_order,0) AS tier_order "
            "FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id=?", (context_id,))]
    except Exception:
        return 0
    finally:
        con.close()
    if not rows:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["character_id"], round(float(r["created_at"] or 0.0), 3)), []).append(r)
    # Only a group holding several DISTINCT stages can be mis-staged; a flat group is already right.
    suspect = [g for g in groups.values() if len({r["tier_order"] for r in g}) > 1]
    if not suspect:
        return 0
    try:
        loaded = _load_goo_and_reached(context_id)
        reached = loaded[1] if loaded else {}
    except Exception:
        reached = {}
    if not reached:
        return 0

    fixed = 0
    con = get_connection()
    try:
        for g in suspect:
            depths = {r["id"]: int((reached.get(int(r["type_id"])) or {}).get("depth") or 0)
                      for r in g}
            if not all(depths.values()):
                continue                      # a step we cannot place — leave the whole group alone
            order = {d: i for i, d in enumerate(sorted(set(depths.values())))}
            for r in g:
                want = order[depths[r["id"]]]
                if want != r["tier_order"]:
                    con.execute("UPDATE pp_reaction_assignments SET tier_order=? WHERE id=?",
                                (want, r["id"]))
                    fixed += 1
        if fixed:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return fixed


def chain_stage_state(rows: list[dict], jobs: list[dict], now: float) -> list[dict]:
    """Per-chain stage progress for one character: `[{stage, steps, done, running, todo, ready,
    names}]`, one entry per stage of each chain the character is holding.

    **What makes a stage DONE is a finished job, read from ESI.** An industry job the game reports
    as `ready` (finished, output not collected) or `delivered` (collected), or whose `end_date` has
    passed, is work that is over — that is the signal the player asked for, and it needs no button.
    A job still `active`/`paused` is in progress; a planned row with no job at all is not started.

    **A stage is READY when every stage below it in its own chain is done.** Stage 1 is always
    ready. This is the answer to "can I start the next lot yet" — before this, the dashboard could
    only say "after stage 1 finishes" and leave the player to work out whether it had.

    Chains are grouped the way every other read groups them: the assign that wrote them
    (`created_at`), so two separate plans on one character don't gate each other.
    """
    done_types: dict[int, int] = {}
    live_types: dict[int, int] = {}
    for j in jobs:
        tid = j.get("product_type_id")
        if not tid:
            continue
        status = (j.get("status") or "").lower()
        finished = status in ("ready", "delivered")
        if not finished and status in ("active", "paused"):
            # ESI's own `end_date` is an ISO string (same parse the countdown uses below). A job
            # past its end date is finished whatever the cached status says — the cache is up to
            # five minutes stale, and "is stage 1 done" should not wait on a refresh.
            end = j.get("end_date")
            try:
                finished = bool(end) and datetime.fromisoformat(
                    str(end).replace("Z", "+00:00")).timestamp() <= now
            except Exception:
                finished = False
        (done_types if finished else live_types)[tid] = \
            (done_types if finished else live_types).get(tid, 0) + 1

    out: list[dict] = []
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("character_id"), round(float(r.get("created_at") or 0.0), 3)),
                          []).append(r)
    for g in groups.values():
        by_stage: dict[int, list[dict]] = {}
        for r in g:
            by_stage.setdefault(int(r.get("tier_order") or 0), []).append(r)
        lower_all_done = True
        for stage in sorted(by_stage):
            steps = by_stage[stage]
            done = running = 0
            for r in steps:
                tid = int(r["type_id"])
                if done_types.get(tid, 0) > 0:
                    done_types[tid] -= 1
                    done += 1
                elif live_types.get(tid, 0) > 0:
                    live_types[tid] -= 1
                    running += 1
            entry = {
                "chain": round(float(steps[0].get("created_at") or 0.0), 3),
                "stage": stage, "steps": len(steps), "done": done, "running": running,
                "todo": len(steps) - done - running,
                # Startable now: nothing below it is unfinished. The first stage always qualifies.
                # A pooled plan needs a second condition on top of this one — see
                # `_gate_stages_account_wide`, applied by the caller.
                "ready": lower_all_done,
                "names": sorted({str(r.get("name") or r["type_id"]) for r in steps}),
            }
            out.append(entry)
            lower_all_done = lower_all_done and done == len(steps)
    return out


def level_stage_runs(context_id: int) -> int:
    """Give every job of ONE product, in ONE stage, on ONE character the SAME run count. Returns
    how many rows changed.

    The complaint this exists for: "for Carbon Fibers I see 125 runs, 90 runs, 75 runs — it's all
    over the place". Those are three separate assigns, each of which sized its own chain's Carbon
    Fiber requirement exactly and correctly. Nothing was wrong with any one of them; what was wrong
    was reading three numbers off the screen and typing three different values into three
    consecutive jobs on the same character.

    **Why levelling them is sound and not a fudge:** the product is fungible. Carbon Fiber made for
    one chain is Carbon Fiber, it lands in the same hangar, and the stage above draws from the pool
    rather than from a particular job. So only the TOTAL for a (character, stage, product) has to
    hold, and how it is split across that product's jobs is ours to choose. The total is preserved,
    or rounded UP when the split doesn't divide evenly — never down, which would leave the stage
    above short.

    Deliberately conservative about everything else:

      * **row count is untouched**, so no slot is claimed or released and the capacity every other
        part of this package computed still holds;
      * **rows keep their own chain and order**, so `_shopping_roots`, `chain_stage_state` and the
        per-order give-back all still see exactly the plans they saw before;
      * it only writes when the numbers actually differ, so it is idempotent and free on a plan
        that is already level.
    """
    ensure_reaction_assignments_table()
    tidy = _tidy_runs_on(context_id)
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT a.id, a.character_id, a.type_id, a.runs, COALESCE(a.tier_order,0) AS tier_order "
            "FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id=?", (context_id,))]
    except Exception:
        return 0
    finally:
        con.close()
    if not rows:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["character_id"], r["tier_order"], r["type_id"]), []).append(r)

    changed = 0
    con = get_connection()
    try:
        for g in groups.values():
            if len(g) < 2:
                continue
            total = sum(int(x["runs"] or 0) for x in g)
            per = -(-total // len(g))               # ceil: never leave the stage above short
            if tidy:
                per = tidy_runs(per)
            if all(int(x["runs"] or 0) == per for x in g):
                continue                            # already one number — nothing to write
            for x in g:
                con.execute("UPDATE pp_reaction_assignments SET runs=? WHERE id=?", (per, x["id"]))
                changed += 1
        if changed:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return changed


# ── One run count per product, across every character (`reactions_level_runs`) ─────────────────
#
# `level_stage_runs` above levels a product WITHIN one character, which is as far as it can go
# without touching the row count. The complaint that outlives it is the cross-character one:
# Carbon Fiber at 125 runs on one character, 90 on the next two and 75 on the fourth, and the same
# shape on every other product. Four numbers to read, four to type, four different finish times.
#
# The priority order is the user's own (TODO 28), and it governs strictly:
#   1. ONE run count per product — every job of it, wherever it sits.
#   2. Aligned end times — a stage lands in one go, one login collects it.
#   3. Fewer slots — the same work in fewer, fuller jobs.
# How much surplus a levelled product may produce. Deliberately WIDER than the 15% `tidy_runs`
# works to, and the reason is the user's, stated plainly: *"it's fine to build a bit too much if it
# doesn't line up."* A budget tight enough to make rounding free is too tight to make a stage land
# together — 35 runs of Oxy-Organic Solvents beside 18 has no common count inside 15% at all, so
# the product kept two numbers, which quietly ranked "cheap" above "one number" and inverted the
# priority list. Surplus is stock, and the next plan spends it (`reactions_use_stock`).
#
# What stops it running away is not the budget but WHEN it is allowed to be spent: see
# `_choose_stage_layout`, which pays surplus only to land a stage together and otherwise takes the
# cheapest count there is.
_LEVEL_BUDGET = 0.50
# A stage counts as landing together inside this much of its own longest job. Exact equality is
# not reachable — run counts are integers and cycle times differ — and a stage whose jobs finish
# within a few percent of each other is one login.
_ALIGN_TOL = 0.10
# What the stage solve lets `_level_options` OFFER, before `_stage_affordable` judges the layout as
# a whole. Deliberately far wider than the real budget: a small product's own requirement is the
# wrong yardstick for a run count the whole stage shares, and a candidate that never gets offered
# can never be weighed against what it buys.
_STAGE_SCAN_BUDGET = 20.0


def _typeable(runs: int) -> bool:
    """Is this a run count you copy, or one you read twice? Small numbers are fine as they are, and
    above that it is a round multiple of one of `tidy_runs`' own steps — 70, 125, 250.

    The step has to be worth something at that size — 68 is a multiple of 2 and is not a round
    number, so a step only counts while it is at least a twentieth of the number itself. 70, 125
    and 375 pass; 68 and 67 do not.

    Deliberately NOT `tidy_runs(r) == r`, which asks a different question: whether r would be
    rounded up FURTHER (70 becomes 75 inside a 15% budget). That says nothing about whether 70 is
    easy to type, and it is."""
    runs = int(runs)
    return runs < 10 or any(runs % s == 0 and s * 20 >= runs for s in _TIDY_STEPS)


def _level_options(total: int, cap: int, max_runs: int, budget: float = _LEVEL_BUDGET,
                    min_runs: int = 0, extra: list[int] | None = None) -> list[dict]:
    """Every run count ONE product could carry on EVERY one of its jobs, and what each costs.

    `total` is the ACCOUNT's requirement for this product in this stage and `cap` the most jobs the
    stage may spend on it. Both are pooled: a product's output lands in a shared hangar, so which
    character makes a given run is a placement question (`level_product_runs` answers it) and not
    something the run count has to respect. What is free to choose here is the SPLIT: jobs of one
    number, `ceil(total/runs)` of them.

    Returned per candidate: the run count, how many jobs it costs, the surplus it produces (rounding
    up is the only direction that is safe — the stage above consumes this one), and whether the
    number is one a human can type without checking.

    `extra` seeds counts from OUTSIDE this product — the durations the rest of its stage is
    considering. Without it a product can only ever propose counts derived from its own
    requirement, so Oxy-Organic Solvents (35 runs needed, candidates topping out at 100) could not
    reach the 120 its stage-mates were settling on however clearly that was the right answer. A
    product cannot join a stage it is not allowed to name.

    Two things get a candidate dropped: more jobs than `cap`, and more than `budget` overshoot. Both
    are a loose sanity range when this is called from the stage solve, which passes a far wider
    budget and judges the real cost across the whole stage instead (`_stage_affordable`) — a small
    product's own requirement is the wrong yardstick for a run count the whole stage shares.

    This is the RANGE of what is affordable, not a recommendation — `_choose_stage_layout` decides
    which of these is worth paying for.
    """
    total = int(total)
    cap = max(1, int(cap))
    if total <= 0:
        return []
    # Every run count that divides the requirement into a whole number of jobs, plus the tidy
    # rounding of each — the only counts worth considering, since anything between two of them
    # produces the same job layout for strictly more surplus.
    cands: set[int] = {int(e) for e in (extra or []) if e and int(e) > 0}
    for j in range(1, cap + 1):
        r = max(1, -(-total // j))
        cands.add(r)
        # ...and the round number just above each, one per step of the tidy ladder, so a layout
        # that costs the same in slots can still be one you copy rather than read.
        for step in _TIDY_STEPS:
            cands.add(-(-r // step) * step)
    out: list[dict] = []
    for r in sorted(cands):
        # `min_runs` is the caller giving ground: the stage solve raises it when the stage was
        # promised more reactors than it has, since a longer run count is fewer jobs.
        if r < max(1, min_runs) or (r > max_runs and r > min_runs):
            continue
        jobs = max(1, -(-total // r))
        if jobs > cap:
            continue                        # more reactors than the stage actually has
        made = jobs * r
        if made - total > total * budget:
            continue                        # tidy numbers are not worth paying for in goo
        out.append({"runs": r, "jobs": jobs, "surplus": made - total, "tidy": _typeable(r)})
    if out:
        return out
    # Nothing affordable, and a product showing three numbers is the thing this exists to remove —
    # so fall back to the SMALLEST count the work can be split into with the reactors there are,
    # whatever that costs in surplus. That count always exists, which is what makes "one number per
    # product" a property of this pass rather than something it manages when the arithmetic is kind.
    floor = max(1, min_runs, -(-total // cap))
    jobs = max(1, -(-total // floor))
    return [{"runs": floor, "jobs": jobs, "surplus": jobs * floor - total, "tidy": _typeable(floor)}]


def _choose_stage_layout(products: dict, prefer_tidy: bool = False) -> dict:
    """Pick one run count per product so the whole STAGE lands together.

    `products` is `{key: {"cycle": hours_per_run, "options": [...], "total": runs_needed}}` for the
    products of a single stage — steps with no dependency on each other, installed in one sitting
    and collected in the next. A job's duration is `runs × cycle`, so once every job of a product
    carries the same run count, aligning the stage is choosing run counts whose durations match.

    Searching over target DURATIONS rather than over run counts directly is what makes alignment
    expressible: for each target every product takes the longest job it can that still lands by
    then, which is also its cheapest in slots.

    **Surplus is spent to LAND a stage, and for nothing else.** A layout whose jobs all finish
    within `_ALIGN_TOL` of each other wins outright, however much it overshoots (inside the budget
    `_level_options` already enforced) — *"it's fine to build a bit too much if it doesn't line
    up."* When no target lines the stage up, the surplus ordering takes over and the cheapest count
    wins. Without that split, a loose budget buys a little alignment for a lot of goo: a product
    that cannot reach the stage's duration gets pushed half way there, paying in full for a stage
    that still doesn't land together.

    `prefer_tidy` (the caller's `reactions_tidy_runs` state) puts a number you can type without
    checking ahead of the surplus, so a stage settles on 70 rather than 67 where that costs only
    surplus. It is not free — the surplus is real goo — which is why it follows the same flag that
    decides whether rounding is worth paying for at all.
    """
    keys = [k for k, p in products.items() if p.get("options")]
    if not keys:
        return {}
    targets = sorted({o["runs"] * products[k]["cycle"] for k in keys for o in products[k]["options"]})
    best_score, best_pick = None, {}
    for d in targets:
        pick = {}
        for k in keys:
            cyc = products[k]["cycle"]
            fits = [o for o in products[k]["options"] if o["runs"] * cyc <= d + 1e-9]
            # Of what fits by `d`, prefer the counts that actually LAND there — then the fewest
            # jobs, then the least surplus. Never simply the largest count that fits: that pays goo
            # for an identical layout (8 jobs of 36 where 8 of 35 was the same five reactors).
            near = [o for o in fits if o["runs"] * cyc >= (1 - _ALIGN_TOL) * d]
            pool = near or fits
            # Nothing this product can do lands by `d` — it takes its shortest, and the stage is
            # simply as spread as that product forces it to be.
            pool = pool or [min(products[k]["options"], key=lambda o: o["runs"])]
            pick[k] = min(pool, key=lambda o: ((o["jobs"], 0 if o["tidy"] else 1, o["surplus"])
                                               if prefer_tidy
                                               else (o["jobs"], o["surplus"], 0 if o["tidy"] else 1)))
        durs = [pick[k]["runs"] * products[k]["cycle"] for k in keys]
        if not _stage_affordable(products, pick):
            continue
        spread = round(max(durs) - min(durs), 6)
        jobs = sum(pick[k]["jobs"] for k in keys)
        surplus = sum(pick[k]["surplus"] for k in keys)
        untidy = sum(0 if pick[k]["tidy"] else 1 for k in keys)
        landed = 0 if spread <= _ALIGN_TOL * max(durs) else 1
        # Slots before surplus, both after landing the stage — fewer, fuller jobs is the whole point
        # ("save slots, lower login cadence"), and the budget above bounds the surplus that buys it.
        # Ranking the goo first is the trap; docs/reactions.md has what it cost.
        score = ((landed, jobs, untidy, surplus, spread) if prefer_tidy
                 else (landed, jobs, surplus, untidy, spread))
        if best_score is None or score < best_score:
            best_score, best_pick = score, pick
    if best_pick:
        return best_pick
    # Every layout the stage could take is over the budget — so take the cheapest one there is
    # rather than leaving the products unlevelled, which is the outcome this pass exists to remove.
    cheap = {k: min(products[k]["options"], key=lambda o: (o["surplus"], o["jobs"])) for k in keys}
    return cheap


def _stage_affordable(products: dict, pick: dict) -> bool:
    """Is this whole-stage layout worth what it overbuilds?

    The budget is judged **per STAGE, not per product** — which is the difference between
    Oxy-Organic Solvents sitting at 35 runs beside its stage-mates at 120, and the whole stage
    reading 120. Reported, exactly: *"there's no reason why it would make 35 oxy when it could make
    120 instead and save slots."* Against its own 207-run requirement, 120 a job is nearly five
    times too much; against the stage's 4,400 runs it is 8% of the batch, and it buys the one thing
    the stage is for — every job the same length, collected in one trip.

    One ceiling, stage-wide: the surplus across everything the stage makes. A product with no
    `total` (the unit tests, which exercise the search itself) is not checked."""
    need = made = 0
    for k, opt in pick.items():
        total = products[k].get("total")
        if total is None:
            return True
        need += int(total)
        made += opt["jobs"] * opt["runs"]
    return need <= 0 or made - need <= need * _LEVEL_BUDGET


def _reaction_cycle_times() -> dict[int, float]:
    """{output_type_id: hours per run} straight from the SDE. The structure/skill time bonus is
    deliberately NOT applied: it scales every reaction by the same factor, and everything here
    compares durations against each other."""
    con = get_connection()
    try:
        return {int(r["output_type_id"]): (float(r["cycle_time"] or 0) / 3600.0) or 1.0
                for r in con.execute(
                    "SELECT output_type_id, MIN(cycle_time) AS cycle_time FROM reactions "
                    "GROUP BY output_type_id")}
    except Exception:
        return {}
    finally:
        con.close()


def level_product_runs(context_id: int) -> int:
    """Give every job of ONE product the SAME run count across EVERY character, and re-split the
    work into as many jobs as that count needs. Returns how many rows were written.

    This is `level_stage_runs` taken to where the complaint actually lives. Levelling within a
    character can only ever move numbers around inside one column of the dashboard; the reported
    shape was 125 runs of Carbon Fiber on one character, 90 on the next two, 75 on the fourth —
    and the same on every other product. Choosing 125 for all four costs nothing (it is what the
    busiest character was already doing), makes every job finish at the same time, and takes the
    fifteen jobs down to twelve.

    **What it may not touch.** A chain's intermediate is consumed by the job above it ON THE SAME
    CHARACTER, so a chain's own requirement is a floor and work never moves between characters —
    only the split changes. The TOP row of every chain is left exactly as it is: its run count is
    what the batch's cost, output value and profit were computed from, and it is what a customer
    order gives back when it is cancelled. And a chain never loses its last row of a product: the
    dashboard reads readiness per chain (`chain_stage_state`), so a chain that stopped mentioning
    a product it is waiting on would announce the stage above as ready while those jobs ran.

    That last rule is also the limit of this pass: two separate chains on ONE character still get
    a job each rather than one shared job, even though the output is fungible and lands in the same
    hangar. Sharing needs chain identity reworked first (a real `chain_id`, so a row can belong to
    more than one chain) — see TODO 28.

    Rows keep their chain (`created_at`), stage (`tier_order`) and order, so `_shopping_roots`,
    `chain_stage_state` and the per-order give-back all still see what they saw before. Cost and
    reward are re-split across the new row count, never re-totalled. Idempotent: a plan that is
    already level is read and not written.
    """
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT a.id, a.character_id, a.type_id, a.name, a.runs, a.input_cost, a.reward, "
            "a.created_at, a.order_id, COALESCE(a.tier_order,0) AS tier_order "
            "FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id=?", (context_id,))]
    except Exception:
        return 0
    finally:
        con.close()
    if not rows:
        return 0

    # What may NOT be re-shaped: the TOP row of a customer ORDER's chain. Its run count is the batch
    # that order was quoted and priced on, and cancelling the order hands exactly those runs back
    # (`give_back_order_runs`), so moving it would make the order's own arithmetic wrong.
    #
    # Everything else is fair game — an order's intermediates, and the top row of a speculative
    # chain. Both were excluded once and both exclusions left a product showing several numbers
    # after the pass had run; see docs/reactions.md, "A speculative chain's TOP row is levelled too".
    top_of_chain: dict[tuple, int] = {}
    for r in rows:
        key = (r["character_id"], round(float(r["created_at"] or 0.0), 3))
        top_of_chain[key] = max(top_of_chain.get(key, 0), int(r["tier_order"] or 0))
    inner = [r for r in rows
             if not (r.get("order_id")
                     and int(r["tier_order"] or 0) == top_of_chain[
                         (r["character_id"], round(float(r["created_at"] or 0.0), 3))])]
    if not inner:
        return 0

    cycles = _reaction_cycle_times()
    caps_now = {c["character_id"]: c for c in _character_capacities(context_id)}
    # What the plan may hold on a character, counting EVERY planned job on it, not just the busiest
    # stage: reactors it has, minus the jobs really running in game. `free_slots` is the peak-tier
    # number — right for "what can I start now", and wrong here, because it let this pass grow
    # stage 1 into reactors that stage 2 was already holding a row in. Reported as "12 slots
    # assigned to characters that only have 10". A row is a line in the plan whether or not it can
    # be installed yet, and the plan must fit the reactors.
    room = {cid: max(0, int(c.get("slots") or 0) - int(c.get("running") or 0))
            for cid, c in caps_now.items()}

    # (stage, product) -> every row of it, POOLED ACROSS CHARACTERS: the requirement is the
    # ACCOUNT's, the run count is the product's, and which character holds a job is just where there
    # was room. That is what makes this pass slot-efficient rather than merely tidy, and two things
    # elsewhere are corrected to match it — `_shopping_roots` (a moved row must not be mistaken for
    # a chain of its own and bought for twice) and `_gate_stages_account_wide` (a chain no longer
    # holds every stage it waits on). docs/reactions.md, "A product is POOLED across characters".
    stages: dict[int, dict[int, list[dict]]] = {}
    for r in inner:
        stages.setdefault(int(r["tier_order"] or 0), {}).setdefault(int(r["type_id"]), []).append(r)

    prefer_tidy = _tidy_runs_on(context_id)
    rows_by_char_stage: dict[int, dict[int, int]] = {}
    for stage, by_product in stages.items():
        for rs in by_product.values():
            for r in rs:
                cid = r["character_id"]
                rows_by_char_stage.setdefault(cid, {})[stage] = \
                    rows_by_char_stage.get(cid, {}).get(stage, 0) + 1

    plan: list[tuple] = []          # (product_key, rows, target_runs, [character per job])
    # Jobs this pass has already placed on a character, stage by stage. Reading the ORIGINAL row
    # counts for every stage instead let stage 0 fill a character up and stage 1 then fill it again
    # from the same starting point — 17 jobs on an 11-slot character. Stages are walked in order so
    # what an earlier one took is a fact by the time a later one asks.
    committed: dict[int, int] = {}
    for stage in sorted(stages):
        by_product = stages[stage]
        later = {cid: sum(n for st, n in per.items() if st > stage)
                 for cid, per in rows_by_char_stage.items()}
        # A stage may be stretched to land together but never past the job that is already the
        # longest in it — levelling then makes a plan tidier and shorter, never slower, which is the
        # only safe thing to do with a number nobody chose.
        stage_cap_hours = max(
            (int(r["runs"] or 0) * cycles.get(int(r["type_id"]), 1.0)
             for rs in by_product.values() for r in rs),
            default=0.0)
        # A character's reactors are ONE pool and every product in the stage draws from it, so the
        # stage is solved as a whole (the give-ground loop below). Per stage rather than across all
        # of them, which is right under the one-slot model: a character's load is its busiest stage,
        # so growth in stage 0 and stage 1 does not add up — this stage may use whatever the
        # character's reactors are not already committed to by its OTHER stages.
        budget = {cid: max(0, n - committed.get(cid, 0) - later.get(cid, 0))
                  for cid, n in room.items()}

        # The shortest this stage can possibly run: all of its work, divided by every reactor it may
        # use. Nothing shorter is installable however the run counts are chosen, so it seeds the
        # floor and the give-ground loop below only has to fix rounding — without it that loop
        # started from the asked-for length and stepped one run at a time, which never got from
        # "5 days" to what the reactors could do.
        stage_room = max(1, sum(budget.values()))
        stage_work = sum(sum(int(r["runs"] or 0) for r in rs) * cycles.get(tid, 1.0)
                         for tid, rs in by_product.items())
        d_floor = stage_work / stage_room

        floor_runs: dict[int, int] = {}     # per product, raised when a character is over-promised
        for tid in by_product:
            cyc = cycles.get(tid, 1.0)
            if cyc > 0 and d_floor > 0:
                floor_runs[tid] = max(1, int(d_floor / cyc))
        layout: dict[int, dict] = {}
        products: dict[int, dict] = {}
        for _attempt in range(24):
            def _build(extra_hours: set[float]) -> dict:
                built: dict[int, dict] = {}
                for tid, rs_all in by_product.items():
                    cyc = cycles.get(tid, 1.0)
                    # ONE pooled requirement per product: the account needs this many runs of it,
                    # and any reactor with room can make them.
                    total = sum(int(r["runs"] or 0) for r in rs_all)
                    max_runs = int(stage_cap_hours / cyc) if cyc > 0 else total
                    # The counts that would land this product on a duration the rest of the stage
                    # is considering — see `_level_options`' `extra`.
                    extra = [max(1, int(h / cyc)) for h in extra_hours if cyc > 0]
                    built[tid] = {
                        "cycle": cyc, "total": total,
                        # Wide here on purpose: what a run count really costs is judged across the
                        # whole stage (`_stage_affordable`), not against this one product's needs.
                        "options": _level_options(total, stage_room, max_runs,
                                                  budget=_STAGE_SCAN_BUDGET,
                                                  min_runs=floor_runs.get(tid, 0), extra=extra),
                    }
                return built

            # Two passes: size every product on its own terms, then re-offer each of them the
            # durations the others turned out to be considering, so a stage can settle on ONE
            # length rather than each product landing wherever its own arithmetic happened to.
            products = _build(set())
            durations = {o["runs"] * p["cycle"] for p in products.values() for o in p["options"]}
            products = _build(durations)
            layout = _choose_stage_layout(products, prefer_tidy)
            asked = sum(opt["jobs"] for opt in layout.values())
            if asked <= stage_room:
                break
            # The stage wants more reactors than the account has free for it, so the greediest
            # product gives ground — and it gives it in one step, the NEXT run count that actually
            # changes the layout, not one more run, which would take a hundred passes to matter.
            worst_tid = max(layout, key=lambda t: layout[t]["jobs"])
            cur = layout[worst_tid]["runs"]
            higher = [o["runs"] for o in products[worst_tid]["options"] if o["runs"] > cur]
            nxt = min(higher) if higher else cur + max(1, cur // 4)
            if nxt <= floor_runs.get(worst_tid, 0):
                break                       # no room left to give — take what we have
            floor_runs[worst_tid] = nxt
        # WHERE the jobs go. A character that already runs the product keeps it (no move for its
        # own sake), then whoever has the most room — so consolidating a product onto fewer
        # reactors moves as few rows as it can.
        room_left = dict(budget)
        for tid, opt in sorted(layout.items(), key=lambda kv: -kv[1]["jobs"]):
            rs_all = sorted(by_product[tid], key=lambda r: r["id"])
            want = max(1, opt["jobs"])
            have: dict[int, int] = {}
            for r in rs_all:
                have[r["character_id"]] = have.get(r["character_id"], 0) + 1
            # Who runs it: the characters already running it keep as much of it as their room
            # allows, and only what is left over goes anywhere new. Placing purely by "most room
            # first" was not STABLE — the next pass saw the new distribution, re-sorted, and moved
            # twelve rows again for an identical layout, on every dashboard load.
            quota: dict[int, int] = {}
            left = want
            for cid in sorted(have, key=lambda c: (-have[c], -room_left.get(c, 0), c)):
                take = min(have[cid], max(0, room_left.get(cid, 0)), left)
                if take:
                    quota[cid] = take
                    room_left[cid] -= take
                    left -= take
            for cid in sorted(room_left, key=lambda c: (-room_left[c], c)):
                if left <= 0:
                    break
                take = min(max(0, room_left[cid]), left)
                if take:
                    quota[cid] = quota.get(cid, 0) + take
                    room_left[cid] -= take
                    left -= take
            if left > 0:
                continue        # nowhere to put it all — leave this product exactly as it is
            # Rows stay put wherever the quota allows; the overflow are the ones that move, and
            # the rows past `want` are the ones that go. Both fall out of this ordering.
            q = dict(quota)
            stay, move = [], []
            for r in rs_all:
                cid = r["character_id"]
                if q.get(cid, 0) > 0:
                    q[cid] -= 1
                    stay.append(r)
                else:
                    move.append(r)
            spots = [r["character_id"] for r in stay]
            for cid in sorted(q):
                spots += [cid] * q[cid]
            for cid in spots:
                committed[cid] = committed.get(cid, 0) + 1
            plan.append(((stage, tid), stay + move, opt["runs"], spots))

    changed = 0
    con = get_connection()
    try:
        for _pkey, rs, runs, spots in plan:
            jobs = len(spots)
            keep = rs[:jobs]
            drop = rs[jobs:]
            if (jobs == len(rs) and all(int(r["runs"] or 0) == runs for r in rs)
                    and [r["character_id"] for r in rs] == spots):
                continue                    # already one number, in the right jobs, in the right
                                            # hands — nothing to write
            # Cost and profit are LINEAR in runs, so they scale with the work rather than being
            # re-split across it. A chain's intermediate rows carry 0 either way (the whole chain's
            # cost rolls up into its top row), but a top row carries the real ISK — and after this
            # pass it may be making more than it was asked for. Dividing the old total across the
            # new jobs would report the same profit for more goo bought.
            was = sum(int(r["runs"] or 0) for r in rs) or 1
            scale = (runs * jobs) / was
            cost = sum(float(r["input_cost"] or 0.0) for r in rs) * scale / jobs
            reward = sum(float(r["reward"] or 0.0) for r in rs) * scale / jobs
            for i, r in enumerate(keep):
                # ...and `character_id`, because a pooled product's jobs go where there is room.
                # Moving a row rather than deleting and re-inserting keeps its id, its chain and
                # its order, so everything reading those still sees the plan it saw before.
                con.execute("UPDATE pp_reaction_assignments SET runs=?, input_cost=?, reward=?, "
                            "character_id=? WHERE id=?",
                            (runs, cost, reward, spots[i], r["id"]))
                changed += 1
            for r in drop:
                con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (r["id"],))
                changed += 1
            proto = rs[0]
            for cid in spots[len(keep):]:
                con.execute(
                    "INSERT INTO pp_reaction_assignments "
                    "(character_id, type_id, name, runs, input_cost, reward, created_at, "
                    "tier_order, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, proto["type_id"], proto["name"], runs, cost, reward,
                     proto["created_at"], proto["tier_order"], proto["order_id"]))
                changed += 1
        if changed:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return changed


def _stage_of_tiers(context_id: int, product_id: int, tiers: list) -> list[int]:
    """A STAGE index per client-supplied chain tier — steps that can run at the same time share one.

    Three sources, in order of how much they know:

      1. the tier's own `tier`, when the client sent one (the wizard and the opportunity list now
         both carry it, computed from the graph's dependency depth);
      2. the GRAPH, re-derived here — the authority, and what repairs a client that didn't send it;
      3. list position, the old rule, kept only as a last resort for a chain we cannot resolve at
         all. It is wrong for siblings, which is the whole reason this function exists.

    Never trusts (1) blindly over (2) when the two disagree in shape: a stamped stage that skips a
    number (0, 2) would leave an empty stage on the dashboard, so the values are re-densified.
    """
    if not tiers:
        return []
    stages: list[int] | None = None
    stamped = [t.tier for t in tiers]
    if all(s is not None for s in stamped):
        stages = [int(s) for s in stamped]
    else:
        try:
            loaded = _load_goo_and_reached(context_id)
            reached = loaded[1] if loaded else {}
            node = reached.get(int(product_id)) or {}
            if node.get("via"):
                depth_of = {int(tid): int((reached.get(int(tid)) or {}).get("depth") or 1)
                            for tid, _ in [(t.type_id, None) for t in tiers]}
                stages = [depth_of.get(int(t.type_id), 1) for t in tiers]
        except Exception:
            stages = None
    if stages is None:
        stages = list(range(len(tiers)))            # last resort — see the docstring
    # Densify: whatever the source, stages must be 0..n with no gaps, in dependency order.
    order = {d: i for i, d in enumerate(sorted(set(stages)))}
    return [order[d] for d in stages]


def _trim_tiers_by_stock(context_id: int, tiers: list) -> list[dict]:
    """Spend held intermediates against a caller-supplied chain, IN PLACE, and report what that
    covered as `[{type_id, name, units, runs_saved}]`.

    Each tier's requirement is its `runs x output_qty`; stock is taken off the front of that and the
    runs recomputed, with a fully-covered tier dropped from the list entirely. Deliberately does NOT
    recurse: by the time a chain reaches this function it is already flat (`_ordered_chain_tiers`
    exploded it), so a tier this drops may leave its own feeder tiers behind — those are still real
    work whose output is still consumed by the tiers above them, just not by this one. The planning
    paths that build the chain themselves (`_ordered_chain_tiers` with a pool) do the recursive
    version, and they are what the wizard and orders use.

    No pool (flag off) ⇒ the list is untouched and this returns [].
    """
    pool = reaction_stock_pool(context_id)
    if not pool or not tiers:
        return []
    # How many units a run of this product yields must come from the formula the PLAN is using —
    # `reached[tid]["via"]` — not from an arbitrary row of `reactions`. Several formulas can output
    # the same product with wildly different batch sizes (20 units vs 10,000 in this SDE), and
    # reading the wrong one misjudges coverage by that same factor, in whichever direction the row
    # order happens to fall. The graph load is cached and this only runs on a user-initiated assign
    # with the feature on and stock present.
    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    if not reached:
        return []                       # no graph, no evidence — leave the caller's chain alone

    covered: list[dict] = []
    keep = []
    for t in tiers:
        via = (reached.get(int(t.type_id)) or {}).get("via") or {}
        qty = float(via.get("output_qty") or 0)
        have = pool.get(int(t.type_id), 0.0)
        if qty <= 0 or have <= 0:
            keep.append(t)
            continue
        needed = t.runs * qty
        used = min(have, needed)
        pool[int(t.type_id)] = have - used
        left_runs = math.ceil((needed - used) / qty)
        covered.append({"type_id": int(t.type_id), "name": t.name, "units": round(used, 1),
                        "runs_saved": t.runs - max(0, left_runs)})
        if left_runs > 0:
            t.runs = left_runs
            t.job_count = max(1, min(t.job_count, left_runs))
            keep.append(t)
    tiers[:] = keep
    return [c for c in covered if c["runs_saved"] > 0]


class AssignRequest(BaseModel):
    character_id: int
    type_id: int
    name: str
    runs: int  # total runs across all jobs for this suggestion
    job_count: int = 1  # how many separate in-game job installs this splits into (one per slot)
    input_cost: float
    reward: float
    # Intermediate reactions this product's own formula needs (see _explode_chain_tiers in
    # _suggest_reactions), deepest-first — each becomes its own set of assignment rows the
    # player must install and let finish BEFORE the top-level reaction above can even start.
    chain_tiers: list[ChainTier] = []


def _assign_guards_on(context_id: int) -> bool:
    """Gated: both halves change what a repeated press DOES. Replacing rather than appending is
    almost always what was meant, but "almost" is why it rolls out rather than lands."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("reactions_assign_guard", context_id)
    except Exception:
        return False


def _clear_assignment_group(con, character_id: int, type_id: int, tier_order: int) -> int:
    """Drop this character's existing plan rows for one (product, tier) — the rows a re-assign is
    about to rewrite. Returns how many went.

    **`order_id IS NULL` only.** Rows raised by the customer-order flow belong to an order that was
    committed against real capacity; a suggestion re-assign must not silently eat them.
    """
    cur = con.execute(
        "DELETE FROM pp_reaction_assignments WHERE character_id=? AND type_id=? AND tier_order=? "
        "AND order_id IS NULL", (character_id, type_id, tier_order))
    return cur.rowcount or 0


def _assigned_slot_capacity(con, character_id: int) -> int:
    """This character's total reaction slots, or 0 when we cannot tell (never scanned).

    0 means "unknown", and unknown never refuses: the same rule the print caps follow — a cap built
    on absent evidence blocks work the player can really do.
    """
    row = con.execute(
        "SELECT character_id, mass_reactions, advanced_mass_reactions, scopes FROM pp_characters "
        "WHERE character_id=?", (character_id,)).fetchone()
    if not row:
        return 0
    try:
        return int(reaction_slots(row)) if reaction_capable(row)[0] else 0
    except Exception:
        return 0


def _parallel_stages_on(context_id: int) -> bool:
    """Gate for ONE slot model (`reactions_parallel_stages`). Off ⇒ `_character_capacities` and the
    dashboard count every planned row against the pool exactly as they always did.

    Why a flag for what is arguably a bug fix: it makes free-slot counts BIGGER, so the wizard and
    customer orders will schedule more work per character. That is the point, and it is also a
    planning-behaviour change on live accounts — CLAUDE.md rule 2.
    """
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("reactions_parallel_stages", context_id)
    except Exception:
        return False


def _concurrent_load(rows: list[dict], adding: dict[int, int] | None = None) -> int:
    """Peak slots a character's plan actually occupies at once.

    **Chain tiers are SEQUENTIAL** — tier 0 must finish before tier 1 can start — so counting every
    row against the slot pool would reject legitimate deep chains that never run simultaneously.
    What competes for slots is everything sharing a tier_order, so the load is the WORST tier, not
    the sum. `adding` is {tier_order: rows} for the assignment being considered.

    **This is THE slot model** — the assign guard, `_character_capacities` and the dashboard's own
    free-slot count all go through it. They used not to: the guard counted the worst tier while the
    other two counted every row, so a 3-stage chain of one job each was authorised as needing one
    slot and then reported as occupying three, and the allocators that read those numbers quietly
    planned less work than the account had reactors for.
    """
    per_tier: dict[int, int] = {}
    for r in rows:
        t = int(r.get("tier_order") or 0)
        per_tier[t] = per_tier.get(t, 0) + 1
    for t, n in (adding or {}).items():
        per_tier[t] = per_tier.get(t, 0) + n
    return max(per_tier.values(), default=0)


@router.post("/api/reactions/assign")
def assign_reaction(req: AssignRequest, context_id: int = Depends(require_context)):
    """Commit a suggested (character, product) pairing as standing "go do this" instructions —
    surfaced on the dashboard until ESI confirms a matching job is actually running, at which
    point it's auto-cleared (see get_industry_jobs). A suggestion sized to use multiple reaction
    slots at once (job_count > 1, e.g. a big batch that needs several parallel jobs to finish
    within the chosen cadence) becomes that many SEPARATE assignment rows — one per actual
    in-game job install — so the dashboard shows the real number of slots this occupies, not one
    square standing in for several.

    Any chain_tiers (intermediate reactions this product's own formula needs, e.g. goo ->
    Ferrofluid -> this product — see _explode_chain_tiers) get their own assignment rows too,
    tagged with a LOWER tier_order so the dashboard can show them as "react this first." Their
    input_cost/reward are recorded as 0 — the full chain's cost/profit is already rolled up into
    the top-level row (unit_cost is computed recursively down to raw goo), so giving the
    intermediate rows their own nonzero values would double-count it if anything ever sums
    pp_reaction_assignments financially. (Expected output value is priced LIVE off current
    market data in get_industry_jobs, not stored here — see that function's own notes on why.)"""
    ensure_reaction_assignments_table()
    # Chain tiers arrive from the CLIENT (the wizard's suggestion, or the manual modal scaling an
    # opportunity's recipe), and the opportunity list is deliberately stock-blind — it is cached and
    # its tiers get scaled linearly, which stock coverage is not. So the trim happens here, at the
    # one point every path passes through to create rows: whatever the caller asked for, a stage the
    # hangar already covers is not committed to a reactor.
    stock_covered = _trim_tiers_by_stock(context_id, req.chain_tiers)
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")

        now = _time.time()
        tier_of = _stage_of_tiers(context_id, req.type_id, req.chain_tiers)
        top_tier_order = (max(tier_of) + 1) if tier_of else 0
        guarded = _assign_guards_on(context_id)
        replaced = 0
        if guarded:
            # **Idempotent.** This endpoint was a bare INSERT, so re-posting the same suggestion
            # appended a second full set of rows — and a frontend bug that reported every SUCCESSFUL
            # assign as failed turned two suggestions into 27 rows on a 10-slot character (reported
            # 2026-08-01). The bug is fixed; any transient failure plus a retry could still do it.
            # Replacing the (character, product, tier) group makes a retry a no-op. Deliberate
            # parallelism is unaffected: how many jobs a product runs side by side is `job_count`,
            # which sets the row count WITHIN the group, so it is expressed here and not by pressing
            # the button twice.
            for tier_order, tier in zip(tier_of, req.chain_tiers):
                replaced += _clear_assignment_group(con, req.character_id, tier.type_id, tier_order)
            replaced += _clear_assignment_group(con, req.character_id, req.type_id, top_tier_order)

            # **Capacity.** Nothing stopped the total exceeding the character's real reaction slots,
            # which is how a 10-slot character ended up holding 27 rows. What competes for a slot is
            # everything at the same TIER — tiers are sequential — so a deep chain is not penalised.
            existing = [dict(r) for r in con.execute(
                "SELECT tier_order FROM pp_reaction_assignments WHERE character_id=?",
                (req.character_id,))]
            # Per STAGE, summed: two siblings in one stage really do hold two reactors at once.
            adding: dict[int, int] = {}
            for stage, t in zip(tier_of, req.chain_tiers):
                adding[stage] = adding.get(stage, 0) + max(1, t.job_count)
            adding[top_tier_order] = max(1, req.job_count)
            peak = _concurrent_load(existing, adding)
            slots = _assigned_slot_capacity(con, req.character_id)
            if slots and peak > slots:
                con.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=f"That needs {peak} reaction slots at once and this character has "
                           f"{slots}. Assign fewer jobs, or spread them across characters.")

        tidy = _tidy_runs_on(context_id)
        for tier_order, tier in zip(tier_of, req.chain_tiers):
            _insert_assignment_rows(con, req.character_id, tier.type_id, tier.name, tier.runs,
                                     tier.job_count, 0.0, 0.0, tier_order, now, tidy=tidy)

        _insert_assignment_rows(con, req.character_id, req.type_id, req.name, req.runs,
                                 req.job_count, req.input_cost, req.reward, top_tier_order, now)
        con.commit()
    finally:
        con.close()
    # `stock_covered` is why the plan may hold fewer stages than the caller asked for — returned so
    # the UI can say so rather than leaving a stage to vanish silently.
    return {"ok": True, "replaced": replaced, "stock_covered": stock_covered}


class AdoptOrphanRequest(BaseModel):
    character_id: int
    type_id: int
    runs: int


@router.post("/api/reactions/adopt-orphan")
def adopt_orphan_job(req: AdoptOrphanRequest, context_id: int = Depends(require_context)):
    """Adopt an ORPHAN running job (one installed in-game with no plan slot — see get_industry_jobs'
    `orphan` flag) INTO the recurring plan. Creates the same pp_reaction_assignments rows a normal
    assign would (top-level + any chain tiers), costed entirely server-side from our own SDE recipe
    — ESI only told us the product + run count, the recipe gives everything else. Once adopted it
    matches (covers) the running job, so it stays hidden from "to install" while the job runs and
    reappears as "to install" when the job finishes: it's now part of the recurring loadout and its
    materials join the next-cycle shopping list. An orphan left un-adopted stays a one-off and never
    becomes recurring."""
    ensure_reaction_assignments_table()
    # All own-connection helpers below run BEFORE the write connection is opened — never hold two at
    # once (the 2026-07-13 pool-exhaustion incident).
    loaded = _load_goo_and_reached(context_id)
    if loaded is None:
        raise HTTPException(status_code=400, detail="No priced materials to cost this reaction")
    _goo, reached, _rbo, _ibr, types = loaded
    node = reached.get(req.type_id)
    if not node or not node.get("via"):
        raise HTTPException(status_code=400, detail="Not a reachable reaction product")

    settings = effective_reaction_settings(context_id)
    m = resolve_market_data(context_id, [req.type_id]).get(req.type_id)
    out_qty = node["via"]["output_qty"]
    total_out = req.runs * out_qty
    vol = (types.get(req.type_id, {}).get("volume") or 0.0)
    v = _value_reaction_batch(node, total_out, m["sell_price"] if m else 0.0, vol, settings)
    input_cost = v["input_cost"]   # materials only — matches how plan rows store input_cost
    reward = v["net_profit"]

    # Chain tiers (intermediate reactions this formula needs), same as assign_reaction — recorded at
    # 0 cost since the whole chain's cost already rolls up into the top-level row's unit_cost.
    ordered = _ordered_chain_tiers(node["via"]["inputs"], req.runs, reached,
                                    reaction_stock_pool(context_id))

    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")
        now = _time.time()
        ranks = tier_ranks(ordered)
        for (tier_tid, info), tier_order in zip(ordered, ranks):
            _insert_assignment_rows(con, req.character_id, tier_tid,
                                     types.get(tier_tid, {}).get("name", str(tier_tid)),
                                     info["runs"], 1, 0.0, 0.0, tier_order, now,
                                     tidy=_tidy_runs_on(context_id))
        _insert_assignment_rows(con, req.character_id, req.type_id,
                                 types.get(req.type_id, {}).get("name", str(req.type_id)),
                                 req.runs, 1, input_cost, reward,
                                 (max(ranks) + 1) if ranks else 0, now)
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign/{assignment_id}")
def unassign_reaction(assignment_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT a.id FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE a.id=? AND c.context_id=?",
            (assignment_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Assignment not found")
        con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (assignment_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign")
def unassign_all_reactions(context_id: int = Depends(require_context)):
    """Clear every pending assignment across all of the caller's characters in one go —
    "Clear all" on the dashboard, for starting a fresh suggestion set without hand-cancelling
    each pending slot one at a time.

    **Order-linked rows go too, and their order's counter is put back.** This used to delete them
    and leave `pp_reaction_orders.assigned_runs` untouched, which is the one combination that
    strands an order: it claims its full run count, holds no rows, schedules nothing, and cannot be
    re-assigned because `remaining` is already zero. Its sibling `_clear_assignment_group` skips
    order rows entirely (a suggestion re-assign must not eat a committed order), and that asymmetry
    is deliberate — a per-product re-assign is a narrow action, "Clear all" is the player saying
    clear all. What was neither honest nor useful was clearing them and keeping the number.

    How much to give back: the TOP row of each chain, which is what `assigned_runs` was incremented
    by in the first place (`_allocate_and_insert` returns the sum of the per-host shares, and each
    host's share IS its top-level row's runs). `_shopping_roots` already identifies exactly those
    rows, for exactly the same reason — a chain's top is the row that stands for the batch.

    A row ESI has confirmed as RUNNING is deleted too, as it always was; the job keeps running
    in-game and shows up as an orphan the player can adopt back into the plan.
    """
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    cleared = 0
    orders_reset: list[int] = []
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            rows = [dict(r) for r in con.execute(
                f"SELECT character_id, type_id, runs, order_id, created_at, "
                f"COALESCE(tier_order,0) AS tier_order FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders})", char_ids)]
            cleared = len(rows)
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE character_id IN ({placeholders})",
                        char_ids)
            orders_reset = give_back_order_runs(con, rows)
            con.commit()
    finally:
        con.close()
    return {"ok": True, "cleared": cleared, "orders_reset": orders_reset}


def _gate_stages_account_wide(characters: list[dict]) -> None:
    """A stage is startable only once every stage BELOW it is finished across the whole account.

    `chain_stage_state` answers this per chain, which was the whole truth while a chain's every
    stage lived on the character that would consume it. Pooling ended that (`level_product_runs`
    lays a product out across whichever reactors had room), so a chain can now hold no row at all
    for a stage it is waiting on — and "every step of MY chain below this is done" is then
    vacuously true the moment the plan is made, which would light up "stage 2, start now" while
    the Carbon Fiber for it sat unstarted on somebody else.

    Conservative on purpose: a stage nobody has finished holds back every stage above it, even
    across chains that do not share materials. That matches how the plan is actually worked —
    install a stage, come back, install the next — and being early with "start now" is the failure
    that matters here. Mutates the entries in place; no return.
    """
    pending: dict[int, bool] = {}
    for c in characters:
        for e in c.get("stages") or []:
            if e.get("todo") or e.get("running"):
                pending[int(e.get("stage") or 0)] = True
    if not pending:
        return
    for c in characters:
        for e in c.get("stages") or []:
            stage = int(e.get("stage") or 0)
            if e.get("ready") and any(pending.get(st) for st in range(stage)):
                e["ready"] = False


def _plan_missing_formulas(context_id: int, characters: list[dict]) -> dict:
    """The acquire-list for what is ALREADY planned — every not-yet-running slot on the dashboard.

    Imported at call time: `library.py` is wired into the package after this module, and a
    module-level import here would depend on that ordering. A failure degrades to "nothing to
    report", which is this report's own empty state anyway.
    """
    try:
        from app.reactions.library import (missing_formulas, wanted_from_sequence,
                                            jobs_from_sequence)
        pending = [p for c in characters for p in (c.get("pending") or [])]
        # Every pending row is one in-game job, so the row count per product is how many formulas
        # have to be held at once (`missing_formulas`' `jobs`).
        return missing_formulas(context_id, wanted_from_sequence(pending),
                                jobs=jobs_from_sequence(pending))
    except Exception:
        return {"complete": False, "formulas": [], "unresolved": []}


def give_back_order_runs(con, rows: list[dict]) -> list[int]:
    """Hand a customer order back the runs its deleted plan rows were holding. Returns the ids of
    the orders that moved. Does NOT commit — the caller owns the transaction that deleted the rows,
    and the counter must move with them or not at all.

    **How much comes back is the TOP row of each chain**, which is exactly what `assigned_runs` was
    incremented by (`_allocate_and_insert` returns the sum of the per-host shares, and a host's
    share IS its top-level row's runs). `_shopping_roots` already identifies those rows, for the
    same reason: a chain's top stands for the batch, its tiers are the work underneath.

    Shared by "Clear all" and the per-order clear so the two can never drift into disagreeing about
    what a cleared order is owed — the disagreement that stranded orders #36-#39 in the first place.
    """
    by_order: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("order_id"):
            by_order.setdefault(int(r["order_id"]), []).append(r)
    moved: list[int] = []
    for order_id, order_rows in by_order.items():
        give_back = sum(int(r["runs"] or 0) for r in _shopping_roots(order_rows))
        if give_back <= 0:
            continue
        # Never below zero: the counter is a commitment total, and an order whose rows were already
        # partly cleared elsewhere must not go negative and start claiming capacity it never had.
        con.execute(
            "UPDATE pp_reaction_orders SET assigned_runs = "
            "CASE WHEN assigned_runs > ? THEN assigned_runs - ? ELSE 0 END WHERE id=?",
            (give_back, give_back, order_id))
        moved.append(order_id)
    return sorted(moved)


def _plan_intermediates(context_id: int, rows: list[dict]) -> set[int]:
    """Products in this plan that ANOTHER row of it consumes — the plan's intermediates.

    Structural, from the recipes: a product is an intermediate here if something else the plan makes
    eats it. That replaced reading it off the stored costs (`input_cost == 0 and reward == 0`),
    which mislabelled a customer order's top row — stored at zero reward deliberately — as an
    intermediate, and with it every figure derived from the distinction. Empty on no graph, which
    degrades to "everything is an end product": the same reading the package had before chains.
    """
    if not rows:
        return set()
    try:
        loaded = _load_goo_and_reached(context_id)
        reached = loaded[1] if loaded else {}
    except Exception:
        return set()
    if not reached:
        return set()
    made = {int(r["type_id"]) for r in rows}
    return {int(i["type_id"])
            for tid in made
            for i in (((reached.get(tid) or {}).get("via") or {}).get("inputs") or [])} & made


def _plan_totals(context_id: int, rows: list[dict], order_meta: dict[int, dict],
                 cycle_hours_by_type: dict[int, float], output_qty_by_type: dict[int, float],
                 market_by_type: dict) -> dict:
    """What the whole plan costs, is worth, and earns per day — valued from the ROWS and today's
    prices, never from the per-row `input_cost`/`reward` written at assign time.

    Those stored figures were the old source and they were wrong twice over. **Chain-tier rows are
    stored at zero cost**, so a plan whose goo all sits in intermediates reported almost nothing
    committed (4.93m against a real ~590m of materials). And **a customer order's top row is stored
    at zero reward** on purpose — an order's revenue is what the client agreed to pay, which nothing
    here can derive — so a plan made of orders reported zero profit per day, asserting that it earns
    nothing rather than admitting nobody had said.

    So:

    * **committed** is the plan's materials at their current unit cost — literally the shopping
      list, priced (`_plan_materials`), which is the ISK you actually have to spend;
    * **output value** counts only END products, the rows nothing else in the plan consumes. An
      intermediate's value is already inside the product above it;
    * an ORDER's revenue is its `client_price`, apportioned across however much of it is assigned
      so far. An order with no price contributes to neither revenue nor profit, and is counted in
      `unpriced_orders` so the caller can say so instead of showing a confident zero;
    * **profit per day** divides by the plan's MAKESPAN — stages run in sequence, so it is the sum
      over stages of the longest job in each. Dividing each row by its own duration (the old rule)
      over-counts badly on a plan that is mostly short parallel jobs.
    """
    out = {"isk_committed": 0.0, "output_value": 0.0, "net_profit": 0.0,
           "net_profit_per_day": 0.0, "unpriced_orders": 0}
    if not rows:
        return out
    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    if not reached:
        return out

    # What the plan must BUY: its own materials, row by row, at the price the graph settled on.
    from app.reactions.graph import _plan_materials
    in_house = {int(r["type_id"]) for r in rows}

    def _cost(subset):
        pool = reaction_stock_pool(context_id)
        return sum(units * float((reached.get(tid) or {}).get("unit_cost") or 0.0)
                   for tid, units in _plan_materials(subset, reached, pool, in_house).items())

    out["isk_committed"] = _cost(rows)

    # END products only — anything another row eats is not sellable output. Same set the per-row
    # display uses, so a row shown as an intermediate can never be counted as revenue.
    consumed = _plan_intermediates(context_id, rows)

    order_revenue: dict[int, float] = {}
    for r in rows:
        tid = int(r["type_id"])
        if tid in consumed:
            continue
        oid = r.get("order_id")
        meta = order_meta.get(int(oid)) if oid else None
        price = (meta or {}).get("client_price")
        total_runs = (meta or {}).get("top_level_runs") or 0
        if oid and price and total_runs > 0:
            # An order is sold at its AGREED price, not at the market — apportioned by how much of
            # it this row represents, so a half-assigned order contributes half its invoice.
            out["output_value"] += float(price) * (int(r["runs"] or 0) / total_runs)
        else:
            # No agreed price (or not an order at all): value the goods at what they are worth on
            # the market. For an order that is a STAND-IN, not the invoice — but it is the honest
            # floor ("if the client fell through you could sell these"), and it beats reporting a
            # plan full of real work as producing nothing, which is what a hard 0 did.
            if oid:
                order_revenue.setdefault(int(oid), 0.0)
            m = market_by_type.get(tid)
            oq = output_qty_by_type.get(tid, 0.0)
            if m and oq:
                out["output_value"] += int(r["runs"] or 0) * oq * m["sell_price"]
    out["unpriced_orders"] = len([o for o, v in order_revenue.items() if v == 0.0])

    # Every row is now valued one way or the other — at the client's price where there is one, at
    # the market otherwise — so profit is simply value minus cost. `unpriced_orders` no longer means
    # "left out of this number"; it means "part of it is a market estimate, not an invoice", which
    # is what the dashboard says.
    out["net_profit"] = out["output_value"] - out["isk_committed"]
    # Makespan: stages are sequential, so the plan is done when the last stage's longest job is.
    by_stage: dict[int, float] = {}
    for r in rows:
        hours = int(r["runs"] or 0) * cycle_hours_by_type.get(int(r["type_id"]), 0.0)
        st = int(r.get("tier_order") or 0)
        by_stage[st] = max(by_stage.get(st, 0.0), hours)
    days = sum(by_stage.values()) / 24.0
    out["net_profit_per_day"] = out["net_profit"] / days if days > 0 else 0.0
    return out


def _unplanned_running_totals(context_id: int, unplanned_running: list[tuple[int, float]],
                              output_qty_by_type: dict[int, float],
                              cycle_hours_by_type: dict[int, float]) -> dict[str, float]:
    """Committed-total deltas (ISK / output value / net profit / profit-per-day) for ORPHAN running
    jobs — ones running in-game with no plan slot (see get_industry_jobs). ESI gives only product +
    run count, so they're valued from our own SDE recipe via _value_reaction_batch, priced with the
    caller's own settings (group sheet, reaction system, time efficiency) exactly like every other
    path. Lazy: the (heavier) reaction cost graph loads only when there ARE such jobs, so a fully-
    planned dashboard pays nothing. A product with no reachable recipe or no live market price is
    skipped rather than guessed — same rule the opportunity list follows."""
    totals = {"isk_committed": 0.0, "output_value": 0.0, "net_profit": 0.0, "net_profit_per_day": 0.0}
    if not unplanned_running:
        return totals
    graph = _load_goo_and_reached(context_id)
    reached = graph[1] if graph else {}
    types = graph[4] if graph else {}
    settings = effective_reaction_settings(context_id)
    up_ids = list({tid for tid, _ in unplanned_running})
    up_market = resolve_market_data(context_id, up_ids) if up_ids else {}
    for tid, runs in unplanned_running:
        node = reached.get(tid)
        m = up_market.get(tid)
        if not node or not node.get("via") or not m:
            continue
        total_out = runs * output_qty_by_type.get(tid, 0.0)
        vol = (types.get(tid, {}).get("volume") or 0.0)
        v = _value_reaction_batch(node, total_out, m["sell_price"], vol, settings)
        totals["isk_committed"] += v["input_cost"]
        totals["output_value"] += v["output_value"]
        totals["net_profit"] += v["net_profit"]
        cyc = cycle_hours_by_type.get(tid, 0)
        if cyc > 0 and runs > 0:
            totals["net_profit_per_day"] += v["net_profit"] / (runs * cyc / 24)
    return totals


@router.get("/api/reactions/jobs")
def get_industry_jobs(context_id: int = Depends(require_context)):
    """Personal reaction-job status for the Reactions wizard's dashboard page: currently
    running jobs (from the last refresh), a capacity summary (free slots right now, across
    every character that's opted into tracking), the per-character opt-in breakdown so the UI
    can offer to connect any character that hasn't opted in yet, and any standing "assigned but
    not yet actually running" instructions (see assign_reaction) — a context can hold several
    characters (an account's own alts, or characters from separate EVE accounts logged into the
    same session), and each authorises the tracking scope independently."""
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    # Fetched BEFORE opening the main connection below, not inside that try block — this opens
    # its OWN connection internally (member_group/get_reaction_settings/account override), and
    # holding two connections open at once per request is exactly what exhausted the pool under
    # concurrency (see app.db._pg_pool's queue.Queue: bounded at pool size, get() waits up to 15s
    # then raises) — a real production incident on 2026-07-13 traced to this exact pattern taking
    # down unrelated endpoints (Dashboard, Setup Analysis) once the pool was saturated. Never hold
    # a second get_connection() open while a first one from the same request is still live.
    time_eff = effective_reaction_settings(context_id).get("time_efficiency_pct", 0.0)
    # Repair the plan BEFORE it is read, not after. Both passes rewrite `pp_reaction_assignments`
    # (`restage_plan_rows` fixes stages stamped under the old position rule; the levelling pass
    # gives each product one run count and re-splits the jobs), and they used to run at the far end
    # of this function — after the SELECT below had already copied the rows into `assignments`. The
    # writes landed, so a SECOND load showed the levelled plan and the one that triggered it showed
    # the old numbers: a plan assigned and immediately re-read came back with the numbers it was
    # asked for rather than the ones it now holds, which reads as the pass not working at all.
    # Both open their own connections, so they must run before the read connection below exists —
    # never two at once (the 2026-07-13 pool-exhaustion incident, noted above).
    try:
        restage_plan_rows(context_id)
        # ...and give each product one run count instead of one per assign, so the numbers being
        # typed into the industry window are the same every time. Across every character behind
        # `reactions_level_runs` (which also re-splits the work into as many jobs as that number
        # needs); within a character otherwise, which is as far as the row count can stay fixed.
        if _level_runs_on(context_id):
            level_product_runs(context_id)
        else:
            level_stage_runs(context_id)
    except Exception:
        pass
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json, fetched_at FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        assignments: dict[int, list] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT id, character_id, type_id, name, runs, input_cost, reward, tier_order, "
                f"created_at, order_id "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) ORDER BY tier_order", char_ids,
            ):
                assignments.setdefault(r["character_id"], []).append(dict(r))
        # Client-order labels for any pending row committed via a customer order (see
        # _allocate_and_insert) — so the dashboard can show "Order: <client>" on those slots
        # instead of just the product name, distinguishing client-committed jobs from
        # speculative-profit ones at a glance.
        order_ids = list({a["order_id"] for rows in assignments.values() for a in rows if a.get("order_id")})
        order_labels: dict[int, str] = {}
        order_meta: dict[int, dict] = {}
        if order_ids:
            placeholders_o = ",".join("?" * len(order_ids))
            for r in con.execute(
                f"SELECT id, client_name, client_price, top_level_runs FROM pp_reaction_orders "
                f"WHERE id IN ({placeholders_o})", order_ids,
            ):
                order_labels[r["id"]] = r["client_name"] or f"Order #{r['id']}"
                # ...and what the client pays, so an order's rows can be valued at the agreed price
                # rather than at a market rate nobody is selling them at. See `_plan_totals`.
                order_meta[r["id"]] = {"client_price": r["client_price"],
                                       "top_level_runs": r["top_level_runs"]}
        # output_type_id -> cycle hours, so a stored assignment (which only keeps `runs`, not its
        # own formula) can be turned into a real duration for the profit/day normalization below —
        # PI's headline number is already a rate (value_per_day), so Reactions' should be too.
        # Reduced by time_eff (fetched above, before this connection was opened) — this query
        # bypasses _load_reaction_graph (which applies the same correction for the opportunity/
        # suggestion/order paths), so the reduction has to be applied here too or a pending
        # assignment's reported profit/day would understate itself using the slower raw SDE time.
        # Same idea, output units per run — needed to turn a pending row's `runs` into an actual
        # output quantity for the live output-value estimate below. Both dicts come from one scan.
        cycle_hours_by_type = {}
        output_qty_by_type = {}
        for r in con.execute("SELECT output_type_id, cycle_time, output_qty FROM reactions"):
            cycle_hours_by_type[r["output_type_id"]] = (r["cycle_time"] or 0) * (1 - time_eff) / 3600.0
            output_qty_by_type[r["output_type_id"]] = r["output_qty"]
        # output_type_id -> set of its recipe's input type_ids, so a running job whose product is
        # itself an input to ANOTHER running reaction can be flagged as an intermediate (its output
        # is consumed on-site by the next tier, not a separately sellable end product). Used to keep
        # the dashboard's "produced units" export to real end products only — see the `consumed`
        # flag on each running job below.
        reaction_inputs_by_output: dict[int, set[int]] = {}
        for r in con.execute(
            "SELECT r.output_type_id AS out, ri.type_id AS inp "
            "FROM reactions r JOIN reaction_inputs ri ON ri.reaction_id = r.reaction_id"
        ):
            reaction_inputs_by_output.setdefault(r["out"], set()).add(r["inp"])
        # Product names for the running-job display — a running job carries only its product
        # type_id from ESI, and the frontend's opportunity-list name lookup misses anything not
        # currently in that list (it showed "#16665" for Hexite). Bounded to reaction outputs
        # (~68 rows), so this is cheap and always complete.
        name_by_type = {r["type_id"]: r["name"] for r in con.execute(
            "SELECT type_id, name FROM types WHERE type_id IN (SELECT output_type_id FROM reactions)")}
    finally:
        con.close()

    # Expected output value is priced LIVE off today's market, not stored at assign-time — a
    # stored snapshot would need retroactive backfilling for every row created before this
    # existed (impossible — no way to know a past market price) and would go stale for older
    # rows anyway as prices move. One bulk fetch across every distinct assigned type_id, same
    # pattern _build_opportunities already uses.
    all_assigned_type_ids = list({r["type_id"] for rows in assignments.values() for r in rows})
    market_by_type = resolve_market_data(context_id, all_assigned_type_ids) if all_assigned_type_ids else {}

    now = _time.time()
    running: list[dict] = []
    characters: list[dict] = []
    # Time-weighted overall completion of all running jobs: Σ elapsed / Σ total duration.
    running_elapsed_sec = 0.0
    running_total_sec = 0.0
    total_slots = 0
    used_slots = 0
    peak_only = _parallel_stages_on(context_id)
    tracked_any = False
    # Which planned products another planned row consumes — the structural read of "this is an
    # intermediate", replacing the old `input_cost == 0 and reward == 0` proxy.
    all_rows = [a for rows_ in assignments.values() for a in rows_]
    consumed_by_plan = _plan_intermediates(context_id, all_rows)
    plan_totals = _plan_totals(context_id, all_rows, order_meta,
                                cycle_hours_by_type, output_qty_by_type, market_by_type)
    pending_isk_committed = plan_totals["isk_committed"]
    pending_net_profit = plan_totals["net_profit"]
    pending_net_profit_per_day = plan_totals["net_profit_per_day"]
    pending_output_value = plan_totals["output_value"]
    unplanned_running: list[tuple[int, float]] = []  # (product_type_id, runs) of running jobs with
    # no covering plan row — installed straight in-game (e.g. a corp job) rather than via the tool's
    # assign flow. Valued after the loop from our own SDE recipe so they still count in the totals.
    for c in chars:
        opted_in, why_not = reaction_capable(c)
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False,
                               "slots": slots, "reason": why_not})
            continue
        tracked_any = True
        total_slots += slots
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        active = [j for j in jobs if j.get("status") in ("active", "paused", "ready")]
        used_slots += len(active)
        # Count-aware, not just a set of type_ids present — a big batch can be split into
        # several separate pending assignment rows for the SAME product (one per job slot), so
        # only as many of them may be cleared as there are actually-running jobs of that type;
        # naive set-membership would wrongly clear every pending row for a product the moment
        # just ONE of its several intended jobs gets installed.
        running_type_counts: dict[int, int] = {}
        for j in active:
            tid = j.get("product_type_id")
            running_type_counts[tid] = running_type_counts.get(tid, 0) + 1

        pending = []
        for a in assignments.get(c["character_id"], []):
            # A live ESI job of this product covers a planned slot: it's hidden from the "to
            # install" list (the running-job square already occupies that slot in the loadout) and
            # NOT deleted — the plan is a persistent loadout, so when the job finishes and drops
            # out of ESI this row reappears as "to install". Count-aware, so N running jobs cover
            # exactly N of this product's planned rows.
            is_running = running_type_counts.get(a["type_id"], 0) > 0
            if is_running:
                running_type_counts[a["type_id"]] -= 1
            # An INTERMEDIATE is a product another row of this plan consumes — its output is not
            # sellable, it feeds the stage above. Read structurally (`in_house`, below) rather than
            # from the stored `input_cost == 0 and reward == 0`, which was only ever a proxy: it
            # also caught a customer order's top row (stored at zero reward on purpose) and any row
            # whose costs had not been written, and it is the reason the headline totals were wrong.
            is_chain_tier = a["type_id"] in consumed_by_plan
            m = market_by_type.get(a["type_id"])
            out_qty_per_run = output_qty_by_type.get(a["type_id"], 0.0)
            # Output valued at the Fuzzworks SELL (list) price — what the product is worth on the
            # market sold the normal way — matching the opportunity list's order_value.
            row_output_value = (a["runs"] * out_qty_per_run * m["sell_price"]) if (m and out_qty_per_run and not is_chain_tier) else 0.0
            # Only NOT-yet-running rows show up as "to install" squares.
            if not is_running:
                pending.append({
                    "assignment_id": a["id"], "type_id": a["type_id"], "name": a["name"], "runs": a["runs"],
                    "tier_order": a["tier_order"],
                    # Which assign wrote this row — the chain it belongs to, so the UI can tell
                    # whether ITS stage below has finished rather than some other plan's.
                    "chain": round(float(a.get("created_at") or 0.0), 3), "input_cost": a["input_cost"], "reward": a["reward"],
                    "output_value": round(row_output_value, 2),
                    "order_id": a.get("order_id"), "order_label": order_labels.get(a.get("order_id")),
                })
        # What the plan occupies AT ONCE, which for pending rows is the worst tier and not the
        # count: stage 2 is queued behind stage 1, so it is not holding a reactor while stage 1
        # runs. Same model as the assign guard and `_character_capacities` — the three used to
        # disagree, and this was the one the player read off the page.
        pending_load = _concurrent_load(pending) if peak_only else len(pending)
        used_slots += pending_load

        characters.append({
            "character_id": c["character_id"], "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active) - pending_load),
            "pending": pending,
            # Opted into tracking but the token lacks the structure-read scope — facility names
            # can't resolve (show "Structure #<id>"); the UI nudges a re-authorise. See
            # esi.STRUCTURES_SCOPE's note on why older tokens don't carry it.
            "needs_structures": "universe.read_structures" not in (c["scopes"] or ""),
            # Per-chain, per-stage progress read off ESI's own job states — what makes "you can
            # start stage 2 now" a fact the page can state instead of a wait the player has to
            # track themselves. See chain_stage_state.
            "stages": chain_stage_state(assignments.get(c["character_id"], []), jobs, now),
        })
        # Whatever remains in running_type_counts after plan-row matching is ORPHAN jobs: running
        # in-game with no plan slot (installed outside the tool's assign flow — e.g. a corp job).
        # Flag each running job as orphan/planned here, collect orphans for SDE-recipe valuation
        # after the loop, and carry character_id so the UI can offer "add to plan" on an orphan.
        orphan_remaining = dict(running_type_counts)
        for j in active:
            tid = j.get("product_type_id")
            is_orphan = orphan_remaining.get(tid, 0) > 0
            if is_orphan:
                orphan_remaining[tid] -= 1
                unplanned_running.append((tid, j.get("runs") or 0))
            end = j.get("end_date")
            start = j.get("start_date")
            hours_left = None
            progress_pct = None
            if end:
                try:
                    end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                    hours_left = round((end_ts - now) / 3600.0, 1)
                    if start:
                        start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                        total = end_ts - start_ts
                        if total > 0:
                            progress_pct = max(0.0, min(1.0, (now - start_ts) / total))
                            running_elapsed_sec += max(0.0, min(total, now - start_ts))
                            running_total_sec += total
                except Exception:
                    pass
            running.append({
                "character_id": c["character_id"],
                "character_name": c["character_name"],
                "product_type_id": tid,
                "name": name_by_type.get(tid),
                "runs": j.get("runs"),
                # Units produced per run (from our SDE recipe) so the dashboard can total the
                # end product of every running job (runs × output_qty) into a copyable list.
                "output_qty": output_qty_by_type.get(tid, 0.0),
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
                "progress_pct": round(progress_pct, 4) if progress_pct is not None else None,
                "orphan": is_orphan,
            })

    # Flag each running job that is an INTERMEDIATE consumed by another running reaction: its
    # product is an input to some other running job's recipe, so its output feeds the next tier
    # on-site rather than being a separately sellable end product. Lets the "produced units" export
    # count only real end products — summing every running job (intermediates included) and pricing
    # it double-counts value already embedded in the final product (a chain reaction's units are
    # not additional sellable output). Purely running-set based, so it also covers orphan jobs.
    running_output_types = {j["product_type_id"] for j in running}
    consumed_types = {
        inp for out in running_output_types
        for inp in reaction_inputs_by_output.get(out, ())
        if inp in running_output_types
    }
    for j in running:
        j["consumed"] = j["product_type_id"] in consumed_types

    # Fold in running jobs that had no plan slot (see _unplanned_running_totals) — valued from our
    # own SDE recipe so in-game/corp jobs still count toward the committed totals.
    up = _unplanned_running_totals(context_id, unplanned_running, output_qty_by_type, cycle_hours_by_type)
    pending_isk_committed += up["isk_committed"]
    pending_output_value += up["output_value"]
    pending_net_profit += up["net_profit"]
    pending_net_profit_per_day += up["net_profit_per_day"]

    # A stage is startable only once every stage below it is done ACROSS the account — see
    # `_gate_stages_account_wide`. Applied here, with every character's stages in hand, rather than
    # inside `chain_stage_state`, which sees one character at a time.
    _gate_stages_account_wide(characters)

    return {
        "tracked": tracked_any,
        "characters": characters,
        "running": sorted(running, key=lambda r: r["hours_left"] if r["hours_left"] is not None else 1e9),
        "running_progress_pct": round(running_elapsed_sec / running_total_sec, 4) if running_total_sec > 0 else None,
        "total_slots": total_slots,
        "free_slots": max(0, total_slots - used_slots),
        "pending_isk_committed": round(pending_isk_committed, 2),
        "pending_net_profit": round(pending_net_profit, 2),
        "pending_net_profit_per_day": round(pending_net_profit_per_day, 2),
        "pending_output_value": round(pending_output_value, 2),
        # Formulas the CURRENT plan needs and the account does not hold. The three planning
        # surfaces already refuse to hand you a stage you can't install, but only from the moment
        # they were switched on — a plan assigned before that (or before a formula was sold) sits
        # in these slots with nothing saying why it can't be installed. This is the same report,
        # asked of what is actually planned. Empty unless a paste made the library complete.
        "missing_formulas": _plan_missing_formulas(context_id, characters),
        # Orders in the plan with no agreed price. Their production cost IS counted (it is real ISK
        # you spend) but they contribute no revenue, so the profit figure understates by however
        # much they are worth. Reported so the UI can say that rather than show a confident number.
        "unpriced_orders": plan_totals["unpriced_orders"],
    }


# ── Character slot capacity (used by the advisor and by order allocation) ──────────────────

def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs AND
    minus already-pending assignments from a previous suggestion the player hasn't installed
    yet) — only characters that have opted into job tracking count, since we can't know a
    non-tracked character's current load. A fresh "Suggest reactions" run must not double-book
    slots a prior suggestion already claimed but hasn't been confirmed as running by ESI yet;
    mirrors get_industry_jobs' slot math, which does the same running+pending subtraction.

    Pending rows count by their WORST TIER, not their total (`_concurrent_load`) — a chain's stages
    never run at the same moment, so reserving a slot for each of them at once idles reactors the
    account really has. Behind `reactions_parallel_stages`; off, this is the old per-row sum."""
    peak_only = _parallel_stages_on(context_id)
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        # Grouped by (character, TIER) rather than counted per character: what competes for a slot
        # is everything at the same tier_order, so a character's planned load is the worst tier and
        # not the sum (`_concurrent_load`). Counting the sum reserved a slot for every stage of a
        # chain at once — stages that by definition never run at the same time — and every
        # allocator downstream of this then had fewer slots to place work in than the account owns.
        pending_tiers: dict[int, dict[int, int]] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT character_id, COALESCE(tier_order,0) AS tier_order, COUNT(*) AS n "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) "
                f"GROUP BY character_id, COALESCE(tier_order,0)", char_ids,
            ):
                pending_tiers.setdefault(r["character_id"], {})[int(r["tier_order"])] = r["n"]
    finally:
        con.close()

    result = []
    for c in chars:
        if not reaction_capable(c)[0]:
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        tiers = pending_tiers.get(c["character_id"], {})
        used += (max(tiers.values(), default=0) if peak_only else sum(tiers.values()))
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
            # The reactors this character HAS, and how many are taken by jobs really running in
            # game. `free_slots` above nets off pending rows by their worst tier — right for "what
            # can I start now", wrong for "how many rows may this plan hold in total", which is
            # what the levelling pass has to answer (see level_product_runs' budget).
            "slots": slots,
            "running": len([j for j in jobs if j.get("status") in ("active", "paused", "ready")]),
        })
    return result


# ── Formula concurrency: a formula is an item, and it is LOCKED while a job runs on it ─────────
# Slots were the only thing this package ever allocated against, so one Ferrofluid formula and ten
# free reactors produced a plan telling the user to install ten parallel jobs they physically
# cannot. The Industry scheduler has modelled exactly this for a while (schedule.py::_print_limits,
# "one formula is one concurrent reaction however many reactor slots are free"); this is the same
# cap, reading the same evidence, on the Reactions side.
#
# **Dependency direction: app/reactions -> app/industry, and only from inside a function.**
# app/industry/slots.py deliberately does not import app.reactions to keep the packages acyclic;
# the same discipline in this direction means the evidence layer is REUSED rather than
# reimplemented (formula_print_floor already unions the personal blueprint cache, enabled asset
# stock incl. corp scans and pastes, and distinct observed blueprint_ids, with a settled
# precedence) without either package's module-import graph gaining an edge — the import happens at
# call time, and a failure to import degrades to "no cap", which is the safe direction.

def _formula_caps_on(context_id: int) -> bool:
    """Gated: capping changes what gets suggested and what an assign commits, so it rolls out
    rather than lands. Off ⇒ `formula_concurrency_caps` returns {} ⇒ the slot math is exactly what
    it was."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("reactions_formula_cap", context_id)
    except Exception:
        return False


def formula_concurrency_caps(context_id: int) -> dict[int, int]:
    """Memoised per request — see `_formula_concurrency_caps` for the rules it applies."""
    return dict(request_memo(("formula_caps", context_id),
                             lambda: _formula_concurrency_caps(context_id)))


def _formula_concurrency_caps(context_id: int) -> dict[int, int]:
    """reaction product_type_id -> how many jobs of that product may run AT ONCE, for the products
    we have evidence about. **A MISSING KEY MEANS UNKNOWN, and unknown never refuses** — the same
    rule `_assigned_slot_capacity` follows, and the reason this returns a sparse map rather than a
    number per product: a cap built on absent evidence blocks work the player can really do.

    Three things make a key absent:

      * the flag is off — {} outright, so nothing anywhere behaves differently;
      * the account's blueprint picture is incomplete (`blueprint_coverage().complete` is false —
        the scope is opt-in per character, so a partly-connected account's counts are a FLOOR, and
        a floor read as a total serialises real work) **and the user has not DECLARED that product
        by hand**. A declaration is not a scan: it is the user stating what they own, which
        `owned_blueprints` already treats as authoritative enough to replace the ESI reading for
        that product, so it is known whatever some other character's scope says. Gating it on
        account-wide coverage cost a real user the cap they had just typed in — 238 formulas
        declared, 10 held of the product they ordered, 20 concurrent jobs assigned, because 12 of
        their 14 characters had never granted the scope. Mirrors `BuildParams.prints_known(tid)`;
      * that particular formula was never seen by any of the three evidence sources.

    Concurrency only, per the evidence layer's own contract: an asset row and a job state no ME, no
    TE and no remaining runs, and a formula has no ME/TE anyway (rig-based) and cannot be copied.

    Nothing here is per-TIER: a chain's tiers run in sequence, so one formula legitimately serves
    tier 0 and then tier 1. Callers apply the cap within a tier, never across tiers.
    """
    if not _formula_caps_on(context_id):
        return {}
    try:
        from app.industry.blueprints import (
            blueprint_coverage, declared_products, formula_print_floor, owned_blueprints)
        owned = owned_blueprints(context_id)
        floor = formula_print_floor(context_id, owned)
        declared = declared_products(owned)
        complete = bool(blueprint_coverage(context_id).get("complete"))
        if not complete and not declared:
            return {}
    except Exception:
        return {}                       # evidence unavailable is evidence absent — never cap

    con = get_connection()
    try:
        outputs = {r["output_type_id"] for r in con.execute(
            "SELECT output_type_id FROM reactions")}
    except Exception:
        return {}
    finally:
        con.close()

    caps: dict[int, int] = {}
    for prod in (set(owned) | set(floor)) & outputs:
        if not complete and prod not in declared:
            continue                              # a floor, not a total — see the docstring
        own = owned.get(prod) or {}
        held = own.get("copies")
        n_owned = (len(held) if held else 1) if own else 0
        n_extra = int(floor.get(prod) or 0)       # already de-duplicated against `owned`
        if not n_owned and not n_extra:
            continue                              # unobserved — no key, no cap
        caps[prod] = max(1, n_owned + n_extra)
    return caps


def _cap_jobs(cap: int | None, want: int) -> int:
    """`want` jobs of one product at one moment, held to the formulas there are. No cap = no
    change; a cap never takes the count below 1, since a tier at zero jobs cannot be installed."""
    return max(1, min(want, cap)) if cap else want


# ── Customer orders: committing a fixed order to real reaction slots ───────────────────────────

def _fit_chain_slots(works: list[float], caps: list[int], budget: int) -> list[int]:
    """How many slots each tier of ONE chain gets, out of a character's free slots.

    A chain is installed tier by tier — the intermediate has to finish before the job eating it can
    start — so the chain takes `sum(work_i / slots_i)` and the tiers do NOT want equal shares. On
    the order this was written for, Carbon Fiber carries 1956 runs beside Oxy-Organic Solvents'
    196; a slot given to the second saves a tenth of what the same slot saves on the first.

    So each spare slot goes to whichever tier gains most from it — the saving from one more being
    `work_i/s_i - work_i/(s_i+1)`. That is exactly optimal for this objective (it is separable and
    convex in the slot counts) and reads more honestly than the closed form, which is slots
    proportional to the square root of work.

    Every tier starts at one slot because a chain with a tier at zero cannot be installed at all.
    `caps` stops a tier being given more slots than it has runs, which would just create empty jobs.
    """
    n = len(works)
    if n == 0 or budget <= 0:
        return []
    slots = [1] * n
    spare = budget - n
    while spare > 0:
        best, best_gain = -1, 0.0
        for i in range(n):
            if slots[i] >= caps[i]:
                continue
            gain = works[i] / slots[i] - works[i] / (slots[i] + 1)
            if gain > best_gain:
                best, best_gain = i, gain
        if best < 0:
            break            # every tier already has a slot per run; more would be empty jobs
        slots[best] += 1
        spare -= 1
    return slots


def _allocate_and_insert(context_id: int, type_id: int, name: str, node: dict, reached: dict,
                          types: dict, runs_needed: int, order_id: int) -> dict:
    """Commits `runs_needed` top-level runs (plus the intermediate chain tiers the formula needs)
    to as many real reaction slots as will actually make the order finish sooner.

    **A slot is a rate, not a container.** The first version of this put each tier in exactly one
    job, reasoning that because this app models no per-job run cap, a whole batch always "fits" in
    one slot once a character has one free. It does fit — and it then takes as long as running
    every run end to end. A real 400,000-unit Reinforced Carbon Fiber order became four jobs of
    ~2000 runs each while fifty-five reaction slots sat idle.

    **A customer order runs flat out, not to a cadence.** The Suggest wizard sizes speculative work
    against a check-in cadence, because there the question is what to leave running until you next
    log in. An order is a commitment someone is waiting on, so the only sensible target is as soon
    as possible: take every free slot that still reduces the finish time, and stop at the point
    where another slot would only add an empty job. Assign a smaller batch (`req.runs`) when you
    want to keep slots back for other work — that is the knob, rather than pacing every order as if
    nobody were waiting for it.

    **Each character gets a COMPLETE chain.** An intermediate's output has to be physically on the
    character running the job that consumes it, so the split across characters is by whole chains
    and never by tier: a character either hosts the product and all of its intermediates, or takes
    no part in the order. Runs are shared out in proportion to free slots, because slots are what a
    character can actually turn into throughput.

    **And a formula is one job at a time.** Slots are not the only limit: the print is a physical
    item locked into the reactor for the job's duration, so a tier never gets more parallel jobs
    than there are formulas of it (`formula_concurrency_caps` — flag-gated, and silent about
    anything it has no evidence for). The cap is per PRODUCT and account-wide, not per character,
    because that is what the item is, so it also bounds how many characters can host the order at
    all: every host needs at least one job of every tier.
    """
    chars = [c for c in _character_capacities(context_id) if c["free_slots"] > 0]
    if not chars or runs_needed <= 0:
        return {"runs_assigned": 0, "characters": []}

    formula = node.get("via")
    # Intermediates already held are spent against this order as it is placed. `ordered_all` only
    # sizes the chain (how many slots a host needs, which formulas are scarce), so it walks a COPY
    # — spending the real pool here would leave nothing for the per-host walks that create the rows.
    stock_pool = reaction_stock_pool(context_id)
    ordered_all = _ordered_chain_tiers(formula["inputs"], runs_needed, reached,
                                        dict(stock_pool)) if formula else []
    tier_count = len(ordered_all)
    per_chain = tier_count + 1          # one slot per intermediate, plus the product itself
    caps_by_type = formula_concurrency_caps(context_id)
    chain_caps = {t: caps_by_type[t] for t in [tid for tid, _ in ordered_all] + [type_id]
                  if caps_by_type.get(t)}

    hosts = sorted((c for c in chars if c["free_slots"] >= per_chain),
                    key=lambda c: -c["free_slots"])
    if not hosts:
        return {"runs_assigned": 0, "characters": [], "error":
                 f"Needs {tier_count} intermediate reaction job slot(s) plus 1 for the product "
                 f"itself, all on one character — none of your tracked characters has that much free "
                 f"right now. Free up slots, or assign a smaller batch."}
    # A character can only take a share if there is at least one run in it for them, so a two-run
    # order never fragments across fourteen characters just because the slots exist.
    hosts = hosts[:max(1, min(len(hosts), runs_needed))]
    # ...and each host runs the WHOLE chain, so it needs one formula of every tier for itself. The
    # scarcest formula in the chain is therefore also the most characters this order can use.
    if chain_caps:
        hosts = hosts[:max(1, min(chain_caps.values()))]

    # How the order's runs are split across the characters that will run it: PROPORTIONAL to each
    # host's free slots. The roomiest character does the most work, so every host finishes at
    # roughly the same time and the order completes as early as its capacity allows. That is why one
    # product shows different run counts on different hosts; an even split was tried and reverted —
    # docs/reactions.md, "An order's runs follow capacity, not fairness".
    capacity = sum(h["free_slots"] for h in hosts)
    shares = [int(runs_needed * h["free_slots"] / capacity) for h in hosts]
    for i in range(runs_needed - sum(shares)):      # rounding remainder, roomiest first
        shares[i % len(hosts)] += 1

    now = _time.time()
    unit_cost = node.get("unit_cost", 0.0) + node.get("job_cost", 0.0)
    top_cycle_h = (node.get("cycle_time") or 0) / 3600.0
    placed: list[dict] = []
    left = dict(chain_caps)             # formulas of each type still unspoken for, account-wide
    con = get_connection()
    try:
        for idx, (host, share) in enumerate(zip(hosts, shares)):
            if share <= 0:
                continue
            # ...and the real pool here, consumed host by host: the units exist once, so the first
            # host's share spends them and the next host reacts what is genuinely left to react.
            tiers = _ordered_chain_tiers(formula["inputs"], share, reached, stock_pool) if formula else []
            # Stage per step, not position in the list: siblings share a stage and run together.
            ranks = tier_ranks(tiers)
            works = [t["runs"] * ((t["cycle_time"] or 0) / 3600.0) for _, t in tiers]
            caps = [max(1, int(t["runs"])) for _, t in tiers]
            works.append(share * top_cycle_h)
            caps.append(share)
            # Formulas left for this host, holding one of each back for every host still to come —
            # a host that cannot install a tier at all cannot run its share of the chain.
            after = len(hosts) - idx - 1
            for i, tid in enumerate([t for t, _ in tiers] + [type_id]):
                if tid in left:
                    caps[i] = _cap_jobs(max(1, left[tid] - after), caps[i])
            slots = _fit_chain_slots(works, caps, host["free_slots"])
            # `_fit_chain_slots` minimises the SUM of the tier durations, which was the right
            # objective while every tier was its own stage. Now that siblings share one, what gates
            # the stage above is the LAST of them to land — so re-balance within each stage the
            # same way the wizard does. Slot-neutral, so the fit above still decides how much
            # capacity the chain gets. Imported at call time: advisor sits above this module.
            if _parallel_stages_on(context_id) and len(tiers) > 1:
                from app.reactions.advisor import _align_stage_jobs
                align = [{"character_id": host["character_id"], "tier": ranks[i],
                          "runs": int(t["runs"]), "cycle_hours": (t["cycle_time"] or 0) / 3600.0,
                          "jobs": slots[i], "cap": caps[i]}
                         for i, (_tid, t) in enumerate(tiers)]
                _align_stage_jobs(align)
                for i, a in enumerate(align):
                    slots[i] = a["jobs"]
            for i, tid in enumerate([t for t, _ in tiers] + [type_id]):
                if tid in left:
                    left[tid] = max(0, left[tid] - slots[i])

            for i, (tid, info) in enumerate(tiers):
                _insert_assignment_rows(con, host["character_id"], tid,
                                         types.get(tid, {}).get("name", str(tid)),
                                         info["runs"], slots[i], 0.0, 0.0,
                                         ranks[i], now, order_id, tidy=_tidy_runs_on(context_id))
            _insert_assignment_rows(con, host["character_id"], type_id, name, share, slots[-1],
                                     unit_cost * share, 0.0,
                                     (max(ranks) + 1) if ranks else 0, now, order_id)
            placed.append({"character_id": host["character_id"],
                            "character_name": host["character_name"],
                            "runs": share, "jobs": sum(slots)})
        con.commit()
    finally:
        con.close()
    return {"runs_assigned": sum(p["runs"] for p in placed), "characters": placed}
