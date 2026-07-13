"""Reaction graph, pricing, and the profit/opportunity + shopping-list computations — the middle
layer of the reactions package. Given a caller's priced leaf materials (group sheet vs market), it
walks the reaction graph to cost every reachable product (_resolve_reachable), values a production
batch (_value_reaction_batch — the single source of truth for reaction economics), and builds the
opportunity ranking + shopping lists. Depends on settings for pricing config; never on jobs/orders,
so it imports first with no cycle."""
import math

from fastapi import Depends

from app.sde import get_connection, load_pi_data
from app.market import fetch_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.cache import cache_get_json, cache_set_json
from app.esi import require_context
from app.groups import member_group

from app.reactions._router import router
from app.reactions.settings import effective_reaction_settings, _resolve_system_id


# Flat 4% SCC (Secure Commerce Commission) surcharge CCP applies to every industry job, on top of
# the system cost index and the structure's facility tax. Not configurable — a fixed tax on the
# job's estimated item value (EIV). See _load_goo_and_reached's job_cost_rate.
SCC_SURCHARGE_PCT = 0.04

# Standup L-Set Reactor Efficiency I (T1) — the rig actually fitted, confirmed via EVE Ref.
# -2% material / -20% time base, x1.1 in null/WH space. Only the material figure matters here.
REACTION_ME_REDUCTION = 0.022


