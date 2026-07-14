"""Personal reaction-job tracking, plan slots, orphan handling, and the suggestion engine — the
'what is running / what should I run' layer. Reads live ESI industry jobs (opt-in scope), matches
them against the persistent plan (pp_reaction_assignments), values everything via the graph layer,
and the wizard knapsack suggests what to react next. Depends on settings + graph; orders builds on
top of this (so this must not import orders)."""
import json as _json
import math
import time as _time
from datetime import datetime, timezone

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.market import fetch_market_data
from app.cache import cache_invalidate, charlist_key
from app.esi import require_context, ESI_BASE, _get_valid_token

from app.reactions._router import router
from app.reactions.settings import effective_reaction_settings
from app.reactions.graph import (
    _load_goo_and_reached, _load_reaction_graph, _value_reaction_batch,
    _explode_chain_tiers, _build_opportunities, _fuel_block_ids,
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
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/universe/structures/{structure_id}/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        name = data.get("name") or name
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


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Fetch this character's reaction jobs (activity_id 9) from ESI, resolving each distinct
    facility to a readable name. Best-effort: returns [] on any failure rather than raising —
    a refresh failing for one character must not block the others."""
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
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
        with httpx.Client(timeout=10) as client:
            pub = client.get(f"{ESI_BASE}/characters/{character_id}/").json()
            corp_id = pub.get("corporation_id")
            if not corp_id:
                return []
            resp = client.get(
                f"{ESI_BASE}/corporations/{corp_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
            )
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
    refreshed = 0
    skipped = 0
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
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(jobs), _time.time()),
            )
            con.commit()
        finally:
            con.close()
        refreshed += 1
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
    finally:
        con.close()


def _insert_assignment_rows(con, character_id: int, type_id: int, name: str, runs: float,
                             job_count: int, input_cost: float, reward: float, tier_order: int,
                             now: float, order_id: int | None = None) -> None:
    """One product's worth of a job commitment, split into `job_count` separate assignment rows
    (one per actual in-game job install — see assign_reaction's own docstring for why). Shared by
    assign_reaction (order_id always None there — no behavior change) and the customer-order
    assign flow (_allocate_and_insert, order_id set) so the row-insertion shape can't drift
    between the two callers."""
    job_count = max(1, job_count)
    runs_per_job = math.ceil(runs / job_count)
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
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")

        now = _time.time()
        for tier_order, tier in enumerate(req.chain_tiers):
            _insert_assignment_rows(con, req.character_id, tier.type_id, tier.name, tier.runs,
                                     tier.job_count, 0.0, 0.0, tier_order, now)

        top_tier_order = len(req.chain_tiers)
        _insert_assignment_rows(con, req.character_id, req.type_id, req.name, req.runs,
                                 req.job_count, req.input_cost, req.reward, top_tier_order, now)
        con.commit()
    finally:
        con.close()
    return {"ok": True}


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
    m = fetch_market_data([req.type_id]).get(req.type_id)
    out_qty = node["via"]["output_qty"]
    total_out = req.runs * out_qty
    vol = (types.get(req.type_id, {}).get("volume") or 0.0)
    v = _value_reaction_batch(node, total_out, m["sell_price"] if m else 0.0, vol, settings)
    input_cost = v["input_cost"]   # materials only — matches how plan rows store input_cost
    reward = v["net_profit"]

    # Chain tiers (intermediate reactions this formula needs), same as assign_reaction — recorded at
    # 0 cost since the whole chain's cost already rolls up into the top-level row's unit_cost.
    tier_runs: dict[int, dict] = {}
    _explode_chain_tiers(node["via"]["inputs"], req.runs, reached, tier_runs)
    ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))

    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")
        now = _time.time()
        for tier_order, (tier_tid, info) in enumerate(ordered):
            _insert_assignment_rows(con, req.character_id, tier_tid,
                                     types.get(tier_tid, {}).get("name", str(tier_tid)),
                                     info["runs"], 1, 0.0, 0.0, tier_order, now)
        _insert_assignment_rows(con, req.character_id, req.type_id,
                                 types.get(req.type_id, {}).get("name", str(req.type_id)),
                                 req.runs, 1, input_cost, reward, len(ordered), now)
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
    each pending slot one at a time."""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE character_id IN ({placeholders})", char_ids)
            con.commit()
    finally:
        con.close()
    return {"ok": True}


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
    up_market = fetch_market_data(up_ids) if up_ids else {}
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
                f"SELECT id, character_id, type_id, name, runs, input_cost, reward, tier_order, order_id "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) ORDER BY tier_order", char_ids,
            ):
                assignments.setdefault(r["character_id"], []).append(dict(r))
        # Client-order labels for any pending row committed via a customer order (see
        # _allocate_and_insert) — so the dashboard can show "Order: <client>" on those slots
        # instead of just the product name, distinguishing client-committed jobs from
        # speculative-profit ones at a glance.
        order_ids = list({a["order_id"] for rows in assignments.values() for a in rows if a.get("order_id")})
        order_labels: dict[int, str] = {}
        if order_ids:
            placeholders_o = ",".join("?" * len(order_ids))
            for r in con.execute(
                f"SELECT id, client_name FROM pp_reaction_orders WHERE id IN ({placeholders_o})", order_ids,
            ):
                order_labels[r["id"]] = r["client_name"] or f"Order #{r['id']}"
        # output_type_id -> cycle hours, so a stored assignment (which only keeps `runs`, not its
        # own formula) can be turned into a real duration for the profit/day normalization below —
        # PI's headline number is already a rate (value_per_day), so Reactions' should be too.
        # Reduced by time_eff (fetched above, before this connection was opened) — this query
        # bypasses _load_reaction_graph (which applies the same correction for the opportunity/
        # suggestion/order paths), so the reduction has to be applied here too or a pending
        # assignment's reported profit/day would understate itself using the slower raw SDE time.
        cycle_hours_by_type = {r["output_type_id"]: (r["cycle_time"] or 0) * (1 - time_eff) / 3600.0
                                for r in con.execute("SELECT output_type_id, cycle_time FROM reactions")}
        # Same idea, output units per run — needed to turn a pending row's `runs` into an actual
        # output quantity for the live output-value estimate below.
        output_qty_by_type = {r["output_type_id"]: r["output_qty"]
                               for r in con.execute("SELECT output_type_id, output_qty FROM reactions")}
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
    market_by_type = fetch_market_data(all_assigned_type_ids) if all_assigned_type_ids else {}

    now = _time.time()
    running: list[dict] = []
    characters: list[dict] = []
    # Time-weighted overall completion of all running jobs: Σ elapsed / Σ total duration.
    running_elapsed_sec = 0.0
    running_total_sec = 0.0
    total_slots = 0
    used_slots = 0
    tracked_any = False
    pending_isk_committed = pending_net_profit = pending_net_profit_per_day = 0.0
    pending_output_value = 0.0
    unplanned_running: list[tuple[int, float]] = []  # (product_type_id, runs) of running jobs with
    # no covering plan row — installed straight in-game (e.g. a corp job) rather than via the tool's
    # assign flow. Valued after the loop from our own SDE recipe so they still count in the totals.
    for c in chars:
        opted_in = "read_character_jobs" in (c["scopes"] or "")
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False, "slots": slots})
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
            # Chain-tier rows (intermediate reactions) carry no sellable output — their product is
            # consumed by the next tier up; assign_reaction stores them with input_cost=reward=0,
            # so they contribute 0 to every total below (the whole chain's cost/profit already
            # lives on the top-level row) and can't double-count a multi-tier chain.
            is_chain_tier = a["input_cost"] == 0 and a["reward"] == 0
            m = market_by_type.get(a["type_id"])
            out_qty_per_run = output_qty_by_type.get(a["type_id"], 0.0)
            # Output valued at the Fuzzworks SELL (list) price — what the product is worth on the
            # market sold the normal way — matching the opportunity list's order_value.
            row_output_value = (a["runs"] * out_qty_per_run * m["sell_price"]) if (m and out_qty_per_run and not is_chain_tier) else 0.0
            # The committed totals cover the WHOLE pipeline — running jobs AND not-yet-started
            # plan rows — not just the "to install" ones. A running job is still committed ISK with
            # output value coming; excluding it (the old behavior) made these numbers collapse the
            # moment a job started. Real market data required to price it (no live price = no guess).
            pending_isk_committed += a["input_cost"]
            pending_net_profit += a["reward"]
            pending_output_value += row_output_value
            # Per-day rate for this specific job: its own real duration (runs × the product's own
            # cycle time), not a shared cadence — a committed job's completion time is a fact.
            if a["reward"] > 0:
                duration_hours = a["runs"] * cycle_hours_by_type.get(a["type_id"], 0)
                if duration_hours > 0:
                    pending_net_profit_per_day += a["reward"] / (duration_hours / 24)
            # Only NOT-yet-running rows show up as "to install" squares.
            if not is_running:
                pending.append({
                    "assignment_id": a["id"], "type_id": a["type_id"], "name": a["name"], "runs": a["runs"],
                    "tier_order": a["tier_order"], "input_cost": a["input_cost"], "reward": a["reward"],
                    "output_value": round(row_output_value, 2),
                    "order_id": a.get("order_id"), "order_label": order_labels.get(a.get("order_id")),
                })
        used_slots += len(pending)

        characters.append({
            "character_id": c["character_id"], "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active) - len(pending)),
            "pending": pending,
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
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
                "progress_pct": round(progress_pct, 4) if progress_pct is not None else None,
                "orphan": is_orphan,
            })

    # Fold in running jobs that had no plan slot (see _unplanned_running_totals) — valued from our
    # own SDE recipe so in-game/corp jobs still count toward the committed totals.
    up = _unplanned_running_totals(context_id, unplanned_running, output_qty_by_type, cycle_hours_by_type)
    pending_isk_committed += up["isk_committed"]
    pending_output_value += up["output_value"]
    pending_net_profit += up["net_profit"]
    pending_net_profit_per_day += up["net_profit_per_day"]

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
    }


