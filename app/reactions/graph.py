"""Reaction graph, pricing, and the profit/opportunity + shopping-list computations — the middle
layer of the reactions package. Given a caller's priced leaf materials (group sheet vs market), it
walks the reaction graph to cost every reachable product (_resolve_reachable), values a production
batch (_value_reaction_batch — the single source of truth for reaction economics), and builds the
opportunity ranking + shopping lists. Depends on settings for pricing config; never on jobs/orders,
so it imports first with no cycle."""
import math
import time as _time

from fastapi import Depends, HTTPException

from app.sde import get_connection, load_pi_data
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.cache import cache_get_json, cache_set_json, request_memo
from app.esi import require_context
from app.groups import member_group

from app.reactions._router import router
from app.reactions._flags import flag_on
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
    are treated as UNLIMITED supply as far as *quantity* goes — a nonzero sheet stock is never a
    granular hard cap (a stock of 100 does NOT limit you to 100 units), per the 2026-07-12 decision
    that sheet stock figures aren't a trustworthy enough signal to cap on. The ONE exception is a
    stock of exactly 0: `_load_goo_and_reached` reads it as "the alliance is out" and simply doesn't
    pass that material into `goo` at all, so it falls back to the market price here — market is the
    always-available default, the alliance sheet only ever a cheaper-when-in-stock bonus on top.

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
                             "reaction_count": 0, "depth": 0, "via": None, "source": "group",
                             "job_cost": 0.0, "alt_cost": None, "alt_source": None}
    for tid, buy_price in purchasable.items():
        if buy_price <= 0:
            continue
        if tid not in reached:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "depth": 0, "via": None, "job_cost": 0.0,
                             "alt_cost": None, "alt_source": None}
        elif buy_price < reached[tid]["unit_cost"]:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "depth": 0, "via": None, "job_cost": 0.0,
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
                # DEPTH is not reaction_count. reaction_count is how many reactions are in the
                # subtree; depth is how many STAGES deep it is — 1 + the deepest input, leaves at 0.
                # Reinforced Carbon Fiber is the case that makes the difference load-bearing: its
                # three inputs (Carbon Fiber, Oxy-Organic Solvents, Thermosetting Polymer) are each
                # one step off raw goo, so they are SIBLINGS that all run at once, and only the
                # product itself is a second stage. Ranking them by subtree size or by list position
                # instead serialises three jobs that have no dependency on each other at all.
                depth = 1 + max((reached[inp["type_id"]]["depth"] for inp in f["inputs"]), default=0)
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
                    "job_cost": job_cost_per_run / f["output_qty"],
                    "max_qty": int(runs) * f["output_qty"],
                    "reaction_count": reaction_count,
                    "depth": depth,
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


# The priced graph, kept for a couple of minutes per account. In-process rather than Redis: the
# structures are keyed by int type_id and full of nested dicts, and a JSON round-trip would both
# stringify every key and cost more than the rebuild it replaces.
#
# What it saves is real. Every order report, every shopping list and every suggest run rebuilds
# this: two full scans of `reactions`/`reaction_inputs`, the type table, the alliance sheet, a
# Redis round-trip per priced material, and the fixed-point expansion over the whole graph. Opening
# three orders in a row did it three times, for prices that are themselves cached for fifteen
# minutes upstream and cannot have moved.
#
# Two minutes is chosen against what the number is FOR: material prices update on a 15-minute
# cache, so nothing here goes stale inside the window, while a settings change (reaction system,
# time efficiency, the group's own sheet) shows up on the next page load rather than needing an
# invalidation hook in five places. Callers treat the result as READ-ONLY — every write to
# `reached` lives in `_resolve_reachable`, which builds it fresh.
_GOO_CACHE: dict[tuple, tuple[float, object]] = {}
_GOO_CACHE_TTL = 300.0
# ...and SHARED, in Redis, because the in-process copy is per worker: prod runs 2 replicas x 3
# workers, so five of every six clicks hit a cold cache and paid the full rebuild. Measured on the
# real account: 3,305 ms cold. That is the single biggest cost on the Reactions tab, and it is the
# same answer for every worker. Which also settles the invalidation question above for good: a
# process-local drop cannot clear the shared copy or the other five workers' own, so the TTL is the
# only honest expiry there is. Don't add an invalidation hook that only clears `_GOO_CACHE`.
_GOO_REDIS_TTL = 300


def _goo_to_json(v):
    """The result has int-keyed top-level dicts; JSON would stringify them silently. Convert on the
    way out and back on the way in, so a cached graph is byte-identical to a fresh one."""
    goo, reached, by_out, by_rx, types = v
    return {"goo": {str(k): x for k, x in goo.items()},
            "reached": {str(k): x for k, x in reached.items()},
            "by_out": {str(k): x for k, x in by_out.items()},
            "by_rx": {str(k): x for k, x in by_rx.items()},
            "types": {str(k): x for k, x in types.items()}}


def _goo_from_json(d):
    return ({int(k): x for k, x in d["goo"].items()},
            {int(k): x for k, x in d["reached"].items()},
            {int(k): x for k, x in d["by_out"].items()},
            {int(k): x for k, x in d["by_rx"].items()},
            {int(k): x for k, x in d["types"].items()})


