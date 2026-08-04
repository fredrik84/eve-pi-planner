#!/usr/bin/env python3
"""Industry skill advisor: the maths is right, and it shuts up when it should.

Two things are worth pinning here. The first is the SP curve — it is a game constant, and if it's
wrong every "per SP" ranking is wrong in a way that looks entirely plausible on screen. The second
is the suppression rules: an advisor that always has an opinion gets ignored, so "no suggestion" is
a feature and is asserted as hard as the suggestions are.

In-process; run inside the container against a NON-PROD database. Seeds fake characters under a
fabricated context id and removes them in a finally.

    kubectl -n dev exec -i <pod> -- python3 - < test_skill_advisor.py
"""
import sys

from app.db import get_connection
from app.industry import advisor as ad

CTX = -98764
CHAR_BUSY = -9101
CHAR_IDLE = -9102

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _cleanup(con):
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def main():
    print("the SP curve matches CCP's published numbers:")
    # Rank-1 skill: 250 / 1,414 / 8,000 / 45,255 / 256,000 SP for levels I-V. Every other rank is
    # this times the rank, so getting rank 1 right gets them all right.
    for lvl, want in ((1, 250), (2, 1414), (3, 8000), (4, 45255), (5, 256000)):
        got = ad.sp_for_level(1, lvl)
        check(abs(got - want) <= 1, f"rank-1 level {lvl} = {want} SP (got {got})")
    # Rank scales linearly: Advanced Mass Production is rank 8.
    check(ad.sp_for_level(8, 5) == 8 * ad.sp_for_level(1, 5), "SP scales linearly with rank")
    check(ad.sp_to_next(3387, 5) is None, "a skill already at V has no next level")
    check(ad.sp_to_next(3387, 4) == ad.sp_for_level(2, 5) - ad.sp_for_level(2, 4),
          "sp_to_next is the DIFFERENCE between levels, not the total for the next one")

    print("job-time gain is measured against the current level, not from zero:")
    # Industry is -4%/level. 0 -> 1 moves time 1.00 -> 0.96, i.e. throughput +4.17%.
    g01 = ad._time_gain_pct(0.04, 0)
    check(abs(g01 - (1 / 0.96 - 1) * 100) < 0.01, f"level 0->1 is +4.17% throughput (got {g01:.2f})")
    # The SAME +1 level is worth MORE at high level, because the divisor is smaller. This is the
    # part a naive "4% per level" model gets wrong.
    g45 = ad._time_gain_pct(0.04, 4)
    check(g45 > g01, f"a later level is worth more, not less ({g45:.2f}% vs {g01:.2f}%)")

    con = get_connection()
    try:
        _cleanup(con)
        # Two characters with identical skills; what differs is whether their slots are busy, which
        # is supplied by the fake pool below rather than by real ESI jobs.
        for cid, nm in ((CHAR_BUSY, "Busy"), (CHAR_IDLE, "Idle")):
            con.execute(
                "INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
                "mass_production, advanced_mass_production, mass_reactions, "
                "advanced_mass_reactions, industry, advanced_industry) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, nm, CTX, "esi-skills.read_skills.v1", 4, 0, 0, 0, 3, 2))
        con.commit()

        real_feature_on = ad._feature_on
        real_pool = ad._slot_pool
        ad._feature_on = lambda *_a, **_k: True

        def _pool(_ctx, free_mfg):
            """A slot pool with everything trained but a controllable amount of it busy."""
            return {"characters": [{"character_id": CHAR_BUSY, "character_name": "Busy",
                                    "manufacturing_slots": 5, "reaction_slots": 0,
                                    "manufacturing_free": free_mfg, "reaction_free": 0}],
                    "excluded": [],
                    "manufacturing_slots": 5, "manufacturing_free": free_mfg,
                    "reaction_slots": 0, "reaction_free": 0}

        try:
            print("a saturated slot pool gets a slot suggestion:")
            ad._slot_pool = lambda c: _pool(c, 0)          # 5 of 5 busy
            res = ad.industry_skill_advice(CTX)
            slot = [s for s in res["suggestions"] if s["skill"] in
                    ("Mass Production", "Advanced Mass Production")]
            check(len(slot) == 1, f"exactly one slot suggestion for the pool (got {len(slot)})")
            if slot:
                check(abs(slot[0]["gain_pct"] - 20.0) < 0.01,
                      f"+1 slot on a pool of 5 is +20% throughput (got {slot[0]['gain_pct']})")
                # Which skill is cheaper depends on LEVEL, not rank, and the answer is often
                # counter-intuitive: the fixture has Mass Production at IV, so its next level
                # costs rank 2 x (256,000 - 45,255) = 421,490 SP, while Advanced Mass Production
                # 0 -> I costs rank 8 x 250 = 2,000 SP. The rank-8 skill is ~200x cheaper because
                # level V is where the curve explodes. Assert the principle (fewest SP wins), not
                # a guess about which skill that turns out to be.
                mp = ad.sp_to_next(3387, 4)          # Mass Production IV -> V
                amp = ad.sp_to_next(24625, 0)        # Advanced Mass Production 0 -> I
                cheaper = "Mass Production" if mp < amp else "Advanced Mass Production"
                check(slot[0]["skill"] == cheaper,
                      f"it picks the fewest-SP path to +1 slot ({cheaper}: "
                      f"{min(mp, amp):,} SP vs {max(mp, amp):,})")
                check(slot[0]["sp"] == min(mp, amp),
                      "and reports that skill's own SP cost")

            print("an idle slot pool is told to deploy, not to train:")
            ad._slot_pool = lambda c: _pool(c, 3)          # only 2 of 5 busy
            res = ad.industry_skill_advice(CTX)
            slot = [s for s in res["suggestions"] if s["skill"] in
                    ("Mass Production", "Advanced Mass Production")]
            check(not slot, "no slot suggestion while slots sit idle")
            check(any(e["pool"] == "manufacturing" for e in res["enough"]),
                  "the idle pool is reported under 'enough' instead")

            print("job-time skills are offered regardless of utilisation:")
            check(any(s["skill"] == "Industry" for s in res["suggestions"]),
                  "Industry is suggested even with idle slots (it always pays)")
            check(any(s["skill"] == "Advanced Industry" for s in res["suggestions"]),
                  "Advanced Industry is suggested too")

            print("ranking is by gain per SP, not raw gain:")
            ordered = [s["gain_per_msp"] for s in res["suggestions"]]
            check(ordered == sorted(ordered, reverse=True), "suggestions are sorted by gain per SP")

            print("a maxed character is left alone:")
            con.execute("UPDATE pp_characters SET industry=5, advanced_industry=5, "
                        "mass_production=5, advanced_mass_production=5 WHERE context_id=?", (CTX,))
            con.commit()
            ad._slot_pool = lambda c: _pool(c, 0)
            res = ad.industry_skill_advice(CTX)
            check(not res["suggestions"],
                  f"nothing is suggested to a fully trained character (got {res['suggestions']})")

            print("the feature flag gates it:")
            ad._feature_on = lambda *_a, **_k: False
            check(ad.industry_skill_advice(CTX) is None,
                  "industry_skill_advice returns None with the feature off")
        finally:
            ad._feature_on = real_feature_on
            ad._slot_pool = real_pool
    finally:
        _cleanup(con)
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
