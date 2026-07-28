"""
Minimum-command-centre tests: the level a layout actually NEEDS, vs the level we assume.

Background: templates were generated (and stamped) at CC5 on the assumption that a player
maxes Command Center Upgrades. They don't have to — an extractor planet is power-grid-bound and
barely touches its CPU budget, so a colony sized below the maximum runs on a much lower level.
These assert the invariants behind the "needs only CCn" advice, and the other half of the
promise: every template we generate leaves FIT_HEADROOM of both budgets free, because one built
to 100% on our estimate is the one that won't fit once the player places the pins.

Runs the layout engine in-process — must run INSIDE the container (needs the SDE + deps):
    docker exec eve-pi-planner-web-1 python3 test_min_cc.py
"""

import sys

sys.path.insert(0, ".")

from app.layout import (CC_BUDGET, CMD_CTR_LEVEL, EXTRACTOR_HEADS, FIT_HEADROOM,
                        compute_resources, generate_extractor_layout, generate_layout, min_cc_for)
from app.sde import load_pi_data

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _a_p1(pi):
    return next(tid for tid, t in pi["types"].items() if t.get("pi_tier") == 1)


def test_min_cc_for_monotonic():
    """min_cc_for must return the LOWEST fitting level, and None when nothing fits."""
    print("min_cc_for:")
    room = 1.0 - FIT_HEADROOM
    cpu5, pg5 = CC_BUDGET[5]
    check(min_cc_for(0, 0) == 1, "an empty layout needs level 1")
    check(min_cc_for(cpu5 + 1, 0) is None, "over the level-5 CPU budget → None")
    check(min_cc_for(0, pg5 + 1) is None, "over the level-5 PG budget → None")
    check(min_cc_for(cpu5 * room, pg5 * room) == 5, "filling level 5 to the headroom line needs 5")
    check(min_cc_for(cpu5, pg5) is None,
          "filling level 5 to 100% has no recommendable level — it leaves no headroom")
    for lvl in range(1, 6):
        cpu_max, pg_max = CC_BUDGET[lvl]
        check(min_cc_for(cpu_max * room, pg_max * room) == lvl,
              f"a layout at level {lvl}'s headroom line needs level {lvl}")


def test_everything_we_generate_keeps_headroom():
    """The core promise: nothing we hand a player is built to the wall. Every generated template,
    at every level and planet type, must leave FIT_HEADROOM of both budgets free."""
    print(f"headroom (≥{round(FIT_HEADROOM * 100)}% of both budgets free):")
    pi = load_pi_data()
    p1 = _a_p1(pi)
    room = 1.0 - FIT_HEADROOM
    for pt in ("Barren", "Temperate", "Lava", "Ice", "Storm", "Gas"):
        for cc in range(1, 6):
            r = generate_extractor_layout(p1, planet_type=pt, cc_level=cc)
            s, res = r["summary"], r["planets"][0]["resources"]
            check(not res["over_fit"],
                  f"extractor {s['planet_type']} CC{cc}: PG {res['pg_pct']}% CPU {res['cpu_pct']}% "
                  f"(≤{round(room * 100)}%)")
    # A factory unit is indivisible — a P4 chain either fits or there is nothing smaller to
    # build. So the promise for factories is: keep the headroom, OR be down to a single
    # irreducible unit AND be flagged, so the card warns instead of quietly shipping a template
    # the client will reject.
    for tier in (2, 3, 4):
        tid = next(t for t, v in pi["types"].items() if v.get("pi_tier") == tier)
        for cc in range(2, 6):
            r = generate_layout(tid, "Barren", count=None, cc_level=cc)
            res, sm = r["planets"][0]["resources"], r["summary"]
            ok = (not res["over_fit"]) or sm["count"] == 1
            check(ok, f"P{tier} factory CC{cc}: {sm['count']} unit(s), PG {res['pg_pct']}% "
                      f"CPU {res['cpu_pct']}%")
            if res["over_fit"]:
                check(sm["min_cc"] is None or sm["min_cc"] > cc,
                      f"P{tier} factory CC{cc}: irreducible and doesn't fit → flagged, "
                      f"min_cc={sm['min_cc']}")