def _load_reaction_graph(con, time_efficiency_pct: float = 0.0) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Returns (reactions_by_output, inputs_by_reaction): reactions_by_output maps a product
    type_id to the list of reaction formulas that can produce it (usually 1, occasionally 2 —
    e.g. a small no-fuel batch vs. a large fuel-block-consuming batch of the same conversion;
    both are kept as separate candidate paths rather than picking one). Each formula dict is
    {reaction_id, output_qty, cycle_time, inputs: [{type_id, quantity}]}.

    `cycle_time` here is the EFFECTIVE per-run duration (raw SDE value reduced by
    time_efficiency_pct — see _RXS_DEFAULTS), applied once at the source so every downstream
    consumer of a formula's cycle_time (chain-tier duration math, the Suggest wizard's cadence/
    completion estimates, the customer-order time estimate, and the frontend's own
    cycle_time-based runtime preview) gets the corrected figure automatically without each of
    them needing its own settings-aware logic. 0.0 (the default) reproduces the raw SDE value —
    unaware/non-context callers (e.g. list_reaction_fuel_blocks, which only needs formula
    shape, not timing) are unaffected."""
    inputs_by_reaction: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs_by_reaction.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})

    reactions_by_output: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        formula = {
            "reaction_id": r["reaction_id"], "output_qty": r["output_qty"],
            "cycle_time": (r["cycle_time"] or 0) * (1 - time_efficiency_pct),
            "inputs": inputs_by_reaction.get(r["reaction_id"], []),
        }
        reactions_by_output.setdefault(r["output_type_id"], []).append(formula)
    return reactions_by_output, inputs_by_reaction


_PURCHASABLE_MAX_QTY = 1_000_000_000  # a large-but-finite stand-in for "unlimited supply" for a
# single LEAF material — kept finite (not literal infinity) so arithmetic on it (cost = qty *
# unit_cost etc.) stays well-defined. On its own this would compound into absurd numbers through
# a multi-tier chain (each tier's max_qty is the previous tier's max_qty ÷ its own consumption ×
# its own output_qty, so a big number in only gets bigger going up the chain) —
# _UNLIMITED_RUNS_CAP below re-applies a sane ceiling at EVERY tier, not just the raw leaf.
_UNLIMITED_RUNS_CAP = 5_000  # a generous but sane ceiling on any one reaction's own run count —
# comfortably above what a real "Suggest reactions" run would ever actually use (cadence/ISK
# capping there is always much smaller in practice), but keeps the RAW opportunity table (which
# isn't cadence-capped) from showing nonsensical hundred-million-unit / trillion-ISK "opportunities."


def _resolve_reachable(goo: dict[int, dict], purchasable: dict[int, float],
                        reactions_by_output: dict[int, list[dict]],
                        job_cost_rate: float = 0.0, adjusted_prices: dict[int, float] | None = None
                        ) -> dict[int, dict]:
    """Fixed-point expansion from available leaf materials through the reaction graph. Returns
    {type_id: node} for every reachable node (goo, market-bought inputs, AND every reaction
    product reachable at any depth), where node carries:
      unit_cost      - ISK to produce one unit, rolled down to raw goo/market cost (ME-adjusted)
      max_qty        - max units producible this tier, capped by _UNLIMITED_RUNS_CAP
      reaction_count - distinct reaction runs needed in the subtree (the "work" proxy)
      via            - None for raw goo/purchasable, else the {reaction_id, ...} formula used
      source         - LEAF nodes only ("via" is None): "group" or "market", whichever priced
                       this specific unit_cost — see the cheaper-wins note below. Absent on
                       reaction-product nodes (never directly "bought", always reacted).
      alt_cost/      - LEAF nodes only: the LOSING price and its source, when both a group sheet
      alt_source       price and a market price were available for this material (None/None if
                       only one source had it at all) — lets a caller show the real ISK/unit
                       difference between the two, not just whichever one silently won.
      job_cost       - ISK of reaction JOB INSTALLATION fees to produce one unit, rolled up the
                       same way unit_cost is (0 for leaves — buying/harvesting installs no job).
                       Real EVE formula per job: EIV x (system cost index + facility tax rate),
                       EIV = sum of that job's own consumed materials valued at CCP's published
                       adjusted price (see app.industry_cost) — NOT the market/group unit_cost
                       used everywhere else. job_cost_rate is that (cost index + tax) sum,
                       precomputed once by the caller; 0.0 (the default) means job cost is
                       skipped entirely — see effective_reaction_settings.
    A reaction only becomes reachable once every one of its inputs is already reachable —
    same "expand until no more nodes unlock" shape as build_sde.py's compute_pi_tiers, just
    walked forward from available inputs instead of backward from a fixed target.

    Both `goo` (a group member's below-market alliance price sheet — see app.groups) and
    `purchasable` (Fuzzworks market price + import shipping — the only source for a non-member's
    moon materials, and for every reaction's non-goo inputs like fuel blocks regardless of group)
    are treated as UNLIMITED supply. This was a deliberate 2026-07-12 decision: a group's price
    sheet stock figures aren't a trustworthy enough signal to hard-cap on (no visibility into how
    often any given sheet is actually maintained — a stale zero used to silently hide a real
    opportunity, and a stale nonzero gave false confidence either way), so only price is trusted,
    not quantity.

    When a material is priced in BOTH dicts (a group member's own sheet AND the open market), the
    CHEAPER of the two wins automatically — so a group member is never stuck trusting a possibly-
    stale sheet price when the market rate is actually better; no manual override needed, the
    math just picks the cheaper source the same way it already picks the cheaper of two reaction
    formulas below."""
    adjusted_prices = adjusted_prices or {}
    # alt_cost/alt_source: the LOSING price, kept alongside the winning one whenever a leaf has
    # both a group sheet price and a market price available — lets a caller (the shopping list)
    # show the real ISK/unit difference between the two, not just whichever one won silently.
    reached: dict[int, dict] = {}
    for tid, g in goo.items():
        if g["sell_price"] > 0:
            reached[tid] = {"unit_cost": g["sell_price"], "max_qty": _PURCHASABLE_MAX_QTY,
                             "reaction_count": 0, "via": None, "source": "group", "job_cost": 0.0,
                             "alt_cost": None, "alt_source": None}
    for tid, buy_price in purchasable.items():
        if buy_price <= 0:
            continue
        if tid not in reached:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "via": None, "job_cost": 0.0, "alt_cost": None, "alt_source": None}
        elif buy_price < reached[tid]["unit_cost"]:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "via": None, "job_cost": 0.0,
                             "alt_cost": reached[tid]["unit_cost"], "alt_source": "group"}
        else:
            reached[tid]["alt_cost"] = buy_price
            reached[tid]["alt_source"] = "market"

    changed = True
    while changed:
        changed = False
        for output_id, formulas in reactions_by_output.items():
            best = None
            for f in formulas:
                if not f["inputs"] or any(inp["type_id"] not in reached for inp in f["inputs"]):
                    continue
                # ME reduces material CONSUMED per run, so it doesn't change unit_cost's
                # normalization directly — it scales down how much of each input a run needs.
                eff_qty = {inp["type_id"]: inp["quantity"] * (1 - REACTION_ME_REDUCTION)
                           for inp in f["inputs"]}
                runs = min(reached[tid]["max_qty"] / q for tid, q in eff_qty.items())
                if runs <= 0:
                    continue
                # Every leaf is unlimited-supply (see docstring) — always re-cap here so a
                # multi-tier chain can't compound the leaf sentinel into an absurd number
                # (each tier's max_qty feeds the next tier's own runs calculation).
                runs = min(runs, _UNLIMITED_RUNS_CAP)
                cost_per_run = sum(q * reached[tid]["unit_cost"] for tid, q in eff_qty.items())
                # This formula's OWN job-install fee (EIV of what IT consumes, valued at CCP's
                # adjusted prices) plus whatever job fees are already embedded in its inputs
                # (a reacted input's job_cost already rolled up its own subtree the same way) —
                # exactly mirrors cost_per_run's roll-up above, just for job fees instead of
                # material ISK. 0 when job_cost_rate is 0 (no reaction system configured).
                own_eiv_per_run = sum(q * adjusted_prices.get(tid, 0.0) for tid, q in eff_qty.items())
                job_cost_per_run = own_eiv_per_run * job_cost_rate + \
                    sum(q * reached[tid]["job_cost"] for tid, q in eff_qty.items())
                reaction_count = 1 + sum(reached[inp["type_id"]]["reaction_count"] for inp in f["inputs"])
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
                    "job_cost": job_cost_per_run / f["output_qty"],
                    "max_qty": int(runs) * f["output_qty"],
                    "reaction_count": reaction_count,
                    # Actual reaction-job cycles of THIS specific formula needed to hit max_qty
                    # (distinct from reaction_count, which is chain DEPTH/distinct-formula count,
                    # not run count) — this is what the wizard's "steps budget" (confirmed by the
                    # user: total reaction runs, not chain complexity) actually constrains.
                    # Deliberately counts only the top-level formula's own cycles, not upstream
                    # feeder reactions' cycles too — a documented simplification, not a full
                    # multi-level rollup (see Phase 3c plan notes).
                    "top_level_runs": int(runs),
                    "cycle_time": f["cycle_time"],
                    "via": {"reaction_id": f["reaction_id"], "cycle_time": f["cycle_time"],
                            "output_qty": f["output_qty"], "inputs": f["inputs"]},
                }
                # Prefer whichever formula yields the lower TOTAL landed cost — material cost
                # AND its own job-install fee — not just unit_cost alone, so a configured
                # reaction system's job cost can actually shift which recipe/path looks cheaper
                # ("least work most profitable" should already have picked the cheap path).
                if best is None or (candidate["unit_cost"] + candidate["job_cost"]) < (best["unit_cost"] + best["job_cost"]):
                    best = candidate
            if best is not None and (output_id not in reached or
                                      (best["unit_cost"] + best["job_cost"]) < (reached[output_id]["unit_cost"] + reached[output_id]["job_cost"])):
                reached[output_id] = best
                changed = True

    return reached


def _value_reaction_batch(node: dict, total_out: float, sell_price: float, volume: float,
                          settings: dict, buy_price: float | None = None) -> dict:
    """The economics of producing `total_out` output units of a reaction — the ONE place this math
    lives. Opportunities, customer orders, orphan adoption, and the running-job totals all call it,
    so a pricing change (the 4% SCC surcharge, sell-vs-buy, a new cost) can't silently drift between
    them (it did, for four copies, before this was extracted).

    Materials + job-install cost come from the recipe roll-up already on `node` (unit_cost/job_cost
    from _resolve_reachable); shipping + collateral from the caller's effective settings. Output is
    valued at `sell_price` (Fuzzworks sell/list — worth sold the normal way); pass `buy_price` too
    for the instant-liquidation figures (dumping to buy orders). Collateral is charged against the
    sell value consistently (the standard freight-collateral reference), regardless of sale method.
    net_profit nets the sell value against every cost; net_profit_instant does so against buy."""
    input_cost = total_out * node.get("unit_cost", 0.0)
    job_cost = total_out * node.get("job_cost", 0.0)
    shipping_volume = total_out * (volume or 0.0)
    shipping = shipping_volume * settings.get("export_isk_per_m3", 0.0)
    output_value = total_out * sell_price
    collateral = output_value * settings.get("export_collateral_pct", 0.0)
    fixed_costs = input_cost + job_cost + shipping + collateral
    result = {
        "input_cost": input_cost, "job_cost": job_cost,
        "shipping_volume": shipping_volume, "shipping": shipping, "collateral": collateral,
        "output_value": output_value, "fixed_costs": fixed_costs,
        "net_profit": output_value - fixed_costs,
    }
    if buy_price is not None:
        instant_value = total_out * buy_price
        result["instant_value"] = instant_value
        result["net_profit_instant"] = instant_value - fixed_costs
    return result


def _load_goo_and_reached(context_id: int, allowed_material_ids: set[int] | None = None):
    """Shared setup for both the profitability table and the shopping-list export: the alliance
    goo stock, the reaction graph, and the fixed-point `reached` expansion (see
    _resolve_reachable) from that goo through to every producible product. Returns
    (goo, reached, reactions_by_output, inputs_by_reaction, types) or None if there's no priced
    material to start from at all.

    A member of a priced group gets that group's below-market pp_moon_goo_prices sheet price for
    their moon materials AS WELL AS the open-market price (+ import shipping) — _resolve_reachable
    picks whichever is cheaper automatically, so a group member is never stuck trusting a
    stale/high sheet price over a better market rate (2026-07-12: the sheet's stock figures were
    dropped as a hard cap for the same reason — no reliable signal for how well-maintained it
    actually is; price is trusted, quantity isn't). Everyone else only ever sees the market
    price — no group deal to fall back to. The Reactions feature itself is open to any logged-in
    user (require_context) — group membership only changes which price(s) a material is costed
    at, not who can see the tool."""
    group = member_group(context_id)
    settings = effective_reaction_settings(context_id)

    con = get_connection()
    try:
        # The reference catalog of "known moon materials" is the UNION across every group's
        # sheet (not just the caller's own) — this is what makes a type_id count as a "moon
        # material" reachable via the market-priced path at all for a non-member; a group's
        # OWN priced rows (queried separately below) are what actually get the below-market
        # sheet price.
        all_goo_rows = con.execute("SELECT DISTINCT type_id FROM pp_moon_goo_prices").fetchall()
        own_goo_rows = (con.execute(
            "SELECT type_id, sell_price FROM pp_moon_goo_prices WHERE group_id=?", (group["id"],)
        ).fetchall() if group else [])
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con, settings.get("time_efficiency_pct", 0.0))
    finally:
        con.close()

    # Advanced filter: restrict which raw moon materials are actually available to this player
    # (e.g. a Gas type their group doesn't stock, or they simply can't reliably buy) — any
    # chain that would need an excluded material is never reachable in the first place. None/
    # empty = no restriction (every priced material is assumed available, the original behavior).
    moon_material_ids = {r["type_id"] for r in all_goo_rows}
    if allowed_material_ids:
        moon_material_ids &= set(allowed_material_ids)
    if not moon_material_ids:
        return None  # no group has priced any moon material at all — nothing to react from

    goo = {r["type_id"]: {"sell_price": r["sell_price"]} for r in own_goo_rows if r["type_id"] in moon_material_ids}

    pi = load_pi_data()
    types = pi["types"]

    # Every reaction input that isn't itself a reaction product (fuel blocks, and other named
    # materials most formulas need alongside moon goo) PLUS every moon material, regardless of
    # group — a group member's material needs a market price to compare against their sheet
    # price (see _resolve_reachable), and a non-member has no other price source at all.
    # Priced at what it costs to instantly ACQUIRE it (the order book's sell_price — market_data
    # field names are from the order book's perspective, buying costs the sell price) PLUS the
    # configured import shipping cost to haul it in (no collateral on the import leg — that's a
    # self-haul assumption, only the export leg uses a courier). Unlimited supply assumed.
    #
    # Deliberately EXCLUDES anything that is itself a reaction product (has its own entry in
    # reactions_by_output, e.g. Ferrofluid, Carbon Polymers — Simple/T1-tier intermediates) even
    # when buying it outright happens to be marginally cheaper in raw ISK than reacting it from
    # goo. Without this exclusion, "make chains" is misleading: a "chain-depth-2" suggestion
    # could actually just be one purchased intermediate + one real reaction, not a genuine
    # goo-to-final-product chain — and the bought intermediate's own market depth was never
    # checked by the liquidity filter (which only looks at the FINAL product), so its assumed
    # "instant, unlimited" availability is a much shakier assumption than for a true raw/
    # manufactured leaf (fuel blocks etc., which have no reaction formula at all and stay
    # purchasable). Only real reaction products get this treatment; true leaves are unaffected.
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    purchasable_ids = {tid for tid in all_input_ids if tid not in reactions_by_output} | moon_material_ids
    # Same "uncheck what you can't reliably get" filter as moon goo, applied to the racial fuel
    # blocks too — each reaction formula needs one SPECIFIC fuel block (fixed by its SDE recipe),
    # but a player's real access to each racial variant varies independently (e.g. cheap local
    # Oxygen production, no reliable Hydrogen supply) — a chain needing an excluded fuel block
    # becomes unreachable, same as an excluded moon material.
    if allowed_material_ids:
        fuel_block_ids = set(_fuel_block_ids(inputs_by_reaction, reactions_by_output, types))
        purchasable_ids -= (fuel_block_ids - set(allowed_material_ids))
    purchasable_ids = list(purchasable_ids)
    purchasable_market = fetch_market_data(purchasable_ids)
    purchasable = {
        tid: m["sell_price"] + settings["import_isk_per_m3"] * (types.get(tid, {}).get("volume") or 0.0)
        for tid, m in purchasable_market.items()
    }

    # Job installation cost: EIV x (system cost index + facility tax) — see _resolve_reachable's
    # job_cost roll-up. Both are 0 (no effect, current behavior preserved) unless the caller's
    # effective settings actually name a reaction system — an unconfigured account never pays
    # the extra ESI round-trip for adjusted prices either.
    reaction_system = settings.get("reaction_system")
    job_cost_rate = 0.0
    adjusted_prices: dict[int, float] = {}
    if reaction_system:
        cost_index = fetch_system_cost_index(_resolve_system_id(reaction_system))
        # CCP adds a flat 4% SCC (Secure Commerce Commission) surcharge to EVERY industry job,
        # on top of the system cost index and the structure's facility tax — a fixed CCP tax, not
        # configurable per structure/system. Without it our estimate understates every job by 4%
        # of EIV; verified against the in-game breakdown (index + facility tax + 4% SCC = total).
        job_cost_rate = cost_index + (settings.get("facility_tax_pct") or 0.0) + SCC_SURCHARGE_PCT
        if job_cost_rate > 0:
            adjusted_prices = fetch_adjusted_prices(list(all_input_ids))

    reached = _resolve_reachable(goo, purchasable, reactions_by_output, job_cost_rate, adjusted_prices)
    return goo, reached, reactions_by_output, inputs_by_reaction, types


_OPPS_CACHE_TTL = 90  # short-lived Redis cache — this is the single most expensive computation
# in the Reactions tab (full reaction-graph walk + a market fetch per candidate + job-cost ESI
# lookups when a reaction system is configured), and it was being recomputed on every dashboard
# refresh (tab open, every assign/cancel, every settings save) even though nothing upstream
# usually changed between them. 90s is short enough that a real price/settings change becomes
# visible again quickly with no explicit invalidation needed — same "TTL is enough" convention
# as app.market's own 15-minute price cache, just shorter since this result is more request-
# specific (per context_id + material filter) and therefore has a much smaller cache-hit pool.


def _build_opportunities(context_id: int, allowed_material_ids: set[int] | None = None) -> list[dict]:
    cache_key = "rx:opps:%d:%s" % (
        context_id,
        ",".join(str(t) for t in sorted(allowed_material_ids)) if allowed_material_ids else "all",
    )
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    result = _build_opportunities_uncached(context_id, allowed_material_ids)
    cache_set_json(cache_key, result, ttl=_OPPS_CACHE_TTL)
    return result


def _build_opportunities_uncached(context_id: int, allowed_material_ids: set[int] | None = None) -> list[dict]:
    loaded = _load_goo_and_reached(context_id, allowed_material_ids)
    if loaded is None:
        return []
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    settings = effective_reaction_settings(context_id)

    # Only reaction PRODUCTS are candidates — shipping raw unreacted goo isn't what this tool
    # is for (that's just the input side).
    candidate_ids = [tid for tid, node in reached.items() if node["reaction_count"] > 0 and node["max_qty"] > 0]
    if not candidate_ids:
        return []

    market = fetch_market_data(candidate_ids)

    opportunities = []
    for tid in candidate_ids:
        node = reached[tid]
        type_info = types.get(tid, {})
        vol = type_info.get("volume") or 0.0
        m = market.get(tid)
        if not m:
            continue  # no live market data for this product — can't price it, skip rather than guess

        qty = node["max_qty"]
        v = _value_reaction_batch(node, qty, m["sell_price"], vol, settings, buy_price=m["buy_price"])
        ship_volume = v["shipping_volume"]

        # Intermediate reactions this product's own formula needs BEFORE the top-level reaction
        # can even start (e.g. goo -> Ferrofluid -> this product) — computed once here at this
        # opportunity's own max batch (top_level_runs) so a caller (the manual-assign modal, the
        # suggestion flow) can linearly scale runs down to whatever smaller batch it actually
        # wants, the same way every other field on this dict already scales. Empty for a
        # single-tier product (the common case) — via is only set on reaction-product nodes.
        chain_tiers = []
        if node.get("via"):
            tier_runs: dict[int, dict] = {}
            _explode_chain_tiers(node["via"]["inputs"], node["top_level_runs"], reached, tier_runs)
            ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
            for tier_tid, info in ordered:
                chain_tiers.append({
                    "type_id": tier_tid, "name": types.get(tier_tid, {}).get("name", str(tier_tid)),
                    "runs": info["runs"], "cycle_time": info["cycle_time"], "output_qty": info["output_qty"],
                })

        opportunities.append({
            "type_id": tid,
            "name": type_info.get("name", str(tid)),
            "steps": node["reaction_count"],
            "top_level_runs": node["top_level_runs"],
            "cycle_time": node["cycle_time"],
            "output_qty": qty,
            "input_cost": round(v["input_cost"], 2),
            "job_cost": round(v["job_cost"], 2),
            "shipping_volume_m3": round(ship_volume, 2),
            "shipping_cost": round(v["shipping"], 2),
            "collateral_cost": round(v["collateral"], 2),
            "instant_sell_value": round(v["instant_value"], 2),
            "sell_order_value": round(v["output_value"], 2),
            "net_profit_instant": round(v["net_profit_instant"], 2),
            "net_profit_order": round(v["net_profit"], 2),
            "profit_per_m3_instant": round(v["net_profit_instant"] / ship_volume, 2) if ship_volume > 0 else None,
            "buy_volume": m["buy_volume"],
            "sell_volume": m["sell_volume"],
            "chain_tiers": chain_tiers,
        })

    opportunities.sort(key=lambda o: -o["net_profit_instant"])
    return opportunities


@router.get("/api/reactions/opportunities")
def reactions_opportunities(context_id: int = Depends(require_context)):
    return {"opportunities": _build_opportunities(context_id)}


def _fuel_block_ids(inputs_by_reaction: dict, reactions_by_output: dict, types: dict) -> dict[int, str]:
    """The racial fuel-block variants (Hydrogen/Helium/Nitrogen/Oxygen) used as reaction inputs
    — a second, distinct category from moon goo in the advanced material-availability filter.
    Each formula's required fuel block is fixed by its SDE recipe, but a player's REAL access to
    each racial variant can differ a lot (e.g. cheap local Oxygen production, but no reliable
    Hydrogen supply) — same idea as "a Gas type your group doesn't stock" already applies to
    moon goo, just for the other big recurring purchasable input. Detected dynamically (name
    match + actually used as a reaction input, not itself a reaction product) rather than
    hardcoded IDs, so it stays correct if the SDE's exact set ever changes."""
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    return {
        tid: types.get(tid, {}).get("name", str(tid))
        for tid in all_input_ids
        if tid not in reactions_by_output and "Fuel Block" in (types.get(tid, {}).get("name") or "")
    }


@router.get("/api/reactions/fuel-blocks")
def list_reaction_fuel_blocks(ctx: int = Depends(require_context)):
    con = get_connection()
    try:
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
    finally:
        con.close()
    types = load_pi_data()["types"]
    ids = _fuel_block_ids(inputs_by_reaction, reactions_by_output, types)
    rows = sorted(({"type_id": tid, "name": name} for tid, name in ids.items()), key=lambda r: r["name"])
    return {"fuel_blocks": rows}


def _explode_shopping_list(type_id: int, units_needed: float, reached: dict, out: dict[int, float]):
    """Recursively break `units_needed` units of `type_id` down to raw moon goo / purchasable
    leaf materials, accumulating into `out`. A leaf (node["via"] is None) just needs that many
    units directly; a reaction product needs ceil(units_needed / its output_qty) actual reaction
    cycles, which in turn consume its own ME-adjusted inputs — same graph _resolve_reachable
    already built, walked back down instead of the forward fixed-point expansion."""
    node = reached.get(type_id)
    if not node or node["via"] is None:
        out[type_id] = out.get(type_id, 0.0) + units_needed
        return
    formula = node["via"]
    reaction_runs = math.ceil(units_needed / formula["output_qty"])
    for inp in formula["inputs"]:
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * reaction_runs
        _explode_shopping_list(inp["type_id"], eff_qty, reached, out)


def _explode_chain_tiers(formula_inputs: list[dict], runs: int, reached: dict, tiers: dict[int, dict]):
    """For `runs` cycles of a formula needing `formula_inputs`, finds every INTERMEDIATE reaction
    product among those inputs (recursively — a real chain can be several tiers deep, e.g. goo ->
    Ferrofluid -> Nonlinear Metamaterials) and accumulates how many of ITS OWN reaction cycles are
    needed to keep the tier above it supplied, into `tiers` (type_id -> {runs, cycle_time,
    output_qty}). Deliberately excludes the TOP-level formula itself — that's the caller's own
    suggestion, already tracked separately, not an "extra" tier. Without this, a suggestion for a
    multi-tier product only ever told the player to install the FINAL reaction, silently assuming
    they'd already have the intermediate on hand — which since the "force real chains" fix
    (intermediates are never just bought) is never actually true."""
    for inp in formula_inputs:
        inp_node = reached.get(inp["type_id"])
        if not inp_node or inp_node["via"] is None:
            continue  # raw goo or a genuine purchasable leaf — nothing to react
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * runs
        formula = inp_node["via"]
        inp_runs = math.ceil(eff_qty / formula["output_qty"])
        tid = inp["type_id"]
        if tid not in tiers:
            tiers[tid] = {"runs": 0, "cycle_time": formula["cycle_time"], "output_qty": formula["output_qty"]}
        tiers[tid]["runs"] += inp_runs
        _explode_chain_tiers(formula["inputs"], inp_runs, reached, tiers)  # this tier may itself be multi-level


def _materials_report(totals: dict[int, float], reached: dict, types: dict) -> list[dict]:
    """Turns a leaf-level {type_id: qty} total (as built by _explode_shopping_list) into the
    display row shape both the pending-assignments shopping list and a customer order's own
    materials report use — kept as one function so the two callers can't drift apart.

    "source" reflects which price actually won for THIS specific leaf (see _resolve_reachable's
    cheaper-wins note) — not just "is this material categorically moon goo." A group's own
    sheet can lose to a better market rate on any given material, and a shopping-list entry
    must follow whichever source is actually cheapest right now, or it'd send the player to
    buy from the alliance when the market is the better (or for a non-member, the only) option.
    unit_cost is the SAME per-unit price _resolve_reachable already settled on for this leaf
    (whichever source won) — surfaced so the player has a concrete expected price to check the
    actual alliance/market quote against, not just a bare quantity. alt_unit_cost/alt_source
    is the LOSING price (if both a group sheet price and a market price were available for
    this material) — the actual ISK/unit gap between them, not just an implicit "cheaper won."
    Market prices always include the configured import shipping cost per m3 already baked in
    (see the "market_data field names..." note in _load_goo_and_reached above) — never a bare
    Jita quote."""
    materials = [
        {
            "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
            "quantity": math.ceil(qty), "source": reached.get(tid, {}).get("source", "market"),
            "unit_cost": round(reached.get(tid, {}).get("unit_cost", 0.0), 2),
            "alt_unit_cost": (round(reached[tid]["alt_cost"], 2)
                               if tid in reached and reached[tid].get("alt_cost") is not None else None),
            "alt_source": reached.get(tid, {}).get("alt_source"),
            "volume_m3": round(math.ceil(qty) * (types.get(tid, {}).get("volume") or 0.0), 2),
        }
        for tid, qty in totals.items()
    ]
    materials.sort(key=lambda m: (m["source"] != "group", m["name"]))
    return materials


@router.get("/api/reactions/shopping-list")
def reactions_shopping_list(context_id: int = Depends(require_context)):
    """Total raw materials needed across every one of the caller's pending SPECULATIVE-PROFIT
    assignments (see assign_reaction) — moon goo AND any purchased materials (fuel blocks etc.),
    summed and broken down to the same leaf level the profitability table prices from. Meant to
    be copied straight into a Jita multibuy tool (Janice) or the alliance's goo buy channel.

    Deliberately EXCLUDES order-linked assignments (order_id IS NOT NULL) — a customer order
    already has its own materials report scoped to that specific order (GET
    /api/reactions/orders/{id}, sized off the order's own target_qty, not whatever a partial
    batch has been assigned so far), so folding it into this general list would both double-count
    against the order's own report and mix a client's specific requirement into an unrelated
    general shopping run."""
    # Deferred import: this reads the jobs-layer plan table, but jobs imports graph, so graph can
    # only reach jobs at call time (not module load) without a circular import.
    from app.reactions.jobs import ensure_reaction_assignments_table
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if not char_ids:
            return {"materials": []}
        placeholders = ",".join("?" * len(char_ids))
        assignments = con.execute(
            f"SELECT type_id, runs FROM pp_reaction_assignments "
            f"WHERE character_id IN ({placeholders}) AND order_id IS NULL",
            char_ids,
        ).fetchall()
    finally:
        con.close()

    if not assignments:
        return {"materials": []}

    loaded = _load_goo_and_reached(context_id)
    if loaded is None:
        return {"materials": []}
    goo, reached, _, _, types = loaded

    totals: dict[int, float] = {}
    for a in assignments:
        node = reached.get(a["type_id"])
        if not node or node["via"] is None:
            continue  # shouldn't happen (assignments are always reaction products), skip defensively
        top_units = a["runs"] * node["via"]["output_qty"]
        _explode_shopping_list(a["type_id"], top_units, reached, totals)

    return {"materials": _materials_report(totals, reached, types)}
