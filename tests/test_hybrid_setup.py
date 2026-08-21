"""
Regression tests for /api/my-setup-plan's "Current setup" demand derivation: hybrid ("BYOS" —
hand-built extraction + on-planet factory chained on one planet, is_hybrid=1) colony exclusion,
and the combined multi-product entry built on top of it.

Root cause #1 (Aiden Lorrent bug report, 2026-07-09): my-setup-plan briefly included hybrid
colonies as if they were regular factories needing externally-supplied P1s, but the supply-side
accounting (_producersOf in analysis.js) deliberately excludes hybrid colonies' own output as
non-exportable (self-consumed on the same planet). That combination manufactured demand supply
could never satisfy — an account whose factories were ALL hybrid read ~0% fed on almost
everything in Setup Analysis, regardless of how well the colonies were actually running. Fixed by
excluding is_hybrid=1 planets from my-setup-plan entirely; they get correct, dedicated analysis
via _hybridReseatSection (client-side, compares each hybrid planet to its own need).

Root cause #2 (SocomTedd report, same day): a player with multiple deployed products had to
switch the Refill tool between each product's "Current setup" plan one at a time, and two
products sharing a P1 input had no shared bookkeeping between their separate views. Fixed by
adding one synthetic "All factories combined" entry (tier=99, always sorts first) whenever an
account has 2+ distinct products, with consumption summed per P1 across all of them.

Requires DEBUG_PI=true in the server's .env (already the local docker-compose default) and the
fixture seeded first:
    docker compose exec -T web python3 scripts/seed_hybrid_fixture.py

Usage:
    python tests/test_hybrid_setup.py [--url http://localhost:8000]
"""

import argparse
import json
import sys
import urllib.request

CTX_HYBRID_ONLY = 999002   # Test Dana: only a hybrid Mechanical Parts colony
CTX_MIXED = 999003         # Test Erin: hybrid Mechanical Parts colony + standalone Coolant factory
CTX_COMBINED = 999004      # Test Fiona: two standalone factories both consuming Precious Metals


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def test_hybrid_only_account_has_no_derived_plans(base: str) -> bool:
    print(f"\n{'='*60}\n  Hybrid-only account: derived plans must be EMPTY\n{'='*60}")
    status, data = get(f"{base}/api/my-setup-plan?debug_context_id={CTX_HYBRID_ONLY}")
    ok = check(status == 200, "200 OK")
    plans = data.get("plans", [])
    ok &= check(plans == [], f"no 'Current setup' plans derived from a hybrid-only account (got {[p['name'] for p in plans]})")
    return ok


def test_mixed_account_only_surfaces_standalone_factory(base: str) -> bool:
    print(f"\n{'='*60}\n  Mixed account: standalone factory surfaces, hybrid colony doesn't\n{'='*60}")
    status, data = get(f"{base}/api/my-setup-plan?debug_context_id={CTX_MIXED}")
    ok = check(status == 200, "200 OK")
    plans = data.get("plans", [])
    names = [p["name"] for p in plans]
    ok &= check(any("Coolant" in n for n in names), f"standalone Coolant factory is present (got {names})")
    ok &= check(not any("Mechanical Parts" in n for n in names),
                f"hybrid Mechanical Parts colony is NOT present (got {names})")
    ok &= check(len(plans) == 1, f"exactly one derived plan (the standalone factory only, got {len(plans)})")
    return ok


def test_combined_entry_sums_shared_input(base: str) -> bool:
    print(f"\n{'='*60}\n  Multi-product account: combined entry sums shared P1s\n{'='*60}")
    status, data = get(f"{base}/api/my-setup-plan?debug_context_id={CTX_COMBINED}")
    ok = check(status == 200, "200 OK")
    plans = data.get("plans", [])
    ok &= check(len(plans) == 3, f"3 plans (2 individual + 1 combined), got {len(plans)}: {[p['name'] for p in plans]}")
    combined = next((p for p in plans if "combined" in p["name"].lower()), None)
    ok &= check(combined is not None, "a combined entry is present")
    if combined is None:
        return ok
    ok &= check(plans[0] is combined, "combined entry sorts first (tier=99, the sensible default pick)")
    ok &= check(combined["factories_count"] == 2, f"combined factories_count sums both (got {combined.get('factories_count')})")
    pm = next((c for c in combined["consumption"] if c["p1_name"] == "Precious Metals"), None)
    ok &= check(pm is not None, "Precious Metals present in combined consumption")
    others = [p for p in plans if p is not combined]
    expected = sum(next((c["units_per_day"] for c in p["consumption"] if c["p1_name"] == "Precious Metals"), 0) for p in others)
    ok &= check(pm is not None and pm["units_per_day"] == expected,
                f"combined Precious Metals units_per_day is the SUM of both products (got {pm and pm['units_per_day']}, expected {expected})")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [
        test_hybrid_only_account_has_no_derived_plans(base),
        test_mixed_account_only_surfaces_standalone_factory(base),
        test_combined_entry_sums_shared_input(base),
    ]
    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} test groups passed")
    print(f"{'='*60}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