def test_extractor_is_power_grid_bound():
    """Extractors are decided by power grid, never CPU — which is why the resource line leads
    with PG on those cards, and why 'needs only CCn' is a PG story."""
    print("extractor binding budget:")
    pi = load_pi_data()
    p1 = _a_p1(pi)
    for pt in ("Barren", "Temperate", "Lava", "Ice"):
        r = generate_extractor_layout(p1, planet_type=pt, cc_level=5)
        s, res = r["summary"], r["planets"][0]["resources"]
        check(res["binding"] == "pg", f"{s['planet_type']}: power grid binds, not CPU")
        check(res["cpu_pct"] < 60, f"{s['planet_type']}: CPU nowhere near binding ({res['cpu_pct']}%)")
        check(s["min_cc"] is not None,
              f"{s['planet_type']}: {s['heads']} heads + {s['facilities_by_tier']['P1']} basics "
              f"→ needs CC{s['min_cc']} (PG {res['pg_pct']}%)")


def test_headroom_costs_capacity_not_correctness():
    """Headroom is a real trade: fewer basics fit than a fill-to-100% model claims. Assert the
    direction (never MORE than the hard-budget fit) so a future tweak to FIT_HEADROOM can't
    silently start over-packing."""
    print("headroom vs hard budget:")
    from app.layout import _structure_ids, build_extractor_template
    pi = load_pi_data()
    p1 = _a_p1(pi)
    for pt in ("Barren", "Storm"):
        struct = _structure_ids(pi, pt)
        struct["planet_type_id"] = pi["name_to_id"].get(f"planet ({pt})".lower())
        for cc in (3, 4, 5):
            got = generate_extractor_layout(p1, planet_type=pt, cc_level=cc)["summary"]
            ptype = got["planet_type"]
            if ptype != pt:
                continue                      # coerced to a planet that yields this P0
            hard = 0
            for nb in range(1, 9):            # most basics that fit ignoring headroom
                b = build_extractor_template(p1, pt, struct, pi, got["heads"], nb, n_launchpads=1)
                b["template"]["CmdCtrLv"] = cc
                if compute_resources(b["template"], struct)["over"]:
                    break
                hard = nb
            fit = got["facilities_by_tier"]["P1"]
            check(fit <= hard, f"{pt} CC{cc}: builds {fit} basics, never more than the {hard} that "
                               f"physically fit")


def test_min_cc_agrees_with_over():
    """min_cc is only meaningful if it agrees with the over-budget flag: a template built at its
    own min_cc must fit, and one built a level below must not."""
    print("min_cc vs over-budget:")
    pi = load_pi_data()
    p1 = _a_p1(pi)
    for pt in ("Barren", "Storm"):
        for cc in (3, 4, 5):
            r = generate_extractor_layout(p1, planet_type=pt, cc_level=cc)
            t, mc = r["planets"][0]["template"], r["summary"]["min_cc"]
            struct = {}
            check(mc is not None and mc <= cc, f"{pt} CC{cc}: min_cc {mc} ≤ built level")
            from app.layout import _structure_ids
            struct = _structure_ids(pi, r["summary"]["planet_type"])
            t2 = dict(t, CmdCtrLv=mc)
            check(not compute_resources(t2, struct)["over_fit"],
                  f"{pt} CC{cc}: fits WITH headroom at its own min_cc ({mc})")
            if mc > 1:
                t3 = dict(t, CmdCtrLv=mc - 1)
                check(compute_resources(t3, struct)["over_fit"],
                      f"{pt} CC{cc}: one level below min_cc ({mc - 1}) loses the headroom")


def test_low_cc_never_exports_over_budget():
    """Heads are the last-resort fit lever. At CC1/CC2 the ECU alone used to blow the budget and
    we shipped an unbuildable template; now heads drop until it fits, and the shortfall is
    reported so the player can see what the low command centre cost them."""
    print("low-CC extractor templates:")
    pi = load_pi_data()
    p1 = _a_p1(pi)
    for pt in ("Barren", "Storm"):
        for cc in (1, 2, 3):
            r = generate_extractor_layout(p1, planet_type=pt, cc_level=cc)
            s, res = r["summary"], r["planets"][0]["resources"]
            check(not res["over_fit"],
                  f"{pt} CC{cc}: fits with headroom ({s['heads']} heads, {s['facilities_by_tier']['P1']} basics, "
                  f"PG {res['pg_pct']}%)")
            check(s["heads_requested"] == EXTRACTOR_HEADS,
                  f"{pt} CC{cc}: reports the {EXTRACTOR_HEADS} heads asked for")
            check(1 <= s["heads"] <= EXTRACTOR_HEADS, f"{pt} CC{cc}: head count stays in range")


