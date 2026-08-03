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

from app.sde import get_connection, ensure_once, add_columns
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
        add_columns(
            con, "pp_industry_orders",
            "label TEXT DEFAULT ''",
            # Per-component "build it anyway" overrides decided in the preview. Without this they
            # were a property of the preview only, so queueing silently reverted every one of them.
            "force_build_ids TEXT DEFAULT ''",
            # Per-product ME/TE the user set while planning. Same reasoning: a decision made in the
            # preview that the queue silently dropped is worse than no decision.
            "me_te_overrides TEXT DEFAULT ''",
            # The margin this order was quoted at. Snapshotted rather than read live: a customer
            # holding a price shouldn't see it move because the builder changed their default.
            "margin_pct REAL",
            # The stock source this build pulls from — the corp hangar or container the materials
            # are being gathered into. One box per build is how players actually keep two
            # simultaneous builds apart, so the sourcing checklist reads that box rather than
            # asking them to tick materials off by hand.
            "source_key TEXT DEFAULT ''",
        )
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
    # The container/hangar this build pulls from, chosen while planning it. Set here rather than
    # only afterwards because "which box is this build's" is decided when the build is decided.
    source_key: str = ""


class OrderUpdate(BaseModel):
    quantity: int | None = None
    force_build_ids: list[int] | None = None
    me_te_overrides: dict[str, list[int]] | None = None
    margin_pct: float | None = None
    mode: str | None = None
    label: str | None = None
    priority: int | None = None
    status: str | None = None
    source_key: str | None = None    # '' unbinds the order from its container


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
            "priority, status, created_at, label, force_build_ids, me_te_overrides, margin_pct, "
            "source_key) VALUES (?,?,?,?,?,?, 'queued', ?,?,?,?,?,?) RETURNING id",
            (ctx, req.product_type_id, name or str(req.product_type_id), req.quantity, req.mode,
             int(_time.time()), _time.time(), (req.label or "").strip()[:60],
             json.dumps(sorted({int(t) for t in req.force_build_ids})),
             json.dumps(req.me_te_overrides or {}), req.margin_pct,
             (req.source_key or "").strip()[:80]),
        ).fetchone()[0]
        con.commit()
        # Naming the box this build pulls from also lets the planner count it — see
        # sourcing.enable_bound_source for why those aren't two separate decisions.
        from app.industry.sourcing import enable_bound_source
        enable_bound_source(ctx, (req.source_key or "").strip())
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
        if req.source_key is not None:
            sets.append("source_key=?"); params.append(req.source_key.strip()[:80])
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
        if req.source_key:
            from app.industry.sourcing import enable_bound_source
            enable_bound_source(ctx, req.source_key.strip())
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
        # The sourcing notes are about THIS order's materials and mean nothing without it; ids are
        # reused by the sequence eventually, so leaving them would attach one build's notes to
        # another's.
        from app.industry.sourcing import clear_order_sourcing
        clear_order_sourcing(ctx, order_id)
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


def _blend_margin(res: dict, order_margins: list, default_pct: float) -> None:
    """Re-price the whole-queue plan using each ORDER's own margin.

    `plan_queue` marks the entire queue up at one rate, but margin is snapshotted per order — so
    changing one customer's quote moved nothing on the builder's own sheet while the share link
    they were sent used the new figure. The two disagreeing about the same order is the bug.

    The queue's cost is a shared-batch total with no per-order split (a component is built once for
    everybody), so each order's share is apportioned by its STANDALONE cost — `unit_cost × quantity`
    from the plan's own memoised unit costs. The shared-batch saving is thus spread pro-rata rather
    than invented per order, and the sheet's net-cost tile stays the base every price derives from.
    With one margin across the queue this reduces exactly to the old single-rate formula.
    """
    m = res.get("metrics")
    if not m or not order_margins:
        return
    net = m.get("net_cost")
    if net is None:
        net = m.get("total_cost") or 0.0
    unit_by_type = {t["type_id"]: (t.get("unit_cost") or 0.0) for t in res.get("targets", [])}
    weights = [(unit_by_type.get(tid, 0.0) * qty,
                default_pct if pct is None else float(pct)) for tid, qty, pct in order_margins]
    total_w = sum(w for w, _ in weights)
    if total_w <= 0:                      # no usable cost basis — fall back to an even split
        weights = [(1.0, pct) for _, pct in weights]
        total_w = float(len(weights))
    price = sum((w / total_w) * net * (1 + pct / 100.0) for w, pct in weights)
    rates = {round(pct, 4) for _, pct in weights}
    m["price"] = round(price, 2)
    m["margin_mixed"] = len(rates) > 1
    # The single rate when they agree; otherwise the effective blended rate, so the number shown
    # next to the price always explains the price.
    m["margin_pct"] = round(rates.pop(), 4) if len(rates) == 1 else (
        round((price / net - 1) * 100.0, 2) if net else 0.0)


