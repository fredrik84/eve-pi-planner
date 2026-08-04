"""In-process tests for the Industry make-or-buy engine (app/industry/graph.py).

Runs against a synthetic, hand-computable recipe graph seeded into an in-memory SQLite DB — no
live SDE or ESI needed, same style as test_optimizer's in-process cases. Asserts durable
invariants of the cost math: the EVE material formula, the build-vs-buy decision, shopping-list
aggregation, and the cost/time totals.

Run: python3 test_industry.py
"""
import json
import math
import sqlite3
import time
from app.db import get_connection
import sys

sys.path.insert(0, ".")

from app.industry.graph import (
    BuildParams, blend_me_te, effective_material_qty, load_manufacturing_graph,
    load_reaction_graph, collect_reachable, build_plan, resolve_unit_costs,
)
from app.industry.schedule import (order_ranks, 
    aggregate_demand, build_tasks, schedule, plan_queue, Task, _built_deps, _depths,
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
    # The recipe graphs are cached per process now, so a test seeding its own synthetic SDE must
    # drop that cache or it silently plans against whatever the previous test loaded.
    from app.industry.graph import clear_graph_cache
    clear_graph_cache()
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


class _KeepOpen:
    """A sqlite connection whose close() does nothing.

    The code under test opens and closes a connection per call, which is right against a pool and
    fatal against `:memory:` — the database only exists while a connection to it does. Everything
    else passes straight through.
    """

    def __init__(self, con):
        self._con = con

    def __getattr__(self, name):
        return getattr(self._con, name)

    def close(self):
        pass


def _patch_db(module):
    """Point one module's `get_connection` at a private in-memory DB. Returns (con, restore)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    keeper = _KeepOpen(con)
    real = module.get_connection
    module.get_connection = lambda: keeper

    def restore():
        module.get_connection = real
        con.close()

    return con, restore


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
        PR._queue_snapshot = lambda ctx, res=None: (orders, plan)   # res: a caller-supplied plan
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

        PR._queue_snapshot = lambda ctx, res=None: (None, None)
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
    # An UN-RESEARCHED original, deliberately: research legitimately moves materials (that is what
    # ME is), so the only way to ask whether *ownership* moves them is to hold ME/TE at 0.
    P2 = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                     owned={100: {"me": 0, "te": 0, "kind": "bpo", "runs": -1}})
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
    # The customer gets the PRICE — that's what they're paying — and nothing about what it cost to
    # make it or what the builder is making on it.
    banned = ["total_cost", "materials_cost", "job_cost", "net_cost", "shopping_list", "margin",
              "character_name", "character_id", "system", "leftover"]
    payload_src = src.split("payload = ")[1]
    for word in banned:
        check(f"the payload never mentions {word}", word not in payload_src)
    check("but it does carry the quoted price", '"price"' in payload_src)
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

    # A manufacturing job may NEVER land on a reaction-only pilot, even when they are the only
    # character with free capacity anywhere — the pools are separate skills and separate structures,
    # and an instruction to build on a toon with no industry slot is one you cannot carry out.
    rx_only = [{"character_id": 7, "character_name": "RxOnly",
                "manufacturing_slots": 0, "reaction_slots": 5}]
    mixed = [{"start_hours": 0.0, "tasks": [
        {"type_id": 1, "activity": "manufacturing", "runs": 1, "duration_hours": 2.0},
        {"type_id": 2, "activity": "reaction", "runs": 1, "duration_hours": 2.0}]}]
    assign_characters(mixed, rx_only)
    check("a reaction pilot is never given manufacturing work",
          mixed[0]["tasks"][0]["character_id"] is None)
    check("but still takes the reaction work", mixed[0]["tasks"][1]["character_id"] == 7)
    # And the reverse: reaction slots do not pad out someone's manufacturing capacity.
    both = [{"character_id": 8, "character_name": "Both",
             "manufacturing_slots": 1, "reaction_slots": 3}]
    three = [{"start_hours": 0.0, "tasks": [
        {"type_id": i, "activity": "manufacturing", "runs": 1, "duration_hours": 9.0}
        for i in range(3)]}]
    assign_characters(three, both)
    check("spare reaction slots never absorb manufacturing jobs",
          sum(1 for t in three[0]["tasks"] if t["character_id"] is not None) == 1)

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


def test_saved_build_options_reach_plans_run_without_a_browser():
    """Build options shape every number, and they used to live only in the browser. A plan run on the
    user's behalf — a customer's share link, the start-now checklist — therefore ran with library
    defaults: no facility time bonus, a 3% threshold that buys components the user builds. That's how
    a share link came to quote 14d 4h against an 8d 8h plan. Stored per account and applied in
    prepare_plan_inputs, which every plan path goes through; an explicitly-sent field still wins."""
    print("test_saved_build_options_reach_plans_run_without_a_browser")
    import inspect
    from app.industry.graph import BuildOptions, prepare_plan_inputs
    from app.industry import settings as ind_settings

    check("the saved options are applied where every plan resolves its inputs",
          "apply_account_build_options" in inspect.getsource(prepare_plan_inputs))

    # An untouched request takes the account's values...
    saved = {"struct_time_pct": 44.0, "struct_material_pct": 6.0, "marginal_pct": 0.5,
             "prioritize_speed": 0, "force_build": 1}
    real_get = ind_settings.get_settings
    ind_settings.get_settings = lambda ctx: dict(saved)          # no DB needed for the merge rule
    try:
        merged = ind_settings.apply_account_build_options(1, BuildOptions())
        check("a bare request inherits the facility bonus", merged.struct_time_pct == 44.0)
        check("and the saving threshold", merged.marginal_pct == 0.5)
        check("and the speed shortcut", merged.prioritize_speed is False)
        check("and build-everything", merged.force_build is True)

        # ...but anything the caller set explicitly is untouched, or the live UI could never tweak a knob
        # without saving it first. A value EQUAL to the pydantic default must still count as explicit.
        explicit = ind_settings.apply_account_build_options(
            1, BuildOptions(marginal_pct=8.0, prioritize_speed=True, force_build=False))
        check("an explicit threshold wins", explicit.marginal_pct == 8.0)
        check("an explicit speed choice wins", explicit.prioritize_speed is True)
        check("an explicit force_build wins even at the default value", explicit.force_build is False)
        check("unset fields still come from the account", explicit.struct_time_pct == 44.0)

        # A row that was never written must read as "no opinion", not as a row of zeros — zeros
        # would look like a deliberate "no facility bonus, threshold 0" and be applied as such.
        ind_settings.get_settings = lambda ctx: {}
        untouched = ind_settings.apply_account_build_options(1, BuildOptions())
        check("no saved row leaves the request exactly as it was",
              untouched.struct_time_pct == 0.0 and untouched.marginal_pct is None)
        ind_settings.get_settings = lambda ctx: dict(saved)

        # The share link plans with a bare BuildOptions apart from its own three fields, so it picks the
        # account's options up the same way — that's the fix for the ETA mismatch.
        from app.industry.shares import _order_plan
        src = inspect.getsource(_order_plan)
        check("the share sets only what is specific to the order",
              "use_stock=False" in src and "force_build_ids=force_ids" in src
              and "struct_time_pct" not in src)

    finally:
        ind_settings.get_settings = real_get


def test_price_is_net_cost_plus_margin():
    """The quote. Priced off NET cost, not total spend: a build that over-produces reusable
    intermediates keeps them, and they're already credited out of net cost — charging the customer
    for them would bill the same materials twice. The margin is the one number the tool can't work
    out for anyone, so it's a knob (default 10%)."""
    print("test_price_is_net_cost_plus_margin")
    from app.industry.graph import resolve_build_params, MARGIN_DEFAULT_PCT
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}

    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                    marginal_pct_of_total=0.0, margin_pct=10.0)
    r = plan_queue([(100, 7)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    m = r["metrics"]
    net = m["total_cost"] - m["leftover_value"]
    check("the price is net cost plus the margin", abs(m["price"] - net * 1.10) < 0.01)
    check("and the margin is reported with it", m["margin_pct"] == 10.0)
    check("leftovers are NOT charged to the customer", m["price"] < m["total_cost"] * 1.10
          if m["leftover_value"] > 0 else True)

    # Zero margin quotes cost — a legitimate setting (building for a corpmate at cost).
    P0 = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                     marginal_pct_of_total=0.0, margin_pct=0.0)
    r0 = plan_queue([(100, 7)], mfg, rx, _prices(SELL), ADJ, P0, NAMES, pools)
    check("0% margin quotes exactly net cost",
          abs(r0["metrics"]["price"] - (r0["metrics"]["total_cost"] - r0["metrics"]["leftover_value"])) < 0.01)

    # The default, and the clamp: nobody quotes -20%, and a fat-fingered 900% is not a price.
    check("the default margin is 10%",
          resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0).margin_pct == MARGIN_DEFAULT_PCT)
    check("a negative margin is clamped away",
          resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0, margin_pct=-5).margin_pct == 0.0)
    check("an absurd margin is capped",
          resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0, margin_pct=900).margin_pct == 100.0)
    check("an explicit 0 is honoured, not treated as unset",
          resolve_build_params(0, 0, 0, None, None, 0.0, 0, 0, margin_pct=0).margin_pct == 0.0)


def test_queue_price_uses_each_orders_own_margin():
    """The builder's own sheet must quote what the customers are quoted. Margin is snapshotted per
    order, but the queue was marked up at ONE blanket rate — so editing a customer's margin moved
    nothing on the Your Build sheet while the share link that customer holds already used the new
    number. Cost is a shared-batch total with no per-order split, so each order's share is
    apportioned by its standalone cost (unit_cost x quantity)."""
    print("test_queue_price_uses_each_orders_own_margin")
    from app.industry.orders import _blend_margin

    def res(net, targets):
        return {"metrics": {"net_cost": net, "total_cost": net}, "targets": targets}

    T2 = [{"type_id": 100, "quantity": 2, "unit_cost": 300.0},
          {"type_id": 200, "quantity": 1, "unit_cost": 400.0}]

    # One margin across the queue → identical to the old single-rate formula.
    r = res(1000.0, T2)
    _blend_margin(r, [(100, 2, 20.0), (200, 1, 20.0)], 10.0)
    check("a single margin prices exactly as before", abs(r["metrics"]["price"] - 1200.0) < 0.01)
    check("and is not flagged mixed", r["metrics"]["margin_mixed"] is False)
    check("the reported rate is that margin", r["metrics"]["margin_pct"] == 20.0)

    # Two margins → cost-weighted blend. 600/1000 at 50%, 400/1000 at 0%.
    r = res(1000.0, T2)
    _blend_margin(r, [(100, 2, 50.0), (200, 1, 0.0)], 10.0)
    check("mixed margins blend by each order's share of cost",
          abs(r["metrics"]["price"] - (600 * 1.5 + 400 * 1.0)) < 0.01)
    check("mixed margins are flagged", r["metrics"]["margin_mixed"] is True)
    check("the reported rate explains the price",
          abs(r["metrics"]["margin_pct"] - 30.0) < 0.01)

    # An order with no margin of its own falls back to the account default, not to zero.
    r = res(1000.0, T2)
    _blend_margin(r, [(100, 2, None), (200, 1, None)], 10.0)
    check("a null order margin uses the account default", abs(r["metrics"]["price"] - 1100.0) < 0.01)

    # Changing one order's margin must move the price — the actual bug reported.
    a = res(1000.0, T2); _blend_margin(a, [(100, 2, 10.0), (200, 1, 10.0)], 10.0)
    b = res(1000.0, T2); _blend_margin(b, [(100, 2, 40.0), (200, 1, 10.0)], 10.0)
    check("raising one order's margin raises the queue price",
          b["metrics"]["price"] > a["metrics"]["price"])

    # No cost basis (unpriced targets) must not divide by zero — fall back to an even split.
    r = res(1000.0, [{"type_id": 100, "quantity": 2, "unit_cost": 0.0}])
    _blend_margin(r, [(100, 2, 20.0)], 10.0)
    check("a zero cost basis still prices without dividing by zero",
          abs(r["metrics"]["price"] - 1200.0) < 0.01)

    # And the end-to-end shape: the real engine must emit the unit_cost the blend needs.
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, min_saving_isk=0.0,
                    marginal_pct_of_total=0.0, margin_pct=10.0)
    q = plan_queue([(100, 7)], mfg, rx, _prices(SELL), ADJ, P, NAMES,
                   {"manufacturing": 5, "reaction": 5})
    check("plan_queue exposes a per-target unit cost to apportion by",
          all(t.get("unit_cost") is not None for t in q["targets"]))
    check("and that unit cost is a number, not the resolver's node dict",
          all(isinstance(t["unit_cost"], (int, float)) for t in q["targets"]))


def test_share_links_outlive_their_order():
    """A link handed to a customer must not break when the build finishes and leaves the queue —
    "404" is the worst possible answer to "did my ship get built?". Every successful render is
    snapshotted onto the share row, and a missing order serves that snapshot, flagged archived. Only
    an unknown or revoked id is a real 404."""
    print("test_share_links_outlive_their_order")
    import inspect
    from app.industry import shares
    src = inspect.getsource(shares.build_status)
    check("the render is snapshotted onto the share",
          "UPDATE pp_industry_shares SET last_payload" in src)
    check("a missing order falls back to the snapshot", 'if snapshot:' in src)
    check("and says so rather than pretending it is live", '"archived"' in src)
    check("a revoked link is still a 404", "revoked=0" in src)
    # The snapshot must be taken AFTER the payload is assembled, or it would store a half-built dict.
    check("the snapshot is of the finished payload",
          src.index("payload = {") < src.index("UPDATE pp_industry_shares SET last_payload"))
    # The table has to carry the columns, and additively — this shipped after the table did.
    # Checked via the shared add_columns() migration helper (app/db.py), which is what every
    # additive migration goes through now; the assertion is on the mechanism + the column name,
    # not on a raw "ALTER TABLE ... ADD COLUMN" spelling that a refactor can legitimately change.
    ddl = inspect.getsource(shares.ensure_industry_shares_table)
    check("the snapshot columns are added additively",
          "add_columns(" in ddl and "last_payload" in ddl)


# ── Rigs are not generic: per-job structure routing ───────────────────────────────────────────
# A Standup M-Set rig covers ONE family of products. A builder runs capital parts in one structure
# and the hull in another, so the planner has to resolve the bonus per JOB — and every number
# downstream (materials, cost, job fee, duration, schedule) has to read the SAME decision.

def _site(key, name, me, te, ci=0.0, tax=0.0, system_id=None):
    """A routed site, shaped exactly as structures.route_job returns one."""
    return {"key": key, "name": name, "system_id": system_id, "me_pct": me, "te_pct": te,
            "material_mult": 1.0 - me / 100.0, "time_mult": 1.0 - te / 100.0,
            "cost_index": ci, "tax_pct": tax}


