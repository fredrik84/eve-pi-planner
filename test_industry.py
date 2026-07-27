"""In-process tests for the Industry make-or-buy engine (app/industry/graph.py).

Runs against a synthetic, hand-computable recipe graph seeded into an in-memory SQLite DB — no
live SDE or ESI needed, same style as test_optimizer's in-process cases. Asserts durable
invariants of the cost math: the EVE material formula, the build-vs-buy decision, shopping-list
aggregation, and the cost/time totals.

Run: python3 test_industry.py
"""
import math
import sqlite3
import time
from app.db import get_connection
import sys

sys.path.insert(0, ".")

from app.industry.graph import (
    BuildParams, effective_material_qty, load_manufacturing_graph, load_reaction_graph,
    collect_reachable, build_plan, resolve_unit_costs,
)
from app.industry.schedule import (order_ranks, 
    aggregate_demand, build_tasks, schedule, plan_queue, Task, _built_deps,
    _critical_priority,
)

# ── Synthetic graph ───────────────────────────────────────────────────────────────────────────
# 100 Widget  (mfg) = 2× Gadget + 10× MineralA
# 101 Gadget  (mfg) = 5× MineralB + 1× Sprocket
# 102 Sprocket(rx)  = 10× Goo  (output 2 per run)
# 200 MineralA, 201 MineralB, 202 Goo — raw (buy only)
NAMES = {100: "Widget", 101: "Gadget", 102: "Sprocket", 200: "MineralA", 201: "MineralB", 202: "Goo"}
SELL = {200: 100.0, 201: 50.0, 202: 20.0, 101: 1000.0, 102: 500.0, 100: 100000.0}
ADJ = {200: 100.0, 201: 50.0, 202: 20.0, 101: 1000.0, 102: 500.0, 100: 90000.0}


