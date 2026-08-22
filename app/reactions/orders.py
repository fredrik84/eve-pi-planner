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

    # An order report is pure PRODUCTION cost (materials + job install) — no shipping/collateral and
    # no markup (the user decides what to charge), so it uses _value_reaction_batch with empty
    # settings: fixed_costs then reduces to material + job.
    v = _value_reaction_batch(node, top_level_runs * output_qty, sell_price=0.0, volume=0.0, settings={})
    if plan_rows:
        # Same rounding gap the materials fix above closes, applied to the OTHER half of "cost to
        # produce": `v["input_cost"]`/`v["job_cost"]` are idealized off `top_level_runs` and never
        # see the levelling pass's real, padded job counts. `materials` already carries the real
        # total for goo — reuse it rather than a second walk. Job-install fees need their own sum:
        # `own_job_cost_per_run` is a row's OWN fee only, never rolled up into its children's (each
        # tier already has its own plan row, so rolling children in here would double-count them).
        material_cost = sum(m["unit_cost"] * m["quantity"] for m in materials)
        job_cost = sum(int(r["runs"] or 0) * reached[int(r["type_id"])].get("own_job_cost_per_run", 0.0)
                       for r in plan_rows if reached.get(int(r["type_id"])))
    else:
        # The preview's shopping list above is already net of pasted/scanned hangar stock. Cost
        # must price those SAME rows: using the graph's full `input_cost` here made an unassigned
        # order say e.g. 358m of materials beside a 260.95m shopping list, then silently switch to
        # 260.95m after assignment when the plan-row branch above took over. Besides contradicting
        # one screen, that made the quoted profit change merely because Assign was clicked. Stock
        # the account already holds is not part of this batch's shopping outlay.
        material_cost = sum(m["unit_cost"] * m["quantity"] for m in materials)
        job_cost = v["job_cost"]
    total_cost = material_cost + job_cost
    cost = {
        "material_cost": round(material_cost, 2), "job_cost": round(job_cost, 2),
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
    recurring_interval_days: float | None = None


def _next_order_priority(con, context_id: int) -> int:
    row = con.execute("SELECT COALESCE(MAX(priority),0)+1 AS priority FROM pp_reaction_orders "
                      "WHERE context_id=? AND status='open'", (context_id,)).fetchone()
    return int(row["priority"] if row else 1)


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
    recurring_days = float(req.recurring_interval_days or 0)
    if recurring_days < 0 or recurring_days > 365:
        raise HTTPException(status_code=400, detail="Recurring cadence must be between 0 and 365 days")
    recurring_days = recurring_days or None
    now = _time.time()
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order_id = con.execute(
            "INSERT INTO pp_reaction_orders (context_id, type_id, name, target_qty, top_level_runs, "
            "assigned_runs, client_name, notes, status, created_at, client_price, "
            "recurring_interval_days, recurring_next_at, priority) "
            "VALUES (?,?,?,?,?,0,?,?,'open',?,?,?,?,?) RETURNING id",
            (context_id, req.type_id, name, req.target_qty, top_level_runs,
             (req.client_name or "").strip() or None, (req.notes or "").strip() or None, now,
             req.client_price if (req.client_price or 0) > 0 else None,
             recurring_days, now if recurring_days else None, _next_order_priority(con, context_id)),
        ).fetchone()[0]
        con.commit()
        order = _order_row(con, order_id)
    finally:
        con.close()
    payload = {"order": order, **_order_report(context_id, order)}
    # Creating an order is the instruction to put its work in the queue.  Recurrence controls when
    # another batch is released; it must not be the hidden switch that makes the first batch claim
    # slots. Creation itself still succeeds when the account is full so the user can decide which
    # existing work to move or leave this order waiting.
    try:
        assigned = assign_reaction_order(order_id, OrderAssignRequest(), context_id)
        payload["order"] = assigned["order"]
        payload["auto_assigned"] = assigned
        if recurring_days and assigned["order"]["assigned_runs"] >= assigned["order"]["top_level_runs"]:
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET recurring_next_at=? WHERE id=?",
                            (now + recurring_days * 86400, order_id))
                con.commit()
                payload["order"] = _order_row(con, order_id)
            finally:
                con.close()
        elif assigned["order"]["assigned_runs"] < assigned["order"]["top_level_runs"]:
            payload["auto_assign_error"] = "Not enough free reaction slots to assign the whole order."
            if recurring_days:
                con = get_connection()
                try:
                    con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?",
                                (payload["auto_assign_error"], order_id))
                    con.commit()
                    payload["order"] = _order_row(con, order_id)
                finally:
                    con.close()
    except HTTPException as exc:
        payload["auto_assign_error"] = exc.detail
        if recurring_days:
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?",
                            (str(exc.detail), order_id))
                con.commit()
                payload["order"] = _order_row(con, order_id)
            finally:
                con.close()
    return payload


