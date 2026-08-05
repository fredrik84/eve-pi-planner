#!/usr/bin/env python3
"""Placeholder characters contribute the Industry job slots their owner DECLARED — and nothing else.

A placeholder ("dummy") has no ESI token, no scopes and no skills, so every other reader of a
character row has to keep treating it as unknown. The invariants worth pinning, in both directions:

  * a placeholder contributes exactly the slots it declares, per pool, 0 included (a builder alt
    that manufactures but never reacts is the normal case, not an edge case);
  * the REAL-character path is untouched — skills still decide, and a real character with no skill
    data is still excluded rather than credited with its free base slot;
  * declared slots never look like measured ones: `skill_time_basis` must stay "assumed" for an
    account whose only "data" is a placeholder, or every job time on it silently drops ~47%;
  * a placeholder is `unknown` to the skill-aware install check, never "proven incapable" — the
    former is true and leaves it usable, the latter is a claim we have no basis for.

In-process; run inside the container against a NON-PROD database. Seeds characters under a
fabricated context id and removes them in a finally.

    docker compose cp test_placeholder_slots.py web:/srv/app/ && \
      docker compose exec web python3 test_placeholder_slots.py
"""
import sys

sys.path.insert(0, ".")
from app.db import get_connection                      # noqa: E402
from app.industry import slots as slotmod              # noqa: E402
from app.industry.graph import account_industry_time_mults   # noqa: E402
from app.industry.schedule import skill_tier           # noqa: E402

CTX = -98771
SKILLS_SCOPE = "esi-skills.read_skills.v1"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _seed_real(con, cid, name, mp=0, amp=0, mr=0, amr=0, scopes=SKILLS_SCOPE):
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, scopes, is_dummy, "
        "mass_production, advanced_mass_production, mass_reactions, advanced_mass_reactions) "
        "VALUES (?,?,?,?,0,?,?,?,?)", (cid, name, CTX, scopes, mp, amp, mr, amr))


def _seed_placeholder(con, cid, name, mfg=0, rx=0):
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, is_dummy, "
        "dummy_mfg_slots, dummy_rx_slots) VALUES (?,?,?,1,?,?)", (cid, name, CTX, mfg, rx))


def _reset(con):
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _pool(flag_on=True):
    """The pool with the feature forced on/off — the flag's live state is an admin setting, so a
    test must not assert against it (see the testing rules: pin invariants, not runtime state)."""
    orig = slotmod._placeholders_on
    slotmod._placeholders_on = lambda ctx: flag_on
    try:
        return slotmod._slot_pool(CTX)
    finally:
        slotmod._placeholders_on = orig


def _by_name(pool, key="characters"):
    return {c["character_name"]: c for c in pool[key]}