def test_a_rig_only_applies_to_what_it_is_for():
    """The bug this whole feature exists for: a capital-rigged structure was quoting its ME on
    every job in the plan. A rig contributes only to products in its own families; the hull ROLE
    bonus is structure-wide and always applies."""
    print("test_a_rig_only_applies_to_what_it_is_for")
    from app.industry.structures import covers, manufacturing_bonus, RIG_FAMILIES
    # 873 = Capital Construction Components, 27 = Battleship, 85 = a charge group.
    check("capital component rig covers capital components", covers(["capital_component"], 873))
    check("capital component rig does NOT cover a battleship", not covers(["capital_component"], 27))
    check("capital ship rig covers a dreadnought (group 485)", covers(["capital_ship"], 485))
    check("capital ship rig does not cover a cruiser (group 26)", not covers(["capital_ship"], 26))
    check("every family key maps to a non-empty group set",
          all(f["groups"] for f in RIG_FAMILIES.values()))
    # Raitaru + T2 ME rig in null: 1% role + 2.4×2.1 = 6.04 when it applies, role alone when not.
    check("covered job gets role + rig", manufacturing_bonus("raitaru", 2, 0, "null") == (6.04, 0.0))
    check("uncovered job keeps only the hull role bonus",
          manufacturing_bonus("raitaru", 2, 0, "null", me_applies=False) == (1.0, 0.0))
    check("uncovered TE job keeps only the role TE",
          manufacturing_bonus("azbel", 0, 2, "null", te_applies=False) == (1.0, 0.0))


def test_a_structure_with_no_families_still_covers_everything():
    """The compatibility promise, asserted rather than trusted: a structure that has rig tiers and
    has never been told what they are for keeps behaving exactly as it does today. Silently cutting
    everyone's quoted efficiency on deploy is the one outcome this feature must not have."""
    print("test_a_structure_with_no_families_still_covers_everything")
    from app.industry.structures import covers, BuildSite
    check("no families = every group", covers([], 873) and covers(None, 27) and covers((), 999999))
    site = BuildSite(key="s:1", name="Old", activity="manufacturing", hull="raitaru",
                     security="null", me_rig=2, te_rig=2)
    for group in (873, 27, 85, None):
        check(f"un-narrowed rig applies to group {group}", site.bonus_for(group) == (6.04, 50.4))


def test_the_planner_picks_the_structure_that_covers_the_job():
    """No knob for this (CLAUDE.md rule 3): the user says what their structures are rigged for and
    the math routes each job. Best ME wins, then best TE, then staying where the consumer is."""
    print("test_the_planner_picks_the_structure_that_covers_the_job")
    from app.industry.structures import BuildSite, route_job
    caps = BuildSite(key="s:1", name="Capital yard", activity="manufacturing", hull="sotiyo",
                     security="null", me_rig=2, te_rig=2,
                     me_families=("capital_component",), te_families=("capital_component",))
    ammo = BuildSite(key="s:2", name="Ammo shop", activity="manufacturing", hull="raitaru",
                     security="null", me_rig=2, te_rig=2,
                     me_families=("ammunition",), te_families=("ammunition",))
    sites = [caps, ammo]
    check("a capital component goes to the capital yard",
          route_job(sites, 873)["key"] == "s:1")
    check("a charge goes to the ammo shop", route_job(sites, 85)["key"] == "s:2")
    # Neither covers a battleship, so both offer the same 1% role ME — the tie must not shuffle
    # the plan around: it stays where its consumer is being built.
    check("a tie stays where the consumer is",
          route_job(sites, 27, prefer="s:2")["key"] == "s:2")
    check("a tie with no incumbent is still deterministic",
          route_job(sites, 27)["key"] in ("s:1", "s:2"))
    # Same rigs, different systems → the cheaper job fee breaks the tie.
    cheap = BuildSite(key="s:3", name="Cheap", activity="manufacturing", hull="raitaru",
                      security="null", me_rig=2, te_rig=2, cost_index=0.01)
    dear = BuildSite(key="s:4", name="Dear", activity="manufacturing", hull="raitaru",
                     security="null", me_rig=2, te_rig=2, cost_index=0.09)
    check("equal rigs → the cheaper system", route_job([dear, cheap], 27)["key"] == "s:3")


def test_cost_materials_time_and_schedule_all_use_the_same_site():
    """The half-threaded version is worse than not doing it at all: costing materials off a rig the
    scheduler knows nothing about produces a plan whose ISK and whose ETA describe two different
    factories. One decision, read through BuildParams, by every consumer."""
    print("test_cost_materials_time_and_schedule_all_use_the_same_site")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = _prices(SELL)
    # Gadget (101) is built in a structure that halves materials AND time; Widget (100) in one that
    # does neither. Nothing else differs.
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                         marginal_pct_of_total=0.0, min_saving_isk=0.0)
    params.job_sites = {100: _site("s:1", "Hull yard", 0, 0),
                        101: _site("s:2", "Parts yard", 50, 50),
                        102: _site("s:2", "Parts yard", 0, 0)}

    plan = build_plan(100, 1, mfg, rx, P, ADJ, params, NAMES)
    gadget = next(n for n in plan["tree"]["inputs"] if n["type_id"] == 101)
    minb = next(n for n in gadget["inputs"] if n["type_id"] == 201)
    mina = next(n for n in plan["tree"]["inputs"] if n["type_id"] == 200)
    # Gadget needs 5 MineralB/run at ME 0; the parts yard's 50% material bonus halves it, and the
    # 2 Gadgets the Widget needs are two runs → 5 (not 10). Widget's own 10 MineralA is untouched.
    check("materials use the site the JOB is built in", minb["qty"] == 5)
    check("a job in the un-rigged structure keeps full materials", mina["qty"] == 10)
    check("each build step names where it was costed",
          gadget["site"] == "Parts yard" and plan["tree"]["site"] == "Hull yard")

    # Same params through the scheduler: gadget's 1800s base halves, widget's 3600s does not.
    memo, unit = resolve_unit_costs(mfg, rx, P, ADJ, params)
    unit(100, frozenset())
    agg = aggregate_demand([(100, 1)], memo, mfg, rx, params, {}, {"manufacturing": 5, "reaction": 5})
    tasks, by_type = build_tasks(agg, mfg, rx, params, {"manufacturing": 5, "reaction": 5})
    dur = {t.type_id: t.duration for t in tasks}
    check("the schedule uses the same time bonus as the cost", dur.get(101) == 900.0)
    check("and leaves the un-rigged job alone", dur.get(100) == 3600.0)
    # And the demand pass agrees with the tree: 2 gadget runs × 5 MineralB × 0.5.
    check("aggregated demand halves the same material", agg[201]["gross"] == 5)


def test_the_job_fee_follows_the_system_the_job_lands_in():
    """Job installation cost is EIV × (system cost index + facility tax + SCC), and the index is
    per SYSTEM. Route a job to another structure and charge it the first system's index and the
    plan's ISK is describing a build that never happens."""
    print("test_the_job_fee_follows_the_system_the_job_lands_in")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = _prices(SELL)
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                         mfg_cost_index=0.10, facility_tax_pct=5.0)
    check("unrouted: the account's index and tax",
          abs(params.job_fee_rate(100, "manufacturing") - (0.10 + 0.05 + 0.04)) < 1e-9)
    params.job_sites = {100: _site("s:1", "Elsewhere", 0, 0, ci=0.02, tax=1.0)}
    check("routed: that structure's own system index and tax",
          abs(params.job_fee_rate(100, "manufacturing") - (0.02 + 0.01 + 0.04)) < 1e-9)
    check("a type with no site still falls back to the account's",
          abs(params.job_fee_rate(101, "manufacturing") - (0.10 + 0.05 + 0.04)) < 1e-9)
    # And the fee actually lands in the plan's job cost: Widget's EIV is 2×1000 + 10×100 = 3000.
    plan = build_plan(100, 1, mfg, rx, P, ADJ, params, NAMES)
    check("the tree's job cost is charged at the routed rate",
          abs(plan["tree"]["job_cost"] - 3000 * 0.07) < 1e-6)


def test_an_unrouted_plan_is_byte_for_byte_the_old_plan():
    """The deploy guarantee: an account with one structure (or none) plans exactly as it does
    today. Routing is opt-in per structure and the flag is off by default; with no job_sites every
    number has to come out of the same code path unchanged."""
    print("test_an_unrouted_plan_is_byte_for_byte_the_old_plan")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = _prices(SELL)
    flat = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0,
                       struct_material_mult=0.9, struct_time_mult=0.8,
                       mfg_cost_index=0.05, facility_tax_pct=2.0)
    check("materials fall back to the flat facility",
          flat.struct_mults_for(100, "manufacturing") == (0.9, 0.8))
    check("reactions keep their own material multiplier and no time bonus",
          flat.struct_mults_for(102, "reaction") == (flat.reaction_material_mult, 1.0))
    a = plan_queue([(100, 3)], mfg, rx, P, ADJ, flat, NAMES, {"manufacturing": 5, "reaction": 5})
    check("nothing new is reported for a single-facility plan",
          a["build_sites"] == [] and a["moves"] == [])
    check("the plan still costs and schedules", a["metrics"]["total_cost"] > 0
          and a["metrics"]["makespan_hours"] > 0)


def test_every_station_change_is_reported():
    """Routing does NOT price freight — so it owes the builder the list of what has to move. A plan
    that quietly spreads a capital build over three structures and never says so is one you find
    out about while holding the parts."""
    print("test_every_station_change_is_reported")
    from app.industry.routing import plan_moves
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = _prices(SELL)
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                         marginal_pct_of_total=0.0, min_saving_isk=0.0)
    params.job_sites = {100: _site("s:1", "Hull yard", 0, 0),
                        101: _site("s:2", "Parts yard", 40, 0),
                        102: _site("s:2", "Parts yard", 0, 0)}
    res = plan_queue([(100, 4)], mfg, rx, P, ADJ, params, NAMES,
                     {"manufacturing": 5, "reaction": 5})
    names = {m["name"] for m in res["moves"]}
    check("the component built elsewhere is listed as a move", "Gadget" in names)
    check("a component built where it is consumed is not", "Sprocket" not in names)
    move = next(m for m in res["moves"] if m["name"] == "Gadget")
    check("the move says where from and where to",
          move["from"] == "Parts yard" and move["to"] == "Hull yard" and move["units"] > 0)
    check("the plan says which structures it used",
          {s["name"] for s in res["build_sites"]} == {"Hull yard", "Parts yard"})
    check("build steps are attributed to their structure",
          all(r["site"] for r in res["requirements"]))
    check("every scheduled job names its structure",
          all(t.get("site") for w in res["schedule"]["waves"] for t in w["tasks"]))
    check("no moves when everything is built in one place",
          plan_moves({100: {"build": True, "runs": 1}}, mfg, rx, BuildParams(), NAMES) == [])


def test_the_rig_family_registry_is_the_single_source():
    """The families are a stored KEY, so the registry is a migration surface: the frontend reads it
    from the backend, and a key that vanishes silently un-narrows somebody's structure."""
    print("test_the_rig_family_registry_is_the_single_source")
    import inspect
    from app.industry.structures import family_registry, RIG_FAMILIES
    fams = family_registry()
    check("the registry is served whole", len(fams) == len(RIG_FAMILIES))
    check("both activities are represented",
          {f["activity"] for f in fams} == {"manufacturing", "reaction"})
    check("every entry has a key and a human label",
          all(f["key"] and f["label"] for f in fams))
    check("manufacturing families are the ones the M-Set line has",
          {"capital_ship", "capital_component", "advanced_component", "ammunition",
           "equipment", "drone", "structure"} <= set(RIG_FAMILIES))
    # Additive migration only — the columns are added to the existing pp_markets table.
    from app import markets
    ddl = inspect.getsource(markets.ensure_markets_table)
    check("the rig-family columns are added additively",
          "add_columns(" in ddl and "me_rig_groups TEXT" in ddl
          and "DROP COLUMN" not in ddl.upper() and "DROP TABLE" not in ddl.upper())
    check("the structure's own system and tax are stored too",
          "system_id BIGINT" in ddl and "facility_tax_pct REAL" in ddl)
    # The whole feature is gated (CLAUDE.md rule 2). Assert the flag EXISTS and is registered, not
    # what state it is in — an admin can flip that.
    from app.features import FEATURE_REGISTRY
    check("the feature is in the registry",
          any(f["key"] == "industry_rig_routing" for f in FEATURE_REGISTRY))
