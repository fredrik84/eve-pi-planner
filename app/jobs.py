"""Background job runner: one owner per job, and a record of every run.

Two problems this solves, both of which only appear once you run more than one replica.

**Duplicate execution.** `make_scheduler()` starts inside every web pod, so with N replicas each
interval job fires N times. Some are idempotent and shrug it off (the completion ledgers upsert on
`job_id`), but the notification check is not: two pods can read the cooldown log in the same instant,
both conclude nothing was sent recently, and both send. The result is duplicate pushes that look
random and are miserable to reproduce. A job now has to win a database lease before it runs.

**No visibility.** Scheduled work was completely invisible — no record of whether a job ran, how long
it took, what it did, or why it failed. `pp_job_runs` is that record, and it's what the admin Jobs
page reads.

The lease is the same shape as a Kubernetes lease: claim with a TTL, renew while working, release at
the end. A crashed holder's lease simply expires, so a dead pod can never wedge a job permanently.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import traceback

from app.db import get_connection
from app.sde import ensure_once

log = logging.getLogger(__name__)

DEFAULT_TTL = 900          # 15 min; long enough for a slow run, short enough to recover from a crash

# Every job that can run, whether or not it has ever run — so the admin page can list and toggle a
# job before its first execution, and so a job that silently stopped firing is still visible.
KNOWN_JOBS = [
    ("notify_check", "Colony alert notifications", "every 15 min"),
    ("reaction_completions", "Reaction completion ledger", "every 15 min"),
    ("manufacturing_completions", "Manufacturing completion ledger", "every 15 min"),
    ("yield_aggregate", "Colony yield aggregation", "daily 03:00"),
    ("contract_scan", "Blueprint contract index (Jita)", "daily 04:20, CronJob"),
]


@ensure_once
def ensure_job_tables():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_job_leases (
                job         TEXT PRIMARY KEY,
                owner       TEXT,
                lease_until REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_job_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                job        TEXT NOT NULL,
                owner      TEXT,
                started_at REAL NOT NULL,
                ended_at   REAL,
                status     TEXT NOT NULL DEFAULT 'running',
                detail     TEXT,
                error      TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_job ON pp_job_runs (job, started_at)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_job_config (
                job        TEXT PRIMARY KEY,
                enabled    INTEGER NOT NULL DEFAULT 1,
                updated_at REAL
            )
        """)
        con.commit()
    finally:
        con.close()


def is_enabled(job: str) -> bool:
    """Jobs default to ON — a new job shouldn't need a row created before it works. Turning one off
    is the explicit act, and it's recorded."""
    ensure_job_tables()
    con = get_connection()
    try:
        r = con.execute("SELECT enabled FROM pp_job_config WHERE job=?", (job,)).fetchone()
    except Exception:
        return True
    finally:
        con.close()
    return True if r is None else bool(r["enabled"])


def set_enabled(job: str, enabled: bool) -> None:
    ensure_job_tables()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_job_config (job, enabled, updated_at) VALUES (?,?,?) "
            "ON CONFLICT (job) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
            (job, 1 if enabled else 0, time.time()),
        )
        con.commit()
    finally:
        con.close()


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(job: str, owner: str, ttl: int = DEFAULT_TTL) -> bool:
    """Win the right to run `job`. True if we got it.

    The conditional UPDATE is the whole mechanism — only a caller whose WHERE clause still matches
    an expired lease can take it, which the database makes atomic. Racing replicas produce exactly
    one winner.
    """
    ensure_job_tables()
    now = time.time()
    con = get_connection()
    try:
        con.execute("INSERT INTO pp_job_leases (job, owner, lease_until) VALUES (?,?,?) "
                    "ON CONFLICT (job) DO NOTHING", (job, None, 0.0))
        cur = con.execute(
            "UPDATE pp_job_leases SET owner=?, lease_until=? "
            "WHERE job=? AND (lease_until IS NULL OR lease_until < ?)",
            (owner, now + ttl, job, now),
        )
        won = (cur.rowcount or 0) == 1
        con.commit()
        return won
    finally:
        con.close()