def _run_queue_plan(ctx: int, req: QueuePlanRequest, want_full: bool = False) -> dict:
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
            "SELECT product_type_id, quantity, force_build_ids, me_te_overrides, margin_pct "
            "FROM pp_industry_orders "
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
        # Margin is snapshotted PER ORDER (a quote a customer holds must not move), so the queue's
        # price can't be one blanket markup — see _blend_margin below.
        order_margins = [(o["product_type_id"], o["quantity"], o["margin_pct"]) for o in orders]
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
    # Progress measures against the FULL requirement, so it needs the same queue planned with no
    # stock netted off. Computed HERE, off inputs already resolved, rather than in a second request
    # that would repeat every DB read in prepare_plan_inputs — the graph, the names, the blueprints,
    # the contract index, the slot pool — to answer a question this plan is already holding.
    # With no stock enabled the two plans are identical, so that case costs nothing at all.
    if want_full:
        res["_full"] = (res if not on_hand else
                        plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                                   inp.names, inp.pools, on_hand=None))
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
    # Same skill-aware assignment as the single-product plan — a queue plan hands out the same
    # jobs, so it would be the one place still able to name someone who can't install them.
    from app.industry.skills import analyze_plan_skills
    sk = analyze_plan_skills(ctx, res.get("requirements") or [], inp.mfg, inp.rx)
    assign_characters(res["schedule"]["waves"], _slot_pool(ctx).get("characters") or [],
                      (sk or {}).get("eligibility"))
    if sk is not None:
        res["skill_gaps"] = sk["gaps"]
    res["skill_time_basis"] = inp.params.skill_time_basis
    from app.industry.graph import _cost_basis
    res["cost_basis"] = _cost_basis(inp.params)
    _blend_margin(res, order_margins, inp.params.margin_pct)
    return res


@router.post("/api/industry/queue-plan")
def queue_plan(req: QueuePlanRequest, ctx: int = Depends(require_context)):
    """Aggregate demand across ALL of the account's queued build orders and schedule them together
    against the account's slot pool — the honest, shared-batch plan for the whole queue.

    Carries the start-now checklist inline (`install`), because it is a view OF THIS PLAN: the page
    was fetching it separately, which planned the entire queue a second time to answer a question the
    plan it already had could answer.
    """
    res = _run_queue_plan(ctx, req, want_full=True)
    full = res.pop("_full", None)
    if not res.get("empty"):
        res["install"] = install_block(ctx, res)
        # Progress rides along for the same reason the checklist does: it is a view OF THIS PLAN.
        # The page fetching it separately meant planning the whole queue a second time.
        try:
            from app.industry.progress import queue_progress
            res["progress"] = queue_progress(ctx, res=full)
        except Exception:
            res["progress"] = None       # never let the progress overlay take the plan down with it
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
                      "activity": t["activity"], "duration_hours": t["duration_hours"],
                      # Why this job is that long — see build_tasks. Carried through so the
                      # checklist can answer "everything else is 5h, why is this 2h32m".
                      "why": t.get("why")}
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


class ForceAboveRequest(QueuePlanRequest):
    """Build every borderline component worth at least `min_saving`, to a fixpoint."""
    min_saving: float = 0.0


# Building a component makes its own inputs a bulk demand, which can make THOSE worth building, and
# so on down the tree. Bounded because forcing only ever grows the set and every round must add at
# least one type; the cap is a safety net against a pathological graph, not an expected exit.
_FORCE_ROUNDS = 8


