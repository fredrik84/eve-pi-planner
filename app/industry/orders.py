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
import json
import time as _time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.esi import require_context

from app.industry._router import router
from app.industry.graph import BuildOptions, build_plan, prepare_plan_inputs
from app.industry.schedule import plan_queue

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
        # Per-component "build it anyway" overrides decided in the preview. Without this they were
        # a property of the preview only, so queueing the order silently reverted every one of them.
        try:
            con.execute("ALTER TABLE pp_industry_orders ADD COLUMN force_build_ids TEXT DEFAULT ''")
            con.commit()
        except Exception:
            pass
        # Per-product ME/TE the user set while planning. Same reasoning as the overrides above: a
        # decision made in the preview that the queue silently dropped is worse than no decision.
        try:
            con.execute("ALTER TABLE pp_industry_orders ADD COLUMN me_te_overrides TEXT DEFAULT ''")
            con.commit()
        except Exception:
            pass
        # The margin this order was quoted at. Snapshotted rather than read live: a customer holding
        # a price shouldn't see it move because the builder changed their default afterwards.
        try:
            con.execute("ALTER TABLE pp_industry_orders ADD COLUMN margin_pct REAL")
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
    force_build_ids: list[int] = []   # components to build regardless of the buy-shortcuts
    me_te_overrides: dict[str, list[int]] = {}   # {"<type_id>": [me, te]} to assume when planning
    margin_pct: float | None = None             # quote margin; None = the account's current default


class OrderUpdate(BaseModel):
    quantity: int | None = None
    force_build_ids: list[int] | None = None
    me_te_overrides: dict[str, list[int]] | None = None
    margin_pct: float | None = None
    mode: str | None = None
    label: str | None = None
    priority: int | None = None
    status: str | None = None


def _parse_map(value) -> dict:
    """The ME/TE column holds a JSON object; anything unparseable means no overrides."""
    try:
        d = json.loads(value or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _parse_ids(value) -> list[int]:
    """The overrides column holds a JSON array; '' / NULL / anything unparseable means none."""
    try:
        return [int(v) for v in json.loads(value or "[]")]
    except Exception:
        return []


def _order_dict(con, row) -> dict:
    """Row → API shape: the overrides come back as {type_id, name} so the UI can show WHICH
    components were overridden without a second lookup per order."""
    d = dict(row)
    ids = _parse_ids(d.get("force_build_ids"))
    d["force_build_ids"] = ids
    d["me_te_overrides"] = _parse_map(d.get("me_te_overrides"))
    d["force_build"] = []
    if ids:
        names = {r["type_id"]: r["name"] for r in con.execute(
            f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
            tuple(ids))}
        d["force_build"] = [{"type_id": t, "name": names.get(t, str(t))} for t in ids]
    return d


def _order_row(con, order_id: int, ctx: int):
    row = con.execute(
        "SELECT * FROM pp_industry_orders WHERE id=? AND context_id=?", (order_id, ctx)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="order not found")
    return _order_dict(con, row)


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
            "priority, status, created_at, label, force_build_ids, me_te_overrides, margin_pct) "
            "VALUES (?,?,?,?,?,?, 'queued', ?,?,?,?,?) RETURNING id",
            (ctx, req.product_type_id, name or str(req.product_type_id), req.quantity, req.mode,
             int(_time.time()), _time.time(), (req.label or "").strip()[:60],
             json.dumps(sorted({int(t) for t in req.force_build_ids})),
             json.dumps(req.me_te_overrides or {}), req.margin_pct),
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
        return {"orders": [_order_dict(con, r) for r in rows]}
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
        if req.force_build_ids is not None:
            sets.append("force_build_ids=?")
            params.append(json.dumps(sorted({int(t) for t in req.force_build_ids})))
        if req.me_te_overrides is not None:
            sets.append("me_te_overrides=?")
            params.append(json.dumps(req.me_te_overrides))
        if req.margin_pct is not None:
            sets.append("margin_pct=?"); params.append(max(0.0, min(100.0, float(req.margin_pct))))
        if req.status is not None:
            if req.status not in _VALID_STATUSES:
                raise HTTPException(status_code=400, detail=f"status must be one of {_VALID_STATUSES}")
            sets.append("status=?"); params.append(req.status)
        if sets:
            params.extend([order_id, ctx])
            con.execute(f"UPDATE pp_industry_orders SET {', '.join(sets)} WHERE id=? AND context_id=?", params)
            con.commit()
            # The customer's link is cached; an edit to the quote has to reach it now, not in a minute.
            from app.industry.shares import invalidate_order_shares
            invalidate_order_shares(order_id)
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


class QueuePlanRequest(BuildOptions):
    """BuildOptions (how to cost/schedule) plus the queue-only slot overrides. The targets aren't
    in the request — they're whatever the account currently has queued.

    Note `use_stock` is inherited: on for planning ("what's left to do"), but the progress endpoint
    turns it OFF so it measures against the FULL requirement — otherwise the denominator would
    shrink as you acquire stock and the bar could never fill."""
    mfg_slots: int | None = None
    rx_slots: int | None = None


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
            "SELECT product_type_id, quantity, force_build_ids, me_te_overrides FROM pp_industry_orders "
            "WHERE context_id=? AND status='queued' ORDER BY priority DESC, id", (ctx,),
        ).fetchall()
        if not orders:
            return {"targets": [], "empty": True}
        # Overrides ride on the order that carried them, but the queue is planned as ONE batch —
        # a component is built once for everybody — so they can only be applied as a union. In
        # practice that's what the user meant: they asked for that component to be built.
        forced = {t for o in orders for t in _parse_ids(o["force_build_ids"])}
        # Same union logic, and for the same reason: one shared batch per component means one ME/TE
        # per component. A later order's explicit choice wins over an earlier one's.
        me_te: dict = {}
        for o in orders:
            me_te.update(_parse_map(o["me_te_overrides"]))
        # Combine duplicate products into one target quantity — insertion order is preserved, so the
        # first time a product appears fixes its place in line.
        combined: dict[int, int] = {}
        for o in orders:
            combined[o["product_type_id"]] = combined.get(o["product_type_id"], 0) + o["quantity"]
        targets = list(combined.items())
    finally:
        con.close()

    if forced:
        req = req.model_copy(update={"force_build_ids": sorted(set(req.force_build_ids) | forced)})
    if me_te:
        req = req.model_copy(update={"me_te_overrides": {**me_te, **(req.me_te_overrides or {})}})
    inp = prepare_plan_inputs(
        ctx, targets, req, mfg_slots=req.mfg_slots, rx_slots=req.rx_slots,
        missing_recipe_detail=lambda tid: f"queued order {tid} has no recipe")
    on_hand = _stock_for(ctx, targets) if req.use_stock else None
    res = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names,
                     inp.pools, on_hand=on_hand)
    # The recipe tree per ordered product. plan_queue returns aggregated demand — correct for cost
    # and scheduling, but it has no structure, and the UI derives its build STAGES from the tree.
    # Without this the status view (the main screen) had no pipeline at all and lumped every job
    # into one unlabelled bucket, while the preview modal showed the real stages.
    res["trees"] = [build_plan(t, q, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                               inp.names)["tree"]
                    for t, q in targets]
    # Who installs what, across the whole schedule. to-install still answers "right now" off the
    # FREE slots; this answers "and then who does the rest", which every later stage lacked.
    from app.industry.schedule import assign_characters
    from app.industry.slots import _slot_pool
    assign_characters(res["schedule"]["waves"], _slot_pool(ctx).get("characters") or [])
    return res


