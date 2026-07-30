#!/usr/bin/env python3
"""job_summary() must report a job's real last run regardless of how chatty its neighbours are.

The bug this pins down: job_summary() used to fold `recent_runs(300)` — the 300 most recent rows
across ALL jobs — down by job name. The three 15-minute jobs write ~288 rows a day between them, so
that window spans about six hours, and a healthy daily job (yield_aggregate at 03:00, contract_scan
at 04:20) sat outside it and was reported to the admin as "never run". A monitoring page that calls
working jobs dead is worse than no page.

In-process, so run it inside the container (needs app/ + psycopg2 on the path). Point it at a
non-prod database — it seeds and then deletes rows in pp_job_runs, all of them namespaced under the
_TEST_PREFIX so it can never touch a real job's history.

    kubectl -n dev exec -i <pod> -- python3 - < test_job_summary.py
"""
import sys
import time

from app.db import get_connection
from app.jobs import ensure_job_tables, job_summary

_TEST_PREFIX = "zz_test_jobsummary_"
_RARE = _TEST_PREFIX + "daily"
_NOISY = _TEST_PREFIX + "every15"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _cleanup(con):
    con.execute("DELETE FROM pp_job_runs WHERE job LIKE ?", (_TEST_PREFIX + "%",))
    con.commit()


def main():
    ensure_job_tables()
    con = get_connection()
    try:
        _cleanup(con)
        now = time.time()

        # One daily job that ran successfully 8 hours ago...
        rare_started = now - 8 * 3600
        con.execute(
            "INSERT INTO pp_job_runs (job, owner, started_at, ended_at, status, detail) "
            "VALUES (?,?,?,?,'ok',?)",
            (_RARE, "test", rare_started, rare_started + 12, "indexed 7 things"))
        # ...buried under far more than 300 newer rows from a 15-minute job, which is exactly the
        # shape that made the old global-window query lose it.
        for i in range(320):
            t = now - (320 - i) * 60
            con.execute(
                "INSERT INTO pp_job_runs (job, owner, started_at, ended_at, status, detail) "
                "VALUES (?,?,?,?,?,?)",
                (_NOISY, "test", t, t + 1, "error" if i < 3 else "ok", ""))
        con.commit()

        by_job = {s["job"]: s for s in job_summary()}

        print("the buried daily job is still reported as having run:")
        rare = by_job.get(_RARE)
        check(rare is not None, "the daily job appears in the summary at all")
        if rare:
            check(rare["status"] == "ok",
                  f"status is its real last status, not 'never run' (got {rare['status']!r})")
            # 128s, not "a second": pp_job_runs.started_at is REAL, which is float4 on Postgres,
            # and an epoch timestamp needs ten significant digits — so every stored time is
            # quantised to ~64s (measured). That's a separate schema defect; this assertion only
            # needs to prove we surfaced THIS run rather than some other one, so it's scoped just
            # tight enough to distinguish it from the noise rows a minute apart.
            check(abs((rare["started_at"] or 0) - rare_started) < 128,
                  "started_at is the real last-run time")
            check(rare["detail"] == "indexed 7 things", "the last run's detail survives")
            check(rare["runs"] == 1, f"runs counts its whole history (got {rare.get('runs')})")

        print("counts are lifetime totals, not a slice of the recent window:")
        noisy = by_job.get(_NOISY)
        check(noisy is not None, "the chatty job appears in the summary")
        if noisy:
            check(noisy["runs"] == 320,
                  f"runs counts all 320 rows, not the capped window (got {noisy.get('runs')})")
            check(noisy["failures"] == 3,
                  f"failures counts all 3 errors (got {noisy.get('failures')})")

        print("jobs that genuinely never ran still say so:")
        known = {s["job"]: s for s in job_summary()}
        never = [s for s in known.values()
                 if s["started_at"] is None and s["status"] != "never run"]
        check(not never, "a job with no rows is labelled 'never run' and nothing else is")
    finally:
        _cleanup(con)
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
