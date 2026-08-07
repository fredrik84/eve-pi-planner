#!/usr/bin/env python3
"""Once you have PASTED your industry window, a formula you never declared is one you don't own.

This pins the one place where this codebase's standing rule — **absent evidence never serialises
work** — is deliberately inverted, and the conditions under which it may be. A real account
declared ~238 formulas, ordered Reinforced Carbon Fiber, and was told to react Carbon Fiber, a
sub-reaction whose formula it does not hold, because "not mentioned" was read as "unknown".

The invariants, in both directions:

  * with NO paste, nothing is ever reported missing — an account that typed in three formulas has
    made a statement about three formulas, not about its library, and the old permissive reading
    must survive untouched for it;
  * a PASTED batch containing formulas makes the library complete, and then a product whose formula
    is absent from every evidence source is reported, with the runs the plan wants and the
    FORMULA's own type_id (contracts list the formula, not the product);
  * a formula the account holds by ANY evidence — declared, pasted, scanned in stock, seen on a job
    — is never reported. Evidence is unioned, because the expensive error here is telling someone
    to buy a formula sitting in their hangar;
  * nothing this module returns is a cost or a shopping-list line;
  * the names a paste could NOT resolve are kept and ride along on every report, because an
    unresolved name is indistinguishable from a formula you don't own (a CCP rename did exactly
    this once) — and they go away with the batch that carried them;
  * with the flag off, every report is empty.

In-process; run inside the container against a NON-PROD database. Seeds rows under a fabricated
context id, flips the two flags it needs to `public` for the duration, and restores both in a
finally.

    docker compose cp test_missing_formulas.py web:/srv/app/ && \
      docker compose exec web python3 test_missing_formulas.py
"""
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                     # noqa: E402
from app.features import ensure_features_table                        # noqa: E402
from app.industry.assets import ensure_asset_tables                   # noqa: E402
from app.industry.blueprints import (                                 # noqa: E402
    MANUAL_FEATURE_KEY, delete_blueprint_batch, ensure_char_blueprints_table,
    ensure_manual_blueprints_table, ensure_paste_unresolved_table, replace_blueprint_batch)
from app.reactions.library import (                                   # noqa: E402
    FEATURE_KEY, held_formula_products, library_state, missing_formulas, wanted_from_sequence)

CTX = -98781
CHAR = -9351

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_stock WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_asset_sources WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_char_blueprints WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _flags(state, keys=(FEATURE_KEY, MANUAL_FEATURE_KEY)):
    """Force the named gates to `state` and return what they were, so the run leaves the DB as it
    found it — a test that silently rolls a feature out to everyone is worse than no test."""
    ensure_features_table()
    con = get_connection()
    try:
        marks = ",".join("?" * len(keys))
        was = {r["key"]: r["state"] for r in con.execute(
            f"SELECT key, state FROM pp_features WHERE key IN ({marks})", tuple(keys))}
        for key in keys:
            con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, key))
        con.commit()
    finally:
        con.close()
    return was


def _restore(was):
    con = get_connection()
    try:
        for key, state in was.items():
            con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, key))
        con.commit()
    finally:
        con.close()


def _paste(name, lines):
    """One pasted industry window, through the real import path (short layout)."""
    return replace_blueprint_batch(CTX, name, "\n".join(lines))


