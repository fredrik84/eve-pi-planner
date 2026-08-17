#!/usr/bin/env python3
"""Blueprints and reaction formulas DECLARED BY HAND (`pp_industry_blueprints`).

`GET /characters/{id}/blueprints/` is PERSONAL-only and there is no corp-hangar blueprint endpoint
without the Director role, so a builder whose prints live in a corp hangar could state their ME/TE
in no way at all: every such build was planned at ME 0 / TE 0 and every material and duration figure
was wrong. Pasting a hangar as STOCK answers the FORMULA case only — an asset row carries no ME, TE
or runs, so it deliberately credits nothing for a manufacturing blueprint. This is the declaration
layer, and this file pins what declaring is allowed to mean.

The invariants:

  * a declaration REPLACES the ESI reading for its product and never adds to it — the two sources
    share no key a user could type (`item_id` is invisible in the client), so an additive rule would
    double-count silently every time a scanned print is also typed in;
  * products the user did NOT declare are untouched by any of this;
  * it does not double-count against the formula evidence layer either: a hand-declared formula, a
    pasted one and one observed in a job are one physical item at least as often as three, and the
    declaration answers alone (`formula_print_floor` precedence a0);
  * ME/TE precedence is per-order override > declared > ESI-read > contract copy > 0/0, and
    `me_source` reports which — "declared" is the user's word and must never be reported as a
    measurement;
  * the BPO-vs-BPC preference moves BOTH the print cap and the cost basis, because an original is
    one print that covers any batch and copies are N prints that get spent;
  * runs blank = a BPO is the encoding `owned_blueprints` already uses internally (`runs = -1`);
  * with the flag off, or with nothing declared, every number is byte-for-byte what it was.

In-process; run inside the container against a NON-PROD database. Seeds rows under a fabricated
context id and removes them in a finally.

    docker compose cp test_manual_blueprints.py web:/srv/app/ && \
      docker compose exec web python3 test_manual_blueprints.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                      # noqa: E402
from app.industry import blueprints as bp                              # noqa: E402
from app.industry.assets import ensure_asset_tables                    # noqa: E402
from app.industry.blueprints import (                                  # noqa: E402
    ensure_char_blueprints_table, ensure_manual_blueprints_table, ensure_formula_job_prints_table,
    manual_blueprints, owned_blueprints, formula_print_floor, _apply_kind_preference,
)
from app.industry.graph import BuildParams                             # noqa: E402
from app.industry.schedule import _print_limits                        # noqa: E402

CTX = -98881
CHAR = -9341

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# The feature is admin/tester-gated in production; these tests are about the MERGE, so the gate is
# forced on for the duration and the flag-off case is asserted separately and explicitly.
_real_enabled = bp._manual_enabled


def _flag_holders():
    """Every module object that BINDS `_manual_enabled`, package included.

    `blueprints` is a package now (TODO 34), and each submodule that uses this name imported it into
    its own globals — so setting the package attribute alone patches nobody, and the gate silently
    falls through to the real (default-off) feature flag. Discovered exactly that way: three checks
    went red on the split. Iterating the loaded submodules keeps this working through the next move
    as well, rather than naming today's two.
    """
    import sys
    mods = [bp]
    for name, mod in list(sys.modules.items()):
        if name.startswith("app.industry.blueprints.") and hasattr(mod, "_manual_enabled"):
            mods.append(mod)
    return mods


def _force_flag(on: bool):
    for m in _flag_holders():
        m._manual_enabled = (lambda ctx: on)


def _reset(con):
    con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_stock WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_sources WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_char_blueprints WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_char_formula_jobs WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _declare(con, row_id, type_id, me=0, te=0, runs=-1, qty=1, prefer=""):
    con.execute(
        "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, quantity, "
        "prefer, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (CTX, row_id, int(type_id), me, te, runs, qty, prefer, 1.0))
    con.commit()


def _character(con):
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
                "VALUES (?,?,?,?)", (CHAR, "DeclareTester", CTX,
                                     "esi-characters.read_blueprints.v1"))
    con.commit()


def _esi_cache(con, rows):
    """One cached blueprint list — a COMPLETE picture, so `prints_known()` is true and the print
    cap is allowed to fire at all."""
    con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?)", (CHAR, json.dumps(rows), 1.0))
    con.commit()


def _source(con, key, scope, tid, qty, enabled=True):
    con.execute(
        "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, item_count, "
        "scope) VALUES (?,?,?,?,?,?,?,?)",
        (CTX, key, "container", key, "", 1 if enabled else 0, 1, scope))
    con.execute("INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                (CTX, key, int(tid), float(qty)))
    con.commit()


def _params(owned=None, stock_prints=None):
    return BuildParams(owned=owned or {}, stock_prints=stock_prints or {},
                       blueprint_coverage={"characters": 1, "cached": 1, "complete": True})


def _sde(con):
    """A real manufacturing blueprint and a real reaction formula out of this container's SDE."""
    mfg = con.execute("SELECT blueprint_type_id, product_type_id FROM blueprints "
                      "WHERE product_type_id IS NOT NULL LIMIT 1").fetchone()
    rx = con.execute("SELECT reaction_id, output_type_id FROM reactions LIMIT 1").fetchone()
    return mfg, rx


