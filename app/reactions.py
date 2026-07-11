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
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.market import fetch_market_data
from app.esi import require_b0ss, require_admin

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
def api_get_reaction_settings(ctx: int = Depends(require_b0ss)):
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


def _resolve_reachable(goo: dict[int, dict], purchasable: dict[int, float],
                        reactions_by_output: dict[int, list[dict]]) -> dict[int, dict]:
    """Fixed-point expansion from the available goo (type_id -> {sell_price, stock}) through
    the reaction graph. Returns {type_id: node} for every reachable node (goo, market-bought
    inputs, AND every reaction product reachable at any depth), where node carries:
      unit_cost      - ISK to produce one unit, rolled down to raw goo/market cost (ME-adjusted)
      max_qty        - max units producible given available stock through the WHOLE chain
      reaction_count - distinct reaction runs needed in the subtree (the "work" proxy)
      via            - None for raw goo/purchasable, else the {reaction_id, ...} formula used
    A reaction only becomes reachable once every one of its inputs is already reachable —
    same "expand until no more nodes unlock" shape as build_sde.py's compute_pi_tiers, just
    walked forward from available inputs instead of backward from a fixed target.

    `purchasable` seeds every reaction input that ISN'T alliance moon goo (fuel blocks, and any
    other named material most reaction formulas need alongside the moon materials) at its Jita
    buy price with effectively unlimited supply — these trade in bulk on the open market, they
    aren't a stock-limited resource the way the alliance's goo deal is. Without this, almost no
    reaction is ever reachable at all (confirmed live: every Composite/Hybrid formula and most
    Simple ones need at least one fuel block)."""
    reached: dict[int, dict] = {
        tid: {"unit_cost": g["sell_price"], "max_qty": g["stock"], "reaction_count": 0, "via": None}
        for tid, g in goo.items() if g["stock"] > 0
    }
    for tid, buy_price in purchasable.items():
        if tid not in reached and buy_price > 0:
            reached[tid] = {"unit_cost": buy_price, "max_qty": float("inf"), "reaction_count": 0, "via": None}

    changed = True
    while changed:
        changed = False
        for output_id, formulas in reactions_by_output.items():
            best = None
            for f in formulas:
                if not f["inputs"] or any(inp["type_id"] not in reached for inp in f["inputs"]):
                    continue
                # A chain built ONLY from purchasable (unlimited) inputs never actually touches
                # real moon goo — not a genuine "goo reaction" opportunity, and would divide by
                # an infinite max_qty below. Require at least one finite (goo-backed) input.
                if all(reached[inp["type_id"]]["max_qty"] == float("inf") for inp in f["inputs"]):
                    continue
                # ME reduces material CONSUMED per run, so it doesn't change unit_cost's
                # normalization directly — it scales down how much of each input a run needs.
                eff_qty = {inp["type_id"]: inp["quantity"] * (1 - REACTION_ME_REDUCTION)
                           for inp in f["inputs"]}
                runs = min(reached[tid]["max_qty"] / q for tid, q in eff_qty.items())
                if runs <= 0 or runs == float("inf"):
                    continue
                cost_per_run = sum(q * reached[tid]["unit_cost"] for tid, q in eff_qty.items())
                reaction_count = 1 + sum(reached[inp["type_id"]]["reaction_count"] for inp in f["inputs"])
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
                    "max_qty": int(runs) * f["output_qty"],
                    "reaction_count": reaction_count,
                    "via": {"reaction_id": f["reaction_id"], "cycle_time": f["cycle_time"],
                            "inputs": f["inputs"]},
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


def _build_opportunities(context_id: int) -> list[dict]:
    # try/finally is load-bearing: an exception between get_connection() and close() (e.g. the
    # reactions/reaction_inputs tables not existing yet on a freshly-deployed environment) would
    # otherwise leak the connection permanently out of the small per-pod pool.
    con = get_connection()
    try:
        goo_rows = con.execute("SELECT type_id, sell_price, stock FROM pp_moon_goo_prices").fetchall()
        goo = {r["type_id"]: {"sell_price": r["sell_price"], "stock": r["stock"]} for r in goo_rows}
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
    finally:
        con.close()

    if not goo:
        return []

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
    # Moon goo itself gets no import cost added (confirmed: already at/near the reaction site).
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    purchasable_ids = [tid for tid in all_input_ids if tid not in goo]
    purchasable_market = fetch_market_data(purchasable_ids)
    purchasable = {
        tid: m["sell_price"] + settings["import_isk_per_m3"] * (types.get(tid, {}).get("volume") or 0.0)
        for tid, m in purchasable_market.items()
    }

    reached = _resolve_reachable(goo, purchasable, reactions_by_output)
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
def reactions_opportunities(context_id: int = Depends(require_b0ss)):
    return {"opportunities": _build_opportunities(context_id)}
