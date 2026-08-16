"""
Smoke tests for the feature-flag system and the Skill-ROI advisor endpoint.

These exercise the PUBLIC surface (no auth): the feature registry, the admin gate on toggling,
and the unauthenticated skill-roi response. Auth-only behaviour (a populated skill-roi for a real
context) is left to manual checks, since it needs a logged-in session.

Usage:
    python test_features.py [--url https://dev.eveindustry.net]
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
# A spread across the groups that still HAVE a flag. The 18 fully-rolled-out ones this list
# used to name were retired on 2026-08-12 — a feature everyone has had for two months is not
# a rollout control, and the registry is the Admin tab's list before it is anything else.
EXPECTED_FEATURES = ["industry", "industry_manual_done", "industry_share", "factory_layout",
                     "redeploy_proximity", "local_market", "reactions_parallel_stages",
                     "reactions_pack_hosts", "reactions_stage_pipeline", "reactions_manual_done"]
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
    code = post_status(f"{base}/api/features/industry", {"state": "public"})
    return check(code == 403, f"anonymous toggle rejected (got HTTP {code})")


def test_feature_group_toggle_gated(base: str) -> bool:
    """Setting a whole GROUP's rung is admin-only, and the gate runs before any validation.

    This endpoint can publish a dozen flags in one request, so an anonymous caller must be
    refused for an unknown group and an invalid state too — a 404 or 400 here would confirm
    which groups exist to somebody with no session.
    """
    print(f"\n{'='*60}\n  POST /api/features/group/<group> is admin-gated\n{'='*60}")
    ok = True
    for path, what in (("Reactions", "real group"),
                       ("Notifications%20%26%20Alerts", "group name with spaces and &"),
                       ("DefinitelyNotAGroup", "unknown group")):
        code = post_status(f"{base}/api/features/group/{path}", {"state": "public"})
        ok &= check(code == 403, f"anonymous group set rejected, {what} (got HTTP {code})")
    code = post_status(f"{base}/api/features/group/Reactions", {"state": "bogus"})
    ok &= check(code == 403, f"anonymous group set rejected before state validation (got HTTP {code})")
    return ok


def test_group_names_the_ui_renders_are_the_ones_the_bulk_control_can_set(base: str) -> bool:
    """The group name `/api/features` reports must be the one `_keys_in_group` resolves.

    The bulk rung control sends back exactly the group name the Admin tab rendered, so these two
    have to agree on every name — including the 'Other' fallback for a feature nobody filed under
    a group, which each side spells out separately. If either drifts, the bulk button 404s on
    precisely the flags the drift covers. Asserted against the LIVE endpoint, because that payload
    is what the UI reads. Deliberately no assertion that every group is in GROUP_ORDER: a group
    missing from it is legal and sorts last (app/features.py), so pinning that would fail on
    supported code.
    """
    print(f"\n{'='*60}\n  rendered group names resolve to the same keys\n{'='*60}")
    from app.features import _keys_in_group

    ok = True
    status, data = get(f"{base}/api/features")
    ok &= check(status == 200, "200 OK")
    api_groups: dict[str, list[str]] = {}
    for f in data.get("features", []):
        api_groups.setdefault(f.get("group") or "Other", []).append(f["key"])
    ok &= check(bool(api_groups), "the endpoint reports at least one group")
    for group, keys in sorted(api_groups.items()):
        ok &= check(_keys_in_group(group) == keys,
                    f"group '{group}': {len(keys)} rendered keys resolve identically server-side")
    return ok


def test_skill_roi_anon(base: str) -> bool:
    print(f"\n{'='*60}\n  GET /api/skill-roi (no session)\n{'='*60}")
    ok = True
    status, data = get(f"{base}/api/skill-roi")
    ok &= check(status == 200, "200 OK")
    ok &= check(data.get("suggestions") == [], "no suggestions without a session")
    return ok


def test_backend_gates_respect_the_whole_ladder(base: str) -> bool:
    """A feature rolled out to TESTERS must actually run for a tester.

    `feature_enabled()` answers only "is it public", so every server-side gate built on it read
    "off" for admins and testers alike — the Admin tab showed a feature on the testers rung while
    the code behind it did nothing. Three Industry features were gated that way and three were
    sitting on `testers` in production. This pins the ladder itself, and that the backend gates
    take a caller so they CAN place someone on it.
    """
    print(f"\n{'='*60}\n  backend feature gates respect admin/tester rungs\n{'='*60}")
    import inspect
    from app import features as F

    ok = True
    # The ladder, with the role lookup stubbed so this tests the rungs and not the DB.
    import app.esi as E
    orig = E.admin_and_tester_status_for_context
    orig_state = F._state_of
    try:
        E.admin_and_tester_status_for_context = lambda c: {1: (True, True), 2: (False, True),
                                                           3: (False, False)}[c]
        expected = {
            "hidden":  {1: False, 2: False, 3: False, None: False},
            "admin":   {1: True,  2: False, 3: False, None: False},
            "testers": {1: True,  2: True,  3: False, None: False},
            "public":  {1: True,  2: True,  3: True,  None: True},
        }
        for state, per_caller in expected.items():
            F._state_of = lambda _k, _s=state: _s
            for ctx, want in per_caller.items():
                got = F.feature_enabled_for("anything", ctx)
                ok &= check(got is want,
                            f"{state}: caller {ctx} sees {got} (expected {want})")
    finally:
        E.admin_and_tester_status_for_context = orig
        F._state_of = orig_state

    # And the gates that sit in front of real features must accept a caller at all — a zero-arg
    # gate cannot consult a rung, which is exactly how this regressed.
    for mod, name in (("app.industry.routing", "routing"),
                      ("app.industry.skills", "skills")):
        m = __import__(mod, fromlist=["_feature_on"])
        params = inspect.signature(m._feature_on).parameters
        ok &= check(len(params) >= 1, f"{name}._feature_on takes a caller")
        src = inspect.getsource(m._feature_on)
        ok &= check("feature_enabled_for" in src,
                    f"{name}._feature_on asks the role-aware gate, not the public-only one")
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


def test_missing_api_route_404s(base: str) -> bool:
    """An unmatched /api/* path must 404, not 405.

    StaticFiles is mounted at "/", so before the catch-all every unmatched API path fell through to
    it — and StaticFiles serves only GET/HEAD, so a POST came back "405 Method Not Allowed". That
    reads as "the endpoint exists but not for this verb", sending you after a bug that isn't there;
    the real cause is normally a replica that hasn't finished rolling out. Cost real debugging time
    once, so it's pinned here.
    """
    print(f"\n{'='*60}\n  unmatched /api/* returns 404, not 405\n{'='*60}")
    ok = True
    for method in ("POST", "GET", "DELETE"):
        req = urllib.request.Request(f"{base}/api/definitely/not/a/route", method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = 0
        ok &= check(code == 404, f"{method} on a missing API path -> {code} (want 404)")
    # A real endpoint must be unaffected by the catch-all.
    code = get_status(f"{base}/api/features")
    ok &= check(code == 200, f"a real API route still works (got HTTP {code})")
    return ok


def test_industry_share_gating(base: str) -> bool:
    """The customer build-status link is the one deliberately PUBLIC industry read — a customer has
    no account. So the two halves must be gated differently: minting/revoking a link is the
    builder's own action (401 anonymous), while reading a status by share id is open and must 404
    on an unknown id rather than leaking whether one exists as a different code."""
    print(f"\n{'='*60}\n  Industry customer build-status links\n{'='*60}")
    ok = True
    code = post_status(f"{base}/api/industry/orders/1/share", {})
    ok &= check(code == 401, f"anonymous share-mint rejected (got HTTP {code})")
    code = delete_status(f"{base}/api/industry/orders/1/share")
    ok &= check(code == 401, f"anonymous share-revoke rejected (got HTTP {code})")
    code = get_status(f"{base}/api/industry/orders/1/share")
    ok &= check(code == 401, f"anonymous share-lookup rejected (got HTTP {code})")
    # Public by design, and an unknown id must be a plain 404.
    code = get_status(f"{base}/api/industry/build-status/definitely-not-a-real-share-id")
    ok &= check(code == 404, f"unknown build-status id -> 404 (got HTTP {code})")
    # The page itself is served to anyone; it explains itself when the link is dead.
    code = get_status(f"{base}/b/definitely-not-a-real-share-id")
    ok &= check(code == 200, f"the customer page loads without a session (got HTTP {code})")
    # The builder's own queue endpoints stay private.
    code = get_status(f"{base}/api/industry/orders")
    ok &= check(code == 401, f"anonymous order list rejected (got HTTP {code})")
    code = get_status(f"{base}/api/industry/progress")
    ok &= check(code == 401, f"anonymous progress read rejected (got HTTP {code})")
    return ok


def main():
    parser = argparse.ArgumentParser()
    # Defaults to the LOCAL container, not production. These suites POST plans and read
    # debug endpoints; pointing them at prod by default meant a plain `python3 test_x.py`
    # ran against live users' service (and silently "passed" by testing prod, not your change).
    # Pass --url explicitly to aim at a deployed environment.
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [
        test_features(base),
        test_feature_toggle_gated(base),
        test_feature_group_toggle_gated(base),
        test_group_names_the_ui_renders_are_the_ones_the_bulk_control_can_set(base),
        test_skill_roi_anon(base),
        test_backend_gates_respect_the_whole_ladder(base),
        test_corp_wallet_gated(base),
        test_reactions_gated(base),
        test_markets_gated(base),
        test_industry_share_gating(base),
        test_groups_gated(base),
        test_delete_account_gated(base),
        test_cleanup_preview_gated(base),
        test_admin_stats_gated(base),
        test_aggregate_yields_gated(base),
        test_debug_memory_gated(base),
        test_debug_user_gated(base),
        test_alert_settings_gated(base),
        test_missing_api_route_404s(base),
    ]
    print(f"\n{'='*60}")
    passed = sum(results)
    print(f"  {passed}/{len(results)} test groups passed")
    print(f"{'='*60}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
