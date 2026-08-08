#!/usr/bin/env python3
"""One run count per product, across every character (`reactions_level_runs`).

The reported shape, verbatim: Carbon Fiber at 125 runs on one character, 90 on the next two, 75 on
the fourth — four numbers to read and type for one fungible product, four different finish times,
fifteen reactors doing what twelve would. Each of those four numbers was correct in isolation; each
came from a separate plan that sized its own chain exactly.

The invariants this pins, in the user's own priority order (TODO 28):

  1. every job of a product carries the SAME run count, on every character;
  2. the jobs of a stage land together — run counts are chosen so durations match;
  3. it costs fewer slots, never more than the character actually has free;
  and underneath all three: no chain is left short, no chain loses its last row of a product, the
  top row of a chain (its commitment) is never touched, and rounding up never costs more than 15%.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_level_runs.py web:/srv/app/ && \
      docker compose exec web python3 test_level_runs.py
"""
import json
import sys
import time

sys.path.insert(0, ".")
from app.db import get_connection                                        # noqa: E402
from app.features import ensure_features_table                           # noqa: E402
from app.reactions.jobs import (_choose_stage_layout, _level_options,    # noqa: E402
                                ensure_industry_jobs_table, ensure_reaction_assignments_table,
                                level_product_runs)

CTX = -98794
CHARS = [(-9371, "Chislen"), (-9372, "Sajkisen414"), (-9373, "Nuori"), (-9374, "Ekaoni")]
CF, OOS, RCF = 57453, 57454, 57455       # Carbon Fiber, Oxy-Organic Solvents, the product above
FLAG = "reactions_level_runs"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _reset(con):
    for cid, _ in CHARS:
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (cid,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _characters(con, skill=5):
    for cid, name in CHARS:
        con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
                    "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?)",
                    (cid, name, CTX, "esi-industry.read_character_jobs.v1", skill, skill))
        con.execute("INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) "
                    "VALUES (?,?,?)", (cid, json.dumps([]), time.time()))
    con.commit()


def _plan(con, char_id, chain_at, rows):
    """One chain on one character: `rows` is [(type_id, name, runs, jobs, tier)]."""
    for tid, name, runs, jobs, tier in rows:
        for _ in range(jobs):
            con.execute(
                "INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                "input_cost, reward, created_at, tier_order) VALUES (?,?,?,?,?,?,?,?)",
                (char_id, tid, name, runs, 1000.0, 0.0, chain_at, tier))
    con.commit()


def _cf_rows(con):
    return [dict(r) for r in con.execute(
        "SELECT a.character_id, a.type_id, a.runs, a.tier_order FROM pp_reaction_assignments a "
        "JOIN pp_characters c ON c.character_id=a.character_id WHERE c.context_id=?", (CTX,))]