def _patch_db_all(*modules):
    """One in-memory DB shared by several modules. Each imports `get_connection` into its own
    namespace, so patching one leaves the others talking to the real database — which is how a
    multi-module flow (assets + orders + sourcing + settings) silently half-tests itself."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    keeper = _KeepOpen(con)
    saved = [(m, m.get_connection) for m in modules]
    for m in modules:
        m.get_connection = lambda _k=keeper: _k

    def restore():
        for m, real in saved:
            m.get_connection = real
        con.close()

    return con, restore


def test_a_container_carries_where_it_is():
    """A container source used to say what it was called and which hangar it hung off, and nothing
    about WHERE — ambiguous exactly when it matters, with cans in several stations. The walk that
    already finds the hangar also finds the root asset, whose `location_id` IS the station or
    structure."""
    print("test_a_container_carries_where_it_is")
    from app.industry.assets import _split_by_source, _USABLE_FLAGS, _CORP_FLAGS, _place_label

    # item 5 (a can) sits in a station; item 6 is a can INSIDE that can, two links from the root.
    rows = [
        {"item_id": 5, "type_id": 0, "quantity": 1, "location_id": 60003760, "location_flag": "Hangar"},
        {"item_id": 6, "type_id": 0, "quantity": 1, "location_id": 5, "location_flag": "Unlocked"},
        {"item_id": 7, "type_id": 200, "quantity": 100, "location_id": 5, "location_flag": "Unlocked"},
        {"item_id": 8, "type_id": 201, "quantity": 50, "location_id": 6, "location_flag": "Unlocked"},
        # A second can in a DIFFERENT structure — the whole point of the feature.
        {"item_id": 9, "type_id": 0, "quantity": 1, "location_id": 1035466617946, "location_flag": "Hangar"},
        {"item_id": 10, "type_id": 202, "quantity": 7, "location_id": 9, "location_flag": "Unlocked"},
    ]
    srcs, stock, conts = _split_by_source(
        rows, _USABLE_FLAGS, lambda _f: "char:1", lambda _f: "Toon — personal hangar")

    check("a container knows the station it is in", srcs["cont:5"]["location_id"] == 60003760)
    check("a nested container reports the ROOT location, not its parent can",
          srcs["cont:6"]["location_id"] == 60003760)
    check("a container in a structure carries the structure id",
          srcs["cont:9"]["location_id"] == 1035466617946)
    check("two cans in different places stay two sources",
          stock["cont:5"][200] == 100.0 and stock["cont:9"][202] == 7.0)
    # A hangar source spans every station that character has one in, so it has no single location —
    # claiming one would be worse than claiming none.
    check("a hangar source claims no location", srcs["char:1"].get("location_id") is None)

    # Corp scans go through the same walk, so they get the same answer.
    corp_rows = [
        {"item_id": 1, "type_id": 0, "quantity": 1, "location_id": 60003760, "location_flag": "CorpSAG1"},
        {"item_id": 2, "type_id": 200, "quantity": 9, "location_id": 1, "location_flag": "Unlocked"},
    ]
    csrcs, _cstock, _cc = _split_by_source(
        corp_rows, set(_CORP_FLAGS), lambda f: f"corp:7:h{_CORP_FLAGS[f]}",
        lambda f: f"Corp — hangar {_CORP_FLAGS[f]}", cont_key=lambda loc: f"corp:7:c{loc}")
    check("a corp container knows where it is too",
          csrcs["corp:7:c1"]["location_id"] == 60003760)

    # The label every list groups by.
    check("a structure gets its system named beside it",
          _place_label("Test Citadel", "1DQ1-A") == "Test Citadel · 1DQ1-A")
    # An NPC station name already opens with its system, and so do plenty of player structures.
    check("a station name that already leads with its system isn't made to repeat it",
          _place_label("Jita IV - Moon 4 - Caldari Navy Assembly Plant", "Jita")
          == "Jita IV - Moon 4 - Caldari Navy Assembly Plant")
    check("the same rule for a structure named that way",
          _place_label("1DQ1-A - Test Citadel", "1DQ1-A") == "1DQ1-A - Test Citadel")
    check("no location resolves to no label", _place_label("", "") == "")
    check("a system with no station name is still better than nothing",
          _place_label("", "Jita") == "Jita")


def test_an_unreadable_structure_never_fails_the_scan():
    """Structure visibility is ACL-gated: somebody else's citadel 403s. That is a normal answer
    about a normal structure and must degrade to "no system name" — exactly like container naming —
    rather than taking the asset scan down. The unresolvable answer is cached too, or every scan
    would re-ask and re-burn the ESI error budget."""
    print("test_an_unreadable_structure_never_fails_the_scan")
    from app.industry import assets as A
    import app.markets as M

    con, restore = _patch_db_all(A)
    try:
        A.ensure_asset_tables()
        con.execute("CREATE TABLE solar_systems (system_id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        con.execute("INSERT INTO solar_systems VALUES (30000142, 'Jita')")
        con.commit()

        calls = []
        real_struct = M.structure_info
        real_get = A.esi_http.get

        class _R:
            def __init__(self, code, body):
                self.status_code, self._b = code, body

            def json(self):
                return self._b

        def fake_struct(sid, token, client=None):
            calls.append(("structure", sid))
            return None                     # a 403 comes back from structure_info as None

        def fake_get(url, **kw):
            calls.append(("http", url))
            if url.startswith("universe/stations/"):
                return _R(200, {"name": "Jita IV - Moon 4 - CNAP", "system_id": 30000142})
            return _R(404, {})

        M.structure_info, A.esi_http.get = fake_struct, fake_get
        try:
            srcs = {"cont:1": {"kind": "container", "name": "Can A", "parent": "",
                               "location_id": 60003760},
                    "cont:2": {"kind": "container", "name": "Can B", "parent": "",
                               "location_id": 1035466617946}}
            A._apply_locations(srcs, "tok")
            check("a station resolves to its name and system",
                  srcs["cont:1"]["location_name"] == "Jita IV - Moon 4 - CNAP"
                  and srcs["cont:1"]["system_name"] == "Jita")
            check("an unreadable structure degrades to no location, not an exception",
                  srcs["cont:2"]["location_name"] == "" and srcs["cont:2"]["system_name"] == "")

            n = len(calls)
            A._apply_locations(srcs, "tok")
            check("both answers are cached — a rescan asks ESI nothing", len(calls) == n)
            check("including the unresolvable one, so a 403 isn't retried every scan",
                  sum(1 for c in calls if c[0] == "structure") == 1)
        finally:
            M.structure_info, A.esi_http.get = real_struct, real_get

        # And the whole thing is best-effort: a resolver that throws must not stop a scan.
        def boom(*_a, **_k):
            raise RuntimeError("ESI is having a day")

        A.esi_http.get = boom
        M.structure_info = boom
        try:
            srcs2 = {"cont:3": {"kind": "container", "name": "Can C", "parent": "",
                                "location_id": 60009999}}
            A._apply_locations(srcs2, "tok")
            check("a throwing lookup leaves the source usable and unlocated",
                  srcs2.get("location_name") is None and "cont:3" in srcs2)
        finally:
            M.structure_info, A.esi_http.get = real_struct, real_get
    finally:
        restore()


def test_where_a_container_is_survives_the_round_trip():
    """The location has to reach every list that shows containers, which means through the store and
    back out of `list_sources` — the one call all four pickers are built from."""
    print("test_where_a_container_is_survives_the_round_trip")
    from app.industry import assets as A

    _con, restore = _patch_db_all(A)
    try:
        A.ensure_asset_tables()
        A._store(1, {"cont:5": {"kind": "container", "name": "Reaction can", "parent": "",
                                "location_id": 60003760, "location_name": "Rx Depot",
                                "system_name": "Jita"},
                     "char:1": {"kind": "hangar", "name": "Toon — personal hangar", "parent": ""}},
                 {"cont:5": {200: 10.0}, "char:1": {201: 5.0}}, {"cont:5", "char:1"}, scope="char:1")
        by_key = {s["key"]: s for s in A.list_sources(1)}
        check("the station reaches the picker", by_key["cont:5"]["location"] == "Rx Depot")
        check("so does the system", by_key["cont:5"]["system"] == "Jita")
        check("and the grouping label is built once, server-side",
              by_key["cont:5"]["place"] == "Rx Depot · Jita")
        check("a hangar has no place and is not forced into one",
              by_key["char:1"]["place"] == "")
        # A rescan must not lose it: _store rewrites the row from the fresh scan every time.
        A._store(1, {"cont:5": {"kind": "container", "name": "Reaction can", "parent": "",
                                "location_id": 60003760, "location_name": "Rx Depot",
                                "system_name": "Jita"}},
                 {"cont:5": {200: 20.0}}, {"cont:5"}, scope="char:1")
        check("and a rescan keeps it",
              [s for s in A.list_sources(1) if s["key"] == "cont:5"][0]["place"]
              == "Rx Depot · Jita")
    finally:
        restore()


def test_a_build_can_be_gathered_from_several_boxes():
    """Reaction stock and manufacturing stock sit in different stations, so one build's materials
    legitimately live in several boxes. The bound set sums across them without double counting, and
    an order written before the set existed still reads as the one box it named."""
    print("test_a_build_can_be_gathered_from_several_boxes")
    from app.industry import assets as A
    from app.industry.orders import order_source_keys, _normalise_source_keys

    _con, restore = _patch_db_all(A)
    try:
        A.ensure_asset_tables()
        A._store(1, {"cont:1": {"kind": "container", "name": "Reaction can", "parent": ""},
                     "cont:2": {"kind": "container", "name": "Mfg can", "parent": ""},
                     "cont:3": {"kind": "container", "name": "Someone else's", "parent": ""}},
                 {"cont:1": {200: 100.0, 201: 5.0}, "cont:2": {200: 40.0}, "cont:3": {200: 999.0}},
                 {"cont:1", "cont:2", "cont:3"}, scope="char:1")

        check("one box still sums to one box", A.source_quantities_multi(1, ["cont:1"]) == {200: 100.0, 201: 5.0})
        check("two boxes add up", A.source_quantities_multi(1, ["cont:1", "cont:2"])[200] == 140.0)
        check("a box named twice is still counted once",
              A.source_quantities_multi(1, ["cont:1", "cont:2", "cont:1"])[200] == 140.0)
        check("a box nobody bound is not counted",
              A.source_quantities_multi(1, ["cont:1", "cont:2"]).get(200) == 140.0)
        check("no boxes means no stock", A.source_quantities_multi(1, []) == {})
        check("the single-box helper still answers as it always did",
              A.source_quantities(1, "cont:2") == {200: 40.0})
        # Labels for the panel: name AND where, in bind order, with a vanished box still listed.
        labels = A.source_labels(1, ["cont:2", "cont:1", "cont:404"])
        check("bound boxes come back in the order they were bound",
              [b["key"] for b in labels] == ["cont:2", "cont:1", "cont:404"])
        check("a box that has left your assets is still shown, flagged",
              labels[2]["missing"] is True and labels[0]["missing"] is False)

        # Back-compat of the binding itself.
        check("an order with only the old single key reads as a one-box set",
              order_source_keys({"source_key": "cont:9", "source_keys": ""}) == ["cont:9"])
        check("an order with a set reads as that set",
              order_source_keys({"source_key": "cont:1", "source_keys": '["cont:1","cont:2"]'})
              == ["cont:1", "cont:2"])
        check("an unbound order binds nothing",
              order_source_keys({"source_key": "", "source_keys": ""}) == [])
        check("garbage in the column is not a crash",
              order_source_keys({"source_key": "", "source_keys": "{nope"}) == [])
        check("duplicates and blanks are normalised away",
              _normalise_source_keys(["cont:1", " ", "cont:1", "cont:2"]) == ["cont:1", "cont:2"])
    finally:
        restore()


def test_the_box_and_the_note_still_take_the_higher_of_the_two():
    """The rule that makes the checklist trustworthy: a hand-noted quantity never erases what is
    really in the containers, and a rescan never erases a note. Adding more boxes must not weaken
    it — the SUM across boxes is what the note is weighed against, still capped at what's needed."""
    print("test_the_box_and_the_note_still_take_the_higher_of_the_two")
    from app.industry.sourcing import _item_row

    s = {"type_id": 200, "name": "MineralA", "qty": 100.0, "unit_price": 10.0}
    r = _item_row(s, {200: 60.0}, {200: 25.0})
    check("the boxes win when they hold more", r["sourced"] == 60.0 and r["remaining"] == 40.0)
    r = _item_row(s, {200: 20.0}, {200: 70.0})
    check("the note wins when it says more", r["sourced"] == 70.0 and r["remaining"] == 30.0)
    # Two boxes holding 60 + 50 is 110 of a 100 requirement: capped, done, never negative.
    r = _item_row(s, {200: 110.0}, {})
    check("more than you need is not more than done", r["sourced"] == 100.0 and r["remaining"] == 0.0)
    check("and it is reported done", r["done"] is True)
    r = _item_row(s, {}, {})
    check("nothing gathered costs the whole line", r["remaining_cost"] == 1000.0)


def test_a_plan_owns_its_containers():
    """A container bound to one build is that build's stock. Sharing it with another build is then
    something the user does on purpose, by picking it there too — not a side effect of binding it.

    The rule that keeps this from being retroactive matters more than the rule itself: an order
    queued before per-plan sources existed still draws on the account-wide tick list, so nothing
    about an in-flight build changes until its owner edits it."""
    print("test_a_plan_owns_its_containers")
    from app.industry import assets as A
    from app.industry import orders as O

    _con, restore = _patch_db_all(A, O)
    try:
        A.ensure_asset_tables()
        A._store(1, {"cont:1": {"kind": "container", "name": "Build A can", "parent": ""},
                     "cont:2": {"kind": "container", "name": "Build B can", "parent": ""},
                     "char:1": {"kind": "hangar", "name": "Toon hangar", "parent": ""}},
                 {"cont:1": {200: 10.0}, "cont:2": {200: 20.0}, "char:1": {200: 5.0}},
                 {"cont:1", "cont:2", "char:1"}, scope="char:1")
        A.set_sources(1, ["char:1"], True)          # the account-wide tick list

        legacy = {"source_key": "cont:1", "source_keys": "", "sources_owned": 0}
        curated_a = {"source_key": "cont:1", "source_keys": '["cont:1"]', "sources_owned": 1}
        curated_b = {"source_key": "cont:2", "source_keys": '["cont:2"]', "sources_owned": 1}

        check("a queue of pre-existing orders keeps using the account pool",
              O.plan_source_keys(1, [legacy]) is None)
        check("a curated order counts its own boxes and nothing else",
              O.plan_source_keys(1, [curated_a]) == ["cont:1"])
        check("two curated orders count the union of theirs",
              sorted(O.plan_source_keys(1, [curated_a, curated_b])) == ["cont:1", "cont:2"])
        mixed = O.plan_source_keys(1, [legacy, curated_b])
        check("a mixed queue never narrows what the uncurated order was already entitled to",
              set(mixed) == {"char:1", "cont:2"})

        # And the stock actually resolved from it. The account pool is 5; build A's can is 10.
        check("the old behaviour is bit-for-bit unchanged for an uncurated queue",
              O._stock_for(1, [], [legacy]) == {200: 5.0})
        check("a curated build spends its own box, not the account's hangar",
              O._stock_for(1, [], [curated_a]) == {200: 10.0})
        check("a box in two curated orders is still only spendable once",
              O._stock_for(1, [], [curated_a, curated_a]) == {200: 10.0})
        check("the ordered product itself is never counted as stock",
              O._stock_for(1, [(200, 1)], [curated_a]) == {})
        check("no orders at all is the account pool, as every caller before this assumed",
              O._stock_for(1, [], None) == {200: 5.0})
        # Owning nothing is not the same as owning an empty set: a build with no box picked has said
        # nothing about where its materials come from, so it falls back rather than counting zero.
        empty = {"source_key": "", "source_keys": "[]", "sources_owned": 1}
        check("a plan that names no box falls back to the account pool",
              O.plan_source_keys(1, [empty]) is None)
    finally:
        restore()


def test_binding_a_set_remembers_it_without_switching_it_on_for_everyone():
    """Two halves of the old single-key bind, separated: remembering the answer (so the next order
    arrives pre-filled) and switching the box on account-wide (so every other plan can spend it).
    Under per-plan sources only the first is wanted — the second is what made one build's can
    everybody's stock."""
    print("test_binding_a_set_remembers_it_without_switching_it_on_for_everyone")
    from app.industry import assets as A
    from app.industry import sourcing as S
    from app.industry import settings as SET

    _con, restore = _patch_db_all(A, S, SET)
    try:
        A.ensure_asset_tables()
        SET.ensure_industry_settings_table.__wrapped__()
        A._store(1, {"cont:1": {"kind": "container", "name": "Rx can", "parent": ""},
                     "cont:2": {"kind": "container", "name": "Mfg can", "parent": ""}},
                 {"cont:1": {200: 10.0}, "cont:2": {200: 20.0}}, {"cont:1", "cont:2"}, scope="char:1")

        S.remember_source_default(1, ["cont:1", "cont:2"])
        check("the whole set is remembered for the next build",
              SET.get_settings(1)["last_source_keys"] == ["cont:1", "cont:2"])
        check("and its first box still fills the old single field",
              SET.get_settings(1)["last_source_key"] == "cont:1")
        check("remembering does NOT make the boxes everybody's stock",
              A.owned_quantities(1) == {})

        # The legacy path — a caller that only sends `source_key` has made no claim to owning its
        # plan's sources, so it keeps the behaviour it has always had.
        S.enable_bound_source(1, "cont:2")
        check("the old single-key bind still enables the box account-wide",
              A.owned_quantities(1) == {200: 20.0})
        check("an empty bind remains a no-op", S.enable_bound_sources(1, []) is None)
        check("and leaves the tick list alone", A.owned_quantities(1) == {200: 20.0})

        # A remembered set whose boxes have since gone must not resurrect them.
        check("nothing is remembered from an empty set",
              S.remember_source_default(1, []) is None)
        check("the last real answer stands", SET.get_settings(1)["last_source_keys"] == ["cont:2"])
    finally:
        restore()


def test_a_named_set_of_containers_is_one_pick():
    """"Reaction stock" is three cans across two stations. Naming that set is what keeps binding it
    to a build at one click rather than three — the effort constraint, not a filing system."""
    print("test_a_named_set_of_containers_is_one_pick")
    from app.industry import assets as A

    _con, restore = _patch_db_all(A)
    try:
        A.ensure_asset_tables()
        saved = A.save_source_set(1, "Reaction stock", ["cont:1", "cont:2", "cont:1"])
        check("a set is saved under its name", saved["name"] == "Reaction stock")
        check("with its boxes de-duplicated", saved["keys"] == ["cont:1", "cont:2"])
        A.save_source_set(1, "Reaction stock", ["cont:9"])
        sets = A.list_source_sets(1)
        check("saving the same name again replaces it rather than growing a twin", len(sets) == 1)
        check("and the replacement is what's stored", sets[0]["keys"] == ["cont:9"])
        A.save_source_set(1, "Capital mats", ["cont:3"])
        check("a different name is a different set", len(A.list_source_sets(1)) == 2)
        check("sets are per account", A.list_source_sets(2) == [])
        check("an unnamed set is not saved", A.save_source_set(1, "   ", ["cont:1"]) == {})
        A.delete_source_set(1, sets[0]["id"])
        check("deleting one leaves the other",
              [s["name"] for s in A.list_source_sets(1)] == ["Capital mats"])
    finally:
        restore()