@router.post("/api/industry/orders/force-above")
def force_build_above(req: ForceAboveRequest, ctx: int = Depends(require_context)):
    """Overrule the buy-it-anyway shortcut for everything saving at least `min_saving`, then keep
    going until nothing new qualifies.

    One press instead of chasing the list. Accepting a batch of these changes the shared batch every
    other decision was weighed against, so a different set comes out borderline afterwards — which,
    a chip at a time, is indistinguishable from the tool inventing new work each time you take its
    advice. Iterating to a fixpoint here means the answer the user gets is stable: after this, there
    is nothing left above their cut-off.

    The rounds share ONE `prepare_plan_inputs`. Only `force_build_ids` changes between them, and the
    expensive half — graphs, prices, names, blueprints, the contract index, the slot pool — does not
    depend on it, so this costs one set of DB reads however many rounds it takes.
    """
    ensure_industry_orders_table()
    cut = max(0.0, float(req.min_saving))
    con = get_connection()
    try:
        orders = con.execute(
            "SELECT id, product_type_id, quantity, force_build_ids, me_te_overrides "
            "FROM pp_industry_orders WHERE context_id=? AND status='queued' "
            "ORDER BY priority DESC, id", (ctx,),
        ).fetchall()
    finally:
        con.close()
    if not orders:
        return {"added": [], "rounds": 0, "empty": True}

    forced = {t for o in orders for t in _parse_ids(o["force_build_ids"])}
    me_te: dict = {}
    for o in orders:
        me_te.update(_parse_map(o["me_te_overrides"]))
    combined: dict[int, int] = {}
    for o in orders:
        combined[o["product_type_id"]] = combined.get(o["product_type_id"], 0) + o["quantity"]
    targets = list(combined.items())

    req = req.model_copy(update={"force_build_ids": sorted(forced),
                                 "me_te_overrides": {**me_te, **(req.me_te_overrides or {})}})
    inp = prepare_plan_inputs(ctx, targets, req, mfg_slots=req.mfg_slots, rx_slots=req.rx_slots,
                              missing_recipe_detail=lambda tid: f"queued order {tid} has no recipe")
    on_hand = _stock_for(ctx, targets) if req.use_stock else None

    added: list[int] = []
    rounds = 0
    for _ in range(_FORCE_ROUNDS):
        rounds += 1
        inp.params.force_build_ids = set(forced)
        res = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names,
                         inp.pools, on_hand=on_hand)
        fresh = {int(s["type_id"]) for s in res.get("shopping_list", [])
                 if s.get("bought_marginal") and (s.get("marginal_saving") or 0) >= cut
                 and int(s["type_id"]) not in forced}
        if not fresh:
            break
        forced |= fresh
        added.extend(sorted(fresh))

    if added:
        # Stored on the first order, like every other force-build: the queue unions them, so one
        # order carries the decision for the whole batch and its ⚒ tag is how it's taken back.
        con = get_connection()
        try:
            con.execute("UPDATE pp_industry_orders SET force_build_ids=? WHERE id=? AND context_id=?",
                        (json.dumps(sorted(forced)), orders[0]["id"], ctx))
            con.commit()
        finally:
            con.close()
        from app.industry.shares import invalidate_order_shares
        invalidate_order_shares(orders[0]["id"])

    return {"added": [{"type_id": t, "name": inp.names.get(t, str(t))} for t in added],
            "rounds": rounds, "cut": cut}


@router.get("/api/industry/queue-plan/packing")
def queue_plan_packing_get(ctx: int = Depends(require_context)):
    """Same diagnostic, openable in a browser tab. A GET plans with the ACCOUNT's saved build
    options (`apply_account_build_options` fills them in), which is what the page uses anyway — and
    it means reading this needs no console, which is where the first attempt at it fell over."""
    return queue_plan_packing(None, ctx)


@router.post("/api/industry/queue-plan/packing")
def queue_plan_packing(req: QueuePlanRequest | None = None, ctx: int = Depends(require_context)):
    """Why every job in the queue is the length it is — one row per type.

    A diagnostic, not a feature. Slot compaction is decided from each type's window, and when the
    result looks wrong from the outside there is no way to tell WHICH of the three bounds bit: the
    type's own uneven split, the plan's pace, or something downstream needing it sooner. This prints
    all of them so the answer is read rather than inferred.
    """
    res = _run_queue_plan(ctx, req or QueuePlanRequest())
    if res.get("empty"):
        return {"empty": True}
    rows: dict[int, dict] = {}
    for w in res["schedule"]["waves"]:
        for t in w["tasks"]:
            why = t.get("why") or {}
            r = rows.setdefault(t["type_id"], {
                "name": t.get("name"), "activity": t["activity"],
                "runs_total": why.get("runs"), "jobs": why.get("jobs"),
                "runs_per_job": why.get("runs_per_job"), "per_run_h": why.get("per_run_h"),
                "job_h": t["duration_hours"],
                "own_h": why.get("own_h"), "pace_h": why.get("pace_h"),
                "consumer_deadline_h": why.get("hard_h"),
                "bound_by": why.get("bound_by"), "needed_by": why.get("needed_by_name"),
            })
            r["job_h"] = max(r["job_h"], t["duration_hours"])
    # What it COULD be if only the pace bound it — the number that says whether a dependency is
    # genuinely in the way or the pace itself is being computed too low.
    for r in rows.values():
        pr = r.get("per_run_h") or 0
        r["could_be_runs_per_job_at_pace"] = (int((r.get("pace_h") or 0) / pr) if pr else None)
    return {"pace_h": max((r.get("pace_h") or 0) for r in rows.values()) if rows else 0,
            "types": sorted(rows.values(), key=lambda r: -(r.get("job_h") or 0))}
