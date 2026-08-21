#!/usr/bin/env python3
"""Clear all gives a customer order its runs back, instead of stranding it.

The two "clear" paths disagreed. `_clear_assignment_group` (a per-product re-assign) deliberately
SKIPS order-linked rows — an order was committed against real capacity and a suggestion re-assign
must not silently eat it. `unassign_all_reactions` ("Clear all") deleted them and left
`pp_reaction_orders.assigned_runs` alone, which is the one combination that strands an order: it
claims its full run count, holds no rows, schedules nothing, and cannot be re-assigned because
`remaining` is already zero. Orders #36-#39 on a real account sat in exactly that shape.

The invariants:

  * Clear all deletes order rows AND hands the runs back to the order;
  * how much comes back is the TOP row of each chain — what `assigned_runs` was incremented by —
    not the sum of every tier row, which would over-credit a chain;
  * the counter never goes below zero;
  * a speculative (non-order) row still just disappears, exactly as before;
  * an order already stranded — counter set, no rows — is repaired when the player next tries to
    assign it, and an order that legitimately has rows is NOT touched by that repair.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_clear_all_orders.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_clear_all_orders.py
"""
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                      # noqa: E402
from app.reactions.jobs import (ensure_reaction_assignments_table,     # noqa: E402
                                ensure_reaction_orders_table, unassign_all_reactions)
from app.reactions.orders import _heal_stranded_counter                # noqa: E402

CTX = -98811
CHAR = -9371
TOP, MID = 9101, 9102

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_reaction_orders WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _character(con):
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id) "
                "VALUES (?,?,?)", (CHAR, "ClearTester", CTX))
    con.commit()


def _order(con, order_id, top_runs, assigned):
    con.execute(
        "INSERT INTO pp_reaction_orders (id, context_id, type_id, name, target_qty, "
        "top_level_runs, assigned_runs, client_name, notes, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
        (order_id, CTX, TOP, "Top", 100.0, top_runs, assigned, "Client", "", time.time()))
    con.commit()