def main():
    ensure_char_blueprints_table()
    ensure_manual_blueprints_table()
    ensure_formula_job_prints_table()
    ensure_asset_tables()
    con = get_connection()
    try:
        mfg, rx = _sde(con)
        if not mfg or not rx:
            print("this SDE has no blueprints/reactions — cannot run")
            return 1
        bp_tid, product = int(mfg["blueprint_type_id"]), int(mfg["product_type_id"])
        rx_tid, formula_out = int(rx["reaction_id"]), int(rx["output_type_id"])

        # ── the flag ──────────────────────────────────────────────────────────────────────────
        print("the feature is registered and ships off:")
        from app.features import FEATURE_REGISTRY
        f = [x for x in FEATURE_REGISTRY if x["key"] == bp.MANUAL_FEATURE_KEY]
        check(len(f) == 1, "industry_manual_blueprints is a registered feature")
        check(f and f[0]["default"] is False, "and defaults to off")

        print("with the flag off nothing is read at all:")
        _reset(con)
        _force_flag(False)
        _declare(con, 1, product, me=10, te=20, runs=-1, qty=1)
        check(manual_blueprints(CTX) == {},
              "a declared row is invisible while the feature is off")
        check(owned_blueprints(CTX) == {},
              "and the holding is exactly what ESI said — nothing")
        check(manual_blueprints(CTX, force=True) != {},
              "though the row is really there (force=True reads it) — the gate, not the write, is off")

        print("and with the flag ON but nothing declared, the holding is byte-for-byte the old one:")
        _force_flag(True)
        _reset(con)
        _character(con)
        _esi_cache(con, [{"type_id": bp_tid, "me": 8, "te": 14, "quantity": -2, "runs": 5}])
        esi_only = owned_blueprints(CTX)
        check(set(esi_only[product]) == {"me", "te", "kind", "runs", "copies", "copy_count"},
              "no new keys appear on a holding nobody declared anything for")
        check(esi_only[product]["me"] == 8 and esi_only[product]["runs"] == 5,
              "and the ESI reading is untouched")

        # ── the merge rule ────────────────────────────────────────────────────────────────────
        print("a declaration REPLACES the ESI reading for its product — it never adds:")
        _declare(con, 1, product, me=10, te=20, runs=30, qty=1)
        merged = owned_blueprints(CTX)
        check(len(merged[product]["copies"]) == 1,
              "one declared print beside one scanned print is ONE print, not two")
        check(merged[product]["me"] == 10 and merged[product]["te"] == 20,
              "and the numbers are the declared ones, not the scanned ones")
        check(merged[product]["runs"] == 30, "with the declared run coverage")
        check(merged[product].get("source") == "manual",
              "the entry says the holding was declared, not measured")

        print("...and declaring the SAME print twice over cannot inflate what the plan believes:")
        # The failure this rule exists to stop: a user re-types prints ESI already reported. Under an
        # additive rule the 2 scanned copies below would be credited as 4.
        _reset(con)
        _character(con)
        _esi_cache(con, [{"type_id": bp_tid, "me": 8, "te": 14, "quantity": -2, "runs": 5},
                         {"type_id": bp_tid, "me": 8, "te": 14, "quantity": -2, "runs": 5}])
        _declare(con, 1, product, me=8, te=14, runs=5, qty=2)
        re_typed = owned_blueprints(CTX)
        check(len(re_typed[product]["copies"]) == 2,
              "two prints scanned and the same two typed in is two prints")
        check(re_typed[product]["runs"] == 10, "and ten runs of coverage, not twenty")

        print("a product nobody declared is untouched by any of it:")
        other = con.execute("SELECT blueprint_type_id, product_type_id FROM blueprints "
                            "WHERE product_type_id IS NOT NULL AND product_type_id != ? LIMIT 1",
                            (product,)).fetchone()
        _reset(con)
        _character(con)
        _esi_cache(con, [{"type_id": bp_tid, "me": 8, "te": 14, "quantity": -2, "runs": 5},
                         {"type_id": int(other["blueprint_type_id"]), "me": 3, "te": 6,
                          "quantity": -2, "runs": 7}])
        _declare(con, 1, product, me=10, te=20, runs=-1, qty=1)
        mixed = owned_blueprints(CTX)
        untouched = mixed[int(other["product_type_id"])]
        check(untouched["me"] == 3 and untouched["runs"] == 7 and "source" not in untouched,
              "the other product still reads exactly as ESI reported it")

        # ── runs encoding ─────────────────────────────────────────────────────────────────────
        print("blank runs = a BPO, which is the encoding this module already used:")
        _reset(con)
        _force_flag(True)
        _declare(con, 1, product, me=10, te=20, runs=-1, qty=1)
        as_bpo = owned_blueprints(CTX)[product]
        check(as_bpo["kind"] == "bpo" and as_bpo["runs"] == -1,
              "runs -1 (a blank field) is an original covering any batch")
        _reset(con)
        _declare(con, 1, product, me=10, te=20, runs=12, qty=3)
        as_bpc = owned_blueprints(CTX)[product]
        check(as_bpc["kind"] == "bpc" and as_bpc["runs"] == 36 and len(as_bpc["copies"]) == 3,
              "a run count is a copy, and a quantity expands into that many separate prints")

        # ── ME/TE precedence, every branch ────────────────────────────────────────────────────
        print("ME/TE precedence: override > declared > ESI-read > contract > 0/0:")
        check(_source_for_owned({"source": "manual"}) == "declared",
              "a declared holding reports itself as declared")
        check(_source_for_owned({}) == "owned",
              "an ESI-read holding still reports itself as owned")
        import inspect
        from app.industry.graph import prepare_plan_inputs
        src = inspect.getsource(prepare_plan_inputs)
        check('params.me_source.setdefault(tid, "declared" if own.get("source") == "manual"' in src,
              "the two provenances are set in the one place the chain is resolved")
        check(src.index('params.me_source[tid] = "contract"')
              < src.index('"declared" if own.get("source")')
              < src.index('params.me_source[tid] = "override"'),
              "contract is overwritten by owned/declared, and the override is applied LAST of all")
        check("declared" not in src.split('for key, val in (opts.me_te_overrides')[1],
              "nothing after the override loop can put the provenance back")

        # And the values themselves, through the real resolver's consumer.
        _reset(con)
        _character(con)
        _esi_cache(con, [{"type_id": bp_tid, "me": 2, "te": 4, "quantity": -2, "runs": 5}])
        _declare(con, 1, product, me=9, te=18, runs=5, qty=1)
        p = _params(owned=owned_blueprints(CTX))
        check(p.me_te_for(product, "manufacturing", 5) == (9.0, 18.0),
              "a batch inside the declared coverage is built at the DECLARED research, not the read one")

        # ── formula evidence layer ────────────────────────────────────────────────────────────
        print("a declared formula and a pasted one are not two formulas:")
        _reset(con)
        _character(con)
        _esi_cache(con, [])
        _source(con, "paste:1", "paste", rx_tid, 3)
        con.execute("INSERT INTO pp_char_formula_jobs (character_id, prints_json, fetched_at) "
                    "VALUES (?,?,?)", (CHAR, json.dumps(
                        [{"blueprint_id": 900 + i, "blueprint_type_id": rx_tid,
                          "blueprint_location_id": 1} for i in range(3)]), 1.0))
        con.commit()
        no_decl = formula_print_floor(CTX, owned_blueprints(CTX))
        check(no_decl.get(formula_out) == 3,
              "without a declaration the paste's three formulas are the floor (control)")
        _declare(con, 1, formula_out, runs=-1, qty=3)
        own_decl = owned_blueprints(CTX)
        check(len(own_decl[formula_out]["copies"]) == 3,
              "declaring three formulas puts three prints in the holding")
        floor = formula_print_floor(CTX, own_decl)
        check(formula_out not in floor,
              "and the stock/observed layer adds NOTHING on top — the same three items, once")
        check(_print_limits(_params(owned=own_decl, stock_prints=floor),
                            formula_out, "reaction", 40) == (3, False),
              "so the concurrency cap is three reactions, not six or nine")

        print("declared formulas feed the print cap the same way owned ones do:")
        _reset(con)
        _character(con)
        _esi_cache(con, [])
        _declare(con, 1, formula_out, runs=-1, qty=1)
        one = owned_blueprints(CTX)
        check(_print_limits(_params(owned=one), formula_out, "reaction", 40) == (1, False),
              "one declared formula is one job at a time")

        # ── a declaration is known per PRODUCT, not per account ───────────────────────────────
        # The Industry half of the bug the Reactions side hit: `prints_known()` asked
        # `blueprint_coverage().complete`, which is an account-wide fact about an ESI SCAN, and used
        # it to suppress a cap on a holding the user had stated by hand. A declaration is not a
        # scan. What must NOT change: an undeclared product on the same half-seen account is still
        # a floor, and a floor never serialises real work.
        print("an incomplete ESI picture does not suppress a DECLARED holding's cap:")
        _reset(con)
        _character(con)
        _esi_cache(con, [{"type_id": rx_tid, "me": 0, "te": 0, "quantity": 3, "runs": -1}])
        con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
                    "VALUES (?,?,?,?)", (CHAR - 1, "NoScope", CTX, ""))
        con.commit()
        cov = bp.blueprint_coverage(CTX)
        check(not cov["complete"] and cov["missing"] == 1,
              f"a character with no blueprint scope makes the account's picture incomplete ({cov})")
        _declare(con, 1, formula_out, runs=-1, qty=10)
        decl_owned = owned_blueprints(CTX)
        half = BuildParams(owned=decl_owned, blueprint_coverage=cov,
                           declared_prints=bp.declared_products(decl_owned))
        check(_print_limits(half, formula_out, "reaction", 40) == (10, False),
              "ten declared formulas cap at ten jobs even though a character was never scanned")
        check(half.prints_known(formula_out) and not half.prints_known(),
              "the product is known while the ACCOUNT still is not — that is the whole fix")

        # The control, and the constraint: same account, a product with only a partial scan behind
        # it. Its ESI count is a floor and must not become a cap.
        undeclared = next((r["output_type_id"] for r in con.execute(
            "SELECT output_type_id FROM reactions WHERE output_type_id != ? LIMIT 1",
            (formula_out,))), None)
        if undeclared is not None:
            scanned = {undeclared: {"me": 0, "te": 0, "kind": "bpo", "runs": -1, "copy_count": 1,
                                    "copies": [{"me": 0, "te": 0, "kind": "bpo", "runs": -1}]}}
            mixed = BuildParams(owned={**decl_owned, **scanned}, blueprint_coverage=cov,
                                declared_prints=bp.declared_products(decl_owned))
            check(_print_limits(mixed, undeclared, "reaction", 40) == (None, False),
                  "and an UNDECLARED product on the same account stays uncapped — absent evidence "
                  "never serialises work")
            check(_print_limits(mixed, formula_out, "reaction", 40) == (10, False),
                  "with the declared one beside it still capped, in the same params")
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CHAR - 1,))
        con.commit()

        # ── BPO vs BPC ────────────────────────────────────────────────────────────────────────
        print("the BPO-vs-BPC preference moves both the print cap and the cost basis:")
        _reset(con)
        _character(con)
        # Two 5-run copies AND an original, all scanned. Without a preference the plan holds all 3.
        _esi_cache(con, [{"type_id": bp_tid, "me": 4, "te": 8, "quantity": -2, "runs": 5},
                         {"type_id": bp_tid, "me": 4, "te": 8, "quantity": -2, "runs": 5},
                         {"type_id": bp_tid, "me": 1, "te": 2, "quantity": -1, "runs": -1}])
        both = owned_blueprints(CTX)[product]
        check(len(both["copies"]) == 3 and both["kind"] == "bpo" and both["runs"] == -1,
              "an original anywhere in the holding covers any batch (control)")
        check(_print_limits(_params(owned={product: both}), product, "manufacturing", 40)[0] == 3,
              "and three prints run three jobs side by side (control)")

        _declare(con, 1, product, qty=0, prefer="bpo")   # a preference and no print of its own
        pref_bpo = owned_blueprints(CTX)[product]
        check(pref_bpo.get("prefer") == "bpo" and pref_bpo.get("source") is None,
              "a quantity-0 row states the preference WITHOUT replacing what ESI read")
        check(len(pref_bpo["copies"]) == 1 and pref_bpo["copies"][0]["kind"] == "bpo",
              "preferring the original leaves exactly the original in the pool")
        check(_print_limits(_params(owned={product: pref_bpo}), product, "manufacturing", 40)[0] == 1,
              "one print, so one job at a time — the cost of never spending a copy")
        check(pref_bpo["me"] == 1 and pref_bpo["te"] == 2,
              "and the batch is costed at the ORIGINAL's research, not the better copies'")

        con.execute("UPDATE pp_industry_blueprints SET prefer='bpc' WHERE context_id=?", (CTX,))
        con.commit()
        pref_bpc = owned_blueprints(CTX)[product]
        check(pref_bpc["kind"] == "bpc" and pref_bpc["runs"] == 10,
              "preferring the copies gives finite coverage that a big batch runs short of")
        check(_print_limits(_params(owned={product: pref_bpc}), product, "manufacturing", 40)[0] >= 2,
              "and two prints, so two jobs run side by side")
        check(pref_bpc["me"] == 4, "costed at the copies' research")

        check(_apply_kind_preference([{"kind": "bpc", "me": 1, "te": 1, "runs": 5}], "bpo")
              == [{"kind": "bpc", "me": 1, "te": 1, "runs": 5}],
              "a preference for a kind you do not hold can never empty the holding")

        # ── the conservatism proof ────────────────────────────────────────────────────────────
        print("nothing that plans a build may read the new keys:")
        check(_no_unguarded_new_key_reads(product),
              "a full plan run against a holding that RAISES on `source`/`prefer` is unaffected")

    finally:
        _reset(con)
        con.close()
        for _m in _flag_holders():
            _m._manual_enabled = _real_enabled

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


