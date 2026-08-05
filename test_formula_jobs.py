#!/usr/bin/env python3
"""Formulas OBSERVED in real industry jobs are a third evidence source for the concurrency cap.

A builder who keeps their reaction formulas in a CORP HANGAR and is not a Director can never be
answered by `/corporations/{id}/assets/` or `/corporations/{id}/blueprints/`. But every industry job
names the SPECIFIC PHYSICAL print it runs on (`blueprint_id`), and both job endpoints they already
grant are readable without Director. So N distinct blueprint_ids sharing one blueprint_type_id is
measured evidence of N physical formulas, wherever they live.

The invariants:

  * distinct observed blueprint_ids raise the floor for the formula's OUTPUT product;
  * two jobs off the SAME blueprint_id are ONE formula — ids are unioned, never counted;
  * a PASTE naming that formula wins outright: the pasted quantity is the answer and the observed
    floor is not added on top of it (the user stating what they hold beats what we inferred);
  * observation may only ever RAISE a number — an unused formula is invisible, so reading it as a
    ceiling would serialise work the builder can really do;
  * nothing about ME, TE or run coverage moves — a job states none of them;
  * and — the one that would be a real outage — SLOT CAPACITY and RUNNING-JOB COUNTS are unchanged.
    Job history is fetched by its own path into its own table precisely so that
    `app/reactions/jobs.py::fetch_industry_jobs`, whose output feeds `_character_capacities`,
    `running_counts` and every free-slot count by COUNTING ROWS, never sees a completed job.

In-process; run inside the container against a NON-PROD database. Seeds rows under a fabricated
context id and removes them in a finally.

    docker compose cp test_formula_jobs.py web:/srv/app/ && \
      docker compose exec web python3 test_formula_jobs.py
"""
import inspect
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                   # noqa: E402
from app.industry.assets import ensure_asset_tables                 # noqa: E402
from app.industry.blueprints import (                               # noqa: E402
    ensure_char_blueprints_table, ensure_formula_job_prints_table, formula_print_floor,
    observed_formula_prints, owned_blueprints)
from app.industry.graph import BuildParams                          # noqa: E402
from app.industry.jobs import ensure_manufacturing_jobs_table, running_counts   # noqa: E402
from app.industry.schedule import _print_limits                     # noqa: E402
from app.reactions.jobs import (                                    # noqa: E402
    ensure_industry_jobs_table, ensure_reaction_assignments_table, fetch_industry_jobs,
    _character_capacities)

CTX = -98774
CHAR = -9341

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    for t in ("pp_asset_stock", "pp_asset_sources"):
        con.execute(f"DELETE FROM {t} WHERE context_id=?", (CTX,))
    for t in ("pp_char_blueprints", "pp_char_formula_jobs", "pp_char_industry_jobs",
              "pp_char_manufacturing_jobs", "pp_reaction_assignments"):
        con.execute(f"DELETE FROM {t} WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _character(con, mass_reactions=3):
    """A character connected for job tracking with reaction skills trained — so it counts toward
    capacity — and a complete blueprint picture, so the cap is allowed to fire at all."""
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
        "VALUES (?,?,?,?)",
        (CHAR, "JobTester", CTX,
         "esi-industry.read_character_jobs.v1 esi-characters.read_blueprints.v1"))
    for col, val in (("mass_reactions", mass_reactions), ("advanced_mass_reactions", 0)):
        try:
            con.execute(f"UPDATE pp_characters SET {col}=? WHERE character_id=?", (val, CHAR))
        except Exception:
            pass
    con.commit()


def _blueprint_cache(con, rows):
    con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?)", (CHAR, json.dumps(rows), 1.0))
    con.commit()


def _history(con, formula, blueprint_ids):
    """The job-history table: one row per DISTINCT print seen, no status/runs/dates at all."""
    con.execute(
        "INSERT INTO pp_char_formula_jobs (character_id, prints_json, fetched_at) VALUES (?,?,?) "
        "ON CONFLICT (character_id) DO UPDATE SET prints_json=excluded.prints_json",
        (CHAR, json.dumps([{"blueprint_id": b, "blueprint_type_id": formula,
                            "blueprint_location_id": 60000}
                           for b in blueprint_ids]), 1.0))
    con.commit()


def _live_jobs(con, formula, product, entries):
    """The Reactions tab's live cache — RAW ESI objects, which have always carried blueprint_id.
    `entries` is [(blueprint_id, status)]."""
    con.execute(
        "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
        "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json",
        (CHAR, json.dumps([
            {"job_id": 900 + i, "activity_id": 9, "status": st, "runs": 10,
             "product_type_id": product, "blueprint_type_id": formula, "blueprint_id": bid,
             "blueprint_location_id": 60000}
            for i, (bid, st) in enumerate(entries)]), 1.0))
    con.commit()