def main():
    from app.esi import ensure_char_tables
    ensure_char_tables()          # brings dummy_mfg_slots/dummy_rx_slots into an older DB
    con = get_connection()
    try:
        _reset(con)

        print("a placeholder contributes exactly the slots it declares:")
        _seed_placeholder(con, -9301, "PhBuilder", mfg=10, rx=0)
        _seed_placeholder(con, -9302, "PhReactor", mfg=0, rx=6)
        con.commit()
        pool = _pool()
        chars = _by_name(pool)
        check(pool["manufacturing_slots"] == 10, f"10 mfg declared → 10 in the pool (got {pool['manufacturing_slots']})")
        check(pool["reaction_slots"] == 6, f"6 rx declared → 6 in the pool (got {pool['reaction_slots']})")
        check(chars["PhBuilder"]["manufacturing_slots"] == 10 and chars["PhBuilder"]["reaction_slots"] == 0,
              "a manufacturing-only placeholder gets 0 reaction slots, not a free base one")
        check(chars["PhReactor"]["manufacturing_slots"] == 0 and chars["PhReactor"]["reaction_slots"] == 6,
              "and the mirror case for a reactions-only placeholder")
        check(all(c["is_placeholder"] for c in pool["characters"]),
              "every placeholder is flagged is_placeholder so the UI can never show it as connected")
        check(pool["manufacturing_free"] == 10 and pool["reaction_free"] == 6,
              "nothing can be running on a character that isn't connected, so free == total")

        print("0/0 is not capacity — an undeclared placeholder is excluded, with a reason:")
        _seed_placeholder(con, -9303, "PhUnset")
        con.commit()
        pool = _pool()
        excl = _by_name(pool, "excluded")
        check("PhUnset" in excl, "a placeholder declaring no slots is listed in `excluded`")
        check("PhUnset" not in _by_name(pool), "...and contributes nothing to the pool")
        check(pool["manufacturing_slots"] == 10 and pool["reaction_slots"] == 6,
              "totals are unchanged by it")
        check(bool(excl.get("PhUnset", {}).get("reason")), "the exclusion says why, rather than vanishing")

        print("with the flag off, placeholders are invisible to Industry exactly as before:")
        pool = _pool(flag_on=False)
        check(pool["manufacturing_slots"] == 0 and pool["reaction_slots"] == 0,
              f"no placeholder capacity at all (got {pool['manufacturing_slots']}/{pool['reaction_slots']})")
        check(not pool["characters"] and not pool["excluded"],
              "and they aren't even reported as excluded — the feature simply isn't there")

        print("the REAL-character path is unchanged, placeholders present or not:")
        _reset(con)
        _seed_real(con, -9310, "RealBuilder", mp=5, amp=4, mr=0, amr=0)
        con.commit()
        before = _pool()
        _seed_placeholder(con, -9311, "PhAlt", mfg=3, rx=3)
        con.commit()
        after = _pool()
        real_before, real_after = _by_name(before)["RealBuilder"], _by_name(after)["RealBuilder"]
        check(real_before == real_after,
              "a real character's row is byte-for-byte the same with a placeholder on the account")
        check(real_after["manufacturing_slots"] == 10,
              f"1 base + Mass Production V + Advanced IV = 10 (got {real_after['manufacturing_slots']})")
        check(real_after["reaction_slots"] == 0,
              "no reaction skills trained → no reaction slots, base slot NOT counted")
        check(real_after["is_placeholder"] is False, "and it is not marked as a placeholder")
        check(after["manufacturing_slots"] == 13,
              f"the two pools add up (10 real + 3 declared, got {after['manufacturing_slots']})")

        print("the real filters still bite — no scope means no capacity, placeholders or not:")
        _reset(con)
        _seed_real(con, -9320, "NoScope", mp=5, amp=5, scopes="")
        _seed_real(con, -9321, "NoSkills", mp=0, amp=0, mr=0, amr=0)
        _seed_placeholder(con, -9322, "PhAlt2", mfg=4)
        con.commit()
        pool = _pool()
        excl = _by_name(pool, "excluded")
        check("NoScope" in excl, "a character with no skills scope is excluded (we'd be inventing capacity)")
        check("NoSkills" in excl, "a scoped character with no slot skills trained is excluded too")
        check(pool["manufacturing_slots"] == 4,
              f"so the only capacity is the placeholder's declared 4 (got {pool['manufacturing_slots']})")
        check(not excl["NoScope"].get("is_placeholder") and not excl["NoSkills"].get("is_placeholder"),
              "excluded real characters are not mislabelled as placeholders")

        print("a placeholder does NOT make job times look measured:")
        _reset(con)
        _seed_placeholder(con, -9330, "PhOnly", mfg=11, rx=11)
        con.commit()
        mfg, rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "assumed",
              f"an account of nothing but placeholders still reports 'assumed' (got {basis})")
        check(abs(mfg - 0.68) < 1e-9 and abs(rx - 0.85) < 1e-9,
              f"and keeps the V/V fallback multipliers (got {mfg}, {rx})")
        _seed_real(con, -9331, "UnscannedReal", mp=5, amp=5)
        con.commit()
        _mfg, _rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "assumed",
              f"declared SLOTS say nothing about job-TIME skills, which stay unproven (got {basis})")

        print("a placeholder is 'unknown' to the install check, never 'proven incapable':")
        from app.industry.skills import _placeholder_ids
        ids = _placeholder_ids(CTX)
        check(-9330 in ids, "placeholders are identified for the eligibility pass")
        check(-9331 not in ids, "...and real characters are not")
        # This is the tiering the scheduler and the start-now checklist share.
        tier = skill_tier({"capable": {34: {-9331}}, "unknown": ids})
        check(tier(-9330, 34) == 1,
              "a placeholder scores tier 1 (unknown), so it gets work but is never marked skill_ok")
        check(tier(-9331, 34) == 2, "a proven-capable real character still outranks it")
        tier_no_ph = skill_tier({"capable": {34: {-9331}}, "unknown": set()})
        check(tier_no_ph(-9330, 34) == 0 and tier(-9330, 34) == 1,
              "without this the same placeholder would score 0 — 'proven incapable', which we can't claim")
    finally:
        _reset(con)
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