def test_an_existing_single_key_order_keeps_planning_identically():
    """The migration promise, end to end: an order queued the old way must plan exactly as it did
    until its owner edits it — and editing it through the new picker is the moment it takes
    ownership. Additive columns only; the old one is still written and still read."""
    print("test_an_existing_single_key_order_keeps_planning_identically")
    from app.industry import assets as A
    from app.industry import orders as O
    from app.industry import sourcing as S
    from app.industry import settings as SET

    con, restore = _patch_db_all(A, O, S, SET)
    try:
        A.ensure_asset_tables()
        SET.ensure_industry_settings_table.__wrapped__()
        O.ensure_industry_orders_table.__wrapped__()
        A._store(1, {"cont:1": {"kind": "container", "name": "Old can", "parent": ""},
                     "cont:2": {"kind": "container", "name": "New can", "parent": ""}},
                 {"cont:1": {200: 10.0}, "cont:2": {200: 20.0}}, {"cont:1", "cont:2"}, scope="char:1")

        # An order exactly as the old code wrote one: source_key only, no set, no ownership.
        con.execute("INSERT INTO pp_industry_orders (id, context_id, product_type_id, name, "
                    "quantity, mode, priority, status, created_at, source_key) "
                    "VALUES (1, 1, 100, 'Widget', 1, 'parallel', 0, 'queued', 1.0, 'cont:1')")
        con.commit()
        row = con.execute("SELECT * FROM pp_industry_orders WHERE id=1").fetchone()
        check("it still names its box", O.order_source_keys(row) == ["cont:1"])
        check("and it has not silently claimed to own its sources",
              O.plan_source_keys(1, [dict(row)]) is None)

        # Now the user edits it through the new picker — `source_keys` is the statement of ownership.
        req = O.OrderUpdate(source_keys=["cont:1", "cont:2"])
        check("the request carries the set", req.source_keys == ["cont:1", "cont:2"])
        keys = O._normalise_source_keys(req.source_keys)
        con.execute("UPDATE pp_industry_orders SET source_key=?, source_keys=?, sources_owned=1 "
                    "WHERE id=1", (keys[0], json.dumps(keys)))
        con.commit()
        row = con.execute("SELECT * FROM pp_industry_orders WHERE id=1").fetchone()
        check("both boxes are bound", O.order_source_keys(row) == ["cont:1", "cont:2"])
        check("the old column still holds the first, for anything that only knows about one",
              row["source_key"] == "cont:1")
        check("and the plan now owns exactly those boxes",
              sorted(O.plan_source_keys(1, [dict(row)])) == ["cont:1", "cont:2"])
        check("which is 30 units of stock, not the account's idea of it",
              O._stock_for(1, [], [dict(row)]) == {200: 30.0})

        # The columns are added the only way this codebase migrates.
        import inspect
        ddl = inspect.getsource(O.ensure_industry_orders_table)
        check("the new columns are additive", "add_columns(" in ddl and "source_keys" in ddl
              and "sources_owned" in ddl)
        # Additive means additive: no column or table is ever removed to make room for the new ones.
        # (Matched on the statements, not the prose — the comments in there discuss decisions being
        # "silently dropped", which is not a DDL statement.)
        up = ddl.upper()
        check("and nothing is dropped",
              "DROP COLUMN" not in up and "DROP TABLE" not in up)
    finally:
        restore()


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
    test_saved_build_options_reach_plans_run_without_a_browser()
    test_price_is_net_cost_plus_margin()
    test_queue_price_uses_each_orders_own_margin()
    test_share_links_outlive_their_order()
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
    test_blacklisted_components_are_always_bought()
    test_a_target_is_never_blacklisted_out_of_its_own_build()
    test_hand_marked_jobs_count_as_progress()
    test_progress_percent_is_weighted_by_job_time_not_run_count()
    test_jobs_are_aligned_to_the_plans_pace_not_stretched_past_it()
    test_slack_travels_down_a_chain_not_just_one_level()
    test_a_job_is_lifted_to_land_with_what_runs_beside_it()
    test_a_deliverable_is_never_paced_against_the_rest_of_the_queue()
    test_a_job_never_carries_more_runs_than_the_blueprint_copy_has()
    test_an_owned_copy_only_covers_the_runs_it_has()
    test_runs_needed_is_never_reported_as_runs_on_the_copy()
    test_one_products_runs_are_not_spread_thinner_than_its_own_pace()
    test_slack_comes_from_the_consumer_not_from_stage_mates()
    test_sourcing_notes_belong_to_one_order()
    test_pasting_a_hangar_sets_what_is_sourced()
    test_the_sourcing_list_is_not_a_second_shopping_list()
    test_binding_a_container_lets_the_planner_spend_it()
    test_first_run_setup_shows_once_and_never_to_an_established_user()
    test_the_customer_sees_the_same_progress_the_builder_does()
    test_stale_caches_refresh_themselves_on_the_way_in()
    test_opening_the_tab_plans_the_queue_once_not_twice()
    test_building_the_borderline_set_runs_to_a_fixpoint()
    test_a_scan_retires_stock_that_is_no_longer_there()
    test_corp_hangars_and_containers_split_like_personal_ones()
    test_ordinary_users_are_not_asked_for_director_permissions()
    test_the_step_by_step_parts_account_for_the_whole()
    test_a_rig_only_applies_to_what_it_is_for()
    test_a_structure_with_no_families_still_covers_everything()
    test_the_planner_picks_the_structure_that_covers_the_job()
    test_cost_materials_time_and_schedule_all_use_the_same_site()
    test_the_job_fee_follows_the_system_the_job_lands_in()
    test_an_unrouted_plan_is_byte_for_byte_the_old_plan()
    test_every_station_change_is_reported()
    test_the_rig_family_registry_is_the_single_source()
    test_a_container_carries_where_it_is()
    test_an_unreadable_structure_never_fails_the_scan()
    test_where_a_container_is_survives_the_round_trip()
    test_a_build_can_be_gathered_from_several_boxes()
    test_the_box_and_the_note_still_take_the_higher_of_the_two()
    test_a_plan_owns_its_containers()
    test_binding_a_set_remembers_it_without_switching_it_on_for_everyone()
    test_a_named_set_of_containers_is_one_pick()
    test_an_existing_single_key_order_keeps_planning_identically()
    test_every_copy_the_account_holds_counts()
    test_a_stack_of_originals_is_not_a_copy_that_covers_nothing()
    test_each_job_runs_off_the_copy_it_is_installed_on()
    test_an_override_still_beats_every_copy_you_own()
    test_a_single_copy_account_plans_exactly_as_before()
    print(f"\nAll {_passed} checks passed.")




def test_runs_needed_is_never_reported_as_runs_on_the_copy():
    """Reported by a builder ordering TWO Phoenixes: the plan looked like it was offering a "BPC
    with 2 runs", and capital blueprint copies only ever carry 1.

    Nothing was misread. The SDE caps the Phoenix blueprint at max_runs 1, every copy indexed off
    Jita contracts carries runs 1, and the scheduler had correctly produced two 1-run jobs. The
    order was for two hulls. What the plan did wrong was report the BATCH's run count and the
    blueprint noun as one fact, leaving the reader to join them.

    Runs needed, runs per copy and copies to buy are THREE numbers. This pins that each is reported
    on its own, that no job is ever planned longer than one copy carries, and — the part that was
    genuinely broken — that `acquisition_costs` actually emits the `runs_per_copy` that
    `build_tasks` caps on. That key was documented and only ever set for `bpo_only`, so the cap for
    a print the plan BUYS was dead code, verified only by tests that hand-built the dict."""
    print("test_runs_needed_is_never_reported_as_runs_on_the_copy")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # Widget stands in for the hull: one run per job, like every capital blueprint.
    mfg = dict(mfg)
    mfg[100] = {**mfg[100], "max_runs": 1}
    pools = {"manufacturing": 8, "reaction": 8}

    # A 1-run copy in the hangar, against an order for two.
    P = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                    owned={100: {"me": 10, "te": 18, "kind": "bpc", "runs": 1}})
    res = plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, P, NAMES, pools)
    req = {r["type_id"]: r for r in res["requirements"]}[100]

    check("the build needs two runs", req["runs"] == 2)
    check("the copy still carries one", req["blueprint"]["runs"] == 1)
    check("and the shortfall is the difference", req["runs_short"] == 1)
    check("copies to buy is its own number", "copies_to_buy" in req)
    # The three must never be the same field: a UI reading `runs` off a row tagged BPC is exactly
    # how "2 runs needed" became "a 2-run BPC".
    check("runs needed is not the copy's run count", req["runs"] != req["blueprint"]["runs"])

    jobs = [t for w in res["schedule"]["waves"] for t in w["tasks"] if t["type_id"] == 100]
    check("a 1-run blueprint yields 1-run jobs", jobs and all(t["runs"] == 1 for t in jobs))
    check("and the full order is still built", sum(t["runs"] for t in jobs) == 2)

    # An ORIGINAL covers any batch — it must not acquire a phantom shortfall from this.
    Pbpo = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                       owned={100: {"me": 10, "te": 18, "kind": "bpo", "runs": -1}})
    rbpo = {r["type_id"]: r for r in plan_queue([(100, 2)], mfg, rx, _prices(SELL), ADJ, Pbpo,
                                                NAMES, pools)["requirements"]}[100]
    check("an original is never short", rbpo["runs_short"] == 0 and rbpo["copies_to_buy"] == 0)

    # The cap `build_tasks` reads has to be a key the real producer sets, not one only tests write.
    import app.industry.bpc as B
    dbcon, restore = _patch_db(B)
    try:
        dbcon.execute("CREATE TABLE blueprints (blueprint_type_id INTEGER PRIMARY KEY, "
                      "product_type_id INTEGER, output_qty INTEGER, base_time INTEGER, "
                      "max_runs INTEGER)")
        dbcon.execute("INSERT INTO blueprints VALUES (1000, 100, 1, 3600, 1)")
        B.ensure_bpc_tables()
        now = time.time()
        for cid, runs, price in ((1, 1, 20e6), (2, 1, 26e6), (3, 1, 22e6)):
            dbcon.execute("INSERT INTO pp_bpc_observations (contract_id, region_id, type_id, "
                          "is_bpc, runs, me, te, price, first_seen, last_seen) "
                          "VALUES (?,?,?,1,?,10,18,?,?,?)",
                          (cid, B.THE_FORGE, 1000, runs, price, now, now))
        dbcon.commit()
        acq = B.acquisition_costs([100], {})[100]
        check("a listed copy reports its run count", acq.get("runs_per_copy") == 1)
        check("and it is not silently zero", (acq.get("runs_per_copy") or 0) > 0)

        # A market where bigger copies exist reports the biggest — the loosest cap that is still
        # true whatever combination cost_for_runs buys.
        dbcon.execute("INSERT INTO pp_bpc_observations (contract_id, region_id, type_id, is_bpc, "
                      "runs, me, te, price, first_seen, last_seen) VALUES (9, ?, 1000, 1, 40, 10, "
                      "18, 7100000, ?, ?)", (B.THE_FORGE, now, now))
        dbcon.commit()
        check("the biggest copy on offer sets the cap",
              B.acquisition_costs([100], {})[100].get("runs_per_copy") == 40)
    finally:
        restore()


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




# ── Always-buy blacklist, hand-marked progress, sourcing, corp stock ──────────────────────────
# The four things a builder asked for after living with the tool: never build THAT, I already did
# this one, here's what I've gathered so far, and read the corp hangar I'm gathering it into.

def test_blacklisted_components_are_always_bought():
    """A standing "I never build that" beats the cost engine, and it has to beat it at COSTING time
    too: deciding to buy a component while still pricing its parent as if it were built is how a
    plan's total stops matching its own shopping list."""
    print("test_blacklisted_components_are_always_bought")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    P = _prices(SELL)

    base = build_plan(100, 1, mfg, rx, P, ADJ, BuildParams(), NAMES)
    check("gadget is built when nothing forbids it",
          any(j["type_id"] == 101 for j in base["jobs"]))

    never = build_plan(100, 1, mfg, rx, P, ADJ, BuildParams(never_build_ids={101}), NAMES)
    shop = {s["type_id"]: s for s in never["shopping_list"]}
    check("a blacklisted component is bought instead", 101 in shop)
    check("and nothing is built for it", not any(j["type_id"] == 101 for j in never["jobs"]))
    check("its own inputs drop off the shopping list", 201 not in shop)
    check("the row says why it's there", shop[101]["blacklisted"] is True)
    # 2 Gadgets at the market price of 1000 each, so the parent is costed against what it will pay.
    check("materials are priced at the buy price", approx(shop[101]["qty"] * 1000.0, 2000.0))
    check("and the total reflects the dearer route",
          never["metrics"]["total_cost"] > base["metrics"]["total_cost"])

    # force_build is per-order and deliberate; the blacklist is a standing default. The specific
    # choice has to win, or an order could never make an exception.
    both = build_plan(100, 1, mfg, rx, P, ADJ,
                      BuildParams(never_build_ids={101}, force_build_ids={101}), NAMES)
    check("an order set to build it anyway still builds it",
          any(j["type_id"] == 101 for j in both["jobs"]))

    # Nothing to fall back on: an item with no price cannot be bought, so refusing to build it would
    # leave the plan with no way to get one at all.
    unpriced = _prices({k: v for k, v in SELL.items() if k != 101})
    no_price = build_plan(100, 1, mfg, rx, unpriced, ADJ, BuildParams(never_build_ids={101}), NAMES)
    check("an unbuyable component is still built", any(j["type_id"] == 101 for j in no_price["jobs"]))


def test_a_target_is_never_blacklisted_out_of_its_own_build():
    """Blacklisting something and then ordering it is not a contradiction to resolve in favour of
    the list — ordering it IS the more recent, more specific instruction."""
    print("test_a_target_is_never_blacklisted_out_of_its_own_build")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    # The engine-level guarantee behind that filter: a root builds on its own buildability.
    res = build_plan(101, 1, mfg, rx, _prices(SELL), ADJ,
                     BuildParams(never_build_ids={101}), NAMES)
    check("the ordered product is built regardless", res["tree"]["decision"] == "build")