@router.post("/api/industry/queue-plan")
def queue_plan(req: QueuePlanRequest, ctx: int = Depends(require_context)):
    """Aggregate demand across ALL of the account's queued build orders and schedule them together
    against the account's slot pool — the honest, shared-batch plan for the whole queue.

    Carries the start-now checklist inline (`install`), because it is a view OF THIS PLAN: the page
    was fetching it separately, which planned the entire queue a second time to answer a question the
    plan it already had could answer.
    """
    res = _run_queue_plan(ctx, req)
    if not res.get("empty"):
        res["install"] = install_block(ctx, res)
    return res


@router.post("/api/industry/to-install")
def to_install(req: QueuePlanRequest | None = None, ctx: int = Depends(require_context)):
    """What to start RIGHT NOW: the ready wave of the queue plan (jobs whose inputs are all
    available), your free slot counts, and how many of the ready jobs fit those free slots. The
    actionable, least-effort answer to 'what should I be doing'.

    TAKES THE SAME BUILD OPTIONS as the queue plan, and must: this used to plan with defaults while
    the screen beside it planned with the user's real settings (facility, threshold, speed, ME/TE
    overrides). The two then disagreed about what is even ready — the checklist would say "start the
    Revelation" off a plan that bought every component, while the plan on screen showed two earlier
    stages of component jobs that nothing was telling you to start.
    """
    res = _run_queue_plan(ctx, req or QueuePlanRequest())
    if res.get("empty"):
        return {"empty": True}
    return install_block(ctx, res)


def install_block(ctx: int, res: dict) -> dict:
    """The checklist, derived from an ALREADY-PLANNED queue.

    Split out so the plan endpoint can return it inline: the page used to POST the plan and then POST
    to-install, which planned the whole queue a second time — the single most expensive thing the
    manufacturing page did, for an answer it had just been given.
    """
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
