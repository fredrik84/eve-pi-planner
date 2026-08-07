"""Fixed-unit customer orders — the top layer of the reactions package. A persistent, trackable
target-quantity job for a client (distinct from the day-cadence profit wizard): compute the runs +
materials + cost + time to produce a fixed number of units, commit them to real reaction slots, and
track status. Builds on the graph (recipe/cost) and jobs (slot allocation) layers."""
import math
import time as _time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.esi import require_context

from app.reactions._router import router
from app.reactions.graph import (
    _load_goo_and_reached, _explode_shopping_list, _ordered_chain_tiers,
    _materials_report, _value_reaction_batch,
)
from app.reactions.jobs import (
    _character_capacities, ensure_reaction_orders_table, ensure_reaction_assignments_table,
    _allocate_and_insert, formula_concurrency_caps, _cap_jobs,
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
            "time": {"tiers": [], "free_slots_now": 0, "estimated_hours": None, "caveat": None,
                      "formula_capped": []},
            "missing_formulas": {"complete": False, "formulas": [], "unresolved": []},
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

    ordered_tiers = _ordered_chain_tiers(formula["inputs"], top_level_runs, reached)
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
    # ...and a tier's runs only spread across as many jobs as it has FORMULAS: the print is locked
    # into the reactor for the job, so free slots are not on their own what a tier can use. This is
    # the number a customer gets quoted, so it has to be the same cap the assign path commits under
    # (`_allocate_and_insert`) or the quote is for an installation the tool would refuse to make.
    # Nothing known about a formula ⇒ no key ⇒ the estimate is exactly what it was.
    free_slots_now = sum(c["free_slots"] for c in _character_capacities(context_id))
    caps = formula_concurrency_caps(context_id)
    sequence = chain_tiers + [{"type_id": order["type_id"], "name": order["name"], "runs": top_level_runs,
                                "cycle_time": node.get("cycle_time")}]
    estimated_hours = 0.0
    formula_capped: list[str] = []
    for tier in sequence:
        cycle_hours = (tier["cycle_time"] or 3600) / 3600.0
        cap = caps.get(tier["type_id"])
        by_slots = min(free_slots_now, tier["runs"]) or 1
        jobs_used = _cap_jobs(cap, by_slots)
        if cap and cap < by_slots:
            formula_capped.append(tier["name"])
        tier["formula_cap"] = cap
        estimated_hours += math.ceil(tier["runs"] / jobs_used) * cycle_hours

    time_report = {
        "tiers": sequence, "free_slots_now": free_slots_now, "estimated_hours": round(estimated_hours, 1),
        # Which steps are held below your free-slot count by how many formulas you hold — one line
        # of "why", so an order quoted at ten times the obvious time doesn't read as a broken tool.
        "formula_capped": formula_capped,
        "caveat": "Assumes your current free reaction slots stay free until each tier finishes, run in "
                  "sequence (each intermediate tier must finish before the next starts) — a rough "
                  "estimate, not a guarantee.",
    }

    # Formulas this order needs and the account does not hold. `sequence` is already every step the
    # order runs — tiers deepest-first plus the product itself — so it is exactly the list to ask
    # about, and asking off it means a tier can never be left out of the check while being left in
    # the plan. Deliberately NOT in `cost`: what you must go and buy is not what the order costs to
    # produce, and folding it in would quote the client for a formula the user may already own or
    # may decide not to buy. Same separation `missing_blueprints` keeps on the Industry side.
    from app.reactions.library import missing_formulas, wanted_from_sequence

    return {"materials": materials, "chain_tiers": chain_tiers, "cost": cost, "time": time_report,
            "missing_formulas": missing_formulas(context_id, wanted_from_sequence(sequence)),
            "stale": False}


class OrderCreateRequest(BaseModel):
    type_id: int
    target_qty: float
    client_name: str | None = None
    notes: str | None = None


def _resolve_order_target(context_id: int, req: OrderCreateRequest) -> tuple[str, int]:
    """Validate a target product + quantity and return (product name, top_level_runs). Shared by the
    preview and create endpoints so they can't drift on reachability or the runs rounding."""
    if req.target_qty <= 0:
        raise HTTPException(status_code=400, detail="Target quantity must be positive")
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(req.type_id) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=404, detail="Not a reachable reaction product right now")
    name = loaded[4].get(req.type_id, {}).get("name", str(req.type_id))
    top_level_runs = max(1, math.ceil(req.target_qty / node["via"]["output_qty"]))
    return name, top_level_runs


@router.post("/api/reactions/orders/preview")
def preview_reaction_order(req: OrderCreateRequest, context_id: int = Depends(require_context)):
    """The review step: the full materials/cost/time report for a would-be order WITHOUT persisting
    it or touching reaction slots. Same math as the created-order report — lets a player see what
    an order needs before committing to it."""
    name, top_level_runs = _resolve_order_target(context_id, req)
    order = {
        "id": None, "type_id": req.type_id, "name": name, "target_qty": req.target_qty,
        "top_level_runs": top_level_runs, "assigned_runs": 0,
        "client_name": (req.client_name or "").strip() or None,
        "notes": (req.notes or "").strip() or None, "status": "preview",
    }
    return {"order": order, "preview": True, **_order_report(context_id, order)}


@router.post("/api/reactions/orders")
def create_reaction_order(req: OrderCreateRequest, context_id: int = Depends(require_context)):
    name, top_level_runs = _resolve_order_target(context_id, req)
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
    completion is always a deliberate player action, never inferred. Either terminal state FREES the
    reaction slots this order had reserved (its pp_reaction_assignments rows) — a completed order's
    jobs are done and a cancelled one's are moot, so neither should keep occupying planned capacity."""
    if req.status not in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'cancelled'")
    ensure_reaction_orders_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        _get_order_or_404(con, order_id, context_id)
        # Release the slots this order claimed (scoped to the account's own characters as defence in
        # depth — the order is already ownership-checked above).
        freed = con.execute(
            "DELETE FROM pp_reaction_assignments WHERE order_id=? AND character_id IN "
            "(SELECT character_id FROM pp_characters WHERE context_id=?)",
            (order_id, context_id)).rowcount
        con.execute("UPDATE pp_reaction_orders SET status=? WHERE id=?", (req.status, order_id))
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order, "freed_slots": freed}


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
