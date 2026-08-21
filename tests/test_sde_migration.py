"""
Regression tests for the SQLite (data/sde.db) -> Postgres SDE migration.

`constellations`/`system_geo`/`system_jumps` are read through the main app DB connection
(Postgres in production) but are populated by scripts/populate_geo.py, which this migration
switched from a hardcoded SQLite connection to app.db.get_connection() (Postgres-aware). This
test is the direct, automated regression check for the public-surface consumer of that data (the
region grouping on /api/constellations) — protects against a future break (e.g. these tables ever
being empty again after a fresh Postgres restore) even though, at the time of this migration, they
happened to already be populated in production via an undocumented prior patch.

The other two consumers of this same data (jump-distance clustering in
app.planner_recommendations._system_recommendations, and the fuel-block rig security lookup in
app.fuelblock_planner._system_security) sit behind an authenticated planning request and aren't
practically exercised by an anonymous urllib test — they were verified directly against a real
Postgres instance during the migration: querying system_jumps for Jita's neighbours correctly
returned Perimeter, and _system_security('Jita') correctly returned 0.95.

Usage:
    python tests/test_sde_migration.py [--url https://dev.eveindustry.net]
"""

import argparse
import json
import sys
import urllib.request


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def test_constellation_regions(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/constellations — region grouping is populated\n{'='*60}")
    ok = True
    status, data = get(f"{base}/api/constellations")
    ok &= check(status == 200, "200 OK")
    regions = data.get("regions", {})
    ok &= check(isinstance(regions, dict), "response has a regions dict")
    non_empty = [v for v in regions.values() if v]
    ok &= check(bool(non_empty), f"at least one constellation has a non-empty region "
                                  f"({len(non_empty)}/{len(regions)} populated)")
    return ok


def main():
    parser = argparse.ArgumentParser()
    # Defaults to the LOCAL container, not production. These suites POST plans and read
    # debug endpoints; pointing them at prod by default meant a plain `python3 tests/test_x.py`
    # ran against live users' service (and silently "passed" by testing prod, not your change).
    # Pass --url explicitly to aim at a deployed environment.
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [
        test_constellation_regions(base),
    ]
    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} test groups passed")
    print(f"{'='*60}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
