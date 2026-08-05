"""
Every table this app needs must exist after a boot against an EMPTY database.

Two real dev-stack outages came from tables that only get created lazily, plus a migration trap
that ONLY bites a fresh install:

  * `CREATE TABLE IF NOT EXISTS x (...)` followed immediately by `ALTER TABLE x ADD COLUMN c`,
    where `c` is already in the CREATE body. On Postgres the failed ALTER aborts the whole
    transaction and the connection wrapper rolls it back — silently discarding the *uncommitted*
    CREATE. An existing database never notices (its CREATE is a no-op, the ALTER genuinely adds
    the column); a new one ends up with no table at all. This is what happened to
    `pp_industry_settings`, and it 500'd the whole manufacturing planner.
  * Tables created only when someone hits the endpoint that owns them, while OTHER code queries
    them directly — `pp_shares` didn't exist until a plan share was saved, so Admin → System Stats
    and DB Cleanup 500'd on a fresh install.

`app.main._ensure_all_tables()` is the fix for the second; `app.db.add_columns()` committing
pending DDL before it risks an ALTER is the fix for the first. This test pins both.

Run inside the container:
    docker exec eve-pi-planner-web-1 python3 test_fresh_db_tables.py
"""

import os
import sys
import tempfile

sys.path.insert(0, ".")

_failures = []


def check(cond, msg):
    ok = bool(cond)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    if not ok:
        _failures.append(msg)
    return ok


# Tables that other modules query DIRECTLY (not just via the endpoint that owns them), so their
# absence is a 500 somewhere rather than an empty list. Admin stats/cleanup drive most of this.
REQUIRED = [
    "pp_characters", "pp_sessions", "pp_user_contexts", "pp_admins", "pp_testers",
    "pp_shares", "pp_profiles", "pp_plan_snapshots", "pp_plan_config", "pp_colony_flags",
    "pp_planets", "pp_planet_submissions", "pp_bugs", "pp_features", "pp_groups",
    "pp_baskets", "pp_basket_items", "pp_char_planets",
    "pp_industry_settings", "pp_industry_orders", "pp_industry_shares",
    "pp_industry_completions", "pp_char_manufacturing_jobs", "pp_char_formula_jobs",
    "pp_reaction_orders", "pp_reaction_assignments", "pp_reaction_completions",
    "pp_char_industry_jobs", "pp_markets", "pp_market_config",
    "pp_notification_settings", "pp_alert_settings", "pp_moon_goo_prices",
]


def main():
    # Point the app at a throwaway SQLite file so this never touches the real database.
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "fresh.db")

    import app.db as db
    db._SQLITE_PATH = __import__("pathlib").Path(db_path)
    db._IS_POSTGRES = False

    from app.main import _ensure_all_tables
    _ensure_all_tables()

    con = db.get_connection()
    have = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    missing = [t for t in REQUIRED if t not in have]
    check(not missing, f"every required table exists after boot on an empty DB "
                       f"(missing: {missing or 'none'})")

    # The specific regression: a CREATE-then-failing-ALTER must not lose the table.
    check("pp_industry_settings" in have,
          "pp_industry_settings survives its own CREATE-then-ALTER migration "
          "(the trap that 500'd the manufacturing planner on a fresh database)")

    # And the column the ALTER was trying to add is really there.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(pp_industry_settings)")}
    check("margin_pct" in cols, "pp_industry_settings.margin_pct is present")

    # Idempotent: running it twice must not throw or drop anything.
    from app.industry.settings import ensure_industry_settings_table
    try:
        ensure_industry_settings_table()
        check(True, "re-running an ensure_* is a safe no-op")
    except Exception as e:
        check(False, f"re-running an ensure_* raised: {e}")

    con.close()
    _check_postgres_abort_semantics()

    print()
    if _failures:
        print(f"  {len(_failures)} FAILED")
        return 1
    print("  ALL TESTS PASSED")
    return 0


def _check_postgres_abort_semantics():
    """The real trap is Postgres-only, and the checks above run on SQLite — which does NOT abort
    the transaction on a failed statement, so they would pass even with the bug present. Emulate
    Postgres' behaviour against a fake connection to pin the actual fix: `add_columns` must commit
    pending DDL BEFORE it risks an ALTER, so a rollback can't discard an uncommitted CREATE TABLE.
    """
    from app.db import add_columns

    class FakePgConn:
        """Aborts + rolls back on any failed statement, exactly like psycopg2 via _PgConn."""
        def __init__(self):
            self.committed = []      # statements made durable
            self.pending = []        # in the open transaction

        def execute(self, sql, params=()):
            if "ADD COLUMN" in sql:          # the column is already in the CREATE body -> fails
                self.pending.clear()         # Postgres aborts the txn; the wrapper rolls it back
                raise Exception('column "margin_pct" of relation already exists')
            self.pending.append(sql)
            return self

        def commit(self):
            self.committed.extend(self.pending)
            self.pending.clear()

    con = FakePgConn()
    con.execute("CREATE TABLE IF NOT EXISTS pp_industry_settings (margin_pct REAL)")
    add_columns(con, "pp_industry_settings", "margin_pct REAL")
    con.commit()

    survived = any("CREATE TABLE" in s for s in con.committed)
    check(survived,
          "under Postgres abort-on-error semantics the CREATE TABLE survives a failing ALTER "
          "(add_columns commits pending DDL first) — without this the table is silently never created")


if __name__ == "__main__":
    sys.exit(main())
