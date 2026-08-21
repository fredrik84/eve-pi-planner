"""Industry planner — live ESI manufacturing-job tracking + free-slot math.

Reads the character's running manufacturing jobs (`GET /characters/{id}/industry/jobs/`, activity_id
1 — the same endpoint app/reactions reads for activity 9) so the planner knows which slots are
actually FREE right now, not just how many exist. Cache-at-fetch per character like the reactions
jobs cache; refreshed on demand, not polled.

`running_counts()` is the shared read both the slot pool (slots.py, for free = total − running) and
the "to install" checklist consume. Manufacturing running jobs come from this module's own cache;
reaction running jobs are read best-effort from the reactions cache (pp_char_industry_jobs) so the
reaction pool's free count is honest too, without importing app/reactions.
"""
import json as _json
import time as _time
from datetime import datetime

from fastapi import Depends

from app.sde import get_connection, ensure_once
from app import completions
from app import esi_http
from app.esi import require_context, INDUSTRY_JOBS_SCOPE

from app.industry._router import router
from app.industry.char_cache import refresh_character_cache

MANUFACTURING_ACTIVITY_ID = 1
# Job statuses that occupy a slot: active + paused + ready (done but not yet delivered — the slot
# stays taken until you deliver). Matches the reactions slot math.
_OCCUPYING = ("active", "paused", "ready")


@ensure_once
def ensure_manufacturing_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_manufacturing_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


def fetch_manufacturing_jobs(character_id: int, access_token: str) -> list[dict] | None:
    """This character's manufacturing jobs (activity_id 1). None on failure (never wipes a good
    cache); [] means genuinely none running."""
    try:
        r = esi_http.get(f"characters/{character_id}/industry/jobs/", token=access_token,
                         timeout=12, params={"include_completed": "true"})
        r.raise_for_status()
        jobs = r.json()
    except Exception:
        return None
    return [
        {
            "job_id": j.get("job_id"), "product_type_id": j.get("product_type_id"),
            "blueprint_type_id": j.get("blueprint_type_id"), "runs": j.get("runs"),
            "status": j.get("status"), "start_date": j.get("start_date"),
            "end_date": j.get("end_date"),
            # `blueprint_id` is the id of the SPECIFIC PHYSICAL print the job is installed on, and
            # `blueprint_location_id` is where that item lives — including a corp hangar no asset or
            # blueprint endpoint will ever show a non-Director. Distinct blueprint_ids for one
            # blueprint_type_id are measured evidence of how many prints are really held; see
            # blueprints.observed_formula_prints. Inert for the slot math here (nothing reads them),
            # kept because dropping them threw the only view of a corp-held print on the floor.
            "blueprint_id": j.get("blueprint_id"),
            "blueprint_location_id": j.get("blueprint_location_id"),
        }
        for j in jobs if j.get("activity_id") == MANUFACTURING_ACTIVITY_ID
    ]


@router.post("/api/industry/jobs/refresh")
def refresh_manufacturing_jobs(context_id: int = Depends(require_context)):
    """Re-read manufacturing jobs from ESI for the caller's characters that granted the industry
    jobs scope. Best-effort per character.

    The same pass also refreshes the reaction-formula job HISTORY (`pp_char_formula_jobs`), which
    rides the identical scope and is the only way a formula kept in a corp hangar is ever seen by a
    non-Director — see blueprints.fetch_formula_job_prints for why it is a separate fetch and table
    rather than `include_completed` on a cache the slot math counts rows in. Its failure is not this
    endpoint's failure: the manufacturing result is reported unchanged.
    """
    ensure_manufacturing_jobs_table()
    out = refresh_character_cache(
        context_id, scope=INDUSTRY_JOBS_SCOPE, table="pp_char_manufacturing_jobs",
        column="jobs_json", fetch=fetch_manufacturing_jobs)
    try:
        from app.industry.blueprints import (ensure_formula_job_prints_table,
                                             fetch_formula_job_prints)
        ensure_formula_job_prints_table()
        con = get_connection()
        try:
            # Read the scopes up front and CLOSE — refresh_character_cache holds its connection open
            # across the ESI calls, so the fetcher must never open a second one (the 2026-07-13
            # pool-exhaustion pattern). The corp-jobs scope decides whether the corp endpoint is
            # worth asking at all.
            scopes = {r["character_id"]: (r["scopes"] or "") for r in con.execute(
                "SELECT character_id, scopes FROM pp_characters WHERE context_id=?", (context_id,))}
        finally:
            con.close()
        formulas = refresh_character_cache(
            context_id, scope=INDUSTRY_JOBS_SCOPE, table="pp_char_formula_jobs",
            column="prints_json",
            fetch=lambda cid, tok: fetch_formula_job_prints(cid, tok, scopes.get(cid, "")))
        out = {**out, "formula_history_refreshed": formulas["refreshed"]}
    except Exception:
        pass
    # A Manufacturing refresh is also when a builder expects newly-free reaction capacity to be
    # noticed. The reaction allocator remains the only writer of reaction reservations.
    try:
        from app.reactions.orders import retry_automatic_orders
        out = {**out, "reaction_recovery": retry_automatic_orders(context_id)}
    except Exception:
        pass
    return out