@router.get("/api/reactions/orders")
def list_reaction_orders(context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    # A due recurring order with no current cycle claimed is released automatically when the
    # Reactions surface refreshes. Failures are per-order and non-fatal: a full account must still
    # get its order list, where the zero-assigned due batch is visible and will be retried.
    con = get_connection()
    try:
        due = [r["id"] for r in con.execute(
            "SELECT id FROM pp_reaction_orders WHERE context_id=? AND status='open' "
            "AND recurring_interval_days>0 AND recurring_next_at<=? AND assigned_runs<top_level_runs",
            (context_id, _time.time())).fetchall()]
    finally:
        con.close()
    for order_id in due:
        try:
            result = assign_reaction_order(order_id, OrderAssignRequest(), context_id)
            con = get_connection()
            try:
                complete = result["order"]["assigned_runs"] >= result["order"]["top_level_runs"]
                error = None if complete else "Not enough free reaction slots to assign the whole recurring batch."
                if complete:
                    row = _order_row(con, order_id)
                    nxt = float(row.get("recurring_next_at") or _time.time())
                    interval = float(row["recurring_interval_days"]) * 86400
                    while nxt <= _time.time():
                        nxt += interval
                    con.execute("UPDATE pp_reaction_orders SET recurring_error=NULL, recurring_next_at=? "
                                "WHERE id=?", (nxt, order_id))
                else:
                    con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?", (error, order_id))
                con.commit()
            finally:
                con.close()
        except HTTPException as exc:
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?", (str(exc.detail), order_id))
                con.commit()
            finally:
                con.close()
    con = get_connection()
    try:
        rows = con.execute(
            # `client_price` is here so the list can SAY which orders have no agreed price. The
            # overview counts them ("1 order priced at market, not at the invoice") and then has to
            # send the reader somewhere; without the price on the row, "somewhere" could only ever
            # be the whole card, leaving them to open each order to find the one meant.
            "SELECT id, type_id, name, target_qty, top_level_runs, assigned_runs, client_name, notes, "
            "status, created_at, client_price, recurring_interval_days, recurring_next_at, recurring_error, "
            "priority, source_kind, source_order_id, source_ref, source_state, source_message "
            "FROM pp_reaction_orders WHERE context_id=? "
            "ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, priority DESC, created_at DESC",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    orders = [dict(r) for r in rows]
    _attach_manufacturing_sources(context_id, orders)
    return {"orders": orders}


def _attach_manufacturing_sources(context_id: int, orders: list[dict]) -> None:
    """Expose the Manufacturing owners behind a physical reaction order.

    The bridge stores exact run shares, but an id and a source_ref are backend bookkeeping rather
    than something a builder can recognise.  Resolve labels here, under the same context boundary,
    so Reactions can link back without a second endpoint or leaking whether another account's order
    exists.
    """
    linked = [int(o["id"]) for o in orders if o.get("source_kind") == "manufacturing"]
    if not linked:
        return
    marks = ",".join("?" * len(linked))
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT s.reaction_order_id,s.manufacturing_order_id,s.runs,i.name,i.label "
            "FROM pp_reaction_order_sources s JOIN pp_industry_orders i "
            "ON i.id=s.manufacturing_order_id AND i.context_id=s.context_id "
            f"WHERE s.context_id=? AND s.reaction_order_id IN ({marks}) "
            "ORDER BY s.reaction_order_id,i.priority DESC,i.id",
            (context_id, *linked)).fetchall()
    except Exception:
        rows = []                 # rolling deploy / pre-bridge database: links are enhancement only
    finally:
        con.close()
    by_order: dict[int, list[dict]] = {}
    for row in rows:
        label = (row["label"] or "").strip()
        by_order.setdefault(int(row["reaction_order_id"]), []).append({
            "order_id": int(row["manufacturing_order_id"]),
            "runs": int(row["runs"] or 0),
            "label": label or row["name"],
            "product_name": row["name"],
        })
    for order in orders:
        order["manufacturing_sources"] = by_order.get(int(order["id"]), [])


class OrderReorderRequest(BaseModel):
    order: list[int]


@router.post("/api/reactions/orders/reorder")
def reorder_reaction_orders(req: OrderReorderRequest,
                            context_id: int = Depends(require_context)):
    """Persist the queue order used by automatic reaction allocation."""
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        open_ids = {int(r["id"]) for r in con.execute(
            "SELECT id FROM pp_reaction_orders WHERE context_id=? AND status='open'",
            (context_id,)).fetchall()}
        sent = [int(i) for i in req.order]
        if len(sent) != len(set(sent)) or set(sent) != open_ids:
            raise HTTPException(status_code=400, detail="Order list must contain every open reaction order once")
        n = len(sent)
        for i, order_id in enumerate(sent):
            con.execute("UPDATE pp_reaction_orders SET priority=? WHERE id=? AND context_id=?",
                        (n - i, order_id, context_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def _linked_quantity_decision(desired: int, committed: int, current: int) -> str:
    """keep, resize, or conflict — committed work is the hard lower bound."""
    if desired == current:
        return "keep"
    return "resize" if desired >= committed else "conflict"


def _active_linked_top_runs(con, order_id: int) -> int:
    """Top-tier runs still reserved now; unlike assigned_runs, completed history is excluded."""
    tier = con.execute("SELECT MAX(COALESCE(tier_order,0)) AS tier FROM pp_reaction_assignments "
                       "WHERE order_id=?", (order_id,)).fetchone()
    if not tier or tier["tier"] is None:
        return 0
    row = con.execute("SELECT COALESCE(SUM(runs),0) AS runs FROM pp_reaction_assignments "
                      "WHERE order_id=? AND COALESCE(tier_order,0)=?", (order_id, tier["tier"])).fetchone()
    return int((row and row["runs"]) or 0)


def _automatic_block_state(detail: str) -> str:
    text = detail.lower()
    return "missing_formula" if "formula" in text or "reachable" in text or "priced material" in text \
        else "capacity_blocked"


def linked_manufacturing_reaction_orders(context_id: int, manufacturing_order_ids: list[int]) -> list[dict]:
    """Physical reaction orders owned by any of the supplied Manufacturing builds."""
    ids = sorted({int(i) for i in manufacturing_order_ids if int(i or 0) > 0})
    if not ids:
        return []
    ensure_reaction_orders_table()
    marks = ",".join("?" * len(ids))
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT DISTINCT o.id AS order_id,o.type_id,o.priority FROM pp_reaction_orders o "
            "JOIN pp_reaction_order_sources s ON s.reaction_order_id=o.id AND s.context_id=o.context_id "
            f"WHERE o.context_id=? AND o.status='open' AND s.manufacturing_order_id IN ({marks}) "
            "ORDER BY o.priority DESC,o.id", (context_id, *ids)).fetchall()
        return [{"order_id": int(r["order_id"]), "type_id": int(r["type_id"])} for r in rows]
    finally:
        con.close()


def sync_manufacturing_reaction_orders(context_id: int, demands: list[dict]) -> dict:
    """Publish ready Manufacturing demand once, then let Reactions own its execution."""
    ensure_reaction_orders_table()
    resolved = []
    for demand in demands:
        type_id = int(demand["type_id"])
        con = get_connection()
        try:
            recipe = con.execute("SELECT output_qty FROM reactions WHERE output_type_id=?",
                                 (type_id,)).fetchone()
        finally:
            con.close()
        target_qty = float(demand["runs"]) * float(recipe["output_qty"] if recipe else 1)
        request = OrderCreateRequest(type_id=type_id, target_qty=target_qty)
        name, top_runs = _resolve_order_target(context_id, request)
        resolved.append((demand, request, name, top_runs))
    created: list[int] = []
    linked_orders: list[dict] = []
    conflicts: list[dict] = []
    con = get_connection()
    try:
        for demand, request, name, top_runs in resolved:
            source_order_id = int(demand.get("order_id") or 0)
            source_ref = str(demand.get("source_ref") or f"order:{source_order_id}")
            owners = [{"order_id": int(o["order_id"]), "runs": int(o["runs"])}
                      for o in (demand.get("owners") or []) if int(o.get("runs") or 0) > 0]
            type_id = int(demand["type_id"])
            existing = con.execute(
                "SELECT * FROM pp_reaction_orders WHERE context_id=? AND source_kind='manufacturing' "
                "AND source_ref=? AND type_id=? AND status='open' LIMIT 1",
                (context_id, source_ref, type_id)).fetchone()
            # A shared batch survives one owner leaving or joining. Find it through the bridge and
            # move its canonical ref instead of creating a second physical reaction order.
            if not existing and owners:
                owner_ids = [o["order_id"] for o in owners]
                marks = ",".join("?" * len(owner_ids))
                existing = con.execute(
                    "SELECT o.* FROM pp_reaction_orders o JOIN pp_reaction_order_sources s "
                    "ON s.reaction_order_id=o.id WHERE o.context_id=? AND o.source_kind='manufacturing' "
                    "AND o.type_id=? AND o.status='open' AND s.manufacturing_order_id IN (" + marks + ") "
                    "ORDER BY o.id LIMIT 1", (context_id, type_id, *owner_ids)).fetchone()
            if existing:
                existing = dict(existing)
                con.execute("UPDATE pp_reaction_orders SET source_ref=?, source_order_id=? WHERE id=?",
                            (source_ref, source_order_id or None, existing["id"]))
                desired = int(top_runs)
                committed = _active_linked_top_runs(con, int(existing["id"]))
                current = int(existing.get("top_level_runs") or 0)
                decision = _linked_quantity_decision(desired, committed, current)
                if decision == "resize":
                    historical = min(int(existing.get("assigned_runs") or 0), desired)
                    con.execute("UPDATE pp_reaction_orders SET target_qty=?, top_level_runs=?, "
                                "assigned_runs=?, source_state=NULL, source_message=NULL WHERE id=?",
                                (request.target_qty, desired, historical, existing["id"]))
                elif decision == "conflict":
                    msg = (f"Manufacturing now needs {desired} runs, but {committed} are already "
                           "committed. Keep the surplus or clear/replan this reaction order.")
                    con.execute("UPDATE pp_reaction_orders SET source_state='quantity_conflict', "
                                "source_message=? WHERE id=?", (msg, existing["id"]))
                    conflicts.append({"order_id": int(existing["id"]), "detail": msg})
                con.execute("DELETE FROM pp_reaction_order_sources WHERE reaction_order_id=?",
                            (existing["id"],))
                for owner in owners:
                    con.execute("INSERT INTO pp_reaction_order_sources "
                                "(reaction_order_id,context_id,manufacturing_order_id,runs) "
                                "VALUES (?,?,?,?)", (existing["id"], context_id,
                                                     owner["order_id"], owner["runs"]))
                linked_orders.append({"order_id": int(existing["id"]), "type_id": type_id,
                                      "owners": owners})
                continue
            now = _time.time()
            order_id = con.execute(
                "INSERT INTO pp_reaction_orders (context_id,type_id,name,target_qty,top_level_runs,"
                "assigned_runs,client_name,notes,status,created_at,priority,source_kind,source_order_id,source_ref) "
                "VALUES (?,?,?,?,?,0,NULL,?,'open',?,?, 'manufacturing',?,?) RETURNING id",
                (context_id, type_id, name, request.target_qty, top_runs,
                 "Ready work from Manufacturing", now, int(demand.get("priority") or 0),
                 source_order_id or None, source_ref)).fetchone()[0]
            created.append(int(order_id))
            linked_orders.append({"order_id": int(order_id), "type_id": type_id,
                                  "owners": owners})
            for owner in owners:
                con.execute("INSERT INTO pp_reaction_order_sources "
                            "(reaction_order_id,context_id,manufacturing_order_id,runs) VALUES (?,?,?,?)",
                            (order_id, context_id, owner["order_id"], owner["runs"]))
        con.commit()
    finally:
        con.close()

    con = get_connection()
    try:
        pending = [int(r["id"]) for r in con.execute(
            "SELECT id FROM pp_reaction_orders WHERE context_id=? AND source_kind='manufacturing' "
            "AND status='open' AND COALESCE(source_state,'')='' "
            "AND assigned_runs<top_level_runs ORDER BY priority DESC,id",
            (context_id,)).fetchall()]
    finally:
        con.close()
    assigned, shortfalls = [], []
    for order_id in pending:
        try:
            result = assign_reaction_order(order_id, OrderAssignRequest(), context_id)
            assigned.append({"order_id": order_id, "runs": result["runs_assigned"]})
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET source_state=NULL, source_message=NULL "
                            "WHERE id=?", (order_id,))
                con.commit()
            finally:
                con.close()
        except HTTPException as exc:
            detail = str(exc.detail)
            shortfalls.append({"order_id": order_id, "detail": detail})
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET source_state=?, source_message=? WHERE id=?",
                            (_automatic_block_state(detail), detail, order_id))
                con.commit()
            finally:
                con.close()
    return {"created": created, "assigned": assigned,
            "shortfalls": shortfalls, "conflicts": conflicts, "orders": linked_orders}


