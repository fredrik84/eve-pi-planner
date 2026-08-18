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
    _load_goo_and_reached, _explode_shopping_list, _ordered_chain_tiers, reaction_stock_pool, _stock_covered_report,
    _materials_report, _value_reaction_batch,
)
from app.reactions.jobs import (
    _character_capacities, ensure_reaction_orders_table, ensure_reaction_assignments_table,
    _allocate_and_insert, formula_concurrency_caps, _cap_jobs, give_back_order_runs,
    live_reaction_runs, _invalidate_dashboard_cache,
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
            "profit": {"client_price": order.get("client_price"), "price_per_unit": None,
                        "profit": None, "margin_pct": None},
            "time": {"tiers": [], "free_slots_now": 0, "estimated_hours": None, "caveat": None,
                      "formula_capped": []},
            "missing_formulas": {"complete": False, "formulas": [], "unresolved": []},
            "stock_covered": [],
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

    # What the job is WORTH, which for an order is whatever the client agreed to pay — the one
    # figure nothing here can derive. Absent a price the answer is "not known", never 0: the
    # dashboard used to report an unpriced order as zero profit, which reads as "this earns
    # nothing" rather than "nobody has said". `None` all the way through keeps the two apart.
    price = order.get("client_price")
    price = float(price) if price not in (None, "") else None
    profit = {
        "client_price": price,
        "price_per_unit": round(price / target_qty, 2) if (price and target_qty) else None,
        "profit": round(price - total_cost, 2) if price is not None else None,
        # Margin on the PRICE (what fraction of the invoice is yours to keep), not markup on cost —
        # it is the number that compares against a market sale, which is also a share of revenue.
        "margin_pct": round((price - total_cost) / price * 100, 1) if price else None,
    }

    # Two SEPARATE pools off the same holding, on purpose: the materials list and the stage list
    # answer different questions about the same order ("what must I buy" / "what must I react"),
    # and each must spend the holding once. Sharing one consumed pool between them would let the
    # materials walk eat the units the stages needed to be told about.
    # **Sized off the PLAN ROWS once the order has any, not off `target_qty`.** The two diverge the
    # moment the levelling pass rounds a run count up: an order for 1000 top-level runs was being
    # planned as 1130 runs of each intermediate, so the report under-stated the intermediates'
    # inputs by 13% — and the report is what people buy from. A plan row is one in-game job, which
    # is the unit the game does material arithmetic in (`_plan_materials`), so this also picks up
    # the per-job rounding the aggregate walk cannot express.
    #
    # Falls back to the target-quantity walk when nothing is assigned yet, which is the quote
    # before any commitment — there are no rows to read and the ideal is the honest answer.
    totals: dict[int, float] = {}
    plan_rows: list[dict] = []
    if order.get("id"):
        con = get_connection()
        try:
            plan_rows = [dict(r) for r in con.execute(
                "SELECT a.character_id, a.type_id, a.name, a.runs, a.tier_order "
                "FROM pp_reaction_assignments a WHERE a.order_id=?", (order["id"],))]
        except Exception:
            plan_rows = []
        finally:
            con.close()
    if plan_rows:
        from app.reactions.graph import _plan_materials
        totals = _plan_materials(plan_rows, reached, dict(reaction_stock_pool(context_id)))
    else:
        _explode_shopping_list(order["type_id"], target_qty, reached, totals,
                               dict(reaction_stock_pool(context_id)))
    materials = _materials_report(totals, reached, types)

    stock_covered: dict[int, dict] = {}
    ordered_tiers = _ordered_chain_tiers(formula["inputs"], top_level_runs, reached,
                                          reaction_stock_pool(context_id), stock_covered)
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
        by_slots = max(1, min(free_slots_now, tier["runs"]))
        jobs_used = _cap_jobs(cap, by_slots)
        if cap and cap < by_slots:
            formula_capped.append(tier["name"])
        tier["formula_cap"] = cap
        estimated_hours += math.ceil(tier["runs"] / jobs_used) * cycle_hours

    # **No free reactors means no estimate, not a one-reactor estimate.** With `free_slots_now` at
    # zero the loop above used to fall back to a single job per tier — the whole order run end to
    # end in one reactor — and quoted the customer that number. On a fully-assigned account that is
    # how "~51d 3h" appeared on an order the tool would not even accept: `_allocate_and_insert`
    # refuses outright when no character has a free slot per tier, so the figure described a layout
    # that was never on offer. A quote nobody can install is worse than saying we cannot say yet.
    no_capacity = free_slots_now <= 0
    time_report = {
        "tiers": sequence, "free_slots_now": free_slots_now,
        "estimated_hours": None if no_capacity else round(estimated_hours, 1),
        # Which steps are held below your free-slot count by how many formulas you hold — one line
        # of "why", so an order quoted at ten times the obvious time doesn't read as a broken tool.
        "formula_capped": formula_capped,
        "caveat": ("Every reaction slot you have is already running or planned, so there is nothing "
                   "to estimate against yet — free some up (or clear part of the plan) and this "
                   "will fill in.") if no_capacity else
                  (f"Assumes the {free_slots_now} reaction slot(s) you have free right now stay free "
                   "until each tier finishes, run in sequence (each intermediate tier must finish "
                   "before the next starts) — a rough estimate, not a guarantee."),
    }

    # Formulas this order needs and the account does not hold. `sequence` is already every step the
    # order runs — tiers deepest-first plus the product itself — so it is exactly the list to ask
    # about, and asking off it means a tier can never be left out of the check while being left in
    # the plan. Deliberately NOT in `cost`: what you must go and buy is not what the order costs to
    # produce, and folding it in would quote the client for a formula the user may already own or
    # may decide not to buy. Same separation `missing_blueprints` keeps on the Industry side.
    from app.reactions.library import missing_formulas, wanted_from_sequence, jobs_from_sequence

    return {"materials": materials, "chain_tiers": chain_tiers, "cost": cost, "profit": profit,
            "time": time_report,
            "missing_formulas": missing_formulas(context_id, wanted_from_sequence(sequence),
                                                 jobs=jobs_from_sequence(sequence)),
            # Stages this order does not have to run because the intermediate is already held.
            "stock_covered": _stock_covered_report(stock_covered, types),
            "stale": False}


class OrderCreateRequest(BaseModel):
    type_id: int
    target_qty: float
    client_name: str | None = None
    notes: str | None = None
    # What the client pays for the whole order. Optional, and None means "not told" rather than
    # free — see `_order_report`'s profit block.
    client_price: float | None = None


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
        "client_price": req.client_price if (req.client_price or 0) > 0 else None,
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
            "assigned_runs, client_name, notes, status, created_at, client_price) "
            "VALUES (?,?,?,?,?,0,?,?,'open',?,?) RETURNING id",
            (context_id, req.type_id, name, req.target_qty, top_level_runs,
             (req.client_name or "").strip() or None, (req.notes or "").strip() or None, _time.time(),
             req.client_price if (req.client_price or 0) > 0 else None),
        ).fetchone()[0]
        con.commit()
        order = _order_row(con, order_id)
    finally:
        con.close()
    return {"order": order, **_order_report(context_id, order)}