def _reaction_fee_system(context_id: int, settings: dict) -> tuple[int | None, float, str]:
    """Which system's install fee a reaction job is quoted at: `(system_id, facility_tax, basis)`.

    `basis` is one of `configured` / `structure` / `reference` / `none`, and it is meant to be SHOWN
    — the same contract as `time_efficiency_source`. A cost the app worked out for itself has to say
    it worked it out, or the next reader cannot tell an inference from a statement.

    **Why this stopped being the account's typed field alone.** An unset `reaction_system` left
    `job_cost_rate` at 0, so install fees fell out of every estimate and the app reported the hole
    in a settings note instead of filling it. That was survivable while "cost" meant the shopping
    list. It stopped being survivable on 2026-08-14, when profit began to be netted against a FULL
    cost base (materials + fees + freight + collateral): a missing fee is now a silently overstated
    profit, not merely an absent line. CLAUDE.md rule 3 — no knob for a number the app can work out.

    Delegates to Industry's `account_build_defaults`, which already answers exactly this question
    (the account's own field first, then a structure it has told us it BUILDS in, with that
    building's own tax, then Jita as an explicitly-labelled floor). Reusing it is rule 4, and it
    matters more than the saved lines: two services quoting one job at two fees is the same class of
    defect as the two clocks this repair removed.

    Never fatal. If the resolver cannot be reached, this falls back to the typed field alone — the
    behaviour that shipped before — because a missing fee is better than a failed page.
    """
    # The CALLER's settings decide the configured case, before any inference is consulted. This is
    # not the same as letting `account_build_defaults` find it: that re-reads the account's stored
    # row, while `settings` here is whatever this request resolved (a group default, an account
    # override). Delegating the configured case too would let the inference quietly answer for a
    # system the caller had already been handed, which is how one job ends up quoted at two fees.
    named = settings.get("reaction_system")
    if named:
        sid = _resolve_system_id(named)
        if sid:
            return sid, float(settings.get("facility_tax_pct") or 0.0), "configured"
    try:
        from app.industry.graph import account_build_defaults
        sid, tax, basis = account_build_defaults(context_id, with_basis=True)
        if sid:
            return int(sid), float(tax or 0.0), basis
    except Exception:
        pass
    # Industry's gate said no (or it has none). Reactions asks its OWN flag and reaches the same
    # helper directly. Deliberately not an `or` inside `_default_system_on`: those are rollout-ladder
    # flags rather than per-account opt-ins, so ORing them would let the Reactions rung move job fees
    # on every Industry quote — a money-moving change behind a switch that never mentions Industry.
    try:
        from app.features import feature_enabled_for
        if feature_enabled_for("reactions_default_system", context_id):
            from app.industry.graph import _fallback_build_system
            sid, tax, basis = _fallback_build_system(
                context_id, float(settings.get("facility_tax_pct") or 0.0))
            if sid:
                return int(sid), float(tax or 0.0), basis
    except Exception:
        pass
    return None, 0.0, "none"


def reaction_fee_basis(context_id: int) -> str:
    """`_reaction_fee_system`'s basis alone, for the surfaces that must say where the fee came from.

    Separate from the graph load on purpose: that result is cached per account and consumed in a
    dozen places, and widening its tuple to carry one label would touch every one of them for a
    string only the settings card and the cost tiles read.
    """
    try:
        from app.reactions.settings import effective_reaction_settings
        return _reaction_fee_system(context_id, effective_reaction_settings(context_id))[2]
    except Exception:
        return "none"


def _load_goo_and_reached(context_id: int, allowed_material_ids: set[int] | None = None):
    key = (int(context_id), frozenset(allowed_material_ids) if allowed_material_ids else None)
    hit = _GOO_CACHE.get(key)
    if hit and (_time.monotonic() - hit[0]) < _GOO_CACHE_TTL:
        return hit[1]
    rkey = "rx:graph:%d:%s" % (
        int(context_id),
        ",".join(str(t) for t in sorted(allowed_material_ids)) if allowed_material_ids else "all")
    shared = cache_get_json(rkey)
    if shared:
        try:
            result = _goo_from_json(shared)
            _GOO_CACHE[key] = (_time.monotonic(), result)
            return result
        except Exception:
            pass                        # malformed/old shape — fall through and rebuild
    result = _load_goo_and_reached_uncached(context_id, allowed_material_ids)
    if result is not None:
        try:
            cache_set_json(rkey, _goo_to_json(result), ttl=_GOO_REDIS_TTL)
        except Exception:
            pass                        # a cache that won't take it must never fail the request
    # Bounded: one entry per (account, material filter) actively being looked at, and stale entries
    # are dropped on the way past rather than by a background sweep.
    now = _time.monotonic()
    for k, (ts, _v) in list(_GOO_CACHE.items()):
        if now - ts >= _GOO_CACHE_TTL:
            _GOO_CACHE.pop(k, None)
    _GOO_CACHE[key] = (now, result)
    return result


