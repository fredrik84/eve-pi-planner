"""
Smoke tests for the feature-flag system and the Skill-ROI advisor endpoint.

These exercise the PUBLIC surface (no auth): the feature registry, the admin gate on toggling,
and the unauthenticated skill-roi response. Auth-only behaviour (a populated skill-roi for a real
context) is left to manual checks, since it needs a logged-in session.

Usage:
    python test_features.py [--url https://eve-pi.failed.name]
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

# Every key the frontend relies on must exist in the registry. We do NOT assert each flag's
# state VALUE: an admin can change visibility at runtime, so the live state legitimately
# diverges from the code default. The durable invariant is "the key exists and state is one of
# the valid values" (app/features.py VALID_STATES: hidden/admin/testers/public).
EXPECTED_FEATURES = ["timeline", "split_extraction", "baskets", "skill_roi", "move_character", "schedule_sync", "pad_fill", "measured_yield", "hybrid_colonies", "measured_yield_blend"]
VALID_STATES = {"hidden", "admin", "testers", "public"}


def get(url: str):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def post_status(url: str, body: dict) -> int:
    """POST and return the HTTP status (we want the gate to reject anonymous callers)."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def get_status(url: str) -> int:
    """GET and return the HTTP status (for endpoints we expect to reject anonymous callers)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def test_features(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/features\n{'='*60}")
    ok = True
    status, data = get(f"{base}/api/features")
    ok &= check(status == 200, "200 OK")
    feats = {f["key"]: f for f in data.get("features", [])}
    for key in EXPECTED_FEATURES:
        present = key in feats
        ok &= check(present, f"feature '{key}' present")
        if present:
            f = feats[key]
            ok &= check(bool(f.get("label")) and bool(f.get("description")),
                        f"'{key}' has label + description")
            ok &= check(f.get("state") in VALID_STATES,
                        f"'{key}' state is valid (got {f.get('state')!r})")
    ok &= check(data.get("is_admin") is False, "anonymous caller is not admin")
    return ok


def test_feature_toggle_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  POST /api/features/<key> is admin-gated\n{'='*60}")
    code = post_status(f"{base}/api/features/timeline", {"state": "public"})
    return check(code == 403, f"anonymous toggle rejected (got HTTP {code})")


def test_skill_roi_anon(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/skill-roi (no session)\n{'='*60}")
    ok = True
    status, data = get(f"{base}/api/skill-roi")
    ok &= check(status == 200, "200 OK")
    ok &= check(data.get("suggestions") == [], "no suggestions without a session")
    return ok


def test_corp_wallet_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/corp-wallet is admin-gated\n{'='*60}")
    code = get_status(f"{base}/api/corp-wallet")
    return check(code == 403, f"anonymous corp-wallet read rejected (got HTTP {code})")


def delete_status(url: str) -> int:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_delete_account_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  DELETE /api/me requires authentication\n{'='*60}")
    ok = True
    # Anonymous call must be rejected with 401
    code = delete_status(f"{base}/api/me")
    ok &= check(code == 401, f"anonymous DELETE /api/me rejected (got HTTP {code})")
    return ok


def test_cleanup_preview_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/admin/cleanup/preview is admin-gated\n{'='*60}")
    code = get_status(f"{base}/api/admin/cleanup/preview")
    return check(code == 403, f"anonymous cleanup preview rejected (got HTTP {code})")


def test_admin_stats_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/admin/stats is admin-gated\n{'='*60}")
    code = get_status(f"{base}/api/admin/stats")
    return check(code == 403, f"anonymous admin stats rejected (got HTTP {code})")


def test_aggregate_yields_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  POST /api/admin/aggregate-yields is admin-gated\n{'='*60}")
    code = post_status(f"{base}/api/admin/aggregate-yields", {})
    return check(code == 403, f"anonymous aggregate-yields trigger rejected (got HTTP {code})")


def test_debug_memory_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/admin/debug/memory is admin-gated\n{'='*60}")
    code = get_status(f"{base}/api/admin/debug/memory")
    return check(code == 403, f"anonymous memory diagnostics rejected (got HTTP {code})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://eve-pi.failed.name")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [
        test_features(base),
        test_feature_toggle_gated(base),
        test_skill_roi_anon(base),
        test_corp_wallet_gated(base),
        test_delete_account_gated(base),
        test_cleanup_preview_gated(base),
        test_admin_stats_gated(base),
        test_aggregate_yields_gated(base),
        test_debug_memory_gated(base),
    ]
    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} test groups passed")
    print(f"{'='*60}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