def _seed_con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE types (type_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE blueprints (blueprint_type_id INTEGER PRIMARY KEY, product_type_id INTEGER,
            output_qty INTEGER, base_time INTEGER, max_runs INTEGER);
        CREATE TABLE blueprint_materials (blueprint_type_id INTEGER, type_id INTEGER, quantity INTEGER);
        CREATE TABLE reactions (reaction_id INTEGER PRIMARY KEY, output_type_id INTEGER,
            output_qty INTEGER, cycle_time INTEGER);
        CREATE TABLE reaction_inputs (reaction_id INTEGER, type_id INTEGER, quantity INTEGER);
        """
    )
    for tid, name in NAMES.items():
        con.execute("INSERT INTO types VALUES (?, ?)", (tid, name))
    # Widget BP 1000
    con.execute("INSERT INTO blueprints VALUES (1000, 100, 1, 3600, 10)")
    con.executemany("INSERT INTO blueprint_materials VALUES (1000, ?, ?)", [(101, 2), (200, 10)])
    # Gadget BP 1001
    con.execute("INSERT INTO blueprints VALUES (1001, 101, 1, 1800, 100)")
    con.executemany("INSERT INTO blueprint_materials VALUES (1001, ?, ?)", [(201, 5), (102, 1)])
    # Sprocket reaction 2000 (2 per run)
    con.execute("INSERT INTO reactions VALUES (2000, 102, 2, 3600)")
    con.execute("INSERT INTO reaction_inputs VALUES (2000, 202, 10)")
    con.commit()
    return con


def _prices(sell):
    return {tid: {"sell_price": p, "source": "Jita"} for tid, p in sell.items()}


_passed = 0


def check(name, cond):
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        raise SystemExit(1)


def approx(a, b, tol=0.01):
    return a is not None and abs(a - b) <= tol


def test_material_formula():
    print("test_material_formula")
    check("no ME", effective_material_qty(10, 1, 0, 1.0) == 10)
    check("ME 10%", effective_material_qty(10, 1, 10, 1.0) == 9)          # 9.0 -> 9
    check("floor at runs", effective_material_qty(1, 5, 90, 1.0) == 5)    # 0.5 -> 1, floored to 5
    check("reaction rig 2.2%", effective_material_qty(10, 1, 0, 0.978) == 10)  # 9.78 -> 10
    check("zero runs", effective_material_qty(10, 0, 0, 1.0) == 0)


def test_graph_loaders():
    print("test_graph_loaders")
    con = _seed_con()
    mfg = load_manufacturing_graph(con)
    rx = load_reaction_graph(con)
    check("widget in mfg", 100 in mfg and mfg[100]["output_qty"] == 1)
    check("widget inputs", sorted(i["type_id"] for i in mfg[100]["inputs"]) == [101, 200])
    check("sprocket in rx", 102 in rx and rx[102]["output_qty"] == 2)
    check("reachable set", collect_reachable(100, mfg, rx) == {100, 101, 102, 200, 201, 202})


def test_build_all():
    print("test_build_all (everything cheaper to build)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    res = build_plan(100, 1, mfg, rx, _prices(SELL), ADJ,
                     BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0), NAMES)
    m = res["metrics"]
    # Hand-computed: materials MineralA 10×100 + MineralB 10×50 + Goo 10×20 = 1700
    check("materials_cost", approx(m["materials_cost"], 1700.0))
    # Job cost = SCC 4% of EIV: widget 3000×.04=120, gadget 1500×.04=60, sprocket 200×.04=8 = 188
    check("job_cost", approx(m["job_cost"], 188.0))
    check("total_cost", approx(m["total_cost"], 1888.0))
    check("job_count", m["job_count"] == 3)
    check("job_hours", approx(m["total_job_hours"], 3.0))  # 3 jobs × 1h
    check("root builds", res["tree"]["decision"] == "build")
    check("no unresolved", res["unresolved"] == [])
    # Shopping list aggregates the three raws only
    shop = {s["type_id"]: s["qty"] for s in res["shopping_list"]}
    check("shop raws only", set(shop) == {200, 201, 202})
    check("shop mineralA qty", shop[200] == 10)


def test_make_or_buy_flips():
    print("test_make_or_buy_flips (cheap Gadget on market → buy it)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    sell = {**SELL, 101: 300.0}  # Gadget build ≈384 > buy 300 → should buy
    res = build_plan(100, 1, mfg, rx, _prices(sell), ADJ, BuildParams(), NAMES)
    shop = {s["type_id"]: s["qty"] for s in res["shopping_list"]}
    check("gadget bought", 101 in shop and shop[101] == 2)
    check("no sprocket build", not any(j["type_id"] == 102 for j in res["jobs"]))
    check("only widget job", [j["type_id"] for j in res["jobs"]] == [100])


def test_root_forced_build():
    print("test_root_forced_build (buying the target is cheaper, but we still build it)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    sell = {**SELL, 100: 10.0}  # Widget dirt cheap on market
    res = build_plan(100, 1, mfg, rx, _prices(sell), ADJ, BuildParams(), NAMES)
    check("root still builds", res["tree"]["decision"] == "build")
    check("root shows buy alt", approx(res["tree"]["buy_unit_cost"], 10.0))


def test_quantity_scales_and_excess():
    print("test_quantity_scales_and_excess (reaction output_qty=2 → odd demand leaves excess)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # 1 Gadget needs 1 Sprocket, but the reaction makes 2/run → 1 run, 2 produced, 1 excess.
    res = build_plan(101, 1, mfg, rx, _prices(SELL), ADJ, BuildParams(), NAMES)
    spr = next(j for j in res["jobs"] if j["type_id"] == 102)
    check("sprocket 1 run", spr["runs"] == 1)
    check("sprocket produced 2", spr["produced"] == 2)
    # tree carries the excess on the sprocket node (feeds the Phase-2 stock ledger)
    spr_node = res["tree"]["inputs"][ [i["type_id"] for i in res["tree"]["inputs"]].index(102) ]
    check("sprocket excess 1", spr_node["excess"] == 1)
    goo = {s["type_id"]: s["qty"] for s in res["shopping_list"]}[202]
    check("goo for 1 run", goo == 10)


def _agg_for(targets):
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    memo, unit = resolve_unit_costs(mfg, rx, _prices(SELL), ADJ, BuildParams())
    for tid, _ in targets:
        unit(tid, frozenset())
    return aggregate_demand(targets, memo, mfg, rx, BuildParams()), mfg, rx


def test_demand_aggregation_shares_batches():
    print("test_demand_aggregation_shares_batches")
    # Two Widgets: Gadget demand = 2 widgets × 2 = 4 → ONE Gadget batch of 4 runs, not two of 2.
    agg, mfg, rx = _agg_for([(100, 2)])
    check("widget runs 2", agg[100]["runs"] == 2)
    check("gadget batched to 4", agg[101]["runs"] == 4)
    check("sprocket runs 2", agg[102]["runs"] == 2)          # 4 needed / 2-per-run
    check("mineralA gross 20", agg[200]["gross"] == 20)
    # Two separate orders for the same product aggregate identically to one order of the sum.
    agg2, _, _ = _agg_for([(100, 1), (100, 1)])
    check("split orders == combined", agg2[101]["runs"] == 4)


def test_excess_ledger():
    print("test_excess_ledger (reaction output 2 → 1 leftover on odd demand)")
    agg, _, _ = _agg_for([(101, 1)])                          # 1 Gadget → 1 Sprocket needed
    check("sprocket produced 2", agg[102]["produced"] == 2)
    check("sprocket leftover 1", agg[102]["leftover"] == 1)


def test_on_hand_reduces_builds():
    print("test_on_hand_reduces_builds (stock nets out net demand)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    memo, unit = resolve_unit_costs(mfg, rx, _prices(SELL), ADJ, BuildParams())
    unit(101, frozenset())
    # Need 4 Gadgets but 3 already in stock → build only 1.
    agg = aggregate_demand([(101, 4)], memo, mfg, rx, BuildParams(), on_hand={101: 3})
    check("gadget net 1", agg[101]["net"] == 1)
    check("gadget runs 1", agg[101]["runs"] == 1)


def test_job_split_respects_the_blueprint_cap():
    """How a type's runs become jobs. Replaces a test of a helper that production stopped calling —
    the real logic lives in build_tasks, and it has two hard rules: never lose runs, and never put
    more runs in one job than the blueprint allows.

    Also the reason a component can legitimately show thousands of runs: many T2 component
    blueprints make ONE unit per run, so units and runs are the same number.
    """
    print("test_job_split_respects_the_blueprint_cap")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    agg = {101: {"type_id": 101, "activity": "manufacturing", "build": True, "gross": 100,
                 "net": 100, "runs": 100, "produced": 100, "leftover": 0, "output_qty": 1}}

    # Split across slots so the batch runs in parallel.
    tasks, _ = build_tasks(agg, mfg, rx, BuildParams(), {"manufacturing": 10, "reaction": 5})
    check("one job per free slot", len(tasks) == 10)
    check("no runs lost", sum(t.runs for t in tasks) == 100)
    check("evenly balanced", max(t.runs for t in tasks) - min(t.runs for t in tasks) <= 1)

    # A single slot means a single job — parallelism is opportunistic, not mandatory.
    tasks, _ = build_tasks(agg, mfg, rx, BuildParams(), {"manufacturing": 1, "reaction": 1})
    check("one slot -> one job", len(tasks) == 1 and tasks[0].runs == 100)

    # A blueprint's per-job cap must force MORE jobs than there are slots.
    capped = dict(mfg[101]); capped["max_runs"] = 7
    mfg2 = {**mfg, 101: capped}
    tasks, _ = build_tasks(agg, mfg2, rx, BuildParams(), {"manufacturing": 10, "reaction": 5})
    check("cap forces extra jobs", len(tasks) >= 15)
    check("no job exceeds the cap", max(t.runs for t in tasks) <= 7)
    check("still no runs lost", sum(t.runs for t in tasks) == 100)


def test_scheduler_linear_chain():
    print("test_scheduler_linear_chain (Sprocket→Gadget→Widget, 2h each, serial)")
    agg, mfg, rx = _agg_for([(100, 2)])
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0)
    tasks, by_type = build_tasks(agg, mfg, rx, P)
    deps = _built_deps(agg, mfg, rx)
    prio = _critical_priority(agg, deps, mfg, rx, P)
    sched = schedule(tasks, by_type, deps, {"manufacturing": 10, "reaction": 5}, prio)
    # Each tier is 2h; the chain is strictly serial regardless of free slots → 6h makespan.
    check("makespan 6h", approx(sched["makespan_hours"], 6.0))
    check("three waves", len(sched["waves"]) == 3)
    check("nothing unscheduled", sched["unscheduled"] == [])
    check("wave0 is reaction", sched["waves"][0]["tasks"][0]["type_id"] == 102)


def test_scheduler_slot_contention():
    print("test_scheduler_slot_contention (4 independent 1h jobs, 2 slots → 2h)")
    tasks = [Task(f"t{i}", 900 + i, "manufacturing", 1, 3600.0) for i in range(4)]
    by_type = {t.type_id: [t] for t in tasks}
    deps = {t.type_id: set() for t in tasks}          # all independent, all ready at t=0
    prio = {t.type_id: 1.0 for t in tasks}
    sched = schedule(tasks, by_type, deps, {"manufacturing": 2, "reaction": 0}, prio)
    check("makespan 2h", approx(sched["makespan_hours"], 2.0))    # 4 jobs / 2 slots = 2 rounds
    check("two start waves", len(sched["waves"]) == 2)
    # With infinite slots the same 4 jobs finish in 1h.
    sched2 = schedule([Task(f"t{i}", 900 + i, "manufacturing", 1, 3600.0) for i in range(4)],
                      by_type, deps, {"manufacturing": 4, "reaction": 0}, prio)
    check("wide makespan 1h", approx(sched2["makespan_hours"], 1.0))


def test_time_aware_make_or_buy():
    print("test_time_aware_make_or_buy (buy the slow bulk component when prioritizing speed)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    memo, unit = resolve_unit_costs(mfg, rx, _prices(SELL), ADJ, BuildParams())
    unit(101, frozenset())
    pools = {"manufacturing": 1, "reaction": 1}
    # 100 Gadgets → ~50 Sprocket runs (reaction, 1h each) = 50h build → exceeds a 24h cap → buy it.
    speed = aggregate_demand([(101, 100)], memo, mfg, rx, BuildParams(max_build_hours=24.0), pools=pools)
    check("sprocket bought for speed", speed[102]["build"] is False and speed[102]["bought_for_speed"])
    check("goo not built (sprocket bought)", 202 not in speed)
    check("gadget still built (target)", speed[101]["build"] is True)
    # Without the cap, the cheaper option (build) wins.
    cost = aggregate_demand([(101, 100)], memo, mfg, rx, BuildParams(), pools=pools)
    check("sprocket built without cap", cost[102]["build"] is True)


def test_marginal_buy():
    print("test_marginal_buy (buy when building saves a trivial %)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # Sprocket builds for ~104; price its market at 106 → building saves <2%, under a 4% floor.
    sell = {**SELL, 102: 106.0}
    memo, unit = resolve_unit_costs(mfg, rx, _prices(sell), ADJ, BuildParams())
    unit(101, frozenset())
    pools = {"manufacturing": 1, "reaction": 1}
    agg = aggregate_demand([(101, 10)], memo, mfg, rx, BuildParams(min_saving_pct=4.0), pools=pools)
    check("sprocket bought (low saving)", agg[102]["build"] is False and agg[102]["bought_marginal"])
    # Without the threshold, the cheaper option (build, 104<106) wins.
    agg2 = aggregate_demand([(101, 10)], memo, mfg, rx, BuildParams(), pools=pools)
    check("sprocket built without threshold", agg2[102]["build"] is True)


def test_manufacturing_slots():
    print("test_manufacturing_slots (skill → slot formula)")
    from app.industry.slots import manufacturing_slots, reaction_slots
    def row(mp, amp, mr=0, amr=0):
        return {"mass_production": mp, "advanced_mass_production": amp,
                "mass_reactions": mr, "advanced_mass_reactions": amr}
    check("base only", manufacturing_slots(row(0, 0)) == 1)
    check("MP5", manufacturing_slots(row(5, 0)) == 6)
    check("MP5+AMP5 capped 11", manufacturing_slots(row(5, 5)) == 11)
    check("over-cap still 11", manufacturing_slots(row(5, 8)) == 11)
    check("reaction slots independent", reaction_slots(row(5, 5, 4, 3)) == 8)


def test_per_product_me_from_blueprints():
    print("test_per_product_me_from_blueprints (owned ME reduces that product's inputs)")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # Own a Widget BPO at ME10 → Widget's inputs drop ~10%; Gadget (no owned BP) stays at ME0.
    params = BuildParams(me_by_product={100: (10.0, 20.0)}, owned={100: {"kind": "bpo", "me": 10, "te": 20}})
    res = build_plan(100, 1, mfg, rx, _prices(SELL), ADJ, params, NAMES)
    # Widget needs 10 MineralA at ME0; at ME10 → 9. Gadget input unchanged (ME0 → 2).
    shop = {s["type_id"]: s["qty"] for s in res["shopping_list"]}
    check("mineralA reduced by ME10", shop[200] == 9)
    check("gadget-side mineralB unchanged", shop[201] == 10)   # 5/run × 2 gadget runs, ME0
    check("me_te_for owned", params.me_te_for(100, "manufacturing") == (10.0, 20.0))
    check("me_te_for fallback", params.me_te_for(101, "manufacturing") == (0.0, 0.0))
    check("me_te_for reaction zero", params.me_te_for(102, "reaction") == (0.0, 0.0))
    check("tree shows owned", res["tree"]["owned"] == {"kind": "bpo", "me": 10, "te": 20})


def test_plan_queue_end_to_end():
    print("test_plan_queue_end_to_end")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    res = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ,
                     BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0), NAMES,
                     {"manufacturing": 10, "reaction": 5})
    # With 10 mfg / 5 rx slots each tier's runs split to run in parallel: Sprocket(2 runs→2 jobs,
    # 1h) → Gadget(4→4 jobs, 0.5h) → Widget(2→2 jobs, 1h) = 2.5h makespan. 8 jobs, 3 build steps.
    check("build_steps 3", res["metrics"]["build_steps"] == 3)
    check("job_count 8 (parallel split)", res["metrics"]["job_count"] == 8)
    check("makespan 2.5h parallel", approx(res["metrics"]["makespan_hours"], 2.5))
    # materials for 2 widgets: MineralA 20×100 + MineralB 20×50 + Goo 20×20 = 3400 (split-invariant)
    check("materials 3400", approx(res["metrics"]["materials_cost"], 3400.0))
    check("shopping 3 raws", len(res["shopping_list"]) == 3)


def test_unpriced_material_does_not_crash():
    """Regression: a material with no market price leaves build_unit_cost present-but-None, which
    a `.get(key, 0.0)` default does NOT catch — it used to raise TypeError and 500 the endpoint
    (real report: planning an Augoror failed). The plan must degrade to a floor cost instead."""
    print("test_unpriced_material_does_not_crash")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # Drop MineralA's price entirely -> Widget/Gadget/Sprocket can't be fully costed.
    partial = {k: v for k, v in SELL.items() if k != 200}
    res = plan_queue([(100, 2)], mfg, rx, _prices(partial), ADJ,
                     BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0), NAMES,
                     {"manufacturing": 10, "reaction": 5})
    check("plan still returns", isinstance(res, dict) and "metrics" in res)
    unres = res.get("unresolved") or []
    ids = {u if isinstance(u, int) else u.get("type_id") for u in unres}
    check("unpriced material flagged as unresolved", 200 in ids)
    check("cost is a finite floor", res["metrics"]["total_cost"] >= 0)


def test_queue_progress_requirements():
    """plan_queue must expose per-type build requirements — progress tracking compares real ESI
    jobs against these, and nothing else in the payload carries the shared-batch run count."""
    print("test_queue_progress_requirements")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    res = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ,
                     BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0), NAMES,
                     {"manufacturing": 10, "reaction": 5})
    reqs = {r["type_id"]: r for r in res.get("requirements", [])}
    check("requirements present", bool(reqs))
    # Every built type in the schedule must appear, with runs matching the aggregated batch.
    sched_types = {t["type_id"] for w in res["schedule"]["waves"] for t in w["tasks"]}
    check("every scheduled type has a requirement", sched_types <= set(reqs))
    check("widget requires 2 runs", reqs[100]["runs"] == 2)
    check("units = runs x output_qty", all(
        r["units"] == r["runs"] * r["output_qty"] for r in reqs.values()))
    # Bought raws are NOT requirements (you don't run a job for them).
    check("raw materials excluded", 200 not in reqs and 201 not in reqs)


def test_progress_rollup():
    """Live and simulated progress both roll per-type runs into queue totals and per-order UNITS.

    Neither path had a test, so the shared roll-up (_queue_snapshot / _type_row / _order_rows /
    _progress_payload, extracted from two near-identical copies) could drift silently. The DB and
    ESI edges are stubbed; what's under test is the arithmetic between them.
    """
    print("test_progress_rollup")
    import app.industry.progress as PR
    import app.industry.assets as ASSETS

    orders = [{"id": 1, "product_type_id": 100, "name": "Widget", "quantity": 4, "label": "cust"}]
    plan = {"requirements": [
        {"type_id": 100, "name": "Widget", "activity": "manufacturing", "runs": 4, "output_qty": 1},
        {"type_id": 110, "name": "Part", "activity": "reaction", "runs": 10, "output_qty": 2},
    ], "schedule": {"waves": [{"tasks": [{"type_id": 110}, {"type_id": 100}]}]}}

    orig = (PR._queue_snapshot, PR._epoch, PR._done_by_type, PR._running_by_type,
            ASSETS.owned_quantities)
    try:
        PR._queue_snapshot = lambda ctx: (orders, plan)
        PR._epoch = lambda ctx: 1234.0
        PR._done_by_type = lambda ctx, since: {110: 6}      # 6 of the 10 Part runs done
        PR._running_by_type = lambda ctx, since: {110: 2}   # 2 more in flight
        ASSETS.owned_quantities = lambda ctx: {100: 1}      # one finished Widget in the hangar

        live = PR.queue_progress(1)
        by = {t["type_id"]: t for t in live["types"]}
        check("live: ledger completions counted", by[110]["done_runs"] == 6)
        check("live: running capped by what's left", by[110]["running_runs"] == 2)
        check("live: waiting is the remainder", by[110]["waiting_runs"] == 2)
        check("live: owned stock counts as done", by[100]["done_runs"] == 1)
        check("live: totals sum the types",
              live["totals"]["required"] == 14 and live["totals"]["done"] == 7)
        check("live: pct from totals", approx(live["pct"], round(100.0 * 7 / 14, 1)))
        check("live: epoch reported", live["since"] == 1234.0)
        row = live["orders"][0]
        check("live: order units, not runs", row["done_units"] == 1 and row["quantity"] == 4)
        check("live: order partially built", row["status"] == "building")
        check("live: order label preserved", row["label"] == "cust")

        # A type's runs must never exceed what the plan asked for, however much is owned/logged.
        ASSETS.owned_quantities = lambda ctx: {100: 99}
        PR._done_by_type = lambda ctx, since: {100: 99, 110: 99}
        capped = PR.queue_progress(1)
        cby = {t["type_id"]: t for t in capped["types"]}
        check("live: done never exceeds required",
              all(t["done_runs"] <= t["required_runs"] for t in capped["types"]))
        check("live: a finished order reads complete", capped["orders"][0]["status"] == "complete")
        check("live: no negative waiting", all(t["waiting_runs"] >= 0 for t in capped["types"]))
        check("live: 100% when everything is done", cby[100]["pct"] == 100.0)

        sim = PR.simulated_progress(1, 50.0)
        check("sim: tagged as simulated", sim.get("simulated") is True and sim["simulated_pct"] == 50.0)
        check("sim: no epoch", sim["since"] is None)
        check("sim: same required total as live", sim["totals"]["required"] == 14)
        check("sim: roughly half done", 5 <= sim["totals"]["done"] <= 9)
        check("sim: totals are consistent", all(
            t["done_runs"] + t["running_runs"] + t["waiting_runs"] == t["required_runs"]
            for t in sim["types"]))
        check("sim: reports no stock", all(t["in_stock"] == 0 for t in sim["types"]))
        check("sim: order row present", len(sim["orders"]) == 1)

        check("sim: 0% is all waiting", PR.simulated_progress(1, 0.0)["totals"]["done"] == 0)
        full = PR.simulated_progress(1, 100.0)
        check("sim: 100% completes everything", full["totals"]["done"] == full["totals"]["required"])
        check("sim: 100% leaves nothing waiting", full["totals"]["waiting"] == 0)

        PR._queue_snapshot = lambda ctx: (None, None)
        check("empty queue reports empty", PR.queue_progress(1) == {"empty": True}
              and PR.simulated_progress(1, 50.0) == {"empty": True})
    finally:
        (PR._queue_snapshot, PR._epoch, PR._done_by_type, PR._running_by_type,
         ASSETS.owned_quantities) = orig


def test_stock_reduces_plan_but_never_the_target():
    """Owned stock nets off intermediate demand, but owning the PRODUCT must not make an order to
    build it plan zero jobs — the user asked to build that. Guards the on_hand wiring."""
    print("test_stock_reduces_plan_but_never_the_target")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0)
    base = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES,
                      {"manufacturing": 10, "reaction": 5})
    n_base = {r["type_id"]: r["runs"] for r in base["requirements"]}

    # Owning the intermediate (Gadget 101) must cut its required runs.
    withstock = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES,
                           {"manufacturing": 10, "reaction": 5}, on_hand={101: 999999})
    n_stock = {r["type_id"]: r["runs"] for r in withstock["requirements"]}
    check("stock removes the intermediate's runs", n_stock.get(101, 0) < n_base.get(101, 0))
    check("target still built with stock present", n_stock.get(100, 0) == n_base.get(100, 0))

    # The endpoint layer must strip the target from on_hand; simulate what _stock_for does.
    stock = {100: 999999, 101: 999999}
    stock.pop(100, None)
    guarded = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES,
                         {"manufacturing": 10, "reaction": 5}, on_hand=stock)
    g = {r["type_id"]: r["runs"] for r in guarded["requirements"]}
    check("owning the product does not zero the order", g.get(100, 0) == n_base.get(100, 0))


def test_marginal_threshold_scales_with_build_size():
    """The 'saves too little, just buy it' threshold is max(3% of total, 5m). The percentage must
    govern big builds and the floor must govern small ones — that's the whole point of one rule
    covering an Augoror and a Revelation. Guards the constant against being re-tuned blindly."""
    print("test_marginal_threshold_scales_with_build_size")
    from app.industry.graph import MARGINAL_BUILD_PCT_OF_TOTAL, MIN_BUILD_SAVING_ISK
    check("percentage is 3%", MARGINAL_BUILD_PCT_OF_TOTAL == 3.0)
    check("absolute floor is 5m", MIN_BUILD_SAVING_ISK == 5_000_000)

    def threshold(total):
        return max(MARGINAL_BUILD_PCT_OF_TOTAL / 100.0 * total, MIN_BUILD_SAVING_ISK)

    # Small hull: 3% is under the floor, so the floor binds (behaviour unchanged from before).
    check("floor binds on a ~150m hull", threshold(157_000_000) == MIN_BUILD_SAVING_ISK)
    check("floor binds on a ~9m hull", threshold(9_000_000) == MIN_BUILD_SAVING_ISK)
    # Capital: the percentage binds and is far above the floor.
    cap = threshold(2_474_000_000)
    check("percentage binds on a ~2.5b capital", cap > MIN_BUILD_SAVING_ISK)
    check("capital threshold is ~74m", abs(cap - 74_220_000) < 1_000_000)
    # Monotonic: a bigger build never gets a smaller threshold.
    check("threshold is monotonic", all(
        threshold(a) <= threshold(b) for a, b in zip(
            [1e7, 1e8, 5e8, 1e9, 5e9], [1e8, 5e8, 1e9, 5e9, 1e10])))


def test_install_assignment_spreads_and_respects_free_slots():
    """The 'do this now' checklist names WHICH character installs each job. It must never hand a
    character more jobs than it has free slots, must skip a character with none, and should spread
    work rather than piling it on one toon. Mirrors the greedy assignment in orders.to_install."""
    print("test_install_assignment_spreads_and_respects_free_slots")

    def assign(ready, chars):
        avail = {c["character_id"]: dict(c) for c in chars}
        out = []
        for t in ready:
            act = t["activity"]
            pick = max(avail.items(), key=lambda kv: kv[1].get(act, 0), default=(None, None))
            cid, info = pick
            if cid is not None and info and info.get(act, 0) > 0:
                info[act] -= 1
                out.append((t["name"], cid))
            else:
                out.append((t["name"], None))
        return out

    chars = [
        {"character_id": 1, "manufacturing": 4, "reaction": 2},
        {"character_id": 2, "manufacturing": 2, "reaction": 2},
        {"character_id": 3, "manufacturing": 0, "reaction": 0},   # full — must never be picked
    ]
    ready = [{"name": f"job{i}", "activity": "manufacturing"} for i in range(6)]
    res = assign(ready, chars)
    used = {}
    for _n, cid in res:
        if cid is not None:
            used[cid] = used.get(cid, 0) + 1
    check("full character never assigned", 3 not in used)
    check("never exceeds char 1's 4 free slots", used.get(1, 0) <= 4)
    check("never exceeds char 2's 2 free slots", used.get(2, 0) <= 2)
    check("spreads across both usable characters", len(used) == 2)
    check("all 6 jobs placed (4+2 capacity)", sum(used.values()) == 6)

    # One more job than capacity -> the extra is reported blocked, not silently dropped.
    over = assign([{"name": f"j{i}", "activity": "manufacturing"} for i in range(7)], chars)
    check("overflow is left unassigned", sum(1 for _n, c in over if c is None) == 1)
    check("overflow still returns every job", len(over) == 7)

    # Reaction jobs draw on the reaction pool, independently of manufacturing.
    rx = assign([{"name": f"r{i}", "activity": "reaction"} for i in range(5)], chars)
    rxu = sum(1 for _n, c in rx if c is not None)
    check("reaction pool capacity respected (2+2)", rxu == 4)


def test_fifo_wins_contested_slots():
    """Slot starvation is the case that matters: with plenty of slots everything runs at once and
    ordering is moot. Squeeze the pool to ONE slot and the first-queued order must finish first, and
    must not be held behind later work — while later orders still use whatever is left over."""
    print("test_fifo_wins_contested_slots")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0)
    prices, adj = _prices(SELL), ADJ

    # Two INDEPENDENT products (110 Cog shares no inputs with 101 Gadget) — dependency must not be
    # what decides the order, otherwise the test proves nothing about FIFO.
    con.execute("INSERT INTO types VALUES (110, 'Cog')")
    con.execute("INSERT INTO blueprints VALUES (1002, 110, 1, 1800, 100)")
    con.execute("INSERT INTO blueprint_materials VALUES (1002, 201, 5)")
    con.commit()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    names = dict(NAMES); names[110] = "Cog"
    pr = _prices({**SELL, 110: 900.0}); ad = {**ADJ, 110: 900.0}

    one_slot = {"manufacturing": 1, "reaction": 1}
    first = plan_queue([(110, 1), (101, 1)], mfg, rx, pr, ad, P, names, one_slot)
    tgt = {t["type_id"]: t for t in first["targets"]}
    check("first-queued gets rank 0", tgt[110]["rank"] == 0)
    check("second-queued gets rank 1", tgt[101]["rank"] == 1)
    check("first-queued finishes no later than second",
          tgt[110]["finish_hours"] <= tgt[101]["finish_hours"])
    check("first_delivery == the first order's finish",
          approx(first["metrics"]["first_delivery_hours"], tgt[110]["finish_hours"]))

    # Reversing the queue reverses who wins the contested slot — proves rank drives it, not the
    # products' own durations or the order they happen to appear in the graph.
    rev = plan_queue([(101, 1), (110, 1)], mfg, rx, pr, ad, P, names, one_slot)
    rtgt = {t["type_id"]: t for t in rev["targets"]}
    check("reversing the queue reverses the ranks", rtgt[101]["rank"] == 0 and rtgt[110]["rank"] == 1)
    check("the newly-first order now starts first",
          rtgt[101]["finish_hours"] <= first["targets"][1]["finish_hours"])

    # A shared component inherits the EARLIEST rank that needs it, so shared batches stay urgent.
    ranks = order_ranks([(100, 1), (101, 1)], mfg, rx)
    check("target 100 is rank 0", ranks[100] == 0)
    check("component shared with the first order keeps rank 0", ranks.get(102) == 0)

    # With slots to spare, both finish as fast as they would alone — FIFO must not serialise work.
    roomy = plan_queue([(110, 1), (101, 1)], mfg, rx, pr, ad, P, names,
                       {"manufacturing": 20, "reaction": 20})
    solo = plan_queue([(101, 1)], mfg, rx, pr, ad, P, names, {"manufacturing": 20, "reaction": 20})
    check("spare slots still run the later order in parallel",
          roomy["metrics"]["makespan_hours"] <= solo["metrics"]["makespan_hours"] * 1.05)


def test_missing_blueprints_are_reported():
    """A manufacturing step you own no blueprint for cannot be installed, so the plan must say so
    rather than quoting a build you can't start. Reactions need no blueprint and must never be
    flagged. Blueprint cost is deliberately NOT added to total_cost — BPCs trade via contracts with
    no market API, so there is no honest figure; the warning carries that instead."""
    print("test_missing_blueprints_are_reported")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)

    # Own nothing: both manufactured types flagged, the reaction (102 Sprocket) never is.
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0)
    res = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P, NAMES,
                     {"manufacturing": 5, "reaction": 5})
    miss = [m["name"] for m in res["metrics"]["missing_blueprints"]]
    check("manufactured steps flagged", "Widget" in miss and "Gadget" in miss)
    check("reaction never needs a blueprint", "Sprocket" not in miss)
    byid = {r["type_id"]: r for r in res["requirements"]}
    check("needs_blueprint set on the manufactured type", byid[100]["needs_blueprint"] is True)
    check("needs_blueprint false for the reaction", byid[102]["needs_blueprint"] is False)

    # Owning the Widget BPO clears only that one.
    P2 = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                     owned={100: {"me": 10, "te": 20, "kind": "bpo", "runs": -1}})
    res2 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P2, NAMES,
                      {"manufacturing": 5, "reaction": 5})
    miss2 = [m["name"] for m in res2["metrics"]["missing_blueprints"]]
    check("owned blueprint clears its own flag", "Widget" not in miss2)
    check("the still-unowned one stays flagged", "Gadget" in miss2)
    check("owned blueprint detail is carried", 
          (byid2 := {r["type_id"]: r for r in res2["requirements"]})[100]["blueprint"]["kind"] == "bpo")

    # Materials must not move just because a blueprint is or isn't owned — the blueprint charge is
    # its own line, not something folded into materials.
    check("materials are unaffected by blueprint ownership",
          approx(res["metrics"]["materials_cost"], res2["metrics"]["materials_cost"]))


def test_bpc_price_summary():
    """Blueprint contract pricing: what's listed NOW vs what it historically went for, since
    blueprints sell out constantly and 'nothing listed today' is the normal case. The median must
    survive the absurd outliers contract markets are full of, and a BPO must never be quoted as a
    BPC price — they differ by orders of magnitude."""
    print("test_bpc_price_summary")
    from app.industry.bpc import _summarise
    now = 1_000_000.0
    live_cutoff = now - 100
    rows = [
        {"price": 300e6, "runs": 1, "last_seen": now},
        {"price": 250e6, "runs": 1, "last_seen": now},          # cheapest live
        {"price": 900e6, "runs": 10, "last_seen": now},
        {"price": 9000e6, "runs": 1, "last_seen": now},         # hopeful seller
        {"price": 240e6, "runs": 1, "last_seen": now - 40 * 86400},   # expired
    ]
    r = _summarise(rows, live_cutoff)
    check("live excludes the expired listing", r["live"]["count"] == 4)
    check("cheapest live is right", approx(r["live"]["cheapest"], 250e6))
    # mean would be ~2.6b; median is 600m. That gap is the whole reason for using a median.
    check("median resists the 9b outlier", r["live"]["median"] < 1000e6)
    check("per-run normalises a 10-run copy", approx(r["live"]["median_per_run"], 275e6))
    check("history keeps everything in window", r["history"]["count"] == 5)
    check("history is cheaper than live here", r["history"]["cheapest"] < r["live"]["cheapest"])

    # Nothing live -> history must still answer rather than returning nothing.
    old = [{"price": 200e6, "runs": 1, "last_seen": now - 30 * 86400}]
    r2 = _summarise(old, live_cutoff)
    check("no live listings reports none", r2["live"] is None)
    check("history still gives an estimate", r2["history"]["count"] == 1)
    check("empty input returns nothing at all", _summarise([], live_cutoff) is None)


def test_blueprint_cost_affects_make_or_buy():
    """A blueprint you don't own is a real cost of BUILDING, so it must (a) be priced into the
    total and (b) count against the margin-saver — otherwise 'cheaper to build' can be quoted for a
    component whose print costs more than the whole build. A print with no copies listed is a
    multi-billion durable asset, so the component is bought instead."""
    print("test_blueprint_cost_affects_make_or_buy")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    base = dict(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                marginal_pct_of_total=0.0)

    # Baseline: Gadget (101) is worth building on materials alone.
    P0 = BuildParams(**base)
    r0 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P0, NAMES, pools)
    check("gadget built when blueprints are free", 101 in {r["type_id"] for r in r0["requirements"]})
    check("no blueprint cost by default", r0["metrics"]["blueprint_cost"] == 0)

    # A cheap BPC: still built, but the cost now shows up in the total.
    P1 = BuildParams(**base, bp_acquire={101: {"kind": "bpc", "price": 1000.0,
                                               "listings": [{"price": 1000.0, "runs": 10}],
                                               "median_per_run": 100.0}})
    r1 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P1, NAMES, pools)
    check("cheap BPC keeps it built", 101 in {r["type_id"] for r in r1["requirements"]})
    check("BPC cost is charged", r1["metrics"]["blueprint_cost"] > 0)
    check("total_cost includes the blueprint",
          r1["metrics"]["total_cost"] > r0["metrics"]["total_cost"])

    # An expensive BPC still reaches the margin-saver: a copy is a consumable you'd genuinely buy
    # for this batch, so it's a real cost of building and the normal margin rule applies to it.
    P2 = BuildParams(**base, bp_acquire={101: {"kind": "bpc", "price": 5e9,
                                               "listings": [{"price": 5e9, "runs": 1}],
                                               "median_per_run": 5e9}})
    r2 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P2, NAMES, pools)
    check("an unaffordable BPC flips it to buy", 101 not in {r["type_id"] for r in r2["requirements"]})
    check("nothing is charged for a print we no longer buy", r2["metrics"]["blueprint_cost"] == 0)

    # Only originals listed: must NOT force a buy. Blueprint ownership is only visible for prints
    # ESI will show us — anything in a corp hangar is corp-owned and needs the Director role, so
    # "no blueprint found" routinely means "we can't see it". Deciding on that absence would refuse
    # to build things the user owns the print for. It's priced and surfaced, never enforced.
    P3 = BuildParams(**base, bp_acquire={101: {"kind": "bpo_only", "price": 4e9, "listings": []}})
    r3 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P3, NAMES, pools)
    check("BPO-only does NOT force a buy", 101 in {r["type_id"] for r in r3["requirements"]})
    check("a durable original is not charged to one build", r3["metrics"]["blueprint_cost"] == 0)

    # The TARGET is what you asked to build — never flipped away, whatever its print costs.
    P4 = BuildParams(**base, bp_acquire={100: {"kind": "bpo_only", "price": 9e9, "listings": []}})
    r4 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P4, NAMES, pools)
    check("the target is still built", 100 in {r["type_id"] for r in r4["requirements"]})


def test_scan_lease_is_single_writer_across_replicas():
    """Prod runs several replicas, so the scan guard cannot live in process memory: every pod would
    start its own ~15k-request scan, and their progress writes would overwrite each other. The lease
    lives in the shared DB, and a crashed holder must not block the region forever."""
    print("test_scan_lease_is_single_writer_across_replicas")
    import time as _t
    import app.industry.bpc as B
    region = 99999998
    B.ensure_bpc_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_bpc_scan WHERE region_id=?", (region,))
    con.commit()
    con.close()

    winners = [n for n in ("pod-a", "pod-b", "pod-c") if B._claim(region, n)]
    check("exactly one replica wins the region", len(winners) == 1)
    owner = winners[0]
    check("the owner can renew", B._renew(region, owner) is True)
    check("a non-owner cannot renew", B._renew(region, "impostor") is False)
    check("a held region can't be re-claimed", B._claim(region, "late") is False)

    # Simulate the holder dying mid-scan: the lease expires and another replica takes over.
    con = get_connection()
    con.execute("UPDATE pp_bpc_scan SET lease_until=? WHERE region_id=?", (_t.time() - 1, region))
    con.commit()
    con.close()
    check("an expired lease is reclaimable", B._claim(region, "recovered") is True)

    B._release(region, "recovered", seen=7, indexed=3)
    con = get_connection()
    row = dict(con.execute("SELECT * FROM pp_bpc_scan WHERE region_id=?", (region,)).fetchone())
    con.execute("DELETE FROM pp_bpc_scan WHERE region_id=?", (region,))
    con.commit()
    con.close()
    check("release frees the lease", row["lease_until"] is None)
    check("release records completion", bool(row["ended_at"]) and row["indexed"] == 3)


def test_esi_budget_guard():
    """Every ESI call goes through one wrapper because CCP bans on the ERROR budget, not on volume.
    The guard must: pace when healthy, stop when the budget is nearly spent, and — the subtle one —
    leave the budget untouched when a response carries no headers, since defaulting to 'plenty'
    there would erase a real backoff."""
    print("test_esi_budget_guard")
    import time as _t
    from app import esi_http as E

    class Resp:
        def __init__(self, headers, code=200):
            self.headers = headers
            self.status_code = code

    E._record(Resp({"x-esi-error-limit-remain": "90", "x-esi-error-limit-reset": "60"}))
    check("healthy budget recorded", E.budget()["remain"] == 90)
    t0 = _t.time()
    E._pre_request_wait()
    check("healthy budget only paces", _t.time() - t0 < 0.5)

    E._record(Resp({"x-esi-error-limit-remain": "2", "x-esi-error-limit-reset": "3"}))
    check("low budget recorded", E.budget()["remain"] == 2)
    t0 = _t.time()
    E._pre_request_wait()
    check("low budget waits out the window", _t.time() - t0 >= 3)

    # The regression this caught: no headers used to write the default 100 and cancel the backoff.
    E._record(Resp({"x-esi-error-limit-remain": "4", "x-esi-error-limit-reset": "30"}))
    E._record(Resp({}))
    check("a header-less response does not reset the budget", E.budget()["remain"] == 4)
    E._record(Resp({"x-esi-error-limit-remain": "not-a-number"}))
    check("a malformed header does not reset the budget", E.budget()["remain"] == 4)

    check("requests are identified to CCP", "eve-pi-planner" in E.USER_AGENT)
    # Reset so a low budget doesn't stall the rest of the suite.
    E._record(Resp({"x-esi-error-limit-remain": "100", "x-esi-error-limit-reset": "0"}))


def test_job_runner_lease_and_toggle():
    """Background jobs: exactly one replica runs each (the scheduler starts in every pod), every run
    is recorded, a failure is captured rather than escaping, and a disabled job defers without
    running or taking the lease."""
    print("test_job_runner_lease_and_toggle")
    import app.jobs as J
    job = "unittest_job"
    con = get_connection()
    for t in ("pp_job_runs", "pp_job_leases", "pp_job_config"):
        try:
            con.execute(f"DELETE FROM {t} WHERE job=?", (job,))
        except Exception:
            pass
    con.commit()
    con.close()

    calls = []
    ok_fn = lambda: (calls.append(1), "did the thing")[1]

    check("a job runs and reports detail", J.run_job(job, ok_fn)["detail"] == "did the thing")
    check("the function actually ran", len(calls) == 1)

    # Two replicas racing: only one may run.
    held = J.claim(job, "other-pod")
    check("another replica can hold the lease", held is True)
    res = J.run_job(job, ok_fn)
    check("a held job is skipped", res["ran"] is False)
    check("the skipped job did NOT execute", len(calls) == 1)
    J.release(job, "other-pod")

    # A failure is recorded, not raised at the scheduler.
    res = J.run_job(job, lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
    check("a failing job returns instead of raising", res.get("error") == "kaboom")
    last = [r for r in J.recent_runs(20) if r["job"] == job][0]
    check("the failure is recorded", last["status"] == "error" and "kaboom" in (last["error"] or ""))

    # Disabled: defers entirely.
    J.set_enabled(job, False)
    check("disabled is reflected", J.is_enabled(job) is False)
    res = J.run_job(job, ok_fn)
    check("a disabled job defers", res["ran"] is False and res["reason"] == "disabled")
    check("a disabled job never executes", len(calls) == 1)
    check("a disabled job takes no lease", J.claim(job, "anyone") is True)
    J.release(job, "anyone")
    J.set_enabled(job, True)
    check("re-enabling restores it", J.is_enabled(job) is True)

    # Every known job is listed even before its first run — a job that stopped firing must be visible.
    names = {j["job"] for j in J.job_summary()}
    check("all known jobs are listed", all(n in names for n, _l, _c in J.KNOWN_JOBS))

    con = get_connection()
    for t in ("pp_job_runs", "pp_job_leases", "pp_job_config"):
        try:
            con.execute(f"DELETE FROM {t} WHERE job=?", (job,))
        except Exception:
            pass
    con.commit()
    con.close()


def test_run_now_trigger():
    """"Run now" without giving the web pods Kubernetes API access: the admin sets a flag, and a
    frequently-ticking CronJob picks it up. The tick must be a cheap no-op unless the job is
    genuinely due, and one request must produce exactly one run."""
    print("test_run_now_trigger")
    import app.jobs as J
    job = "unittest_due"
    interval = 22 * 3600
    con = get_connection()
    for t in ("pp_job_runs", "pp_job_config", "pp_job_leases"):
        try:
            con.execute(f"DELETE FROM {t} WHERE job=?", (job,))
        except Exception:
            pass
    con.commit()
    con.close()

    due, why = J.is_due(job, interval)
    check("a job that never ran is due", due is True and why == "never run")

    J.run_job(job, lambda: "ok")
    due, _ = J.is_due(job, interval)
    check("not due again inside the interval", due is False)

    J.request_run(job)
    due, why = J.is_due(job, interval)
    check("Run now makes it due", due is True and why == "requested")

    calls = []
    J.run_job(job, lambda: (calls.append(1), "ran")[1])
    check("the requested run executed", len(calls) == 1)
    due, _ = J.is_due(job, interval)
    check("the request is consumed after one run", due is False)

    # A disabled job must never be due, even if someone queued it earlier.
    J.request_run(job)
    J.set_enabled(job, False)
    due, why = J.is_due(job, interval)
    check("a disabled job is never due", due is False and why == "disabled")
    J.set_enabled(job, True)

    # An elapsed interval makes it due again without anyone asking.
    con = get_connection()
    con.execute("UPDATE pp_job_runs SET started_at=? WHERE job=?",
                (time.time() - interval - 60, job))
    con.execute("UPDATE pp_job_config SET run_requested=NULL WHERE job=?", (job,))
    con.commit()
    con.close()
    due, why = J.is_due(job, interval)
    check("an elapsed interval is due on its own", due is True and "last ran" in why)

    con = get_connection()
    for t in ("pp_job_runs", "pp_job_config", "pp_job_leases"):
        try:
            con.execute(f"DELETE FROM {t} WHERE job=?", (job,))
        except Exception:
            pass
    con.commit()
    con.close()


def test_cost_breakdown_adds_up():
    """Materials + job fees + blueprints must equal total_cost. The blueprint line was in the total
    before it was in the breakdown, so the displayed parts summed to less than the total — the kind
    of discrepancy that quietly destroys trust in every other number on the page."""
    print("test_cost_breakdown_adds_up")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                    marginal_pct_of_total=0.0,
                    # Cheap enough that the component is still worth building — a dear copy would
                    # (correctly) flip it to buy and there'd be no charge to check.
                    bp_acquire={101: {"kind": "bpc", "price": 500.0,
                                      "listings": [{"price": 500.0, "runs": 5}],
                                      "median_per_run": 100.0}})
    r = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    m = r["metrics"]
    check("a blueprint charge is present", m["blueprint_cost"] > 0)
    parts = m["materials_cost"] + m["job_cost"] + m["blueprint_cost"]
    check("the parts sum to the total", approx(parts, m["total_cost"]))
    check("net cost still credits leftovers",
          approx(m["net_cost"], m["total_cost"] - m["leftover_value"]))

    # With nothing to buy, the blueprint line is zero rather than absent — the UI hides it, but the
    # arithmetic must stay valid.
    P0 = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                     marginal_pct_of_total=0.0)
    r0 = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P0, NAMES, pools)
    m0 = r0["metrics"]
    check("no blueprints to buy -> zero", m0["blueprint_cost"] == 0)
    check("the parts still sum to the total",
          approx(m0["materials_cost"] + m0["job_cost"], m0["total_cost"]))


def test_blueprint_copies_cover_the_runs():
    """A copy carries a fixed number of runs and one contract is one item, so a batch bigger than a
    single copy means buying several. It must minimise TOTAL cost, not cost-per-run: needing one run,
    a per-run greedy takes a 300m 10-run copy over a 50m 1-run copy — 6x the price for runs you'll
    never use."""
    print("test_blueprint_copies_cover_the_runs")
    from app.industry.bpc import cost_for_runs
    info = {"listings": [{"price": 50e6, "runs": 1},
                         {"price": 300e6, "runs": 10},
                         {"price": 200e6, "runs": 5}],
            "median_per_run": 40e6}

    check("1 run buys the cheap 1-run copy", approx(cost_for_runs(info, 1)["cost"], 50e6))
    check("5 runs buys the 5-run copy", approx(cost_for_runs(info, 5)["cost"], 200e6))
    check("10 runs buys the 10-run copy", approx(cost_for_runs(info, 10)["cost"], 300e6))
    # 6 runs: the 5-run + 1-run pair (250m) beats the single 10-run copy (300m).
    six = cost_for_runs(info, 6)
    check("6 runs combines two cheap copies", approx(six["cost"], 250e6) and six["copies"] == 2)
    check("16 runs needs all three copies", cost_for_runs(info, 16)["copies"] == 3)

    # More runs than the market can supply: flagged, not silently under-priced.
    big = cost_for_runs(info, 40)
    check("an uncoverable batch is flagged", big["covered"] is False)
    check("the shortfall is reported", big["short_runs"] == 24)
    check("the shortfall is priced in", big["cost"] > 550e6)
    check("copies stay a sane number", big["copies"] < 10)

    # Degenerate inputs must not explode.
    check("zero runs costs nothing", cost_for_runs(info, 0)["cost"] == 0)
    none = cost_for_runs({"listings": [], "median_per_run": 0}, 5)
    check("no listings -> not covered", none["covered"] is False and none["short_runs"] == 5)
    check("a runs=0 listing is ignored",
          cost_for_runs({"listings": [{"price": 1e6, "runs": 0}], "median_per_run": 0}, 3)["covered"] is False)


def test_queue_plan_returns_trees():
    """The status view is the main screen and builds its pipeline/stages from the recipe tree — but
    plan_queue returns aggregated demand, which has no structure. Without a tree the status view
    showed no stages at all and lumped every job into one unlabelled bucket, while the preview modal
    (which calls a different endpoint) showed them correctly. The two must agree."""
    print("test_queue_plan_returns_trees")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                    marginal_pct_of_total=0.0)
    pools = {"manufacturing": 5, "reaction": 5}

    # plan_queue itself stays structure-free; the endpoint layer attaches the trees. Reproduce that
    # here so the contract the UI depends on is pinned.
    res = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    trees = [build_plan(t, q, mfg, rx, _prices(SELL), ADJ, P, NAMES)["tree"] for t, q in [(100, 2)]]
    check("a tree is produced per target", len(trees) == 1 and trees[0]["type_id"] == 100)
    check("the tree has structure", len(trees[0].get("inputs") or []) > 0)

    # Every type the plan says to BUILD must appear in the tree, or the pipeline would omit a stage.
    in_tree = set()
    def walk(n):
        in_tree.add(n["type_id"])
        for c in n.get("inputs") or []:
            walk(c)
    walk(trees[0])
    built = {r["type_id"] for r in res["requirements"]}
    check("every built type is reachable in the tree", built <= in_tree)

    # Several queued products -> one tree each, so a multi-product queue still renders stages.
    multi = [(100, 1), (101, 1)]
    trees2 = [build_plan(t, q, mfg, rx, _prices(SELL), ADJ, P, NAMES)["tree"] for t, q in multi]
    check("one tree per queued product", len(trees2) == 2)
    check("each tree is rooted at its own product",
          [t["type_id"] for t in trees2] == [100, 101])


def test_force_build_ignores_the_shortcuts():
    """"Build everything" drops both shortcuts that buy components: the saving threshold AND its
    absolute floor. The floor is why the slider alone can't express this — at 0% the 5m floor still
    buys every small component. It must NOT, however, build at a loss: ignoring marginal savings
    means small gains count, not that paying more to build makes sense."""
    print("test_force_build_ignores_the_shortcuts")
    from app.industry.graph import (resolve_build_params, MARGINAL_BUILD_PCT_OF_TOTAL,
                                    MIN_BUILD_SAVING_ISK)
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}

    # The knobs themselves.
    normal = resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0)
    check("normally the percentage applies",
          normal.marginal_pct_of_total == MARGINAL_BUILD_PCT_OF_TOTAL)
    check("normally the floor applies", normal.min_saving_isk == MIN_BUILD_SAVING_ISK)
    forced = resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0, force_build=True)
    check("force_build zeroes the percentage", forced.marginal_pct_of_total == 0.0)
    check("force_build zeroes the floor too", forced.min_saving_isk == 0.0)
    # The slider alone cannot: at 0% the floor still stands.
    slider0 = resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0, marginal_pct=0)
    check("the slider at 0 still keeps the floor", slider0.min_saving_isk == MIN_BUILD_SAVING_ISK)

    # A component whose saving is small: bought under the floor, built when forced.
    P_floor = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                          min_saving_isk=1e9, marginal_pct_of_total=0.0)
    r_floor = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P_floor, NAMES, pools)
    P_force = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                          min_saving_isk=0.0, marginal_pct_of_total=0.0)
    r_force = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P_force, NAMES, pools)
    check("a big floor buys the components",
          len(r_floor["requirements"]) < len(r_force["requirements"]))
    check("forcing builds more of them", len(r_force["requirements"]) >= 2)

    # Building at an outright loss is still refused: make the component dearer to build than to buy.
    dear = {**SELL, 101: 1.0}          # buying a Gadget is nearly free, so building it loses money
    r_loss = plan_queue([(100, 1)], mfg, rx, _prices(dear), ADJ, P_force, NAMES, pools)
    check("a loss-making component is still bought",
          101 not in {r["type_id"] for r in r_loss["requirements"]})


def test_marginal_sweep_covers_every_slider_stop():
    """The slider's live read-out is driven by a sweep of the whole curve. It must cover every stop,
    stay monotone-ish in the direction the knob means (higher % = buy more = fewer build steps, never
    more), and collapse stops that resolve to the same ISK threshold onto one identical plan — that
    dedup is what keeps the sweep to a handful of plans instead of one per stop."""
    print("test_marginal_sweep_covers_every_slider_stop")
    from app.industry.schedule import sweep_marginal, MARGINAL_SWEEP_PCTS
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                    marginal_pct_of_total=3.0)

    pts = sweep_marginal([(100, 40)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    check("one point per slider stop", [p["pct"] for p in pts] == MARGINAL_SWEEP_PCTS)
    check("every point carries cost and makespan",
          all(p["total_cost"] > 0 and p["makespan_hours"] is not None and "threshold" in p
              for p in pts))
    check("raising the threshold never adds build steps",
          all(a["build_steps"] >= b["build_steps"] for a, b in zip(pts, pts[1:])))
    check("the threshold rises with the percentage",
          all(a["threshold"] <= b["threshold"] for a, b in zip(pts, pts[1:])))
    # Same resolved threshold => the same plan, which is what lets the UI say "same plan as below".
    by_thr = {}
    for p in pts:
        by_thr.setdefault(p["threshold"], []).append(p)
    check("stops sharing a threshold share a plan",
          all(all(q["total_cost"] == g[0]["total_cost"] and q["build_steps"] == g[0]["build_steps"]
                  for q in g) for g in by_thr.values()))

    # The sweep must report the same numbers as a real plan run at that setting — the whole point is
    # that the live preview matches what you get when you let go of the slider.
    for pct in (0.0, 3.0, 10.0):
        P_at = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                           marginal_pct_of_total=pct)
        m = plan_queue([(100, 40)], mfg, rx, _prices(SELL), ADJ, P_at, NAMES, pools)["metrics"]
        pt = next(p for p in pts if p["pct"] == pct)
        check(f"sweep at {pct}% matches a real plan",
              pt["total_cost"] == m["total_cost"] and pt["makespan_hours"] == m["makespan_hours"])

    # The floor dominates on a small build, so every low stop is literally the same plan.
    P_floor = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=1e9,
                          marginal_pct_of_total=3.0)
    flat = sweep_marginal([(100, 1)], mfg, rx, _prices(SELL), ADJ, P_floor, NAMES, pools)
    check("a floored build gives one flat curve",
          len({p["threshold"] for p in flat}) == 1)


def test_marginal_saving_is_reported_and_overridable():
    """A "low saving" line has to say WHAT the saving was — a bare verdict asks the user to trust
    it — and the user must be able to overrule the shortcut for that one component. The override is
    per type_id and only defeats the shortcuts; it must not force a component the cost engine says
    is outright cheaper to buy, and it must not touch anything else in the plan."""
    print("test_marginal_saving_is_reported_and_overridable")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    # A floor big enough that both components are bought for "low saving".
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=1e9,
                    marginal_pct_of_total=0.0)
    r = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    shop = {s["type_id"]: s for s in r["shopping_list"]}
    check("the flipped component is on the shopping list", 101 in shop)
    check("it is flagged low-saving", shop[101]["bought_marginal"] is True)
    sv = shop[101]["marginal_saving"]
    check("and reports the ISK it would have saved", sv is not None and sv > 0)
    # The saving is (buy - build) × units: worth checking it's the real number, not a placeholder.
    from app.industry.graph import resolve_unit_costs
    memo, unit = resolve_unit_costs(mfg, rx, _prices(SELL), ADJ, P)
    unit(100, frozenset())
    node = memo[101]
    check("the reported saving is (buy − build) × qty",
          abs(sv - (node["buy_unit_cost"] - node["build_unit_cost"]) * shop[101]["qty"]) < 0.01)

    # Override just that one component.
    P_force = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=1e9,
                          marginal_pct_of_total=0.0, force_build_ids={101})
    r2 = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P_force, NAMES, pools)
    built = {q["type_id"] for q in r2["requirements"]}
    check("the overridden component is now built", 101 in built)
    check("it left the shopping list", 101 not in {s["type_id"] for s in r2["shopping_list"]})
    check("its own inputs are now bought instead", 201 in {s["type_id"] for s in r2["shopping_list"]})
    check("nothing else was forced along with it",
          built == {t["type_id"] for t in r["requirements"]} | {101})
    check("building it is cheaper, as the saving promised",
          r2["metrics"]["total_cost"] < r["metrics"]["total_cost"])

    # An override never buys a loss: the engine's own build/buy verdict is untouched by it.
    dear = {**SELL, 101: 1.0}          # buying a Gadget is nearly free → building it loses money
    r3 = plan_queue([(100, 1)], mfg, rx, _prices(dear), ADJ, P_force, NAMES, pools)
    check("a loss-making component is still bought",
          101 not in {q["type_id"] for q in r3["requirements"]})


def test_order_overrides_persist_and_apply_to_the_queue():
    """A "build it anyway" decided in the preview has to survive being queued, or the override is a
    lie the moment the user acts on it. Stored per order as a JSON id list, then UNIONED across the
    queue at plan time — the queue builds one shared batch per component, so an override can only be
    all-or-nothing for that component, and the union is what the user asked for."""
    print("test_order_overrides_persist_and_apply_to_the_queue")
    from app.industry.orders import _parse_ids
    check("an empty column parses to no overrides", _parse_ids("") == [])
    check("NULL parses to no overrides", _parse_ids(None) == [])
    check("garbage parses to no overrides, not a crash", _parse_ids("{nope") == [])
    check("a stored list round-trips", _parse_ids("[101, 102]") == [101, 102])

    # The union across queued orders is what reaches the planner: two orders, each overriding a
    # different component, must both be honoured in the one shared plan.
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    rows = [{"force_build_ids": "[101]"}, {"force_build_ids": "[102]"}, {"force_build_ids": ""}]
    forced = {t for r in rows for t in _parse_ids(r["force_build_ids"])}
    check("overrides union across the queue", forced == {101, 102})

    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=1e9,
                    marginal_pct_of_total=0.0, force_build_ids=forced)
    r = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    built = {q["type_id"] for q in r["requirements"]}
    check("both overridden components are built", {101, 102} <= built)


def test_customer_build_status_leaks_nothing():
    """The share payload is customer-facing and login-free, so what it does NOT contain is the
    feature. Assert the shape by construction: no ISK of any kind, no character/system/structure,
    and nothing about the builder's other orders."""
    print("test_customer_build_status_leaks_nothing")
    import inspect
    from app.industry import shares
    src = inspect.getsource(shares.build_status)
    banned = ["total_cost", "materials_cost", "job_cost", "net_cost", "shopping_list",
              "character_name", "character_id", "system", "isk", "price", "leftover"]
    for word in banned:
        check(f"the payload never mentions {word}", word not in src.split("payload = ")[1])
    # The stage walk must be rooted at the shared order's own product, never the queue's targets.
    stage_src = inspect.getsource(shares._stage_of_types)
    check("stages are derived from the shared product alone", "_depths([target_id]" in stage_src)
    # And the plan it measures against is this order's own, not the aggregated queue plan.
    plan_src = inspect.getsource(shares._order_plan)
    check("the order is planned on its own", "[(product_type_id, quantity)]" in plan_src)
    check("stock is not netted off the customer's view", "use_stock=False" in plan_src)


def test_blueprint_me_te_comes_from_the_copy_you_would_buy():
    """A print you don't own used to be costed at ME 0 / TE 0 — the un-researched worst case — even
    though the contract index already knows the research on every listed copy. The rule: the copy
    the plan would actually BUY (cheapest per run, ties to better research) sets the ME/TE, so price
    and efficiency describe the same purchase. Precedence is override > owned > contract > default."""
    print("test_blueprint_me_te_comes_from_the_copy_you_would_buy")
    from app.industry.bpc import representative_me_te

    check("nothing listed -> no opinion", representative_me_te({}) is None)
    check("a listing with no runs is not a copy you can buy",
          representative_me_te({"listings": [{"price": 1e6, "runs": 0, "me": 10, "te": 20}]}) is None)
    # Cheapest PER RUN wins, not cheapest outright: a 300m 100-run copy beats a 50m 1-run copy.
    listings = [{"price": 50e6, "runs": 1, "me": 0, "te": 0},
                {"price": 300e6, "runs": 100, "me": 10, "te": 20}]
    check("the copy the plan buys sets ME/TE", representative_me_te({"listings": listings}) == (10, 20))
    # Equal price per run -> take the researched one; you would.
    tie = [{"price": 100e6, "runs": 10, "me": 0, "te": 0},
           {"price": 100e6, "runs": 10, "me": 9, "te": 18}]
    check("ties break toward better research", representative_me_te({"listings": tie}) == (9, 18))

    # ME genuinely changes the plan: same build, researched print, fewer materials.
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    base = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                       marginal_pct_of_total=0.0)
    researched = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                             marginal_pct_of_total=0.0, me_by_product={100: (10, 20)},
                             me_source={100: "contract"})
    r0 = plan_queue([(100, 10)], mfg, rx, _prices(SELL), ADJ, base, NAMES, pools)
    r1 = plan_queue([(100, 10)], mfg, rx, _prices(SELL), ADJ, researched, NAMES, pools)
    check("a researched print costs less to build",
          r1["metrics"]["total_cost"] < r0["metrics"]["total_cost"])
    check("and finishes sooner", r1["metrics"]["makespan_hours"] < r0["metrics"]["makespan_hours"])

    # The plan reports what it assumed and where it came from — an invisible assumption is the bug.
    req = {q["type_id"]: q for q in r1["requirements"]}
    check("the step reports its ME", req[100]["me"] == 10)
    check("the step reports its TE", req[100]["te"] == 20)
    check("and says where that came from", req[100]["me_source"] == "contract")
    check("a reaction step is never given blueprint ME/TE",
          all(q["me_source"] == "reaction" for q in r1["requirements"] if q["activity"] == "reaction"))


