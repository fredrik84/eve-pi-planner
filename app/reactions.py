"""
Moon-goo reaction profitability ranking (Phase 2 of the B0SS reactions tool — see
app/moon_goo.py for the alliance price list this reads from). Not part of the PI planner's
extractor/factory distribution algorithm — a separate, unrelated read-only advisory tool.

Starting from whatever goo is actually in stock (app.moon_goo's pp_moon_goo_prices), walks the
reaction graph forward (Simple -> Composite, any depth) to find every reachable product, and
for each computes: cost to make the max achievable quantity (capped by stock, ME-adjusted),
value at Jita (both instant-sell/buy and sell-order/ask, with order-book depth alongside so the
caller can judge liquidity), and shipping+collateral cost to get it there. Ranks by profit but
returns every dimension (steps, profit/m3, volume) un-collapsed — "advice, not a tool": the
comparison happens client-side, this doesn't pick a single winner.

This evaluates each candidate chain IN ISOLATION (as if all available goo went to that one
product) — it does not account for competing chains sharing the same raw materials. That
cross-product allocation is Phase 3's job (an LP over reaction-slot + stock constraints).
"""
import json as _json
import math
import time as _time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.market import fetch_market_data
from app.esi import require_admin, require_context, ESI_BASE, _get_valid_token, is_b0ss_member

router = APIRouter()

# Standup L-Set Reactor Efficiency I (T1) — the rig actually fitted, confirmed via EVE Ref.
# -2% material / -20% time base, x1.1 in null/WH space. Only the material figure matters here.
REACTION_ME_REDUCTION = 0.022

# Shared, admin-configurable shipping/collateral rates — these are alliance-wide assumptions
# (courier rates, insurance terms) that change over time, not a per-user preference, so this
# is a single global row (no context_id) rather than per-account settings like alert_settings.
# Import (buying materials in from Jita) has no collateral — that's a self-haul/no-3rd-party-
# courier assumption; only the export leg (shipping the reacted product OUT to sell) uses a
# courier and needs collateral declared. Confirmed with the user: moon goo itself has zero
# import cost (picked up at/near the reaction site), only non-goo purchased inputs (fuel
# blocks etc.) pay the import rate.
_RXS_DEFAULTS = {"import_isk_per_m3": 1200.0, "export_isk_per_m3": 1200.0, "export_collateral_pct": 0.005}


@ensure_once
def ensure_reaction_settings_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_settings (
                id                   INTEGER PRIMARY KEY,
                import_isk_per_m3    REAL NOT NULL DEFAULT 1200,
                export_isk_per_m3    REAL NOT NULL DEFAULT 1200,
                export_collateral_pct REAL NOT NULL DEFAULT 0.005
            )
        """)
        con.commit()
    finally:
        con.close()


def get_reaction_settings() -> dict:
    """Effective shipping/collateral settings — the saved singleton row if an admin has
    customized it, else the defaults above. Nothing changes in behavior until an admin edits
    these (same convention as app.alert_settings)."""
    ensure_reaction_settings_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT import_isk_per_m3, export_isk_per_m3, export_collateral_pct "
            "FROM pp_reaction_settings WHERE id=1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return dict(_RXS_DEFAULTS)
    return {
        "import_isk_per_m3": row["import_isk_per_m3"],
        "export_isk_per_m3": row["export_isk_per_m3"],
        "export_collateral_pct": row["export_collateral_pct"],
    }


class ReactionSettingsUpdate(BaseModel):
    import_isk_per_m3: float
    export_isk_per_m3: float
    export_collateral_pct: float


@router.get("/api/reactions/settings")
def api_get_reaction_settings(ctx: int = Depends(require_context)):
    return get_reaction_settings()


@router.put("/api/reactions/settings")
def api_update_reaction_settings(req: ReactionSettingsUpdate, ctx: int = Depends(require_admin)):
    ensure_reaction_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_reaction_settings (id, import_isk_per_m3, export_isk_per_m3, export_collateral_pct) "
            "VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "import_isk_per_m3=excluded.import_isk_per_m3, export_isk_per_m3=excluded.export_isk_per_m3, "
            "export_collateral_pct=excluded.export_collateral_pct",
            (req.import_isk_per_m3, req.export_isk_per_m3, req.export_collateral_pct),
        )
        con.commit()
    finally:
        con.close()
    return get_reaction_settings()


def _load_reaction_graph(con) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Returns (reactions_by_output, inputs_by_reaction): reactions_by_output maps a product
    type_id to the list of reaction formulas that can produce it (usually 1, occasionally 2 —
    e.g. a small no-fuel batch vs. a large fuel-block-consuming batch of the same conversion;
    both are kept as separate candidate paths rather than picking one). Each formula dict is
    {reaction_id, output_qty, cycle_time, inputs: [{type_id, quantity}]}."""
    inputs_by_reaction: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs_by_reaction.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})

    reactions_by_output: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        formula = {
            "reaction_id": r["reaction_id"], "output_qty": r["output_qty"],
            "cycle_time": r["cycle_time"], "inputs": inputs_by_reaction.get(r["reaction_id"], []),
        }
        reactions_by_output.setdefault(r["output_type_id"], []).append(formula)
    return reactions_by_output, inputs_by_reaction