def _stock(con, key, scope, tid, qty):
    con.execute(
        "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, item_count, "
        "scope) VALUES (?,?,?,?,?,?,?,?)", (CTX, key, "container", key, "", 1, 1, scope))
    con.execute("INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                (CTX, key, int(tid), float(qty)))
    con.commit()


def main():
    ensure_asset_tables()
    ensure_char_blueprints_table()
    ensure_manual_blueprints_table()
    ensure_paste_unresolved_table()
    was = _flags("public")
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT r.reaction_id, r.output_type_id, t.name AS fname, p.name AS pname "
            "FROM reactions r JOIN types t ON t.type_id = r.reaction_id "
            "JOIN types p ON p.type_id = r.output_type_id ORDER BY r.reaction_id LIMIT 2").fetchall()
        if len(rows) < 2:
            print("this SDE has fewer than two named reactions — cannot run")
            return 2
        have, want = dict(rows[0]), dict(rows[1])
        print(f"declared: {have['pname']} · missing: {want['pname']}")
        _reset(con)

        wanted = {have["output_type_id"]: 10, want["output_type_id"]: 200}

        print("with NO paste, absence stays UNKNOWN and nothing is reported:")
        con.execute(
            "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, quantity, "
            "prefer, updated_at, batch, batch_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (CTX, 1, have["output_type_id"], 0, 0, -1, 1, "", time.time(), "", ""))
        con.commit()
        state = library_state(CTX)
        check(not state["complete"], "a hand-typed row is a statement about that row, not a library")
        check(missing_formulas(CTX, wanted)["formulas"] == [],
              "so a product with no formula anywhere is still not called missing")

        print("a PASTED batch containing formulas completes the library:")
        _reset(con)
        res = _paste("Reactor box", [f"3 x {have['fname']}\t0\t0\t-1\tComposite"])
        check(res.get("added") == 3, f"the paste imported (added={res.get('added')})")
        state = library_state(CTX)
        check(state["complete"], "one pasted window naming a formula is a complete statement")
        check(state["formulas_declared"] == 1, f"1 formula declared (got {state['formulas_declared']})")

        rep = missing_formulas(CTX, wanted)
        names = [r["name"] for r in rep["formulas"]]
        check(have["pname"] not in names, "the formula the paste NAMES is not reported missing")
        check(want["pname"] in names, f"the one it doesn't name IS ({names})")
        row = next((r for r in rep["formulas"] if r["type_id"] == want["output_type_id"]), None)
        check(row and row["runs_needed"] == 200, f"it carries the runs the plan wants (got {row})")
        check(row and row["formula_type_id"] == want["reaction_id"],
              "and the FORMULA's type_id — contracts list the formula, not the product")
        check(row and row["formula_name"] == want["fname"], "named as the item you'd go and buy")
        check(set(rep) == {"complete", "formulas", "unresolved", "formulas_declared"},
              f"the report is advice, not a cost: no price/total keys (got {sorted(rep)})")
        check(all("cost" not in k and "price" not in k for r in rep["formulas"] for k in r),
              "and no row carries a cost of its own")

        print("a formula held on OTHER evidence is never reported:")
        _stock(con, "corp:9:c100", "corp:9", want["reaction_id"], 2)
        held = held_formula_products(CTX)
        check(want["output_type_id"] in held, "a formula in an enabled corp container is held")
        check(missing_formulas(CTX, wanted)["formulas"] == [],
              "so nothing is missing — evidence is unioned, never just the declaration")

        # A doubled letter, not a real rename: `Fullerides Reaction Formula` — the actual client
        # copy that started this — RESOLVES, because the parser's product-name fallback (ee633be)
        # strips the suffix and finds the product. This has to be a name nothing can rescue.
        print("names a paste could not resolve are KEPT, and ride along on the report:")
        _reset(con)
        _paste("Reactor box", [f"3 x {have['fname']}\t0\t0\t-1\tComposite",
                               "Nanotransistorss Reaction Formula\t0\t0\t-1\tComposite"])
        rep = missing_formulas(CTX, wanted)
        unresolved = [u["name"] for u in rep["unresolved"]]
        check(unresolved == ["Nanotransistorss Reaction Formula"],
              f"the unmatched name survives the import (got {unresolved})")
        check(rep["unresolved"][0]["batch_name"] == "Reactor box",
              "tagged with the batch that carried it, so the user knows which window to fix")
        check(library_state(CTX)["unresolved"] == rep["unresolved"],
              "the same list the library state reports — one source, not two")

        print("...and go away with the batch that carried them:")
        batch = con.execute("SELECT DISTINCT batch FROM pp_industry_blueprints WHERE context_id=? "
                            "AND batch<>''", (CTX,)).fetchone()["batch"]
        delete_blueprint_batch(CTX, batch)
        check(library_state(CTX)["unresolved"] == [],
              "a warning about a paste the user deleted is one they cannot act on")
        check(not library_state(CTX)["complete"],
              "and with its only pasted batch gone the library is not complete again")

        print("a re-paste REPLACES its batch's unresolved names rather than piling them up:")
        _paste("Reactor box", [f"1 x {have['fname']}\t0\t0\t-1\tComposite", "Bogus Widget I\t0\t0\t-1\tX"])
        check([u["name"] for u in library_state(CTX)["unresolved"]] == ["Bogus Widget I"],
              "the previous run's unmatched name is gone, not accumulated")

        print("the step list every surface passes in is built the same way:")
        check(wanted_from_sequence([{"type_id": 5, "runs": 3}, {"type_id": 5, "runs": 4},
                                    {"type_id": 6, "runs": 1}]) == {5: 7, 6: 1},
              "runs for one product across several steps add up")
        check(wanted_from_sequence([]) == {} and wanted_from_sequence(None) == {},
              "and an empty plan asks about nothing")

        print("the DASHBOARD asks the same question of what is already planned:")
        _reset(con)
        _paste("Reactor box", [f"3 x {have['fname']}\t0\t0\t-1\tComposite"])
        from app.reactions.jobs import _plan_missing_formulas
        rep = _plan_missing_formulas(CTX, [{"pending": [
            {"type_id": have["output_type_id"], "runs": 4},
            {"type_id": want["output_type_id"], "runs": 7}]}])
        check([(r["name"], r["runs_needed"]) for r in rep["formulas"]] == [(want["pname"], 7)],
              f"a plan already holding slots is checked too, with ITS runs (got {rep['formulas']})")
        check(_plan_missing_formulas(CTX, []) == {
            "complete": True, "formulas": [], "unresolved": [], "formulas_declared": 1},
              "and an empty dashboard asks about nothing")

        print("with the flag off, every report is empty even on a complete library:")
        _paste("Reactor box", [f"1 x {have['fname']}\t0\t0\t-1\tComposite"])
        check(missing_formulas(CTX, wanted)["formulas"], "(precondition: it reports with the flag on)")
        _flags("hidden", keys=(FEATURE_KEY,))       # the reactions gate only — the library stays complete
        rep = missing_formulas(CTX, wanted)
        check(library_state(CTX)["complete"], "the library is still complete")
        check(rep["formulas"] == [], "...and nothing at all is reported")
        _flags("public", keys=(FEATURE_KEY,))
    finally:
        _reset(con)
        con.close()
        _restore(was)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