@router.get("/api/reactions/orders")
def list_reaction_orders(context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        rows = con.execute(
            # `client_price` is here so the list can SAY which orders have no agreed price. The
            # overview counts them ("1 order priced at market, not at the invoice") and then has to
            # send the reader somewhere; without the price on the row, "somewhere" could only ever
            # be the whole card, leaving them to open each order to find the one meant.
            "SELECT id, type_id, name, target_qty, top_level_runs, assigned_runs, client_name, notes, "
            "status, created_at, client_price FROM pp_reaction_orders WHERE context_id=? "
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


def _heal_stranded_counter(order: dict, context_id: int) -> dict:
    """An OPEN order claiming committed runs while holding no assignment rows at all is stranded:
    it looks fully assigned, schedules nothing, and `remaining <= 0` refuses to let the player
    re-assign it. Orders #36-#39 on a real account sat in exactly that shape.

    The counter is the thing that is wrong — the rows are the truth about what is committed — so it
    is reset to zero and the order becomes assignable again. Deliberately narrow: ONLY when there
    are no rows whatsoever. An order with rows (running or pending) has a counter that means
    something, and a fully-assigned order with rows still gets the honest "everything has already
    been assigned" refusal.

    Runs on the ASSIGN path rather than on read: this is a repair, and a repair belongs to a
    deliberate action the player took, not to a GET that happened to load the page.
    """
    if not order.get("assigned_runs"):
        return order
    con = get_connection()
    try:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM pp_reaction_assignments WHERE order_id=? AND character_id IN "
            "(SELECT character_id FROM pp_characters WHERE context_id=?)",
            (order["id"], context_id)).fetchone()["n"] or 0
        if n:
            return order
        con.execute("UPDATE pp_reaction_orders SET assigned_runs=0 WHERE id=?", (order["id"],))
        con.commit()
        return _order_row(con, order["id"])
    except Exception:
        return order                    # a repair must never be the reason an assign fails
    finally:
        con.close()


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
    order = _heal_stranded_counter(order, context_id)
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
        order = _order_row(con, order_id)
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    return {"order": order, "runs_assigned": result["runs_assigned"], "characters": result["characters"]}


@router.delete("/api/reactions/orders/{order_id}/assignments")
def clear_reaction_order_assignments(order_id: int, context_id: int = Depends(require_context)):
    """Drop everything this order has committed to reaction slots and hand it back its runs, so it
    can be assigned again from scratch. The order itself survives — status, client, target quantity
    and all.

    The counterpart the order flow was missing. "Assign next batch" only ever adds, `Clear all`
    wipes the whole account, and cancelling the order to free its slots throws the order away — so
    re-planning one order (different characters, a changed batch size, a formula that has since
    been sold) meant clearing everything or recreating the order by hand.

    A row ESI already shows as RUNNING is cleared too, and its in-game job keeps running: it
    becomes an ORPHAN on the dashboard, adoptable back into a plan with one click. That is the same
    thing `Clear all` does, and the count is reported so the UI can say so before the player
    commits to it rather than after.
    """
    ensure_reaction_orders_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
        rows = [dict(r) for r in con.execute(
            "SELECT a.character_id, a.type_id, a.runs, a.order_id, a.created_at, "
            "COALESCE(a.tier_order,0) AS tier_order FROM pp_reaction_assignments a "
            "WHERE a.order_id=? AND a.character_id IN "
            "(SELECT character_id FROM pp_characters WHERE context_id=?)", (order_id, context_id))]
        if not rows:
            # Nothing committed, but the counter may still claim runs — the stranded shape this
            # endpoint exists to make recoverable. Reset it so the order can move again.
            con.execute("UPDATE pp_reaction_orders SET assigned_runs=0 WHERE id=?", (order_id,))
            con.commit()
            _invalidate_dashboard_cache(context_id)
            return {"ok": True, "cleared": 0, "runs_returned": order["assigned_runs"] or 0,
                    "running_cleared": 0, "order": _order_row(con, order_id)}
        _release_order_slots(con, order_id, context_id)
        give_back_order_runs(con, rows)
        con.commit()
        after = _order_row(con, order_id)
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    returned = (order["assigned_runs"] or 0) - (after["assigned_runs"] or 0)
    return {"ok": True, "cleared": len(rows), "runs_returned": returned,
            "running_cleared": _running_rows_among(context_id, rows), "order": after}


def _release_order_slots(con, order_id: int, context_id: int) -> int:
    """Delete the plan rows an order holds and return how many there were. Scoped to the account's
    own characters as defence in depth even where the caller has already ownership-checked the
    order (CLAUDE.md rule 8) — one statement, so that scoping cannot drift between callers."""
    return con.execute(
        "DELETE FROM pp_reaction_assignments WHERE order_id=? AND character_id IN "
        "(SELECT character_id FROM pp_characters WHERE context_id=?)", (order_id, context_id)).rowcount


def _order_row(con, order_id: int) -> dict:
    return dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())