_PURCHASABLE_MAX_QTY = 1_000_000_000  # a large-but-finite stand-in for "the open market can
# absorb as much as we need" for a single LEAF material — kept finite (not literal infinity) so
# arithmetic on it (cost = qty * unit_cost etc.) stays well-defined. On its own this would
# compound into absurd numbers through a multi-tier chain (each tier's max_qty is the previous
# tier's max_qty ÷ its own consumption × its own output_qty, so a big number in only gets
# bigger going up the chain) — _UNLIMITED_RUNS_CAP below re-applies a sane ceiling at EVERY
# tier of an "unlimited" (no real finite-stock goo) chain, not just at the raw leaf.
_UNLIMITED_RUNS_CAP = 5_000  # a generous but sane ceiling on any one tier's reaction-run count
# when its entire supply chain traces back to purchasable (market) materials only — comfortably
# above what a real "Suggest reactions" run would ever actually use (cadence/ISK capping there
# is always much smaller in practice), but keeps the RAW opportunity table (which isn't cadence-
# capped) from showing nonsensical hundred-million-unit / trillion-ISK "opportunities."


def _resolve_reachable(goo: dict[int, dict], purchasable: dict[int, float],
                        reactions_by_output: dict[int, list[dict]], require_real_goo: bool = True) -> dict[int, dict]:
    """Fixed-point expansion from the available goo (type_id -> {sell_price, stock}) through
    the reaction graph. Returns {type_id: node} for every reachable node (goo, market-bought
    inputs, AND every reaction product reachable at any depth), where node carries:
      unit_cost      - ISK to produce one unit, rolled down to raw goo/market cost (ME-adjusted)
      max_qty        - max units producible given available stock through the WHOLE chain
      reaction_count - distinct reaction runs needed in the subtree (the "work" proxy)
      via            - None for raw goo/purchasable, else the {reaction_id, ...} formula used
      unlimited      - True if every input feeding this node traces back only to purchasable
                       (market) leaves, never a real finite-stock goo leaf
    A reaction only becomes reachable once every one of its inputs is already reachable —
    same "expand until no more nodes unlock" shape as build_sde.py's compute_pi_tiers, just
    walked forward from available inputs instead of backward from a fixed target.

    `purchasable` seeds every reaction input that ISN'T alliance moon goo (fuel blocks, and any
    other named material most reaction formulas need alongside the moon materials) at its Jita
    buy price with effectively unlimited supply — these trade in bulk on the open market, they
    aren't a stock-limited resource the way the alliance's goo deal is. Without this, almost no
    reaction is ever reachable at all (confirmed live: every Composite/Hybrid formula and most
    Simple ones need at least one fuel block).

    `require_real_goo` (True for B0SS members) excludes any chain built ENTIRELY from purchasable
    inputs — not a genuine use of the alliance's below-market goo deal. Non-B0SS callers pass
    False, since for them there IS no real finite-stock goo at all (moon materials are folded
    into `purchasable` too, priced from the open market instead) — every one of their reachable
    chains is "market-only" by definition, and excluding those would leave nothing reachable."""
    reached: dict[int, dict] = {
        tid: {"unit_cost": g["sell_price"], "max_qty": g["stock"], "reaction_count": 0, "via": None, "unlimited": False}
        for tid, g in goo.items() if g["stock"] > 0
    }
    for tid, buy_price in purchasable.items():
        if tid not in reached and buy_price > 0:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "reaction_count": 0,
                             "via": None, "unlimited": True}

    changed = True
    while changed:
        changed = False
        for output_id, formulas in reactions_by_output.items():
            best = None
            for f in formulas:
                if not f["inputs"] or any(inp["type_id"] not in reached for inp in f["inputs"]):
                    continue
                # A chain built ONLY from purchasable (unlimited) inputs never actually touches
                # real moon goo — not a genuine "goo reaction" opportunity for a B0SS member.
                if require_real_goo and all(reached[inp["type_id"]]["unlimited"] for inp in f["inputs"]):
                    continue
                # ME reduces material CONSUMED per run, so it doesn't change unit_cost's
                # normalization directly — it scales down how much of each input a run needs.
                eff_qty = {inp["type_id"]: inp["quantity"] * (1 - REACTION_ME_REDUCTION)
                           for inp in f["inputs"]}
                runs = min(reached[tid]["max_qty"] / q for tid, q in eff_qty.items())
                if runs <= 0:
                    continue
                is_unlimited = all(reached[inp["type_id"]]["unlimited"] for inp in f["inputs"])
                if is_unlimited:
                    # Re-cap at EVERY tier, not just the raw leaf — a chain several tiers deep
                    # would otherwise compound _PURCHASABLE_MAX_QTY into an astronomical number
                    # (each tier's max_qty feeds the next tier's own runs calculation).
                    runs = min(runs, _UNLIMITED_RUNS_CAP)
                cost_per_run = sum(q * reached[tid]["unit_cost"] for tid, q in eff_qty.items())
                reaction_count = 1 + sum(reached[inp["type_id"]]["reaction_count"] for inp in f["inputs"])
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
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
                    "unlimited": is_unlimited,
                }
                # Prefer whichever formula yields lower unit_cost (most profitable path to
                # this product) — "least work most profitable" means the tool should already
                # have picked the cheap recipe, not surface a worse one alongside it.
                if best is None or candidate["unit_cost"] < best["unit_cost"]:
                    best = candidate
            if best is not None and (output_id not in reached or best["unit_cost"] < reached[output_id]["unit_cost"]):
                reached[output_id] = best
                changed = True

    return reached


