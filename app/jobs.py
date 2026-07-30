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
from app.sde import ensure_once, add_columns

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
        # "Run now" without giving the web pods access to the Kubernetes API: the admin sets a
        # timestamp here, and the CronJob — which ticks often and exits immediately when there's
        # nothing to do — picks it up on its next pass.
        add_columns(con, "pp_job_config", "run_requested REAL")
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


def request_run(job: str) -> None:
    """Ask for `job` to run at the next opportunity."""
    ensure_job_tables()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_job_config (job, enabled, run_requested, updated_at) VALUES (?,1,?,?) "
            "ON CONFLICT (job) DO UPDATE SET run_requested=excluded.run_requested",
            (job, time.time(), time.time()),
        )
        con.commit()
    finally:
        con.close()


def _clear_request(job: str) -> None:
    con = get_connection()
    try:
        con.execute("UPDATE pp_job_config SET run_requested=NULL WHERE job=?", (job,))
        con.commit()
    finally:
        con.close()


def run_state(job: str) -> dict:
    """Everything the scheduler and the admin page need to know about one job's readiness."""
    ensure_job_tables()
    con = get_connection()
    try:
        cfg = con.execute("SELECT enabled, run_requested FROM pp_job_config WHERE job=?",
                          (job,)).fetchone()
        last = con.execute(
            "SELECT started_at, ended_at, status FROM pp_job_runs "
            "WHERE job=? AND status='ok' ORDER BY started_at DESC LIMIT 1", (job,)).fetchone()
    except Exception:
        return {"enabled": True, "requested": None, "last_ok": None}
    finally:
        con.close()
    return {
        "enabled": True if cfg is None else bool(cfg["enabled"]),
        "requested": (cfg["run_requested"] if cfg else None),
        "last_ok": (last["started_at"] if last else None),
    }


def is_due(job: str, min_interval: float) -> tuple[bool, str]:
    """Should this job run right now? (due, why)

    Folding the schedule in here means one frequently-ticking CronJob covers both cases: it runs
    when a human asked, and otherwise no more often than `min_interval`. That's what makes "run now"
    possible without a second schedule or Kubernetes API access.
    """
    st = run_state(job)
    if not st["enabled"]:
        return False, "disabled"
    if st["requested"]:
        return True, "requested"
    last_ok = st["last_ok"]
    if last_ok is None:
        return True, "never run"
    age = time.time() - last_ok
    if age >= min_interval:
        return True, f"last ran {age / 3600:.1f}h ago"
    return False, f"ran {age / 3600:.1f}h ago, interval {min_interval / 3600:.0f}h"


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
    _clear_request(job)      # consumed — clear before running so one request means one run
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
    one you most need to see, and it would be invisible if this only listed what's in the log.

    Asks the database for the latest run *per job* rather than slicing the tail of one global log.
    It used to fold `recent_runs(300)` down by job, which quietly reported healthy daily jobs as
    "never run": the three 15-minute jobs alone write ~288 rows a day, so a 300-row window spans
    barely six hours, and anything less frequent than that fell off the end. `runs`/`failures` are
    lifetime totals for the same reason — counts taken from that window described the window, not
    the job. Both queries ride the (job, started_at) index.
    """
    ensure_job_tables()
    known = {name: (label, cadence) for name, label, cadence in KNOWN_JOBS}
    con = get_connection()
    try:
        totals = con.execute(
            "SELECT job, COUNT(*) AS runs, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failures "
            "FROM pp_job_runs GROUP BY job").fetchall()
        tot = {r["job"]: (int(r["runs"] or 0), int(r["failures"] or 0)) for r in totals}
        seen: dict[str, dict] = {}
        # Jobs retired from KNOWN_JOBS still have history worth showing, so union the two sets.
        for name in list(known) + [j for j in tot if j not in known]:
            last = con.execute(
                "SELECT id, job, owner, started_at, ended_at, status, detail, error "
                "FROM pp_job_runs WHERE job=? ORDER BY started_at DESC LIMIT 1", (name,)).fetchone()
            runs, failures = tot.get(name, (0, 0))
            if last is None:
                seen[name] = {"job": name, "owner": None, "started_at": None, "ended_at": None,
                              "status": "never run", "detail": "", "error": "",
                              "runs": 0, "failures": 0}
            else:
                seen[name] = {**dict(last), "runs": runs, "failures": failures}
    finally:
        con.close()
    out = []
    for name, r in seen.items():
        label, cadence = known.get(name, ("", ""))
        st = run_state(name)
        out.append({**r, "label": label, "cadence": cadence,
                    "enabled": st["enabled"], "requested": st["requested"]})
    return sorted(out, key=lambda x: (x["started_at"] is None, -(x["started_at"] or 0)))
