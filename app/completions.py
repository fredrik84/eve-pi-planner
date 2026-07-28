"""Shared machinery for the forward-only "finished job" ledgers.

Manufacturing and reactions each keep a ledger of completed ESI jobs — one row per job_id, valued
once at completion — feeding the account's lifetime turnover / net-profit tiles and the
service-wide admin totals. The two were written separately and had drifted into being the same
code twice over: identical table shape, identical "which jobs finished since the last sweep"
scan, identical per-context sweeper, identical lifetime SUM query. Only the *valuation* genuinely
differs (a manufacturing recipe with ME and job fees vs. a reaction batch priced off the goo
sheet), so that is the one thing callers still supply.

**Two tables, deliberately not one.** `pp_industry_completions` and `pp_reaction_completions` are
named directly by app.industry.progress and app.admin's service stats, and merging them would be
a live data migration for no functional gain. This module unifies the code, not the storage.

Everything here is DB-only — it reads the cached ESI job snapshot rather than calling ESI — so it
is cheap enough to run on the 15-minute scheduler tick.
"""

import json as _json
import logging
import time as _time
from datetime import datetime

from app.sde import get_connection

log = logging.getLogger(__name__)

LEDGER_DDL = """
    CREATE TABLE IF NOT EXISTS {table} (
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
"""


def ensure_ledger(table: str, index: str) -> None:
    con = get_connection()
    try:
        con.execute(LEDGER_DDL.format(table=table))
        con.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (context_id)")
        con.commit()
    finally:
        con.close()


def pending_completions(context_id: int, jobs_table: str, ledger: str,
                        statuses) -> list[tuple]:
    """Jobs for this context that have FINISHED since the last sweep.

    Finished = end_date in the past, still present in the cached snapshot, not already logged. A
    job cancelled before its end_date simply drops out of the snapshot and never lands here.
    Returns [(job_id, character_id, product_type_id, runs, end_ts)].
    """
    con = get_connection()
    try:
        rows = con.execute(
            f"SELECT j.character_id, j.jobs_json FROM {jobs_table} j "
            "JOIN pp_characters c ON c.character_id = j.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
        known = {r["job_id"] for r in con.execute(
            f"SELECT job_id FROM {ledger} WHERE context_id=?", (context_id,))}
    finally:
        con.close()
    if not rows:
        return []

    now = _time.time()
    pending: list[tuple] = []
    for r in rows:
        try:
            jobs = _json.loads(r["jobs_json"] or "[]")
        except Exception:
            continue
        for j in jobs:
            jid = j.get("job_id")
            if jid is None or jid in known or j.get("status") not in statuses:
                continue
            end = j.get("end_date")
            if not end:
                continue
            try:
                end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if end_ts <= now:
                pending.append((jid, r["character_id"], j.get("product_type_id"),
                                j.get("runs") or 0, end_ts))
    return pending


def record_completions(ledger: str, context_id: int, valued) -> int:
    """Insert valued completions. `valued` is [(job_id, character_id, type_id, runs, end_ts,
    output_value, input_cost)]. Idempotent — job_id is the PK and the insert is ON CONFLICT DO
    NOTHING, so re-running a sweep is always safe."""
    if not valued:
        return 0
    con = get_connection()
    try:
        for jid, cid, tid, runs, end_ts, out_val, in_cost in valued:
            con.execute(
                f"INSERT INTO {ledger} (job_id, context_id, character_id, product_type_id, runs, "
                "output_value, input_cost, net_profit, completed_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (job_id) DO NOTHING",
                (jid, context_id, cid, tid, runs, round(out_val, 2), round(in_cost, 2),
                 round(out_val - in_cost, 2), end_ts),
            )
        con.commit()
    finally:
        con.close()
    return len(valued)


def sweep_all(jobs_table: str, log_one, label: str) -> int:
    """Run `log_one(context_id)` for every context that tracks jobs in `jobs_table`. Per-context
    failures are isolated so one bad account can't stall the sweep."""
    con = get_connection()
    try:
        ctxs = [r["context_id"] for r in con.execute(
            f"SELECT DISTINCT c.context_id FROM {jobs_table} j "
            "JOIN pp_characters c ON c.character_id = j.character_id")]
    except Exception:
        return 0
    finally:
        con.close()
    total = 0
    for ctx in ctxs:
        try:
            total += log_one(ctx)
        except Exception as exc:
            log.warning("%s completion logging failed for context %s: %s", label, ctx, exc)
    if total:
        log.info("Logged %d new %s completions across %d contexts", total, label, len(ctxs))
    return total


def lifetime(ledger: str, context_id: int) -> dict:
    """Lifetime turnover / net profit / job count / earliest completion for one account.
    Forward-only: completions from before the ledger existed aren't captured."""
    con = get_connection()
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(output_value),0) AS turnover, COALESCE(SUM(net_profit),0) AS net, "
            f"COUNT(*) AS jobs, MIN(completed_at) AS since FROM {ledger} WHERE context_id=?",
            (context_id,)).fetchone()
    finally:
        con.close()
    return {"turnover": round(row["turnover"] or 0, 2), "net_profit": round(row["net"] or 0, 2),
            "jobs": row["jobs"] or 0, "since": row["since"]}