def _load_goo_and_reached(context_id: int, allowed_material_ids: set[int] | None = None):
    """Shared setup for both the profitability table and the shopping-list export: the alliance
    goo stock, the reaction graph, and the fixed-point `reached` expansion (see
    _resolve_reachable) from that goo through to every producible product. Returns
    (goo, reached, reactions_by_output, inputs_by_reaction, types) or None if there's no goo to
    start from at all.

    B0SS alliance members get the below-market pp_moon_goo_prices deal (goo has real, finite
    stock, no import cost — already at/near the reaction site). Everyone else can still use the
    tool, just priced off the open market instead: moon materials are folded into `purchasable`
    (Fuzzworks-priced, unlimited supply, WITH import shipping — unlike B0SS goo, it isn't
    already at the reaction site) rather than kept in `goo` at all. The Reactions feature itself
    is open to any logged-in user (require_context) — this only changes which price a material
    is costed at, not who can see the tool."""
    from app.esi import is_b0ss_member
    is_b0ss = is_b0ss_member(context_id)

    con = get_connection()
    try:
        goo_rows = con.execute("SELECT type_id, sell_price, stock FROM pp_moon_goo_prices").fetchall()
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
    finally:
        con.close()

    # Advanced filter: restrict which raw moon materials are actually available to this player
    # (e.g. a Gas type their alliance doesn't stock, or they simply can't reliably buy) — any
    # chain that would need an excluded material is never reachable in the first place. None/
    # empty = no restriction (every priced material is assumed available, the original behavior).
    moon_material_ids = {r["type_id"] for r in goo_rows}
    if allowed_material_ids:
        moon_material_ids &= set(allowed_material_ids)
    if not moon_material_ids:
        return None  # the admin-managed goo list itself is empty — nothing to react from, B0SS or not

    if is_b0ss:
        goo = {r["type_id"]: {"sell_price": r["sell_price"], "stock": r["stock"]}
               for r in goo_rows if r["type_id"] in moon_material_ids}
    else:
        goo = {}  # no fixed-stock alliance deal for this account — priced from the market instead, below

    settings = get_reaction_settings()
    pi = load_pi_data()
    types = pi["types"]

    # Every reaction input that isn't alliance moon goo (fuel blocks, and other named
    # materials most formulas need alongside them) — priced at what it costs to instantly
    # ACQUIRE them (buy from existing Jita sell orders = the order book's sell_price; the
    # market_data field names are from the order book's perspective, not ours — buying costs
    # the sell price, selling earns the buy price) PLUS the configured import shipping cost
    # to get them from Jita to the reaction site (no collateral on the import leg — that's a
    # self-haul assumption, only the export leg uses a courier). Unlimited supply assumed.
    # Moon goo itself gets no import cost added for B0SS members (confirmed: already at/near
    # the reaction site) — non-B0SS members DO pay it on moon materials too, folded in below,
    # since for them it's just another market purchase that needs hauling in.
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
    purchasable_ids = [tid for tid in all_input_ids if tid not in goo and tid not in reactions_by_output]
    if not is_b0ss:
        purchasable_ids = list(set(purchasable_ids) | moon_material_ids)
    purchasable_market = fetch_market_data(purchasable_ids)
    purchasable = {
        tid: m["sell_price"] + settings["import_isk_per_m3"] * (types.get(tid, {}).get("volume") or 0.0)
        for tid, m in purchasable_market.items()
    }

    reached = _resolve_reachable(goo, purchasable, reactions_by_output, require_real_goo=is_b0ss)
    return goo, reached, reactions_by_output, inputs_by_reaction, types


def _build_opportunities(context_id: int, allowed_material_ids: set[int] | None = None) -> list[dict]:
    loaded = _load_goo_and_reached(context_id, allowed_material_ids)
    if loaded is None:
        return []
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    settings = get_reaction_settings()

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
        input_cost = qty * node["unit_cost"]
        ship_volume = qty * vol
        shipping_cost = ship_volume * settings["export_isk_per_m3"]
        # Collateral is a transport cost (courier contract), charged regardless of how the
        # cargo is later sold — declared consistently against Jita sell (the standard
        # freight-collateral reference value), not whichever sell method ends up chosen.
        collateral_cost = qty * m["sell_price"] * settings["export_collateral_pct"]
        instant_value = qty * m["buy_price"]
        order_value = qty * m["sell_price"]
        fixed_costs = input_cost + shipping_cost + collateral_cost

        opportunities.append({
            "type_id": tid,
            "name": type_info.get("name", str(tid)),
            "steps": node["reaction_count"],
            "top_level_runs": node["top_level_runs"],
            "cycle_time": node["cycle_time"],
            "output_qty": qty,
            "input_cost": round(input_cost, 2),
            "shipping_volume_m3": round(ship_volume, 2),
            "shipping_cost": round(shipping_cost, 2),
            "collateral_cost": round(collateral_cost, 2),
            "instant_sell_value": round(instant_value, 2),
            "sell_order_value": round(order_value, 2),
            "net_profit_instant": round(instant_value - fixed_costs, 2),
            "net_profit_order": round(order_value - fixed_costs, 2),
            "profit_per_m3_instant": round((instant_value - fixed_costs) / ship_volume, 2) if ship_volume > 0 else None,
            "buy_volume": m["buy_volume"],
            "sell_volume": m["sell_volume"],
        })

    opportunities.sort(key=lambda o: -o["net_profit_instant"])
    return opportunities


@router.get("/api/reactions/opportunities")
def reactions_opportunities(context_id: int = Depends(require_context)):
    return {"opportunities": _build_opportunities(context_id)}


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