def test_every_stage_gets_a_character_not_just_the_first():
    """The to-install checklist named a character for the jobs you can start NOW; every later stage
    was anonymous, so a plan said "stage 1: 12 jobs" without ever saying who installs them. Slots are
    interchangeable and the pools are the sum of the characters' own slots, so an aggregate-feasible
    schedule is always assignable — walk the waves, release a slot when its job ends, hand each job
    to whoever has the most capacity free."""
    print("test_every_stage_gets_a_character_not_just_the_first")
    from app.industry.schedule import assign_characters

    chars = [{"character_id": 1, "character_name": "Alpha", "manufacturing_slots": 2, "reaction_slots": 0},
             {"character_id": 2, "character_name": "Beta", "manufacturing_slots": 1, "reaction_slots": 2}]
    waves = [
        {"start_hours": 0.0, "tasks": [
            {"type_id": 10, "activity": "manufacturing", "runs": 1, "duration_hours": 5.0},
            {"type_id": 11, "activity": "manufacturing", "runs": 1, "duration_hours": 5.0},
            {"type_id": 12, "activity": "manufacturing", "runs": 1, "duration_hours": 5.0},
            {"type_id": 13, "activity": "reaction", "runs": 1, "duration_hours": 2.0}]},
        {"start_hours": 6.0, "tasks": [
            {"type_id": 20, "activity": "manufacturing", "runs": 1, "duration_hours": 3.0}]},
    ]
    assign_characters(waves, chars)
    first = waves[0]["tasks"]
    check("every job in the first wave has a character",
          all(t["character_id"] is not None for t in first))
    check("a later stage is assigned too", waves[1]["tasks"][0]["character_id"] is not None)
    check("names come along for the UI", waves[1]["tasks"][0]["character_name"] in ("Alpha", "Beta"))
    # 3 manufacturing jobs across 2+1 slots: nobody may be double-booked beyond their slot count.
    mfg_load = {}
    for t in first:
        if t["activity"] == "manufacturing":
            mfg_load[t["character_id"]] = mfg_load.get(t["character_id"], 0) + 1
    check("Alpha is not given more than her 2 slots", mfg_load.get(1, 0) <= 2)
    check("Beta is not given more than his 1 slot", mfg_load.get(2, 0) <= 1)
    check("the reaction job went to the only reaction pilot",
          [t for t in first if t["activity"] == "reaction"][0]["character_id"] == 2)

    # Capacity is RELEASED: the 5h jobs are done by hour 6, so the later job reuses a slot.
    check("a finished job frees its slot", waves[1]["tasks"][0]["character_id"] in (1, 2))

    # More jobs than slots at one instant → the extra is left unassigned rather than invented.
    tight = [{"start_hours": 0.0, "tasks": [
        {"type_id": i, "activity": "manufacturing", "runs": 1, "duration_hours": 9.0} for i in range(5)]}]
    assign_characters(tight, chars)
    assigned = [t for t in tight[0]["tasks"] if t["character_id"] is not None]
    check("assignment never exceeds real capacity", len(assigned) == 3)
    check("the overflow is honest about having no slot",
          all(t["character_name"] is None for t in tight[0]["tasks"] if t["character_id"] is None))

    # No character data at all → nothing invented, and no crash.
    plain = [{"start_hours": 0.0, "tasks": [{"type_id": 1, "activity": "manufacturing", "runs": 1,
                                             "duration_hours": 1.0}]}]
    assign_characters(plain, [])
    check("no characters means no assignment, not a guess",
          "character_id" not in plain[0]["tasks"][0])


