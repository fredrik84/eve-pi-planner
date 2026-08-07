#!/usr/bin/env python3
"""Intermediates you already hold are not reacted again.

Reactions planned every chain from raw moon goo: `_explode_chain_tiers` walked the recipe and never
asked whether the account was already holding the intermediate. Hold 200 Carbon Fiber and the tool
still told you to react 200 Carbon Fiber first, and to wait a cycle for it, before the stage that
consumes it — pointless waiting, and goo bought for units already in the hangar.

The invariants:

  * a tier the holding covers OUTRIGHT is dropped, and so is everything below it — you don't react
    the inputs of something already in the hangar;
  * a PARTIAL holding shortens the tier instead of dropping it, and the runs left are the runs the
    remainder really needs;
  * a unit is spent ONCE inside one plan: two branches needing the same intermediate cannot both
    claim it;
  * the shopping list spends the same way, at every level;
  * what stock covered is always REPORTED — a stage that silently disappears is indistinguishable
    from a bug;
  * with the flag off, nothing anywhere is consulted and every walk is the pure recipe.

In-process; run inside the container against a NON-PROD database. The graph walks are pure
functions over a hand-built `reached` map, so the chain cases here are exact rather than
market-dependent.

    docker compose cp test_reaction_stock.py web:/srv/app/ && \
      docker compose exec web python3 test_reaction_stock.py
"""
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                      # noqa: E402
from app.features import ensure_features_table                         # noqa: E402
from app.industry.assets import ensure_asset_tables                    # noqa: E402
from app.reactions.graph import (_explode_shopping_list,               # noqa: E402
                                 _ordered_chain_tiers, reaction_stock_pool)

CTX = -98801
FLAG = "reactions_use_stock"

# A hand-built two-tier chain: TOP is made from MID (a reaction) and GOO (a leaf); MID is made from
# GOO2. Exact numbers, no market data, no ME: REACTION_ME_REDUCTION is applied by the walker, so the
# quantities below are what it consumes before that reduction.
TOP, MID, DEEP, GOO, GOO2 = 9001, 9002, 9003, 9004, 9005

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reached():
    """{type_id: node} in the shape `_resolve_reachable` produces — leaves have via=None."""
    leaf = {"via": None, "unit_cost": 1.0, "reaction_count": 0}
    return {
        GOO: dict(leaf), GOO2: dict(leaf),
        DEEP: {"via": {"reaction_id": 3, "cycle_time": 3600, "output_qty": 10,
                       "inputs": [{"type_id": GOO2, "quantity": 100}]},
               "unit_cost": 2.0, "reaction_count": 1},
        MID: {"via": {"reaction_id": 2, "cycle_time": 3600, "output_qty": 10,
                      "inputs": [{"type_id": DEEP, "quantity": 20}, {"type_id": GOO, "quantity": 50}]},
              "unit_cost": 3.0, "reaction_count": 2},
        TOP: {"via": {"reaction_id": 1, "cycle_time": 3600, "output_qty": 5,
                      "inputs": [{"type_id": MID, "quantity": 40}, {"type_id": GOO, "quantity": 10}]},
              "unit_cost": 5.0, "reaction_count": 3},
    }


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


