"""Alliance-shared build structures: suggestions, adoption, and the scoping that keeps one
alliance's buildings out of another's plans.

Pure-function tests — `_list_markets` and `member_group` are stubbed, so nothing here touches the
live markets table.
"""
import sys
sys.path.insert(0, ".")

import app.markets as M

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


def _struct(i, loc, name, **kw):
    d = {"id": i, "kind": "structure", "location_id": loc, "name": name, "build_mfg": 1,
         "build_rx": 0, "price_from": 0}
    d.update(kw)
    return d


class _Stub:
    """Stands in for the two DB reads `_suggested_structures` makes."""

    def __init__(self, groups, markets, on=True):
        self.groups, self.markets, self.on = groups, markets, on
        self._real = (M.member_group, M._list_markets, M._group_structures_on)

    def __enter__(self):
        M.member_group = lambda ctx: self.groups.get(ctx)
        M._list_markets = lambda kind, oid: list(self.markets.get((kind, oid), []))
        M._group_structures_on = lambda ctx: self.on
        return self

    def __exit__(self, *a):
        M.member_group, M._list_markets, M._group_structures_on = self._real


def test_an_alliances_structures_are_suggested_to_its_members_only():
    """Two alliances on one install. A structure shared to one group must never appear to the
    other: they don't dock there, and a plan routed into somebody else's citadel is worse than no
    suggestion at all."""
    print("test_an_alliances_structures_are_suggested_to_its_members_only")
    groups = {1: {"id": 10, "name": "B0SS", "alliance_id": 99007887},
              2: {"id": 20, "name": "Other", "alliance_id": 12345},
              3: None}
    markets = {
        ("group", 10): [_struct(100, 6001, "MTO2-2 - glizzymaker3000")],
        ("group", 20): [_struct(200, 6002, "Somewhere Else - Their Azbel")],
        ("account", 1): [], ("account", 2): [], ("account", 3): [],
    }
    with _Stub(groups, markets):
        mine = M._suggested_structures(1, [])
        theirs = M._suggested_structures(2, [])
        nobody = M._suggested_structures(3, [])
    check("a member is suggested their own alliance's structure",
          [m["name"] for m in mine] == ["MTO2-2 - glizzymaker3000"])
    check("and never the other alliance's",
          [m["name"] for m in theirs] == ["Somewhere Else - Their Azbel"])
    check("an account in no group is suggested nothing", nobody == [])
    check("a suggestion names the alliance it came from", mine[0]["group_name"] == "B0SS")
    check("and is marked as a suggestion, not a structure they have", mine[0]["suggested"] is True)


def test_a_structure_you_already_have_is_not_suggested_back_to_you():
    """Suggestions are what's MISSING. Re-offering a building the member has described themselves
    invites a second row for one structure, which is two rig answers that can disagree."""
    print("test_a_structure_you_already_have_is_not_suggested_back_to_you")
    groups = {1: {"id": 10, "name": "B0SS", "alliance_id": 99007887}}
    shared = [_struct(100, 6001, "glizzymaker3000"), _struct(101, 6002, "posthus")]
    own = [_struct(5, 6001, "glizzymaker3000", me_rig=2)]
    markets = {("group", 10): shared, ("account", 1): own}
    with _Stub(groups, markets):
        sugg = M._suggested_structures(1, own)
    check("only the one they lack is suggested", [m["location_id"] for m in sugg] == [6002])
    check("their own answer for the shared one is untouched", own[0]["me_rig"] == 2)


def test_suggestions_are_inert_until_adopted():
    """Nothing about a member's plan may move because somebody else described a building.
    `build_structures` — what the planner routes jobs into — stays the account's own list."""
    print("test_suggestions_are_inert_until_adopted")
    groups = {1: {"id": 10, "name": "B0SS", "alliance_id": 99007887}}
    markets = {("group", 10): [_struct(100, 6001, "glizzymaker3000")],
               ("account", 1): [_struct(5, 6003, "my own tower")]}
    with _Stub(groups, markets):
        sites = M.build_structures(1)
    check("the planner sees only what the account itself configured",
          [m["location_id"] for m in sites] == [6003])


def test_the_feature_gate_hides_suggestions_entirely():
    """Off means off: no suggestion, not a suggestion with nothing in it."""
    print("test_the_feature_gate_hides_suggestions_entirely")
    groups = {1: {"id": 10, "name": "B0SS", "alliance_id": 99007887}}
    markets = {("group", 10): [_struct(100, 6001, "glizzymaker3000")], ("account", 1): []}
    with _Stub(groups, markets, on=False):
        check("nothing is suggested while the flag is off", M._suggested_structures(1, []) == [])


def test_only_structures_are_shareable():
    """A followed REGION market is not a building — it can't be adopted as a build site and must
    not turn up in a list of buildings to add."""
    print("test_only_structures_are_shareable")
    groups = {1: {"id": 10, "name": "B0SS", "alliance_id": 99007887}}
    region = {"id": 300, "kind": "region", "location_id": 7001, "name": "The Forge",
              "build_mfg": 0, "build_rx": 0, "price_from": 1}
    markets = {("group", 10): [region, _struct(100, 6001, "glizzymaker3000")], ("account", 1): []}
    with _Stub(groups, markets):
        sugg = M._suggested_structures(1, [])
    check("a region is never suggested as a building",
          [m["kind"] for m in sugg] == ["structure"])


if __name__ == "__main__":
    test_an_alliances_structures_are_suggested_to_its_members_only()
    test_a_structure_you_already_have_is_not_suggested_back_to_you()
    test_suggestions_are_inert_until_adopted()
    test_the_feature_gate_hides_suggestions_entirely()
    test_only_structures_are_shareable()
    if _failed:
        print(f"\n{_failed} of {_passed + _failed} checks FAILED.")
        sys.exit(1)
    print(f"\nAll {_passed} checks passed.")
