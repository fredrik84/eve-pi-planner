"""
Tests for /api/redeploy-candidates — the two Setup Analysis "redeploy the CC elsewhere" signals:

  1. Own-character collisions: two of the account's own EXTRACTOR colonies on the same planet_id.
     Planetary depletion is per-planet and shared across every extractor on it, so your own alts
     cannibalise each other's yield (worst when they pull the same P0). A factory sharing the planet
     is NOT a collision (it doesn't extract), and a solo colony is never flagged.
  2. Depleting deposits: an extractor colony whose install-yield (peak_day, one sample per program
     in pp_colony_yield) has trended DOWN across the last several programs — the deposit is
     exhausting, so a reseat only chases a sinking ceiling. A steady-low (thin-but-flat) planet is
     NOT flagged, nor is one with too few programs to judge, nor an up-trend.

Requires DEBUG_PI=true in the server's .env (the local docker-compose default) and the fixture
seeded first:
    docker compose exec -T web python3 scripts/seed_redeploy_fixture.py

Usage:
    python test_redeploy_candidates.py [--url http://localhost:8000]
"""

import argparse
import json
import sys
import urllib.request

CTX_COLLISION = 999005
CTX_DEPLETION = 999006


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def _by_location(items):
    return {i["location"]: i for i in items}


def test_collisions(base: str) -> bool:
    print(f"\n{'='*60}\n  Own-character collisions\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_COLLISION}")
    ok = check(status == 200, "200 OK")
    cols = data.get("collisions", [])
    # Two colliding planets (900001 same-P0, 900002 different-P0); the solo and the extractor+factory
    # planets must NOT appear.
    ok &= check(len(cols) == 2, f"exactly 2 collisions (got {len(cols)}: {[c['location'] for c in cols]})")
    by_loc = _by_location(cols)
    # P1 = the same-resource planet; must be flagged shared_resource, both occupants named.
    p1 = by_loc.get("? P1") or next((c for c in cols if c["shared_resource"]), None)
    ok &= check(p1 is not None and p1["shared_resource"] is True, "same-P0 planet flagged shared_resource=True")
    if p1:
        occ = {o["character"] for o in p1["occupants"]}
        ok &= check(occ == {"Test Gale", "Test Hana"}, f"both characters named on the shared planet (got {occ})")
    # The different-P0 planet is a collision, but NOT shared_resource.
    diff = next((c for c in cols if not c["shared_resource"]), None)
    ok &= check(diff is not None, "different-P0 shared planet is still a collision (shared_resource=False)")
    # Same-resource contention sorts ahead of mere same-planet doubling-up.
    ok &= check(cols[0]["shared_resource"] is True, "shared-resource collision is sorted first")
    # No factory-sharing or solo planet leaked in — every collision has exactly 2 extractor occupants.
    ok &= check(all(len(c["occupants"]) == 2 for c in cols), "no solo/factory planet leaked in as a collision")
    return ok


def test_depletion(base: str) -> bool:
    print(f"\n{'='*60}\n  Depleting deposits\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_DEPLETION}")
    ok = check(status == 200, "200 OK")
    dep = data.get("depleting", [])
    # Only the clean downtrend (900010) and the one-blip downtrend (900014) qualify.
    ok &= check(len(dep) == 2, f"exactly 2 depleting colonies (got {len(dep)}: {[d['location'] for d in dep]})")
    locs = {d["location"] for d in dep}
    ok &= check(locs == {"? P1", "? P5"}, f"the two downtrend colonies flagged (got {locs})")
    # Biggest decline first; the clean 40000→26000 (35%) beats the blippy 40000→27000 (32%).
    if len(dep) == 2:
        ok &= check(dep[0]["decline_pct"] >= dep[1]["decline_pct"], "sorted by decline %, largest first")
        clean = next((d for d in dep if d["location"] == "? P1"), None)
        ok &= check(clean and clean["decline_pct"] == 35, f"clean downtrend reports 35% decline (got {clean and clean['decline_pct']})")
        ok &= check(clean and clean["programs"] == 6 and clean["peak_last"] == 26000,
                    "clean downtrend reports 6 programs, 26000 current peak")
    return ok


def test_no_cross_signal(base: str) -> bool:
    print(f"\n{'='*60}\n  Signals are independent (no cross-leak)\n{'='*60}")
    # The collision account has no yield history → no depletion; the depletion account is single-char
    # → no collisions.
    _, col = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_COLLISION}")
    _, dep = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_DEPLETION}")
    ok = check(col.get("depleting") == [], "collision account reports no depleting colonies")
    ok &= check(dep.get("collisions") == [], "single-character depletion account reports no collisions")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    results = [
        test_collisions(base),
        test_depletion(base),
        test_no_cross_signal(base),
    ]
    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        sys.exit(0)
    print(f"  {results.count(False)} TEST GROUP(S) FAILED")
    sys.exit(1)


if __name__ == "__main__":
    main()