def _stock(con, type_id, qty):
    con.execute("INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, "
                "item_count, scope) VALUES (?,?,?,?,?,?,?,?)",
                (CTX, f"cont:{type_id}", "container", "box", "", 1, 1, ""))
    con.execute("INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                (CTX, f"cont:{type_id}", type_id, float(qty)))
    con.commit()


def _reset(con):
    con.execute("DELETE FROM pp_asset_stock WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_sources WHERE context_id=?", (CTX,))
    con.commit()


def main():
    ensure_asset_tables()
    was = _flag("public")
    reached = _reached()
    top_inputs = reached[TOP]["via"]["inputs"]
    con = get_connection()
    try:
        _reset(con)

        print("with no stock, the whole chain is planned (the behaviour that shipped):")
        base = dict(_ordered_chain_tiers(top_inputs, 10, reached))
        check(set(base) == {MID, DEEP},
              f"both intermediate tiers are there (got {sorted(base)})")
        mid_runs, deep_runs = base[MID]["runs"], base[DEEP]["runs"]
        check(mid_runs > 0 and deep_runs > 0, f"MID x{mid_runs}, DEEP x{deep_runs}")

        print("a holding that covers a tier OUTRIGHT drops it and everything below it:")
        # MID needs 40 x 10 runs, less the ME reduction — 10k units is comfortably everything.
        covered = {}
        tiers = dict(_ordered_chain_tiers(top_inputs, 10, reached, {MID: 10000.0}, covered))
        check(MID not in tiers, "the covered tier is not planned")
        check(DEEP not in tiers,
              "and neither is the tier that existed only to feed it — its inputs aren't work")
        check(covered.get(MID, {}).get("runs_saved") == mid_runs,
              f"the saving is reported in runs (got {covered.get(MID, {}).get('runs_saved')}, want {mid_runs})")
        check(covered[MID]["units"] > 0, "with the units it consumed")

        print("a PARTIAL holding shortens the tier instead of dropping it:")
        covered = {}
        half_units = base[MID]["runs"] * reached[MID]["via"]["output_qty"] / 2.0
        tiers = dict(_ordered_chain_tiers(top_inputs, 10, reached, {MID: half_units}, covered))
        check(MID in tiers and tiers[MID]["runs"] < mid_runs,
              f"MID still runs, but for less (got {tiers.get(MID, {}).get('runs')} of {mid_runs})")
        check(DEEP in tiers and tiers[DEEP]["runs"] < deep_runs,
              "and the tier feeding it shrinks with it, rather than making what's no longer needed")
        check(0 < covered[MID]["runs_saved"] < mid_runs, "the saving reported is the partial one")

        print("a unit is spent ONCE within a plan:")
        # TOP needs MID; DEEP is below MID. A pool holding just enough for one of them must not
        # satisfy both — the pool is consumed as the walk goes.
        pool = {MID: half_units, DEEP: 10.0}
        tiers = dict(_ordered_chain_tiers(top_inputs, 10, reached, pool, {}))
        check(pool[MID] == 0, "the MID units are gone from the pool once spent")
        check(sum(pool.values()) < half_units + 10.0, "and so are the DEEP ones it reached")

        print("the shopping list spends the same holding, at every level:")
        full, held = {}, {}
        _explode_shopping_list(TOP, 100, reached, full)
        _explode_shopping_list(TOP, 100, reached, held, {MID: 10000.0})
        check(full.get(GOO2, 0) > 0, "the pure walk buys goo for the deep tier")
        check(held.get(GOO2, 0) == 0,
              f"holding the intermediate buys none of it (got {held.get(GOO2)})")
        check(held.get(GOO, 0) < full.get(GOO, 0),
              "and less of the goo the covered tier would have consumed")

        print("the pool itself is the ENABLED holding, and only with the flag on:")
        _stock(con, MID, 1234)
        pool = reaction_stock_pool(CTX)
        check(pool.get(MID) == 1234.0, f"an enabled container is in the pool (got {pool.get(MID)})")
        con.execute("UPDATE pp_asset_sources SET enabled=0 WHERE context_id=?", (CTX,))
        con.commit()
        check(reaction_stock_pool(CTX) == {},
              "a container you have not ticked is not stock you said you'd spend")
        con.execute("UPDATE pp_asset_sources SET enabled=1 WHERE context_id=?", (CTX,))
        con.commit()
        _flag("hidden")
        check(reaction_stock_pool(CTX) == {}, "and with the flag off there is no pool at all")
        _flag("public")

        print("no pool means the pure recipe, byte for byte:")
        check(dict(_ordered_chain_tiers(top_inputs, 10, reached, None, {})) == base,
              "the walk with stock=None is the walk that shipped")
        check(dict(_ordered_chain_tiers(top_inputs, 10, reached, {}, {})) == base,
              "...and so is an empty pool")
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