# ── Wizard suggestion engine ────────────────────────────────────────────────────────────────
# Two stages, not one monolithic LP: WHAT to run (a knapsack — genuinely an LP's job) and WHO
# runs it (bin-packing onto real characters/slots — not naturally an LP, and keeping it a
# separate greedy step means each stage is small enough to hand-verify on its own).

_MIN_LIQUIDITY = 1000  # order-book depth (both sides) a candidate must clear to be suggested —
# fixed heuristic, not a UI knob, per "use liquidity as a selection filter, don't show it".
_CANDIDATE_POOL_SIZE = 30  # how many of the liquidity-filtered opportunities feed the knapsack


def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs AND
    minus already-pending assignments from a previous suggestion the player hasn't installed
    yet) — only characters that have opted into job tracking count, since we can't know a
    non-tracked character's current load. A fresh "Suggest reactions" run must not double-book
    slots a prior suggestion already claimed but hasn't been confirmed as running by ESI yet;
    mirrors get_industry_jobs' slot math, which does the same running+pending subtraction."""
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
        pending_counts: dict[int, int] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT character_id, COUNT(*) AS n FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders}) GROUP BY character_id", char_ids,
            ):
                pending_counts[r["character_id"]] = r["n"]
    finally:
        con.close()

    result = []
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        used += pending_counts.get(c["character_id"], 0)
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
        })
    return result