def _load_goo_and_reached_uncached(context_id: int, allowed_material_ids: set[int] | None = None):
    """Shared setup for both the profitability table and the shopping-list export: the alliance
    goo stock, the reaction graph, and the fixed-point `reached` expansion (see
    _resolve_reachable) from that goo through to every producible product. Returns
    (goo, reached, reactions_by_output, inputs_by_reaction, types) or None if there's no priced
    material to start from at all.

    A member of a priced group gets that group's below-market pp_moon_goo_prices sheet price for
    their moon materials AS WELL AS the open-market price (+ import shipping) — _resolve_reachable
    picks whichever is cheaper automatically, so a group member is never stuck trusting a
    stale/high sheet price over a better market rate. Market is always the fallback default: a
    material the sheet marks stock 0 ("alliance is out") is dropped from `goo` so it's priced/
    sourced from the market instead, consistently across suggestions, the shopping list, and
    customer orders — the alliance sheet is only ever a cheaper-when-in-stock bonus. (A *nonzero*
    stock is still NOT a granular quantity cap, per the 2026-07-12 decision — only exactly-0 is
    read as unavailable.) Everyone else only ever sees the market price — no group deal to fall
    back to. The Reactions feature itself is open to any logged-in user (require_context) — group
    membership only changes which price(s) a material is costed at, not who can see the tool."""
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
            "SELECT type_id, sell_price, stock FROM pp_moon_goo_prices WHERE group_id=?", (group["id"],)
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

    # Stock 0 on the sheet = "alliance is out": the material is dropped from `goo` entirely so it
    # falls back to the open-market price (source becomes "market"). Market is the always-available
    # default everywhere in the tool — suggestions, shopping list, and customer orders alike — and
    # the alliance sheet is only ever a cheaper-when-in-stock bonus layered on top (a nonzero stock
    # is still treated as unlimited supply — see _resolve_reachable's docstring; only exactly-0 is
    # this binary "not available" signal, not a granular quantity cap).
    goo = {
        r["type_id"]: {"sell_price": r["sell_price"]}
        for r in own_goo_rows
        if r["type_id"] in moon_material_ids and (r["stock"] or 0) > 0
    }

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
    purchasable_market = resolve_market_data(context_id, purchasable_ids)
    # Import freight = the cost to HAUL a bought input to the reaction site. It's only real for
    # items bought from JITA (the remote fallback) — a material sourced from a followed local/
    # alliance market is assumed to be at/near the reaction site, so no Jita import cost is added.
    # If a user follows a far-off market they price their own transport into that market's numbers,
    # not us (confirmed with the user 2026-07-21). `source` is set by resolve_market_data:
    # "Jita" for the fallback, else the winning market's name.
    purchasable = {}
    for tid, m in purchasable_market.items():
        vol = types.get(tid, {}).get("volume") or 0.0
        freight = settings["import_isk_per_m3"] * vol if m.get("source") == "Jita" else 0.0
        purchasable[tid] = m["sell_price"] + freight

    # Job installation cost: EIV x (system cost index + facility tax) — see _resolve_reachable's
    # job_cost roll-up. Both are 0 (no effect, current behavior preserved) unless the caller's
    # effective settings actually name a reaction system — an unconfigured account never pays
    # the extra ESI round-trip for adjusted prices either.
    sys_id, facility_tax, _fee_basis = _reaction_fee_system(context_id, settings)
    job_cost_rate = 0.0
    adjusted_prices: dict[int, float] = {}
    if sys_id:
        cost_index = fetch_system_cost_index(sys_id)
        # CCP adds a flat 4% SCC (Secure Commerce Commission) surcharge to EVERY industry job,
        # on top of the system cost index and the structure's facility tax — a fixed CCP tax, not
        # configurable per structure/system. Without it our estimate understates every job by 4%
        # of EIV; verified against the in-game breakdown (index + facility tax + 4% SCC = total).
        job_cost_rate = cost_index + facility_tax + SCC_SURCHARGE_PCT
        if job_cost_rate > 0:
            adjusted_prices = fetch_adjusted_prices(list(all_input_ids))

    reached = _resolve_reachable(goo, purchasable, reactions_by_output, job_cost_rate, adjusted_prices)
    # Tag each LEAF node with the human-readable market that priced it, so the shopping list /
    # materials report can show a per-line source badge. A leaf priced from the group sheet reads
    # "Group sheet"; a market-priced leaf carries the winning market's name (structure/region/Jita)
    # from resolve_market_data's `source` field. Reaction-product nodes are always reacted, never
    # bought, so they get no badge.
    market_src = {tid: m.get("source", "Jita") for tid, m in purchasable_market.items()}
    for tid, node in reached.items():
        if node.get("via") is not None:
            continue
        node["market_name"] = "Group sheet" if node.get("source") == "group" else market_src.get(tid)
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

    market = resolve_market_data(context_id, candidate_ids)

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
            ordered = _ordered_chain_tiers(node["via"]["inputs"], node["top_level_runs"], reached)
            # `tier` rides along so a caller that only has this payload (the manual-assign modal)
            # still knows which of these steps run together — position in the list does not say so.
            for (tier_tid, info), stage in zip(ordered, tier_ranks(ordered)):
                chain_tiers.append({
                    "type_id": tier_tid, "name": types.get(tier_tid, {}).get("name", str(tier_tid)),
                    "runs": info["runs"], "cycle_time": info["cycle_time"],
                    "output_qty": info["output_qty"], "tier": stage,
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
    """Ranked reaction opportunities, plus what their job cost was actually costed against.

    Unlike the manufacturing planner — which still charges the 4% SCC and facility tax without a
    system, so only the index share is missing — an unconfigured reaction system zeroes the WHOLE
    `job_cost_rate` here (see _load_goo_and_reached), and the adjusted-price fetch is skipped with
    it. So these profits are quoted with NO installation fee at all, which flatters every one of
    them. That is deliberate ("behave as before until configured") but it must not be invisible.
    """
    basis = {"system": None, "cost_index": 0.0, "facility_tax_pct": 0.0}
    try:
        s = effective_reaction_settings(context_id)
        sysname = s.get("reaction_system")
        basis = {
            "system": sysname,
            "cost_index": fetch_system_cost_index(_resolve_system_id(sysname)) if sysname else 0.0,
            "facility_tax_pct": s.get("facility_tax_pct") or 0.0,
        }
    except Exception:
        pass
    return {"opportunities": _build_opportunities(context_id), "cost_basis": basis}


@router.get("/api/reactions/job-detail")
def reactions_job_detail(type_id: int, runs: int = 1, context_id: int = Depends(require_context)):
    """Full breakdown for ONE running (or hypothetical) reaction job of `runs` runs of `type_id`:
    the materials it consumes, input/job cost, output value and net profit, units produced and
    total runtime — priced LIVE the same way the opportunity list is (the caller's own
    group/import settings), so the modal a player opens off a running slot agrees with the rest of
    the tool. Reuses _value_reaction_batch (the single source of truth for reaction economics) and
    the shopping-list explode rather than re-deriving any of it.

    **`output_value` and `net_profit` are the INSTANT-SELL figures** — what buy orders pay today,
    less every cost. That is CLAUDE.md's pricing invariant: a reaction good's sell-order price is
    not achievable profit, and the modal's profit line (and the ROI the frontend derives from it)
    is exactly a profit figure. Until 2026-08-14 both were quoted at sell orders while
    `instant_value` was computed and never read by anything. The sell-order pair is still returned,
    as `sell_order_value` / `net_profit_order`, and the modal labels them as the patient answer
    rather than the achievable one.

    `runtime_hours` comes off the graph's `cycle_time`, which is the raw SDE figure reduced by the
    account's time efficiency — now DERIVED from measured job durations rather than a hand-typed
    0% (see app.reactions.settings). So the quoted duration is the clock the account really reacts
    at, not one running ~2.14x slow."""
    runs = max(1, runs)
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(type_id) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=404, detail="Not a reachable reaction product right now")
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    settings = effective_reaction_settings(context_id)
    type_info = types.get(type_id, {})
    vol = type_info.get("volume") or 0.0
    output_qty_per_run = node["via"]["output_qty"]
    total_out = output_qty_per_run * runs

    market = resolve_market_data(context_id, [type_id])
    m = market.get(type_id)
    sell_price = m["sell_price"] if m else 0.0
    buy_price = m["buy_price"] if m else None
    v = _value_reaction_batch(node, total_out, sell_price, vol, settings, buy_price=buy_price)

    totals: dict[int, float] = {}
    _explode_shopping_list(type_id, total_out, reached, totals)
    materials = _materials_report(totals, reached, types)

    cycle_time = node.get("cycle_time") or 0
    return {
        "type_id": type_id,
        "name": type_info.get("name", str(type_id)),
        "runs": runs,
        "units": round(total_out, 2),
        "output_qty_per_run": output_qty_per_run,
        "runtime_hours": round(cycle_time * runs / 3600.0, 2),
        "input_cost": round(v["input_cost"], 2),
        "job_cost": round(v["job_cost"], 2),
        # Instant-sell (buy orders) — the achievable pair, and what the modal shows as Profit.
        "output_value": round(v.get("instant_value", 0.0), 2),
        "instant_value": round(v.get("instant_value", 0.0), 2),
        "net_profit": round(v.get("net_profit_instant", v["net_profit"]), 2),
        # Sell orders — market context, shown as "if you sell to orders (patience required)".
        "sell_order_value": round(v["output_value"], 2),
        "net_profit_order": round(v["net_profit"], 2),
        "shipping": round(v["shipping"], 2),
        "collateral": round(v["collateral"], 2),
        "priced": m is not None,
        "materials": materials,
    }


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


# ── One request, one answer ────────────────────────────────────────────────────────────────────
# The evidence layer (owned blueprints, the print floor, enabled stock, the paste library) is
# genuinely expensive on a real account — fourteen characters' blueprint JSON, the whole asset
# table, every observed job — and a single order report asked for it FIVE times: two stock pools,
# the formula caps, and `missing_formulas` rebuilding the same blueprint evidence a second time.
# Each answer is identical within one request, so this collapses them.
#
# Scoped to the REQUEST, not a TTL cache, and that distinction is the point: pasting a window or
# re-scanning assets must show up on the very next page load, not in thirty seconds. A ContextVar
# gives that for free — Starlette runs each request (sync endpoints included) in its own copied
# context, so the store cannot leak between requests. The 5-second stamp is belt-and-braces for any
# execution path that somehow reuses a context, and a direct call outside a request (every test in
# this repo) simply gets a fresh store and no memoisation worth the name.
def reaction_stock_pool(context_id: int) -> dict[int, float]:
    """{type_id: units} the account can actually spend — ENABLED asset sources only, the same pool
    every other read of stock in this app uses (`owned_quantities`).

    Empty unless `reactions_use_stock` is on, and empty on any failure: no stock is the behaviour
    this package had since it was written, so it is the safe direction.

    **Closed 2026-08-14 — there IS a reservation ledger now** (`app.industry.reservations`, behind
    `stock_reservations`). Materials assigned to a reactor but not yet installed are netted off
    inside `owned_quantities`, so a second planning run and the Manufacturing tab both see the same
    free pool rather than re-promising units this service has already claimed. A job that is really
    running claims nothing: its inputs have left the container, so the next asset scan reports that
    on its own and reserving them again would subtract them twice.
    """
    def _build():
        try:
            if not flag_on("reactions_use_stock", context_id):
                return {}
            from app.industry.assets import owned_quantities
            return {tid: q for tid, q in owned_quantities(context_id).items() if q > 0}
        except Exception:
            return {}

    # A COPY per caller, always: the pool is consumed as a plan is walked, and two callers in one
    # request (an order report walks materials and stages separately) must each spend the holding
    # once rather than share one draining dict.
    return dict(request_memo(("stock_pool", context_id), _build))


def _take_from_stock(stock: dict[int, float] | None, type_id: int, units_needed: float) -> float:
    """Spend what the account already holds of `type_id` against `units_needed`, returning what is
    STILL needed. `stock` is mutated — a unit spent here cannot be spent again by the next branch of
    the same plan, which is the whole reason the pool is threaded through the recursion rather than
    read fresh at each node.

    `stock` of None (the flag off, or a caller that deliberately wants the pure recipe — the
    opportunity list, which is cached and scaled linearly by its callers) means no stock at all and
    every caller behaves exactly as it did before assets were consulted.
    """
    if not stock or units_needed <= 0:
        return units_needed
    have = stock.get(type_id, 0.0)
    if have <= 0:
        return units_needed
    used = min(have, units_needed)
    stock[type_id] = have - used
    return units_needed - used


def _stock_covered_report(stock_covered: dict, types: dict) -> list[dict]:
    """The "you already hold this, so it is not work" note, named and newest-saving first.

    `_ordered_chain_tiers` fills `stock_covered` as it walks; a stage that silently vanishes from a
    plan reads as a bug, so both the wizard and an order report say which ones went and what they
    covered. Same shape on both, because it is the same answer.
    """
    return sorted(
        ({**c, "name": types.get(c["type_id"], {}).get("name", str(c["type_id"])),
          "units": round(c["units"], 1)} for c in stock_covered.values()),
        key=lambda c: -c["runs_saved"])


def _explode_shopping_list(type_id: int, units_needed: float, reached: dict, out: dict[int, float],
                           stock: dict[int, float] | None = None):
    """Recursively break `units_needed` units of `type_id` down to raw moon goo / purchasable
    leaf materials, accumulating into `out`. A leaf (node["via"] is None) just needs that many
    units directly; a reaction product needs ceil(units_needed / its output_qty) actual reaction
    cycles, which in turn consume its own ME-adjusted inputs — same graph _resolve_reachable
    already built, walked back down instead of the forward fixed-point expansion.

    **A REQUIREMENT, derived from a recipe** — for work nobody has planned yet (a customer order
    being quoted, or an intermediate a plan does not cover). What the PLAN will run is a different
    question with a different answer, and `_plan_materials` is the one that answers it.

    With a `stock` pool, units already in an enabled source are spent first at EVERY level: hold the
    intermediate and neither it nor the goo underneath it is shopped for."""
    units_needed = _take_from_stock(stock, type_id, units_needed)
    if units_needed <= 0:
        return
    node = reached.get(type_id)
    if not node or node["via"] is None:
        out[type_id] = out.get(type_id, 0.0) + units_needed
        return
    formula = node["via"]
    reaction_runs = math.ceil(units_needed / formula["output_qty"])
    for inp in formula["inputs"]:
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * reaction_runs
        _explode_shopping_list(inp["type_id"], eff_qty, reached, out, stock)


def _plan_materials(assignments: list[dict], reached: dict,
                    stock: dict[int, float] | None = None,
                    made_in_house: set[int] | None = None) -> dict[int, float]:
    """What the PLAN has to buy, computed ROW BY ROW. `{type_id: units}`.

    **A plan row is exactly one in-game job**, and that is the unit the game itself does material
    arithmetic in: a job's requirement is `ceil(quantity x runs x (1 - ME))`, rounded once, per job.
    Deriving the list from a recursive walk over aggregated units instead got two things wrong, both
    in the direction that leaves you unable to install the last job:

      * it rounded once per BATCH, not per job — 19 jobs needing 587 fuel blocks each is 11,153,
        while `ceil(5 x 2280 x 0.978)` is 11,150, and three blocks short is one job you cannot start;
      * it re-applied the stock pool underneath rows the plan still holds. Stock shortens a chain
        when it is ASSIGNED (`_trim_tiers_by_stock`); subtracting it a second time here bought goo
        for 440 runs of a product the plan was going to install 480 runs of.

    **An input the plan makes for itself is skipped**, since its own rows are in this same walk and
    account for it — which is what `_shopping_roots` and the old `planned` budget existed to
    approximate, and why both are gone from this path. An input the plan does NOT hold is real work
    the plan depends on and nobody has scheduled, so it is derived from its recipe
    (`_explode_shopping_list`) and stock legitimately applies to it — nothing is going to be
    installed for it.
    """
    # Passed in when costing a SUBSET of a plan (see `_plan_totals`' unpriced-order split): the
    # skip rule has to stay the whole plan's, or a subset would start buying an intermediate the
    # rows outside it produce.
    if made_in_house is None:
        made_in_house = {int(a["type_id"]) for a in assignments}
    out: dict[int, float] = {}
    for a in assignments:
        node = reached.get(a["type_id"])
        if not node or not node.get("via"):
            continue                    # not a reachable reaction product — nothing to buy for
        runs = int(a["runs"] or 0)
        if runs <= 0:
            continue
        for inp in node["via"]["inputs"]:
            tid = int(inp["type_id"])
            if tid in made_in_house:
                continue                # another row of this plan produces it
            # Per JOB, rounded once — the game's own arithmetic.
            qty = math.ceil(inp["quantity"] * runs * (1 - REACTION_ME_REDUCTION))
            _explode_shopping_list(tid, qty, reached, out, stock)
    return out


def _explode_chain_tiers(formula_inputs: list[dict], runs: int, reached: dict, tiers: dict[int, dict],
                          stock: dict[int, float] | None = None, covered: dict[int, dict] | None = None):
    """For `runs` cycles of a formula needing `formula_inputs`, finds every INTERMEDIATE reaction
    product among those inputs (recursively — a real chain can be several tiers deep, e.g. goo ->
    Ferrofluid -> Nonlinear Metamaterials) and accumulates how many of ITS OWN reaction cycles are
    needed to keep the tier above it supplied, into `tiers` (type_id -> {runs, cycle_time,
    output_qty}). Deliberately excludes the TOP-level formula itself — that's the caller's own
    suggestion, already tracked separately, not an "extra" tier. Without this, a suggestion for a
    multi-tier product only ever told the player to install the FINAL reaction, silently assuming
    they'd already have the intermediate on hand — which since the "force real chains" fix
    (intermediates are never just bought) is never actually true.

    ...unless they DO have it on hand. With a `stock` pool (enabled asset sources — see
    `reaction_stock_pool`), units already held are spent against the tier's requirement before any
    runs are planned for it, and a tier the stock covers outright is dropped along with everything
    below it: you don't react the inputs of something already sitting in the hangar. What the stock
    covered is recorded in `covered` so the plan can SAY why a stage vanished — a step that silently
    disappears is indistinguishable from a bug."""
    for inp in formula_inputs:
        inp_node = reached.get(inp["type_id"])
        if not inp_node or inp_node["via"] is None:
            continue  # raw goo or a genuine purchasable leaf — nothing to react
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * runs
        tid = inp["type_id"]
        wanted = eff_qty
        eff_qty = _take_from_stock(stock, tid, eff_qty)
        if covered is not None and eff_qty < wanted:
            c = covered.setdefault(tid, {"type_id": tid, "units": 0.0, "runs_saved": 0})
            c["units"] += wanted - eff_qty
        formula = inp_node["via"]
        if eff_qty <= 0:
            # Fully covered — no runs, and no recursion: the inputs of a tier you already hold are
            # not work this plan has to do.
            if covered is not None:
                covered[tid]["runs_saved"] += math.ceil(wanted / formula["output_qty"])
            continue
        inp_runs = math.ceil(eff_qty / formula["output_qty"])
        if covered is not None and tid in covered:
            covered[tid]["runs_saved"] += max(0, math.ceil(wanted / formula["output_qty"]) - inp_runs)
        if tid not in tiers:
            tiers[tid] = {"runs": 0, "cycle_time": formula["cycle_time"], "output_qty": formula["output_qty"]}
        tiers[tid]["runs"] += inp_runs
        # this tier may itself be multi-level
        _explode_chain_tiers(formula["inputs"], inp_runs, reached, tiers, stock, covered)


def _ordered_chain_tiers(formula_inputs: list[dict], runs: int, reached: dict,
                          stock: dict[int, float] | None = None,
                          covered: dict[int, dict] | None = None) -> list:
    """Explode a formula's intermediate tiers (`_explode_chain_tiers`) and return them as a list of
    (type_id, info) ordered deepest-first (by reaction_count) — the shape every assign/order/
    opportunity path needs. Factored out so the accumulator + reaction_count sort can't drift across
    its five call sites.

    `stock`/`covered` are passed straight through; see `_explode_chain_tiers`. Callers that hold a
    concrete batch pass a pool, the opportunity list deliberately does not (it is cached and its
    callers scale its tiers linearly, and stock coverage is not linear)."""
    tier_runs: dict[int, dict] = {}
    _explode_chain_tiers(formula_inputs, runs, reached, tier_runs, stock, covered)
    for tid, info in tier_runs.items():
        info["depth"] = int(reached.get(tid, {}).get("depth") or 1)
    # Deepest-first by DEPTH — the stage a step belongs to — with reaction_count only breaking ties
    # inside a stage for a stable order. Sorting by reaction_count alone put siblings in an
    # arbitrary sequence, which the callers below then stamped as sequential tier_orders.
    return sorted(tier_runs.items(),
                  key=lambda kv: (kv[1]["depth"], reached.get(kv[0], {}).get("reaction_count", 0)))


# Steps a run count is allowed to be rounded up to, largest first. The rule below takes the FIRST
# one that lands within the overshoot budget, so a big number gets a big round step and a small one
# gets a small step — 213 -> 225, 79 -> 80, 41 -> 45, 11 -> 12.
_TIDY_STEPS = (1000, 500, 250, 100, 50, 25, 10, 5, 2)
_TIDY_BUDGET = 0.15          # never overshoot the real requirement by more than this


def tidy_runs(runs: int, budget: float = _TIDY_BUDGET) -> int:
    """Round a run count UP to a number a human can type without checking, or leave it alone.

    Why round at all: these numbers are entered by hand in the industry window, one job at a time,
    and a stage of three products at 79 / 41 / 213 runs is three lookups every time you sit down to
    install. Rounding is the difference between reading the plan and copying it.

    Why round UP, never down: the next stage consumes this stage's output, and coming up short
    means the stage above cannot run at all. Overshooting produces a little surplus, which is
    material the account keeps — and with `reactions_use_stock` on, the surplus is spent by the
    next plan rather than sitting there.

    Why a BUDGET: the surplus is real ISK, so a tidy number is only worth it if it is nearly free.
    Nothing rounds past 15% over the true requirement, and anything under 10 runs is left exactly
    as it is — "3 runs" is already easy to type and rounding it to 5 would be a 67% overshoot.
    """
    runs = int(runs)
    if runs < 10:
        return runs
    for step in _TIDY_STEPS:
        if step >= runs:
            continue                    # a step bigger than the number itself is not a rounding
        up = -(-runs // step) * step    # ceil to the next multiple
        if up == runs:
            return runs                 # already a tidy number
        if (up - runs) / runs <= budget:
            return up
    return runs


def tier_ranks(ordered: list) -> list[int]:
    """Dense 0-based STAGE index per entry of an `_ordered_chain_tiers` result.

    Steps that share a depth share a rank, because they have no dependency on each other and run at
    the same time. This replaced `enumerate(...)` at every insert site, which is the bug it exists
    for: Reinforced Carbon Fiber's three inputs — Carbon Fiber, Oxy-Organic Solvents and
    Thermosetting Polymer, each one step off raw goo and fuel blocks — were stamped tier_order
    0/1/2 purely from their position in a list. The dashboard then labelled them Stage 1/2/3 and
    greyed two of them out, and `_concurrent_load` counted three simultaneous jobs as one slot.
    They are one stage; the product they feed is the second.
    """
    ranks, seen = [], {}
    for _tid, info in ordered:
        d = int(info.get("depth") or 1)
        ranks.append(seen.setdefault(d, len(seen)))
    return ranks


def _shopping_roots(rows: list[dict]) -> list[dict]:
    """The TOP row of each chain in `rows` — the row that stands for the batch.

    **The shopping list no longer uses this** (see `_plan_materials`, which works from the plan rows
    directly and needs no notion of a root). Its one live caller is `give_back_order_runs`: a
    cancelled order is owed back exactly what `assigned_runs` was incremented by, which is the sum
    of its chains' top rows. The name is historical — it was built for the shopping list, where a
    chain assign stores a row per intermediate AND one for the product, and exploding all of them
    counted a two-tier chain's goo twice.

    **A chain is identified by the assign that created it: (character_id, created_at).** All of a
    chain's rows are written in one call with a single timestamp (`assign_reaction`,
    `_allocate_and_insert`, `adopt_orphan` all compute `now` once), and a separate assign of the
    same product gets its own. Within a group only the HIGHEST tier is a root; everything below it
    is an instruction to react something the root's walk already bought for.

    Deliberately not "is this product an input of another assigned product": that also skips a
    product deliberately assigned on its own to sell, which is real demand. And deliberately not a
    new `chain_id` column, which could not group the rows already in the table.

    A group whose top row was cancelled leaves its remaining tier as the new highest — correct, it
    is genuinely the top of what is left to buy for.
    """
    groups: dict[tuple, list[dict]] = {}
    chain_top: dict[float, int] = {}
    for r in rows:
        # Rounded to the millisecond: one insert writes one timestamp to every row it creates, so
        # this is an exact match in practice; the rounding only guards float round-tripping.
        at = round(float(r.get("created_at") or 0.0), 3)
        groups.setdefault((r.get("character_id"), at), []).append(r)
        chain_top[at] = max(chain_top.get(at, 0), int(r.get("tier_order") or 0))
    roots: list[dict] = []
    for (_cid, at), rs in groups.items():
        # The top tier of the CHAIN, not of this character's share of it. Since the levelling pass
        # started pooling a product across characters (`level_product_runs`), an intermediate can
        # sit on a character that holds nothing else of its chain — where it would be the highest
        # tier present, be mistaken for a chain of its own, and have its goo bought a second time
        # on top of what the real top row's walk already covers.
        #
        # Still grouped per character as well, because an ORDER writes one timestamp across every
        # host it uses: each host's top row is a real root, and merging them by timestamp alone
        # would drop all but one of them from the list and under-buy.
        top = chain_top.get(at, 0)
        roots.extend(r for r in rs if int(r.get("tier_order") or 0) == top)
    return roots


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
    A JITA-sourced price includes the configured import shipping cost per m3 baked in (the haul to
    the reaction site); a price from a followed LOCAL/alliance market does NOT — that market is
    assumed at/near the reaction site, so no Jita import freight is added (see the freight note in
    _load_goo_and_reached). `market_name` names which market actually priced the leaf."""
    materials = [
        {
            "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
            "quantity": math.ceil(qty), "source": reached.get(tid, {}).get("source", "market"),
            "market_name": reached.get(tid, {}).get("market_name"),
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
def reactions_shopping_list(include_orders: bool = False,
                            context_id: int = Depends(require_context)):
    """Total raw materials needed across every one of the caller's pending SPECULATIVE-PROFIT
    assignments (see assign_reaction) — moon goo AND any purchased materials (fuel blocks etc.),
    summed and broken down to the same leaf level the profitability table prices from. Meant to
    be copied straight into a Jita multibuy tool (Janice) or the alliance's goo buy channel.

    By default EXCLUDES order-linked assignments (order_id IS NOT NULL) — a customer order
    already has its own materials report scoped to that specific order (GET
    /api/reactions/orders/{id}, sized off the order's own target_qty, not whatever a partial
    batch has been assigned so far), so folding it into this general list would both double-count
    against the order's own report and mix a client's specific requirement into an unrelated
    general shopping run.

    `include_orders=true` folds them in anyway, for the one job that reasoning does not cover:
    actually going shopping. A player whose work is ALL customer orders got an empty list and the
    words "nothing currently assigned" while four assignments sat in the table — technically the
    truth about speculative work, and useless. The counts below are always reported so the caller
    can say which case it is instead of showing an empty list as if there were nothing to buy.
    Note the double-count warning still stands: this list and the per-order reports overlap by
    construction, so use one or the other for a given buy, never both."""
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
            return {"materials": [], "formulas": {"complete": False, "formulas": [], "unresolved": []},
                    "speculative_count": 0, "order_count": 0}
        placeholders = ",".join("?" * len(char_ids))
        rows = [dict(r) for r in con.execute(
            f"SELECT character_id, type_id, runs, order_id, created_at, "
            f"COALESCE(tier_order,0) AS tier_order FROM pp_reaction_assignments "
            f"WHERE character_id IN ({placeholders})",
            char_ids,
        )]
    finally:
        con.close()

    speculative = [r for r in rows if r["order_id"] is None]
    order_linked = [r for r in rows if r["order_id"] is not None]
    # Always reported, even when the list is empty: "you have nothing assigned" and "everything you
    # have is on a customer order" need different answers from the caller, and they are
    # indistinguishable from an empty materials list alone.
    counts = {"speculative_count": len(speculative), "order_count": len(order_linked)}
    assignments = rows if include_orders else speculative

    if not assignments:
        return {"materials": [], "formulas": {"complete": False, "formulas": [], "unresolved": []},
                **counts}

    loaded = _load_goo_and_reached(context_id)
    if loaded is None:
        return {"materials": [], "formulas": {"complete": False, "formulas": [], "unresolved": []},
                **counts}
    goo, reached, _, _, types = loaded

    # Row by row — a plan row is one in-game job, and the game rounds materials per job. See
    # `_plan_materials` for why deriving this from a recursive walk over the chain got it wrong in
    # both directions at once (short on fuel blocks, short on anything the account also held).
    #
    # One stock pool for the whole list, consumed as it goes, so two rows needing the same
    # unplanned intermediate don't both claim it.
    totals = _plan_materials(assignments, reached, reaction_stock_pool(context_id))

    # ...and the FORMULAS the same plan needs that the account does not hold. A shopping list is
    # where you go to buy things, and a formula you're short of blocks the plan far harder than a
    # missing crate of goo — but it is a separate purchase, not a multibuy line, so it stays its
    # own section and is deliberately not folded into `materials` or any cost total (the same
    # separation `missing_blueprints` keeps on the Industry side). Every row is asked about,
    # including chain tiers, since a stage you cannot install stops everything above it.
    from app.reactions.library import missing_formulas, wanted_from_sequence, jobs_from_sequence
    # A plan row is exactly one in-game job, so the row count per product IS how many formulas the
    # plan needs at once — see `missing_formulas`' `jobs`.
    formulas = missing_formulas(context_id, wanted_from_sequence(assignments),
                                jobs=jobs_from_sequence(assignments))
    return {"materials": _materials_report(totals, reached, types), "formulas": formulas, **counts}


@router.get("/api/reactions/recurring-products")
def reactions_recurring_products(context_id: int = Depends(require_context)):
    """The distinct top-level products currently in play — either a speculative (non-order) chain
    already assigned, or an open customer order still short of its target — for the Shopping
    list's "scale to a deadline" feature (see `_rxOpenRecurringDeadline` in reactions.js). Reported
    live (2026-08-19): the only way to buy for a deadline was to wait for the day and see what's
    short, or hand-figure it from the cadence setting — this reuses whatever the account already
    has queued instead of asking the user to pick a product all over again, since that product list
    already exists as the current plan.

    `assigned_runs` is in the same top_level_runs unit `POST /api/reactions/orders/preview`
    reports back, so a caller can search that endpoint for "max qty by this deadline" and compare
    its `order.top_level_runs` against this number directly to say how many MORE runs to start."""
    from app.reactions.jobs import ensure_reaction_assignments_table, ensure_reaction_orders_table
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        rows: list[dict] = []
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            rows = [dict(r) for r in con.execute(
                f"SELECT character_id, type_id, name, runs, order_id, created_at, "
                f"COALESCE(tier_order,0) AS tier_order FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders}) AND order_id IS NULL",
                char_ids,
            )]
        orders = [dict(r) for r in con.execute(
            "SELECT id, type_id, name, top_level_runs, COALESCE(assigned_runs,0) AS assigned_runs, "
            "client_name FROM pp_reaction_orders WHERE context_id=? AND status='open'",
            (context_id,),
        )]
    finally:
        con.close()

    # Only the top row of each chain stands for the batch — see `_shopping_roots`, whose one other
    # caller has the same "count each queued batch once, not once per intermediate" need.
    speculative: dict[int, dict] = {}
    for r in _shopping_roots(rows):
        p = speculative.setdefault(r["type_id"], {"type_id": r["type_id"], "name": r["name"], "assigned_runs": 0})
        p["assigned_runs"] += r["runs"]

    order_products = [
        {"type_id": o["type_id"], "name": o["name"], "order_id": o["id"], "client_name": o["client_name"],
         "assigned_runs": o["assigned_runs"], "top_level_runs": o["top_level_runs"],
         "remaining_runs": o["top_level_runs"] - o["assigned_runs"]}
        for o in orders if o["top_level_runs"] - o["assigned_runs"] > 0
    ]

    return {"speculative": list(speculative.values()), "orders": order_products}
