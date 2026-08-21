#!/usr/bin/env python3
"""Required-skills-to-build: the account really is told what it can't install, and why.

The invariant that matters most here is that **skills do not pool**. One character installs one
job, so an account whose skills are split across two toons cannot build the thing, and any check
that unions them is wrong in the one direction that costs the user a failed plan. That case gets
its own assertion below.

In-process, so run it inside the container. It seeds fake rows (a fabricated context id, fake
characters and fake blueprint_skills entries, all namespaced by the constants below) and deletes
them again in a finally. Point it at a NON-PROD database — it writes to shared tables.

    kubectl -n dev exec -i <pod> -- python3 - < tests/test_required_skills.py
"""
import sys

try: import _bootstrap  # noqa: F401
except ModuleNotFoundError: from tests import _bootstrap  # noqa: F401
from app.db import get_connection
from app.industry import skills as sk

# Fabricated ids, chosen far outside real ranges so a failed cleanup can never collide with real
# data and is trivially greppable.
CTX = -98765
CHAR_A = -9001          # the specialist: has the capital skill, lacks the rig skill
CHAR_B = -9002          # the generalist: has the rig skill, lacks the capital skill
CHAR_C = -9003          # never scanned — no skill rows at all
BP_MFG = -7001
BP_RX = -7002
BP_RX2 = -7003
SKILL_CAP = -6001       # "Capital Ship Construction"
SKILL_RIG = -6002       # "Rig Mastery"
SKILL_FREE = -6003      # a level-0 requirement
PROD_MFG = -5001
PROD_RX = -5002
PROD_RX2 = -5003

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _cleanup(con):
    con.execute("DELETE FROM pp_char_skills WHERE character_id IN (?,?,?)", (CHAR_A, CHAR_B, CHAR_C))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM blueprint_skills WHERE blueprint_type_id IN (?,?,?)",
                (BP_MFG, BP_RX, BP_RX2))
    con.execute("DELETE FROM types WHERE type_id IN (?,?,?)", (SKILL_CAP, SKILL_RIG, SKILL_FREE))
    con.commit()


# The two graph shapes plan_skill_gaps consumes, hand-built so the test doesn't need a real plan.
MFG = {PROD_MFG: {"blueprint_type_id": BP_MFG}}
RX = {PROD_RX: {"reaction_id": BP_RX}, PROD_RX2: {"reaction_id": BP_RX2}}
REQUIREMENTS = [
    {"type_id": PROD_MFG, "name": "Test Capital", "activity": "manufacturing"},
    {"type_id": PROD_RX, "name": "Test Reaction", "activity": "reaction"},
    # A second reaction that B CAN install — without it every step is beyond every character and
    # the "prefers a capable character" assertion below would pass for the wrong reason.
    {"type_id": PROD_RX2, "name": "Test Easy Reaction", "activity": "reaction"},
]