def _suggest_reactions(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                        material_ids: set[int] | None = None) -> dict:
    opportunities = _build_opportunities(context_id, allowed_material_ids=material_ids)
    # Needed again in stage 2 to walk each chosen candidate's own formula tree for chain_tiers
    # (the intermediate reactions a multi-tier product needs before its own reaction can even
    # start) — cheap to recompute (fetch_market_data's own cache absorbs the repeat cost).
    _loaded = _load_goo_and_reached(context_id, material_ids)
    reached = _loaded[1] if _loaded else {}
    types = _loaded[4] if _loaded else {}
    candidates = [o for o in opportunities
                  if o["buy_volume"] >= _MIN_LIQUIDITY and o["sell_volume"] >= _MIN_LIQUIDITY
                  and o["top_level_runs"] > 0 and o["net_profit_instant"] > 0
                  and o["steps"] <= max_chain_depth]
    empty = {"suggestions": [], "totals": {
        "isk_committed": 0.0, "isk_budget": isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
        "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"}}
    if not candidates:
        return empty

    # Cap each candidate's usable batch size so a huge, cheap-per-unit chain doesn't get most of
    # the ISK budget allocated to a run count that could never actually finish within the
    # player's chosen cadence (e.g. "weekly") even using every free reaction slot at once — the
    # cap uses the single best character's free-slot count as an upper bound (stage 2 below
    # clamps further to whichever character an assignment actually lands on). Cost/output/profit
    # scale down linearly with runs (unit cost and unit price don't change with batch size).
    chars_for_cap = _character_capacities(context_id)
    max_slots_available = max((c["free_slots"] for c in chars_for_cap), default=0) or 1
    capped = []
    for o in candidates:
        cycle_hours = o["cycle_time"] / 3600.0 if o["cycle_time"] else 0
        if cycle_hours <= 0:
            continue
        max_runs_in_cadence = int(max_slots_available * cadence_hours / cycle_hours)
        if max_runs_in_cadence <= 0:
            continue
        if max_runs_in_cadence >= o["top_level_runs"]:
            capped.append(o)
            continue
        scale = max_runs_in_cadence / o["top_level_runs"]
        c2 = dict(o)
        c2["top_level_runs"] = max_runs_in_cadence
        c2["output_qty"] = o["output_qty"] * scale
        c2["input_cost"] = o["input_cost"] * scale
        c2["net_profit_instant"] = o["net_profit_instant"] * scale
        c2["shipping_volume_m3"] = o["shipping_volume_m3"] * scale
        c2["instant_sell_value"] = o["instant_sell_value"] * scale
        capped.append(c2)
    candidates = capped
    if not candidates:
        return empty

    # Rank by profit per step (the "least work most profitable" ordering) before truncating to
    # a small pool — keeps the LP tiny regardless of how many opportunities Phase 2 finds.
    candidates.sort(key=lambda o: -(o["net_profit_instant"] / o["top_level_runs"]))
    candidates = candidates[:_CANDIDATE_POOL_SIZE]

    import highspy  # lazy: only ever needed here, keeps it off the cold-start path (matches app.optimizer)
    n = len(candidates)
    h = highspy.Highs()
    h.silent()
    # x_i in [0,1]: what fraction of candidate i's (cadence-capped) max achievable batch to
    # actually run — a continuous relaxation, not a strict per-unit integer knapsack, since with
    # only ISK as a resource constraint the LP optimum is naturally at-or-near integer anyway (at
    # most one variable fractional at the ISK cap), and this stays small/fast and easy to
    # hand-verify, matching this codebase's existing app.optimizer approach.
    hvars = h.addVariables(n, lb=[0.0] * n, ub=[1.0] * n)
    h.maximize(sum(float(c["net_profit_instant"]) * hvars[i] for i, c in enumerate(candidates)))
    h.addConstr(sum(float(c["input_cost"]) * hvars[i] for i, c in enumerate(candidates)) <= float(isk_budget))
    # Real reaction slots are ALSO a shared, limited resource across every chosen candidate —
    # the per-candidate cadence cap above only checked each one against the single BEST
    # character's slots in isolation, so the LP could (and did, in a real reported case) fund
    # several products that each look individually cadence-feasible but together demand more
    # slots than actually exist across the account; stage 2's real bin-packing then has no
    # choice but to badly overshoot the chosen cadence on whichever gets scheduled last (a real
    # instance: two suggestions landing at 24d/10d runtimes against a much shorter cadence).
    # Uses the continuous (non-ceiled) slot-demand — runs × cycle_hours ÷ cadence_hours — so it
    # stays linear in x_i; the ceil() rounding that can nudge stage 2's actual slot count up by
    # a fraction per suggestion is a minor, expected difference, not the systemic multi-week
    # overshoot this constraint fixes.
    total_free_slots = sum(c["free_slots"] for c in chars_for_cap)
    slot_demand = [c["top_level_runs"] * (c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0) / cadence_hours
                   for c in candidates]
    h.addConstr(sum(slot_demand[i] * hvars[i] for i in range(n)) <= float(total_free_slots))
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return empty

    x = h.getSolution().col_value
    chosen = [(c, xi) for c, xi in zip(candidates, x) if xi > 1e-6]
    chosen.sort(key=lambda cx: -(cx[0]["net_profit_instant"] * cx[1]))
    chosen = chosen[:10]  # the wizard shows up to 10 concrete suggestions
    if not chosen:
        return empty

    # Stage 2 below allocates real slots in ASCENDING ideal-slot-need order (smallest first),
    # not this profit order — letting the biggest, most profit-heavy candidate go first would
    # let it greedily claim its ENTIRE ideal slot count, leaving only rounding scraps for
    # smaller candidates; a small candidate losing even 1 slot to ceil() rounding can lose HALF
    # its allocation (a real reported case: needed 2 slots, got 1, runtime nearly doubled),
    # while a big candidate absorbing that same 1-slot shortfall barely moves its own
    # percentage. Smallest-need-first minimizes the worst-case overshoot across the whole set.
    # `suggestions` is re-sorted back to this original profit order before being returned, so
    # display order is unaffected — only the internal allocation order changes.
    def _ideal_slots_for(c, xi):
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        return max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

    alloc_order = sorted(chosen, key=lambda cx: _ideal_slots_for(cx[0], cx[1]))

    # Stage 2: allocate real reaction slots to each chosen product, all targeting completion
    # within roughly one cadence period — NOT a queue over unbounded future time (the old model),
    # since everything here is sized to finish around the same ~cadence window. Each suggestion
    # claims `slots_used` of a character's free slots (a one-time budget for this cadence period,
    # not something that frees up mid-period) — using MORE slots for a bigger batch so it still
    # finishes on time, rather than trickling one run at a time through a single slot for weeks.
    # `job_count`/`runs_per_job` are what the player actually installs in-game (one job install
    # per slot); `runs` is just the total for display.
    chars = _character_capacities(context_id)
    remaining_slots = {c["character_id"]: c["free_slots"] for c in chars if c["free_slots"] > 0}
    char_names = {c["character_id"]: c["character_name"] for c in chars}
    touched_chars: set[int] = set()

    suggestions = []
    isk_committed = net_profit = total_output_value = total_output_m3 = 0.0
    max_completion_hours = 0.0
    for c, xi in alloc_order:
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        ideal_slots = max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

        available = [cid for cid, free in remaining_slots.items() if free > 0]
        if not available:
            continue  # no character has any reaction slots left at all — this suggestion can't be scheduled
        # Prefer consolidating onto an already-used character (fewer characters touched overall)
        # as long as it still has room; otherwise open a fresh one with the most free slots.
        touched_with_room = [cid for cid in touched_chars if remaining_slots.get(cid, 0) > 0]
        pick_id = max(touched_with_room, key=lambda cid: remaining_slots[cid]) if touched_with_room \
            else max(available, key=lambda cid: remaining_slots[cid])

        slots_used = min(ideal_slots, remaining_slots[pick_id])
        remaining_slots[pick_id] -= slots_used
        touched_chars.add(pick_id)

        # Stage 1's cadence cap sized every candidate assuming it COULD land on the single best
        # character's free-slot count — but only one candidate ever actually can. Once it's
        # known here which REAL character (and how many of ITS slots) this suggestion landed
        # on, downscale runs_needed (and everything computed from it below, via xi) to what
        # those specific slots can really finish within cadence, instead of keeping the full
        # run count and letting real duration balloon past what was asked for (a real reported
        # case: multiple suggestions each independently sized for a "best" character that only
        # one of them could actually get, landing at 11d4h against a much shorter cadence).
        if slots_used < ideal_slots:
            achievable_runs = max(1, int(slots_used * cadence_hours / cycle_hours))
            xi *= min(1.0, achievable_runs / runs_needed)
            runs_needed = achievable_runs

        runs_per_job = math.ceil(runs_needed / slots_used)
        duration_hours = (runs_needed / slots_used) * cycle_hours
        max_completion_hours = max(max_completion_hours, duration_hours)

        # Chain tiers: any INTERMEDIATE reaction this product's own formula needs (e.g.
        # goo -> Ferrofluid -> this product) — each is a SEPARATE job the player must install
        # and let finish BEFORE the top-level reaction can even start, since the "force real
        # chains" fix means an intermediate is never just bought pre-made. Slots for these come
        # from the SAME character (one suggestion, one character does the whole chain — simpler
        # than spreading it), taken out of whatever's left after the top tier's own allocation.
        chain_tiers = []
        top_via = reached.get(c["type_id"], {}).get("via")
        if top_via:
            tier_runs: dict[int, dict] = {}
            _explode_chain_tiers(top_via["inputs"], runs_needed, reached, tier_runs)
            # Deepest (closest to raw goo) first — the one the player must react first.
            ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
            for tid, info in ordered:
                t_cycle_hours = info["cycle_time"] / 3600.0 if info["cycle_time"] else 1.0
                t_ideal_slots = max(1, math.ceil(info["runs"] * t_cycle_hours / cadence_hours)) if cadence_hours > 0 else info["runs"]
                t_slots_used = max(1, min(t_ideal_slots, remaining_slots.get(pick_id, 0)))
                remaining_slots[pick_id] = remaining_slots.get(pick_id, 0) - t_slots_used
                chain_tiers.append({
                    "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
                    "runs": info["runs"],
                    "job_count": t_slots_used,
                    "runs_per_job": math.ceil(info["runs"] / t_slots_used),
                })

        cost = c["input_cost"] * xi
        reward = c["net_profit_instant"] * xi
        output_qty = c["output_qty"] * xi
        output_value = c["instant_sell_value"] * xi
        output_m3 = c["shipping_volume_m3"] * xi
        isk_committed += cost
        net_profit += reward
        total_output_value += output_value
        total_output_m3 += output_m3

        # How much MORE this specific product could use if it were ISK-funded all the way to
        # actually filling its claimed slots for the whole cadence window, instead of finishing
        # early and leaving them idle until the next check-in. Bounded by `top_level_runs` (the
        # true cadence/stock-capped max for this candidate) so this never suggests spending ISK
        # on more than could physically be produced.
        max_runs_per_job_for_cadence = math.floor(cadence_hours / cycle_hours) if cycle_hours > 0 else runs_per_job
        aligned_runs = min(slots_used * max_runs_per_job_for_cadence, c["top_level_runs"])
        align_extra_runs = max(0, aligned_runs - runs_needed)
        align_ratio = aligned_runs / c["top_level_runs"]
        align_extra_isk = round(align_extra_runs * (c["input_cost"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0
        align_extra_reward = round(align_extra_runs * (c["net_profit_instant"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0

        # Profit normalized to ISK/day, matching how the PI planner already reports value_per_day
        # — divided by the CADENCE window (not this suggestion's own, possibly-shorter runtime),
        # since a batch that finishes early just leaves its claimed slots idle until the next
        # cadence check-in; the cadence-normalized rate is the honest "average ISK/day this
        # delivers" including that idle time (the align hint above already targets closing this
        # exact gap by suggesting more spend to fill the whole window).
        profit_per_day = round(reward / (cadence_hours / 24), 2) if cadence_hours > 0 else None

        suggestions.append({
            "type_id": c["type_id"], "name": c["name"],
            "runs": runs_needed,
            "job_count": slots_used,
            "runs_per_job": runs_per_job,
            "input_cost": round(cost, 2),
            "reward": round(reward, 2),
            "profit_per_day": profit_per_day,
            "output_qty": round(output_qty, 1),
            "output_value": round(output_value, 2),
            "output_m3": round(output_m3, 1),
            "runtime_hours": round(duration_hours, 1),
            "align_extra_isk": align_extra_isk,
            "align_extra_reward": align_extra_reward,
            # Absolute (not delta) values for applying the alignment in one click — the frontend
            # swaps a suggestion's displayed fields to these wholesale rather than re-running the
            # whole optimizer, so clicking "align" only ever changes THIS product, nothing else.
            "aligned_runs": aligned_runs,
            "aligned_runs_per_job": max_runs_per_job_for_cadence,
            "aligned_input_cost": round(c["input_cost"] * align_ratio, 2),
            "aligned_reward": round(c["net_profit_instant"] * align_ratio, 2),
            "aligned_profit_per_day": round(c["net_profit_instant"] * align_ratio / (cadence_hours / 24), 2) if cadence_hours > 0 else None,
            "aligned_output_qty": round(c["output_qty"] * align_ratio, 1),
            "aligned_output_value": round(c["instant_sell_value"] * align_ratio, 2),
            "aligned_output_m3": round(c["shipping_volume_m3"] * align_ratio, 1),
            "assigned_character": char_names.get(pick_id, "?"),
            "assigned_character_id": pick_id,
            "chain_tiers": chain_tiers,
        })

    # Built in allocation order (smallest slot-need first, see alloc_order above) — restore
    # profit-descending order for display, matching what the LP itself ranked as most valuable.
    suggestions.sort(key=lambda s: -s["reward"])

    # "isk" = spent (near enough) the whole budget; "neither" = ran out of profitable, liquid,
    # within-chain-depth/cadence candidates before using it all — raising the ISK budget further
    # won't help, there's nothing more suitable to spend it on right now.
    binding = "isk" if isk_committed >= 0.97 * isk_budget else "neither"

    return {
        "suggestions": suggestions,
        "totals": {
            "isk_committed": round(isk_committed, 2),
            "isk_budget": isk_budget,
            "net_profit": round(net_profit, 2),
            "net_profit_per_day": round(net_profit / (cadence_hours / 24), 2) if cadence_hours > 0 and suggestions else None,
            "output_value": round(total_output_value, 2),
            "output_m3": round(total_output_m3, 1),
            "characters_used": len(touched_chars),
            "completion_hours": round(max_completion_hours, 1) if suggestions else None,
            "binding": binding,
        },
    }


class SuggestRequest(BaseModel):
    isk_budget: float
    max_chain_depth: int = 2
    cadence_hours: float = 168.0  # default weekly — how long you want a batch to run before checking back in
    material_ids: list[int] | None = None  # None/empty = no restriction, every priced material usable


_BUDGET_SENSITIVITY_STEP = 0.10  # "what if you raised your ISK budget by 10%?"


def _build_advisor(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                    material_ids: set[int] | None, current_profit: float, current_profit_per_day: float | None,
                    current_binding: str, suggestions: list[dict]) -> dict:
    """Cheap, easily-computable "how could this be better" hints — not a full analysis, just the
    obvious low-effort wins: whether a bit more ISK would actually buy meaningfully more profit
    right now (vs. there being nothing left worth spending it on within the current chain-depth/
    cadence/material limits), per-product cadence-alignment gaps (a suggestion that finishes
    early and leaves its claimed slots idle for the rest of the cadence window, for want of a bit
    more ISK to keep them running), and which excluded fuel blocks would be worth allowing back
    in. Deliberately does NOT suggest skill training — unlike every other hint here, training a
    reaction skill takes real days/weeks in-game, not something this session can act on, so it
    read as permanent background noise rather than a real "low-effort win" (explicit user
    feedback)."""
    # Budget sensitivity: only worth suggesting "raise your ISK budget" when ISK is actually the
    # thing holding this back right now (current_binding == "isk") — if the current run already
    # left ISK unspent ("neither"), the real limit is something else (chain depth, cadence,
    # material filter, or simply no more profitable/liquid candidates), and more ISK wouldn't
    # help; recommending it anyway would be confusing/wrong advice.
    budget_hint = None
    if current_binding == "isk" and current_profit > 0:
        bigger = _suggest_reactions(context_id, isk_budget * (1 + _BUDGET_SENSITIVITY_STEP),
                                     max_chain_depth, cadence_hours, material_ids)
        extra_profit = bigger["totals"]["net_profit"] - current_profit
        if extra_profit > current_profit * 0.01:
            budget_hint = {
                "extra_isk": round(isk_budget * _BUDGET_SENSITIVITY_STEP, 2),
                "extra_profit": round(extra_profit, 2),
            }

    # Per-product cadence-alignment gaps (see the align_extra_isk/align_extra_reward computed
    # alongside each suggestion in _suggest_reactions) — worth a mention only when it's a
    # meaningful amount of profit, not a rounding-sized sliver.
    align_hints = [
        {"name": s["name"], "extra_isk": s["align_extra_isk"], "extra_reward": s["align_extra_reward"]}
        for s in suggestions if s.get("align_extra_isk", 0) > 0 and s["align_extra_reward"] > current_profit * 0.01
    ]

    # Fuel-block breadth: if the caller restricted which racial fuel blocks to use (the advanced
    # material filter, e.g. "only Oxygen — that's my cheap local one"), quantify what re-adding
    # each EXCLUDED one would actually be worth, in the same ISK/day terms the rest of this tool
    # already reports profit in — turns "I locked myself to one fuel block" from a guess into a
    # real number ("+12% ISK/day if you also used Hydrogen Fuel Block") the player can weigh
    # against how much of a hassle sourcing that second variant actually is for them.
    fuel_block_hints = []
    if material_ids is not None and current_profit_per_day:
        con = get_connection()
        try:
            reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
        finally:
            con.close()
        all_fuel_blocks = _fuel_block_ids(inputs_by_reaction, reactions_by_output, load_pi_data()["types"])
        excluded = {tid: name for tid, name in all_fuel_blocks.items() if tid not in material_ids}
        for tid, name in excluded.items():
            widened = _suggest_reactions(context_id, isk_budget, max_chain_depth, cadence_hours,
                                          material_ids | {tid})
            widened_per_day = widened["totals"].get("net_profit_per_day") or 0.0
            extra_per_day = widened_per_day - current_profit_per_day
            if extra_per_day > current_profit_per_day * 0.01:
                fuel_block_hints.append({
                    "type_id": tid, "name": name,
                    "extra_isk_per_day": round(extra_per_day, 2),
                    "extra_pct": round(100 * extra_per_day / current_profit_per_day, 1),
                })
        fuel_block_hints.sort(key=lambda h: -h["extra_isk_per_day"])

    return {"budget_hint": budget_hint, "align_hints": align_hints, "fuel_block_hints": fuel_block_hints}


@router.post("/api/reactions/suggest")
def suggest_reactions(req: SuggestRequest, context_id: int = Depends(require_context)):
    if req.isk_budget <= 0 or req.max_chain_depth <= 0 or req.cadence_hours <= 0:
        return {"suggestions": [], "totals": {
            "isk_committed": 0.0, "isk_budget": req.isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
            "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"},
            "advisor": {"budget_hint": None, "align_hints": [], "fuel_block_hints": []}}
    material_ids = set(req.material_ids) if req.material_ids else None
    result = _suggest_reactions(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours, material_ids)
    result["advisor"] = _build_advisor(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours,
                                        material_ids, result["totals"]["net_profit"],
                                        result["totals"].get("net_profit_per_day"), result["totals"]["binding"],
                                        result["suggestions"])
    return result


# ── Customer orders: committing a fixed order to real reaction slots ───────────────────────────

def _allocate_and_insert(context_id: int, type_id: int, name: str, node: dict, reached: dict,
                          types: dict, runs_needed: int, order_id: int) -> dict:
    """Commits `runs_needed` top-level runs (plus any intermediate chain-tier reactions the
    formula needs) onto ONE character with enough free reaction slots right now — deliberately
    single-character, not spread across several like _suggest_reactions' stage 2: there's no
    per-job runs cap in this app's model (assign_reaction already lets one job carry an
    arbitrary run count), so a whole batch always fits in one job once a character has a free
    slot for it, and an intermediate reaction's output has to be physically on the same
    character as the job that consumes it anyway (same rule the manual-assign modal already
    enforces). Repeated "assign next batch" calls naturally land on different characters over
    time as each one's slots fill up, since this always re-reads free slots fresh."""
    chars = [c for c in _character_capacities(context_id) if c["free_slots"] > 0]
    if not chars or runs_needed <= 0:
        return {"runs_assigned": 0, "characters": []}
    chars.sort(key=lambda c: -c["free_slots"])

    formula = node.get("via")
    tier_runs: dict[int, dict] = {}
    if formula:
        _explode_chain_tiers(formula["inputs"], runs_needed, reached, tier_runs)
    ordered_tiers = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
    chain_job_slots = len(ordered_tiers)

    pick = next((c for c in chars if c["free_slots"] >= chain_job_slots + 1), None)
    if pick is None:
        if chain_job_slots > 0:
            return {"runs_assigned": 0, "characters": [], "error":
                     f"Needs {chain_job_slots} intermediate reaction job slot(s) plus 1 for the product "
                     f"itself, all on one character — none of your tracked characters has that much free "
                     f"right now. Free up slots, or assign a smaller batch."}
        pick = chars[0]

    now = _time.time()
    con = get_connection()
    try:
        for tier_order, (tid, info) in enumerate(ordered_tiers):
            tname = types.get(tid, {}).get("name", str(tid))
            _insert_assignment_rows(con, pick["character_id"], tid, tname, info["runs"], 1,
                                     0.0, 0.0, tier_order, now, order_id)
        unit_cost = node.get("unit_cost", 0.0) + node.get("job_cost", 0.0)
        _insert_assignment_rows(con, pick["character_id"], type_id, name, runs_needed, 1,
                                 unit_cost * runs_needed, 0.0, len(ordered_tiers), now, order_id)
        con.commit()
    finally:
        con.close()
    return {"runs_assigned": runs_needed,
            "characters": [{"character_id": pick["character_id"], "character_name": pick["character_name"],
                             "runs": runs_needed}]}