def retry_automatic_orders(context_id: int) -> dict:
    """Retry work the application owns after capacity may have changed, in visible priority order.

    One-off customer orders remain manual. Manufacturing hand-offs and due/previously-blocked
    recurring cycles are automatic by contract. A failure is recorded on the order and does not
    stop lower-priority rows being inspected, while the allocator itself remains the authority on
    whether any capacity really exists.
    """
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM pp_reaction_orders WHERE context_id=? AND status='open' "
            "AND assigned_runs<top_level_runs AND (source_kind='manufacturing' OR "
            "(recurring_interval_days>0 AND (recurring_next_at<=? OR recurring_error IS NOT NULL))) "
            "ORDER BY priority DESC,id", (context_id, _time.time())).fetchall()]
    finally:
        con.close()
    assigned, blocked = [], []
    for order in rows:
        try:
            result = assign_reaction_order(int(order["id"]), OrderAssignRequest(), context_id)
            assigned.append({"order_id": int(order["id"]), "runs": int(result["runs_assigned"])})
            con = get_connection()
            try:
                current = _order_row(con, int(order["id"]))
                nxt = current.get("recurring_next_at")
                days = float(current.get("recurring_interval_days") or 0)
                if days > 0 and current["assigned_runs"] >= current["top_level_runs"]:
                    nxt = float(nxt or _time.time())
                    while nxt <= _time.time():
                        nxt += days * 86400
                con.execute("UPDATE pp_reaction_orders SET source_state=NULL,source_message=NULL,"
                            "recurring_error=NULL,recurring_next_at=? WHERE id=?", (nxt, order["id"]))
                con.commit()
            finally:
                con.close()
        except HTTPException as exc:
            detail = str(exc.detail)
            blocked.append({"order_id": int(order["id"]), "detail": detail})
            con = get_connection()
            try:
                if order.get("source_kind") == "manufacturing":
                    con.execute("UPDATE pp_reaction_orders SET source_state=?,source_message=? WHERE id=?",
                                (_automatic_block_state(detail), detail, order["id"]))
                else:
                    con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?",
                                (detail, order["id"]))
                con.commit()
            finally:
                con.close()
    if rows:
        _invalidate_dashboard_cache(context_id)
    return {"attempted": len(rows), "assigned": assigned, "blocked": blocked}


