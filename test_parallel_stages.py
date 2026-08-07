#!/usr/bin/env python3
"""A chain's stages reuse one reactor, and idle reactors get put to work.

Two defects, one model. Reaction chain tiers are SEQUENTIAL — tier 0 must finish before tier 1 can
start, because tier 0's output is tier 1's input — and this codebase had two answers to what that
costs in slots: `_concurrent_load` (the assign guard) counted the WORST TIER, while
`_character_capacities` and the dashboard counted EVERY row. A 3-stage chain of one job each was
authorised as needing one slot and then reported as occupying three, and every allocator reading
those numbers planned less work than the account had reactors for.

The invariants:

  * a character's planned load is its worst tier, not its row count — one shared model
    (`_concurrent_load`), asked by the guard, the capacities helper and the dashboard alike;
  * with the flag OFF every one of those numbers is byte-for-byte the old per-row sum;
  * the idle-slot pass spends only capacity nobody claimed, never takes a slot from an allocated
    step, never exceeds the formulas held, and never gives a step more jobs than it has runs;
  * it only ever moves `jobs` — runs, cost and profit are not its business;
  * widening a tier that is not the busiest is FREE (it fits inside slots the character already
    holds) and so happens even with zero idle capacity.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_parallel_stages.py web:/srv/app/ && \
      docker compose exec web python3 test_parallel_stages.py
"""
import json
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                       # noqa: E402
from app.features import ensure_features_table                          # noqa: E402
from app.reactions.advisor import _widen_to_idle_slots                  # noqa: E402
from app.reactions.jobs import (_character_capacities, _concurrent_load,  # noqa: E402
                                ensure_industry_jobs_table, ensure_reaction_assignments_table)

CTX = -98793
CHAR = -9361
FLAG = "reactions_parallel_stages"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _flag(state):
    ensure_features_table()
    con = get_connection()
    try:
        was = con.execute("SELECT state FROM pp_features WHERE key=?", (FLAG,)).fetchone()
        con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, FLAG))
        con.commit()
        return was["state"] if was else None
    finally:
        con.close()


def _character(con, slots_skill=5):
    """One tracked character with real reaction skills — 1 base + Mass Reactions + Advanced gives
    a slot count the capacity helper will actually compute."""
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
                "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?)",
                (CHAR, "StageTester", CTX, "esi-industry.read_character_jobs.v1", slots_skill, slots_skill))
    con.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) "
                "VALUES (?,?,?)", (CHAR, json.dumps([]), time.time()))
    con.commit()


def _assign(con, tid, tier, n=1):
    """`n` plan rows for one product at one tier — what one suggestion's stage looks like."""
    nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_reaction_assignments")
              .fetchone()["n"])
    for i in range(n):
        con.execute(
            "INSERT INTO pp_reaction_assignments (id, character_id, type_id, name, runs, "
            "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?,?)",
            (nxt + i, CHAR, tid, f"Step {tid}", 10, 0.0, 0.0, time.time(), tier))
    con.commit()


def _step(cid, tier, runs, cycle_hours, jobs, cap=None):
    return {"character_id": cid, "tier": tier, "runs": runs, "cycle_hours": cycle_hours,
            "jobs": jobs, "cap": cap}


def main():
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    was = _flag("public")
    con = get_connection()
    try:
        _reset(con)
        _character(con)
        slots = _character_capacities(CTX)
        if not slots:
            print("the seeded character computed no reaction slots — cannot run")
            return 2
        total = slots[0]["free_slots"]
        print(f"character has {total} reaction slots")

        print("a 3-stage chain of one job each occupies ONE slot, not three:")
        _assign(con, 101, 0)
        _assign(con, 102, 1)
        _assign(con, 103, 2)
        free_on = _character_capacities(CTX)[0]["free_slots"]
        check(free_on == total - 1,
              f"free slots drop by the worst tier only (got {free_on}, want {total - 1})")
        _flag("hidden")
        free_off = _character_capacities(CTX)[0]["free_slots"]
        check(free_off == total - 3,
              f"...and with the flag off it is the old per-row sum (got {free_off}, want {total - 3})")
        _flag("public")

        print("...but jobs that DO run together still each cost a slot:")
        _assign(con, 104, 0, n=2)          # tier 0 now holds 3 rows
        free_on = _character_capacities(CTX)[0]["free_slots"]
        check(free_on == total - 3,
              f"three jobs at the same tier are three slots (got {free_on}, want {total - 3})")
        rows = [dict(r) for r in con.execute(
            "SELECT tier_order FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))]
        check(_concurrent_load(rows) == 3,
              "and the guard the capacities helper now shares agrees with it")
        check(len(rows) == 5, "even though five rows are planned in total")

        print("the idle-slot pass spends idle capacity on the biggest time saver:")
        # Two steps on one character, both at the busiest tier, 4 reactors idle. 100 runs x 1h
        # gains far more from a second job than 2 runs x 1h does.
        big, small = _step(1, 0, 100, 1.0, 1), _step(1, 0, 2, 1.0, 1)
        handed = _widen_to_idle_slots([big, small], {1: 4})
        check(handed == 4, f"all four idle reactors are used (got {handed})")
        check(big["jobs"] > small["jobs"],
              f"and they go to the slow step first (big={big['jobs']}, small={small['jobs']})")

        print("it never exceeds the formulas held, or the runs there are:")
        capped = _step(1, 0, 100, 1.0, 1, cap=2)
        check(_widen_to_idle_slots([capped], {1: 8}) == 1 and capped["jobs"] == 2,
              f"a 2-formula step stops at 2 jobs however many reactors are free (got {capped['jobs']})")
        tiny = _step(1, 0, 3, 1.0, 1)
        _widen_to_idle_slots([tiny], {1: 8})
        check(tiny["jobs"] <= 3, f"and a 3-run step never gets more than 3 jobs (got {tiny['jobs']})")

        print("with nothing idle it does nothing — it cannot take a slot from allocated work:")
        held = _step(1, 0, 100, 1.0, 1)
        check(_widen_to_idle_slots([held], {1: 0}) == 0 and held["jobs"] == 1,
              "zero idle reactors, zero extra jobs")

        print("widening a tier BELOW the busiest one is free:")
        # Tier 1 holds 3 jobs, tier 0 holds 1. Tier 0 can grow to 3 without the character's peak
        # load moving at all, so it happens even with no idle capacity to spend.
        low, high = _step(1, 0, 100, 1.0, 1), _step(1, 1, 100, 1.0, 3)
        check(_widen_to_idle_slots([low, high], {1: 0}) == 2 and low["jobs"] == 3,
              f"the quiet tier fills up to the busy one for free (got {low['jobs']})")
        check(high["jobs"] == 3, "and the busy tier is untouched with nothing idle to give it")

        print("it only ever moves `jobs`:")
        s = _step(1, 0, 50, 2.0, 1)
        before = {k: v for k, v in s.items() if k != "jobs"}
        _widen_to_idle_slots([s], {1: 3})
        check(all(s[k] == v for k, v in before.items()),
              "runs, cycle time, tier and character are exactly as they were")
    finally:
        _reset(con)
        con.close()
        _flag(was or "admin")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
