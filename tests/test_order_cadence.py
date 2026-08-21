#!/usr/bin/env python3
"""The order a customer is quoted is the order the dashboard draws.

Reported as five surfaces that disagree about one plan; this is the second of them. A customer
order was committed by `_allocate_and_insert`, which took no cadence argument and read none — it
ran the batch flat out on the argument that somebody is waiting. `split_order_tops_to_cadence` then
paced exactly the same rows on the NEXT dashboard load. So the player showed a customer one layout
and the next page load said another, about the same order, with no edit in between.

The fix is not to drop the pacing — it is to do it where the commitment is made. Since 2026-08-14
the order is paced at quote time, and the dashboard pass finds nothing left to do.

**The invariant this pins is the agreement, not either layout.** Whatever job shape the commit
produces, the dashboard's own repair passes must leave it alone: same rows, same run counts, same
characters. A future change to either side that breaks the tie fails here rather than on screen.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_order_cadence.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_order_cadence.py
"""
import json
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                        # noqa: E402
from app.features import ensure_features_table                           # noqa: E402
from app.reactions.jobs import (_allocate_and_insert, _reaction_cadence_hours,   # noqa: E402
                                ensure_industry_jobs_table,
                                ensure_reaction_assignments_table,
                                level_product_runs, split_order_tops_to_cadence)

CTX = -98795
CID = -9391
# A real reaction output, so `_reaction_cycle_times` has a cycle to read — with a fake type id the
# dashboard pass short-circuits and the test would pass by doing nothing at all.
PROD = 16654                    # Titanium Chromide
ORDER_ID = -4242
CADENCE_DAYS = 2.0

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _set_feature_state(key, state):
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT state FROM pp_features WHERE key=?", (key,)).fetchone()
    con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, key))
    con.commit()
    con.close()
    return (row["state"] if row else None) or "admin"


def _reset(con):
    con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
    con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CID,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _seed(con):
    # 5 levels of both reaction skills — a real slot count, so the allocator has somewhere to put
    # the jobs the cadence asks for.
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
                "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?)",
                (CID, "Cadence Tester", CTX, "esi-industry.read_character_jobs.v1", 5, 5))
    con.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) "
                "VALUES (?,?,?)", (CID, json.dumps([]), time.time()))
    con.commit()


def _rows(con):
    """The order's layout as the dashboard would read it: (character, runs) per row, sorted."""
    return sorted((r["character_id"], int(r["runs"] or 0)) for r in con.execute(
        "SELECT a.character_id, a.runs FROM pp_reaction_assignments a "
        "JOIN pp_characters c ON c.character_id = a.character_id WHERE c.context_id=?", (CTX,)))


