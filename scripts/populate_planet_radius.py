"""Populate pp_planets.diameter (km) from Fuzzwork's SDE celestials dump (mapDenormalize.csv, under
.../dump/latest/csv/). Real planets vary ENORMOUSLY within a type (e.g. a Temperate can be 8,000 or
25,000 km), which the flat per-type PLANET_DIAM model can't capture — so the factory layout over- or
under-packs and the CPU/PG overflows the grid in-game. Storing the real diameter in the shared Planet DB
lets the planner verify factory fit against the ACTUAL planet (and benefits every user).

Idempotent. Re-run after each SDE patch. Run inside the container (it needs network):
    kubectl exec -n eve-pi deploy/eve-pi-planner -- python3 scripts/populate_planet_radius.py

Source columns used: groupID=7 (planets), solarSystemID, celestialIndex (= planet number), radius (m).
diameter_km = radius_m * 2 / 1000 = radius_m / 500.
"""
import csv
import io
import sys
import urllib.request

sys.path.insert(0, ".")
from app.db import get_connection

BASE = "https://www.fuzzwork.co.uk/dump/latest/csv/"


def _stream(name: str):
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "eve-pi-planner/1.0"})
    return urllib.request.urlopen(req, timeout=300)


def main() -> None:
    con = get_connection()
    # Column existence by NAME, not position: PRAGMA table_info(t) returns 6 columns on SQLite
    # (cid, name, type, notnull, dflt_value, pk) but app.db._pg_translate rewrites it to a
    # 4-column Postgres query (name, type, dflt_value, pk) — positional indexing (the old `r[1]`)
    # silently picked up the wrong column on Postgres. Key-based access is correct on both.
    cols = [r["name"] for r in con.execute("PRAGMA table_info(pp_planets)")]
    if "diameter" not in cols:
        con.execute("ALTER TABLE pp_planets ADD COLUMN diameter REAL")
        con.commit()

    want_systems = {r["system"] for r in con.execute("SELECT DISTINCT system FROM pp_planets")}

    # Resolve only the system NAMES we actually have planets for → solarSystemID (small file).
    name2id = {}
    for row in csv.DictReader(io.TextIOWrapper(_stream("mapSolarSystems.csv"), encoding="utf-8")):
        if row["solarSystemName"] in want_systems:
            name2id[row["solarSystemName"]] = int(row["solarSystemID"])
    id2name = {v: k for k, v in name2id.items()}
    print(f"Resolved {len(name2id)}/{len(want_systems)} Planet-DB systems to IDs.")

    # Stream the big celestials dump, keep only planets in our systems, write the real diameter.
    n = 0
    for row in csv.DictReader(io.TextIOWrapper(_stream("mapDenormalize.csv"), encoding="utf-8")):
        if row.get("groupID") != "7":          # 7 = planets
            continue
        nm = id2name.get(int(row["solarSystemID"]))
        if not nm:
            continue
        ci = int(row["celestialIndex"] or 0)
        rad = float(row["radius"] or 0)
        if ci <= 0 or rad <= 0:
            continue
        diam_km = round(rad / 500.0)
        n += con.execute("UPDATE pp_planets SET diameter=? WHERE system=? AND planet_num=?",
                         (diam_km, nm, ci)).rowcount
    con.commit()
    have = con.execute("SELECT COUNT(*) FROM pp_planets WHERE diameter IS NOT NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM pp_planets").fetchone()[0]
    con.close()
    print(f"Updated {n} rows; {have}/{total} Planet-DB rows now have a real diameter.")


if __name__ == "__main__":
    main()
