"""In-process tests for the Industry make-or-buy engine (app/industry/graph.py).

Runs against a synthetic, hand-computable recipe graph seeded into an in-memory SQLite DB — no
live SDE or ESI needed, same style as test_optimizer's in-process cases. Asserts durable
invariants of the cost math: the EVE material formula, the build-vs-buy decision, shopping-list
aggregation, and the cost/time totals.

Run: python3 test_industry.py
"""
import math
import sqlite3
import sys

sys.path.insert(0, ".")

from app.industry.graph import (
    BuildParams, effective_material_qty, load_manufacturing_graph, load_reaction_graph,
    collect_reachable, build_plan, resolve_unit_costs,
)
from app.industry.schedule import (
    aggregate_demand, build_tasks, schedule, plan_queue, Task, _split_runs, _built_deps,
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


def test_split_runs():
    print("test_split_runs (BPC run cap)")
    check("unlimited", _split_runs(50, 0) == [50])
    check("under cap", _split_runs(5, 10) == [5])
    check("even split", _split_runs(20, 10) == [10, 10])
    check("uneven balanced", _split_runs(25, 10) == [9, 8, 8])


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
    test_split_runs()
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
    test_stock_reduces_plan_but_never_the_target()
    test_marginal_threshold_scales_with_build_size()
    test_install_assignment_spreads_and_respects_free_slots()
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