def _running_rows_among(context_id: int, rows: list[dict]) -> int:
    """How many of the cleared rows had a live in-game job behind them — the ones that carry on
    running as orphans. Counted per (character, product) against the cached ESI snapshot, the same
    count-aware matching the dashboard uses, so N running jobs cover exactly N rows."""
    try:
        live = {k: len(v) for k, v in live_reaction_runs(context_id).items()}
        n = 0
        for r in rows:
            key = (int(r["character_id"]), int(r["type_id"]))
            if live.get(key, 0) > 0:
                live[key] -= 1
                n += 1
        return n
    except Exception:
        return 0                        # a best-effort footnote must never fail the clear


class OrderPriceRequest(BaseModel):
    # None or 0 clears it back to "not told" — an order can lose its price as legitimately as it
    # gains one (a deal falls through, a number was typed wrong).
    client_price: float | None = None


@router.post("/api/reactions/orders/{order_id}/price")
def set_reaction_order_price(order_id: int, req: OrderPriceRequest,
                             context_id: int = Depends(require_context)):
    """Set (or clear) what the client pays for an order that already exists.

    The price shipped as a create-form field only, which meant every order made before it — and any
    order where the number was agreed after the work was planned, which is the normal way round —
    could never be given one. Its revenue then stayed unknown forever and the dashboard reported
    the whole plan as earning nothing. Editable is the point: a price is a negotiation, not a
    property of the recipe.
    """
    ensure_reaction_orders_table()
    price = req.client_price if (req.client_price or 0) > 0 else None
    con = get_connection()
    try:
        _get_order_or_404(con, order_id, context_id)
        con.execute("UPDATE pp_reaction_orders SET client_price=? WHERE id=?", (price, order_id))
        con.commit()
        order = _order_row(con, order_id)
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    return {"order": order, **_order_report(context_id, order)}


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
        freed = _release_order_slots(con, order_id, context_id)
        con.execute("UPDATE pp_reaction_orders SET status=? WHERE id=?", (req.status, order_id))
        con.commit()
        order = _order_row(con, order_id)
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
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
