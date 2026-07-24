"""Industry planner — Phase 2: demand aggregation + the parallel-slot scheduler.

Two problems on top of the Phase-1 make-or-buy cost core (graph.py):

1. DEMAND AGGREGATION across a whole queue of build orders. Rather than exploding each order into
   its own tree (which double-counts shared components), we do a single MRP-style explosion in
   low-level-code order: sum ALL demand for a type before computing its runs, so a component two
   capitals share is built in ONE combined batch. Output rounding (a reaction makes 2/run, an odd
   demand) leaves excess, carried in a stock ledger and reported per type.

2. SCHEDULING the resulting jobs into limited parallel slots. A resource-constrained list
   scheduler over the job DAG: two independent slot pools (manufacturing + reaction), priority =
   critical-path length, a job is ready the moment its BUILT inputs are complete (BOUGHT inputs are
   available at t=0). This front-loads every leaf build in parallel and pulls later-tier work
   forward as inputs land — reporting makespan, per-wave timeline, and slot occupancy.

Pure/testable: every function takes prebuilt graphs + params. The endpoint wires real data.
Slot counts are request parameters here; Phase 3 derives them per character from ESI skills and
makes the schedule persistent + queueable.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router
from app.industry.graph import (
    BuildParams, load_manufacturing_graph, load_reaction_graph, collect_reachable,
    effective_material_qty, resolve_unit_costs, SCC_SURCHARGE_PCT, resolve_build_params,
)
from app.industry.slots import _slot_pool


# ── Demand aggregation (MRP explosion) ────────────────────────────────────────────────────────

def _depths(targets: list[int], mfg: dict, rx: dict) -> dict[int, int]:
    """Low-level code per type: 0 for a target, and a built type's inputs sit one level below the
    deepest consumer that reaches them. Processing built types in ascending depth guarantees every
    consumer's demand is accumulated before the component's runs are computed. Cycle-guarded."""
    depth: dict[int, int] = {}

    def visit(tid: int, d: int, stack: frozenset):
        if tid in stack:                    # cycle guard
            return
        if d <= depth.get(tid, -1):         # already recorded at an equal-or-deeper level
            return
        depth[tid] = d
        recipe = mfg.get(tid) or rx.get(tid)
        if recipe:
            inner = stack | {tid}
            for inp in recipe["inputs"]:
                visit(inp["type_id"], d + 1, inner)

    for t in targets:
        visit(t, 0, frozenset())
    return depth


def aggregate_demand(targets: list[tuple[int, int]], memo: dict, mfg: dict, rx: dict,
                     params: BuildParams, on_hand: dict[int, float] | None = None,
                     pools: dict[str, int] | None = None) -> dict[int, dict]:
    """Combined per-type demand across all order targets. Targets are always built; every other
    type follows its make-or-buy decision in `memo`. Returns {type_id: {activity, build, gross,
    net, runs, produced, leftover, output_qty, bought_for_speed}}.

    TIME-AWARE make-or-buy: when `params.max_build_hours > 0`, a component the cost engine would
    build is flipped to BUY if producing its whole batch would take longer than that wall-clock cap
    (runs × cycle ÷ pool slots) and it's purchasable — so the slow bulk marathons (thousands of
    reaction runs) come off the market instead of dominating the makespan, while everything that
    builds fast is still produced. Prioritizes time-to-completion over cost, per the design goal."""
    on_hand = dict(on_hand or {})
    pools = pools or {}
    target_ids = {t for t, _ in targets}
    depth = _depths([t for t, _ in targets], mfg, rx)

    gross: dict[int, float] = defaultdict(float)
    for tid, qty in targets:
        gross[tid] += qty

    def is_built(tid: int) -> bool:
        if tid in target_ids:
            return (tid in mfg) or (tid in rx)          # a target is built if it *can* be
        return memo.get(tid, {}).get("decision") == "build"

    # Process built types shallowest-first so demand is fully accumulated before runs are computed.
    built = [tid for tid in depth if is_built(tid)]
    built.sort(key=lambda t: depth[t])

    result: dict[int, dict] = {}
    flipped: set[int] = set()
    for tid in built:
        recipe = mfg.get(tid) or rx.get(tid)
        activity = "manufacturing" if tid in mfg else "reaction"
        me, te = params.me_te_for(tid, activity)
        mult = (params.struct_material_mult if activity == "manufacturing"
                else params.reaction_material_mult)
        output_qty = recipe["output_qty"]
        net = max(0.0, gross[tid] - on_hand.get(tid, 0.0))
        runs = max(0, math.ceil(net / output_qty)) if net > 0 else 0
        produced = runs * output_qty

        # Time-aware flip: buy this batch instead of building if it's slow AND purchasable (never
        # flip a target — the user asked to build that).
        if (params.max_build_hours > 0 and runs > 0 and tid not in target_ids
                and (memo.get(tid) or {}).get("buy_unit_cost") is not None):
            P = max(1, pools.get(activity, 1))
            wall_h = runs * recipe["base_time"] * (1 - te / 100.0) / P / 3600.0
            if wall_h > params.max_build_hours:
                flipped.add(tid)          # falls through to the bought loop; inputs not exploded
                continue

        result[tid] = {
            "type_id": tid, "activity": activity, "build": True,
            "gross": gross[tid], "net": net, "runs": runs, "produced": produced,
            "leftover": produced - net, "output_qty": output_qty, "bought_for_speed": False,
        }
        for inp in recipe["inputs"]:
            gross[inp["type_id"]] += effective_material_qty(inp["quantity"], runs, me, mult)

    # Bought / raw types: whatever gross demand is left that isn't built.
    for tid, qty in gross.items():
        if tid in result or qty <= 0:
            continue
        result[tid] = {"type_id": tid, "activity": None, "build": False, "gross": qty,
                       "net": qty, "runs": 0, "produced": 0, "leftover": 0, "output_qty": 0,
                       "bought_for_speed": tid in flipped}
    return result


# ── Scheduling ────────────────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    task_id: str
    type_id: int
    activity: str          # "manufacturing" | "reaction" → which slot pool
    runs: int
    duration: float        # seconds
    start: float = 0.0
    end: float = 0.0
    slot: int = 0


def _split_runs(total: int, max_runs: int) -> list[int]:
    """Split a type's total runs into per-job-instance run counts, bounded by the BPC run cap
    (`max_runs`, 0 = unlimited). Each instance is one in-game job install / one slot occupancy."""
    if total <= 0:
        return []
    if not max_runs or total <= max_runs:
        return [total]
    n = math.ceil(total / max_runs)
    base, extra = divmod(total, n)
    return [base + 1 if i < extra else base for i in range(n)]


def _balanced(total: int, n: int) -> list[int]:
    base, extra = divmod(total, n)
    return [base + 1 if i < extra else base for i in range(n)]


def build_tasks(agg: dict, mfg: dict, rx: dict, params: BuildParams,
                pools: dict[str, int] | None = None) -> tuple[list[Task], dict]:
    """One or more Task instances per built type. A type's runs are split so they can run in
    PARALLEL across the pool's slots — up to `pool_size` concurrent jobs — while still respecting
    the per-BPC run cap (`max_runs`). This is what lets a big reaction (thousands of runs, no BPC
    cap → otherwise one multi-month job) actually spread across your reaction slots the way you'd
    run it in-game. Returns (tasks, tasks_by_type)."""
    pools = pools or {}
    tasks: list[Task] = []
    by_type: dict[int, list[Task]] = {}
    for tid, info in agg.items():
        if not info["build"] or info["runs"] <= 0:
            continue
        recipe = mfg.get(tid) or rx.get(tid)
        activity = info["activity"]
        max_runs = mfg[tid]["max_runs"] if tid in mfg else 0
        _me, te = params.me_te_for(tid, activity)
        per_run = recipe["base_time"] * (1 - te / 100.0)
        R = info["runs"]
        P = max(1, pools.get(activity, 1))
        cap = max_runs if max_runs else R
        # Split into enough jobs to (a) fill up to P slots concurrently and (b) never exceed the
        # per-job run cap — whichever forces MORE jobs.
        n = max(min(P, R), math.ceil(R / cap))
        for i, r in enumerate(_balanced(R, n)):
            t = Task(f"{tid}-{i}", tid, activity, r, r * per_run)
            tasks.append(t)
            by_type.setdefault(tid, []).append(t)
    return tasks, by_type


def _built_deps(agg: dict, mfg: dict, rx: dict) -> dict[int, set[int]]:
    """For each built type, the set of its inputs that are ALSO built (bought inputs are available
    at t=0, so they're not scheduling dependencies)."""
    deps: dict[int, set[int]] = {}
    for tid, info in agg.items():
        if not info["build"]:
            continue
        recipe = mfg.get(tid) or rx.get(tid)
        deps[tid] = {inp["type_id"] for inp in recipe["inputs"]
                     if agg.get(inp["type_id"], {}).get("build")}
    return deps


def _critical_priority(agg: dict, deps: dict, mfg: dict, rx: dict, params: BuildParams) -> dict[int, float]:
    """crit_to_end(type) = own duration + longest crit of any consumer — the classic list-scheduler
    priority. Higher = more urgent (on a longer path to a finished product)."""
    consumers: dict[int, set[int]] = defaultdict(set)
    for tid, dset in deps.items():
        for d in dset:
            consumers[d].add(tid)

    def type_dur(tid: int) -> float:
        info = agg[tid]
        recipe = mfg.get(tid) or rx.get(tid)
        _me, te = params.me_te_for(tid, info["activity"])
        return info["runs"] * recipe["base_time"] * (1 - te / 100.0)

    memo: dict[int, float] = {}

    def crit(tid: int, stack: frozenset) -> float:
        if tid in memo:
            return memo[tid]
        if tid in stack:
            return 0.0
        best = 0.0
        for c in consumers.get(tid, ()):
            best = max(best, crit(c, stack | {tid}))
        memo[tid] = type_dur(tid) + best
        return memo[tid]

    return {tid: crit(tid, frozenset()) for tid in deps}


def schedule(tasks: list[Task], by_type: dict, deps: dict, pools: dict[str, int],
             priority: dict[int, float]) -> dict:
    """Event-driven resource-constrained list scheduler. Fills each pool's free slots with the
    highest-priority ready tasks, advancing time to the next job completion when nothing more can
    start. Returns makespan, per-task placements, and waves (tasks grouped by start time)."""
    free = dict(pools)
    completed: set[int] = set()
    running: list[Task] = []
    started: set[str] = set()
    now = 0.0
    remaining = len(tasks)

    def type_done(tid: int) -> bool:
        return all(t.end and t.task_id in started for t in by_type[tid])

    while remaining > 0:
        # Start every ready task that fits a free slot in its pool, most-critical first.
        ready = sorted(
            (t for t in tasks if t.task_id not in started
             and all(d in completed for d in deps.get(t.type_id, ()))),
            key=lambda t: priority.get(t.type_id, 0.0), reverse=True,
        )
        started_any = False
        for t in ready:
            if free.get(t.activity, 0) > 0:
                t.slot = pools[t.activity] - free[t.activity]
                t.start = now
                t.end = now + t.duration
                free[t.activity] -= 1
                started.add(t.task_id)
                running.append(t)
                remaining -= 1
                started_any = True
        if started_any:
            # Newly started tasks don't unlock anything until they finish; fall through to advance.
            pass
        if not running:
            if not started_any:
                break  # nothing running and nothing startable → unschedulable remainder
            continue
        # Advance to the next completion(s), free those slots, mark newly-complete types.
        next_end = min(t.end for t in running)
        now = next_end
        for t in [x for x in running if x.end == next_end]:
            free[t.activity] += 1
            running.remove(t)
        for tid in by_type:
            if tid not in completed and all(x.end and x.end <= now for x in by_type[tid]):
                completed.add(tid)

    makespan = max((t.end for t in tasks if t.end), default=0.0)
    waves: dict[float, list[Task]] = defaultdict(list)
    for t in tasks:
        if t.task_id in started:
            waves[t.start].append(t)
    wave_list = [
        {"start_hours": round(s / 3600.0, 2),
         "tasks": [{"type_id": t.type_id, "activity": t.activity, "runs": t.runs,
                    "duration_hours": round(t.duration / 3600.0, 2), "slot": t.slot}
                   for t in sorted(ts, key=lambda t: t.type_id)]}
        for s, ts in sorted(waves.items())
    ]
    return {
        "makespan_hours": round(makespan / 3600.0, 2),
        "waves": wave_list,
        "unscheduled": [t.task_id for t in tasks if t.task_id not in started],
    }


# ── Orchestrator + endpoint ───────────────────────────────────────────────────────────────────

def plan_queue(targets: list[tuple[int, int]], mfg: dict, rx: dict, prices: dict, adjusted: dict,
               params: BuildParams, names: dict[int, str], pools: dict[str, int],
               on_hand: dict[int, float] | None = None) -> dict:
    """End-to-end queue plan: aggregate demand across all targets, schedule the jobs, and roll up
    cost + time metrics + a combined priced shopping list."""
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    for tid, _ in targets:
        unit(tid, frozenset())

    agg = aggregate_demand(targets, memo, mfg, rx, params, on_hand, pools)
    tasks, by_type = build_tasks(agg, mfg, rx, params, pools)
    deps = _built_deps(agg, mfg, rx)
    priority = _critical_priority(agg, deps, mfg, rx, params)
    sched = schedule(tasks, by_type, deps, pools, priority)
    for w in sched["waves"]:                       # enrich wave tasks with readable names
        for t in w["tasks"]:
            t["name"] = names.get(t["type_id"], str(t["type_id"]))

    # Cost roll-up from the aggregated demand (single batch per type — the honest, shared-batch cost).
    materials_cost = 0.0
    job_cost = 0.0
    shopping = []
    for tid, info in agg.items():
        if info["build"]:
            recipe = mfg.get(tid) or rx.get(tid)
            ci = params.mfg_cost_index if info["activity"] == "manufacturing" else params.rx_cost_index
            eiv = sum(inp["quantity"] * info["runs"] * adjusted.get(inp["type_id"], 0.0)
                      for inp in recipe["inputs"])
            job_cost += eiv * (ci + params.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT)
        elif info["gross"] > 0:
            price = (prices.get(tid) or {}).get("sell_price")
            line = (price or 0.0) * info["gross"]
            materials_cost += line
            shopping.append({
                "type_id": tid, "name": names.get(tid, str(tid)), "qty": info["gross"],
                "unit_price": price, "source": (prices.get(tid) or {}).get("source"),
                "line_cost": line if price else None,
                "bought_for_speed": info.get("bought_for_speed", False),
            })
    shopping.sort(key=lambda r: r["line_cost"] or 0.0, reverse=True)

    # Leftover intermediates (batch-rounding overproduction) are reusable/sellable inventory, not a
    # cost of the finished product — value them at their build unit cost and credit that back so the
    # net product cost excludes what we can recycle.
    leftovers = []
    leftover_value = 0.0
    for tid, info in agg.items():
        if info.get("leftover", 0) <= 0:
            continue
        uc = (memo.get(tid) or {}).get("build_unit_cost") or 0.0
        val = info["leftover"] * uc
        leftover_value += val
        leftovers.append({"type_id": tid, "name": names.get(tid, str(tid)),
                          "qty": info["leftover"], "value": round(val, 2)})
    leftovers.sort(key=lambda r: r["value"], reverse=True)
    total_cost = materials_cost + job_cost
    return {
        "targets": [{"type_id": t, "name": names.get(t, str(t)), "quantity": q} for t, q in targets],
        "schedule": sched,
        "shopping_list": shopping,
        "leftovers": leftovers,
        "unresolved": [s["type_id"] for s in shopping if s["unit_price"] is None],
        "metrics": {
            "materials_cost": round(materials_cost, 2),
            "job_cost": round(job_cost, 2),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(leftover_value, 2),
            "net_cost": round(total_cost - leftover_value, 2),
            "job_count": len(tasks),
            "build_steps": len(by_type),      # distinct things to build (parallel splits collapsed)
            "makespan_hours": sched["makespan_hours"],
            "slots": pools,
        },
    }


class QueueTarget(BaseModel):
    type_id: int
    quantity: int = 1


class IndustryQueueRequest(BaseModel):
    targets: list[QueueTarget]
    me_pct: float = 0.0
    te_pct: float = 0.0
    system_id: int | None = None
    facility_tax_pct: float | None = None
    mfg_slots: int | None = None        # None → derive from the account's characters' skills
    rx_slots: int | None = None
    prioritize_speed: bool = True


@router.post("/api/industry/plan-queue")
def industry_plan_queue(req: IndustryQueueRequest, ctx: int = Depends(require_context)):
    """Aggregate a queue of build orders, schedule the jobs across parallel slots, and report
    makespan + waves + combined shopping list + cost metrics. Own-account scoped."""
    if not req.targets:
        raise HTTPException(status_code=400, detail="no targets")
    if any(t.quantity < 1 for t in req.targets):
        raise HTTPException(status_code=400, detail="quantities must be ≥ 1")
    con = get_connection()
    try:
        mfg = load_manufacturing_graph(con)
        rx = load_reaction_graph(con)
        for t in req.targets:
            if t.type_id not in mfg and t.type_id not in rx:
                raise HTTPException(status_code=400, detail=f"No recipe for type {t.type_id}")
        ids: set[int] = set()
        for t in req.targets:
            ids |= collect_reachable(t.type_id, mfg, rx)
        names = {r["type_id"]: r["name"]
                 for r in con.execute(
                     f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                     tuple(ids))}
    finally:
        con.close()

    prices = resolve_market_data(ctx, list(ids))
    adjusted = fetch_adjusted_prices(list(ids))
    from app.industry.graph import SPEED_BUILD_CAP_HOURS
    mbh = SPEED_BUILD_CAP_HOURS if req.prioritize_speed else 0.0
    params = resolve_build_params(ctx, req.me_pct, req.te_pct, req.system_id, req.facility_tax_pct, mbh)
    # Slot pools: use the request overrides, else derive from the account's characters' skills
    # (falling back to 1 each so a brand-new account with no manufacturing skills still schedules).
    pool = _slot_pool(ctx)
    mfg_slots = req.mfg_slots if req.mfg_slots is not None else pool["manufacturing_slots"]
    rx_slots = req.rx_slots if req.rx_slots is not None else pool["reaction_slots"]
    pools = {"manufacturing": max(1, mfg_slots), "reaction": max(1, rx_slots)}
    targets = [(t.type_id, t.quantity) for t in req.targets]
    return plan_queue(targets, mfg, rx, prices, adjusted, params, names, pools)
