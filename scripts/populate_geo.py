"""
Populate geography tables into data/sde.db from Fuzzwork's small SDE CSV exports
(no full SDE download). Idempotent — safe to re-run. Builds:

  - constellations(name, region)             — group/filter constellations by region
  - system_geo(system, constellation, security) — auto-fill constellation on import;
        security drives the manufacturing ME rig multiplier (×1 / ×1.9 / ×2.1)
  - system_jumps(system, neighbour)          — adjacent (stargate-connected) systems

    python scripts/populate_geo.py
"""

import csv
import io
import sqlite3
import sys
import urllib.request

BASE = "https://www.fuzzwork.co.uk/dump/latest/"
DB_PATH = "data/sde.db"


def _fetch_rows(name: str) -> list[dict]:
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "eve-pi-planner/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return list(csv.DictReader(io.StringIO(resp.read().decode("utf-8", errors="replace"))))


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
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

    con = sqlite3.connect(db)
    con.execute("DROP TABLE IF EXISTS constellations")
    con.execute("CREATE TABLE constellations (name TEXT PRIMARY KEY, region TEXT)")
    con.executemany("INSERT OR REPLACE INTO constellations (name, region) VALUES (?, ?)", constellation_rows)

    con.execute("DROP TABLE IF EXISTS system_geo")
    con.execute("CREATE TABLE system_geo (system TEXT PRIMARY KEY, constellation TEXT, security REAL)")
    con.executemany(
        "INSERT OR REPLACE INTO system_geo (system, constellation, security) VALUES (?, ?, ?)",
        system_rows,
    )

    con.execute("DROP TABLE IF EXISTS system_jumps")
    con.execute("CREATE TABLE system_jumps (system TEXT, neighbour TEXT)")
    con.executemany("INSERT INTO system_jumps (system, neighbour) VALUES (?, ?)", sorted(jump_pairs))
    con.execute("CREATE INDEX idx_jumps_system ON system_jumps(system)")
    con.commit()

    nc = con.execute("SELECT COUNT(*) FROM constellations").fetchone()[0]
    ns = con.execute("SELECT COUNT(*) FROM system_geo").fetchone()[0]
    nj = con.execute("SELECT COUNT(*) FROM system_jumps").fetchone()[0]
    con.close()
    print(f"Populated {nc:,} constellations / {len(set(regions.values())):,} regions, "
          f"{ns:,} systems, {nj:,} jump links into {db}.")


if __name__ == "__main__":
    main()