@router.get("/api/reactions/shopping-list")
def reactions_shopping_list(context_id: int = Depends(require_context)):
    """Total raw materials needed across every one of the caller's pending assignments (see
    assign_reaction) — moon goo AND any purchased materials (fuel blocks etc.), summed and
    broken down to the same leaf level the profitability table prices from. Meant to be copied
    straight into a Jita multibuy tool (Janice) or the alliance's goo buy channel."""
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
            f"SELECT type_id, runs FROM pp_reaction_assignments WHERE character_id IN ({placeholders})",
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

    materials = [
        {
            "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
            "quantity": math.ceil(qty), "is_moon_goo": tid in goo,
        }
        for tid, qty in totals.items()
    ]
    materials.sort(key=lambda m: (not m["is_moon_goo"], m["name"]))
    return {"materials": materials}


# ── Personal reaction-job tracking (opt-in scope, see app.esi.INDUSTRY_JOBS_SCOPES) ────────────
# Cache-at-fetch, not live-fetch-on-every-page-load (same shape as app.pi_sim's colony state):
# ESI already reports start_date/end_date directly for a job, so there's no decay/rate math to
# simulate forward — just cache the raw filtered job list with a fetched_at timestamp, refreshed
# on demand (a "Refresh" button, same UX as the existing planet rescan) rather than polling.

@ensure_once
def ensure_industry_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_industry_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


_structure_name_cache: dict[int, str] = {}  # structure names don't change — cache for process lifetime


def _resolve_structure_name(structure_id: int, access_token: str) -> str:
    if structure_id in _structure_name_cache:
        return _structure_name_cache[structure_id]
    name = f"Structure #{structure_id}"
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/universe/structures/{structure_id}/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        name = data.get("name") or name
    except Exception:
        pass  # best-effort — an unresolvable structure just shows its raw ID, never blocks the fetch
    _structure_name_cache[structure_id] = name
    return name


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Fetch this character's reaction jobs (activity_id 11) from ESI, resolving each distinct
    facility to a readable name. Best-effort: returns [] on any failure rather than raising —
    a refresh failing for one character must not block the others."""
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == 11]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


@router.post("/api/reactions/jobs/refresh")
def refresh_industry_jobs(context_id: int = Depends(require_context)):
    """Refresh the caller's own characters' cached reaction-job list — only characters that
    have actually granted the industry-jobs scope (opted in via ?reactions=1 login) are
    fetched; others are silently skipped, not an error, since most PI-planner accounts never
    opt into this."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
    finally:
        con.close()

    refreshed = 0
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        token = _get_valid_token(c["character_id"])
        if not token:
            continue
        jobs = fetch_industry_jobs(c["character_id"], token)
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(jobs), _time.time()),
            )
            con.commit()
        finally:
            con.close()
        refreshed += 1
    return {"ok": True, "characters_refreshed": refreshed}


def reaction_slots(character_row: dict) -> int:
    """1 base slot + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at
    the game's real max of 11 (5+5+1)."""
    return min(11, 1 + (character_row.get("mass_reactions") or 0) + (character_row.get("advanced_mass_reactions") or 0))