def main():
    ensure_features_table()
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()

    from app.industry.settings import (get_max_reaction_job_days,
                                       set_max_reaction_job_days)
    was_flag = _set_feature_state("industry_job_length_policy", "public")
    was_days = get_max_reaction_job_days(CTX)
    con = get_connection()
    try:
        set_max_reaction_job_days(CTX, CADENCE_DAYS)
        cad = _reaction_cadence_hours(CTX)
        check(abs(cad - CADENCE_DAYS * 24.0) < 1e-6,
              f"the account really is on a {CADENCE_DAYS:g}-day rhythm ({cad}h)")

        _reset(con)
        _seed(con)
        con.close()

        # Titanium Chromide's SDE cycle is 3h a run, so a 2-day window is 16 runs a job. A 400-run
        # batch cannot be one job on any rhythm the player would recognise.
        cyc = None
        c2 = get_connection()
        cyc = c2.execute("SELECT MIN(cycle_time) AS t FROM reactions WHERE output_type_id=?",
                         (PROD,)).fetchone()["t"]
        c2.close()
        node = {"via": None, "unit_cost": 100.0, "job_cost": 0.0, "cycle_time": cyc}
        res = _allocate_and_insert(CTX, PROD, "Titanium Chromide", node, {}, {}, 400, ORDER_ID)
        check(res.get("runs_assigned") == 400,
              f"the whole batch is committed (got {res.get('runs_assigned')})")

        con = get_connection()
        quoted = _rows(con)
        check(len(quoted) > 1,
              f"the batch is spread over real slots rather than run end to end (got "
              f"{len(quoted)} jobs of {sorted({r for _, r in quoted})} runs)")
        # The cadence is a stated target, not an absolute, and pacing is bounded by the reactors
        # the host actually has. So the durable statement is the pair: either every job lands
        # inside the window, or the host has nothing left to split it onto. What must never happen
        # is a job over the window beside an idle reactor that could have taken it.
        free_left = con.execute(
            "SELECT COUNT(*) AS n FROM pp_reaction_assignments WHERE character_id=?",
            (CID,)).fetchone()["n"]
        longest_h = max(r for _, r in quoted) * (cyc / 3600.0)
        check(longest_h <= CADENCE_DAYS * 24.0 + 3.0 or free_left >= 11,
              f"no job outruns the window with a reactor still free to take it "
              f"({longest_h:.1f}h against a {CADENCE_DAYS * 24.0:.0f}h window, "
              f"{free_left} of 11 reactors used)")

        # ── ...and when it DOES outrun the window, the row says so ───────────────────────────────
        # An order's rows never reach the leveller: its top row is excluded by design (the run count
        # is the batch the order was quoted on). So if the breach is not recorded at the moment the
        # row is written, nothing will ever record it, and the badge decision 1 promises can never
        # appear on the one kind of plan a customer is waiting for.
        badges = [dict(r) for r in con.execute(
            "SELECT runs, COALESCE(cadence_over_h,0) AS over FROM pp_reaction_assignments "
            "WHERE character_id=?", (CID,))]
        # Measured against the window PLUS the grace, which is the one definition every pass that
        # sizes a reaction job uses: the grace is slack the player already absorbs, spent
        # deliberately rather than buying another reactor for a few minutes, so reporting it back as
        # a breach would flag the plan for doing what it was told. The leveller measures the same
        # way (`cap_hours_by_tid` carries the grace), and the two must not disagree.
        window_h = CADENCE_DAYS * 24.0 + 3.0
        for b in badges:
            b["real_h"] = b["runs"] * (cyc / 3600.0)
        overruns = [b for b in badges if b["real_h"] > window_h + 1e-6]
        check(bool(overruns),
              f"this fixture really does overrun the window ({len(overruns)} of {len(badges)} jobs)")
        check(all(b["over"] > 0 for b in overruns),
              f"every over-window job carries a breach, not a silent 0 "
              f"(got {sorted({round(b['over'], 1) for b in overruns})})")
        check(all(abs(b["over"] - (b["real_h"] - window_h)) < 0.05 for b in overruns),
              "...and the number is the real overrun against the window, not an approximation")
        check(all(b["over"] == 0 for b in badges if b["real_h"] <= window_h + 1e-6),
              "a job that fits, or fits inside the grace, claims no breach")
        con.close()

        # ── ...and now the next dashboard read, which runs both repair passes before it reads ────
        moved = split_order_tops_to_cadence(CTX)
        level_product_runs(CTX)
        con = get_connection()
        after = _rows(con)
        check(moved == 0,
              f"the dashboard's cadence split finds nothing left to do (wrote {moved} rows)")
        check(after == quoted,
              "THE INVARIANT: the layout the customer was quoted is the layout the dashboard draws")

        # ...and it is stable, not merely equal once. A second read must not start moving it either.
        con.close()
        split_order_tops_to_cadence(CTX)
        level_product_runs(CTX)
        con = get_connection()
        check(_rows(con) == quoted, "and a second read leaves it alone as well")

        # ...and the same again for an order that comfortably FITS, so the no-op above is not
        # merely the dashboard pass refusing to touch a plan it had no room to change.
        _reset(con)
        _seed(con)
        con.close()
        res2 = _allocate_and_insert(CTX, PROD, "Titanium Chromide", node, {}, {}, 100, ORDER_ID)
        check(res2.get("runs_assigned") == 100, "a smaller batch commits in full too")
        con = get_connection()
        quoted2 = _rows(con)
        longest2 = max(r for _, r in quoted2) * (cyc / 3600.0)
        check(longest2 <= CADENCE_DAYS * 24.0,
              f"every job of it lands inside the window ({longest2:.1f}h of "
              f"{CADENCE_DAYS * 24.0:.0f}h)")
        con.close()
        moved2 = split_order_tops_to_cadence(CTX)
        level_product_runs(CTX)
        con = get_connection()
        check(moved2 == 0 and _rows(con) == quoted2,
              "and the dashboard draws exactly what was quoted, unchanged")

        # ── The re-split must not ERASE what the row was carrying ────────────────────────────────
        # `split_order_tops_to_cadence` deletes a row and inserts N in its place. An INSERT that
        # omits the four cost/breach columns does not leave them alone — it resets them to the
        # schema default, so a row that arrived carrying a real overrun left claiming none. Every
        # badge on every order this pass touched was silently wiped.
        #
        # The stage is starved on purpose: nine unrelated rows leave two free reactors, so the
        # free-slot check binds and the split cannot bring the batch inside the window. That is the
        # case where the badge matters most — and the case where it used to disappear.
        _reset(con)
        _seed(con)
        for _ in range(9):
            con.execute(
                "INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?)",
                (CID, PROD + 1, "Filler", 5, 0.0, 0.0, 7000.0, 0))
        con.execute(
            "INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, input_cost, "
            "reward, created_at, tier_order, order_id, cadence_over_h, surplus_runs, jobs_saved, "
            "recover_runs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CID, PROD, "Titanium Chromide", 200, 20000.0, 0.0, 7100.0, 0, ORDER_ID,
             673.2, 41, 3, 17))
        con.commit()
        con.close()

        from app.reactions.jobs import _reaction_time_mult
        mult = _reaction_time_mult(CTX)
        moved3 = split_order_tops_to_cadence(CTX)
        con = get_connection()
        split_rows = [dict(r) for r in con.execute(
            "SELECT runs, COALESCE(cadence_over_h,0) AS over, COALESCE(surplus_runs,0) AS surplus, "
            "COALESCE(jobs_saved,0) AS saved, COALESCE(recover_runs,0) AS recover "
            "FROM pp_reaction_assignments WHERE character_id=? AND order_id=?", (CID, ORDER_ID))]
        check(moved3 > 1 and len(split_rows) == moved3,
              f"the batch really was re-split ({moved3} rows)")
        check(sum(r["runs"] for r in split_rows) == 200,
              f"and the total is preserved exactly (got {sum(r['runs'] for r in split_rows)})")
        graced = CADENCE_DAYS * 24.0 + 3.0
        real_h = [r["runs"] * (cyc / 3600.0) * mult for r in split_rows]
        check(max(real_h) > graced,
              f"the free-slot check bound, so the jobs still overrun ({max(real_h):.1f}h of "
              f"{graced:.0f}h)")
        check(all(r["over"] > 0 for r in split_rows),
              f"EVERY re-split row still reports its overrun — it is not reset to 0 "
              f"(got {sorted({round(r['over'], 1) for r in split_rows})})")
        check(all(abs(r["over"] - (h - graced)) < 0.05 for r, h in zip(split_rows, real_h)),
              "...and it is RE-MEASURED for the shorter job, not the deleted row's stale number")
        check(all((r["surplus"], r["saved"], r["recover"]) == (41, 3, 17) for r in split_rows),
              f"the leveller's cost note survives the re-split unchanged "
              f"(got {sorted({(r['surplus'], r['saved'], r['recover']) for r in split_rows})})")

        _reset(con)
    finally:
        try:
            con.close()
        except Exception:
            pass
        set_max_reaction_job_days(CTX, was_days)
        _set_feature_state("industry_job_length_policy", was_flag)

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
