"""Industry planner — the persistent build queue.

A build order is a target product + quantity the player wants made, tracked across sessions (not a
one-shot calculator). Multiple orders queue up; `/api/industry/queue-plan` aggregates demand across
ALL of an account's queued orders and schedules them together against the account's real slot pool
(shared components batch once — see schedule.aggregate_demand). This is the "queue entire builds"
layer; committing jobs to real slots + live ESI job tracking + spawning reaction orders are the
next slices.

`mode` is the per-order serial/parallel lever from the spec (default parallel = fill slots). It's
persisted now; the scheduler treats everything as parallel in this slice — honouring `serial` (run
the final assemblies one-after-another) is a follow-up. Own-account scoped throughout (rule 8).
"""
import time as _time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.markets import resolve_market_data
from app.industry_cost import fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router
from app.industry.graph import (
    load_manufacturing_graph, load_reaction_graph, collect_reachable, resolve_build_params,
    build_plan,
)
from app.industry.schedule import plan_queue
from app.industry.slots import _slot_pool

_VALID_MODES = ("parallel", "serial")
_VALID_STATUSES = ("queued", "done", "cancelled")


@ensure_once
def ensure_industry_orders_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id      INTEGER NOT NULL,
                product_type_id INTEGER NOT NULL,
                name            TEXT NOT NULL,
                quantity        INTEGER NOT NULL,
                mode            TEXT NOT NULL DEFAULT 'parallel',
                priority        INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'queued',
                created_at      REAL NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ind_orders_ctx ON pp_industry_orders (context_id)")
        # Who/what this order is for — a customer name, a contract, a fleet. Several identical
        # Revelations are indistinguishable without it, which is exactly the case that matters.
        try:
            con.execute("ALTER TABLE pp_industry_orders ADD COLUMN label TEXT DEFAULT ''")
            con.commit()
        except Exception:
            pass
        con.commit()
    finally:
        con.close()


class OrderCreate(BaseModel):
    product_type_id: int
    quantity: int = 1
    mode: str = "parallel"
    label: str = ""          # free text: customer, contract, whatever makes it identifiable


class OrderUpdate(BaseModel):
    quantity: int | None = None
    mode: str | None = None
    label: str | None = None
    priority: int | None = None
    status: str | None = None


def _order_row(con, order_id: int, ctx: int):
    row = con.execute(
        "SELECT * FROM pp_industry_orders WHERE id=? AND context_id=?", (order_id, ctx)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="order not found")
    return dict(row)


@router.post("/api/industry/orders")
def create_order(req: OrderCreate, ctx: int = Depends(require_context)):
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    if req.mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {_VALID_MODES}")
    ensure_industry_orders_table()
    con = get_connection()
    try:
        name = None
        r = con.execute("SELECT name FROM types WHERE type_id=?", (req.product_type_id,)).fetchone()
        if r:
            name = r["name"]
        # Must be buildable — no point queuing a raw material.
        buildable = con.execute(
            "SELECT 1 FROM blueprints WHERE product_type_id=? "
            "UNION SELECT 1 FROM reactions WHERE output_type_id=? LIMIT 1",
            (req.product_type_id, req.product_type_id),
        ).fetchone()
        if not buildable:
            raise HTTPException(status_code=400, detail="that type has no manufacturing or reaction recipe")
        # RETURNING id — cur.lastrowid is None on Postgres (prod), which made the follow-up lookup
        # 404 ("order not found") even though the insert committed.
        oid = con.execute(
            "INSERT INTO pp_industry_orders (context_id, product_type_id, name, quantity, mode, "
            "priority, status, created_at, label) VALUES (?,?,?,?,?,?, 'queued', ?,?) RETURNING id",
            (ctx, req.product_type_id, name or str(req.product_type_id), req.quantity, req.mode,
             int(_time.time()), _time.time(), (req.label or "").strip()[:60]),
        ).fetchone()[0]
        con.commit()
        return _order_row(con, oid, ctx)
    finally:
        con.close()


@router.get("/api/industry/orders")
def list_orders(ctx: int = Depends(require_context)):
    ensure_industry_orders_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT * FROM pp_industry_orders WHERE context_id=? AND status='queued' "
            "ORDER BY priority ASC, created_at ASC", (ctx,),
        ).fetchall()
        return {"orders": [dict(r) for r in rows]}
    finally:
        con.close()


class OrderReorder(BaseModel):
    order: list[int]          # order ids, first = first in line


@router.post("/api/industry/orders/reorder")
def reorder_orders(req: OrderReorder, ctx: int = Depends(require_context)):
    """Set the queue order. First in the list is first in line.

    This is not cosmetic: the scheduler ranks by queue position, so the first order wins a contested
    slot and its ETA is the "first delivery" figure. Stored as a descending priority because the
    queue reads `ORDER BY priority DESC, id`.
    """
    ensure_industry_orders_table()
    con = get_connection()
    try:
        n = len(req.order)
        for i, oid in enumerate(req.order):
            con.execute(
                "UPDATE pp_industry_orders SET priority=? WHERE id=? AND context_id=?",
                (n - i, oid, ctx),
            )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.patch("/api/industry/orders/{order_id}")