def _source_for_owned(own: dict) -> str:
    """The one line of `prepare_plan_inputs` this file is asserting about, isolated so the branch is
    exercised without standing up a whole plan."""
    return "declared" if own.get("source") == "manual" else "owned"


def _no_unguarded_new_key_reads(_product: int) -> bool:
    """A full plan against an owned-holding whose dicts REFUSE the two new keys.

    Same idea as `test_a_plan_with_no_job_length_ceiling_is_byte_for_byte_the_old_plan`'s params
    proxy: the old code could not read `source` or `prefer` because they did not exist, so anything
    in the planner reading one unguarded — where a declaration is not in play — is a behaviour
    change hiding as an additive key. Both plans must also be equal, byte for byte.
    """
    import sqlite3
    from app.industry.graph import (load_manufacturing_graph, load_reaction_graph,
                                    clear_graph_cache, BuildParams)
    from app.industry.schedule import plan_queue

    clear_graph_cache()
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE types (type_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE blueprints (blueprint_type_id INTEGER PRIMARY KEY, product_type_id INTEGER,
            output_qty INTEGER, base_time INTEGER, max_runs INTEGER);
        CREATE TABLE blueprint_materials (blueprint_type_id INTEGER, type_id INTEGER, quantity INTEGER);
        CREATE TABLE reactions (reaction_id INTEGER PRIMARY KEY, output_type_id INTEGER,
            output_qty INTEGER, cycle_time INTEGER);
        CREATE TABLE reaction_inputs (reaction_id INTEGER, type_id INTEGER, quantity INTEGER);
    """)
    names = {100: "Widget", 200: "MineralA"}
    for tid, name in names.items():
        con.execute("INSERT INTO types VALUES (?, ?)", (tid, name))
    con.execute("INSERT INTO blueprints VALUES (1000, 100, 1, 3600, 10)")
    con.execute("INSERT INTO blueprint_materials VALUES (1000, 200, 10)")
    con.commit()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    prices = {200: {"sell_price": 100.0, "source": "Jita"},
              100: {"sell_price": 100000.0, "source": "Jita"}}
    adj = {200: 100.0, 100: 90000.0}
    pools = {"manufacturing": 3, "reaction": 2}

    class _NoNewKeys(dict):
        """The holding as it was BEFORE these keys existed: asking for one raises."""

        def get(self, key, default=None):
            if key in ("source", "prefer"):
                raise AssertionError(f"unguarded read of new key {key!r}")
            return dict.get(self, key, default)

        def __getitem__(self, key):
            if key in ("source", "prefer"):
                raise AssertionError(f"unguarded read of new key {key!r}")
            return dict.__getitem__(self, key)

    # The guard has to be a real one — a proxy that quietly answers None would make this test
    # pass by doing nothing.
    try:
        _NoNewKeys({"me": 1}).get("source")
        raise AssertionError("the strict holding did not refuse `source` — the test proves nothing")
    except AssertionError as exc:
        if "unguarded read" not in str(exc):
            raise

    copies = [{"me": 5, "te": 10, "kind": "bpc", "runs": 8}]
    plain = {100: {"me": 5, "te": 10, "kind": "bpc", "runs": 8,
                   "copies": [dict(c) for c in copies], "copy_count": 1}}
    strict = {100: _NoNewKeys({"me": 5, "te": 10, "kind": "bpc", "runs": 8,
                               "copies": [dict(c) for c in copies], "copy_count": 1})}
    coverage = {"characters": 1, "cached": 1, "complete": True}
    a = plan_queue([(100, 20)], mfg, rx, prices, adj,
                   BuildParams(owned=plain, blueprint_coverage=coverage,
                               mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                               struct_time_mult=1.0), names, pools)
    b = plan_queue([(100, 20)], mfg, rx, prices, adj,
                   BuildParams(owned=strict, blueprint_coverage=coverage,
                               mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                               struct_time_mult=1.0), names, pools)
    return json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
