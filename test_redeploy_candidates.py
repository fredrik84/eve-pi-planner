"""
Tests for /api/redeploy-candidates — the two Setup Analysis extraction signals:

  1. Same-hotspot proximity: two colonies on the same planet whose extractor heads (ESI lat/lon +
     head_radius, captured in pp_char_planets.ext_heads) overlap the SAME resource hotspot — they
     deplete that spot together. Sharing a planet is normal; only overlapping same-P0 head discs are
     flagged. Heads far apart, heads pulling different P0s, a solo colony, and a factory sharing the
     planet must all NOT be flagged.
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

CTX_PROXIMITY = 999005
CTX_DEPLETION = 999006


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def test_proximity(base: str) -> bool:
    print(f"\n{'='*60}\n  Same-hotspot head overlap\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_PROXIMITY}")
    ok = check(status == 200, "200 OK")
    prox = data.get("proximity", [])
    # Only planet 900001 (same P0, overlapping heads) qualifies. Far-apart heads, different-P0 heads,
    # the solo colony, and the extractor+factory planet must all be excluded.
    ok &= check(len(prox) == 1, f"exactly 1 overlapping-hotspot planet (got {len(prox)}: {[p['location'] for p in prox]})")
    if prox:
        p = prox[0]
        ok &= check(p["location"] == "? P1", f"the same-spot planet is flagged (got {p['location']})")
        ok &= check(set(p.get("characters", [])) == {"Test Gale", "Test Hana"},
                    f"both characters named (got {p.get('characters')})")
        ok &= check(p["overlap_pct"] == 100, f"coincident heads report 100% overlap (got {p['overlap_pct']})")
        ok &= check(p.get("p0_name") == "Aqueous Liquids", f"the shared P0 is named (got {p.get('p0_name')})")
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
    # The proximity account has no yield history → no depletion; the depletion account is single-char
    # → no proximity overlaps.
    _, prox = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_PROXIMITY}")
    _, dep = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_DEPLETION}")
    ok = check(prox.get("depleting") == [], "proximity account reports no depleting colonies")
    ok &= check(dep.get("proximity") == [], "single-character depletion account reports no proximity overlaps")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    results = [
        test_proximity(base),
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
