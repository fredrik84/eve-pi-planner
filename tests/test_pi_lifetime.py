"""Durable PI accounting: rescans dedupe and bounded trend pruning cannot shrink totals."""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import esi, planner


def test_pi_program_ledger_is_durable_and_deduplicated():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE pp_characters (character_id INTEGER PRIMARY KEY, context_id INTEGER);
        CREATE TABLE pp_colony_yield (
          character_id INTEGER, planet_id INTEGER, install_ts REAL, p0_type_id INTEGER,
          peak_day REAL, prog_days REAL, scanned_ts REAL, head_centroid TEXT,
          PRIMARY KEY(character_id, planet_id, install_ts));
        CREATE TABLE pp_pi_program_ledger (
          context_id INTEGER, character_id INTEGER, planet_id INTEGER, install_ts REAL,
          p0_type_id INTEGER, peak_day REAL, prog_days REAL, recorded_at REAL,
          PRIMARY KEY(character_id, planet_id, install_ts));
        INSERT INTO pp_characters VALUES (7, 42);
    """)
    for install in range(1, 13):
        sim = {"install": install, "peak_p0_day": 1000, "program_days": 1,
               "outputs": [{"tier": 0, "type_id": 1}]}
        esi._record_yield_sample(con, 7, 99, sim, 100 + install)
    # Seeing the current program again updates its checkpoint; it does not add earnings twice.
    esi._record_yield_sample(con, 7, 99, sim, 999)
    assert con.execute("SELECT COUNT(*) FROM pp_colony_yield").fetchone()[0] == 10
    assert con.execute("SELECT COUNT(*) FROM pp_pi_program_ledger").fetchone()[0] == 12
    assert con.execute("SELECT recorded_at FROM pp_pi_program_ledger WHERE install_ts=12").fetchone()[0] == 999

    old_con, old_pi, old_prices = planner.get_connection, planner.load_pi_data, planner.fetch_prices
    try:
        planner.get_connection = lambda: con
        planner.load_pi_data = lambda: {
            "types": {1: {"pi_tier": 0}, 2: {"pi_tier": 1}},
            "schematics": {2: {"inputs": [{"type_id": 1, "quantity": 3000}], "output_qty": 20}},
        }
        planner.fetch_prices = lambda _ids: {2: 100.0}
        total = planner.pi_lifetime_estimate(42)
        assert total["programs"] == 12
        assert total["value"] > 0
    finally:
        planner.get_connection, planner.load_pi_data, planner.fetch_prices = old_con, old_pi, old_prices
        con.close()


if __name__ == "__main__":
    test_pi_program_ledger_is_durable_and_deduplicated()
    print("ALL TESTS PASSED")
