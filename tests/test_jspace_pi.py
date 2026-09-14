#!/usr/bin/env python3
"""J-space PI contract: import safely, plan normally, never invent static proximity."""

import sqlite3
import sys

sys.path.insert(0, ".")

from app.planetary import P0_COLUMNS, _parse_planet_rows, _write_planet_rows  # noqa: E402
from app.planner_recommendations import _system_recommendations_impl  # noqa: E402


# Real SDE identities (same wormhole constellation), copied into an isolated DB so the test
# remains deterministic and never modifies the application's shared Planet DB.
J1 = ("J000102", "H-C00333", -0.99, 31_002_604)
J2 = ("J000214", "H-C00333", -0.99, 31_002_599)
failures = []


def check(condition, message):
    print(f"  {'PASS' if condition else 'FAIL'}: {message}")
    if not condition:
        failures.append(message)


def database():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE system_geo (system TEXT, constellation TEXT, security REAL, system_id INTEGER)")
    con.executemany("INSERT INTO system_geo VALUES (?,?,?,?)", (J1, J2))
    con.execute("CREATE TABLE system_jumps (system TEXT, neighbour TEXT)")
    p0_ddl = ", ".join(f"{col} INTEGER NOT NULL DEFAULT 0" for col in P0_COLUMNS)
    con.execute(f"""CREATE TABLE pp_planets (
        id INTEGER PRIMARY KEY, system TEXT NOT NULL, planet_num INTEGER NOT NULL,
        planet_type TEXT NOT NULL, constellation TEXT NOT NULL, {p0_ddl},
        UNIQUE(system, planet_num))""")
    return con


def main():
    con = database()
    print("\nKnown J-space import")
    rows, skipped, errors = _parse_planet_rows(
        "j000102,1,Barren,,80,70,60,50,40\nJ000214,2,Barren,,40,50,60,70,80", con
    )
    check(len(rows) == 2 and skipped == 0 and not errors, "known J-space rows parse without error")
    check(rows[0]["system"] == J1[0], "lowercase pasted name is canonicalised to the SDE spelling")
    check(rows[0]["constellation"] == J1[1], "J-space constellation is filled from system_geo")
    imported, write_skipped = _write_planet_rows(con, rows)
    check((imported, write_skipped) == (2, 0), "known J-space planets upsert normally")

    print("\nBad J-space row is isolated")
    bad_rows, bad_skipped, bad_errors = _parse_planet_rows(
        "J999999,1,Barren,,80,70,60,50,40\nJ000102,3,Barren,,80,70,60,50,40", con
    )
    check(len(bad_rows) == 1 and bad_skipped == 1, "unknown J-name is skipped without losing the valid row")
    check(any("Unknown J-space system" in e for e in bad_errors), "the skipped row explains the J-name problem")

    print("\nJ-space recommendation")
    recs = _system_recommendations_impl(
        ["Aqueous Liquids", "Noble Metals"], con, top_n=10,
        systems=[J1[0], J2[0]], preferred_systems=2, max_jumps=5,
    )
    single = next(r for r in recs if r["systems_needed"] == [J1[0]])
    pair = next(r for r in recs if r["systems_needed"] == [J1[0], J2[0]])
    check(single["proximity_known"] is True and single["within_jumps"] is True,
          "one J-space system is trivially local")
    check(pair["proximity_known"] is False, "multi-system J-space proximity is explicitly unknown")
    check(pair["within_jumps"] is False and pair["jumps"] is None,
          "no static adjacency or made-up jump count is assigned")

    print("\n" + (f"FAILED: {len(failures)}" if failures else "All checks passed."))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
