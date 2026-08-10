#!/usr/bin/env python3
"""One run count per product, across every character (`reactions_level_runs`).

The reported shape, verbatim: Carbon Fiber at 125 runs on one character, 90 on the next two, 75 on
the fourth — four numbers to read and type for one fungible product, four different finish times,
fifteen reactors doing what twelve would. Each of those four numbers was correct in isolation; each
came from a separate plan that sized its own chain exactly.

The invariants this pins, in the user's own priority order (TODO 28):

  1. every job of a product carries the SAME run count, on every character;
  2. the jobs of a stage land together — run counts are chosen so durations match;
  3. it costs fewer slots, never more than the character actually has free;
  and underneath all three: no chain is left short, no chain loses its last row of a product, the
  top row of a chain (its commitment) is never touched, and the surplus that buys all of it is
  bounded — half again the product's total, three times any one chain's own requirement, and only
  ever spent to LAND a stage or take a reactor back.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_level_runs.py web:/srv/app/ && \
      docker compose exec web python3 test_level_runs.py
"""
import json
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                        # noqa: E402
from app.features import ensure_features_table                           # noqa: E402
from app.reactions.jobs import (_choose_stage_layout, _level_options,    # noqa: E402
                                ensure_industry_jobs_table, ensure_reaction_assignments_table,
                                level_product_runs)

CTX = -98794
CHARS = [(-9371, "Chislen"), (-9372, "Sajkisen414"), (-9373, "Nuori"), (-9374, "Ekaoni")]
CF, OOS, RCF = 57453, 57454, 57455       # Carbon Fiber, Oxy-Organic Solvents, the product above
TP = 57456                               # Thermosetting Polymer — the third sibling of that stage
FLAG = "reactions_level_runs"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    for cid, _ in CHARS:
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (cid,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _characters(con, skill=5):
    for cid, name in CHARS:
        con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
                    "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?)",
                    (cid, name, CTX, "esi-industry.read_character_jobs.v1", skill, skill))
        con.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) "
                    "VALUES (?,?,?)", (cid, json.dumps([]), time.time()))
    con.commit()


def _plan(con, char_id, chain_at, rows):
    """One chain on one character: `rows` is [(type_id, name, runs, jobs, tier)]."""
    for tid, name, runs, jobs, tier in rows:
        for _ in range(jobs):
            con.execute(
                "INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?)",
                (char_id, tid, name, runs, 1000.0, 0.0, chain_at, tier))
    con.commit()


def _cf_rows(con):
    return [dict(r) for r in con.execute(
        "SELECT a.character_id, a.type_id, a.runs, a.tier_order FROM pp_reaction_assignments a "
        "JOIN pp_characters c ON c.character_id=a.character_id WHERE c.context_id=?", (CTX,))]


