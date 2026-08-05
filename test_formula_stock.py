#!/usr/bin/env python3
"""Reaction formulas found in ENABLED stock raise the concurrency cap — and change nothing else.

`GET /characters/{id}/blueprints/` returns PERSONAL blueprints only, so a builder who keeps their
formulas in a corp hangar container has an empty `pp_char_blueprints` for them and the print cap
never fires: the plan schedules N parallel reactions off the one formula they own. Those formulas
are visible in `pp_asset_stock` (corp scan, or the pasted hangar every non-Director uses), and this
pins what counting them is allowed to mean.

The invariants, in both directions:

  * a formula in an enabled stock source raises the cap for its OUTPUT product;
  * a formula in a source the user has not ticked raises nothing — stock is opt-in per source, and
    stock you cannot draw from is the error this codebase treats as the dangerous one;
  * the same formula seen by BOTH scans is one formula: a personal container is visible to the
    asset scan and to the blueprint endpoint, so those two are reconciled with `max`, never added,
    while a corp/pasted box adds because neither personal scan can see it;
  * nothing about ME, TE or run coverage moves — an asset row states none of them;
  * an account we know nothing about is not capped at all (`prints_known` and the no-evidence rule);
  * manufacturing blueprints in stock are NOT counted (their ME/TE and run coverage would have to be
    invented), so a manufacturing type's cap is byte-for-byte what it was.

In-process; run inside the container against a NON-PROD database. Seeds rows under a fabricated
context id and removes them in a finally.

    docker compose cp test_formula_stock.py web:/srv/app/ && \
      docker compose exec web python3 test_formula_stock.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                   # noqa: E402
from app.industry.assets import ensure_asset_tables                 # noqa: E402
from app.industry.blueprints import (ensure_char_blueprints_table,  # noqa: E402
                                     owned_blueprints, stock_formula_prints)
from app.industry.graph import BuildParams                          # noqa: E402
from app.industry.schedule import _print_limits                     # noqa: E402

CTX = -98773
CHAR = -9340

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    con.execute("DELETE FROM pp_asset_stock WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_sources WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_char_blueprints WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _source(con, key, scope, enabled, tid, qty):
    con.execute(
        "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, item_count, "
        "scope) VALUES (?,?,?,?,?,?,?,?)",
        (CTX, key, "container", key, "", 1 if enabled else 0, 1, scope))
    con.execute("INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                (CTX, key, int(tid), float(qty)))
    con.commit()


def _blueprint_cache(con, rows):
    """One cached blueprint list for one character — a COMPLETE picture of this account, so
    `prints_known()` is true and the cap is allowed to fire at all."""
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
                "VALUES (?,?,?,?)", (CHAR, "StockTester", CTX, "esi-characters.read_blueprints.v1"))
    con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?)", (CHAR, json.dumps(rows), 1.0))
    con.commit()


def _params(stock_prints=None, owned=None):
    """Params whose blueprint picture is COMPLETE — the incomplete case is asserted separately."""
    return BuildParams(owned=owned or {}, stock_prints=stock_prints or {},
                       blueprint_coverage={"characters": 1, "cached": 1, "complete": True})


def main():
    ensure_asset_tables()
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        row = con.execute("SELECT reaction_id, output_type_id FROM reactions "
                          "ORDER BY reaction_id LIMIT 1").fetchone()
        if not row:
            print("no `reactions` rows in this SDE — cannot run")
            return 2
        formula, product = int(row["reaction_id"]), int(row["output_type_id"])
        mfg = con.execute("SELECT blueprint_type_id, product_type_id FROM blueprints "
                          "ORDER BY blueprint_type_id LIMIT 1").fetchone()
        print(f"formula {formula} -> product {product}")
        _reset(con)

        print("a formula in an ENABLED stock source raises the cap for its product:")
        _source(con, "corp:1:c500", "corp:1", True, formula, 3)
        counts = stock_formula_prints(CTX, {})
        check(counts.get(product) == 3, f"3 formulas in a corp container are counted (got {counts.get(product)})")
        p = _params(stock_prints=counts)
        check(_print_limits(p, product, "reaction", 40) == (3, False),
              f"...and cap 40 runs of it to 3 concurrent jobs (got {_print_limits(p, product, 'reaction', 40)})")

        print("a DISABLED source is not spendable, so it raises nothing:")
        con.execute("UPDATE pp_asset_sources SET enabled=0 WHERE context_id=? AND key=?",
                    (CTX, "corp:1:c500"))
        con.commit()
        check(stock_formula_prints(CTX, {}) == {}, "an unticked container contributes no prints")
        check(_print_limits(_params(), product, "reaction", 40) == (None, False),
              "and with no other evidence the type is left UNCAPPED, not capped at 1")

        print("the same formula in both scans is ONE formula:")
        _reset(con)
        _blueprint_cache(con, [{"type_id": formula, "me": 0, "te": 0, "quantity": 2, "runs": -1}])
        _source(con, "cont:600", "char:-9340", True, formula, 2)
        owned = owned_blueprints(CTX)
        check(owned.get(product, {}).get("copy_count") == 2,
              f"the blueprint cache alone knows 2 (got {owned.get(product, {}).get('copy_count')})")
        check(stock_formula_prints(CTX, owned).get(product) is None,
              "a personal container the blueprint endpoint already reported adds NOTHING")
        p = _params(stock_prints=stock_formula_prints(CTX, owned), owned=owned)
        check(_print_limits(p, product, "reaction", 40) == (2, False),
              f"so the cap stays 2, not 4 (got {_print_limits(p, product, 'reaction', 40)})")

        print("...but a corp box the personal scans cannot see does add:")
        _source(con, "corp:2:c700", "corp:2", True, formula, 3)
        extra = stock_formula_prints(CTX, owned)
        check(extra.get(product) == 3, f"the corp container's 3 are extra (got {extra.get(product)})")
        p = _params(stock_prints=extra, owned=owned)
        check(_print_limits(p, product, "reaction", 40) == (5, False),
              f"2 personal + 3 corp = 5 concurrent (got {_print_limits(p, product, 'reaction', 40)})")

        print("a paste and a corp scan are most likely the same hangar, so they never double:")
        _source(con, "paste:900", "", True, formula, 3)
        extra = stock_formula_prints(CTX, owned)
        check(extra.get(product) == 3,
              f"3 pasted beside the same 3 scanned stays 3, not 6 (got {extra.get(product)})")

        print("nothing about ME, TE or run coverage moves:")
        p = _params(stock_prints={product: 5})
        check(p.me_te_for(product, "reaction") == (0.0, 0.0),
              "a reaction still has no blueprint ME/TE")
        check(p.copies_for(product, "reaction") == [],
              "stock never becomes a researched copy the plan can build off")
        check(p.owned == {} and p.me_by_product == {},
              "and it never leaks into `owned` or the per-product ME map")
        check(product not in p.me_source,
              "no ME provenance is claimed for a print we only saw as an asset row")

        print("an account we know nothing about is not capped at all:")
        _reset(con)
        check(_print_limits(_params(), product, "reaction", 40) == (None, False),
              "no blueprints, no stock → no cap (absent evidence never serialises work)")
        _source(con, "corp:3:c800", "corp:3", True, formula, 2)
        incomplete = BuildParams(stock_prints=stock_formula_prints(CTX, {}),
                                 blueprint_coverage={"characters": 14, "cached": 2,
                                                     "complete": False})
        check(not incomplete.prints_known(), "2 of 14 characters cached is not a complete picture")
        check(_print_limits(incomplete, product, "reaction", 40) == (None, False),
              "so it is still uncapped — stock is a floor too, and a floor must not serialise")

        print("manufacturing blueprints in stock are deliberately NOT counted:")
        _reset(con)
        if mfg:
            bp_tid, bp_prod = int(mfg["blueprint_type_id"]), int(mfg["product_type_id"])
            _source(con, "corp:4:c900", "corp:4", True, bp_tid, 4)
            counts = stock_formula_prints(CTX, {})
            check(bp_prod not in counts,
                  "a BPO sitting in a hangar states no ME, TE or runs, so it credits nothing")
            check(counts == {}, f"nothing at all is counted from it (got {counts})")
            check(_print_limits(_params(), bp_prod, "manufacturing", 40) == (None, False),
                  "and the manufacturing cap is exactly what it was before stock was read")
        else:
            check(False, "no `blueprints` rows in this SDE — manufacturing case not exercised")
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
