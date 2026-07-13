"""
Moon-goo reaction profitability ranking — see app/moon_goo.py for the (now group-scoped, see
app.groups) alliance price sheet this reads from. Not part of the PI planner's extractor/factory
distribution algorithm — a separate, unrelated read-only advisory tool.

Starting from whatever's priced (a caller's own group's price sheet, if any, compared against
the open market — see _load_goo_and_reached), walks the reaction graph forward (Simple ->
Composite, any depth) to find every reachable product, and for each computes: cost to make a
run at the achievable quantity (ME-adjusted), value at Jita (both instant-sell/buy and
sell-order/ask, with order-book depth alongside so the caller can judge liquidity), and
shipping+collateral cost to get it there. Ranks by profit but returns every dimension (steps,
profit/m3, volume) un-collapsed — "advice, not a tool": the comparison happens client-side,
this doesn't pick a single winner.

This evaluates each candidate chain IN ISOLATION (as if unlimited supply went to that one
product) — it does not account for competing chains sharing the same raw materials. That
cross-product allocation is what _suggest_reactions' knapsack does, further down.
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
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.cache import cache_get_json, cache_set_json, cache_invalidate, charlist_key
from app.esi import require_context, ESI_BASE, _get_valid_token
from app.groups import member_group, is_group_manager

# The shared router lives in _router so submodules can register endpoints without importing this
# __init__ (which imports them). Settings is the base layer — importing it registers its endpoints
# and re-exports the pricing resolver + helpers the rest of this module (and callers/tests) use.
from app.reactions._router import router
from app.reactions.settings import (  # noqa: F401 — re-exported for the package's public surface
    _RXS_DEFAULTS, _GLOBAL_SETTINGS_GROUP_ID,
    ensure_reaction_settings_table, get_reaction_settings, ensure_account_reaction_settings_table,
    _account_reaction_settings_override, effective_reaction_settings, ReactionSettingsUpdate,
    _resolve_system_id, _validate_reaction_system,
)
# Graph/pricing layer — importing it registers its endpoints (opportunities/fuel-blocks/shopping-
# list) and re-exports the recipe/cost helpers this module's jobs+orders code (and the tests) use.
from app.reactions.graph import (  # noqa: F401 — re-exported for the package's public surface
    SCC_SURCHARGE_PCT, REACTION_ME_REDUCTION,
    _load_reaction_graph, _resolve_reachable, _value_reaction_batch, _load_goo_and_reached,
    _build_opportunities, _build_opportunities_uncached, _fuel_block_ids,
    _explode_shopping_list, _explode_chain_tiers, _materials_report,
)
# Jobs/plan/suggest layer — importing it registers those endpoints and re-exports the helpers the
# customer-order endpoints below build on (slot allocation, assignment inserts, capacities).
from app.reactions.jobs import (  # noqa: F401 — re-exported for the package's public surface
    get_industry_jobs, reaction_slots, refresh_industry_jobs, fetch_industry_jobs,
    fetch_corp_industry_jobs, ensure_industry_jobs_table, ensure_reaction_assignments_table,
    ensure_reaction_orders_table, _insert_assignment_rows, _character_capacities,
    _allocate_and_insert, _suggest_reactions, _build_advisor, _unplanned_running_totals,
    assign_reaction, adopt_orphan_job, unassign_reaction, unassign_all_reactions,
    ChainTier, AssignRequest, AdoptOrphanRequest, SuggestRequest,
)


def _order_report(context_id: int, order: dict) -> dict:
    """Materials/cost/time report for a customer order — recomputed LIVE against current prices
    and the order's OWN stored `top_level_runs`/`target_qty` (a fixed order doesn't rescale with
    market conditions the way the day-cadence opportunity table does). No markup is applied to
    cost — the user decides what to actually charge the client; this only reports what it costs
    to produce."""
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(order["type_id"]) if loaded else None
    if not node or node.get("via") is None:
        return {
            "materials": [], "chain_tiers": [],
            "cost": {"material_cost": None, "job_cost": None, "total_cost": None, "cost_per_unit": None},
            "time": {"tiers": [], "free_slots_now": 0, "estimated_hours": None, "caveat": None},
            "stale": True,
        }
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    formula = node["via"]
    output_qty = formula["output_qty"]
    top_level_runs = order["top_level_runs"]
    target_qty = order["target_qty"]

    # An order report is pure PRODUCTION cost (materials + job install) — no shipping/collateral and
    # no markup (the user decides what to charge), so it uses _value_reaction_batch with empty
    # settings: fixed_costs then reduces to material + job.
    v = _value_reaction_batch(node, top_level_runs * output_qty, sell_price=0.0, volume=0.0, settings={})
    total_cost = v["fixed_costs"]
    cost = {
        "material_cost": round(v["input_cost"], 2), "job_cost": round(v["job_cost"], 2),
        "total_cost": round(total_cost, 2),
        "cost_per_unit": round(total_cost / target_qty, 2) if target_qty else 0.0,
    }

    totals: dict[int, float] = {}
    _explode_shopping_list(order["type_id"], target_qty, reached, totals)
    materials = _materials_report(totals, reached, types)

    tier_runs: dict[int, dict] = {}
    _explode_chain_tiers(formula["inputs"], top_level_runs, reached, tier_runs)
    ordered_tiers = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
    chain_tiers = [
        {"type_id": tid, "name": types.get(tid, {}).get("name", str(tid)), "runs": info["runs"],
         "cycle_time": info["cycle_time"], "output_qty": info["output_qty"]}
        for tid, info in ordered_tiers
    ]

    # Time estimate: chain tiers must finish before the tier above can even start (sequential,
    # not parallel-with-each-other), so durations ADD across tiers — within a single tier, spread
    # its own runs across however many free slots you have right now. An honest approximation,
    # not a guarantee (see the caveat text) — matches this tool's "advice, not a tool" convention
    # rather than presenting false precision.
    free_slots_now = sum(c["free_slots"] for c in _character_capacities(context_id))
    sequence = chain_tiers + [{"type_id": order["type_id"], "name": order["name"], "runs": top_level_runs,
                                "cycle_time": node.get("cycle_time")}]
    estimated_hours = 0.0
    for tier in sequence:
        cycle_hours = (tier["cycle_time"] or 3600) / 3600.0
        jobs_used = min(free_slots_now, tier["runs"]) or 1
        estimated_hours += math.ceil(tier["runs"] / jobs_used) * cycle_hours

    time_report = {
        "tiers": sequence, "free_slots_now": free_slots_now, "estimated_hours": round(estimated_hours, 1),
        "caveat": "Assumes your current free reaction slots stay free until each tier finishes, run in "
                  "sequence (each intermediate tier must finish before the next starts) — a rough "
                  "estimate, not a guarantee.",
    }

    return {"materials": materials, "chain_tiers": chain_tiers, "cost": cost, "time": time_report, "stale": False}


class OrderCreateRequest(BaseModel):
    type_id: int
    target_qty: float
    client_name: str | None = None
    notes: str | None = None


@router.post("/api/reactions/orders")
def create_reaction_order(req: OrderCreateRequest, context_id: int = Depends(require_context)):
    if req.target_qty <= 0:
        raise HTTPException(status_code=400, detail="Target quantity must be positive")
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(req.type_id) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=404, detail="Not a reachable reaction product right now")
    types = loaded[4]
    name = types.get(req.type_id, {}).get("name", str(req.type_id))
    output_qty = node["via"]["output_qty"]
    top_level_runs = max(1, math.ceil(req.target_qty / output_qty))

    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order_id = con.execute(
            "INSERT INTO pp_reaction_orders (context_id, type_id, name, target_qty, top_level_runs, "
            "assigned_runs, client_name, notes, status, created_at) VALUES (?,?,?,?,?,0,?,?,'open',?) "
            "RETURNING id",
            (context_id, req.type_id, name, req.target_qty, top_level_runs,
             (req.client_name or "").strip() or None, (req.notes or "").strip() or None, _time.time()),
        ).fetchone()[0]
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order, **_order_report(context_id, order)}


@router.get("/api/reactions/orders")
def list_reaction_orders(context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, type_id, name, target_qty, top_level_runs, assigned_runs, client_name, notes, "
            "status, created_at FROM pp_reaction_orders WHERE context_id=? "
            "ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, created_at DESC",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return {"orders": [dict(r) for r in rows]}


def _get_order_or_404(con, order_id: int, context_id: int) -> dict:
    row = con.execute(
        "SELECT * FROM pp_reaction_orders WHERE id=? AND context_id=?", (order_id, context_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(row)


@router.get("/api/reactions/orders/{order_id}")
def get_reaction_order(order_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
    finally:
        con.close()
    return {"order": order, **_order_report(context_id, order)}


class OrderAssignRequest(BaseModel):
    runs: int | None = None  # None = assign everything still remaining


@router.post("/api/reactions/orders/{order_id}/assign")
def assign_reaction_order(order_id: int, req: OrderAssignRequest, context_id: int = Depends(require_context)):
    """Commits the next batch of this order's remaining runs to a real reaction slot — see
    _allocate_and_insert. Occupies slots the same way the suggestion/manual-assign flow does;
    `assigned_runs` is monotonic (never decreases here) since committing a slot is a real
    action taken, distinct from the order's own status (still `open` until the player marks it
    delivered — see set_reaction_order_status)."""
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
    finally:
        con.close()
    if order["status"] != "open":
        raise HTTPException(status_code=400, detail="This order isn't open")
    remaining = order["top_level_runs"] - order["assigned_runs"]
    if remaining <= 0:
        raise HTTPException(status_code=400, detail="Every run for this order has already been assigned")
    runs_to_assign = min(req.runs, remaining) if req.runs else remaining
    if runs_to_assign <= 0:
        raise HTTPException(status_code=400, detail="Nothing to assign")

    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(order["type_id"]) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=400, detail="This product isn't reachable right now — check priced materials")
    reached, types = loaded[1], loaded[4]

    result = _allocate_and_insert(context_id, order["type_id"], order["name"], node, reached, types,
                                   runs_to_assign, order_id)
    if result["runs_assigned"] <= 0:
        raise HTTPException(status_code=400, detail=result.get("error") or "No free reaction slots right now")

    con = get_connection()
    try:
        con.execute("UPDATE pp_reaction_orders SET assigned_runs = assigned_runs + ? WHERE id=?",
                     (result["runs_assigned"], order_id))
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order, "runs_assigned": result["runs_assigned"], "characters": result["characters"]}


class OrderStatusRequest(BaseModel):
    status: str  # 'completed' or 'cancelled'


@router.post("/api/reactions/orders/{order_id}/status")
def set_reaction_order_status(order_id: int, req: OrderStatusRequest, context_id: int = Depends(require_context)):
    """Manual override — "I delivered the goods to the client" / "the client backed out". This
    tool has no way to know a real reaction job finished or the goods actually changed hands, so
    completion is always a deliberate player action, never inferred."""
    if req.status not in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'cancelled'")
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        _get_order_or_404(con, order_id, context_id)
        con.execute("UPDATE pp_reaction_orders SET status=? WHERE id=?", (req.status, order_id))
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order}


@router.delete("/api/reactions/orders/{order_id}")
def delete_reaction_order(order_id: int, context_id: int = Depends(require_context)):
    """Only when nothing's been committed yet (assigned_runs == 0) — once real reaction slots
    have been claimed for this order, cancel it instead so the assignment history linked via
    order_id never dangles."""
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
        if order["assigned_runs"] > 0:
            raise HTTPException(status_code=400,
                                 detail="Runs have already been assigned to this order — cancel it instead of deleting")
        con.execute("DELETE FROM pp_reaction_orders WHERE id=?", (order_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}