def test_the_checklist_and_the_plan_agree_on_what_is_ready():
    """The "start now" checklist and the plan beside it must be the SAME plan. to-install used to
    run with default options while the screen used the user's real ones, so the checklist would name
    a job the plan scheduled last — "start the Revelation" while two stages of component jobs sat
    above it with nothing telling you to build them. Options change which jobs are ready, so both
    callers have to pass them."""
    print("test_the_checklist_and_the_plan_agree_on_what_is_ready")
    import inspect
    from app.industry import orders
    sig = inspect.signature(orders.to_install)
    check("to-install accepts build options", "req" in sig.parameters)
    check("and they are the queue's own option shape",
          sig.parameters["req"].annotation in (orders.QueuePlanRequest, "QueuePlanRequest | None",
                                               orders.QueuePlanRequest | None))
    src = inspect.getsource(orders.to_install)
    check("the caller's options reach the plan", "_run_queue_plan(ctx, req or" in src)

    # The invariant that makes this matter: options genuinely change which jobs are ready first.
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    bought = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=1e9,
                         marginal_pct_of_total=0.0)
    built = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                        marginal_pct_of_total=0.0)
    w_bought = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, bought, NAMES, pools)["schedule"]["waves"]
    w_built = plan_queue([(100, 1)], mfg, rx, _prices(SELL), ADJ, built, NAMES, pools)["schedule"]["waves"]
    ready_bought = {t["type_id"] for t in w_bought[0]["tasks"]}
    ready_built = {t["type_id"] for t in w_built[0]["tasks"]}
    check("buying the components makes the product itself the ready job", ready_bought == {100})
    check("building them makes a COMPONENT the ready job instead", 100 not in ready_built)
    check("so the two settings disagree about what to start", ready_bought != ready_built)


