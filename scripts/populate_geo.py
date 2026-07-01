"""
Populate geography tables from Fuzzwork's small SDE CSV exports (no full SDE download) into the
app's database (Postgres in production, SQLite in dev — via app.db.get_connection()). Idempotent
— skips entirely if already populated. Builds:

  - constellations(name, region)             — group/filter constellations by region
  - system_geo(system, constellation, security) — auto-fill constellation on import;
        security drives the manufacturing ME rig multiplier (×1 / ×1.9 / ×2.1)
  - system_jumps(system, neighbour)          — adjacent (stargate-connected) systems

    python scripts/populate_geo.py
"""

import csv
import io
import sys
import urllib.request

sys.path.insert(0, ".")
from app.db import get_connection, _IS_POSTGRES

BASE = "https://www.fuzzwork.co.uk/dump/latest/csv/"

# Different key from build_sde.py's lock — these are separate scripts/tables; no reason to
# serialize one against the other across pods, only against concurrent runs of itself.
_ADVISORY_LOCK_KEY = 918_273_646

_DDL = [
    "CREATE TABLE IF NOT EXISTS constellations (name TEXT PRIMARY KEY, region TEXT)",
    "CREATE TABLE IF NOT EXISTS system_geo (system TEXT PRIMARY KEY, constellation TEXT, security REAL)",
    "CREATE TABLE IF NOT EXISTS system_jumps (system TEXT NOT NULL, neighbour TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_jumps_system ON system_jumps (system)",
]


def _fetch_rows(name: str) -> list[dict]:
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "eve-pi-planner/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        # utf-8-sig strips a leading BOM if present (Fuzzwork's CSVs now ship with one) — plain
        # utf-8 folds it into the first header's key ('﻿regionID' instead of 'regionID'),
        # breaking every dict lookup on that column.
        return list(csv.DictReader(io.StringIO(resp.read().decode("utf-8-sig", errors="replace"))))


def _already_populated(con) -> bool:
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM constellations").fetchone()
        return bool(row and row["n"] > 0)
    except Exception:
        return False


def _populate(con) -> None:
    print("Fetching region/constellation/system maps from Fuzzwork…")
    regions = {r["regionID"]: r["regionName"] for r in _fetch_rows("mapRegions.csv")}
    consts = _fetch_rows("mapConstellations.csv")
    con_name = {c["constellationID"]: c["constellationName"] for c in consts}
    systems = _fetch_rows("mapSolarSystems.csv")
    sys_name = {s["solarSystemID"]: s["solarSystemName"] for s in systems}
    jumps = _fetch_rows("mapSolarSystemJumps.csv")

    def _sec(s):
        try:
            return round(float(s.get("security", "")), 2)
        except (TypeError, ValueError):
            return None

    constellation_rows = [(c["constellationName"], regions.get(c["regionID"], "")) for c in consts]
    system_rows = [(s["solarSystemName"], con_name.get(s["constellationID"], ""), _sec(s)) for s in systems]
    jump_pairs: set[tuple] = set()
    for j in jumps:
        a, b = sys_name.get(j["fromSolarSystemID"]), sys_name.get(j["toSolarSystemID"])
        if a and b and a != b:
            jump_pairs.add((a, b))
            jump_pairs.add((b, a))   # store both directions for easy lookup

    for stmt in _DDL:
        con.execute(stmt)
    con.commit()

    # OR IGNORE (not OR REPLACE): this only ever runs against freshly-created, empty tables
    # (guarded by _already_populated() in main()), so there's nothing to replace — every row is
    # a first insert either way, and OR IGNORE is what app.db's _pg_translate already supports.
    con.executemany("INSERT OR IGNORE INTO constellations (name, region) VALUES (?, ?)", constellation_rows)
    con.executemany(
        "INSERT OR IGNORE INTO system_geo (system, constellation, security) VALUES (?, ?, ?)",
        system_rows,
    )
    con.executemany("INSERT OR IGNORE INTO system_jumps (system, neighbour) VALUES (?, ?)", sorted(jump_pairs))
    con.commit()

    nc = con.execute("SELECT COUNT(*) FROM constellations").fetchone()[0]
    ns = con.execute("SELECT COUNT(*) FROM system_geo").fetchone()[0]
    nj = con.execute("SELECT COUNT(*) FROM system_jumps").fetchone()[0]
    print(f"Populated {nc:,} constellations / {len(set(regions.values())):,} regions, "
          f"{ns:,} systems, {nj:,} jump links.")


def main() -> None:
    con = get_connection()
    try:
        if _IS_POSTGRES:
            con.execute("SELECT pg_advisory_lock(?)", (_ADVISORY_LOCK_KEY,))
        try:
            for stmt in _DDL:   # tables must exist before the presence check can query them
                con.execute(stmt)
            con.commit()
            if _already_populated(con):
                print("Geo tables already populated — skipping.")
                return
            _populate(con)
        finally:
            if _IS_POSTGRES:
                con.execute("SELECT pg_advisory_unlock(?)", (_ADVISORY_LOCK_KEY,))
                con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    main()
