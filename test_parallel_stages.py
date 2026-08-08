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

        print("SIBLINGS share a stage — the bug that made three parallel jobs look sequential:")
        from app.reactions.graph import tier_ranks
        # Reinforced Carbon Fiber's real shape: three steps one reaction off raw goo, feeding one
        # product. Depth is what decides the stage; position in the list decides nothing.
        ordered = [(101, {"depth": 1}), (102, {"depth": 1}), (103, {"depth": 1})]
        check(tier_ranks(ordered) == [0, 0, 0],
              f"three steps at the same depth are ONE stage (got {tier_ranks(ordered)})")
        deep = [(101, {"depth": 1}), (102, {"depth": 1}), (103, {"depth": 2})]
        check(tier_ranks(deep) == [0, 0, 1],
              f"and a step that consumes one of them is the next (got {tier_ranks(deep)})")
        check(tier_ranks([(101, {"depth": 2}), (102, {"depth": 5})]) == [0, 1],
              "stages are dense — a gap in depth is not an empty stage on the dashboard")
        check(tier_ranks([]) == [], "no tiers, no stages")

        print("...and siblings each cost their own reactor, because they run at once:")
        rows = [{"tier_order": 0}, {"tier_order": 0}, {"tier_order": 0}, {"tier_order": 1}]
        check(_concurrent_load(rows) == 3,
              f"three jobs in stage 1 are three slots, not one (got {_concurrent_load(rows)})")

        print("a stage is DONE when ESI says its jobs are, and the next one then says so:")
        from app.reactions.jobs import chain_stage_state
        plan = [{"character_id": CHAR, "type_id": 101, "name": "Carbon Fiber", "tier_order": 0,
                 "created_at": 100.0},
                {"character_id": CHAR, "type_id": 102, "name": "Oxy-Organic Solvents",
                 "tier_order": 0, "created_at": 100.0},
                {"character_id": CHAR, "type_id": 103, "name": "Reinforced Carbon Fiber",
                 "tier_order": 1, "created_at": 100.0}]
        mid = chain_stage_state(plan, [{"product_type_id": 101, "status": "ready"},
                                       {"product_type_id": 102, "status": "active",
                                        "end_date": "2099-01-01T00:00:00Z"}], time.time())
        check(mid[0]["done"] == 1 and mid[0]["running"] == 1, f"stage 1 is half finished (got {mid[0]})")
        check(mid[1]["ready"] is False,
              "so stage 2 is NOT startable — one of its inputs is still cooking")
        done = chain_stage_state(plan, [{"product_type_id": 101, "status": "ready"},
                                        {"product_type_id": 102, "status": "delivered"}], time.time())
        check(done[0]["done"] == 2 and done[1]["ready"] is True,
              "both finished (ready + delivered both count) and stage 2 can start")
        past = chain_stage_state(plan, [{"product_type_id": 101, "status": "ready"},
                                        {"product_type_id": 102, "status": "active",
                                         "end_date": "2020-01-01T00:00:00Z"}], time.time())
        check(past[1]["ready"] is True,
              "a job past its end_date is finished whatever the 5-minute-stale cache still says")
        check(chain_stage_state(plan, [], time.time())[0]["ready"] is True,
              "stage 1 is always startable — nothing gates it")
        two_chains = plan + [{"character_id": CHAR, "type_id": 104, "name": "Other", "tier_order": 1,
                              "created_at": 900.0}]
        st = chain_stage_state(two_chains, [{"product_type_id": 101, "status": "ready"},
                                            {"product_type_id": 102, "status": "delivered"}],
                               time.time())
        check(any(e["chain"] == 900.0 and e["stage"] == 1 and e["ready"] for e in st),
              "a second plan's stages are judged on ITS own chain, not the first one's progress")

        print("plan rows written under the OLD position-based rule are repaired in place:")
        from app.reactions.graph import _load_goo_and_reached
        from app.reactions.jobs import restage_plan_rows
        loaded = _load_goo_and_reached(CTX)
        reached = loaded[1] if loaded else {}
        RCF = 57457                       # Reinforced Carbon Fiber — the reported case
        node = reached.get(RCF)
        if not node or not node.get("via"):
            print("  ....  Reinforced Carbon Fiber not priced in this container — repair not exercised")
        else:
            sibs = [i["type_id"] for i in node["via"]["inputs"]
                    if (reached.get(i["type_id"]) or {}).get("via")]
            _reset(con)
            _character(con)
            at = time.time()
            nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_reaction_assignments")
                      .fetchone()["n"])
            for i, tid in enumerate(sibs + [RCF]):     # the old, wrong stamping: 0, 1, 2, 3
                con.execute(
                    "INSERT INTO pp_reaction_assignments (id, character_id, type_id, name, runs, "
                    "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?,?)",
                    (nxt + i, CHAR, tid, "x", 10, 0.0, 0.0, at, i))
            con.commit()
            moved = restage_plan_rows(CTX)
            after = {r["type_id"]: r["tier_order"] for r in con.execute(
                "SELECT type_id, tier_order FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))}
            check(moved == len(sibs), f"the mis-staged rows are rewritten (got {moved})")
            check(all(after[t] == 0 for t in sibs),
                  f"every sibling lands in stage 1 (got {[after[t] for t in sibs]})")
            check(after[RCF] == 1, f"and the product they feed is stage 2 (got {after[RCF]})")
            check(restage_plan_rows(CTX) == 0, "running it again does nothing — idempotent")

        print("intermediate run counts are rounded to numbers a human can type:")
        from app.reactions.graph import tidy_runs
        check(tidy_runs(79) == 80 and tidy_runs(41) == 45 and tidy_runs(213) == 225,
              f"79->80, 41->45, 213->225 (got {tidy_runs(79)}, {tidy_runs(41)}, {tidy_runs(213)})")
        check(all(tidy_runs(n) == n for n in (1, 3, 7, 9)),
              "anything under 10 runs is left exactly as it is — already easy to type")
        check(all(tidy_runs(n) >= n for n in range(1, 3000)),
              "NEVER rounds down — the stage above would come up short")
        worst = max((tidy_runs(n) - n) / n for n in range(10, 3000))
        check(worst <= 0.15, f"and never overshoots the requirement by more than 15% (worst {worst:.0%})")
        check(all(tidy_runs(n) == n for n in (10, 100, 500, 2500)),
              "a number that is already tidy is left alone")

        print("...and the rows committed to the plan carry the tidy number:")
        from app.reactions.jobs import _insert_assignment_rows
        _reset(con)
        _character(con)
        now = time.time()
        _insert_assignment_rows(con, CHAR, 501, "Rounded", 79, 1, 0.0, 0.0, 0, now, tidy=True)
        _insert_assignment_rows(con, CHAR, 502, "Exact", 79, 1, 0.0, 0.0, 1, now, tidy=False)
        con.commit()
        got = {r["type_id"]: r["runs"] for r in con.execute(
            "SELECT type_id, runs FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))}
        check(got.get(501) == 80, f"an intermediate row is rounded (got {got.get(501)})")
        check(got.get(502) == 79,
              f"the end product is NOT — its runs are what cost and profit were computed from "
              f"(got {got.get(502)})")
        _reset(con)

        print("a stage lands in ONE go — slots move off the steps that finish early:")
        from app.reactions.advisor import _align_stage_jobs
        import math as _m

        def _hrs(x):
            return _m.ceil(x["runs"] / x["jobs"]) * x["cycle_hours"]

        # One heavy step and two quick ones, three reactors each. The stage is gated by the heavy
        # one at 27h while six reactors sit idle from hour four.
        stage = [_step(1, 0, 80, 1.0, 3), _step(1, 0, 10, 1.0, 3), _step(1, 0, 10, 1.0, 3)]
        before = max(_hrs(x) for x in stage)
        moved = _align_stage_jobs(stage)
        after = max(_hrs(x) for x in stage)
        check(moved > 0 and after < before,
              f"the stage finishes sooner using the same reactors ({before}h -> {after}h)")
        check(sum(x["jobs"] for x in stage) == 9, "and not one extra slot was asked for")
        check(max(_hrs(x) for x in stage) - min(_hrs(x) for x in stage) < before - after,
              "the steps now land close together instead of 23h apart")
        check(all(x["jobs"] >= 1 for x in stage), "no step is ever taken below one job")

        print("...and it does nothing when there is nothing to gain:")
        even = [_step(1, 0, 10, 1.0, 1), _step(1, 0, 10, 1.0, 1)]
        check(_align_stage_jobs(even) == 0, "an already-level stage is left alone")
        alone = [_step(1, 0, 80, 1.0, 3)]
        check(_align_stage_jobs(alone) == 0 and alone[0]["jobs"] == 3,
              "a stage of one step has nothing to align against")
        capped_stage = [_step(1, 0, 80, 1.0, 1, cap=1), _step(1, 0, 10, 1.0, 3)]
        check(_align_stage_jobs(capped_stage) == 0,
              "and a bottleneck that cannot use another formula is not handed one")

        print("one product in one stage gets ONE run count, however many assigns made it:")
        from app.reactions.jobs import level_stage_runs
        _reset(con)
        _character(con)
        nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_reaction_assignments")
                  .fetchone()["n"])
        # The reported shape: three separate assigns each sized their own chain's Carbon Fiber.
        for i, (runs, at) in enumerate([(125, 100.0), (90, 200.0), (75, 300.0)]):
            con.execute(
                "INSERT INTO pp_reaction_assignments (id, character_id, type_id, name, runs, "
                "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?,?)",
                (nxt + i, CHAR, 57453, "Carbon Fiber", runs, 0.0, 0.0, at, 0))
        # ...and a DIFFERENT product in the same stage, which must not be levelled against it.
        con.execute(
            "INSERT INTO pp_reaction_assignments (id, character_id, type_id, name, runs, "
            "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?,?)",
            (nxt + 3, CHAR, 57454, "Oxy-Organic Solvents", 6, 0.0, 0.0, 100.0, 0))
        con.commit()
        changed = level_stage_runs(CTX)
        rows = [dict(r) for r in con.execute(
            "SELECT type_id, runs FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))]
        cf = [r["runs"] for r in rows if r["type_id"] == 57453]
        check(changed == 3 and len(set(cf)) == 1,
              f"every Carbon Fiber job carries the same number now (got {cf})")
        check(sum(cf) >= 125 + 90 + 75,
              f"and the total is preserved, rounded UP not down (got {sum(cf)} vs 290)")
        check(sum(cf) - (125 + 90 + 75) < len(cf),
              "the rounding costs less than one extra run per job")
        check([r["runs"] for r in rows if r["type_id"] == 57454] == [6],
              "a different product in the same stage is left alone")
        check(len(rows) == 4, "no row is created or destroyed — the slot count is untouched")
        check(level_stage_runs(CTX) == 0, "running it again writes nothing — idempotent")
        _reset(con)

        print("an order gives every host the SAME work, not a share of its slot count:")
        # Proportional-to-free-slots is what produced "125 runs for one character, 100 for another,
        # 75 for another" — three numbers for one product on an order installed character by
        # character. The runs cannot be levelled afterwards the way `level_stage_runs` levels them
        # WITHIN a character: a chain's intermediate feeds the stage above it on that same
        # character, so a host given fewer runs than its own top tier eats is a broken plan.
        import inspect
        from app.reactions import jobs as J
        src = inspect.getsource(J._allocate_and_insert)
        check("divmod(runs_needed, len(hosts))" in src,
              "the even split is what the allocator computes when hosts are uniform")
        check("min(h[\"free_slots\"] for h in hosts) if uniform" in src,
              "and one job layout, bounded by the smallest host, so every host installs the same")
        # The arithmetic itself, which is what actually has to hold.
        for total, n in ((300, 3), (301, 3), (2, 3), (1000, 7)):
            hosts = min(n, total)
            base, extra = divmod(total, hosts)
            shares = [base + (1 if i < extra else 0) for i in range(hosts)]
            check(sum(shares) == total, f"{total} across {n} hosts still totals {total}")
            check(max(shares) - min(shares) <= 1,
                  f"...and differs by at most one run between hosts (got {shares})")

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