def _source(con, key, scope, tid, qty):
    con.execute(
        "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, item_count, "
        "scope) VALUES (?,?,?,?,?,?,?,?)",
        (CTX, key, "container", key, "", 1, 1, scope))
    con.execute("INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                (CTX, key, int(tid), float(qty)))
    con.commit()


def _params(stock_prints=None, owned=None):
    return BuildParams(owned=owned or {}, stock_prints=stock_prints or {},
                       blueprint_coverage={"characters": 1, "cached": 1, "complete": True})


def main():
    ensure_asset_tables()
    ensure_char_blueprints_table()
    ensure_formula_job_prints_table()
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    ensure_manufacturing_jobs_table()
    con = get_connection()
    try:
        row = con.execute("SELECT reaction_id, output_type_id FROM reactions "
                          "ORDER BY reaction_id LIMIT 1").fetchone()
        if not row:
            print("no `reactions` rows in this SDE — cannot run")
            return 2
        formula, product = int(row["reaction_id"]), int(row["output_type_id"])
        print(f"formula {formula} -> product {product}")
        _reset(con)
        _character(con)

        print("distinct observed blueprint_ids raise the floor:")
        _history(con, formula, [7001, 7002])
        check(observed_formula_prints(CTX) == {product: 2},
              f"2 distinct prints observed (got {observed_formula_prints(CTX)})")
        floor = formula_print_floor(CTX, {})
        check(floor.get(product) == 2, f"the floor is 2 (got {floor.get(product)})")
        check(_print_limits(_params(stock_prints=floor), product, "reaction", 40) == (2, False),
              "...and 40 runs are capped at 2 concurrent jobs, not one per free slot")

        print("two jobs off the SAME print are ONE formula:")
        _history(con, formula, [7001, 7001, 7001])
        _live_jobs(con, formula, product, [(7001, "active")])
        check(observed_formula_prints(CTX) == {product: 1},
              f"the same id seen three times, and in both caches, counts once "
              f"(got {observed_formula_prints(CTX)})")
        check(formula_print_floor(CTX, {}).get(product) == 1, "so the floor is 1, not 3")

        print("the live reactions cache counts too — it has always carried blueprint_id:")
        _history(con, formula, [])
        _live_jobs(con, formula, product, [(8001, "active"), (8002, "active")])
        check(observed_formula_prints(CTX) == {product: 2},
              f"2 running jobs on 2 different prints (got {observed_formula_prints(CTX)})")

        print("a PASTE naming the formula overrides the observed floor:")
        _history(con, formula, [7001, 7002, 7003, 7004])
        _live_jobs(con, formula, product, [])
        _source(con, "paste:910", "", formula, 1)
        floor = formula_print_floor(CTX, {})
        check(floor.get(product) == 1,
              f"the pasted 1 is the answer — 4 observed are NOT added on top (got {floor.get(product)})")
        check(_print_limits(_params(stock_prints=floor), product, "reaction", 40) == (1, False),
              "so the plan runs one job at a time, as the user said they can")

        print("...but a corp SCAN is not a statement, so it is reconciled with a max:")
        con.execute("DELETE FROM pp_asset_stock WHERE context_id=?", (CTX,))
        con.execute("DELETE FROM pp_asset_sources WHERE context_id=?", (CTX,))
        con.commit()
        _source(con, "corp:5:c910", "corp:5", formula, 2)
        floor = formula_print_floor(CTX, {})
        check(floor.get(product) == 4,
              f"4 observed beats the 2 a scan found, and never sums to 6 (got {floor.get(product)})")

        print("observation NEVER lowers a number:")
        _reset(con)
        _character(con)
        _blueprint_cache(con, [{"type_id": formula, "me": 0, "te": 0, "quantity": 3, "runs": -1}])
        _history(con, formula, [7001])
        owned = owned_blueprints(CTX)
        check(owned.get(product, {}).get("copy_count") == 3, "the blueprint endpoint reports 3")
        floor = formula_print_floor(CTX, owned)
        check(product not in floor, "one formula ever USED adds nothing to three known held")
        check(_print_limits(_params(stock_prints=floor, owned=owned), product, "reaction", 40)
              == (3, False), "so the cap stays 3, never drops to the 1 we happened to observe")
        _history(con, formula, [7001, 7002, 7003, 7004, 7005])
        floor = formula_print_floor(CTX, owned)
        check(floor.get(product) == 2, f"but 5 observed raises it by 2 (got {floor.get(product)})")
        check(_print_limits(_params(stock_prints=floor, owned=owned), product, "reaction", 40)
              == (5, False), "to 5 concurrent — prints the personal endpoint cannot see")

        print("nothing about ME, TE or run coverage moves:")
        p = _params(stock_prints={product: 5})
        check(p.me_te_for(product, "reaction") == (0.0, 0.0), "a reaction still has no ME/TE")
        check(p.copies_for(product, "reaction") == [],
              "an observed job never becomes a researched copy the plan can build off")
        check(p.owned == {} and p.me_by_product == {} and product not in p.me_source,
              "and it never leaks into `owned`, the per-product ME map or ME provenance")

        print("SLOT CAPACITY AND RUNNING COUNTS ARE UNCHANGED BY THE HISTORY:")
        _reset(con)
        _character(con)                       # 1 + 3 Mass Reactions = 4 reaction slots
        _live_jobs(con, formula, product,
                   [(8001, "active"), (8002, "active"), (8003, "delivered")])
        base_running = running_counts(CTX)
        base_caps = _character_capacities(CTX)
        check(base_running.get(CHAR, {}).get("reaction") == 2,
              f"2 occupying jobs before any history exists (got {base_running.get(CHAR)})")
        check(base_caps and base_caps[0]["free_slots"] == 2,
              f"4 slots - 2 running = 2 free (got {base_caps})")
        _history(con, formula, [8001, 8002, 8003, 9001, 9002, 9003, 9004])
        check(running_counts(CTX) == base_running,
              f"7 historical prints change the running count by nothing (got {running_counts(CTX)})")
        check(_character_capacities(CTX) == base_caps,
              f"...and free slots by nothing (got {_character_capacities(CTX)})")
        check(observed_formula_prints(CTX).get(product) == 7,
              "while the floor sees all 7 — the two readings are structurally separate")
        src = inspect.getsource(fetch_industry_jobs)
        check("include_completed" not in src,
              "app/reactions/jobs.py::fetch_industry_jobs still fetches RUNNING jobs only")
        check("status" not in json.loads(
                  con.execute("SELECT prints_json FROM pp_char_formula_jobs WHERE character_id=?",
                              (CHAR,)).fetchone()["prints_json"])[0],
              "and a history row carries no status, so nothing counting occupancy could use it")
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