@ensure_once
def ensure_reaction_assignments_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_assignments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                runs         INTEGER NOT NULL,
                input_cost   REAL NOT NULL,
                reward       REAL NOT NULL,
                created_at   REAL NOT NULL
            )
        """)
        con.commit()
        # tier_order: 0 = the deepest intermediate reaction (react first — a real chain, e.g.
        # goo -> Ferrofluid -> Nonlinear Metamaterials, needs the intermediate done before the
        # top-level reaction can even start), ascending up to the top-level product itself
        # (highest number in the group = react last). Existing single-tier assignments default
        # to 0, unaffected — additive migration, matches this codebase's convention.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN tier_order INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except Exception:
            pass
    finally:
        con.close()


class ChainTier(BaseModel):
    type_id: int
    name: str
    runs: int
    job_count: int = 1


class AssignRequest(BaseModel):
    character_id: int
    type_id: int
    name: str
    runs: int  # total runs across all jobs for this suggestion
    job_count: int = 1  # how many separate in-game job installs this splits into (one per slot)
    input_cost: float
    reward: float
    # Intermediate reactions this product's own formula needs (see _explode_chain_tiers in
    # _suggest_reactions), deepest-first — each becomes its own set of assignment rows the
    # player must install and let finish BEFORE the top-level reaction above can even start.
    chain_tiers: list[ChainTier] = []


@router.post("/api/reactions/assign")
def assign_reaction(req: AssignRequest, context_id: int = Depends(require_context)):
    """Commit a suggested (character, product) pairing as standing "go do this" instructions —
    surfaced on the dashboard until ESI confirms a matching job is actually running, at which
    point it's auto-cleared (see get_industry_jobs). A suggestion sized to use multiple reaction
    slots at once (job_count > 1, e.g. a big batch that needs several parallel jobs to finish
    within the chosen cadence) becomes that many SEPARATE assignment rows — one per actual
    in-game job install — so the dashboard shows the real number of slots this occupies, not one
    square standing in for several.

    Any chain_tiers (intermediate reactions this product's own formula needs, e.g. goo ->
    Ferrofluid -> this product — see _explode_chain_tiers) get their own assignment rows too,
    tagged with a LOWER tier_order so the dashboard can show them as "react this first." Their
    input_cost/reward are recorded as 0 — the full chain's cost/profit is already rolled up into
    the top-level row (unit_cost is computed recursively down to raw goo), so giving the
    intermediate rows their own nonzero values would double-count it if anything ever sums
    pp_reaction_assignments financially."""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")

        now = _time.time()
        for tier_order, tier in enumerate(req.chain_tiers):
            tier_job_count = max(1, tier.job_count)
            tier_runs_per_job = math.ceil(tier.runs / tier_job_count)
            for _ in range(tier_job_count):
                con.execute(
                    "INSERT INTO pp_reaction_assignments "
                    "(character_id, type_id, name, runs, input_cost, reward, created_at, tier_order) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (req.character_id, tier.type_id, tier.name, tier_runs_per_job, 0.0, 0.0, now, tier_order),
                )

        job_count = max(1, req.job_count)
        runs_per_job = math.ceil(req.runs / job_count)
        top_tier_order = len(req.chain_tiers)
        for _ in range(job_count):
            con.execute(
                "INSERT INTO pp_reaction_assignments "
                "(character_id, type_id, name, runs, input_cost, reward, created_at, tier_order) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (req.character_id, req.type_id, req.name, runs_per_job,
                 req.input_cost / job_count, req.reward / job_count, now, top_tier_order),
            )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign/{assignment_id}")
def unassign_reaction(assignment_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT a.id FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE a.id=? AND c.context_id=?",
            (assignment_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Assignment not found")
        con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (assignment_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign")
def unassign_all_reactions(context_id: int = Depends(require_context)):
    """Clear every pending assignment across all of the caller's characters in one go —
    "Clear all" on the dashboard, for starting a fresh suggestion set without hand-cancelling
    each pending slot one at a time."""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE character_id IN ({placeholders})", char_ids)
            con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.get("/api/reactions/jobs")
def get_industry_jobs(context_id: int = Depends(require_context)):
    """Personal reaction-job status for the Reactions wizard's dashboard page: currently
    running jobs (from the last refresh), a capacity summary (free slots right now, across
    every character that's opted into tracking), the per-character opt-in breakdown so the UI
    can offer to connect any character that hasn't opted in yet, and any standing "assigned but
    not yet actually running" instructions (see assign_reaction) — a context can hold several
    characters (an account's own alts, or characters from separate EVE accounts logged into the
    same session), and each authorises the tracking scope independently."""
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json, fetched_at FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        assignments: dict[int, list] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT id, character_id, type_id, name, runs, input_cost, reward, tier_order FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders}) ORDER BY tier_order", char_ids,
            ):
                assignments.setdefault(r["character_id"], []).append(dict(r))
    finally:
        con.close()

    now = _time.time()
    running: list[dict] = []
    characters: list[dict] = []
    total_slots = 0
    used_slots = 0
    tracked_any = False
    fulfilled_ids: list[int] = []
    pending_isk_committed = pending_net_profit = 0.0
    for c in chars:
        opted_in = "read_character_jobs" in (c["scopes"] or "")
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False, "slots": slots})
            continue
        tracked_any = True
        total_slots += slots
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        active = [j for j in jobs if j.get("status") in ("active", "paused", "ready")]
        used_slots += len(active)
        # Count-aware, not just a set of type_ids present — a big batch can be split into
        # several separate pending assignment rows for the SAME product (one per job slot), so
        # only as many of them may be cleared as there are actually-running jobs of that type;
        # naive set-membership would wrongly clear every pending row for a product the moment
        # just ONE of its several intended jobs gets installed.
        running_type_counts: dict[int, int] = {}
        for j in active:
            tid = j.get("product_type_id")
            running_type_counts[tid] = running_type_counts.get(tid, 0) + 1

        pending = []
        for a in assignments.get(c["character_id"], []):
            if running_type_counts.get(a["type_id"], 0) > 0:
                running_type_counts[a["type_id"]] -= 1
                fulfilled_ids.append(a["id"])  # ESI now confirms this specific job is actually running — clear it
            else:
                pending.append({
                    "assignment_id": a["id"], "type_id": a["type_id"], "name": a["name"], "runs": a["runs"],
                    "tier_order": a["tier_order"], "input_cost": a["input_cost"], "reward": a["reward"],
                })
                # Intermediate-tier rows are stored with input_cost/reward=0 (the full chain's
                # cost/profit already lives on the top-level row — see assign_reaction), so
                # summing every pending row never double-counts a multi-tier chain.
                pending_isk_committed += a["input_cost"]
                pending_net_profit += a["reward"]
        used_slots += len(pending)

        characters.append({
            "character_id": c["character_id"], "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active) - len(pending)),
            "pending": pending,
        })
        for j in active:
            end = j.get("end_date")
            hours_left = None
            if end:
                try:
                    end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                    hours_left = round((end_ts - now) / 3600.0, 1)
                except Exception:
                    pass
            running.append({
                "character_name": c["character_name"],
                "product_type_id": j.get("product_type_id"),
                "runs": j.get("runs"),
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
            })

    if fulfilled_ids:
        con = get_connection()
        try:
            placeholders = ",".join("?" * len(fulfilled_ids))
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE id IN ({placeholders})", fulfilled_ids)
            con.commit()
        finally:
            con.close()

    return {
        "tracked": tracked_any,
        "characters": characters,
        "running": sorted(running, key=lambda r: r["hours_left"] if r["hours_left"] is not None else 1e9),
        "total_slots": total_slots,
        "free_slots": max(0, total_slots - used_slots),
        "pending_isk_committed": round(pending_isk_committed, 2),
        "pending_net_profit": round(pending_net_profit, 2),
    }


# ── Wizard suggestion engine ────────────────────────────────────────────────────────────────
# Two stages, not one monolithic LP: WHAT to run (a knapsack — genuinely an LP's job) and WHO
# runs it (bin-packing onto real characters/slots — not naturally an LP, and keeping it a
# separate greedy step means each stage is small enough to hand-verify on its own).

_MIN_LIQUIDITY = 1000  # order-book depth (both sides) a candidate must clear to be suggested —
# fixed heuristic, not a UI knob, per "use liquidity as a selection filter, don't show it".
_CANDIDATE_POOL_SIZE = 30  # how many of the liquidity-filtered opportunities feed the knapsack


def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs AND
    minus already-pending assignments from a previous suggestion the player hasn't installed
    yet) — only characters that have opted into job tracking count, since we can't know a
    non-tracked character's current load. A fresh "Suggest reactions" run must not double-book
    slots a prior suggestion already claimed but hasn't been confirmed as running by ESI yet;
    mirrors get_industry_jobs' slot math, which does the same running+pending subtraction."""
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        pending_counts: dict[int, int] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT character_id, COUNT(*) AS n FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders}) GROUP BY character_id", char_ids,
            ):
                pending_counts[r["character_id"]] = r["n"]
    finally:
        con.close()

    result = []
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        used += pending_counts.get(c["character_id"], 0)
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
        })
    return result


