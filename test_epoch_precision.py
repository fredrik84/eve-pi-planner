#!/usr/bin/env python3
"""Epoch columns keep sub-second precision on Postgres.

`REAL` is an 8-byte double on SQLite but **float4** on Postgres — ~7 significant digits, where a
Unix epoch needs 10. In the current epoch range float4's spacing is 128 seconds, so every timestamp
was landing within ~64s of the truth and roughly 75% of sub-minute job runs rendered as a 0-second
duration. `app.db.widen_epoch_columns()` widens them to `double precision` at startup.

In-process; run inside the container against a NON-PROD database. It writes and deletes one row in
pp_job_runs under a greppable fake job name.

    kubectl -n dev exec -i <pod> -- python3 - < test_epoch_precision.py
"""
import sys
import time

from app.db import get_connection, widen_epoch_columns, _EPOCH_COLUMNS, _IS_POSTGRES

JOB = "zz_test_epoch_precision"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def main():
    if not _IS_POSTGRES:
        print("SQLite: REAL is already a double, nothing to widen — skipping.")
        return 0

    from app.jobs import ensure_job_tables
    ensure_job_tables()

    print("the migration runs and is idempotent:")
    first = widen_epoch_columns()
    print(f"       (widened {len(first)} column(s) on this pass)")
    second = widen_epoch_columns()
    check(second == [], f"a second run changes nothing (got {second})")

    con = get_connection()
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND data_type='real'").fetchall()
        still_real = {(r["table_name"], r["column_name"]) for r in rows}

        print("no epoch column is left at float4:")
        existing = {(r["table_name"], r["column_name"]) for r in con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public'").fetchall()}
        leftover = [f"{t}.{c}" for t, c in _EPOCH_COLUMNS
                    if (t, c) in still_real and (t, c) in existing]
        check(not leftover, f"every existing epoch column is double precision (leftover: {leftover})")

        print("a written timestamp survives the round trip:")
        # The exact value from the prod measurement that exposed this: it used to read back as
        # 1785409408.0, a 56-second lie.
        ts = 1785409464.304
        con.execute("DELETE FROM pp_job_runs WHERE job=?", (JOB,))
        con.execute("INSERT INTO pp_job_runs (job, owner, started_at, ended_at, status) "
                    "VALUES (?,?,?,?,'ok')", (JOB, "test", ts, ts + 12.0))
        con.commit()
        got = con.execute("SELECT started_at, ended_at FROM pp_job_runs WHERE job=?",
                          (JOB,)).fetchone()
        drift = abs(got["started_at"] - ts)
        check(drift < 0.001, f"started_at round-trips within a millisecond (drift {drift:.3f}s)")

        print("short job durations are no longer flattened to zero:")
        dur = got["ended_at"] - got["started_at"]
        check(abs(dur - 12.0) < 0.001, f"a 12-second job reports 12 seconds (got {dur:.3f}s)")

        print("the non-epoch float4 columns were deliberately left alone:")
        # 7 significant digits is plenty for a percentage, a volume or an ISK amount; widening
        # everything would have been a bigger migration for no gain. types.volume is the canary.
        check(("types", "volume") in still_real,
              "types.volume is still real — only epochs were targeted")
    finally:
        con.execute("DELETE FROM pp_job_runs WHERE job=?", (JOB,))
        con.commit()
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
