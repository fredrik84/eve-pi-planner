#!/usr/bin/env python3
"""The shopping list buys a chain's materials ONCE.

`reactions_shopping_list` exploded every pending plan row down to raw goo. A chain assign stores a
row per intermediate AND one for the product, and the product's own walk already contains every
intermediate's materials — so a two-tier chain's goo was counted twice (7,726 units became 15,452 on
a synthetic chain) and anyone with a multi-tier plan was told to buy about double.

The invariants:

  * a chain contributes its materials once — only the highest tier of an assign is a root;
  * an assign is identified by (character_id, created_at), which is what all three insert paths
    actually write, so a SEPARATE assign of the same product is its own root and still counts;
  * a product deliberately assigned on its own is real demand and is never skipped, even when
    something else in the plan happens to consume it;
  * a group whose top row was cancelled falls back to what is left as the new root;
  * rows on different characters are different chains even when written in the same instant.

In-process, pure: `_shopping_roots` is a function over rows, so this needs no database and no
market data.

    docker compose cp test_shopping_roots.py web:/srv/app/ && \
      docker compose exec web python3 test_shopping_roots.py
"""
import math
import sys

sys.path.insert(0, ".")
from app.reactions.graph import _shopping_roots                        # noqa: E402

TOP, MID, DEEP, OTHER = 9001, 9002, 9003, 9009

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def row(char, tid, tier, at, runs=10):
    return {"character_id": char, "type_id": tid, "tier_order": tier, "created_at": at,
            "runs": runs, "order_id": None}


def ids(rows):
    return sorted((r["type_id"], r["tier_order"]) for r in rows)


