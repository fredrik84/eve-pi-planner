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

Pure/testable and deliberately I/O-free: every function takes prebuilt graphs + params, and this
module owns no endpoint, DB handle or market lookup. Callers (graph.py's /api/industry/plan and
orders.py's queue endpoints) resolve the real data and hand it in.
"""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, collect_reachable, effective_material_qty, resolve_unit_costs,
    SCC_SURCHARGE_PCT,
)


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


def marginal_threshold(memo: dict, targets: list[tuple[int, int]], params: BuildParams) -> float:
    """ISK a component must save before building it is worth a job.

    `max(marginal_pct_of_total % of the whole build, min_saving_isk)` — the percentage governs big
    builds, the absolute floor governs small ones, so one rule covers an Augoror and a Revelation.
    Shared by the decision itself and the figure reported back to the UI so the two can't drift.

    NOTE `build_unit_cost` is present-but-None when a material couldn't be priced, so a
    `.get(key, 0.0)` default does NOT save us here — it must be coerced with `or 0.0`.
    """
    total_build_value = sum(((memo.get(t) or {}).get("build_unit_cost") or 0.0) * qty
                            for t, qty in targets)
    pct_abs = (params.marginal_pct_of_total / 100.0 * total_build_value
               if params.marginal_pct_of_total > 0 else 0.0)
    return max(pct_abs, params.min_saving_isk)


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

    marginal_abs = marginal_threshold(memo, targets, params)

    result: dict[int, dict] = {}
    flipped: set[int] = set()             # bought for speed (slow to build)
    flipped_marginal: set[int] = set()    # bought because building saves a trivial amount
    marginal_saving: dict[int, float] = {}   # ISK building it WOULD have saved (negative = costs more)
    blueprint_cost_total = 0.0
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
                and tid not in params.force_build_ids
                and (memo.get(tid) or {}).get("buy_unit_cost") is not None):
            P = max(1, pools.get(activity, 1))
            wall_h = runs * recipe["base_time"] * (1 - te / 100.0) / P / 3600.0
            if wall_h > params.max_build_hours:
                flipped.add(tid)          # falls through to the bought loop; inputs not exploded
                continue

        # Blueprint you don't own: a COPY is a consumable, so its price is a genuine cost of this
        # batch and counts against the saving like any other input.
        #
        # What it must NOT do is decide for you. Blueprint ownership is read per character, and only
        # characters that granted the blueprints scope are visible — a corp-held print, or an alt
        # that was never connected, looks identical to "you don't own one". Acting on that absence
        # would silently refuse to build things the user owns the print for. So a missing blueprint
        # is priced and surfaced, never used to force a buy.
        bp = params.bp_acquire.get(tid)
        bp_cost = 0.0
        bp_buy = None
        if bp and runs > 0 and tid not in params.owned and bp["kind"] != "bpo_only":
            # Priced from the real listings: a copy carries a fixed number of runs and one contract
            # is one item, so a 40-run batch off 10-run copies means buying four of them.
            from app.industry.bpc import cost_for_runs
            bp_buy = cost_for_runs(bp, runs)
            bp_cost = bp_buy["cost"]
            blueprint_cost_total += bp_cost

        # Marginal-saving flip: buy if building saves a trivial amount — negligible vs the whole
        # product, or a tiny % of the component's own buy price. Not worth a job.
        node = memo.get(tid) or {}
        buc, byc = node.get("build_unit_cost"), node.get("buy_unit_cost")
        if runs > 0 and tid not in target_ids and buc is not None and byc is not None:
            total_saving = (byc - buc) * net - bp_cost      # the print is part of building it
            total_buy = byc * net
            # `<= 0` matters now that the blueprint counts: the cost engine only ever proposes
            # building when the MATERIALS are cheaper, so the saving used to be positive by
            # construction and the old guard required that. Add the price of a print you don't own
            # and it can go negative — building genuinely costs more than buying — which is the
            # clearest possible reason to buy, and the old guard skipped exactly that case.
            below_abs = marginal_abs > 0 and total_saving < marginal_abs
            below_pct = (params.min_saving_pct > 0 and total_buy > 0
                         and total_saving < params.min_saving_pct / 100.0 * total_buy)
            if (total_saving <= 0 or below_abs or below_pct) and tid not in params.force_build_ids:
                flipped_marginal.add(tid)
                # What the user gives up by taking the shortcut. Reported per material so "low
                # saving" is an amount they can judge, not a verdict they have to trust.
                marginal_saving[tid] = round(total_saving, 2)
                continue

        result[tid] = {
            "type_id": tid, "activity": activity, "build": True,
            "gross": gross[tid], "net": net, "runs": runs, "produced": produced,
            "leftover": produced - net, "output_qty": output_qty, "bought_for_speed": False,
            "blueprint_cost": bp_cost, "blueprint_buy": bp_buy,
        }
        for inp in recipe["inputs"]:
            gross[inp["type_id"]] += effective_material_qty(inp["quantity"], runs, me, mult)

    # Bought / raw types: whatever gross demand is left that isn't built.
    for tid, qty in gross.items():
        if tid in result or qty <= 0:
            continue
        result[tid] = {"type_id": tid, "activity": None, "build": False, "gross": qty,
                       "net": qty, "runs": 0, "produced": 0, "leftover": 0, "output_qty": 0,
                       "bought_for_speed": tid in flipped, "bought_marginal": tid in flipped_marginal,
                       "marginal_saving": marginal_saving.get(tid),
                       # Built only because the user overrode the shortcut — not the engine's call.
                       "forced_build": False}
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
        st = params.struct_time_mult if activity == "manufacturing" else 1.0
        skill = params.mfg_skill_time_mult if activity == "manufacturing" else params.rx_skill_time_mult
        per_run = recipe["base_time"] * (1 - te / 100.0) * st * skill
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
        st = params.struct_time_mult if info["activity"] == "manufacturing" else 1.0
        skill = params.mfg_skill_time_mult if info["activity"] == "manufacturing" else params.rx_skill_time_mult
        return info["runs"] * recipe["base_time"] * (1 - te / 100.0) * st * skill

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


def order_ranks(targets: list[tuple[int, int]], mfg: dict, rx: dict) -> dict[int, int]:
    """type_id -> rank of the EARLIEST queued order that needs it (0 = first in line).

    Demand is aggregated by type, so a component shared by orders 1 and 3 is one batch; it inherits
    rank 0 because order 1 is waiting on it. That's the point: shared work keeps the efficiency of a
    single batch while still being scheduled as urgently as the earliest order that needs it.
    """
    rank: dict[int, int] = {}
    for i, (tid, _qty) in enumerate(targets):
        for t in collect_reachable(tid, mfg, rx):
            if t not in rank or i < rank[t]:
                rank[t] = i
    return rank


def _fifo_priority(crit: dict[int, float], rank: dict[int, int]) -> dict[int, tuple]:
    """Scheduling priority: first-in-line WINS a contested slot, critical path breaks ties.

    Critical path alone minimises total makespan but is order-blind — when slots are scarce a later
    order's long chain can take the slot the first order's next stage was waiting for, which is
    exactly backwards for someone filling customer orders. Ranking first means order 1 is never held
    behind order 2, while order 2 still gets every slot order 1 isn't using.
    """
    return {tid: (-rank.get(tid, 0), crit.get(tid, 0.0)) for tid in crit}


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

    while remaining > 0:
        # Start every ready task that fits a free slot in its pool, most-critical first.
        ready = sorted(
            (t for t in tasks if t.task_id not in started
             and all(d in completed for d in deps.get(t.type_id, ()))),
            key=lambda t: priority.get(t.type_id, (0, 0.0)), reverse=True,
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

def _finish_of(tasks: list, type_id: int) -> float:
    """Hours until the last job producing `type_id` completes — i.e. that order is deliverable."""
    ends = [t.end for t in tasks if t.type_id == type_id and t.end is not None]
    return round(max(ends) / 3600.0, 2) if ends else 0.0


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
    crit = _critical_priority(agg, deps, mfg, rx, params)
    ranks = order_ranks(targets, mfg, rx)
    sched = schedule(tasks, by_type, deps, pools, _fifo_priority(crit, ranks))
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
                "bought_marginal": info.get("bought_marginal", False),
                "marginal_saving": info.get("marginal_saving"),
                "bought_no_blueprint": info.get("bought_no_blueprint", False),
            })
    shopping.sort(key=lambda r: r["line_cost"] or 0.0, reverse=True)

    # Blueprints you don't own are a real cost of building, so they're part of the total — and they
    # already counted against the margin-saver above, so a component whose print costs more than
    # building saves was bought instead of appearing here.
    blueprint_cost = sum(info.get("blueprint_cost", 0.0) for info in agg.values())

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
    total_cost = materials_cost + job_cost + blueprint_cost
    return {
        "targets": [{"type_id": t, "name": names.get(t, str(t)), "quantity": q,
                     "rank": i, "finish_hours": _finish_of(tasks, t)}
                    for i, (t, q) in enumerate(targets)],
        # Per-type build requirements — what progress tracking compares real ESI jobs against.
        # Exposed from `agg` because it's the only place the shared-batch run count exists.
        "requirements": [
            {"type_id": tid, "name": names.get(tid, str(tid)), "activity": info["activity"],
             "runs": info["runs"], "output_qty": info["output_qty"],
             "units": info["runs"] * info["output_qty"],
             # What this step was costed and timed at, and where that came from — so an assumed
             # ME/TE is visible and correctable rather than an invisible input to every number.
             "me": params.me_te_for(tid, info["activity"])[0],
             "te": params.me_te_for(tid, info["activity"])[1],
             "me_source": (params.me_source.get(tid, "default")
                           if info["activity"] == "manufacturing" else "reaction"),
             # You cannot install a job without the blueprint, so a build step you own nothing for
             # isn't a plan — it's a shopping trip you haven't been told about. Reactions use a
             # formula, not a blueprint, so they're never flagged.
             "blueprint": params.owned.get(tid),
             "needs_blueprint": info["activity"] == "manufacturing" and tid not in params.owned}
            for tid, info in agg.items() if info["build"] and info["runs"] > 0
        ],
        "schedule": sched,
        "shopping_list": shopping,
        "leftovers": leftovers,
        "unresolved": [s["type_id"] for s in shopping if s["unit_price"] is None],
        "metrics": {
            "materials_cost": round(materials_cost, 2),
            "job_cost": round(job_cost, 2),
            "blueprint_cost": round(blueprint_cost, 2),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(leftover_value, 2),
            "net_cost": round(total_cost - leftover_value, 2),
            "job_count": len(tasks),
            "build_steps": len(by_type),      # distinct things to build (parallel splits collapsed)
            "makespan_hours": sched["makespan_hours"],
            # When the FIRST queued order is done — the number that matters when you owe someone a
            # delivery, as distinct from when the whole queue drains.
            "first_delivery_hours": (_finish_of(tasks, targets[0][0]) if targets else 0.0),
            # Blueprints you don't own that the plan still builds — priced into total_cost above
            # when copies are listed. Anything with no copies available was flipped to buy instead.
            "missing_blueprints": sorted(
                ({"type_id": tid, "name": names.get(tid, str(tid)),
                  "runs_needed": info["runs"],
                  # None when nothing is listed to buy — the UI says "no copies listed" rather
                  # than implying a number it doesn't have.
                  "copies": (info.get("blueprint_buy") or {}).get("copies"),
                  "cost": (info.get("blueprint_buy") or {}).get("cost"),
                  "covered": (info.get("blueprint_buy") or {}).get("covered")}
                 for tid, info in agg.items()
                 if info["build"] and info["runs"] > 0
                 and info["activity"] == "manufacturing" and tid not in params.owned),
                key=lambda x: x["name"]),
            "slots": pools,
            # What the marginal rule actually resolved to for THIS build, so the UI can show the
            # consequence of the setting in ISK rather than a bare percentage.
            "marginal_threshold": round(marginal_threshold(memo, targets, params), 2),
            "marginal_pct": params.marginal_pct_of_total,
            # What to quote. Net cost is the base: the leftovers this build over-produces stay with
            # the builder and are already credited out of it, so charging them to the customer would
            # bill the same materials twice.
            "margin_pct": params.margin_pct,
            "price": round((total_cost - leftover_value) * (1 + params.margin_pct / 100.0), 2),
        },
    }



def assign_characters(waves: list[dict], characters: list[dict]) -> list[dict]:
    """Stamp `character_id` / `character_name` onto every scheduled job, across the WHOLE schedule.

    The to-install checklist already named a character for the jobs you can start right now, but
    everything after that was anonymous: a plan would say "stage 1: 12 jobs" and never say who
    installs them, which is not an instruction anyone can follow.

    The scheduler places jobs into two anonymous slot pools whose sizes are the sum of the
    characters' own slots, so an aggregate-feasible schedule is always assignable per character —
    slots are interchangeable. This walks the waves in time order, releasing a character's slot when
    that job ends, and gives each job to whoever has the most capacity free at that moment (which
    spreads the work rather than hammering one toon).

    Pure and I/O-free like everything else here: the caller supplies the characters. Jobs stay
    unassigned when there's no capacity or no character data, rather than inventing an assignee.
    """
    cap: dict[tuple[int, str], int] = {}
    names: dict[int, str] = {}
    for c in characters or []:
        cid = c.get("character_id")
        if cid is None:
            continue
        names[cid] = c.get("character_name") or str(cid)
        for act, key in (("manufacturing", "manufacturing_slots"), ("reaction", "reaction_slots")):
            cap[(cid, act)] = max(0, int(c.get(key) or 0))
    if not cap:
        return waves

    free = dict(cap)
    releases: list[tuple[float, int, str]] = []      # (end_hours, character_id, activity)
    for w in waves:
        start = w.get("start_hours") or 0.0
        # Hand back every slot whose job has finished by the time this wave starts.
        still = []
        for end, cid, act in releases:
            if end <= start + 1e-9:
                free[(cid, act)] = free.get((cid, act), 0) + 1
            else:
                still.append((end, cid, act))
        releases = still
        for t in w.get("tasks", []):
            act = t.get("activity")
            best, best_free = None, 0
            for (cid, a), n in free.items():
                if a != act or n <= 0:
                    continue
                # Most free capacity wins; character_id breaks ties so the result is deterministic.
                if n > best_free or (n == best_free and best is not None and cid < best):
                    best, best_free = cid, n
            if best is None:
                t["character_id"] = None
                t["character_name"] = None
                continue
            free[(best, act)] -= 1
            releases.append((start + (t.get("duration_hours") or 0.0), best, act))
            t["character_id"] = best
            t["character_name"] = names.get(best)
    return waves


# Slider stops the UI offers for the marginal-saving knob (0–10% in 0.5 steps).
MARGINAL_SWEEP_PCTS = [round(i * 0.5, 1) for i in range(21)]


def sweep_marginal(targets: list[tuple[int, int]], mfg: dict, rx: dict, prices: dict,
                   adjusted: dict, params: BuildParams, names: dict[int, str], pools: dict[str, int],
                   on_hand: dict[int, float] | None = None,
                   pcts: list[float] | None = None) -> list[dict]:
    """Cost + makespan at every stop of the marginal-saving slider, so the UI can show what dragging
    it actually buys you (time saved, ISK spent) without a round trip per pixel.

    The knob only ever moves the ISK threshold `marginal_threshold()` resolves to, and that value
    is floored at `min_saving_isk` — so on a small build every low percentage collapses to the same
    threshold and therefore the same plan. Deduping by the resolved threshold means the sweep runs
    a handful of plans, not one per stop."""
    pcts = pcts if pcts is not None else MARGINAL_SWEEP_PCTS
    # marginal_threshold needs a priced memo, and resolve_unit_costs is marginal-independent (the
    # knob is applied later, in aggregate_demand) — so one memo serves every stop.
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    for tid, _ in targets:
        unit(tid, frozenset())
    by_threshold: dict[float, dict] = {}
    out = []
    for pct in pcts:
        p = copy.copy(params)
        p.marginal_pct_of_total = pct
        thr = round(marginal_threshold(memo, targets, p), 2)
        point = by_threshold.get(thr)
        if point is None:
            m = plan_queue(targets, mfg, rx, prices, adjusted, p, names, pools,
                           on_hand=dict(on_hand or {}))["metrics"]
            point = {
                "threshold": thr,
                "total_cost": m["total_cost"],
                "net_cost": m["net_cost"],
                "makespan_hours": m["makespan_hours"],
                "build_steps": m["build_steps"],
                "job_count": m["job_count"],
            }
            by_threshold[thr] = point
        out.append({"pct": pct, **point})
    return out