def test_full_cc_keeps_all_heads():
    """The head-dropping lever must NOT kick in at the levels the planner models (it assumes 10
    heads of extraction), or plan throughput silently diverges from the exported template."""
    print("head preservation at CC3-5:")
    pi = load_pi_data()
    p1 = _a_p1(pi)
    for pt in ("Barren", "Temperate", "Lava", "Ice", "Oceanic", "Plasma"):
        for cc in (3, 4, 5):
            s = generate_extractor_layout(p1, planet_type=pt, cc_level=cc)["summary"]
            check(s["heads"] == EXTRACTOR_HEADS, f"{s['planet_type']} CC{cc}: all {EXTRACTOR_HEADS} heads kept")


def test_factory_min_cc_matches_packing():
    """max_count is 'the most units that fit', so one more must NOT fit — that's the invariant
    min_cc has to agree with. Where the packing is budget-limited (P2/P3) that means min_cc is
    the built level; where it's limited by unit granularity instead (a P4 chain is large enough
    that only one fits, leaving headroom) min_cc lands BELOW it, which is exactly the advice
    worth surfacing. Dropping the count must always drop the requirement."""
    print("factory min_cc:")
    pi = load_pi_data()
    p4 = next((tid for tid, t in pi["types"].items() if t.get("pi_tier") == 4), None)
    p3 = next((tid for tid, t in pi["types"].items() if t.get("pi_tier") == 3), None)
    p2 = next(tid for tid, t in pi["types"].items() if t.get("pi_tier") == 2)
    for tid in filter(None, (p2, p3, p4)):
        full = generate_layout(tid, "Barren", count=None, cc_level=5)["summary"]
        name, mc = full["product_name"], full["max_count"]
        check(full["min_cc"] is not None, f"{name} packed to max ({mc}) fits CC5")
        over = generate_layout(tid, "Barren", count=mc + 1, cc_level=5)
        check(over["planets"][0]["resources"]["over_fit"],
              f"{name}: one unit past max_count ({mc + 1}) breaks the headroom")
        if mc > 1:
            check(full["min_cc"] == 5, f"{name}: budget-limited packing needs the full CC5")
            lean = generate_layout(tid, "Barren", count=1, cc_level=5)["summary"]
            check(lean["min_cc"] is not None and lean["min_cc"] < 5,
                  f"{name} at count=1 needs only CC{lean['min_cc']}")
        else:
            check(full["min_cc"] <= 5, f"{name}: one chain per planet, needs CC{full['min_cc']}")


def test_required_cc_helpers():
    """The advisor's 'lowest level that runs what you already have' helpers, used per character
    across BOTH their extractor and factory planets."""
    print("required-CC helpers:")
    from app.planner import _required_cc_extractor, _required_cc_factory
    pi = load_pi_data()
    p2 = next(tid for tid, t in pi["types"].items() if t.get("pi_tier") == 2)
    for pt in ("Barren", "Lava"):
        for cc in range(1, 6):
            req = _required_cc_extractor(pt, cc)
            check(1 <= req <= cc, f"extractor {pt} @CC{cc}: required {req} within 1..{cc}")
    for cc in range(1, 6):
        req = _required_cc_factory(p2, "Barren", cc)
        check(1 <= req <= cc, f"factory @CC{cc}: required {req} within 1..{cc}")
    # A character on a maxed-out factory genuinely needs their level — the advice must not tell
    # someone to "stop training" when the packing really does use the budget.
    check(_required_cc_factory(p2, "Barren", 5) == 5,
          "a factory packed at CC5 reports CC5 as required")


if __name__ == "__main__":
    test_min_cc_for_monotonic()
    test_everything_we_generate_keeps_headroom()
    test_extractor_is_power_grid_bound()
    test_headroom_costs_capacity_not_correctness()
    test_min_cc_agrees_with_over()
    test_low_cc_never_exports_over_budget()
    test_full_cc_keeps_all_heads()
    test_factory_min_cc_matches_packing()
    test_required_cc_helpers()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("all passed")