def _rows(con, specs, order_id=None, at=None):
    """specs = [(type_id, tier_order, runs)] written as ONE assign (one timestamp)."""
    at = at or time.time()
    nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_reaction_assignments")
              .fetchone()["n"])
    for i, (tid, tier, runs) in enumerate(specs):
        con.execute(
            "INSERT INTO pp_reaction_assignments (id, character_id, type_id, name, runs, "
            "input_cost, reward, created_at, tier_order, order_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (nxt + i, CHAR, tid, f"P{tid}", runs, 0.0, 0.0, at, tier, order_id))
    con.commit()


def _assigned(con, order_id):
    return con.execute("SELECT assigned_runs FROM pp_reaction_orders WHERE id=?",
                       (order_id,)).fetchone()["assigned_runs"]


def main():
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        _reset(con)
        _character(con)

        print("Clear all hands an order's runs back:")
        _order(con, -5001, 200, 200)
        _rows(con, [(MID, 0, 400), (TOP, 1, 200)], order_id=-5001)
        res = unassign_all_reactions(context_id=CTX)
        check(res["cleared"] == 2, f"both rows are gone (got {res['cleared']})")
        check(res["orders_reset"] == [-5001], f"and the order is reported (got {res['orders_reset']})")
        check(_assigned(con, -5001) == 0,
              f"its counter is back to 0, so it can be re-assigned (got {_assigned(con, -5001)})")

        print("the credit is the TOP row, not every tier:")
        _reset(con)
        _character(con)
        _order(con, -5002, 200, 200)
        # A 3-tier chain: crediting every row would give back 200+400+400 and underflow to 0 by
        # luck rather than by arithmetic — this order is only PARTLY assigned, so the difference
        # is visible.
        _order(con, -5003, 500, 200)
        _rows(con, [(MID, 0, 400), (9103, 1, 400), (TOP, 2, 200)], order_id=-5003)
        unassign_all_reactions(context_id=CTX)
        check(_assigned(con, -5003) == 0,
              f"200 committed, 200 given back (got {_assigned(con, -5003)})")
        check(_assigned(con, -5002) == 200,
              "an order with no rows in this clear is untouched")

        print("two separate assigns against one order both come back:")
        _reset(con)
        _character(con)
        _order(con, -5004, 500, 300)
        _rows(con, [(MID, 0, 200), (TOP, 1, 100)], order_id=-5004, at=1000.0)
        _rows(con, [(MID, 0, 400), (TOP, 1, 200)], order_id=-5004, at=2000.0)
        unassign_all_reactions(context_id=CTX)
        check(_assigned(con, -5004) == 0, f"100 + 200 = the 300 committed (got {_assigned(con, -5004)})")

        print("the counter never goes below zero:")
        _reset(con)
        _character(con)
        _order(con, -5005, 500, 50)
        _rows(con, [(TOP, 0, 200)], order_id=-5005)      # more rows than the counter knows about
        unassign_all_reactions(context_id=CTX)
        check(_assigned(con, -5005) == 0, f"clamped at 0, never negative (got {_assigned(con, -5005)})")

        print("a speculative row still just disappears:")
        _reset(con)
        _character(con)
        _rows(con, [(MID, 0, 400), (TOP, 1, 200)])
        res = unassign_all_reactions(context_id=CTX)
        check(res["cleared"] == 2 and res["orders_reset"] == [],
              "nothing to give back when no order was involved")

        print("clearing ONE order frees its slots and hands back only ITS runs:")
        from app.reactions.orders import clear_reaction_order_assignments
        _reset(con)
        _character(con)
        _order(con, -5010, 500, 300)
        _order(con, -5011, 400, 200)
        _rows(con, [(MID, 0, 600), (TOP, 1, 300)], order_id=-5010, at=1000.0)
        _rows(con, [(MID, 0, 400), (TOP, 1, 200)], order_id=-5011, at=2000.0)
        _rows(con, [(TOP, 0, 50)])                       # a speculative row, nothing to do with either
        res = clear_reaction_order_assignments(-5010, context_id=CTX)
        check(res["cleared"] == 2, f"only that order's rows are freed (got {res['cleared']})")
        check(res["runs_returned"] == 300, f"and only its runs come back (got {res['runs_returned']})")
        check(_assigned(con, -5010) == 0, "the cleared order is fully unassigned again")
        check(_assigned(con, -5011) == 200, "the other order is untouched")
        left = con.execute("SELECT COUNT(*) AS n FROM pp_reaction_assignments WHERE character_id=?",
                           (CHAR,)).fetchone()["n"]
        check(left == 3, f"its sibling's rows and the speculative row survive (got {left})")
        check(res["order"]["status"] == "open" and res["order"]["top_level_runs"] == 500,
              "the ORDER itself is kept — this is a re-plan, not a cancellation")

        print("...and it is how a stranded order gets moving again:")
        _order(con, -5012, 200, 200)                     # counter set, no rows
        res = clear_reaction_order_assignments(-5012, context_id=CTX)
        check(res["cleared"] == 0 and _assigned(con, -5012) == 0,
              "no rows to free, but the stale counter is reset so it can be assigned")

        print("...and another account cannot touch it:")
        try:
            clear_reaction_order_assignments(-5011, context_id=CTX - 1)
            check(False, "clearing someone else's order must 404")
        except Exception as exc:
            check(getattr(exc, "status_code", None) == 404,
                  f"clearing someone else's order 404s (got {exc})")

        print("an already-stranded order is repaired when the player next assigns it:")
        _reset(con)
        _character(con)
        _order(con, -5006, 200, 200)                      # counter set, no rows: the stuck shape
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (-5006,)).fetchone())
        healed = _heal_stranded_counter(order, CTX)
        check(healed["assigned_runs"] == 0, "the stale counter is reset so the order can move again")

        print("...but an order that really does hold rows is left alone:")
        _order(con, -5007, 200, 200)
        _rows(con, [(TOP, 0, 200)], order_id=-5007)
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (-5007,)).fetchone())
        check(_heal_stranded_counter(order, CTX)["assigned_runs"] == 200,
              "a fully-assigned order keeps its counter and its honest refusal")
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