def main():
    print("one chain assign contributes only its top row:")
    chain = [row(1, DEEP, 0, 100.0), row(1, MID, 1, 100.0), row(1, TOP, 2, 100.0)]
    check(ids(_shopping_roots(chain)) == [(TOP, 2)],
          f"the product is the root, its intermediates are not (got {ids(_shopping_roots(chain))})")

    print("...however many jobs the top tier is split across:")
    split = chain + [row(1, TOP, 2, 100.0), row(1, TOP, 2, 100.0)]
    check(len(_shopping_roots(split)) == 3 and {r["type_id"] for r in _shopping_roots(split)} == {TOP},
          "every row at the top tier is a root — each carries its own share of the runs")

    print("a SEPARATE assign is its own chain:")
    two = chain + [row(1, DEEP, 0, 200.0), row(1, MID, 1, 200.0)]
    roots = _shopping_roots(two)
    check(ids(roots) == sorted([(MID, 1), (TOP, 2)]),
          f"the second assign's own top counts too (got {ids(roots)})")

    print("a product assigned on its own is real demand, never skipped:")
    standalone = [row(1, TOP, 2, 100.0), row(1, MID, 0, 300.0)]
    check(ids(_shopping_roots(standalone)) == sorted([(MID, 0), (TOP, 2)]),
          "even though TOP's chain consumes MID — that MID is a different, deliberate job")

    print("a chain whose top row was cancelled falls back to what is left:")
    beheaded = [row(1, DEEP, 0, 100.0), row(1, MID, 1, 100.0)]
    check(ids(_shopping_roots(beheaded)) == [(MID, 1)],
          "the highest remaining tier becomes the root")

    print("the same instant on two characters is two chains:")
    two_chars = [row(1, MID, 0, 100.0), row(1, TOP, 1, 100.0),
                 row(2, MID, 0, 100.0), row(2, TOP, 1, 100.0)]
    roots = _shopping_roots(two_chars)
    check(len(roots) == 2 and {r["character_id"] for r in roots} == {1, 2},
          f"each character's chain keeps its own root (got {len(roots)})")

    print("the list is computed PER ROW, and matches what the game asks for:")
    # The reported plan, verbatim (2026-08-10): 19 jobs x 120 runs of Carbon Fiber and of
    # Thermosetting Polymer, 4 jobs x 120 runs of Oxy-Organic Solvents, under Reinforced Carbon
    # Fiber. The six totals below were computed by hand off the in-game requirements and are the
    # oracle this test exists to hold us to.
    from app.reactions.graph import _plan_materials, REACTION_ME_REDUCTION as RME
    RCF_T, CF_T, OOS_T, TP_T = 57457, 57453, 57454, 57455
    HYDRO, ATM, EVAP, SILI, O2_FB, H2_FB = 16633, 16634, 16635, 16636, 4312, 4246
    rx = {
        CF_T:  {"via": {"output_qty": 200.0, "inputs": [
            {"type_id": H2_FB, "quantity": 5}, {"type_id": HYDRO, "quantity": 100},
            {"type_id": EVAP, "quantity": 100}]}},
        TP_T:  {"via": {"output_qty": 200.0, "inputs": [
            {"type_id": O2_FB, "quantity": 5}, {"type_id": ATM, "quantity": 100},
            {"type_id": SILI, "quantity": 100}]}},
        OOS_T: {"via": {"output_qty": 10.0, "inputs": [
            {"type_id": O2_FB, "quantity": 5}, {"type_id": HYDRO, "quantity": 2000},
            {"type_id": ATM, "quantity": 2000}]}},
        RCF_T: {"via": {"output_qty": 200.0, "inputs": [
            {"type_id": CF_T, "quantity": 200}, {"type_id": OOS_T, "quantity": 1},
            {"type_id": TP_T, "quantity": 200}]}},
    }
    for leaf in (HYDRO, ATM, EVAP, SILI, O2_FB, H2_FB):
        rx[leaf] = {"via": None}

    plan = ([{"type_id": CF_T, "runs": 120} for _ in range(19)]
            + [{"type_id": TP_T, "runs": 120} for _ in range(19)]
            + [{"type_id": OOS_T, "runs": 120} for _ in range(4)]
            + [{"type_id": RCF_T, "runs": 233} for _ in range(10)])
    EXPECT = {HYDRO: 1_161_864, ATM: 1_161_864, EVAP: 222_984, SILI: 222_984,
              O2_FB: 13_501, H2_FB: 11_153}
    got = {t: math.ceil(q) for t, q in _plan_materials(plan, rx).items()}
    for tid, want in EXPECT.items():
        check(got.get(tid) == want,
              f"{tid}: {got.get(tid, 0):,} (expected {want:,})")
    check(set(got) == set(EXPECT), f"and nothing else on the list (got {sorted(set(got)-set(EXPECT))})")

    print("...rounded per JOB, the way the game does, never per batch:")
    # 5 fuel blocks x 120 runs x 0.978 = 586.8 -> 587 a job, 19 jobs = 11,153. Rounding the batch
    # instead gives 11,150 and leaves you three blocks short of installing the 19th job.
    per_job = math.ceil(5 * 120 * (1 - RME)) * 19
    per_batch = math.ceil(5 * 120 * 19 * (1 - RME))
    check(got[H2_FB] == per_job and per_job > per_batch,
          f"{per_job:,} per job, not {per_batch:,} per batch")

    print("...and a product the PLAN makes is never also bought, or deducted from stock:")
    # Oxy-Organic Solvents is made by its own rows, so holding 400 of it must not shrink the goo
    # the plan's 4 OOS jobs will consume — those jobs are still going to be installed. Deducting it
    # a second time here is what put the list 78,240 Hydrocarbons short of the plan.
    held = _plan_materials(plan, rx, {OOS_T: 400.0, CF_T: 100_000.0})
    check(math.ceil(held[HYDRO]) == EXPECT[HYDRO],
          f"holding 400 Oxy-Organic Solvents changes nothing (got {math.ceil(held[HYDRO]):,})")
    check(OOS_T not in held and CF_T not in held,
          "and an in-house product never appears as something to buy")

    print("edge cases don't throw:")
    check(_shopping_roots([]) == [], "no rows, no roots")
    check(len(_shopping_roots([{"character_id": 1, "type_id": TOP, "runs": 1,
                                "tier_order": None, "created_at": None}])) == 1,
          "a row with null tier/timestamp is still a root, not an exception")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
