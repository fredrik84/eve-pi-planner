"""Demand aggregation — the MRP explosion.

One pass in low-level-code order, so a component two capitals share is built in ONE batch
rather than once per order."""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
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
        mult, _tm = params.struct_mults_for(tid, activity)
        output_qty = recipe["output_qty"]
        net = max(0.0, gross[tid] - on_hand.get(tid, 0.0))
        runs = max(0, math.ceil(net / output_qty)) if net > 0 else 0
        produced = runs * output_qty
        # Demand is aggregated BEFORE jobs are split, so a per-job ME/TE has no meaning here: what
        # this batch's materials cost is what the copies it consumes come to, runs-weighted. Passing
        # the run count is what lets `me_te_for` weigh exactly those copies (and price the runs no
        # owned copy covers off the one the plan would buy) instead of crediting the whole batch to
        # the best copy in the drawer.
        me, te = params.me_te_for(tid, activity, runs)

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
        # What the account's OWN blueprints cover. An original covers everything; copies cover the
        # runs they carry — ALL of them, summed. Owning a 4-run copy for a 20-run batch is not "you
        # have the blueprint", it is sixteen runs you still have to find; and owning fourteen copies
        # worth 212 runs is not five runs, which is what counting only the best copy reported.
        own = (params.owned or {}).get(tid) or {}
        covered = (runs if own.get("kind") == "bpo" else
                   min(runs, max(0, int(own.get("runs") or 0))) if own else 0)
        short = max(0, runs - covered)
        if bp and short > 0 and bp["kind"] != "bpo_only":
            # Priced from the real listings: a copy carries a fixed number of runs and one contract
            # is one item, so a 40-run batch off 10-run copies means buying four of them.
            from app.industry.bpc import cost_for_runs
            bp_buy = cost_for_runs(bp, short)
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
            # Runs this batch needs that the account's own blueprint cannot cover, so the plan can
            # say "you hold a 4-run copy and this is 20 runs" instead of implying you're ready.
            "runs_short": short if own else 0,
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