def _suggest_reactions(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                        material_ids: set[int] | None = None) -> dict:
    opportunities = _build_opportunities(context_id, allowed_material_ids=material_ids)
    # Needed again in stage 2 to walk each chosen candidate's own formula tree for chain_tiers
    # (the intermediate reactions a multi-tier product needs before its own reaction can even
    # start) — cheap to recompute (fetch_market_data's own cache absorbs the repeat cost).
    _loaded = _load_goo_and_reached(context_id, material_ids)
    reached = _loaded[1] if _loaded else {}
    types = _loaded[4] if _loaded else {}
    candidates = [o for o in opportunities
                  if o["buy_volume"] >= _MIN_LIQUIDITY and o["sell_volume"] >= _MIN_LIQUIDITY
                  and o["top_level_runs"] > 0 and o["net_profit_instant"] > 0
                  and o["steps"] <= max_chain_depth]
    empty = {"suggestions": [], "totals": {
        "isk_committed": 0.0, "isk_budget": isk_budget, "net_profit": 0.0, "output_value": 0.0,
        "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"}}
    if not candidates:
        return empty

    # Cap each candidate's usable batch size so a huge, cheap-per-unit chain doesn't get most of
    # the ISK budget allocated to a run count that could never actually finish within the
    # player's chosen cadence (e.g. "weekly") even using every free reaction slot at once — the
    # cap uses the single best character's free-slot count as an upper bound (stage 2 below
    # clamps further to whichever character an assignment actually lands on). Cost/output/profit
    # scale down linearly with runs (unit cost and unit price don't change with batch size).
    chars_for_cap = _character_capacities(context_id)
    max_slots_available = max((c["free_slots"] for c in chars_for_cap), default=0) or 1
    capped = []
    for o in candidates:
        cycle_hours = o["cycle_time"] / 3600.0 if o["cycle_time"] else 0
        if cycle_hours <= 0:
            continue
        max_runs_in_cadence = int(max_slots_available * cadence_hours / cycle_hours)
        if max_runs_in_cadence <= 0:
            continue
        if max_runs_in_cadence >= o["top_level_runs"]:
            capped.append(o)
            continue
        scale = max_runs_in_cadence / o["top_level_runs"]
        c2 = dict(o)
        c2["top_level_runs"] = max_runs_in_cadence
        c2["output_qty"] = o["output_qty"] * scale
        c2["input_cost"] = o["input_cost"] * scale
        c2["net_profit_instant"] = o["net_profit_instant"] * scale
        c2["shipping_volume_m3"] = o["shipping_volume_m3"] * scale
        c2["instant_sell_value"] = o["instant_sell_value"] * scale
        capped.append(c2)
    candidates = capped
    if not candidates:
        return empty

    # Rank by profit per step (the "least work most profitable" ordering) before truncating to
    # a small pool — keeps the LP tiny regardless of how many opportunities Phase 2 finds.
    candidates.sort(key=lambda o: -(o["net_profit_instant"] / o["top_level_runs"]))
    candidates = candidates[:_CANDIDATE_POOL_SIZE]

    import highspy  # lazy: only ever needed here, keeps it off the cold-start path (matches app.optimizer)
    n = len(candidates)
    h = highspy.Highs()
    h.silent()
    # x_i in [0,1]: what fraction of candidate i's (cadence-capped) max achievable batch to
    # actually run — a continuous relaxation, not a strict per-unit integer knapsack, since with
    # only ISK as a resource constraint the LP optimum is naturally at-or-near integer anyway (at
    # most one variable fractional at the ISK cap), and this stays small/fast and easy to
    # hand-verify, matching this codebase's existing app.optimizer approach.
    hvars = h.addVariables(n, lb=[0.0] * n, ub=[1.0] * n)
    h.maximize(sum(float(c["net_profit_instant"]) * hvars[i] for i, c in enumerate(candidates)))
    h.addConstr(sum(float(c["input_cost"]) * hvars[i] for i, c in enumerate(candidates)) <= float(isk_budget))
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return empty

    x = h.getSolution().col_value
    chosen = [(c, xi) for c, xi in zip(candidates, x) if xi > 1e-6]
    chosen.sort(key=lambda cx: -(cx[0]["net_profit_instant"] * cx[1]))
    chosen = chosen[:10]  # the wizard shows up to 10 concrete suggestions
    if not chosen:
        return empty

    # Stage 2: allocate real reaction slots to each chosen product, all targeting completion
    # within roughly one cadence period — NOT a queue over unbounded future time (the old model),
    # since everything here is sized to finish around the same ~cadence window. Each suggestion
    # claims `slots_used` of a character's free slots (a one-time budget for this cadence period,
    # not something that frees up mid-period) — using MORE slots for a bigger batch so it still
    # finishes on time, rather than trickling one run at a time through a single slot for weeks.
    # `job_count`/`runs_per_job` are what the player actually installs in-game (one job install
    # per slot); `runs` is just the total for display.
    chars = _character_capacities(context_id)
    remaining_slots = {c["character_id"]: c["free_slots"] for c in chars if c["free_slots"] > 0}
    char_names = {c["character_id"]: c["character_name"] for c in chars}
    touched_chars: set[int] = set()

    suggestions = []
    isk_committed = net_profit = total_output_value = total_output_m3 = 0.0
    max_completion_hours = 0.0
    for c, xi in chosen:
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        ideal_slots = max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

        available = [cid for cid, free in remaining_slots.items() if free > 0]
        if not available:
            continue  # no character has any reaction slots left at all — this suggestion can't be scheduled
        # Prefer consolidating onto an already-used character (fewer characters touched overall)
        # as long as it still has room; otherwise open a fresh one with the most free slots.
        touched_with_room = [cid for cid in touched_chars if remaining_slots.get(cid, 0) > 0]
        pick_id = max(touched_with_room, key=lambda cid: remaining_slots[cid]) if touched_with_room \
            else max(available, key=lambda cid: remaining_slots[cid])

        slots_used = min(ideal_slots, remaining_slots[pick_id])
        remaining_slots[pick_id] -= slots_used
        touched_chars.add(pick_id)

        runs_per_job = math.ceil(runs_needed / slots_used)
        duration_hours = (runs_needed / slots_used) * cycle_hours
        max_completion_hours = max(max_completion_hours, duration_hours)

        # Chain tiers: any INTERMEDIATE reaction this product's own formula needs (e.g.
        # goo -> Ferrofluid -> this product) — each is a SEPARATE job the player must install
        # and let finish BEFORE the top-level reaction can even start, since the "force real
        # chains" fix means an intermediate is never just bought pre-made. Slots for these come
        # from the SAME character (one suggestion, one character does the whole chain — simpler
        # than spreading it), taken out of whatever's left after the top tier's own allocation.
        chain_tiers = []
        top_via = reached.get(c["type_id"], {}).get("via")
        if top_via:
            tier_runs: dict[int, dict] = {}
            _explode_chain_tiers(top_via["inputs"], runs_needed, reached, tier_runs)
            # Deepest (closest to raw goo) first — the one the player must react first.
            ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
            for tid, info in ordered:
                t_cycle_hours = info["cycle_time"] / 3600.0 if info["cycle_time"] else 1.0
                t_ideal_slots = max(1, math.ceil(info["runs"] * t_cycle_hours / cadence_hours)) if cadence_hours > 0 else info["runs"]
                t_slots_used = max(1, min(t_ideal_slots, remaining_slots.get(pick_id, 0)))
                remaining_slots[pick_id] = remaining_slots.get(pick_id, 0) - t_slots_used
                chain_tiers.append({
                    "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
                    "runs": info["runs"],
                    "job_count": t_slots_used,
                    "runs_per_job": math.ceil(info["runs"] / t_slots_used),
                })

        cost = c["input_cost"] * xi
        reward = c["net_profit_instant"] * xi
        output_qty = c["output_qty"] * xi
        output_value = c["instant_sell_value"] * xi
        output_m3 = c["shipping_volume_m3"] * xi
        isk_committed += cost
        net_profit += reward
        total_output_value += output_value
        total_output_m3 += output_m3

        # How much MORE this specific product could use if it were ISK-funded all the way to
        # actually filling its claimed slots for the whole cadence window, instead of finishing
        # early and leaving them idle until the next check-in. Bounded by `top_level_runs` (the
        # true cadence/stock-capped max for this candidate) so this never suggests spending ISK
        # on more than could physically be produced.
        max_runs_per_job_for_cadence = math.floor(cadence_hours / cycle_hours) if cycle_hours > 0 else runs_per_job
        aligned_runs = min(slots_used * max_runs_per_job_for_cadence, c["top_level_runs"])
        align_extra_runs = max(0, aligned_runs - runs_needed)
        align_ratio = aligned_runs / c["top_level_runs"]
        align_extra_isk = round(align_extra_runs * (c["input_cost"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0
        align_extra_reward = round(align_extra_runs * (c["net_profit_instant"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0

        suggestions.append({
            "type_id": c["type_id"], "name": c["name"],
            "runs": runs_needed,
            "job_count": slots_used,
            "runs_per_job": runs_per_job,
            "input_cost": round(cost, 2),
            "reward": round(reward, 2),
            "output_qty": round(output_qty, 1),
            "output_value": round(output_value, 2),
            "output_m3": round(output_m3, 1),
            "runtime_hours": round(duration_hours, 1),
            "align_extra_isk": align_extra_isk,
            "align_extra_reward": align_extra_reward,
            # Absolute (not delta) values for applying the alignment in one click — the frontend
            # swaps a suggestion's displayed fields to these wholesale rather than re-running the
            # whole optimizer, so clicking "align" only ever changes THIS product, nothing else.
            "aligned_runs": aligned_runs,
            "aligned_runs_per_job": max_runs_per_job_for_cadence,
            "aligned_input_cost": round(c["input_cost"] * align_ratio, 2),
            "aligned_reward": round(c["net_profit_instant"] * align_ratio, 2),
            "aligned_output_qty": round(c["output_qty"] * align_ratio, 1),
            "aligned_output_value": round(c["instant_sell_value"] * align_ratio, 2),
            "aligned_output_m3": round(c["shipping_volume_m3"] * align_ratio, 1),
            "assigned_character": char_names.get(pick_id, "?"),
            "assigned_character_id": pick_id,
            "chain_tiers": chain_tiers,
        })

    # "isk" = spent (near enough) the whole budget; "neither" = ran out of profitable, liquid,
    # within-chain-depth/cadence candidates before using it all — raising the ISK budget further
    # won't help, there's nothing more suitable to spend it on right now.
    binding = "isk" if isk_committed >= 0.97 * isk_budget else "neither"

    return {
        "suggestions": suggestions,
        "totals": {
            "isk_committed": round(isk_committed, 2),
            "isk_budget": isk_budget,
            "net_profit": round(net_profit, 2),
            "output_value": round(total_output_value, 2),
            "output_m3": round(total_output_m3, 1),
            "characters_used": len(touched_chars),
            "completion_hours": round(max_completion_hours, 1) if suggestions else None,
            "binding": binding,
        },
    }


class SuggestRequest(BaseModel):
    isk_budget: float
    max_chain_depth: int = 2
    cadence_hours: float = 168.0  # default weekly — how long you want a batch to run before checking back in
    material_ids: list[int] | None = None  # None/empty = no restriction, every priced material usable


_BUDGET_SENSITIVITY_STEP = 0.10  # "what if you raised your ISK budget by 10%?"


def _build_advisor(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                    material_ids: set[int] | None, current_profit: float, current_binding: str,
                    suggestions: list[dict]) -> dict:
    """Cheap, easily-computable "how could this be better" hints — not a full analysis, just the
    obvious low-effort wins: missing skill training on already-tracked characters, whether a bit
    more ISK would actually buy meaningfully more profit right now (vs. there being nothing left
    worth spending it on within the current chain-depth/cadence/material limits), and per-product
    cadence-alignment gaps (a suggestion that finishes early and leaves its claimed slots idle
    for the rest of the cadence window, for want of a bit more ISK to keep them running)."""
    # Only characters this plan actually fully books benefit from more slots — hinting "train
    # more" for every non-maxed character regardless of whether they're anywhere near their cap
    # is just noise (true for almost everyone almost always, and not actually actionable: extra
    # slots they're not using yet wouldn't change anything about this plan).
    used_slots_by_char: dict[int, int] = {}
    for s in suggestions:
        cid = s["assigned_character_id"]
        used_slots_by_char[cid] = used_slots_by_char.get(cid, 0) + s["job_count"]

    skill_hints = []
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchall()
    finally:
        con.close()
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        if reaction_slots(c) <= 1:
            continue  # sitting at the bare base slot (no Mass Reactions trained at all) — same
                      # "not worth surfacing" threshold the dashboard loadout already uses;
                      # training advice for a throwaway/untouched alt isn't useful
        if used_slots_by_char.get(c["character_id"], 0) < reaction_slots(c):
            continue  # this character still has spare slots this plan didn't need — not a bottleneck
        mr, amr = c["mass_reactions"] or 0, c["advanced_mass_reactions"] or 0
        if mr < 5:
            skill_hints.append(f"{c['character_name']}: training Mass Reactions to level {mr + 1} "
                                f"would add 1 more reaction slot (this plan is using all of their current ones)")
        elif amr < 5:
            skill_hints.append(f"{c['character_name']}: training Advanced Mass Reactions to level "
                                f"{amr + 1} would add 1 more reaction slot (this plan is using all of their current ones)")

    # Budget sensitivity: only worth suggesting "raise your ISK budget" when ISK is actually the
    # thing holding this back right now (current_binding == "isk") — if the current run already
    # left ISK unspent ("neither"), the real limit is something else (chain depth, cadence,
    # material filter, or simply no more profitable/liquid candidates), and more ISK wouldn't
    # help; recommending it anyway would be confusing/wrong advice.
    budget_hint = None
    if current_binding == "isk" and current_profit > 0:
        bigger = _suggest_reactions(context_id, isk_budget * (1 + _BUDGET_SENSITIVITY_STEP),
                                     max_chain_depth, cadence_hours, material_ids)
        extra_profit = bigger["totals"]["net_profit"] - current_profit
        if extra_profit > current_profit * 0.01:
            budget_hint = {
                "extra_isk": round(isk_budget * _BUDGET_SENSITIVITY_STEP, 2),
                "extra_profit": round(extra_profit, 2),
            }

    # Per-product cadence-alignment gaps (see the align_extra_isk/align_extra_reward computed
    # alongside each suggestion in _suggest_reactions) — worth a mention only when it's a
    # meaningful amount of profit, not a rounding-sized sliver.
    align_hints = [
        {"name": s["name"], "extra_isk": s["align_extra_isk"], "extra_reward": s["align_extra_reward"]}
        for s in suggestions if s.get("align_extra_isk", 0) > 0 and s["align_extra_reward"] > current_profit * 0.01
    ]

    return {"skill_hints": skill_hints, "budget_hint": budget_hint, "align_hints": align_hints}


@router.post("/api/reactions/suggest")
def suggest_reactions(req: SuggestRequest, context_id: int = Depends(require_context)):
    if req.isk_budget <= 0 or req.max_chain_depth <= 0 or req.cadence_hours <= 0:
        return {"suggestions": [], "totals": {
            "isk_committed": 0.0, "isk_budget": req.isk_budget, "net_profit": 0.0, "output_value": 0.0,
            "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"},
            "advisor": {"skill_hints": [], "budget_hint": None, "align_hints": []}}
    material_ids = set(req.material_ids) if req.material_ids else None
    result = _suggest_reactions(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours, material_ids)
    result["advisor"] = _build_advisor(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours,
                                        material_ids, result["totals"]["net_profit"], result["totals"]["binding"],
                                        result["suggestions"])
    return result