def renew(job: str, owner: str, ttl: int = DEFAULT_TTL) -> bool:
    """Extend our lease. False means someone else owns it now and we should stop."""
    con = get_connection()
    try:
        cur = con.execute("UPDATE pp_job_leases SET lease_until=? WHERE job=? AND owner=?",
                          (time.time() + ttl, job, owner))
        con.commit()
        return (cur.rowcount or 0) == 1
    finally:
        con.close()


def release(job: str, owner: str) -> None:
    con = get_connection()
    try:
        con.execute("UPDATE pp_job_leases SET lease_until=NULL WHERE job=? AND owner=?",
                    (job, owner))
        con.commit()
    finally:
        con.close()


def _start_run(job: str, owner: str) -> int | None:
    con = get_connection()
    try:
        row = con.execute(
            "INSERT INTO pp_job_runs (job, owner, started_at, status) VALUES (?,?,?,'running') "
            "RETURNING id", (job, owner, time.time()),
        ).fetchone()
        con.commit()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        con.close()


def _end_run(run_id: int | None, status: str, detail: str = "", error: str = "") -> None:
    if run_id is None:
        return
    con = get_connection()
    try:
        con.execute("UPDATE pp_job_runs SET ended_at=?, status=?, detail=?, error=? WHERE id=?",
                    (time.time(), status, detail[:500], error[:2000], run_id))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def run_job(job: str, fn, ttl: int = DEFAULT_TTL) -> dict:
    """Run `fn` if we can win the lease, recording the attempt either way.

    `fn` may return a short string (or anything str()-able) describing what it did — that lands in
    the run's `detail` and is what the admin page shows.
    """
    # Check BEFORE taking the lease so a disabled job costs nothing and can't block another
    # replica. Skips aren't written to the run log — four jobs ticking every 15 minutes would bury
    # the real history — the admin page shows the off state instead.
    if not is_enabled(job):
        return {"job": job, "ran": False, "reason": "disabled"}
    owner = owner_id()
    if not claim(job, owner, ttl):
        return {"job": job, "ran": False, "reason": "held by another replica"}
    run_id = _start_run(job, owner)
    try:
        detail = fn()
        _end_run(run_id, "ok", str(detail or ""))
        return {"job": job, "ran": True, "detail": str(detail or "")}
    except Exception as e:                      # a job must never take the scheduler down with it
        log.exception("job %s failed", job)
        _end_run(run_id, "error", "", f"{e}\n{traceback.format_exc()}")
        return {"job": job, "ran": True, "error": str(e)}
    finally:
        release(job, owner)


def recent_runs(limit: int = 60) -> list[dict]:
    ensure_job_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, job, owner, started_at, ended_at, status, detail, error "
            "FROM pp_job_runs ORDER BY started_at DESC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def job_summary() -> list[dict]:
    """Latest state per job, including jobs that have never run — a job that stopped firing is the
    one you most need to see, and it would be invisible if this only listed what's in the log."""
    runs = recent_runs(300)
    seen: dict[str, dict] = {}
    for r in runs:
        j = r["job"]
        if j not in seen:
            seen[j] = {**r, "runs": 0, "failures": 0}
        seen[j]["runs"] += 1
        if r["status"] == "error":
            seen[j]["failures"] += 1
    known = {name: (label, cadence) for name, label, cadence in KNOWN_JOBS}
    for name, (label, cadence) in known.items():
        seen.setdefault(name, {"job": name, "owner": None, "started_at": None, "ended_at": None,
                               "status": "never run", "detail": "", "error": "",
                               "runs": 0, "failures": 0})
    out = []
    for name, r in seen.items():
        label, cadence = known.get(name, ("", ""))
        out.append({**r, "label": label, "cadence": cadence, "enabled": is_enabled(name)})
    return sorted(out, key=lambda x: (x["started_at"] is None, -(x["started_at"] or 0)))