def main():
    sk.ensure_char_skills_table()
    con = get_connection()
    try:
        # blueprint_skills is an SDE table, normally created by scripts/build_sde.py. Create it
        # here if the database predates the backfill, so this test can run against a DB that
        # hasn't rebuilt its SDE yet. Identical DDL to _BP_SKILLS_DDL — keep them in step.
        con.execute("""
            CREATE TABLE IF NOT EXISTS blueprint_skills (
                blueprint_type_id INTEGER NOT NULL,
                activity          TEXT    NOT NULL,
                skill_type_id     INTEGER NOT NULL,
                level             INTEGER NOT NULL,
                PRIMARY KEY (blueprint_type_id, activity, skill_type_id)
            )
        """)
        con.commit()
        _cleanup(con)
        # Skill names come from the SDE `types` table, like every other type name.
        for tid, nm in ((SKILL_CAP, "Test Capital Construction"), (SKILL_RIG, "Test Rig Mastery"),
                        (SKILL_FREE, "Test Injected Only")):
            con.execute("INSERT INTO types (type_id, name, group_id) VALUES (?,?,0)", (tid, nm))
        # The manufacturing step needs BOTH real skills, plus one level-0 requirement that must
        # never be reported (ESI cannot tell us whether an uninjected skill is missing).
        for sid, lvl in ((SKILL_CAP, 4), (SKILL_RIG, 3), (SKILL_FREE, 0)):
            con.execute("INSERT INTO blueprint_skills VALUES (?,?,?,?)",
                        (BP_MFG, "manufacturing", sid, lvl))
        con.execute("INSERT INTO blueprint_skills VALUES (?,?,?,?)", (BP_RX, "reaction", SKILL_RIG, 5))
        con.execute("INSERT INTO blueprint_skills VALUES (?,?,?,?)", (BP_RX2, "reaction", SKILL_RIG, 3))
        for cid, nm in ((CHAR_A, "Specialist"), (CHAR_B, "Generalist"), (CHAR_C, "Unscanned")):
            con.execute("INSERT INTO pp_characters (character_id, character_name, context_id) "
                        "VALUES (?,?,?)", (cid, nm, CTX))
        # A has the capital skill but not the rig one; B is the mirror image. Neither can install
        # the manufacturing job alone — together they "could", which is exactly the wrong answer.
        #
        # B's Rig Mastery is IV, not V, on purpose. At V it satisfies the reaction outright, the
        # reaction step correctly drops out of the report, and the two reaction assertions below
        # stop testing anything. At IV each step has a DIFFERENT closest character (A for the
        # manufacturing step, B for the reaction), which is the property worth pinning: the choice
        # is made per step, not once per plan.
        for cid, sid, lvl in ((CHAR_A, SKILL_CAP, 5), (CHAR_A, SKILL_RIG, 0),
                              (CHAR_B, SKILL_CAP, 0), (CHAR_B, SKILL_RIG, 4)):
            con.execute("INSERT INTO pp_char_skills VALUES (?,?,?)", (cid, sid, lvl))
        con.commit()

        real_feature_on = sk._feature_on
        sk._feature_on = lambda *_a, **_k: True
        try:
            g = sk.plan_skill_gaps(CTX, REQUIREMENTS, MFG, RX)
        finally:
            sk._feature_on = real_feature_on

        print("the check runs and finds the seeded gaps:")
        check(g is not None, "plan_skill_gaps returns a result while the feature is on")
        if g is None:
            return 1
        by_type = {s["type_id"]: s for s in g["steps"]}

        print("skills do NOT pool across characters:")
        mfg_step = by_type.get(PROD_MFG)
        check(mfg_step is not None,
              "the manufacturing step is reported as blocked even though A+B jointly have both skills")
        if mfg_step:
            missing_ids = {m["skill_id"] for m in mfg_step["missing"]}
            check(len(mfg_step["missing"]) == 1,
                  f"the best character is short exactly one skill (got {len(mfg_step['missing'])})")
            check(SKILL_FREE not in missing_ids, "a level-0 requirement is never reported as missing")
            check(mfg_step["character_id"] in (CHAR_A, CHAR_B),
                  "the named character is one that actually has skill data")
            check(mfg_step["character_name"] != "Unscanned",
                  "a character with no skill data is never named as 'closest'")
            m = mfg_step["missing"][0]
            check(m["name"].startswith("Test "), f"the missing skill is named, not a bare id ({m['name']})")
            check("need" in m and "have" in m, "the gap reports both the needed and the held level")

        print("the reaction step is checked from the same table:")
        rx_step = by_type.get(PROD_RX)
        check(rx_step is not None, "the reaction step is checked too (one table serves both activities)")
        if rx_step:
            check(rx_step["missing"][0]["need"] == 5, "the reaction's level-5 requirement is carried through")
            check(rx_step["character_id"] == CHAR_B,
                  "the character who is closest on THIS step is chosen per step, not once per plan")

        print("unscanned characters are reported as unknown, not as unskilled:")
        check("Unscanned" in (g["characters_without_data"] or []),
              "the never-scanned character is listed under characters_without_data")
        check(g["blocked_steps"] == len(g["steps"]), "blocked_steps agrees with the step list")

        print("the plan-wide summary rolls up the worst level demanded:")
        summary = {m["skill_id"]: m for m in g["missing"]}
        rig = summary.get(SKILL_RIG)
        check(rig is not None, "the rig skill appears in the plan-wide missing summary")
        if rig:
            check(rig["level"] == 5,
                  f"the summary carries the HIGHEST level any step demands (got {rig['level']})")

        print("the scheduler prefers a character who can actually install the job:")
        from app.industry.schedule import assign_characters
        sk._feature_on = lambda *_a, **_k: True
        try:
            full = sk.analyze_plan_skills(CTX, REQUIREMENTS, MFG, RX)
        finally:
            sk._feature_on = real_feature_on
        elig = full["eligibility"]
        # PROD_RX2 needs Rig III: B (IV) can install it, A (0) cannot. PROD_RX needs Rig V, which
        # NEITHER can do — that's the fallback case, tested separately below.
        check(elig["capable"].get(PROD_RX2) == {CHAR_B},
              f"eligibility names exactly who can install a step (got {elig['capable'].get(PROD_RX2)})")
        # A deliberately gets MORE free slots than B, so a capacity-only assignment would pick A.
        chars = [{"character_id": CHAR_A, "character_name": "Specialist",
                  "manufacturing_slots": 5, "reaction_slots": 5},
                 {"character_id": CHAR_B, "character_name": "Generalist",
                  "manufacturing_slots": 1, "reaction_slots": 1}]
        rx_only = [{"start_hours": 0.0, "tasks": [
            {"type_id": PROD_RX2, "activity": "reaction", "runs": 1, "duration_hours": 1.0}]}]
        assign_characters(rx_only, chars, elig)
        t = rx_only[0]["tasks"][0]
        check(t["character_id"] == CHAR_B,
              "the capable character wins even though the other has 5x the free slots")
        check(t.get("skill_ok") is True, "an assignment that works is stamped skill_ok=True")

        print("capacity still decides among equally-capable characters:")
        # Nobody is capable of the manufacturing step, so the tier is flat and the old
        # most-free-capacity rule must still apply — A has more slots, so A takes it.
        mfg_only = [{"start_hours": 0.0, "tasks": [
            {"type_id": PROD_MFG, "activity": "manufacturing", "runs": 1, "duration_hours": 1.0}]}]
        assign_characters(mfg_only, chars, elig)
        t = mfg_only[0]["tasks"][0]
        check(t["character_id"] == CHAR_A, "with no capable candidate, most-free-capacity still wins")
        check(t.get("skill_ok") is False,
              "falling back to someone who can't install it is stamped skill_ok=False, not hidden")

        print("without eligibility the behaviour is byte-for-byte the old one:")
        legacy = [{"start_hours": 0.0, "tasks": [
            {"type_id": PROD_RX, "activity": "reaction", "runs": 1, "duration_hours": 1.0}]}]
        assign_characters(legacy, chars)
        t = legacy[0]["tasks"][0]
        check(t["character_id"] == CHAR_A, "capacity-only assignment is unchanged when not skill-aware")
        check("skill_ok" not in t,
              "skill_ok is absent (not None/False) when no check ran, so 'unchecked' never reads as 'fine'")

        print("the feature flag really gates it:")
        sk._feature_on = lambda *_a, **_k: False
        try:
            off = sk.plan_skill_gaps(CTX, REQUIREMENTS, MFG, RX)
        finally:
            sk._feature_on = real_feature_on
        check(off is None, "plan_skill_gaps returns None with the feature off, so the key is omitted")

        print("the write path is gated too:")
        sk._feature_on = lambda *_a, **_k: False
        try:
            sk.store_character_skills(CHAR_C, {SKILL_CAP: 5})
        finally:
            sk._feature_on = real_feature_on
        n = con.execute("SELECT COUNT(*) AS n FROM pp_char_skills WHERE character_id=?",
                        (CHAR_C,)).fetchone()["n"]
        check(n == 0, "store_character_skills writes nothing while the feature is off")

        sk._feature_on = lambda *_a, **_k: True
        try:
            sk.store_character_skills(CHAR_C, {SKILL_CAP: 5, SKILL_RIG: 2})
            n = con.execute("SELECT COUNT(*) AS n FROM pp_char_skills WHERE character_id=?",
                            (CHAR_C,)).fetchone()["n"]
            check(n == 2, f"store_character_skills writes the full list when on (got {n})")
            # Replace-not-merge: a second write with fewer skills must not leave the old ones behind.
            sk.store_character_skills(CHAR_C, {SKILL_CAP: 5})
            n = con.execute("SELECT COUNT(*) AS n FROM pp_char_skills WHERE character_id=?",
                            (CHAR_C,)).fetchone()["n"]
            check(n == 1, f"a refresh REPLACES the skill list rather than merging into it (got {n})")
            # An empty fetch is a failure, not "this character has no skills" — never wipe.
            sk.store_character_skills(CHAR_C, {})
            n = con.execute("SELECT COUNT(*) AS n FROM pp_char_skills WHERE character_id=?",
                            (CHAR_C,)).fetchone()["n"]
            check(n == 1, "an empty skill map is treated as a failed fetch and leaves the list alone")
        finally:
            sk._feature_on = real_feature_on
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
