"""
Tests for /api/redeploy-candidates — the two Setup Analysis extraction signals:

  1. Overlapping reachable ranges: two colonies on the same planet whose reachable extraction AREAS
     (ECU centre + head spread + head_radius, from pp_char_planets.ext_heads) overlap for the same
     P0 — they compete for the same hotspots and reseating can't escape it. The server returns EVERY
     overlap above a tiny floor with its overlap_pct; the client applies the user's threshold. So a
     range case with far-apart current heads is still flagged (footprint, not head position); far-
     apart CCs, different-P0 areas, a solo colony, and a factory sharing the planet are not.
  2. Depleting deposits: an extractor colony whose install-yield (peak_day, one sample per program
     in pp_colony_yield) has trended DOWN across the last several programs — the deposit is
     exhausting, so a reseat only chases a sinking ceiling. A steady-low (thin-but-flat) planet is
     NOT flagged, nor is one with too few programs to judge, nor an up-trend.
  3. Cross-link: when a colony is BOTH in a range overlap and depleting, the depleting character is
     named as the precedence mover, and its depletion entry is tagged also_overlapping.

Requires DEBUG_PI=true in the server's .env (the local docker-compose default) and the fixture
seeded first:
    docker compose exec -T web python3 scripts/seed_redeploy_fixture.py

Usage:
    python tests/test_redeploy_candidates.py [--url http://localhost:8000]
"""

import argparse
import json
import sys
import urllib.request

CTX_PROXIMITY = 999005
CTX_DEPLETION = 999006
CTX_CROSS = 999007


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def test_proximity(base: str) -> bool:
    print(f"\n{'='*60}\n  Overlapping reachable ranges\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_PROXIMITY}")
    ok = check(status == 200, "200 OK")
    prox = data.get("proximity", [])
    by_loc = {p["location"]: p for p in prox}
    # Server returns every real overlap (client filters by threshold): P1 (coincident, 100%),
    # P6 (~25%, below the 50% default), P8 (the range case). Far CCs / different-P0 / solo / factory
    # must all be absent.
    ok &= check(set(by_loc) == {"? P1", "? P6", "? P8"},
                f"exactly the three overlapping planets returned (got {sorted(by_loc)})")
    p1 = by_loc.get("? P1")
    ok &= check(p1 and p1["overlap_pct"] == 100, f"coincident CCs report 100% overlap (got {p1 and p1['overlap_pct']})")
    ok &= check(p1 and set(p1.get("characters", [])) == {"Test Gale", "Test Hana"}, "both characters named")
    ok &= check(p1 and p1.get("p0_name") == "Aqueous Liquids", "the shared P0 is named")
    # The P1 the P0 refines to (what the player's templates are named by) is surfaced too.
    ok &= check(bool(p1 and p1.get("p1_name")), f"the refined P1 name is surfaced (got {p1 and p1.get('p1_name')})")
    # planet_type drives the command-centre shopping list — must be returned per finding.
    ok &= check(p1 and p1.get("planet_type") == "Barren", f"P1 finding carries its planet type (got {p1 and p1.get('planet_type')})")
    p8 = by_loc.get("? P8")
    ok &= check(p8 and p8.get("planet_type") == "Lava", f"P8 finding carries its (different) planet type (got {p8 and p8.get('planet_type')})")
    p6 = by_loc.get("? P6")
    ok &= check(p6 and p6["overlap_pct"] == 25, f"P6 reports 25% overlap, below the default (got {p6 and p6['overlap_pct']})")
    # P8: current heads are 0.24 rad apart (no head overlap), but the reachable areas overlap heavily —
    # this is the whole point of the range model over a head-position check.
    p8 = by_loc.get("? P8")
    ok &= check(p8 and p8["overlap_pct"] >= 50, f"P8 (far heads, overlapping ranges) flagged by footprint (got {p8 and p8['overlap_pct']}%)")
    return ok


def test_precedence(base: str) -> bool:
    print(f"\n{'='*60}\n  Cross-link: depleting character takes precedence\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_CROSS}")
    ok = check(status == 200, "200 OK")
    prox = data.get("proximity", [])
    dep = data.get("depleting", [])
    ok &= check(len(prox) == 1 and prox[0].get("precedence_character") == "Test Gill",
                f"the depleting character is named as the precedence mover (got {prox and prox[0].get('precedence_character')})")
    gill = next((d for d in dep if d["character"] == "Test Gill"), None)
    ok &= check(gill is not None and gill.get("also_overlapping") is True,
                "Gill's depletion entry is tagged also_overlapping")
    return ok


def test_depletion(base: str) -> bool:
    print(f"\n{'='*60}\n  Reseat exhausted (reseated repeatedly, still marginal)\n{'='*60}")
    status, data = get(f"{base}/api/redeploy-candidates?debug_context_id={CTX_DEPLETION}")
    ok = check(status == 200, "200 OK")
    dep = data.get("depleting", [])
    by_loc = {d["location"]: d for d in dep}
    # Flagged = can't beat prior best across >=3 programs: the declining P1 and the flat-but-never-improving
    # P4. NOT flagged: the up-trend P2 (a reseat is still helping) or the 2-program P3 (too little history).
    ok &= check(set(by_loc) == {"? P1", "? P4"}, f"only the reseat-exhausted colonies flagged (got {sorted(by_loc)})")
    ok &= check(dep[0]["decline_pct"] >= dep[-1]["decline_pct"], "sorted by decline %, largest first")
    clean = by_loc.get("? P1")
    ok &= check(clean and clean["decline_pct"] == 35 and clean["programs"] == 6 and clean["peak_last"] == 26000
                and clean["peak_best"] == 40000, f"declining colony reports 35% off a 40000 best over 6 programs (got {clean})")
    # Head movement confirms the reseats: P1's centroid moved every program (5 confirmed), P4's never did.
    ok &= check(clean and clean.get("reseat_tracked") is True and clean.get("reseats_confirmed") == 5,
                f"moved-heads colony shows 5 confirmed reseats (got {clean and clean.get('reseats_confirmed')})")
    flat = by_loc.get("? P4")
    ok &= check(flat and flat.get("reseat_tracked") is True and flat.get("reseats_confirmed") == 0,
                f"never-moved colony is flagged but shows 0 confirmed reseats (got {flat and flat.get('reseats_confirmed')})")
    # Last-reseat date: the moved-heads colony's newest program install_ts (fixture ts0 + 5 days); the
    # colony has no current ext_heads so reach is unknown → every move dates as a reseat, none a redeploy.
    ok &= check(clean and clean.get("reseat_at") == 1_700_000_000 + 5 * 86400,
                f"moved-heads colony dates its last reseat to the newest program (got {clean and clean.get('reseat_at')})")
    ok &= check(clean and clean.get("redeploy_at") is None, "no redeploy dated without a reach yardstick")
    ok &= check(flat and flat.get("reseat_at") is None, "never-moved colony reports no reseat date")
    # Placements map is wired (a dict) so the UI can name concrete redeploy destinations.
    ok &= check(isinstance(data.get("placements"), dict), "response carries a placements map")
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
        test_precedence(base),
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
