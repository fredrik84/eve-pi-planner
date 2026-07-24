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
        con.commit()
    finally:
        con.close()


class OrderCreate(BaseModel):
    product_type_id: int
    quantity: int = 1
    mode: str = "parallel"


class OrderUpdate(BaseModel):
    quantity: int | None = None
    mode: str | None = None
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
            "priority, status, created_at) VALUES (?,?,?,?,?,?, 'queued', ?) RETURNING id",
            (ctx, req.product_type_id, name or str(req.product_type_id), req.quantity, req.mode,
             int(_time.time()), _time.time()),
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


def _run_queue_plan(ctx: int, req: QueuePlanRequest) -> dict:
    """Shared core of the whole-queue plan: aggregate every queued order's demand and schedule it.
    Returns the plan_queue result, or {"empty": True} when the queue is empty. Used by both the
    queue-plan endpoint and the to-install checklist."""
    ensure_industry_orders_table()
    con = get_connection()
    try:
        orders = con.execute(
            "SELECT product_type_id, quantity FROM pp_industry_orders "
            "WHERE context_id=? AND status='queued'", (ctx,),
        ).fetchall()
        if not orders:
            return {"targets": [], "empty": True}
        # Combine duplicate products into one target quantity.
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
    params = resolve_build_params(ctx, req.me_pct, req.te_pct, req.system_id, req.facility_tax_pct, mbh)
    pool = _slot_pool(ctx)
    mfg_slots = req.mfg_slots if req.mfg_slots is not None else pool["manufacturing_slots"]
    rx_slots = req.rx_slots if req.rx_slots is not None else pool["reaction_slots"]
    pools = {"manufacturing": max(1, mfg_slots), "reaction": max(1, rx_slots)}
    return plan_queue(targets, mfg, rx, prices, adjusted, params, names, pools)


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
    # Annotate each ready job with whether a free slot of its pool is available (greedy fill, most
    # critical first — the schedule already ordered wave 0 by priority within the wave).
    remaining = dict(free)
    for t in ready:
        pool_key = t["activity"]
        if remaining.get(pool_key, 0) > 0:
            t["fits_now"] = True
            remaining[pool_key] -= 1
        else:
            t["fits_now"] = False
    return {
        "ready": ready,
        "free": free,
        "fit_count": sum(1 for t in ready if t.get("fits_now")),
        "makespan_hours": res["metrics"]["makespan_hours"],
        "later_waves": max(0, len(waves) - 1),
    }