def main():
    test_material_formula()
    test_graph_loaders()
    test_build_all()
    test_make_or_buy_flips()
    test_root_forced_build()
    test_quantity_scales_and_excess()
    test_demand_aggregation_shares_batches()
    test_excess_ledger()
    test_on_hand_reduces_builds()
    test_job_split_respects_the_blueprint_cap()
    test_scheduler_linear_chain()
    test_scheduler_slot_contention()
    test_time_aware_make_or_buy()
    test_marginal_buy()
    test_structure_bonus()
    test_manufacturing_slots()
    test_per_product_me_from_blueprints()
    test_plan_queue_end_to_end()
    test_unpriced_material_does_not_crash()
    test_queue_progress_requirements()
    test_queue_plan_returns_trees()
    test_progress_rollup()
    test_stock_reduces_plan_but_never_the_target()
    test_marginal_threshold_scales_with_build_size()
    test_force_build_ignores_the_shortcuts()
    test_marginal_sweep_covers_every_slider_stop()
    test_marginal_saving_is_reported_and_overridable()
    test_order_overrides_persist_and_apply_to_the_queue()
    test_customer_build_status_leaks_nothing()
    test_blueprint_me_te_comes_from_the_copy_you_would_buy()
    test_every_stage_gets_a_character_not_just_the_first()
    test_the_checklist_and_the_plan_agree_on_what_is_ready()
    test_install_assignment_spreads_and_respects_free_slots()
    test_fifo_wins_contested_slots()
    test_missing_blueprints_are_reported()
    test_bpc_price_summary()
    test_blueprint_cost_affects_make_or_buy()
    test_cost_breakdown_adds_up()
    test_blueprint_copies_cover_the_runs()
    test_scan_lease_is_single_writer_across_replicas()
    test_esi_budget_guard()
    test_job_runner_lease_and_toggle()
    test_run_now_trigger()
    print(f"\nAll {_passed} checks passed.")