def update_order(order_id: int, req: OrderUpdate, ctx: int = Depends(require_context)):
    ensure_industry_orders_table()
    con = get_connection()
    try:
        _order_row(con, order_id, ctx)  # 404s if not the caller's
        sets, params = [], []
        if req.quantity is not None:
            if req.quantity < 1:
                raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
            sets.append("quantity=?"); params.append(req.quantity)
        if req.mode is not None:
            if req.mode not in _VALID_MODES:
                raise HTTPException(status_code=400, detail=f"mode must be one of {_VALID_MODES}")
            sets.append("mode=?"); params.append(req.mode)
        if req.priority is not None:
            sets.append("priority=?"); params.append(req.priority)
        if req.label is not None:
            sets.append("label=?"); params.append(req.label.strip()[:60])
        if req.status is not None:
            if req.status not in _VALID_STATUSES:
                raise HTTPException(status_code=400, detail=f"status must be one of {_VALID_STATUSES}")
            sets.append("status=?"); params.append(req.status)
        if sets:
            params.extend([order_id, ctx])
            con.execute(f"UPDATE pp_industry_orders SET {', '.join(sets)} WHERE id=? AND context_id=?", params)
            con.commit()
        return _order_row(con, order_id, ctx)
    finally:
        con.close()


@router.delete("/api/industry/orders/{order_id}")
def delete_order(order_id: int, ctx: int = Depends(require_context)):
    ensure_industry_orders_table()
    con = get_connection()
    try:
        _order_row(con, order_id, ctx)
        con.execute("DELETE FROM pp_industry_orders WHERE id=? AND context_id=?", (order_id, ctx))
        con.commit()
        return {"deleted": order_id}
    finally:
        con.close()


class QueuePlanRequest(BaseModel):
    system_id: int | None = None
    me_pct: float = 0.0
    te_pct: float = 0.0
    facility_tax_pct: float | None = None
    mfg_slots: int | None = None
    rx_slots: int | None = None
    prioritize_speed: bool = True
    struct_material_pct: float = 0.0
    struct_time_pct: float = 0.0
    # Subtract what you already own from the demand. On for planning ("what's left to do"); the
    # progress endpoint turns it OFF so it measures against the FULL requirement — otherwise the
    # denominator would shrink as you acquire stock and the bar could never fill.
    use_stock: bool = True
    marginal_pct: float | None = None   # build only if it saves >= this % of the build


def _stock_for(ctx: int, targets) -> dict[int, float]:
    """Owned quantities to net off the demand, EXCLUDING the products being ordered. Owning a
    Revelation must not make an order to build one plan zero jobs — you asked to build it. Only
    intermediates and materials count as stock. Empty when assets were never fetched."""
    from app.industry.assets import owned_quantities
    stock = owned_quantities(ctx)
    for tid, _ in targets:
        stock.pop(tid, None)
    return stock


def _run_queue_plan(ctx: int, req: QueuePlanRequest) -> dict:
    """Shared core of the whole-queue plan: aggregate every queued order's demand and schedule it.
    Returns the plan_queue result, or {"empty": True} when the queue is empty. Used by both the
    queue-plan endpoint and the to-install checklist."""
    ensure_industry_orders_table()
    con = get_connection()
    try:
        # FIFO: oldest order first, so the scheduler's rank matches the order you took the work in.
        # Without an explicit ORDER BY the row order is whatever the DB returns, which would make
        # "first in line" arbitrary.
        orders = con.execute(
            "SELECT product_type_id, quantity FROM pp_industry_orders "
            "WHERE context_id=? AND status='queued' ORDER BY priority DESC, id", (ctx,),
        ).fetchall()
        if not orders:
            return {"targets": [], "empty": True}
        # Combine duplicate products into one target quantity — insertion order is preserved, so the
        # first time a product appears fixes its place in line.
        combined: dict[int, int] = {}
        for o in orders:
            combined[o["product_type_id"]] = combined.get(o["product_type_id"], 0) + o["quantity"]
        targets = list(combined.items())

        mfg = load_manufacturing_graph(con)
        rx = load_reaction_graph(con)
        ids: set[int] = set()
        for tid, _ in targets:
            if tid not in mfg and tid not in rx:
                raise HTTPException(status_code=400, detail=f"queued order {tid} has no recipe")
            ids |= collect_reachable(tid, mfg, rx)
        names = {r["type_id"]: r["name"]
                 for r in con.execute(
                     f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                     tuple(ids))}
    finally:
        con.close()

    prices = resolve_market_data(ctx, list(ids))
    adjusted = fetch_adjusted_prices(list(ids))
    from app.industry.graph import SPEED_BUILD_CAP_HOURS
    mbh = SPEED_BUILD_CAP_HOURS if req.prioritize_speed else 0.0
    params = resolve_build_params(ctx, req.me_pct, req.te_pct, req.system_id, req.facility_tax_pct, mbh,
                                  req.struct_material_pct, req.struct_time_pct, req.marginal_pct)
    # What an unowned blueprint would cost to acquire, so building can be priced honestly and the
    # margin-saver can see it. Best-effort: an empty index just leaves the old behaviour.
    try:
        from app.industry.bpc import acquisition_costs
        params.bp_acquire = acquisition_costs(list(ids), params.owned)
    except Exception:
        params.bp_acquire = {}
    pool = _slot_pool(ctx)
    mfg_slots = req.mfg_slots if req.mfg_slots is not None else pool["manufacturing_slots"]
    rx_slots = req.rx_slots if req.rx_slots is not None else pool["reaction_slots"]
    pools = {"manufacturing": max(1, mfg_slots), "reaction": max(1, rx_slots)}
    on_hand = _stock_for(ctx, targets) if req.use_stock else None
    res = plan_queue(targets, mfg, rx, prices, adjusted, params, names, pools, on_hand=on_hand)
    # The recipe tree per ordered product. plan_queue returns aggregated demand — correct for cost
    # and scheduling, but it has no structure, and the UI derives its build STAGES from the tree.
    # Without this the status view (the main screen) had no pipeline at all and lumped every job
    # into one unlabelled bucket, while the preview modal showed the real stages.
    res["trees"] = [build_plan(t, q, mfg, rx, prices, adjusted, params, names)["tree"]
                    for t, q in targets]
    return res


