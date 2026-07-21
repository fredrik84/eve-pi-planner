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
EXPECTED_FEATURES = ["timeline", "split_extraction", "baskets", "skill_roi", "move_character", "schedule_sync", "pad_fill", "measured_yield", "hybrid_colonies", "measured_yield_blend", "alert_settings", "local_market"]
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


def test_reactions_gated(base: str) -> bool:
    # Open to any logged-in user (not exclusive to any one alliance — group membership only
    # picks the moon-goo pricing source, a group's own price sheet vs. Fuzzworks market prices,
    # see app.groups); still requires being logged in at all, so anonymous requests get 401.
    print(f"\n{'='*60}\n  Reactions endpoints require login (open to all users, group-priced or market-priced)\n{'='*60}")
    ok = True
    code = get_status(f"{base}/api/reactions/opportunities")
    ok &= check(code == 401, f"anonymous reactions-opportunities read rejected (got HTTP {code})")
    code = get_status(f"{base}/api/moon-goo")
    ok &= check(code == 401, f"anonymous moon-goo price list read rejected (got HTTP {code})")
    code = get_status(f"{base}/api/reactions/jobs")
    ok &= check(code == 401, f"anonymous reactions-jobs read rejected (got HTTP {code})")
    code = post_status(f"{base}/api/reactions/suggest", {"isk_budget": 1, "max_chain_depth": 1})
    ok &= check(code == 401, f"anonymous reactions-suggest rejected (got HTTP {code})")
    code = post_status(f"{base}/api/reactions/assign",
                        {"character_id": 1, "type_id": 1, "name": "x", "runs": 1, "input_cost": 1, "reward": 1})
    ok &= check(code == 401, f"anonymous reactions-assign rejected (got HTTP {code})")
    code = delete_status(f"{base}/api/reactions/assign/1")
    ok &= check(code == 401, f"anonymous reactions-unassign rejected (got HTTP {code})")
    code = delete_status(f"{base}/api/reactions/assign")
    ok &= check(code == 401, f"anonymous reactions-clear-all rejected (got HTTP {code})")
    code = get_status(f"{base}/api/reactions/shopping-list")
    ok &= check(code == 401, f"anonymous reactions-shopping-list rejected (got HTTP {code})")
    code = get_status(f"{base}/api/reactions/fuel-blocks")
    ok &= check(code == 401, f"anonymous reactions-fuel-blocks rejected (got HTTP {code})")
    code = get_status(f"{base}/api/reactions/account-settings")
    ok &= check(code == 401, f"anonymous reactions-account-settings read rejected (got HTTP {code})")
    code = put_status(f"{base}/api/reactions/account-settings",
                       {"import_isk_per_m3": 1, "export_isk_per_m3": 1, "export_collateral_pct": 0.01})
    ok &= check(code == 401, f"anonymous reactions-account-settings write rejected (got HTTP {code})")
    code = delete_status(f"{base}/api/reactions/account-settings")
    ok &= check(code == 401, f"anonymous reactions-account-settings reset rejected (got HTTP {code})")
    return ok


def test_markets_gated(base: str) -> bool:
    # Followed-market config + local/structure market search are per-account (require_context),
    # so anonymous requests get 401 — same shape as the reactions account-settings endpoints.
    print(f"\n{'='*60}\n  Market config endpoints require login\n{'='*60}")
    ok = True
    code = get_status(f"{base}/api/markets")
    ok &= check(code == 401, f"anonymous markets-list rejected (got HTTP {code})")
    code = get_status(f"{base}/api/markets/search?q=jita")
    ok &= check(code == 401, f"anonymous markets-search rejected (got HTTP {code})")
    code = post_status(f"{base}/api/markets", {"kind": "region", "location_id": 1, "name": "x"})
    ok &= check(code == 401, f"anonymous markets-add rejected (got HTTP {code})")
    code = delete_status(f"{base}/api/markets/1")
    ok &= check(code == 401, f"anonymous markets-delete rejected (got HTTP {code})")
    code = post_status(f"{base}/api/markets/reorder", {"order": [1], "scope": "account"})
    ok &= check(code == 401, f"anonymous markets-reorder rejected (got HTTP {code})")
    code = post_status(f"{base}/api/markets/reader", {"character_id": 1})
    ok &= check(code == 401, f"anonymous markets-reader rejected (got HTTP {code})")
    code = post_status(f"{base}/api/markets/complete", {})
    ok &= check(code == 401, f"anonymous markets-complete rejected (got HTTP {code})")
    return ok


def test_groups_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  Group management endpoints are gated\n{'='*60}")
    ok = True
    # Site-admin-only CRUD: 403 for anonymous (require_admin, matches test_corp_wallet_gated's shape).
    code = get_status(f"{base}/api/admin/groups")
    ok &= check(code == 403, f"anonymous groups-list rejected (got HTTP {code})")
    code = post_status(f"{base}/api/admin/groups", {"name": "x", "alliance_id": 1})
    ok &= check(code == 403, f"anonymous group-create rejected (got HTTP {code})")
    code = post_status(f"{base}/api/admin/groups/1/managers", {"character_name": "x"})
    ok &= check(code == 403, f"anonymous manager-add rejected (got HTTP {code})")
    code = post_status(f"{base}/api/admin/groups/1/pages", {"page_key": "reactions"})
    ok &= check(code == 403, f"anonymous page-allow rejected (got HTTP {code})")
    # Caller-facing (require_context): 401 for anonymous, same convention as reactions above.
    code = get_status(f"{base}/api/groups/mine")
    ok &= check(code == 401, f"anonymous groups-mine rejected (got HTTP {code})")
    # Group-scoped moon-goo writes (require_context + is_group_manager): 401 for anonymous.
    code = post_status(f"{base}/api/moon-goo/1/row", {"type_id": 1, "sell_price": 1, "stock": 1})
    ok &= check(code == 401, f"anonymous group-scoped moon-goo write rejected (got HTTP {code})")
    return ok


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


def test_debug_user_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/admin/debug/user is admin-gated\n{'='*60}")
    code = get_status(f"{base}/api/admin/debug/user?character_name=nobody")
    return check(code == 403, f"anonymous user debug lookup rejected (got HTTP {code})")


def put_status(url: str, body: dict) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_alert_settings_gated(base: str) -> bool:
    print(f"\n{'='*60}\n  /api/alert-settings requires authentication\n{'='*60}")
    ok = True
    code = get_status(f"{base}/api/alert-settings")
    ok &= check(code == 401, f"anonymous GET rejected (got HTTP {code})")
    body = {"expiring_hours": 3, "storage_warn_pct": 80, "storage_high_pct": 95,
            "storage_high_ttf_hours": 2, "storage_urgent_hours": 3}
    code = put_status(f"{base}/api/alert-settings", body)
    ok &= check(code == 401, f"anonymous PUT rejected (got HTTP {code})")
    code = post_status(f"{base}/api/alert-settings/reset", {})
    ok &= check(code == 401, f"anonymous reset rejected (got HTTP {code})")
    return ok


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
        test_reactions_gated(base),
        test_markets_gated(base),
        test_groups_gated(base),
        test_delete_account_gated(base),
        test_cleanup_preview_gated(base),
        test_admin_stats_gated(base),
        test_aggregate_yields_gated(base),
        test_debug_memory_gated(base),
        test_debug_user_gated(base),
        test_alert_settings_gated(base),
    ]
    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} test groups passed")
    print(f"{'='*60}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