def test_hand_marked_jobs_count_as_progress():
    print("test_hand_marked_jobs_count_as_progress")
    from app.industry.progress import resolve_done, _ALL

    check("a hand mark fills an untouched step", resolve_done(10, 0, 0, 10) == 10)
    check("it never lowers what was measured", resolve_done(10, 7, 0, 2) == 7)
    check("owning the output still counts", resolve_done(10, 0, 4, 0) == 4)
    check("nothing exceeds what the plan asked for", resolve_done(10, 99, 0, 99) == 10)
    check("no signal is no progress", resolve_done(10, 0, 0, 0) == 0)
    check("'all of it' is a sentinel, not a run count", _ALL < 0)

    # Storage round-trip against a real (in-memory) DB, including the two rules that matter: a mark
    # is epoch-gated like every other done-signal, and clearing it means clearing it.
    from app.industry import progress as P
    con, restore = _patch_db(P)
    try:
        P.ensure_manual_done_table.__wrapped__()   # @ensure_once may already have fired at import
        t0 = time.time()
        P.set_manual_done(1, 100, None)
        check("marked done for all runs", P._manual_by_type(1, t0 - 5) == {100: _ALL})
        check("a mark from before this queue is ignored", P._manual_by_type(1, t0 + 60) == {})
        P.set_manual_done(1, 100, 3)
        check("a partial mark stores its run count", P._manual_by_type(1, t0 - 5) == {100: 3})
        # Half a step is a real state: five of twelve runs installed and finished, the rest waiting
        # on a slot. It counts for exactly what it says and no more.
        check("a partial mark fills only its share", P.resolve_done(12, 0, 0, 5) == 5)
        check("and never more than the plan asks for", P.resolve_done(12, 0, 0, 99) == 12)

        # `observed_runs` is what the count would be with the marks taken away. The browser needs it
        # to recompute done_runs itself the instant you tick something, instead of waiting on a
        # re-plan of the whole queue; done_runs alone can't be un-mixed once a mark is folded in.
        req = {"name": "Widget", "activity": "manufacturing", "output_qty": 1}
        row = P._type_row(100, req, 12, 12, 0, in_stock=0, manual=12, observed=4)
        check("a row says what was observed without the mark", row["observed_runs"] == 4)
        check("alongside what the mark made of it", row["done_runs"] == 12)
        check("recomputing from those two matches the server's own rule",
              max(row["observed_runs"], row["manual_runs"]) == row["done_runs"])
        plain = P._type_row(100, req, 12, 4, 0, in_stock=0)
        check("with no mark, observed is simply the count", plain["observed_runs"] == 4)
        P.set_manual_done(1, 100, 0)
        check("clearing removes it entirely", P._manual_by_type(1, t0 - 5) == {})
        check("and another account's marks are invisible", P._manual_by_type(2, 0) == {})
    finally:
        restore()


def test_progress_percent_is_weighted_by_job_time_not_run_count():
    """Run count is the right unit for MARKING a step and the wrong one for summarising a build.
    Bulk components come in hundreds of short runs while the capital part is a handful of very long
    ones, so counting runs reported 71.8% done when what had actually finished was 57 minutes of a
    multi-day build. Weighting by job time makes the headline agree with the clock beside it."""
    print("test_progress_percent_is_weighted_by_job_time_not_run_count")
    from app.industry.progress import _weighted_pct, _progress_payload, _hours_by_type

    req = {"name": "x", "activity": "manufacturing", "output_qty": 1}
    from app.industry.progress import _type_row
    # 300 quick reaction runs (1 hour of work all told) finished; one 99-hour capital job not.
    bulk = _type_row(1, req, 300, 300, 0, in_stock=0, job_hours=1.0)
    cap = _type_row(2, req, 1, 0, 0, in_stock=0, job_hours=99.0)
    payload = _progress_payload([bulk, cap], [])
    check("by run count the build would look nearly finished", payload["runs_pct"] > 99)
    check("by job time it is barely started", payload["pct"] == 1.0)
    check("both numbers are reported, so the tile can say which it used",
          payload["pct"] != payload["runs_pct"])
    check("and the hours behind it are exposed for the tooltip",
          payload["hours"] == {"total": 100.0, "done": 1.0})

    # No schedule times at all (an older plan, or a queue that scheduled nothing) — fall back to
    # runs rather than reporting zero progress on a build that has some.
    no_hours = [_type_row(1, req, 10, 5, 0, in_stock=0)]
    check("with no times known it falls back to counting runs",
          _weighted_pct(no_hours) is None and _progress_payload(no_hours, [])["pct"] == 50.0)

    # The per-type hours come from the plan's own schedule, summed across parallel splits.
    res = {"schedule": {"waves": [
        {"tasks": [{"type_id": 7, "duration_hours": 2.0}, {"type_id": 7, "duration_hours": 3.0}]},
        {"tasks": [{"type_id": 8, "duration_hours": 4.0}]}]}}
    check("a type's runs split across parallel jobs are summed",
          _hours_by_type(res) == {7: 5.0, 8: 4.0})


def test_slack_comes_from_the_consumer_not_from_stage_mates():
    """Reported from a real build: one component running 8 jobs of 1 run (5h 05m) beside another
    running 9 jobs of 1 run (2h 32m), both feeding the same work, across 5 characters and 29 slots.
    Two runs of the 2h 32m one is 5h 04m — it could take half the slots and land at the same moment.

    The first version paced each type against its stage-mates in the same POOL, which misses this:
    a type alone at its stage paces against itself, and two types feeding one job from different
    depths never see each other. A component's deadline is when the job consuming it can start."""
    print("test_slack_comes_from_the_consumer_not_from_stage_mates")
    # 1: the pace-setter, 8 runs of ~38m that cannot be narrowed (its own work sets the deadline).
    # 2: 9 runs of ~17m, which alone would split 9 ways. 3: the assembly that eats both.
    mfg = {
        1: {"base_time": 2287, "max_runs": 1, "output_qty": 1, "inputs": [{"type_id": 0, "quantity": 1}]},
        2: {"base_time": 1013, "max_runs": 10, "output_qty": 1, "inputs": [{"type_id": 0, "quantity": 1}]},
        3: {"base_time": 3600, "max_runs": 1, "output_qty": 1,
            "inputs": [{"type_id": 1, "quantity": 1}, {"type_id": 2, "quantity": 1}]},
    }
    agg = {
        1: {"build": True, "runs": 8, "activity": "manufacturing"},
        2: {"build": True, "runs": 9, "activity": "manufacturing"},
        3: {"build": True, "runs": 1, "activity": "manufacturing"},
    }
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 29, "reaction": 29}
    deps = _built_deps(agg, mfg, {})
    depths = {1: 1, 2: 1, 3: 0}

    wide, wide_by = build_tasks(agg, mfg, {}, params, pools, depths=depths)
    check("left alone, the quick component takes a slot per run", len(wide_by[2]) == 9)

    _tasks, by_type = build_tasks(agg, mfg, {}, params, pools, depths=depths, deps=deps)
    check("with the consumer's deadline known, it uses fewer", len(by_type[2]) < 9)
    check("and it still finishes by the time the assembly can start",
          max(t.duration for t in by_type[2]) <= max(t.duration for t in by_type[1]) + 1e-6)
    check("the pace-setter is untouched", len(by_type[1]) == 8)
    check("slots saved", len(_tasks) < len(wide))

    # The promise that makes it safe: the build does not take a minute longer.
    prio = {1: (2, 0.0), 2: (1, 0.0), 3: (0, 0.0)}
    s_wide = schedule(wide, wide_by, deps, pools, prio)
    s_packed = schedule(_tasks, by_type, deps, pools, prio)
    check("and the whole build still finishes at the same time",
          approx(s_wide["makespan_hours"], s_packed["makespan_hours"], 0.02))


def test_one_products_runs_are_not_spread_thinner_than_its_own_pace():
    """Reported from a real build: Sulfuric Acid spread over 29 slots as a mix of 2-run jobs (5h 05m)
    and 1-run jobs (2h 32m). The 2-run jobs set the pace, so every 1-run job finishes in half the
    time and then its slot sits idle — the build is no faster for occupying them, and meanwhile
    there are no slots left to start a second plan in. Two runs each, half the jobs, same finish.

    This is a type's OWN slack and needs no dependency information at all: an uneven split finishes
    when the biggest chunk does, so every other job may carry that many runs too."""
    print("test_one_products_runs_are_not_spread_thinner_than_its_own_pace")
    per_run = 9120                      # 2h 32m
    mfg = {1: {"base_time": per_run, "max_runs": 10, "output_qty": 1, "inputs": []}}
    agg = {1: {"build": True, "runs": 35, "activity": "manufacturing"}}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 29, "reaction": 29}

    # The old behaviour, stated directly rather than obtained from build_tasks: one job per slot,
    # 35 runs over 29 slots, so six jobs carry 2 runs and 23 carry 1. The 2-run jobs set the pace.
    naive_jobs, naive_dur = 29, 2 * per_run

    packed, by_type = build_tasks(agg, mfg, {}, params, pools, depths={1: 0}, deps={1: set()})
    check("no job is left carrying less than the pace allows",
          max(t.runs for t in by_type[1]) == 2)
    check("which is 18 slots instead of 29", len(by_type[1]) == 18 < naive_jobs)
    check("and not one minute slower",
          approx(max(t.duration for t in by_type[1]), naive_dur, 1e-6))
    check("the runs themselves are all still there", sum(t.runs for t in by_type[1]) == 35)
    check("so 11 slots are free for other work", naive_jobs - len(by_type[1]) == 11)

    # Own slack needs no dependency graph — it is a fact about one type's own uneven split — so it
    # must hold on every path into build_tasks, including callers that pass neither depths nor deps.
    _b, bare = build_tasks(agg, mfg, {}, params, pools)
    check("and it holds even with no plan shape given", len(bare[1]) == 18)
    # A blueprint copy that can only carry so many runs still binds.
    capped = {1: {"base_time": per_run, "max_runs": 1, "output_qty": 1, "inputs": []}}
    _t, capped_by = build_tasks(agg, capped, {}, params, pools, depths={1: 0}, deps={1: set()})
    check("but a 1-run blueprint cap is never exceeded",
          max(t.runs for t in capped_by[1]) == 1)


def test_an_owned_copy_only_covers_the_runs_it_has():
    """Owning a blueprint was treated as owning it for any batch size. It isn't: a COPY carries a
    fixed number of runs, so holding a 4-run copy against a 20-run batch is sixteen runs you still
    have to find — priced at nothing, and reported as "you have the blueprint", which tells a
    builder they are ready to start when they are not. An ORIGINAL genuinely does cover any batch."""
    print("test_an_owned_copy_only_covers_the_runs_it_has")
    mfg = {1: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []}}
    agg_of = lambda: {1: {"build": True, "runs": 20, "activity": "manufacturing"}}
    prices, adj, names = _prices({1: 5_000_000.0}), {1: 1.0}, {1: "Widget"}
    pools = {"manufacturing": 5, "reaction": 5}
    acquire = {1: {"kind": "bpc", "price": 10_000_000.0, "runs_per_copy": 5, "live": True,
                   "listings": [{"runs": 5, "price": 10_000_000.0}] * 8}}

    def bp_cost(owned):
        p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                        owned=owned, bp_acquire=acquire)
        memo, _u = resolve_unit_costs(mfg, {}, prices, adj, p)
        agg = aggregate_demand([(1, 20)], memo, mfg, {}, p, None, pools)
        return agg[1].get("blueprint_cost", 0.0), agg[1].get("runs_short", 0)

    none_cost, none_short = bp_cost({})
    check("owning nothing charges for the whole batch's copies", none_cost > 0)
    check("and nothing is 'short', because nothing was assumed", none_short == 0)

    bpo_cost, bpo_short = bp_cost({1: {"me": 0, "te": 0, "kind": "bpo", "runs": -1}})
    check("an original covers any batch, for free", bpo_cost == 0.0 and bpo_short == 0)

    part_cost, part_short = bp_cost({1: {"me": 0, "te": 0, "kind": "bpc", "runs": 8}})
    check("an 8-run copy leaves 12 runs to find", part_short == 12)
    check("which are charged for", part_cost > 0)
    # 12 runs is three 5-run copies where 20 runs is four, so holding one really is cheaper. (Note
    # it isn't always: a shortfall of 16 still needs four copies, because copies come whole.)
    check("but less than owning nothing at all", part_cost < none_cost)
    check("and the shortfall is charged in whole copies", part_cost == none_cost * 3 / 4)

    full_cost, full_short = bp_cost({1: {"me": 0, "te": 0, "kind": "bpc", "runs": 25}})
    check("a copy with runs to spare covers the batch", full_short == 0 and full_cost == 0.0)


def test_a_job_never_carries_more_runs_than_the_blueprint_copy_has():
    """Packing deliberately makes manufacturing jobs longer, which runs straight into the thing the
    SDE cap does not describe: a COPY carries a fixed number of runs. A 20-run batch is happy as two
    10-run jobs when the plan has the slack, and impossible off 5-run copies. Reactions have no
    blueprint and are untouched by any of this."""
    print("test_a_job_never_carries_more_runs_than_the_blueprint_copy_has")
    # A 10h unsplittable job beside 20 one-hour runs feeding the same assembly: the 20 runs have
    # ten hours of room, so without a copy limit they would pack into 2 jobs of 10.
    mfg = {
        1: {"base_time": 36000, "max_runs": 1, "output_qty": 1, "inputs": []},
        2: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []},
        3: {"base_time": 3600, "max_runs": 1, "output_qty": 1,
            "inputs": [{"type_id": 1, "quantity": 1}, {"type_id": 2, "quantity": 1}]},
    }
    agg = {1: {"build": True, "runs": 1, "activity": "manufacturing"},
           2: {"build": True, "runs": 20, "activity": "manufacturing"},
           3: {"build": True, "runs": 1, "activity": "manufacturing"}}
    pools = {"manufacturing": 29, "reaction": 29}
    deps = _built_deps(agg, mfg, {})
    depths = {1: 1, 2: 1, 3: 0}

    free = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    _t, by_free = build_tasks(agg, mfg, {}, free, pools, depths=depths, deps=deps)
    check("with no copy limit the slack is taken in full", max(t.runs for t in by_free[2]) == 10)

    # Copies the plan would BUY carry 5 runs each — one contract is one copy.
    buying = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                         bp_acquire={2: {"kind": "bpc", "price": 1.0, "runs_per_copy": 5}})
    _t2, by_buy = build_tasks(agg, mfg, {}, buying, pools, depths=depths, deps=deps)
    check("a bought copy's run count caps the job", max(t.runs for t in by_buy[2]) == 5)
    check("and the batch is still built in full", sum(t.runs for t in by_buy[2]) == 20)

    # A copy the account already HOLDS, with runs left on it.
    held = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                       owned={2: {"me": 0, "te": 0, "kind": "bpc", "runs": 4}})
    _t3, by_held = build_tasks(agg, mfg, {}, held, pools, depths=depths, deps=deps)
    check("so does a copy you already own", max(t.runs for t in by_held[2]) == 4)

    # An original has no run limit of its own — only the blueprint type's per-job cap applies.
    bpo = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                      owned={2: {"me": 0, "te": 0, "kind": "bpo", "runs": -1}})
    _t4, by_bpo = build_tasks(agg, mfg, {}, bpo, pools, depths=depths, deps=deps)
    check("an original is not limited that way", max(t.runs for t in by_bpo[2]) == 10)

    # Reactions have no blueprint at all, so nothing here may touch them.
    rx = {9: {"base_time": 3600, "max_runs": 0, "output_qty": 1, "inputs": []}}
    ragg = {9: {"build": True, "runs": 20, "activity": "reaction"}}
    _t5, by_rx = build_tasks(ragg, {}, rx, buying, pools, depths={9: 0}, deps={9: set()})
    check("a reaction is never capped by a blueprint it doesn't have",
          sum(t.runs for t in by_rx[9]) == 20)