def finish_manufacturing_reaction_orders(context_id: int, manufacturing_order_id: int,
                                         status: str) -> dict:
    """Remove one Manufacturing owner's share without orphaning an ESI-running reaction.

    A shared batch remains open for its other owners and shrinks only to its active reservation
    floor. The physical reaction order closes when its final owner leaves.
    """
    ensure_reaction_orders_table()
    ensure_reaction_assignments_table()
    source_ref = f"order:{int(manufacturing_order_id)}"
    con = get_connection()
    try:
        linked = [dict(r) for r in con.execute(
            "SELECT DISTINCT o.* FROM pp_reaction_orders o LEFT JOIN pp_reaction_order_sources s "
            "ON s.reaction_order_id=o.id WHERE o.context_id=? AND o.source_kind='manufacturing' "
            "AND o.status='open' AND (o.source_ref=? OR s.manufacturing_order_id=?)",
            (context_id, source_ref, manufacturing_order_id)).fetchall()]
    finally:
        con.close()
    finished, preserved = [], []
    terminal = "completed" if status == "done" else "cancelled"
    for order in linked:
        con = get_connection()
        try:
            con.execute("DELETE FROM pp_reaction_order_sources WHERE reaction_order_id=? "
                        "AND manufacturing_order_id=?", (order["id"], manufacturing_order_id))
            remaining = [dict(r) for r in con.execute(
                "SELECT manufacturing_order_id,runs FROM pp_reaction_order_sources "
                "WHERE reaction_order_id=? ORDER BY manufacturing_order_id", (order["id"],)).fetchall()]
            if remaining:
                desired = sum(int(r["runs"]) for r in remaining)
                committed = _active_linked_top_runs(con, int(order["id"]))
                ids = ",".join(str(int(r["manufacturing_order_id"])) for r in remaining)
                new_ref = f"shared:{int(order['type_id'])}:{ids}"
                if desired >= committed:
                    recipe = con.execute("SELECT output_qty FROM reactions WHERE output_type_id=?",
                                         (order["type_id"],)).fetchone()
                    output_qty = float(recipe["output_qty"] if recipe else 1)
                    historical = min(int(order.get("assigned_runs") or 0), desired)
                    con.execute("UPDATE pp_reaction_orders SET source_ref=?, source_order_id=NULL, "
                                "target_qty=?, top_level_runs=?, assigned_runs=?, source_state=NULL, "
                                "source_message=NULL WHERE id=?",
                                (new_ref, desired * output_qty, desired, historical, order["id"]))
                else:
                    msg = (f"A Manufacturing owner left this shared batch, but {committed} runs are "
                           f"already committed and the remaining builds need {desired}. Keep the "
                           "surplus or clear/replan this reaction order.")
                    con.execute("UPDATE pp_reaction_orders SET source_ref=?, source_order_id=NULL, "
                                "source_state='quantity_conflict', source_message=? WHERE id=?",
                                (new_ref, msg, order["id"]))
                con.commit()
                continue
            con.commit()
        finally:
            con.close()
        con = get_connection()
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT character_id,type_id,runs,order_id,created_at,COALESCE(tier_order,0) AS tier_order "
                "FROM pp_reaction_assignments WHERE order_id=? AND character_id IN "
                "(SELECT character_id FROM pp_characters WHERE context_id=?)",
                (order["id"], context_id)).fetchall()]
        finally:
            con.close()
        running = _running_rows_among(context_id, rows)
        if running:
            msg = (f"Manufacturing is {status}, but {running} linked reaction job(s) are still "
                   "running in EVE. The reaction order was preserved for you to finish or cancel.")
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET source_state='running_after_finish', "
                            "source_message=? WHERE id=?", (msg, order["id"]))
                con.commit()
            finally:
                con.close()
            preserved.append({"order_id": int(order["id"]), "detail": msg})
            continue
        con = get_connection()
        try:
            freed = _release_order_slots(con, int(order["id"]), context_id)
            con.execute("UPDATE pp_reaction_orders SET status=?, source_state=NULL, "
                        "source_message=NULL WHERE id=?", (terminal, order["id"]))
            con.commit()
        finally:
            con.close()
        finished.append({"order_id": int(order["id"]), "freed_slots": int(freed or 0)})
    if linked:
        _invalidate_dashboard_cache(context_id)
    return {"finished": finished, "preserved": preserved}


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
    _attach_manufacturing_sources(context_id, [order])
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
    if not order.get("assigned_runs") or order.get("source_kind") == "manufacturing":
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
        if order.get("source_kind") == "manufacturing":
            con.execute("UPDATE pp_reaction_orders SET source_state=NULL,source_message=NULL WHERE id=?",
                        (order_id,))
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


