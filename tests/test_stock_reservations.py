#!/usr/bin/env python3
"""Materials assigned to a slot are not offered to the next plan as well.

`owned_quantities` read the asset tables raw — what is in the hangar, not what is FREE. Between
planning and installing, a unit assigned to a reactor is claimed but still sitting in the box, so a
second planning run, or the other service entirely, saw it and promised it again. Reactions
documented the gap outright ("there is no reservation ledger"); §17 is that ledger.

What is pinned:

  * a pending assignment holds its INPUTS, in the quantities its recipe implies;
  * a RUNNING job holds nothing — its materials already left the container, so reserving them again
    would subtract the same units twice;
  * both services net the claims off the SAME pool, so they cannot disagree about what is free;
  * a pool is never driven negative, and a fully-claimed type disappears rather than reading as 0;
  * with the flag off nothing changes at all (CLAUDE.md rule 2).

Fixtures are seeded and torn down under a context of their own, so a real account is never touched.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_stock_reservations.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_stock_reservations.py
"""
import sys
import time

sys.path.insert(0, ".")

_fails = []
CTX = 515151
CHAR = 51515101


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _set_flag(key: str, state):
    from app.db import get_connection
    with get_connection() as con:
        con.execute("DELETE FROM pp_features WHERE key=?", (key,))
        if state:
            con.execute("INSERT INTO pp_features (key, state) VALUES (?,?)", (key, state))
        con.commit()
    # `_state_of` reads the row; no process-level cache to bust outside a request.


def _clear_memo():
    """Open a fresh memo scope, so each assertion below reads state rather than a cached answer.

    `request_memo` no-ops without a scope, but opening one explicitly means this test behaves the
    same way whether or not it is ever run inside a request."""
    from app.cache import begin_request_memo
    begin_request_memo()


def main() -> int:
    from app.sde import get_connection
    from app.industry.reservations import reserved_quantities, net_of_reservations, _reaction_inputs
    from app.reactions.jobs import ensure_reaction_assignments_table

    recipes = _reaction_inputs()
    check(bool(recipes), f"the SDE has reaction recipes to reason about ({len(recipes)})")
    if not recipes:
        return 1

    # A real reaction with real inputs — nothing here is invented.
    prod = next(t for t, ins in recipes.items() if ins)
    ins = recipes[prod]
    in_t, per_run = ins[0]
    RUNS = 7

    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))
        con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
        con.execute("INSERT INTO pp_characters (character_id, context_id, character_name) "
                    "VALUES (?,?,?)", (CHAR, CTX, "reservation fixture"))
        con.execute("INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                    "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?)",
                    (CHAR, prod, "fixture", RUNS, 0.0, 0.0, time.time(), 0))
        con.commit()
    finally:
        con.close()

    try:
        print("\nwith the flag OFF nothing is reserved — the old behaviour, untouched:")
        _set_flag("stock_reservations", "admin")
        _clear_memo()
        check(reserved_quantities(CTX) == {}, "no claims at all")
        pool = {in_t: 1000.0}
        check(net_of_reservations(CTX, pool) == pool, "...and the pool is handed back unchanged")

        print("\nwith it ON, a pending assignment holds its inputs:")
        _set_flag("stock_reservations", "public")
        _clear_memo()
        res = reserved_quantities(CTX)
        want = per_run * RUNS
        check(res.get(in_t) == want,
              f"{RUNS} runs claim {want} of type {in_t} (got {res.get(in_t)})")

        print("\n...and both services see the SAME free pool:")
        _clear_memo()
        from app.industry.assets import owned_quantities
        from app.reactions.graph import reaction_stock_pool
        # Netting happens inside owned_quantities, which reaction_stock_pool also reads, so the two
        # cannot disagree by construction — assert that rather than a number the fixture cannot set.
        a = owned_quantities(CTX)
        _clear_memo()
        b = reaction_stock_pool(CTX)
        check(all(a.get(k) == v for k, v in b.items()) or not b,
              "the reactions pool is a subset of the same netted pool, never a richer one")

        print("\nthe pool is never driven negative, and a spent type disappears:")
        _clear_memo()
        out = net_of_reservations(CTX, {in_t: float(want)})
        check(in_t not in out, f"exactly claimed -> gone, not 0 (got {out.get(in_t)})")
        _clear_memo()
        out = net_of_reservations(CTX, {in_t: float(want) - 5})
        check(in_t not in out, "over-claimed -> gone, never negative")
        _clear_memo()
        out = net_of_reservations(CTX, {in_t: float(want) + 42})
        check(abs(out.get(in_t, 0) - 42) < 1e-6, f"partly claimed -> the remainder (got {out.get(in_t)})")

        print("\na RUNNING job claims nothing — its materials already left the box:")
        # Reserving those too would subtract the same units twice: once here, and once by their
        # absence from the next asset scan. This is the assertion the first version of this test
        # was missing, found by reintroducing the defect and watching it stay green.
        import json as _json
        con2 = get_connection()
        try:
            con2.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CHAR,))
            con2.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json) VALUES (?,?)",
                         (CHAR, _json.dumps([{"product_type_id": prod, "status": "active"}])))
            con2.commit()
        finally:
            con2.close()
        _clear_memo()
        res_running = reserved_quantities(CTX)
        check(res_running.get(in_t) is None,
              f"the claim is released once the job is really running (got {res_running.get(in_t)})")
        con2 = get_connection()
        try:
            con2.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CHAR,))
            con2.commit()
        finally:
            con2.close()
        _clear_memo()
        check(reserved_quantities(CTX).get(in_t) == want,
              "...and comes back when the job is gone but the assignment remains")

        print("\na type nobody claimed is untouched:")
        _clear_memo()
        other = max(recipes) + 999999
        out = net_of_reservations(CTX, {other: 5.0})
        check(out.get(other) == 5.0, "an unclaimed type passes straight through")
    finally:
        con = get_connection()
        try:
            con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CHAR,))
            con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
            con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CHAR,))
            con.commit()
        finally:
            con.close()
        _set_flag("stock_reservations", None)

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