def test_jobs_are_aligned_to_the_plans_pace_not_stretched_past_it():
    """The reported wave, and both mistakes it exposed.

    First: slack said a 4-run reaction could become ONE 10h 11m job, which put that item seven and a
    half hours further away and held a slot for all of it. Compaction fills up to the pace the plan
    already runs at; it must never set a new one.

    Second: refusing to overshoot that pace AT ALL left the same item as four 1-run jobs, because
    two runs came to 5h 06m against a 5h 05m pace — four slots held for the sake of sixty seconds.
    Runs are indivisible and rarely divide the pace evenly, so a sliver of overshoot is allowed. The
    result is what was asked for: everything lands together, and there is one moment to log in at."""
    print("test_jobs_are_aligned_to_the_plans_pace_not_stretched_past_it")
    H = 3600
    rx = {100: {"base_time": int(2.546 * H), "max_runs": 0, "output_qty": 1, "inputs": []},
          200: {"base_time": int(2.533 * H), "max_runs": 0, "output_qty": 1, "inputs": []},
          300: {"base_time": int(2.533 * H), "max_runs": 0, "output_qty": 1, "inputs": []}}
    mfg = {1: {"base_time": H, "max_runs": 1, "output_qty": 1,
               "inputs": [{"type_id": t, "quantity": 1} for t in (100, 200, 300)]}}
    agg = {1: {"build": True, "runs": 1, "activity": "manufacturing"},
           100: {"build": True, "runs": 4, "activity": "reaction"},
           200: {"build": True, "runs": 14, "activity": "reaction"},
           300: {"build": True, "runs": 2, "activity": "reaction"}}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 9, "reaction": 10}
    deps = _built_deps(agg, mfg, rx)
    tasks, by = build_tasks(agg, mfg, rx, params, pools, depths=_depths([1], mfg, rx), deps=deps)

    pace = 2 * 2.533 * H                      # the 14-run item's own split sets it
    check("the 4-run item is aligned to the pace, not stretched past it",
          len(by[100]) == 2 and by[100][0].runs == 2)
    check("the item that sets the pace is left alone", len(by[200]) == 7)
    check("and the 2-run item is compacted onto it", len(by[300]) == 1 and by[300][0].runs == 2)
    check("nothing runs more than a sliver past the pace",
          max(t.duration for t in tasks) <= pace * 1.05 + 1)
    # Ten or twenty minutes of misalignment is immaterial to someone who logs in once to set
    # everything going; seconds of it are not worth a slot. So the allowance has a floor.
    from app.industry.schedule import _ALIGN_FLOOR
    check("with a floor generous enough to actually buy a slot", _ALIGN_FLOOR >= 15 * 60)
    # A job holding ONE run can only grow by taking a second, which is a 100% increase by
    # definition. An allowance below that is arithmetically incapable of merging such a job, however
    # much slack it has — measured on a real Archon, 5% left 232 jobs where 100% leaves 159.
    from app.industry.schedule import _PACE_OVERSHOOT
    check("and an allowance that can actually reach the next whole run", _PACE_OVERSHOOT >= 1.0)
    # The bound that actually matters commercially: a builder quoting 8 days against a competitor's
    # 14 cannot spend hours of delivery to save logins. Overshoot is capped by a slice of the whole
    # build, so it can never be the difference between winning a contract and losing it.
    prio0 = {t: (0, 0.0) for t in agg}
    packed_span = schedule(tasks, by, deps, pools, prio0)["makespan_hours"]
    wide0, wideby0 = build_tasks(agg, mfg, rx, params, pools)
    wide_span = schedule(wide0, wideby0, deps, pools, prio0)["makespan_hours"]
    check("and delivery is never meaningfully later for it",
          packed_span <= wide_span * 1.01 + 0.02)
    check("so the whole wave lands within minutes of itself",
          max(t.duration for t in tasks) - min(t.duration for t in by[200]) < 0.1 * H)

    wide, wide_by = build_tasks(agg, mfg, rx, params, pools)
    check("and it holds fewer slots than spreading everything wide", len(tasks) < len(wide))
    prio = {t: (0, 0.0) for t in agg}
    check("without costing time — here it saves it, since the wide split contends for slots",
          schedule(tasks, by, deps, pools, prio)["makespan_hours"]
          <= schedule(wide, wide_by, deps, pools, prio)["makespan_hours"] + 0.05)


def test_slack_travels_down_a_chain_not_just_one_level():
    """Reported from a real plan: four things in the same wave finishing at 2h 32m, 5h 05m, 2h 47m
    and 10h 11m — four moments to log in at, from work that could have landed together. They fed
    DIFFERENT consumers, and the first rule capped each component at its consumer's EARLIEST start,
    so a component whose consumer was itself off the critical path inherited nothing.

    The deadline has to be when the consumer must START, which is only known once that consumer has
    itself been stretched — hence the backward pass runs consumers first."""
    print("test_slack_travels_down_a_chain_not_just_one_level")
    H = 3600
    # 40 -> 20 -> root, with 30 the long branch. 20 is off the critical path by nine hours, so 40
    # can take that room too: one job of four runs instead of four jobs of one.
    mfg = {1: {"base_time": H, "max_runs": 1, "output_qty": 1,
               "inputs": [{"type_id": 20, "quantity": 1}, {"type_id": 30, "quantity": 1}]},
           20: {"base_time": H, "max_runs": 10, "output_qty": 1,
                "inputs": [{"type_id": 40, "quantity": 1}]},
           30: {"base_time": 10 * H, "max_runs": 1, "output_qty": 1, "inputs": []},
           40: {"base_time": H, "max_runs": 10, "output_qty": 1, "inputs": []}}
    agg = {t: {"build": True, "runs": 1, "activity": "manufacturing"} for t in (1, 20, 30)}
    agg[40] = {"build": True, "runs": 4, "activity": "manufacturing"}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 10, "reaction": 10}
    deps = _built_deps(agg, mfg, {})

    tasks, by_type = build_tasks(agg, mfg, {}, params, pools, depths=_depths([1], mfg, {}), deps=deps)
    check("a component two levels below the long branch inherits its slack",
          len(by_type[40]) == 1 and by_type[40][0].runs == 4)
    check("the long branch itself is untouched", len(by_type[30]) == 1)

    # And the whole point: it costs nothing. Same finish, fewer slots held.
    wide, wide_by = build_tasks(agg, mfg, {}, params, pools)
    prio = {t: (0, 0.0) for t in agg}
    check("with no time lost anywhere",
          approx(schedule(wide, wide_by, deps, pools, prio)["makespan_hours"],
                 schedule(tasks, by_type, deps, pools, prio)["makespan_hours"], 0.02))
    check("and fewer slots held to do it", len(tasks) < len(wide))


def test_a_job_is_lifted_to_land_with_what_runs_beside_it():
    """A window says how long a job MAY take; it cannot say when to LAND, and a builder logs in at
    landings. Reported off a real Archon: Hypnagogic Neurolink Enhancer at 10h 11m beside Sulfuric
    Acid at 7h 39m and Oxy-Organic Solvents at 5h 05m — three trips to start work one could cover.

    The shape that defeats the per-job allowance: `quick` is on the critical path, so its consumer
    needs it almost immediately and its window stays at its own length. The allowance can only add
    ONE run to that, which is why widening it (2% to 100%) moved nothing — `quick` needed two. Only
    a target reaches the pace its cohort is already running at."""
    print("test_a_job_is_lifted_to_land_with_what_runs_beside_it")
    H = 3600.0
    rx = {10: {"base_time": H, "output_qty": 1, "inputs": []}}
    mfg = {1: {"base_time": H, "max_runs": 1, "output_qty": 1,
               "inputs": [{"type_id": 20, "quantity": 1}, {"type_id": 21, "quantity": 1}]},
           # Long, and fed by `quick` — this is what squeezes 10's deadline to nothing.
           20: {"base_time": 20 * H, "max_runs": 1, "output_qty": 1,
                "inputs": [{"type_id": 10, "quantity": 1}]},
           21: {"base_time": H, "max_runs": 1, "output_qty": 1,
                "inputs": [{"type_id": 11, "quantity": 1}]},
           # Sets the cohort's pace at 3h and is pinned there by its copy's run cap, so it cannot
           # drift while the thing beside it is being lifted onto it.
           11: {"base_time": H, "max_runs": 3, "output_qty": 1, "inputs": []}}
    agg = {1: {"build": True, "runs": 1, "activity": "manufacturing"},
           20: {"build": True, "runs": 1, "activity": "manufacturing"},
           21: {"build": True, "runs": 1, "activity": "manufacturing"},
           10: {"build": True, "runs": 8, "activity": "reaction"},          # quick, 1h a run
           11: {"build": True, "runs": 30, "activity": "manufacturing"}}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 10, "reaction": 10}
    depths, deps = _depths([1], mfg, rx), _built_deps(agg, mfg, rx)

    _t, plain = build_tasks(agg, mfg, rx, params, pools, depths=depths, deps=deps, align=False)
    _t2, by = build_tasks(agg, mfg, rx, params, pools, depths=depths, deps=deps)

    pace = max(t.duration for t in by[11])
    check("the pace-setter is left alone", len(by[11]) == len(plain[11]))
    check("the allowance alone could not reach it",
          max(t.duration for t in plain[10]) < pace - 1e-6)
    check("and the quick one is lifted to land with it",
          approx(max(t.duration for t in by[10]), pace, 1e-6))
    check("in fewer slots than it held before", len(by[10]) < len(plain[10]))
    check("with none of its runs dropped", sum(t.runs for t in by[10]) == 8)
    check("a deliverable is never lifted", len(by[1]) == len(plain[1]) == 1)


def test_a_deliverable_is_never_paced_against_the_rest_of_the_queue():
    """Slack is for components, whose deadline is the job that eats them. A type with no consumer is
    a DELIVERABLE and answers to itself: pacing a finished product against the slowest thing in the
    queue trades the one number a customer feels for slots nobody asked to free. Caught by trimming
    tests — a 20-run product taking an hour alone became a ten-hour job beside a 100-hour order."""
    print("test_a_deliverable_is_never_paced_against_the_rest_of_the_queue")
    mfg = {1: {"base_time": 3600, "max_runs": 10, "output_qty": 1, "inputs": []},
           2: {"base_time": 360000, "max_runs": 1, "output_qty": 1, "inputs": []}}
    agg = {1: {"build": True, "runs": 20, "activity": "manufacturing"},
           2: {"build": True, "runs": 1, "activity": "manufacturing"}}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 20, "reaction": 20}

    alone, by_alone = build_tasks({1: agg[1]}, mfg, {}, params, pools, depths={1: 0}, deps={1: set()})
    both, by_both = build_tasks(agg, mfg, {}, params, pools, depths={1: 0, 2: 0},
                                deps=_built_deps(agg, mfg, {}))
    check("a deliverable finishes as fast queued as it does alone",
          approx(max(t.duration for t in by_both[1]), max(t.duration for t in by_alone[1]), 1e-6))
    check("and is not consolidated to match a slower order", len(by_both[1]) == len(by_alone[1]))
    check("the slow order is left alone too", len(by_both[2]) == 1)


def test_stale_caches_refresh_themselves_on_the_way_in():
    """Pressing Refresh was the user's job, and forgetting it is silent: a stale job cache overstates
    free slots, stale assets tell you to buy what's already in your hangar. Each cache gets its own
    threshold (a full asset list is heavy; jobs are cheap and move hourly), and an attempt is stamped
    whether or not it worked — age only advances on SUCCESS, so a permanently-failing character would
    otherwise be retried on every single tab open forever."""
    print("test_stale_caches_refresh_themselves_on_the_way_in")
    import inspect
    from app.industry import freshness as F

    check("the thresholds are per cache, not one global number",
          len(set(F._THRESHOLDS.values())) == len(F._THRESHOLDS))
    check("the heavy asset scan is the least eager",
          F._THRESHOLDS["assets"] > F._THRESHOLDS["jobs"])
    check("blueprints, which barely change, are the least eager of all",
          F._THRESHOLDS["blueprints"] > F._THRESHOLDS["assets"])

    # Never fetched at all is NOT stale: an account with no connected character must not have a
    # refresh attempted on its behalf every time it opens the tab.
    real = F.cache_ages
    try:
        F.cache_ages = lambda ctx: {"jobs": None, "assets": None, "blueprints": None}
        check("nothing cached yet is left alone", F.stale_kinds(1) == [])
        F.cache_ages = lambda ctx: {"jobs": F._THRESHOLDS["jobs"] + 1, "assets": 5,
                                    "blueprints": None}
        check("only what is actually past its own threshold is refreshed",
              F.stale_kinds(1) == ["jobs"])
    finally:
        F.cache_ages = real


def test_opening_the_tab_plans_the_queue_once_not_twice():
    """The page fetched the plan and the progress separately, and each planned the whole queue — the
    most expensive thing the tab did, done twice, for an answer the first was already holding."""
    print("test_opening_the_tab_plans_the_queue_once_not_twice")
    import inspect
    from app.industry import orders as O
    from app.industry import progress as P

    check("progress accepts a plan instead of always making one",
          inspect.signature(P.queue_progress).parameters["res"].default is None)
    check("and only plans for itself when it wasn't given one",
          "if res is None:" in inspect.getsource(P._queue_snapshot))
    # The two plans differ (progress measures the FULL requirement), so the saving is in the inputs
    # — and when no stock is netted off they are the same object and the second is skipped outright.
    run = inspect.getsource(O._run_queue_plan)
    check("the second plan reuses the resolved inputs, and is skipped when stock changes nothing",
          "want_full" in run and "res if not on_hand else" in run)


def test_building_the_borderline_set_runs_to_a_fixpoint():
    """Accepting a batch of borderline components changes the shared batch every other decision was
    weighed against, so a different set comes out borderline afterwards. A chip at a time that is
    indistinguishable from the tool inventing new work, so one press iterates until nothing new
    qualifies. Termination is the property worth pinning: the forced set only ever grows."""
    print("test_building_the_borderline_set_runs_to_a_fixpoint")
    import inspect
    from app.industry import orders as O

    src = inspect.getsource(O.force_build_above)
    check("it iterates, and stops when a round adds nothing",
          "for _ in range(_FORCE_ROUNDS)" in src and "if not fresh:" in src)
    check("the forced set only ever grows, which is what guarantees it terminates",
          "forced |= fresh" in src and "forced -=" not in src and "forced.discard" not in src)
    check("with a bound on the rounds regardless",
          isinstance(O._FORCE_ROUNDS, int) and O._FORCE_ROUNDS > 1)


def test_sourcing_notes_belong_to_one_order():
    print("test_sourcing_notes_belong_to_one_order")
    from app.industry import sourcing as S
    con, restore = _patch_db(S)
    try:
        S.ensure_sourcing_table.__wrapped__()
        S.set_sourced(1, 10, 200, 5000)
        S.set_sourced(1, 11, 200, 7)
        check("a note is stored per order", S._manual(1, 10) == {200: 5000.0})
        check("and another order's note is separate", S._manual(1, 11) == {200: 7.0})
        S.set_sourced(1, 10, 200, 0)
        check("zero clears rather than storing a nought", S._manual(1, 10) == {})
        S.set_sourced(1, 11, 201, 3)
        S.clear_order_sourcing(1, 11)
        check("deleting an order takes its notes with it", S._manual(1, 11) == {})
        check("another account sees none of it", S._manual(2, 10) == {})
    finally:
        restore()