def main():
    ensure_features_table()
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()

    print("the run counts one product could carry, and what each costs:")
    # The reported case as pure numbers: four chains needing 375 / 360 / 360 / 300 of one product,
    # currently split 3 / 4 / 4 / 4 ways. Plenty of headroom, no ceiling worth the name.
    opts = _level_options([375, 360, 360, 300], [10, 10, 10, 10], max_runs=10_000)
    by_runs = {o["runs"]: o for o in opts}
    check(all(len(set(o["per_group"])) >= 0 for o in opts) and opts, "there are options at all")
    check(125 in by_runs and by_runs[125]["jobs"] == 12,
          f"125 runs everywhere is 12 jobs, not 15 (got {by_runs.get(125, {}).get('jobs')})")
    check(by_runs[125]["per_group"] == [3, 3, 3, 3],
          f"and three jobs on every character (got {by_runs[125]['per_group']})")
    check(by_runs[125]["surplus"] == 1500 - 1395,
          f"the surplus is 105 runs of stock (got {by_runs[125]['surplus']})")
    check(all(o["surplus"] <= 0.15 * 1395 for o in opts),
          "nothing offered overshoots the real requirement by more than 15%")
    check(all(o["runs"] * min(o["per_group"]) >= 0 for o in opts) and
          all(sum(j * o["runs"] for j in [o["per_group"][i]]) >= [375, 360, 360, 300][i]
              for o in opts for i in range(4)),
          "every option still covers every chain's own requirement in full")

    print("...and it never asks for a reactor the character does not have:")
    tight = _level_options([375, 360, 360, 300], [3, 4, 4, 4], max_runs=10_000)
    check(all(all(j <= c for j, c in zip(o["per_group"], [3, 4, 4, 4])) for o in tight),
          "no option exceeds the per-character job cap")
    check(any(o["runs"] == 125 for o in tight), "125 still fits inside 3/4/4/4 slots")
    check(not _level_options([1000, 3], [4, 4], max_runs=10_000),
          "3 runs on one chain and 1000 on another has no cheap common number — left alone")

    print("a stage is chosen to land in ONE go:")
    # Two products in a stage, one twice as slow per run. Levelling them to the same RUN count
    # would land them 50 hours apart; the same duration is what a single login collects.
    layout = _choose_stage_layout({
        CF: {"cycle": 1.0, "options": _level_options([200, 200], [8, 8], 200)},
        OOS: {"cycle": 2.0, "options": _level_options([100, 100], [8, 8], 100)},
    })
    d_cf = layout[CF]["runs"] * 1.0
    d_oos = layout[OOS]["runs"] * 2.0
    check(abs(d_cf - d_oos) <= 1e-6,
          f"both products finish at the same hour ({d_cf}h vs {d_oos}h)")
    check(layout[CF]["runs"] != layout[OOS]["runs"],
          "which means the same DURATION, not naively the same number")

    print("...and settles on a number you can type when tidy runs are on:")
    # 200 and 120 needed, four reactors: 67 is the cheapest number in goo, 70 the one you can type
    # without checking. Same five jobs either way — the only difference is the surplus.
    pair = {CF: {"cycle": 1.0, "options": _level_options([200, 120], [4, 4], 100)}}
    check(_choose_stage_layout(pair)[CF]["runs"] == 67, "off: the cheapest number wins (67)")
    check(_choose_stage_layout(pair, prefer_tidy=True)[CF]["runs"] == 70,
          "on: the tidy number wins for the same job count (70)")

    con = get_connection()
    try:
        _reset(con)
        _characters(con)
        # The reported plan: one chain per character, each with its own Carbon Fiber requirement
        # under a Reinforced Carbon Fiber top row that must not be touched.
        for (cid, _), (runs, jobs, top) in zip(CHARS, [(125, 3, 40), (90, 4, 38),
                                                       (90, 4, 38), (75, 4, 30)]):
            _plan(con, cid, 1000.0 + cid, [(CF, "Carbon Fiber", runs, jobs, 0),
                                           (RCF, "Reinforced Carbon Fiber", top, 1, 1)])
        before = _cf_rows(con)
        cf_before = [r for r in before if r["type_id"] == CF]
        check(len({r["runs"] for r in cf_before}) == 3 and len(cf_before) == 15,
              "before: three different numbers across 15 jobs")

        written = level_product_runs(CTX)
        after = _cf_rows(con)
        cf = [r for r in after if r["type_id"] == CF]
        counts = sorted({r["runs"] for r in cf})
        check(written > 0 and len(counts) == 1,
              f"ONE run count for Carbon Fiber on every character (got {counts})")
        per_char = {}
        for r in cf:
            per_char[r["character_id"]] = per_char.get(r["character_id"], 0) + 1
        check(len(cf) < 15, f"and in fewer jobs than before (15 -> {len(cf)})")
        for (cid, name), need in zip(CHARS, [375, 360, 360, 300]):
            made = per_char.get(cid, 0) * counts[0]
            check(made >= need, f"{name}'s chain still gets the {need} runs it needs (has {made})")
            check(per_char.get(cid, 0) >= 1, f"{name} keeps at least one Carbon Fiber job")
        check(sum(per_char.get(c, 0) for c, _ in CHARS) * counts[0] <= 1.15 * 1395,
              "the rounding-up surplus stays inside 15%")
        tops = [r for r in after if r["type_id"] == RCF]
        check(len(tops) == 4 and sorted(r["runs"] for r in tops) == [30, 38, 38, 40],
              "the top row of every chain — its commitment — is untouched")
        check(level_product_runs(CTX) == 0, "running it again writes nothing — idempotent")

        print("a stage is never made SLOWER than it already was:")
        longest = max(r["runs"] for r in cf_before)
        check(counts[0] <= longest,
              f"no job runs longer than the longest job already planned ({counts[0]} <= {longest})")

        print("two chains on ONE character each keep their own job:")
        _reset(con)
        _characters(con)
        cid = CHARS[0][0]
        _plan(con, cid, 2000.0, [(CF, "Carbon Fiber", 100, 2, 0), (RCF, "RCF", 20, 1, 1)])
        _plan(con, cid, 3000.0, [(CF, "Carbon Fiber", 60, 2, 0), (RCF, "RCF", 12, 1, 1)])
        level_product_runs(CTX)
        rows = [r for r in _cf_rows(con) if r["type_id"] == CF]
        chains = [dict(r) for r in con.execute(
            "SELECT created_at, COUNT(*) AS n FROM pp_reaction_assignments WHERE character_id=? "
            "AND type_id=? GROUP BY created_at", (cid, CF))]
        check(len({r["runs"] for r in rows}) == 1,
              f"both chains carry the same number (got {sorted({r['runs'] for r in rows})})")
        check(len(chains) == 2 and all(c["n"] >= 1 for c in chains),
              "and neither chain lost its last row — the dashboard still knows it is waiting")
        _reset(con)
    finally:
        con.close()

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
