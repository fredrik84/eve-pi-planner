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

    print("the list buys for the runs the plan will ACTUALLY run, not the bare requirement:")
    # Intermediate run counts get rounded up so they are typeable (tidy_runs); the materials for
    # those extra runs have to be bought or the player installs a job they cannot fill.
    from test_reaction_stock import _reached, TOP as G_TOP, MID as G_MID, GOO2 as G_GOO2
    from app.reactions.graph import _explode_shopping_list
    r = _reached()
    exact, rounded = {}, {}
    _explode_shopping_list(G_TOP, 100, r, exact)
    _explode_shopping_list(G_TOP, 100, r, rounded, None, {G_MID: 10_000.0})
    check(rounded.get(G_GOO2, 0) > exact.get(G_GOO2, 0),
          "a planned intermediate above the requirement raises what its inputs cost")
    lower = {}
    _explode_shopping_list(G_TOP, 100, r, lower, None, {G_MID: 1.0})
    check(lower.get(G_GOO2, 0) == exact.get(G_GOO2, 0),
          "and a plan holding LESS than the requirement never lowers the list — that is a short "
          "plan, not a cheaper one")

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