def test_pasting_a_hangar_sets_what_is_sourced():
    """A capital build has 50+ distinct materials, so confirming them one at a time is data entry,
    not a checklist — the client's own copy is the fast, accurate answer. Two rules make the paste
    trustworthy: it REPLACES the previous note (it's a snapshot, so a material since consumed has to
    drop back), and it ignores what this build doesn't need (people select the whole hangar)."""
    print("test_pasting_a_hangar_sets_what_is_sourced")
    from app.industry import sourcing as S
    from app.industry import assets as A

    con, restore = _patch_db(S)
    # The paste parser resolves names against the SDE, which lives behind assets.get_connection.
    seeded = sqlite3.connect(":memory:")
    seeded.row_factory = sqlite3.Row
    seeded.execute("CREATE TABLE types (type_id INTEGER PRIMARY KEY, name TEXT)")
    for tid, nm in NAMES.items():
        seeded.execute("INSERT INTO types VALUES (?, ?)", (tid, nm))
    seeded.commit()
    real_assets_con = A.get_connection
    A.get_connection = lambda _c=_KeepOpen(seeded): _c
    try:
        S.ensure_sourcing_table.__wrapped__()
        wanted = {200, 201, 202}

        res = S.apply_paste(1, 10, "MineralA\t1 000\nMineralB\t40\nWidget\t2\nNotAThing\t5", wanted)
        check("materials this build needs are matched", res["matched"] == 2)
        check("and quantities survive EVE's thousands separators",
              S._manual(1, 10) == {200: 1000.0, 201: 40.0})
        check("an item the build doesn't need is ignored, not an error", res["ignored"] == 1)
        check("an unreadable name is reported", res["unknown"] == ["NotAThing"])

        # A second paste is the new truth: MineralB is gone from the pile, so it must go to zero.
        S.apply_paste(1, 10, "MineralA\t1 500", wanted)
        check("a later paste replaces rather than merges", S._manual(1, 10) == {200: 1500.0})

        check("nothing readable is reported as such",
              S.apply_paste(1, 10, "   ", wanted).get("error") == "empty")
        check("and a failed paste leaves the previous one alone", S._manual(1, 10) == {200: 1500.0})
    finally:
        A.get_connection = real_assets_con
        seeded.close()
        restore()


def test_the_sourcing_list_is_not_a_second_shopping_list():
    """The two lists are the same materials seen two ways, and they legitimately disagree: the queue
    plan nets off stock and batches shared components once across every order, while sourcing plans
    one order at its full requirement. That's fine as long as only ONE of them talks about money —
    two priced lists showing different numbers for the same item is how a page loses the reader's
    trust. Sourcing carries the shortfall's cost and nothing else."""
    print("test_the_sourcing_list_is_not_a_second_shopping_list")
    import inspect
    from app.industry import sourcing as S

    # Asserted on the row a sourcing item actually is, not on the source text — the function reads a
    # unit price to work out the shortfall's cost, and must not publish it.
    row = S._item_row({"type_id": 200, "name": "MineralA", "qty": 100.0, "unit_price": 5.0,
                       "source": "Jita", "line_cost": 500.0},
                      {200: 30.0}, {200: 10.0})
    priced = sorted(k for k in row if k in ("unit_price", "line_cost", "source", "margin", "price"))
    check("no pricing fields are published on a sourcing row", priced == [])
    check("except the shortfall's cost, which is what decides a shopping trip",
          approx(row["remaining_cost"], 350.0))                 # 70 short × 5
    check("the box beats a smaller hand-written note", row["sourced"] == 30.0)
    check("and a row isn't done until the whole requirement is", row["done"] is False)
    check("and the full requirement is what it measures against, not what's left after stock",
          "use_stock=False" in inspect.getsource(S._order_requirement))


def test_binding_a_container_lets_the_planner_spend_it():
    """"This build pulls from that box" and "the planner may count that box" were two switches, and
    only one got thrown — so the checklist said you had the materials while the shopping list beside
    it still told you to buy them. Binding throws both. Unbinding throws neither back: enabling is
    additive and one tick to undo, while auto-disabling could switch off a source the user turned on
    themselves or that another order still draws from."""
    print("test_binding_a_container_lets_the_planner_spend_it")
    import inspect
    from app.industry import sourcing as S
    from app.industry import assets as A
    from app.industry import orders as O
    from app.industry import settings as SET

    con, restore = _patch_db(A)
    try:
        A.ensure_asset_tables()
        A._store(1, {"cont:9": {"kind": "container", "name": "Archon build can", "parent": ""}},
                 {"cont:9": {200: 400.0}}, {"cont:9"}, scope="char:5")
        check("a freshly scanned container is off until it's chosen",
              A.owned_quantities(1) == {})

        # Both modules must share one database here: binding writes stock state through assets and
        # the remembered default through settings.
        real_s, real_set = S.get_connection, SET.get_connection
        S.get_connection = A.get_connection
        SET.get_connection = A.get_connection
        try:
            SET.ensure_industry_settings_table.__wrapped__()
            S.enable_bound_source(1, "cont:9")
            check("binding a build to it lets the planner spend it",
                  A.owned_quantities(1) == {200: 400.0})
            # ...and the next build starts with the same answer already filled in, because a builder
            # running a can per build otherwise answers this question on every single order.
            check("the container is remembered as the account's default",
                  SET.get_settings(1)["last_source_key"] == "cont:9")
            S.enable_bound_source(1, "cont:11")
            check("and the default follows the most recent choice",
                  SET.get_settings(1)["last_source_key"] == "cont:11")
        finally:
            S.get_connection, SET.get_connection = real_s, real_set

        # Unbinding is the caller passing an empty key. It must be a no-op, not a switch-off.
        S.enable_bound_source(1, "")
        check("unbinding leaves the source alone", A.owned_quantities(1) == {200: 400.0})
        check("a key that doesn't exist is harmless", S.enable_bound_source(1, "cont:404") is None)
    finally:
        restore()



def test_first_run_setup_shows_once_and_never_to_an_established_user():
    """The setup screen is remembered per ACCOUNT (a browser flag would re-ask on every device and
    forget on a cache clear), and the migration that adds it must not hand a first-run screen to
    someone who has been using the tab for months — anyone with saved build options has obviously
    been here before. That backfill has to survive a pod restart, which it does only because an
    un-onboarded account owns no settings row at all."""
    print("test_first_run_setup_shows_once_and_never_to_an_established_user")
    from app.industry import settings as S
    con, restore = _patch_db(S)
    try:
        S.ensure_industry_settings_table.__wrapped__()

        # An established user: settings saved, flag never set (i.e. rows predating this feature).
        con.execute("INSERT INTO pp_industry_settings (context_id, margin_pct, updated_at) "
                    "VALUES (1, 10.0, 123.0)")
        # A user part-way through setup owns no row at all — that's what makes the backfill safe.
        con.commit()
        S.ensure_industry_settings_table.__wrapped__()      # re-run, as a restart would
        check("an established account is treated as already set up",
              S.get_settings(1)["onboarded"] is True)
        check("an account that has never saved anything is not",
              S.get_settings(2)["onboarded"] is False)

        # Completing setup is its own write, so a debounced save of the plan form can't set it...
        S.set_blacklist(2, [34])
        check("another write doesn't quietly complete setup for you",
              S.get_settings(2)["onboarded"] is False)
        # ...and completing it doesn't disturb what else is stored.
        S.complete_onboarding(2)
        check("completing setup sticks", S.get_settings(2)["onboarded"] is True)
        check("and leaves the rest of the account's settings alone",
              S.get_settings(2)["never_build_ids"] == [34])

        # The admin replay. It must write 0, not NULL: the backfill claims NULL rows, so a NULL
        # would be silently undone on the next restart and the screen would never appear.
        S.reset_onboarding(2)
        check("an admin can replay the setup screen", S.get_settings(2)["onboarded"] is False)
        S.ensure_industry_settings_table.__wrapped__()
        check("and a restart does not undo the replay", S.get_settings(2)["onboarded"] is False)
        # Gated on the dependency itself, not on the source reading like it is.
        import inspect
        from app.esi import require_admin, require_context
        dep = inspect.signature(S.reset_onboarding).parameters["ctx"].default
        check("resetting is admin-gated", getattr(dep, "dependency", None) is require_admin)
        check("while completing it is open to any logged-in user",
              getattr(inspect.signature(S.complete_onboarding).parameters["ctx"].default,
                      "dependency", None) is require_context)
    finally:
        restore()


def test_the_customer_sees_the_same_progress_the_builder_does():
    """Twice this path drifted by keeping its own copy of a rule: marking a step done moved the
    builder's bar and not the customer's, then the builder's headline moved to job-time weighting
    while the share still counted runs (10% against 48%, same build, same moment). Both views must
    combine the same signals the same way, and a mark has to drop the share's cached page."""
    print("test_the_customer_sees_the_same_progress_the_builder_does")
    import inspect
    from app.industry import shares as SH
    from app.industry import progress as P

    src = inspect.getsource(SH.build_status)
    check("the share combines all three signals through the shared rules, not its own copies",
          "_manual_by_type" in src and "resolve_done(" in src and "_hours_by_type" in src)
    check("marking a step drops every cached customer page on the account",
          "invalidate_context_shares" in inspect.getsource(P.industry_mark_done))
    check("a marked step reads as done with no ledger and no stock", P.resolve_done(8, 0, 0, 8) == 8)


def test_a_scan_retires_stock_that_is_no_longer_there():
    """Counting stock you cannot draw from makes the planner build too little and the shopping list
    miss materials — the asymmetric error this module is built to avoid. So a re-scan replaces
    everything that scan owns, not merely what it happened to find this time: a container that has
    since been emptied has to disappear, not keep its last known contents forever."""
    print("test_a_scan_retires_stock_that_is_no_longer_there")
    from app.industry import assets as A
    con, restore = _patch_db(A)
    try:
        A.ensure_asset_tables()   # not @ensure_once — safe to call against the patched DB
        A._store(1, {"char:5": {"kind": "hangar", "name": "Alt", "parent": ""},
                     "cont:9": {"kind": "container", "name": "Build box", "parent": ""}},
                 {"char:5": {200: 10.0}, "cont:9": {201: 5.0}},
                 {"char:5", "cont:9"}, scope="char:5")
        A.set_sources(1, ["char:5", "cont:9"], True)
        check("both sources are counted", A.owned_quantities(1) == {200: 10.0, 201: 5.0})

        # Next scan: the container is gone from the answer entirely.
        A._store(1, {"char:5": {"kind": "hangar", "name": "Alt", "parent": ""}},
                 {"char:5": {200: 12.0}}, {"char:5"}, scope="char:5")
        check("the emptied container is retired", A.owned_quantities(1) == {200: 12.0})
        check("and it is gone from the source list",
              [s["key"] for s in A.list_sources(1)] == ["char:5"])
        check("the surviving source keeps being switched on",
              A.list_sources(1)[0]["enabled"] is True)

        # A different scan's sources are none of its business.
        A._store(1, {"corp:7:h1": {"kind": "hangar", "name": "Corp — Ore", "parent": ""}},
                 {"corp:7:h1": {202: 1.0}}, {"corp:7:h1"}, scope="corp:7")
        check("a corp scan leaves the personal one alone",
              {s["key"] for s in A.list_sources(1)} == {"char:5", "corp:7:h1"})
        check("corp sources are flagged as such",
              [s["corp"] for s in A.list_sources(1) if s["key"].startswith("corp:")] == [True])
    finally:
        restore()


def test_ordinary_users_are_not_asked_for_director_permissions():
    """Corp assets and division names are gated behind the Director role, so for almost every player
    they are permissions that can never be used — and each is a line on the consent screen they must
    agree to before they can plan anything. They were folded into the one unified superset when corp
    hangars shipped, which meant every single login asked a whole userbase for corporation-wide read
    access so the occasional director could skip a copy-paste. They now belong to their own flow."""
    print("test_ordinary_users_are_not_asked_for_director_permissions")
    from app.esi import (REACTIONS_SCOPES, INDUSTRY_SCOPES, MARKET_SCOPES, SCOPES,
                         DIRECTOR_SCOPES, CORP_ASSETS_SCOPE, CORP_DIVISIONS_SCOPE)

    for name, scopes in (("the base login", SCOPES), ("reactions", REACTIONS_SCOPES),
                         ("industry", INDUSTRY_SCOPES), ("markets", MARKET_SCOPES)):
        check(f"{name} does not ask for corp assets", CORP_ASSETS_SCOPE not in scopes)
        check(f"{name} does not ask for division names", CORP_DIVISIONS_SCOPE not in scopes)

    check("the director flow asks for both", CORP_ASSETS_SCOPE in DIRECTOR_SCOPES
          and CORP_DIVISIONS_SCOPE in DIRECTOR_SCOPES)
    # A strict superset, or connecting a director would strip the scopes every other tool relies on
    # — the exact silo bug the single-superset rule exists to prevent.
    missing = [s for s in REACTIONS_SCOPES.split() if s not in DIRECTOR_SCOPES.split()]
    check("and keeps everything a normal character has", missing == [])


def test_corp_hangars_and_containers_split_like_personal_ones():
    """Corp stock goes through the same bucketing as personal stock — one item belongs to exactly
    one source, the container it sits in if any, otherwise the hangar. Anything that isn't a corp
    hangar division (deliveries, a ship's hold) is not stock a job can be fed from."""
    print("test_corp_hangars_and_containers_split_like_personal_ones")
    from app.industry.assets import _split_by_source, _CORP_FLAGS

    rows = [
        {"item_id": 1, "type_id": 200, "quantity": 100, "location_id": 60000, "location_flag": "CorpSAG1"},
        {"item_id": 2, "type_id": 0, "quantity": 1, "location_id": 60000, "location_flag": "CorpSAG2"},
        {"item_id": 3, "type_id": 201, "quantity": 7, "location_id": 2, "location_flag": "Unlocked"},
        # Not stock: the corp deliveries hangar, and a module in a ship parked in a corp hangar.
        {"item_id": 4, "type_id": 202, "quantity": 5, "location_id": 60000, "location_flag": "CorpDeliveries"},
        {"item_id": 5, "type_id": 0, "quantity": 1, "location_id": 60000, "location_flag": "CorpSAG1"},
        {"item_id": 6, "type_id": 203, "quantity": 3, "location_id": 5, "location_flag": "Cargo"},
    ]
    srcs, stock, conts = _split_by_source(
        rows, set(_CORP_FLAGS),
        lambda f: f"corp:7:h{_CORP_FLAGS[f]}",
        lambda f: f"Corp — hangar {_CORP_FLAGS[f]}",
        cont_key=lambda loc: f"corp:7:c{loc}")

    check("a hangar division becomes one source", stock.get("corp:7:h1", {}).get(200) == 100.0)
    check("a container inside it is its own source", stock.get("corp:7:c2", {}).get(201) == 7.0)
    check("the container is reported for naming", conts == [2])
    check("its parent hangar is named", srcs["corp:7:c2"]["parent"] == "Corp — hangar 2")
    check("deliveries are not usable stock", not any(202 in s for s in stock.values()))
    check("a ship's cargo is not usable stock", not any(203 in s for s in stock.values()))
    check("corp keys never collide with personal ones",
          all(k.startswith("corp:7:") for k in srcs))


