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

import httpx
from fastapi import Depends

from app.sde import get_connection, ensure_once
from app.esi import require_context, ESI_BASE, _get_valid_token, INDUSTRY_JOBS_SCOPE

from app.industry._router import router

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
        with httpx.Client(timeout=12) as client:
            r = client.get(
                f"{ESI_BASE}/characters/{character_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"include_completed": "true"},
            )
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
        }
        for j in jobs if j.get("activity_id") == MANUFACTURING_ACTIVITY_ID
    ]


@router.post("/api/industry/jobs/refresh")
def refresh_manufacturing_jobs(context_id: int = Depends(require_context)):
    """Re-read manufacturing jobs from ESI for the caller's characters that granted the industry
    jobs scope. Best-effort per character."""
    ensure_manufacturing_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchall()
        refreshed, skipped = 0, 0
        for c in chars:
            if INDUSTRY_JOBS_SCOPE not in (c["scopes"] or ""):
                skipped += 1
                continue
            tok = _get_valid_token(c["character_id"])
            if not tok:
                skipped += 1
                continue
            jobs = fetch_manufacturing_jobs(c["character_id"], tok)
            if jobs is None:
                skipped += 1
                continue
            con.execute(
                "INSERT INTO pp_char_manufacturing_jobs (character_id, jobs_json, fetched_at) "
                "VALUES (?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
                "jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(jobs), _time.time()),
            )
            refreshed += 1
        con.commit()
    finally:
        con.close()
    return {"refreshed": refreshed, "skipped": skipped}


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

@ensure_once
def ensure_manufacturing_completions_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_completions (
                job_id          BIGINT PRIMARY KEY,
                context_id      INTEGER NOT NULL,
                character_id    BIGINT,
                product_type_id INTEGER,
                runs            INTEGER,
                output_value    REAL NOT NULL DEFAULT 0,
                input_cost      REAL NOT NULL DEFAULT 0,
                net_profit      REAL NOT NULL DEFAULT 0,
                completed_at    REAL NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_indcomp_ctx ON pp_industry_completions (context_id)")
        con.commit()
    finally:
        con.close()


def log_manufacturing_completions(context_id: int) -> int:
    """Record manufacturing jobs for this context that FINISHED since the last sweep (end_date
    passed, still in the cached snapshot, not already logged). Idempotent (job_id PK, INSERT ON
    CONFLICT DO NOTHING). Values each completion once at completion from the current recipe +
    market: output_value = product sell × produced; input_cost ≈ materials (ME0) + job fee; net =
    output − input. DB-only (reads the cached snapshot), cheap for the 15-min tick. Returns count."""
    ensure_manufacturing_jobs_table()
    ensure_manufacturing_completions_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT j.character_id, j.jobs_json FROM pp_char_manufacturing_jobs j "
            "JOIN pp_characters c ON c.character_id = j.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
        known = {r["job_id"] for r in con.execute(
            "SELECT job_id FROM pp_industry_completions WHERE context_id=?", (context_id,))}
    finally:
        con.close()
    if not rows:
        return 0

    now = _time.time()
    pending = []   # (job_id, character_id, product_type_id, runs, end_ts)
    for r in rows:
        try:
            jobs = _json.loads(r["jobs_json"] or "[]")
        except Exception:
            continue
        for j in jobs:
            jid = j.get("job_id")
            if jid is None or jid in known or j.get("status") not in _OCCUPYING:
                continue
            end = j.get("end_date")
            if not end:
                continue
            try:
                end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if end_ts <= now:
                pending.append((jid, r["character_id"], j.get("product_type_id"), j.get("runs") or 0, end_ts))
    if not pending:
        return 0

    # Value each finished job from the manufacturing recipe + market (locks value at completion).
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

    inserted = 0
    con = get_connection()
    try:
        for jid, cid, tid, runs, end_ts in pending:
            recipe = mfg.get(tid)
            output_value = input_cost = 0.0
            if recipe and runs > 0:
                produced = runs * (recipe["output_qty"] or 1)
                output_value = ((prices.get(tid) or {}).get("sell_price") or 0.0) * produced
                me, _te = params.me_te_for(tid, "manufacturing")
                eiv = 0.0
                for inp in recipe["inputs"]:
                    qty = effective_material_qty(inp["quantity"], runs, me, 1.0)
                    input_cost += ((prices.get(inp["type_id"]) or {}).get("sell_price") or 0.0) * qty
                    eiv += inp["quantity"] * runs * adjusted.get(inp["type_id"], 0.0)
                input_cost += eiv * (params.mfg_cost_index + params.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT)
            con.execute(
                "INSERT INTO pp_industry_completions (job_id, context_id, character_id, "
                "product_type_id, runs, output_value, input_cost, net_profit, completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (job_id) DO NOTHING",
                (jid, context_id, cid, tid, runs, round(output_value, 2), round(input_cost, 2),
                 round(output_value - input_cost, 2), end_ts),
            )
            inserted += 1
        con.commit()
    finally:
        con.close()
    return inserted


def log_all_manufacturing_completions() -> int:
    """Sweep every context that tracks manufacturing jobs and log new completions. Scheduled every
    15 min alongside the notification check. Per-context failures are isolated."""
    ensure_manufacturing_jobs_table()
    con = get_connection()
    try:
        ctxs = [r["context_id"] for r in con.execute(
            "SELECT DISTINCT c.context_id FROM pp_char_manufacturing_jobs j "
            "JOIN pp_characters c ON c.character_id = j.character_id")]
    except Exception:
        return 0
    finally:
        con.close()
    total = 0
    for ctx in ctxs:
        try:
            total += log_manufacturing_completions(ctx)
        except Exception:
            continue
    return total


@router.get("/api/industry/lifetime")
def manufacturing_lifetime(context_id: int = Depends(require_context)):
    """This account's lifetime manufacturing ledger: turnover (Σ produced output value), net profit,
    job count, and earliest completion. Forward-only. `used` flags whether they've ever completed a
    manufacturing job (drives whether the stats show at all)."""
    ensure_manufacturing_completions_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(output_value),0) AS turnover, COALESCE(SUM(net_profit),0) AS net, "
            "COUNT(*) AS jobs, MIN(completed_at) AS since "
            "FROM pp_industry_completions WHERE context_id=?", (context_id,)).fetchone()
    finally:
        con.close()
    return {"turnover": round(row["turnover"] or 0, 2), "net_profit": round(row["net"] or 0, 2),
            "jobs": row["jobs"] or 0, "since": row["since"], "used": (row["jobs"] or 0) > 0}


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