@router.post("/api/industry/queue-plan")
def queue_plan(req: QueuePlanRequest, ctx: int = Depends(require_context)):
    """Aggregate demand across ALL of the account's queued build orders and schedule them together
    against the account's slot pool — the honest, shared-batch plan for the whole queue."""
    return _run_queue_plan(ctx, req)


@router.get("/api/industry/to-install")
def to_install(ctx: int = Depends(require_context)):
    """What to start RIGHT NOW: the ready wave of the queue plan (jobs whose inputs are all
    available), your free slot counts, and how many of the ready jobs fit those free slots. The
    actionable, least-effort answer to 'what should I be doing'."""
    res = _run_queue_plan(ctx, QueuePlanRequest())
    if res.get("empty"):
        return {"empty": True}
    from app.industry.slots import _slot_pool
    pool = _slot_pool(ctx)
    free = {"manufacturing": pool["manufacturing_free"], "reaction": pool["reaction_free"]}
    waves = res["schedule"]["waves"]
    ready = list(waves[0]["tasks"]) if waves else []

    # Assign each ready job to a SPECIFIC character with a free slot, rather than just reporting
    # that "a slot" exists somewhere. We know each character's free manufacturing/reaction slots, so
    # the checklist can name who installs what — the difference between "slot free" and an
    # instruction you can actually follow. Most-loaded-first keeps a single toon from hogging the
    # list while others idle; the schedule already ordered wave 0 by criticality.
    avail = {c["character_id"]: {"name": c["character_name"],
                                 "manufacturing": c["manufacturing_free"],
                                 "reaction": c["reaction_free"]}
             for c in pool.get("characters", [])}
    for t in ready:
        act = t["activity"]
        pick = max(avail.items(), key=lambda kv: kv[1].get(act, 0), default=(None, None))
        cid, info = pick
        if cid is not None and info and info.get(act, 0) > 0:
            info[act] -= 1
            t["fits_now"] = True
            t["character_id"] = cid
            t["character_name"] = info["name"]
        else:
            t["fits_now"] = False
            t["character_id"] = None
            t["character_name"] = None

    # Per-character view so the UI can show slots the way the Reactions dashboard does: how many of
    # each pool are busy, free, and about to be filled by this checklist.
    assigned_by_char: dict[int, int] = {}
    for t in ready:
        if t.get("character_id") is not None:
            assigned_by_char[t["character_id"]] = assigned_by_char.get(t["character_id"], 0) + 1
    chars = []
    for c in pool.get("characters", []):
        cid = c["character_id"]
        chars.append({
            "character_id": cid, "character_name": c["character_name"],
            "manufacturing_slots": c["manufacturing_slots"], "manufacturing_free": c["manufacturing_free"],
            "reaction_slots": c["reaction_slots"], "reaction_free": c["reaction_free"],
            "assigned": assigned_by_char.get(cid, 0),
            "jobs": [{"name": t.get("name"), "type_id": t["type_id"], "runs": t["runs"],
                      "activity": t["activity"], "duration_hours": t["duration_hours"]}
                     for t in ready if t.get("character_id") == cid],
        })
    chars.sort(key=lambda c: (-c["assigned"], c["character_name"] or ""))

    return {
        "ready": ready,
        "free": free,
        "characters": chars,
        "unassigned": [t for t in ready if not t.get("fits_now")],
        "fit_count": sum(1 for t in ready if t.get("fits_now")),
        "makespan_hours": res["metrics"]["makespan_hours"],
        "later_waves": max(0, len(waves) - 1),
    }
