"""Make-or-buy: unit costs resolved bottom-up, the reaction-policy report, and `build_plan` —
the one-product tree the preview modal renders."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

from app.industry.graph.params import BuildParams, blueprint_summary
from app.industry.graph.sde import _producer, effective_material_qty
# ── Make-or-buy resolution ────────────────────────────────────────────────────────────────────

def resolve_unit_costs(mfg: dict, rx: dict, prices: dict, adjusted: dict,
                       params: BuildParams) -> dict[int, dict]:
    """Bottom-up unit cost + build/buy decision for every reachable node. Memoized per type_id,
    cycle-guarded (a recipe cycle degrades to buy). Unit build cost uses a representative single
    run for the decision; the top-down explode uses the real per-job quantities."""
    memo: dict[int, dict] = {}

    def unit(type_id: int, stack: frozenset) -> dict:
        if type_id in memo:
            return memo[type_id]
        buy = (prices.get(type_id) or {}).get("sell_price") or None
        activity, recipe = _producer(type_id, mfg, rx)
        build_uc = None
        if recipe and type_id not in stack:
            me, _te = params.me_te_for(type_id, activity)
            mult, _tm = params.struct_mults_for(type_id, activity)
            fee_rate = params.job_fee_rate(type_id, activity)
            inner = stack | {type_id}
            total_in = 0.0
            eiv = 0.0
            ok = True
            for inp in recipe["inputs"]:
                child = unit(inp["type_id"], inner)
                # Keep resolving the siblings even once one input is unpriceable. `explode` reads
                # `memo[...]` for EVERY input of a node it builds, and the root is always built
                # whatever its own decision says — so bailing out here left the root's remaining
                # inputs unmemoized and the plan died with a KeyError on the first of them. Only
                # `ok` decides the outcome; the extra walk just fills the cache the explode needs.
                if child["unit_cost"] is None:
                    ok = False
                    continue
                qty = effective_material_qty(inp["quantity"], 1, me, mult)
                total_in += child["unit_cost"] * qty
                eiv += inp["quantity"] * adjusted.get(inp["type_id"], 0.0)
            if ok:
                job = eiv * fee_rate
                build_uc = (total_in + job) / recipe["output_qty"]

        # Blacklisted: the user always buys this one. Applied HERE rather than at demand time so the
        # parent's cost is the price of buying it too — deciding to buy but costing the parent as if
        # it were built is the mismatch that makes a plan's total unbelievable.
        blacklisted = (type_id in params.never_build_ids
                       and type_id not in params.force_build_ids and buy is not None)
        # The account's reaction policy, applied at the SAME layer and for the same reason: it makes
        # the whole subtree under a bought reaction disappear on its own, with nothing pruned by
        # hand. `force_build_ids` still wins (a per-type "build it anyway" is the more specific
        # instruction) and a reaction with no buy price is still BUILT — refusing to build what
        # can't be bought would leave the plan no way to get one at all.
        by_policy = (params.reaction_policy_buys(type_id, activity)
                     and type_id not in params.force_build_ids and buy is not None)
        if blacklisted or by_policy:
            decision = "buy"
        elif build_uc is not None and buy is not None:
            decision = "build" if build_uc < buy * (1 - params.build_margin) else "buy"
        elif build_uc is not None:
            decision = "build"
        elif buy is not None:
            decision = "buy"
        else:
            decision = "unresolved"
        unit_cost = (build_uc if decision == "build"
                     else buy if decision == "buy" else None)
        node = {
            "type_id": type_id, "activity": activity, "buildable": recipe is not None,
            "decision": decision, "unit_cost": unit_cost, "blacklisted": blacklisted,
            "reaction_policy": by_policy,
            "build_unit_cost": build_uc, "buy_unit_cost": buy,
            "source": (prices.get(type_id) or {}).get("source"),
        }
        memo[type_id] = node
        return node

    return memo, unit


def reaction_policy_report(memo: dict, params: BuildParams, names: dict[int, str],
                           rows: list[tuple[int, float, bool]]) -> dict | None:
    """What the reaction policy did to this plan, in ISK.

    Buying reaction outputs instead of running them is the same shape of trade as the
    marginal-saving threshold, so it follows the same rule: report what the shortcut cost rather
    than quietly taking it. A builder quoting against a competitor has to see that not reacting
    moved their floor.

    `isk` is signed the same way `marginal_saving` is — **what BUILDING these would save over
    buying them** — which is what makes it read correctly in both directions:
      * policy in force  → the reactions were bought, so a positive figure is what the convenience
        cost this build;
      * order overriding it → the reactions were built, so the same positive figure is what
        building them saved against the standing rule.
    `None` when the policy touched nothing, so the UI has nothing to draw.

    `rows` is (type_id, quantity, was_built) — the caller knows which of its own rows are which.
    """
    items = []
    total = 0.0
    for tid, qty, built in rows:
        # The ACTIVITY comes off the resolved node, never assumed from the row: a raw material has
        # no producer at all, and asking the policy about it as if it were a reaction reports every
        # mineral in the build as something the policy bought.
        node = memo.get(tid) or {}
        if qty <= 0 or not params.reaction_policy_buys(tid, node.get("activity"),
                                                       ignore_override=True):
            continue
        if tid in params.force_build_ids:
            continue          # exempted per type; not the policy's doing either way
        buc, byc = node.get("build_unit_cost"), node.get("buy_unit_cost")
        if built and not params.build_reactions_anyway:
            continue          # built despite the policy (no buy price) — nothing was traded away
        saving = None if buc is None or byc is None else round((byc - buc) * qty, 2)
        if saving is not None:
            total += saving
        items.append({"type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                      "built": built, "saving": saving})
    if not items:
        return None
    items.sort(key=lambda r: -(r["saving"] or 0.0))
    return {
        "overridden": bool(params.build_reactions_anyway),
        "all": bool(params.buy_all_reactions),
        "categories": sorted(params.buy_reaction_categories),
        "items": items,
        "isk": round(total, 2),
    }


def build_plan(target: int, quantity: int, mfg: dict, rx: dict, prices: dict, adjusted: dict,
               params: BuildParams, names: dict[int, str]) -> dict:
    """Full read-only plan: resolve unit costs, then explode the target quantity top-down into a
    build tree, an aggregated priced shopping list, a job list, and cost + time metrics. The root
    is always built (that's what the user asked to make), even if buying it would be cheaper —
    that comparison is still surfaced on the root node."""
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    unit(target, frozenset())  # populate the memo from the target down

    shopping: dict[int, float] = {}
    jobs: list[dict] = []
    totals = {"materials_cost": 0.0, "job_cost": 0.0, "job_seconds": 0.0, "leftover_value": 0.0}

    def explode(type_id: int, qty: float, is_root: bool = False) -> dict:
        node = memo[type_id]
        activity, recipe = _producer(type_id, mfg, rx)
        do_build = node["decision"] == "build" or (is_root and node["buildable"])
        if not do_build or recipe is None:
            price = node["buy_unit_cost"]
            line = (price or 0.0) * qty
            if node["decision"] != "unresolved":
                shopping[type_id] = shopping.get(type_id, 0.0) + qty
                totals["materials_cost"] += line
            return {
                "type_id": type_id, "name": names.get(type_id, str(type_id)),
                "decision": "unresolved" if node["decision"] == "unresolved" else "buy",
                "qty": qty, "unit_cost": price, "line_cost": line if price else None,
                "source": node.get("source"),
            }

        mult, st = params.struct_mults_for(type_id, activity)
        output_qty = recipe["output_qty"]
        runs = max(1, math.ceil(qty / output_qty))
        # Runs first: ME/TE depends on how many copies this batch consumes.
        me, te = params.me_te_for(type_id, activity, runs)
        produced = runs * output_qty
        if produced > qty:   # batch-rounding overproduction is reusable inventory, credit it back
            totals["leftover_value"] += (produced - qty) * (node["build_unit_cost"] or 0.0)

        children = []
        eiv = 0.0
        for inp in recipe["inputs"]:
            need = effective_material_qty(inp["quantity"], runs, me, mult)
            # EIV (the job-cost basis) uses BASE quantities × runs — ME never reduces it.
            eiv += inp["quantity"] * runs * adjusted.get(inp["type_id"], 0.0)
            children.append(explode(inp["type_id"], need))
        job_cost = eiv * params.job_fee_rate(type_id, activity)
        skill = params.mfg_skill_time_mult if activity == "manufacturing" else params.rx_skill_time_mult
        job_seconds = recipe["base_time"] * runs * (1 - te / 100.0) * st * skill
        totals["job_cost"] += job_cost
        totals["job_seconds"] += job_seconds
        jobs.append({
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "activity": activity, "runs": runs, "output_qty": output_qty,
            "produced": produced, "job_cost": job_cost, "job_seconds": job_seconds,
        })
        return {
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "decision": "build", "activity": activity, "qty": qty, "runs": runs,
            "produced": produced, "excess": produced - qty,
            "unit_cost": node["build_unit_cost"], "buy_unit_cost": node["buy_unit_cost"],
            "job_cost": job_cost, "owned": blueprint_summary(params.owned.get(type_id)),
            "inputs": children,
            # Which of your structures this step was costed in — the rigs there are what the ME/TE
            # above came from, so the number is unreadable without it.
            "site": (params.site_for(type_id, activity) or {}).get("name"),
        }

    tree = explode(target, quantity, is_root=True)

    shopping_list = sorted(
        (
            {
                "type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                "unit_price": (prices.get(tid) or {}).get("sell_price"),
                "source": (prices.get(tid) or {}).get("source"),
                "line_cost": ((prices.get(tid) or {}).get("sell_price") or 0.0) * qty,
                # Bought because of the account's standing always-buy rule rather than because
                # building lost on cost — the same flag the queue's list carries, since the two
                # lists render through the same row.
                "blacklisted": tid in params.never_build_ids,
                # …and the same for the account's reaction policy: a standing rule has to be
                # visible where it takes effect, or the plan looks like it got make-or-buy wrong.
                "reaction_policy": bool((memo.get(tid) or {}).get("reaction_policy")),
            }
            for tid, qty in shopping.items()
        ),
        key=lambda r: r["line_cost"], reverse=True,
    )
    unresolved = [s["type_id"] for s in shopping_list if s["unit_price"] is None]
    total_cost = totals["materials_cost"] + totals["job_cost"]
    return {
        "target": {"type_id": target, "name": names.get(target, str(target)), "quantity": quantity},
        "tree": tree,
        "shopping_list": shopping_list,
        "jobs": jobs,
        "metrics": {
            "materials_cost": round(totals["materials_cost"], 2),
            "job_cost": round(totals["job_cost"], 2),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(totals["leftover_value"], 2),
            "net_cost": round(total_cost - totals["leftover_value"], 2),
            "job_count": len(jobs),
            "total_job_hours": round(totals["job_seconds"] / 3600.0, 2),
        },
        "unresolved": unresolved,
        # What the account's reaction policy cost (or, when this build overrides it, saved).
        "reaction_policy": reaction_policy_report(
            memo, params, names,
            [(s["type_id"], s["qty"], False) for s in shopping_list]
            + [(j["type_id"], j["produced"], True) for j in jobs]),
    }