def test_the_step_by_step_parts_account_for_the_whole():
    """Reported on a real 2× Phoenix queue: the summary said the build takes ~13d 12h while the
    steps above it topped out at "+14h", and nothing on the screen explained the gap.

    Both numbers were right. The steps render each stage's START offset into one wall clock, and
    the finished-hull job — 12d 21h of the 13d 12h — appeared only inside a collapsed "show items"
    fold. Two numbers disagreeing on one screen with no reconciliation is the defect, so the
    invariant is that the collapsed-stage view the renderer builds accounts for the makespan.

    Pinned in two halves: the schedule must CARRY what the reconciliation needs (a stage's last
    landing is the makespan), and `_indStepsHtml` must actually render it. The trap is asserted
    too — summing the start offsets is nowhere near the total, and always will be, so the fix can
    never be "make the steps add up".
    """
    print("test_the_step_by_step_parts_account_for_the_whole")
    import os
    import re
    H = 3600
    # One short component stage feeding one very long final job — the Phoenix shape.
    rx = {}
    mfg = {1: {"base_time": 300 * H, "max_runs": 10, "output_qty": 1,
               "inputs": [{"type_id": 2, "quantity": 1}]},
           2: {"base_time": 2 * H, "max_runs": 10, "output_qty": 1, "inputs": []}}
    agg = {1: {"build": True, "runs": 2, "activity": "manufacturing"},
           2: {"build": True, "runs": 2, "activity": "manufacturing"}}
    params = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0)
    pools = {"manufacturing": 4, "reaction": 0}
    deps = _built_deps(agg, mfg, rx)
    tasks, by = build_tasks(agg, mfg, rx, params, pools, depths=_depths([1], mfg, rx), deps=deps)
    sched = schedule(tasks, by, deps, pools, {t: (0, 0.0) for t in agg})
    waves = sched["waves"]
    makespan = sched["makespan_hours"]

    # The renderer's own collapse: one row per stage, min start / max landing / longest job.
    stage_of = {2: 0, 1: 1}
    stages = {}
    for w in waves:
        for t in w["tasks"]:
            s = stages.setdefault(stage_of[t["type_id"]], {"start": float("inf"), "end": 0.0, "longest": 0.0})
            s["start"] = min(s["start"], w["start_hours"])
            s["end"] = max(s["end"], w["start_hours"] + t["duration_hours"])
            s["longest"] = max(s["longest"], t["duration_hours"])

    check("every scheduled job reports the length the steps render",
          all(t.get("duration_hours") is not None for w in waves for t in w["tasks"]))
    check("the last stage to land IS the build's length",
          abs(max(s["end"] for s in stages.values()) - makespan) < 0.02)
    check("and each stage lands after it starts, so start+longest explains it",
          all(s["end"] <= s["start"] + s["longest"] + 0.02 for s in stages.values()))
    # The trap, stated as an invariant so nobody "fixes" it by making the offsets sum.
    check("summing the start offsets is NOT the build's length",
          sum(s["start"] for s in stages.values()) < makespan * 0.2)

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "industry.js"), encoding="utf-8").read()
    body = src[src.index("function _indStepsHtml("):]
    body = body[:body.index("\nfunction ", 1)]
    check("the step collapse tracks when a stage has landed", re.search(r"\bs\.end\s*=", body))
    check("and the longest job inside it", re.search(r"\bs\.longest\s*=", body))
    check("a step header renders that job length, not only its start offset",
          "s.longest" in body.split("html +=", 1)[1] and "s.end" in body.split("html +=", 1)[1])
    # The total has to say what it measures. Left bare, it reads as the sum of the steps above it.
    done = body[body.index("ind-step-done"):]
    check("the total says it is wall clock, not a sum of the steps",
          "not lengths to add up" in done)
    check("and names the step that drives it", "driver" in done)



def _seed_blueprint_cache(dbcon, items, context_id=1, character_id=7):
    """One character's ESI blueprint cache, as `owned_blueprints` reads it.
    `items`: [(blueprint_type_id, quantity, runs, me, te)] — raw ESI shape, quirks included."""
    dbcon.executescript(
        """
        CREATE TABLE IF NOT EXISTS blueprints (blueprint_type_id INTEGER PRIMARY KEY,
            product_type_id INTEGER, output_qty INTEGER, base_time INTEGER, max_runs INTEGER);
        CREATE TABLE IF NOT EXISTS pp_characters (character_id INTEGER PRIMARY KEY,
            context_id INTEGER);
        CREATE TABLE IF NOT EXISTS pp_char_blueprints (character_id INTEGER PRIMARY KEY,
            blueprints_json TEXT NOT NULL DEFAULT '[]', fetched_at REAL);
        """)
    dbcon.execute("INSERT OR REPLACE INTO blueprints VALUES (1000, 100, 1, 3600, 100)")
    dbcon.execute("INSERT OR REPLACE INTO pp_characters VALUES (?, ?)", (character_id, context_id))
    rows = [{"type_id": bt, "quantity": q, "runs": r, "me": me, "te": te}
            for bt, q, r, me, te in items]
    dbcon.execute("INSERT OR REPLACE INTO pp_char_blueprints VALUES (?, ?, 0)",
                  (character_id, json.dumps(rows)))
    dbcon.commit()


def test_every_copy_the_account_holds_counts():
    """Reported: the planner ignores blueprint copies whose ME/TE doesn't match. It did — worse, it
    ignored every copy but ONE. `owned_blueprints` collapsed a product's whole holding down to the
    single best print and threw the rest away, so an account holding 14 Capital Armor Plates copies
    worth 212 runs was credited with 5 and told to go and buy the other 207 it already had."""
    print("test_every_copy_the_account_holds_counts")
    import app.industry.blueprints as B
    dbcon, restore = _patch_db(B)
    try:
        _seed_blueprint_cache(dbcon, [
            (1000, -2, 5, 10, 20),      # the best-researched copy — all the old code ever saw
            (1000, -2, 30, 8, 16),
            (1000, -2, 25, 0, 0),
        ])
        own = B.owned_blueprints(1)[100]
        check("every copy is kept", own["copy_count"] == 3)
        check("coverage is the SUM of their runs, not the best one's", own["runs"] == 60)
        check("and they are ordered best-researched first",
              [c["me"] for c in own["copies"]] == [10, 8, 0])
    finally:
        restore()

    # ...and that coverage is what the batch is measured against.
    mfg = {1: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []}}
    prices, adj = _prices({1: 5_000_000.0}), {1: 1.0}
    pools = {"manufacturing": 5, "reaction": 5}
    copies = [{"me": 10, "te": 20, "kind": "bpc", "runs": 5},
              {"me": 0, "te": 0, "kind": "bpc", "runs": 30}]
    held = {1: {"me": 10, "te": 20, "kind": "bpc", "runs": 35, "copies": copies, "copy_count": 2}}
    p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                    owned=held, bp_acquire={1: {"kind": "bpc", "price": 10e6, "runs_per_copy": 5,
                                                "live": True,
                                                "listings": [{"runs": 5, "price": 10e6}] * 8}})
    memo, _u = resolve_unit_costs(mfg, {}, prices, adj, p)
    agg = aggregate_demand([(1, 30)], memo, mfg, {}, p, None, pools)
    check("a 30-run batch is covered by 35 runs of copies", agg[1]["runs_short"] == 0)
    check("so no copies are bought for it", agg[1].get("blueprint_cost", 0.0) == 0.0)
    agg2 = aggregate_demand([(1, 50)], memo, mfg, {}, p, None, pools)
    check("and a bigger batch is short by what the WHOLE holding leaves", agg2[1]["runs_short"] == 15)
    check("which is what gets charged for", agg2[1]["blueprint_cost"] > 0)


def test_a_stack_of_originals_is_not_a_copy_that_covers_nothing():
    """ESI's `quantity` is -1 for a singleton, -2 for a copy — and a POSITIVE number for a stack of
    ORIGINALS fresh from the market. Reading `quantity == -1` as the only original filed all of
    those as copies carrying -1 runs, i.e. as covering nothing: 26 blueprints in production, each
    telling its owner to buy a print they are holding. `runs == -1` is the unambiguous marker — a
    real copy always carries a positive run count."""
    print("test_a_stack_of_originals_is_not_a_copy_that_covers_nothing")
    import app.industry.blueprints as B
    check("a singleton original is an original", B.classify_blueprint(-1, -1) == "bpo")
    check("a STACK of originals is too", B.classify_blueprint(5, -1) == "bpo")
    check("a copy is a copy", B.classify_blueprint(-2, 10) == "bpc")
    check("and -1 runs is never a copy, whatever the quantity says",
          B.classify_blueprint(-2, -1) == "bpo")

    dbcon, restore = _patch_db(B)
    try:
        _seed_blueprint_cache(dbcon, [(1000, 5, -1, 9, 18)])
        own = B.owned_blueprints(1)[100]
        check("the stack is reported as an original", own["kind"] == "bpo")
        check("and an original covers any batch", own["runs"] == -1)
        mfg = {100: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []}}
        p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                        owned={100: own})
        memo, _u = resolve_unit_costs(mfg, {}, _prices({100: 5e6}), {100: 1.0}, p)
        agg = aggregate_demand([(100, 40)], memo, mfg, {}, p, None,
                               {"manufacturing": 5, "reaction": 5})
        check("so nothing is short and nothing is bought",
              agg[100]["runs_short"] == 0 and agg[100].get("blueprint_cost", 0.0) == 0.0)
        check("and its research is used, not ME 0",
              p.me_te_for(100, "manufacturing", 40) == (9, 18))
    finally:
        restore()


def test_each_job_runs_off_the_copy_it_is_installed_on():
    """ME/TE is a property of the COPY a job runs on, so jobs of one type in one batch legitimately
    differ — the best copies are consumed first. The aggregate the demand pass uses is then a
    runs-weighted figure over exactly those copies (crediting the whole batch to the best copy in
    the drawer over-states what it saves), and runs no copy covers are built at whatever the plan
    would buy."""
    print("test_each_job_runs_off_the_copy_it_is_installed_on")
    mfg = {2: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []}}
    agg = {2: {"build": True, "runs": 6, "activity": "manufacturing"}}
    pools = {"manufacturing": 6, "reaction": 6}
    copies = [{"me": 10, "te": 20, "kind": "bpc", "runs": 2},
              {"me": 0, "te": 0, "kind": "bpc", "runs": 4}]
    p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                    owned={2: {"me": 10, "te": 20, "kind": "bpc", "runs": 6, "copies": copies}})
    _t, by = build_tasks(agg, mfg, {}, p, pools)
    jobs = by[2]
    check("every run is still built", sum(t.runs for t in jobs) == 6)
    check("the best copies are installed first",
          [t.te for t in jobs][:2] == [20, 20] and [t.te for t in jobs][2:] == [0, 0, 0, 0])
    check("and a job's LENGTH follows its own copy",
          approx(jobs[0].duration, 2880.0) and approx(jobs[-1].duration, 3600.0))

    check("the batch figure is runs-weighted over the copies it spends",
          approx(p.me_te_for(2, "manufacturing", 6)[0], (10 * 2 + 0 * 4) / 6))
    check("not the best copy's, which would over-credit the materials",
          p.me_te_for(2, "manufacturing", 6)[0] < 10)
    check("a batch the first copy covers alone IS the first copy",
          p.me_te_for(2, "manufacturing", 2) == (10, 20))

    # Runs past everything owned are built off the copy the plan would BUY.
    p2 = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                     owned={2: {"me": 10, "te": 20, "kind": "bpc", "runs": 2,
                                "copies": copies[:1]}},
                     buy_me_te={2: (4, 8)},
                     bp_acquire={2: {"kind": "bpc", "price": 1.0, "runs_per_copy": 2}})
    # Three slots, so the six runs land as three 2-run jobs — one per copy, which is the point.
    _t2, by2 = build_tasks(agg, mfg, {}, p2, {"manufacturing": 3, "reaction": 3})
    check("the owned copy runs first, the bought ones after",
          [t.te for t in by2[2]] == [20, 8, 8])
    check("no job carries more runs than one copy", max(t.runs for t in by2[2]) == 2)
    check("and the shortfall is weighed at what it will really be built at",
          approx(p2.me_te_for(2, "manufacturing", 6)[0], (10 * 2 + 4 * 4) / 6))

    # The blend itself, stated directly.
    check("an original answers for the whole batch",
          blend_me_te([{"me": 3, "te": 6, "runs": -1}], 100, (0, 0)) == (3, 6))
    check("and for a batch of unknown size too",
          blend_me_te([{"me": 3, "te": 6, "runs": -1}], None, (0, 0)) == (3, 6))


def test_an_override_still_beats_every_copy_you_own():
    """Precedence is user override > owned blueprint > contract copy > ME 0/TE 0, and per-copy
    assignment must not quietly outrank the one thing the user said explicitly."""
    print("test_an_override_still_beats_every_copy_you_own")
    mfg = {2: {"base_time": 3600, "max_runs": 100, "output_qty": 1, "inputs": []}}
    agg = {2: {"build": True, "runs": 4, "activity": "manufacturing"}}
    copies = [{"me": 10, "te": 20, "kind": "bpc", "runs": 2},
              {"me": 0, "te": 0, "kind": "bpc", "runs": 2}]
    p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                    owned={2: {"me": 10, "te": 20, "kind": "bpc", "runs": 4, "copies": copies}},
                    me_by_product={2: (7.0, 14.0)}, me_source={2: "override"})
    check("the override is what the batch is costed at",
          p.me_te_for(2, "manufacturing", 4) == (7.0, 14.0))
    check("and the copies are not consulted at all", p.copies_for(2, "manufacturing") == [])
    _t, by = build_tasks(agg, mfg, {}, p, {"manufacturing": 4, "reaction": 4})
    check("every job runs at the overridden efficiency",
          all(t.te == 14.0 for t in by[2]) and len(by[2]) == 4)


def test_a_single_copy_account_plans_exactly_as_before():
    """The deploy guarantee: an account holding ONE print for a product plans identically however
    the holding is described — the multi-copy path has to reduce to the old one, byte for byte."""
    print("test_a_single_copy_account_plans_exactly_as_before")
    con = _seed_con()
    mfg, rx = load_manufacturing_graph(con), load_reaction_graph(con)
    pools = {"manufacturing": 5, "reaction": 5}
    one = {"me": 10, "te": 20, "kind": "bpc", "runs": 4}

    def plan(owned):
        p = BuildParams(mfg_skill_time_mult=1.0, rx_skill_time_mult=1.0, struct_time_mult=1.0,
                        owned=owned)
        return plan_queue([(100, 3)], mfg, rx, _prices(SELL), ADJ, p, NAMES, pools)

    summary_only = plan({100: dict(one)})
    with_copies = plan({100: dict(one, copies=[dict(one)], copy_count=1)})

    def _blind(res):
        """Same plan, minus the one thing that is genuinely new: how many copies were counted."""
        r = json.loads(json.dumps(res))
        for req in r["requirements"]:
            (req.get("blueprint") or {}).pop("copy_count", None)
        return json.dumps(r, sort_keys=True)

    check("the whole plan is identical", _blind(summary_only) == _blind(with_copies))
    check("the count of copies counted is reported",
          {r["type_id"]: r for r in with_copies["requirements"]}[100]["blueprint"]["copy_count"] == 1)
    check("and it still uses the copy's research",
          {r["type_id"]: r for r in with_copies["requirements"]}[100]["me"] == 10)
    check("the per-copy list never reaches the payload",
          "copies" not in ({r["type_id"]: r for r in with_copies["requirements"]}[100]["blueprint"]))


if __name__ == "__main__":
    main()