def _occupying(jobs: list[dict]) -> int:
    return sum(1 for j in jobs if j.get("status") in _OCCUPYING)


def running_counts(context_id: int) -> dict[int, dict]:
    """{character_id: {"manufacturing": n, "reaction": m}} — slot-occupying jobs right now.
    Manufacturing from this module's cache; reaction best-effort from the reactions cache."""
    ensure_manufacturing_jobs_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,))]
        mfg = {r["character_id"]: r["jobs_json"] for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_manufacturing_jobs")}
        rx = {}
        try:
            rx = {r["character_id"]: r["jobs_json"] for r in con.execute(
                "SELECT character_id, jobs_json FROM pp_char_industry_jobs")}
        except Exception:
            pass  # reactions cache table may not exist if Reactions was never used
    finally:
        con.close()
    out: dict[int, dict] = {}
    for cid in char_ids:
        m = _occupying(_json.loads(mfg[cid])) if cid in mfg else 0
        r = _occupying(_json.loads(rx[cid])) if cid in rx else 0
        out[cid] = {"manufacturing": m, "reaction": r}
    return out


def running_jobs(context_id: int, names: dict[int, str] | None = None) -> list[dict]:
    """Flat list of the account's currently-occupying manufacturing jobs for display, newest end
    last. `names` maps product_type_id → name when available."""
    ensure_manufacturing_jobs_table()
    names = names or {}
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT j.jobs_json, c.character_name FROM pp_char_manufacturing_jobs j "
            "JOIN pp_characters c ON j.character_id = c.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        try:
            jobs = _json.loads(row["jobs_json"])
        except Exception:
            continue
        for j in jobs:
            if j.get("status") not in _OCCUPYING:
                continue
            out.append({
                "character_name": row["character_name"],
                "product_type_id": j.get("product_type_id"),
                "name": names.get(j.get("product_type_id"), str(j.get("product_type_id"))),
                "runs": j.get("runs"), "status": j.get("status"), "end_date": j.get("end_date"),
            })
    return out


# ── Completions ledger (turnover / net profit) ──────────────────────────────────────────────
# Forward-only ledger of FINISHED manufacturing jobs, one row per ESI job_id — mirrors
# app.reactions' pp_reaction_completions so both tools feed the same turnover/profit surfaces.

LEDGER = "pp_industry_completions"


@ensure_once
def ensure_manufacturing_completions_table():
    completions.ensure_ledger(LEDGER, "idx_indcomp_ctx")


def log_manufacturing_completions(context_id: int) -> int:
    """Record manufacturing jobs for this context that FINISHED since the last sweep. Scan, dedupe
    and insert are shared (app.completions); this supplies the manufacturing VALUATION — output =
    product sell x produced, input ~= materials at the resolved ME + the job installation fee.
    Value is locked in at completion; the market moving later doesn't rewrite history."""
    ensure_manufacturing_jobs_table()
    ensure_manufacturing_completions_table()
    pending = completions.pending_completions(
        context_id, "pp_char_manufacturing_jobs", LEDGER, _OCCUPYING)
    if not pending:
        return 0

    from app.industry.graph import (
        load_manufacturing_graph, effective_material_qty, resolve_build_params, SCC_SURCHARGE_PCT,
    )
    from app.markets import resolve_market_data
    from app.industry_cost import fetch_adjusted_prices
    con = get_connection()
    try:
        mfg = load_manufacturing_graph(con)
    finally:
        con.close()

    need_ids = set()
    for _, _, tid, _, _ in pending:
        if tid in mfg:
            need_ids.add(tid)
            for inp in mfg[tid]["inputs"]:
                need_ids.add(inp["type_id"])
    prices = resolve_market_data(context_id, list(need_ids)) if need_ids else {}
    adjusted = fetch_adjusted_prices(list(need_ids)) if need_ids else {}
    params = resolve_build_params(context_id, 0.0, 0.0, None, None)

    valued = []
    for jid, cid, tid, runs, end_ts in pending:
        recipe = mfg.get(tid)
        output_value = input_cost = 0.0
        if recipe and runs > 0:
            produced = runs * (recipe["output_qty"] or 1)
            output_value = ((prices.get(tid) or {}).get("sell_price") or 0.0) * produced
            me, _te = params.me_te_for(tid, "manufacturing", runs)
            eiv = 0.0
            for inp in recipe["inputs"]:
                qty = effective_material_qty(inp["quantity"], runs, me, 1.0)
                input_cost += ((prices.get(inp["type_id"]) or {}).get("sell_price") or 0.0) * qty
                eiv += inp["quantity"] * runs * adjusted.get(inp["type_id"], 0.0)
            input_cost += eiv * (params.mfg_cost_index + params.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT)
        valued.append((jid, cid, tid, runs, end_ts, output_value, input_cost))
    return completions.record_completions(LEDGER, context_id, valued)


def log_all_manufacturing_completions() -> int:
    """Sweep every context that tracks manufacturing jobs. Scheduled every 15 min alongside the
    notification check; per-context failures are isolated."""
    ensure_manufacturing_jobs_table()
    return completions.sweep_all("pp_char_manufacturing_jobs", log_manufacturing_completions,
                                 "manufacturing")


@router.get("/api/industry/lifetime")
def manufacturing_lifetime(context_id: int = Depends(require_context)):
    """This account's lifetime manufacturing ledger: turnover (Σ produced output value), net profit,
    job count, and earliest completion. Forward-only. `used` flags whether they've ever completed a
    manufacturing job (drives whether the stats show at all)."""
    ensure_manufacturing_completions_table()
    out = completions.lifetime(LEDGER, context_id)
    return {**out, "used": out["jobs"] > 0}


def next_manufacturing_completion(context_id: int) -> dict | None:
    """The soonest-finishing running manufacturing job for this account: {hours_left, name,
    tracked}, or None if nothing is running/tracked. Feeds the dashboard 'Up next' + maintenance
    routine. Reads the cached snapshot only (no ESI)."""
    jobs = running_jobs(context_id)
    now = _time.time()
    timed = []
    for j in jobs:
        end = j.get("end_date")
        if not end:
            continue
        try:
            end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        timed.append((max(0.0, (end_ts - now) / 3600.0), j))
    if not timed:
        return None
    timed.sort(key=lambda x: x[0])
    h, j = timed[0]
    name = j.get("name")
    if (not name or name == str(j.get("product_type_id"))) and j.get("product_type_id"):
        con = get_connection()
        try:
            r = con.execute("SELECT name FROM types WHERE type_id=?", (j["product_type_id"],)).fetchone()
            if r:
                name = r["name"]
        finally:
            con.close()
    return {"hours_left": round(h, 1), "name": name, "tracked": True}


@router.get("/api/industry/jobs")
def get_manufacturing_jobs(context_id: int = Depends(require_context)):
    """The account's currently-running manufacturing jobs (with product names resolved)."""
    jobs = running_jobs(context_id)
    ids = [j["product_type_id"] for j in jobs if j["product_type_id"]]
    if ids:
        con = get_connection()
        try:
            nm = {r["type_id"]: r["name"] for r in con.execute(
                f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})", ids)}
        finally:
            con.close()
        for j in jobs:
            j["name"] = nm.get(j["product_type_id"], j["name"])
    return {"jobs": jobs}