def test_structure_bonus():
    print("test_structure_bonus (hull + rigs + security → ME/TE)")
    from app.industry.structures import manufacturing_bonus, reaction_bonus, _sec_band
    check("sec band high", _sec_band(0.9) == "high")
    check("sec band low", _sec_band(0.3) == "low")
    check("sec band null", _sec_band(0.0) == "null")
    # Raitaru (1% role ME) + T2 ME rig (2.4%) in hi-sec ×1.0 → 1 + 2.4 = 3.4% ME; no TE rig → 0
    check("raitaru T2 ME hi", manufacturing_bonus("raitaru", 2, 0, "high") == (3.4, 0.0))
    # Sotiyo + T2 ME rig in null ×2.1 → 1 + 2.4×2.1 = 6.04% ME
    check("sotiyo T2 ME null", manufacturing_bonus("sotiyo", 2, 0, "null") == (6.04, 0.0))
    # T2 TE rig in null → 24×2.1 = 50.4% TE
    check("azbel T2 TE null", manufacturing_bonus("azbel", 0, 2, "null") == (1.0, 50.4))
    # No structure → nothing
    check("no hull no rig", manufacturing_bonus(None, 0, 0, "high") == (0.0, 0.0))
    # Reaction: Tatara + T2 rig low ×1.9 → 2.4×1.9 = 4.56% ME
    check("tatara T2 ME low", reaction_bonus("tatara", 2, 0, "low") == (4.56, 0.0))


if __name__ == "__main__":
    main()