class OrderRecurrenceRequest(BaseModel):
    action: str  # retry, skip, or stop


@router.post("/api/reactions/orders/{order_id}/recurrence")
def change_reaction_order_recurrence(order_id: int, req: OrderRecurrenceRequest,
                                     context_id: int = Depends(require_context)):
    """Resolve a recurring release that could not claim enough capacity without guessing for the
    user: retry after they free slots, skip only this cadence point, or stop future recurrence."""
    if req.action not in ("retry", "skip", "stop"):
        raise HTTPException(status_code=400, detail="action must be retry, skip, or stop")
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
        days = float(order.get("recurring_interval_days") or 0)
        if days <= 0:
            raise HTTPException(status_code=400, detail="This order is not recurring")
        if req.action == "stop":
            con.execute("UPDATE pp_reaction_orders SET recurring_interval_days=NULL, "
                        "recurring_next_at=NULL, recurring_error=NULL WHERE id=?", (order_id,))
        elif req.action == "skip":
            nxt = float(order.get("recurring_next_at") or _time.time())
            while nxt <= _time.time():
                nxt += days * 86400
            con.execute("UPDATE pp_reaction_orders SET recurring_next_at=?, recurring_error=NULL WHERE id=?",
                        (nxt, order_id))
        else:
            con.execute("UPDATE pp_reaction_orders SET recurring_error=NULL WHERE id=?", (order_id,))
        con.commit()
    finally:
        con.close()
    if req.action == "retry":
        try:
            result = assign_reaction_order(order_id, OrderAssignRequest(), context_id)
        except HTTPException as exc:
            con = get_connection()
            try:
                con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?",
                            (str(exc.detail), order_id))
                con.commit()
            finally:
                con.close()
            raise
        con = get_connection()
        try:
            current = _order_row(con, order_id)
            complete = current["assigned_runs"] >= current["top_level_runs"]
            error = None if complete else "Not enough free reaction slots to assign the whole recurring batch."
            if complete:
                nxt = float(current.get("recurring_next_at") or _time.time())
                while nxt <= _time.time():
                    nxt += float(current["recurring_interval_days"]) * 86400
                con.execute("UPDATE pp_reaction_orders SET recurring_error=NULL, recurring_next_at=? WHERE id=?",
                            (nxt, order_id))
            else:
                con.execute("UPDATE pp_reaction_orders SET recurring_error=? WHERE id=?", (error, order_id))
            con.commit()
            result["order"] = _order_row(con, order_id)
        finally:
            con.close()
        return result
    con = get_connection()
    try:
        return {"order": _order_row(con, order_id)}
    finally:
        con.close()


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
        current = _get_order_or_404(con, order_id, context_id)
        # Release the slots this order claimed (scoped to the account's own characters as defence in
        # depth — the order is already ownership-checked above).
        freed = _release_order_slots(con, order_id, context_id)
        if req.status == "completed" and (current.get("recurring_interval_days") or 0) > 0:
            interval = float(current["recurring_interval_days"]) * 86400
            next_at = float(current.get("recurring_next_at") or _time.time())
            while next_at <= _time.time():
                next_at += interval
            con.execute("UPDATE pp_reaction_orders SET assigned_runs=0, recurring_next_at=?, "
                        "status='open' WHERE id=?", (next_at, order_id))
        else:
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
        con.execute("DELETE FROM pp_reaction_order_sources WHERE reaction_order_id=? AND context_id=?",
                    (order_id, context_id))
        con.execute("DELETE FROM pp_reaction_orders WHERE id=?", (order_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}