def main():
    ensure_features_table()
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()

    print("the run counts one product could carry, and what each costs:")
    # The reported case as pure numbers, POOLED: the account needs 1,395 runs of one product
    # (375 + 360 + 360 + 300 across four chains) and has 40 reactors it may spend on the stage.
    # Which character makes a given run is a placement question, not one the run count answers —
    # so the search sees one requirement and one reactor budget.
    opts = _level_options(1395, 40, max_runs=10_000)
    by_runs = {o["runs"]: o for o in opts}
    check(bool(opts), "there are options at all")
    check(125 in by_runs and by_runs[125]["jobs"] == 12,
          f"125 runs everywhere is 12 jobs, not 15 (got {by_runs.get(125, {}).get('jobs')})")
    check(by_runs[125]["surplus"] == 1500 - 1395,
          f"the surplus is 105 runs of stock (got {by_runs[125]['surplus']})")
    check(all(o["surplus"] <= 0.50 * 1395 for o in opts),
          "nothing offered overshoots the real requirement by more than the budget")
    check(all(o["jobs"] * o["runs"] >= 1395 for o in opts),
          "every option still covers the requirement in full")

    print("...and it never asks for a reactor the stage does not have:")
    tight = _level_options(1395, 15, max_runs=10_000)
    check(all(o["jobs"] <= 15 for o in tight), "no option exceeds the reactor budget")
    check(any(o["runs"] == 125 for o in tight), "125 still fits inside 15 reactors")

    print("a small product gets ONE number, in as few reactors as it needs:")
    # The reported case: Oxy-Organic Solvents at 35 runs on some characters and 18 on others,
    # 53 runs in total. Pooled, that is one 53-run job — the two numbers were an artefact of
    # making each chain's intermediate on the character that would consume it.
    oos = _level_options(53, 2, max_runs=125)
    check(oos, "with two reactors there is a common count")
    layout_oos = _choose_stage_layout({OOS: {"cycle": 3.0, "options": oos}})
    check(layout_oos[OOS]["runs"] == 53 and layout_oos[OOS]["jobs"] == 1,
          f"one job of 53 runs (got {layout_oos[OOS]['jobs']} × {layout_oos[OOS]['runs']})")
    check(layout_oos[OOS]["surplus"] == 0, f"and no surplus at all (got {layout_oos[OOS]['surplus']})")
    room = _choose_stage_layout({OOS: {"cycle": 3.0, "options": _level_options(53, 4, 125)}})
    check(room[OOS]["runs"] == 53 and room[OOS]["jobs"] == 1,
          f"spare reactors do not split it further — fewer, fuller jobs is the point "
          f"(got {room[OOS]['jobs']} × {room[OOS]['runs']})")

    print("surplus is spent to LAND a stage, and for nothing else:")
    # Two products in one stage: 300 runs of a 1h reaction (300h) beside 80 of a 3h one (240h).
    # Building 100 of the second instead of the 80 it needs lands both at 300h — worth paying for.
    landed = _choose_stage_layout({CF: {"cycle": 1.0, "options": _level_options(300, 4, 300)},
                                   OOS: {"cycle": 3.0, "options": _level_options(80, 4, 300)}})
    d_fast = landed[CF]["runs"] * 1.0
    d_slow = landed[OOS]["runs"] * 3.0
    check(abs(d_fast - d_slow) <= 0.10 * max(d_fast, d_slow),
          f"the stage lands together ({d_fast}h vs {d_slow}h)")
    check(landed[OOS]["surplus"] > 0, "which took building more of the shorter one than it needs")
    # ...and when landing together is out of reach (the slow one may not exceed its own 80 runs),
    # nothing is paid for getting closer.
    stuck = _choose_stage_layout({CF: {"cycle": 1.0, "options": _level_options(300, 1, 300)},
                                  OOS: {"cycle": 3.0, "options": _level_options(80, 1, 80)}})
    check(stuck[OOS]["surplus"] == 0 and stuck[CF]["surplus"] == 0,
          "a stage that cannot land together buys no partial alignment at all")

    print("a stage is chosen to land in ONE go:")
    # Two products in a stage, one twice as slow per run. Levelling them to the same RUN count
    # would land them 50 hours apart; the same duration is what a single login collects.
    layout = _choose_stage_layout({
        CF: {"cycle": 1.0, "options": _level_options(400, 16, 200)},
        OOS: {"cycle": 2.0, "options": _level_options(200, 16, 100)},
    })
    d_cf = layout[CF]["runs"] * 1.0
    d_oos = layout[OOS]["runs"] * 2.0
    check(abs(d_cf - d_oos) <= 1e-6,
          f"both products finish at the same hour ({d_cf}h vs {d_oos}h)")
    check(layout[CF]["runs"] != layout[OOS]["runs"],
          "which means the same DURATION, not naively the same number")

    print("...and settles on a number you can type when tidy runs are on:")
    # 320 runs across at most 8 reactors, none longer than 70: 64 is the cheapest number in goo,
    # 65 the one you can type without checking. Same five jobs either way — only the surplus moves.
    pair = {CF: {"cycle": 1.0, "options": _level_options(320, 8, 70)}}
    check(_choose_stage_layout(pair)[CF]["runs"] == 64, "off: the cheapest number wins (64)")
    check(_choose_stage_layout(pair, prefer_tidy=True)[CF]["runs"] == 65,
          "on: the tidy number wins for the same job count (65)")

    con = get_connection()
    try:
        _reset(con)
        _characters(con)
        # The reported plan: one chain per character, each with its own Carbon Fiber requirement
        # under a Reinforced Carbon Fiber top row that must not be touched.
        for (cid, _), (runs, jobs, top) in zip(CHARS, [(125, 3, 40), (90, 4, 38),
                                                       (90, 4, 38), (75, 4, 30)]):
            _plan(con, cid, 1000.0 + cid, [(CF, "Carbon Fiber", runs, jobs, 0),
                                           (RCF, "Reinforced Carbon Fiber", top, 1, 1)])
        before = _cf_rows(con)
        cf_before = [r for r in before if r["type_id"] == CF]
        check(len({r["runs"] for r in cf_before}) == 3 and len(cf_before) == 15,
              "before: three different numbers across 15 jobs")

        written = level_product_runs(CTX)
        after = _cf_rows(con)
        cf = [r for r in after if r["type_id"] == CF]
        counts = sorted({r["runs"] for r in cf})
        check(written > 0 and len(counts) == 1,
              f"ONE run count for Carbon Fiber on every character (got {counts})")
        per_char = {}
        for r in cf:
            per_char[r["character_id"]] = per_char.get(r["character_id"], 0) + 1
        check(len(cf) < 15, f"and in fewer jobs than before (15 -> {len(cf)})")
        # POOLED, since 2026-08-08: the requirement is the ACCOUNT's, not each character's, because
        # the output goes to a shared hangar ("we do not need to use the same character to build the
        # entire chain"). So the invariant is the total, and a character keeping a job of its own is
        # explicitly no longer one — consolidating onto fewer reactors is the point.
        check(len(cf) * counts[0] >= 375 + 360 + 360 + 300,
              f"the account still makes every run the plan needed (has {len(cf) * counts[0]})")
        check(len(cf) * counts[0] <= 1.5 * 1395, "the surplus stays inside the budget")
        # The product a chain is FOR gets one number too. It did not until 2026-08-08, and that
        # exclusion is what left a product showing three numbers after the pass had run: the same
        # product is an intermediate under one chain and a standalone job on the next character,
        # and the player types both.
        tops = sorted({r["runs"] for r in after if r["type_id"] == RCF})
        check(len(tops) == 1, f"the product at the top of a chain is levelled as well (got {tops})")
        made_top = len([r for r in after if r["type_id"] == RCF]) * tops[0]
        check(made_top >= 40 + 38 + 38 + 30,
              f"...and the account still makes every run of it that was planned (has {made_top})")
        check(level_product_runs(CTX) == 0, "running it again writes nothing — idempotent")

        print("a stage is never made SLOWER than it already was:")
        longest = max(r["runs"] for r in cf_before)
        check(counts[0] <= longest,
              f"no job runs longer than the longest job already planned ({counts[0]} <= {longest})")

        print("the reported 8-character plan comes out with ONE number per product:")
        # Pasted off the dashboard 2026-08-08, after the pass had supposedly run: Thermosetting
        # Polymer at 100/125/175, Carbon Fiber at 90/100/125. Three numbers on two products, and
        # the reasons were structural — a standalone job is the top of its own chain (excluded from
        # levelling at the time), and a character whose free reactors ran out had its product
        # dropped from the pass entirely rather than levelled to a bigger count.
        REPORTED = [  # character -> [(type_id, total runs, jobs)] exactly as it was on screen
            ("Chislen", [(CF, 375, 3), (OOS, 35, 1), (TP, 375, 3)]),
            ("ekaoni", [(CF, 300, 4), (OOS, 30, 1), (TP, 300, 3)]),
            ("Mimonama", [(CF, 180, 2), (OOS, 18, 1), (TP, 175, 1)]),
            ("Nuori", [(CF, 360, 4), (OOS, 35, 1), (TP, 360, 4)]),
            ("sajkisen", [(CF, 360, 4), (OOS, 35, 1), (TP, 360, 4)]),
            ("Sarmaras", [(CF, 180, 2), (OOS, 18, 1), (TP, 175, 1)]),
            ("Uittaras", [(CF, 180, 2), (OOS, 18, 1), (TP, 175, 1)]),
            ("Vauhilen", [(CF, 180, 2), (OOS, 18, 1), (TP, 175, 1)]),
        ]

        def _build_reported():
            for i, (name, _rows) in enumerate(REPORTED):
                con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (-9500 - i,))
                con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (-9500 - i,))
            con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
            con.commit()
            for i, (name, rows) in enumerate(REPORTED):
                cid = -9500 - i
                con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, "
                            "scopes, mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?)",
                            (cid, name, CTX, "esi-industry.read_character_jobs.v1", 5, 5))
                con.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at)"
                            " VALUES (?,?,?)", (cid, json.dumps([]), time.time()))
                _plan(con, cid, 1000.0 + i,
                      [(tid, str(tid), -(-total // jobs), jobs, 0) for tid, total, jobs in rows]
                      + [(RCF, "Reinforced Carbon Fiber", 40, 1, 1)])

        def _counts(tid):
            return sorted({r["runs"] for r in _cf_rows(con) if r["type_id"] == tid})

        _build_reported()
        check(len(_counts(TP)) == 4 and len(_counts(CF)) == 3,
              "before: four numbers for Thermosetting Polymer, three for Carbon Fiber")
        level_product_runs(CTX)
        for tid, nm in [(CF, "Carbon Fiber"), (TP, "Thermosetting Polymer"),
                        (OOS, "Oxy-Organic Solvents"), (RCF, "Reinforced Carbon Fiber")]:
            got = _counts(tid)
            check(len(got) == 1, f"{nm}: one number across all eight characters (got {got})")
        check(level_product_runs(CTX) == 0, "and a second pass over it writes nothing")

        # Reported: "there's no reason why it would make 35 oxy when it could make 120 instead" and
        # "you can consolidate the 8 slots we use for oxy to fewer". Pooled, the eight 35-run jobs
        # become the two the account's 207 runs actually need — which is what the stage solve
        # arrives at on its own, with no length for the player to set (removed 2026-08-10).
        oos_rows = [r for r in _cf_rows(con) if r["type_id"] == OOS]
        check(len(_counts(OOS)) == 1 and len(oos_rows) <= 3,
              f"...and Oxy-Organic Solvents consolidates to a couple of jobs "
              f"({len(oos_rows)} × {_counts(OOS)})")
        check(len(oos_rows) * _counts(OOS)[0] >= 207,
              "...still making every run of it the plan needed")

        def _over_capacity():
            """Characters holding more planned jobs than they have reactors — counting EVERY row,
            not just the busiest stage. Reported as "12 slots assigned to characters that only
            have 10": a row is a line in the plan whether or not it can be installed yet."""
            from app.reactions.jobs import _character_capacities
            slots = {c["character_id"]: c["slots"] for c in _character_capacities(CTX)}
            held = {}
            for r in _cf_rows(con):
                held[r["character_id"]] = held.get(r["character_id"], 0) + 1
            return {cid: (n, slots.get(cid, 0)) for cid, n in held.items() if n > slots.get(cid, 0)}

        check(not _over_capacity(),
              f"no character holds more jobs than it has reactors (over: {_over_capacity()})")

        for i, _ in enumerate(REPORTED):
            con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (-9500 - i,))
            con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (-9500 - i,))
        con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
        con.commit()

        print("a row committed to a CUSTOMER ORDER is never re-shaped:")
        # Its run count is the batch the order was quoted on, and cancelling hands exactly those
        # runs back (give_back_order_runs) — moving it would make the order's own arithmetic wrong.
        _reset(con)
        _characters(con)
        cid = CHARS[0][0]
        _plan(con, cid, 5000.0, [(CF, "Carbon Fiber", 90, 2, 0), (RCF, "RCF", 20, 1, 1)])
        con.execute("INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                    "input_cost, reward, created_at, tier_order, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, CF, "Carbon Fiber", 137, 0.0, 0.0, 6000.0, 0, 4242))
        con.commit()
        level_product_runs(CTX)
        ordered_rows = [dict(r) for r in con.execute(
            "SELECT runs FROM pp_reaction_assignments WHERE order_id=4242")]
        check([r["runs"] for r in ordered_rows] == [137],
              f"the order's 137 runs are exactly as they were (got {[r['runs'] for r in ordered_rows]})")
        _reset(con)

        print("the dashboard reports the plan it just levelled, not the one before:")
        # The repair passes used to run at the END of GET /api/reactions/jobs, after the rows had
        # already been read into the payload — so the load that triggered the levelling returned
        # the OLD numbers and only the next one showed the new ones. Assign, look, and it reads as
        # a pass that does nothing.
        import inspect
        from app.reactions import jobs as _J
        src = inspect.getsource(_J.get_industry_jobs)
        check(src.index("level_product_runs(context_id)") < src.index("FROM pp_reaction_assignments"),
              "the plan is levelled BEFORE the rows behind the response are read")
        check(src.index("restage_plan_rows(context_id)") < src.index("FROM pp_reaction_assignments"),
              "...and so is the stage repair it runs beside")

        print("two chains on ONE character each keep their own job:")
        _reset(con)
        _characters(con)
        cid = CHARS[0][0]
        _plan(con, cid, 2000.0, [(CF, "Carbon Fiber", 100, 2, 0), (RCF, "RCF", 20, 1, 1)])
        _plan(con, cid, 3000.0, [(CF, "Carbon Fiber", 60, 2, 0), (RCF, "RCF", 12, 1, 1)])
        level_product_runs(CTX)
        rows = [r for r in _cf_rows(con) if r["type_id"] == CF]
        chains = [dict(r) for r in con.execute(
            "SELECT created_at, COUNT(*) AS n FROM pp_reaction_assignments WHERE character_id=? "
            "AND type_id=? GROUP BY created_at", (cid, CF))]
        check(len({r["runs"] for r in rows}) == 1,
              f"both chains carry the same number (got {sorted({r['runs'] for r in rows})})")
        check(len(chains) == 2 and all(c["n"] >= 1 for c in chains),
              "and neither chain lost its last row — the dashboard still knows it is waiting")
        _reset(con)
    finally:
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
