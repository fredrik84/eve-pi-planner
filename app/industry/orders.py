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
from app.industry.schedule import plan_queue, plan_queue_per_order

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
            # "This build makes its own reactions" — the per-ORDER exception to the account's
            # standing reaction policy, stored beside force_build_ids because it is the same kind of
            # instruction one rung coarser. 0 = follow the account policy, which is what every order
            # written before this column existed keeps doing.
            "build_reactions INTEGER DEFAULT 0",
            # The margin this order was quoted at. Snapshotted rather than read live: a customer
            # holding a price shouldn't see it move because the builder changed their default.
            "margin_pct REAL",
            # The stock source this build pulls from — the corp hangar or container the materials
            # are being gathered into. One box per build is how players actually keep two
            # simultaneous builds apart, so the sourcing checklist reads that box rather than
            # asking them to tick materials off by hand.
            "source_key TEXT DEFAULT ''",
            # The FULL bound set, a JSON array of source keys. Reaction stock and manufacturing
            # stock routinely sit in different stations, so one build's materials legitimately live
            # in several boxes. Additive by design: `source_key` keeps the first of them, so an
            # order written before this column existed — and any reader that only knows about the
            # single key — still works untouched.
            "source_keys TEXT DEFAULT ''",
            # Has the user curated THIS order's sources? 1 = the set above is the whole answer for
            # this plan: it is what the checklist measures and the only stock this plan may count.
            # 0 = the order predates per-plan sources and keeps drawing on the account-wide tick
            # list, exactly as it did before. The column is what makes the change non-retroactive:
            # an in-flight build cannot silently lose sight of a can it was already counting.
            "sources_owned INTEGER DEFAULT 0",
            # Where this build's OUTPUT belongs. A job in EVE delivers to exactly one container, so
            # a batch shared between two builds has nowhere to go — which is the whole reason
            # `industry_per_order_plans` exists, and the half that was missing until 2026-08-14.
            #
            # **It is a property of the PLAN, not of a job** (user's ruling, 2026-08-14). One box
            # chosen once, inherited by every job in the order — nothing is configured per job, and
            # the shared-batch problem disappears because a batch belongs to one plan by
            # construction. Jobs already carry `order_id`, so nothing needs a column of its own.
            #
            # Blank means "not stated", which is NOT an error: output then lands wherever the job is
            # installed, exactly as it always did. Corp hangars need the Director role and not every
            # builder has one, so this can never be required.
            "output_source_key TEXT DEFAULT ''",
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
    build_reactions: bool = False     # build this order's reactions whatever the account policy says
    margin_pct: float | None = None             # quote margin; None = the account's current default
    # The container/hangar this build pulls from, chosen while planning it. Set here rather than
    # only afterwards because "which box is this build's" is decided when the build is decided.
    source_key: str = ""
    # …and the rest of them, when a build's materials are spread over more than one box. Both fields
    # are accepted so an older client (or a caller that only ever binds one) needs no change at all;
    # `source_keys` wins when both are sent, since it is the more complete statement.
    source_keys: list[str] = []
    # Where the OUTPUT goes. Chosen while planning for the same reason the input boxes are: "which
    # box is this build's" is decided when the build is decided. '' = not stated, which is allowed.
    output_source_key: str = ""


class OrderUpdate(BaseModel):
    quantity: int | None = None
    force_build_ids: list[int] | None = None
    me_te_overrides: dict[str, list[int]] | None = None
    build_reactions: bool | None = None
    margin_pct: float | None = None
    mode: str | None = None
    label: str | None = None
    priority: int | None = None
    status: str | None = None
    source_key: str | None = None    # '' unbinds the order from its container
    source_keys: list[str] | None = None   # the whole bound set; [] unbinds every box
    output_source_key: str | None = None   # '' clears it; output then lands where the job is installed


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


def order_source_keys(row) -> list[str]:
    """The boxes an order pulls from, in bind order.

    `source_keys` is the truth once it has been written; an order queued before it existed has only
    `source_key`, and reads as the one-element set it always meant. Both are kept in step on write
    (see `_normalise_source_keys`) so neither column can be the stale one.
    """
    d = dict(row) if not isinstance(row, dict) else row
    try:
        keys = [str(k) for k in json.loads(d.get("source_keys") or "[]") if str(k).strip()]
    except Exception:
        keys = []
    if not keys and (d.get("source_key") or "").strip():
        keys = [d["source_key"].strip()]
    return list(dict.fromkeys(keys))


def _normalise_source_keys(keys) -> list[str]:
    """De-duplicated, order-preserving, length-capped — the same cap the single column has always
    applied, since each element still has to fit it."""
    return list(dict.fromkeys((str(k) or "").strip()[:80] for k in (keys or []) if str(k).strip()))


def order_output_source_key(row) -> tuple[str, str]:
    """Where this order's OUTPUT belongs: `(key, basis)`.

    `basis` is `stated` / `inherited` / `none`, and it is meant to be SHOWN — the same contract as
    the job-fee and time-efficiency sources. A box the app picked has to say it picked it.

    **The plan owns this, not the job** (user's ruling, 2026-08-14). A job delivers to exactly one
    container, so making it a per-job setting is what made the shared-batch case unanswerable; one
    box per plan, inherited by every job in it, has no such case.

    Falls back to the FIRST bound input box, because the same container is normally both: a builder
    gathers a build into a box and wants the output back in it — that is how they track what they
    have acquired. `none` is a legitimate answer, not a failure: output then lands wherever the job
    is installed, which is what happened before this existed, and corp hangars need the Director
    role so a box can never be required.
    """
    d = dict(row) if not isinstance(row, dict) else row
    stated = (d.get("output_source_key") or "").strip()
    if stated:
        return stated, "stated"
    inputs = order_source_keys(d)
    if inputs:
        return inputs[0], "inherited"
    return "", "none"


def _order_dict(con, row) -> dict:
    """Row → API shape: the overrides come back as {type_id, name} so the UI can show WHICH
    components were overridden without a second lookup per order."""
    d = dict(row)
    d["source_keys"] = order_source_keys(d)
    out_key, out_basis = order_output_source_key(d)
    d["output_source_key"] = out_key
    d["output_source_basis"] = out_basis
    try:
        from app.industry.assets import source_name
        d["output_source_name"] = source_name(d.get("context_id"), out_key) if out_key else None
    except Exception:
        d["output_source_name"] = None
    ids = _parse_ids(d.get("force_build_ids"))
    d["force_build_ids"] = ids
    d["me_te_overrides"] = _parse_map(d.get("me_te_overrides"))
    d["build_reactions"] = bool(d.get("build_reactions"))
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
        # Sending `source_keys` at all is the statement "this plan's sources are mine to decide" —
        # a client that only knows the old single field keeps the old account-wide behaviour. An
        # EMPTY set is not that statement: picking no box says nothing about where this build's
        # materials come from, so it falls back to the account pool rather than to no stock at all.
        sent = "source_keys" in req.model_fields_set
        keys = _normalise_source_keys(req.source_keys if sent
                                      else ([req.source_key] if req.source_key else []))
        owned = sent and bool(keys)
        oid = con.execute(
            "INSERT INTO pp_industry_orders (context_id, product_type_id, name, quantity, mode, "
            "priority, status, created_at, label, force_build_ids, me_te_overrides, margin_pct, "
            "build_reactions, source_key, source_keys, sources_owned, output_source_key) "
            "VALUES (?,?,?,?,?,?, 'queued', ?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (ctx, req.product_type_id, name or str(req.product_type_id), req.quantity, req.mode,
             int(_time.time()), _time.time(), (req.label or "").strip()[:60],
             json.dumps(sorted({int(t) for t in req.force_build_ids})),
             json.dumps(req.me_te_overrides or {}), req.margin_pct,
             1 if req.build_reactions else 0,
             keys[0] if keys else "", json.dumps(keys), 1 if owned else 0,
             (req.output_source_key or "").strip()),
        ).fetchone()[0]
        con.commit()
        from app.industry.sourcing import remember_source_default, enable_bound_sources
        if owned:
            # The plan owns its sources: they count for THIS build and no other. Nothing global is
            # switched on, so hauling into this order's can does not quietly hand its contents to
            # every other build — sharing a box is putting it in both sets, deliberately.
            remember_source_default(ctx, keys)
        else:
            enable_bound_sources(ctx, keys)
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
        if req.build_reactions is not None:
            sets.append("build_reactions=?")
            params.append(1 if req.build_reactions else 0)
        if req.margin_pct is not None:
            sets.append("margin_pct=?"); params.append(max(0.0, min(100.0, float(req.margin_pct))))
        # One binding, two columns kept in lockstep: whichever field the caller sent decides the
        # whole set, and `source_key` always ends up as its first element. Letting them be written
        # independently is how the two would come to disagree about which box a build pulls from.
        bound, sent = None, False
        if req.source_keys is not None:
            bound, sent = _normalise_source_keys(req.source_keys), True
        elif req.source_key is not None:
            bound = _normalise_source_keys([req.source_key] if req.source_key.strip() else [])
        owned = sent and bool(bound)
        if bound is not None:
            sets.append("source_key=?"); params.append(bound[0] if bound else "")
            sets.append("source_keys=?"); params.append(json.dumps(bound))
            if sent:
                # Editing the set through the per-plan picker is the user taking ownership of this
                # order's stock — the point at which it stops drawing on the account-wide pool.
                # Clearing every box hands it back, rather than leaving a plan that owns nothing and
                # can therefore count nothing.
                sets.append("sources_owned=?"); params.append(1 if bound else 0)
        # SEPARATE from the block above, and it must stay separate: the output box is its own
        # binding, so a PATCH that sets only it must not touch `source_keys`, and a PATCH that sets
        # only the sources must not clear it. Nesting these two under one `if` shipped on
        # 2026-08-14 and silently dropped every multi-box bind that did not also send an output box.
        if req.output_source_key is not None:
            # '' is a real instruction here (clear it), not "unset", so this is an explicit
            # `is not None` rather than a truthiness test.
            sets.append("output_source_key=?"); params.append(req.output_source_key.strip())
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
        if bound:
            from app.industry.sourcing import enable_bound_sources, remember_source_default
            (remember_source_default if owned else enable_bound_sources)(ctx, bound)
        result = _order_row(con, order_id, ctx)
    finally:
        con.close()
    if req.status in ("done", "cancelled"):
        from app.reactions.orders import finish_manufacturing_reaction_orders
        result["reaction_lifecycle"] = finish_manufacturing_reaction_orders(ctx, order_id, req.status)
    return result


@router.delete("/api/industry/orders/{order_id}")
def delete_order(order_id: int, ctx: int = Depends(require_context)):
    ensure_industry_orders_table()
    con = get_connection()
    try:
        _order_row(con, order_id, ctx)
    finally:
        con.close()
    from app.reactions.orders import finish_manufacturing_reaction_orders
    reaction_lifecycle = finish_manufacturing_reaction_orders(ctx, order_id, "cancelled")
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_orders WHERE id=? AND context_id=?", (order_id, ctx))
        con.commit()
        # The sourcing notes are about THIS order's materials and mean nothing without it; ids are
        # reused by the sequence eventually, so leaving them would attach one build's notes to
        # another's.
        from app.industry.sourcing import clear_order_sourcing
        clear_order_sourcing(ctx, order_id)
        return {"deleted": order_id, "reaction_lifecycle": reaction_lifecycle}
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
    # Plan each order on its own rather than aggregating the queue. None = whatever the account has
    # saved (the normal path); True/False is how the compare endpoint asks for one specific answer
    # off the same inputs, and how a caller that must not be re-costed pins the old behaviour.
    per_order_plans: bool | None = None


def plan_source_keys(ctx: int, orders) -> list[str] | None:
    """Which boxes THIS plan may spend, or None for "the account-wide tick list" (the old pool).

    A plan owns its own sources: an order whose set the user has curated (`sources_owned`) counts
    that set and nothing else, so hauling a can into build A does not quietly hand its contents to
    build B. Sharing a container between two builds is then something the user does on purpose, by
    putting it in both sets.

    Two rules keep that from being retroactive, which matters more here than usual:
    * **An uncurated order still draws on the account pool.** Every order queued before this
      existed is uncurated, so nothing about an in-flight build changes until its owner edits it.
    * **A mixed queue is the UNION of both.** The queue is planned as one aggregated batch, so the
      pool an uncurated order is entitled to is available to that batch — narrowing it because a
      *different* order was curated would make the planner buy materials the user has in hand,
      which is the expensive direction to be wrong in.
    Counting is by key set, so a container in two orders' sets is still only spendable once.
    """
    rows = [dict(o) for o in (orders or [])]
    # An order that claims ownership but names no box owns nothing to spend — treat it as having no
    # opinion, or it would silently deny the whole queue the account pool.
    curated = [o for o in rows if int(o.get("sources_owned") or 0) and order_source_keys(o)]
    if not curated:
        return None
    keys = [k for o in curated for k in order_source_keys(o)]
    if len(curated) != len(rows):
        from app.industry.assets import enabled_source_keys
        keys += enabled_source_keys(ctx)
    return list(dict.fromkeys(keys))


# What the builder has typed into the sourcing panel counts as stock the plan may spend.
SOURCED_COUNTS_FEATURE = "industry_sourced_counts"


def _stock_for(ctx: int, targets, orders=None) -> dict[int, float]:
    """Owned quantities to net off the demand, EXCLUDING the products being ordered. Owning a
    Revelation must not make an order to build one plan zero jobs — you asked to build it. Only
    intermediates and materials count as stock. Empty when assets were never fetched."""
    from app.industry.assets import owned_quantities, source_quantities_multi
    keys = plan_source_keys(ctx, orders)
    stock = owned_quantities(ctx) if keys is None else source_quantities_multi(ctx, keys)
    _add_noted_stock(ctx, stock, orders)
    for tid, _ in targets:
        stock.pop(tid, None)
    return stock


def _add_noted_stock(ctx: int, stock: dict, orders) -> None:
    """Fold in what the builder has marked as already sourced, in place.

    Gated: it changes what a plan buys, which is the number everything else on the page derives
    from. With the flag off the pool is exactly what it was.
    """
    from app.features import feature_enabled_for
    if not feature_enabled_for(SOURCED_COUNTS_FEATURE, ctx):
        return
    from app.industry.sourcing import noted_stock_excess
    for tid, qty in noted_stock_excess(ctx, orders).items():
        stock[tid] = stock.get(tid, 0.0) + qty


def _order_stock(ctx: int, targets, order_rows) -> dict[int, list]:
    """{order_id: {type_id: qty}} — what each order may spend, planned apart.

    The aggregated plan can only ask one question ("what may this QUEUE spend"), which is why
    `plan_source_keys` unions a curated order's boxes with the account pool. Planned apart the
    honest answer is per order: a curated order spends ITS boxes and nothing else — that is what
    curating them meant — and an uncurated one still draws on the account-wide tick list. The
    queue-wide remainder in `plan_queue_per_order` then stops two orders spending the same item.
    """
    from app.industry.assets import owned_quantities, source_quantities_multi
    pool = None
    out: dict[int, dict] = {}
    for o in order_rows:
        keys = order_source_keys(o) if int(o.get("sources_owned") or 0) else []
        if keys:
            stock = source_quantities_multi(ctx, keys)
        else:
            if pool is None:
                pool = owned_quantities(ctx)
            stock = dict(pool)
        # Planned apart, an order counts its OWN notes and nobody else's — the same reasoning that
        # gives a curated order its own boxes and nothing else.
        _add_noted_stock(ctx, stock, [o])
        for tid, _ in targets:
            stock.pop(tid, None)
        out[o["id"]] = stock
    return out


def _mark_already_held(ctx: int, res: dict, on_hand) -> None:
    """Say, per shopping-list row, how much of it the builder already holds.

    The gap this closes: `aggregate_demand` nets stock off BUILT types only (`schedule.py`, the
    `for tid in built` loop) — a component you hold is not rebuilt. Nothing has ever netted it off
    BOUGHT ones, so a raw material sitting in the container bound to the order, or ticked off in
    the sourcing panel, was still listed as something to go and buy. That is the tool arguing with
    something the user told it.

    **Quantities change, money does not.** `to_buy` is what is left to purchase; `qty` and
    `line_cost` stay the full requirement, because the material is still consumed by the build and
    a quote that silently dropped every time the builder happened to have stock would understate
    what the job actually costs to run. This is the same split the sourcing panel already makes —
    it reports a shortfall and its cost, and refuses to price anything else, precisely so two
    lists cannot show two different numbers for one item.
    """
    from app.features import feature_enabled_for
    if not feature_enabled_for(SOURCED_COUNTS_FEATURE, ctx):
        return
    pool = dict(on_hand or {})
    if not pool:
        return
    for row in (res.get("shopping_list") or []):
        have = float(pool.get(int(row["type_id"]), 0.0))
        if have <= 0:
            continue
        qty = float(row.get("qty") or 0.0)
        row["have"] = min(have, qty)
        row["to_buy"] = max(0.0, qty - have)


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


def _reaction_owner_weights(trees: list[dict], order_rows: list[dict]) -> dict[int, dict[int, float]]:
    """Reaction type -> Manufacturing order -> demand weight, without unsharing the batch."""
    by_product: dict[int, list[dict]] = {}
    for order in order_rows:
        by_product.setdefault(int(order["product_type_id"]), []).append(order)
    out: dict[int, dict[int, float]] = {}

    def walk(node: dict, found: dict[int, float]) -> None:
        if node.get("decision") == "build" and node.get("activity") == "reaction":
            tid = int(node["type_id"])
            found[tid] = found.get(tid, 0.0) + float(node.get("runs") or 0)
        for child in node.get("inputs") or []:
            walk(child, found)

    for tree in trees:
        owners = by_product.get(int(tree.get("type_id") or 0)) or []
        total_qty = sum(max(0, int(o["quantity"])) for o in owners)
        if not owners or total_qty <= 0:
            continue
        found: dict[int, float] = {}
        walk(tree, found)
        for tid, runs in found.items():
            weights = out.setdefault(tid, {})
            for order in owners:
                oid = int(order["id"])
                weights[oid] = weights.get(oid, 0.0) + runs * int(order["quantity"]) / total_qty
    return out


def _allocate_owner_runs(total_runs: int, weights: dict[int, float]) -> list[dict]:
    """Largest-remainder attribution: shares add up exactly to the one physical batch."""
    if total_runs <= 0 or not weights:
        return []
    scale = sum(max(0.0, w) for w in weights.values())
    if scale <= 0:
        return []
    exact = {oid: total_runs * max(0.0, w) / scale for oid, w in weights.items()}
    shares = {oid: int(v) for oid, v in exact.items()}
    left = total_runs - sum(shares.values())
    for oid in sorted(exact, key=lambda i: (-(exact[i] - shares[i]), i))[:left]:
        shares[oid] += 1
    return [{"order_id": oid, "runs": runs} for oid, runs in sorted(shares.items()) if runs > 0]


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
            "SELECT id, product_type_id, quantity, force_build_ids, me_te_overrides, margin_pct, "
            "COALESCE(build_reactions,0) AS build_reactions, "
            "COALESCE(source_key,'') AS source_key, COALESCE(source_keys,'') AS source_keys, "
            "COALESCE(output_source_key,'') AS output_source_key, "
            "COALESCE(sources_owned,0) AS sources_owned FROM pp_industry_orders "
            "WHERE context_id=? AND status='queued' ORDER BY priority DESC, id", (ctx,),
        ).fetchall()
        if not orders:
            return {"targets": [], "empty": True}
        # Overrides ride on the order that carried them, but the queue is planned as ONE batch —
        # a component is built once for everybody — so they can only be applied as a union. In
        # practice that's what the user meant: they asked for that component to be built.
        forced = {t for o in orders for t in _parse_ids(o["force_build_ids"])}
        # Unioned for exactly the same reason: the queue builds ONE shared batch per component, so
        # if any order in it makes its own reactions, that batch is reacted.
        reacts = any(o["build_reactions"] for o in orders)
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
        # Materialised before the connection closes — the stock resolution below needs each order's
        # own source set, and a plan is no longer entitled to whatever the account happens to have
        # ticked (see plan_source_keys).
        order_rows = [dict(o) for o in orders]
    finally:
        con.close()

    # Does this queue get planned order by order, or as one shared batch? The request wins when it
    # says (that is how the compare endpoint gets both answers off one set of inputs); otherwise the
    # account's saved choice, and only where the feature is actually rolled out to this caller.
    from app.features import feature_enabled_for
    from app.industry.settings import get_per_order_plans
    per_order = (get_per_order_plans(ctx) and feature_enabled_for("industry_per_order_plans", ctx)
                 if req.per_order_plans is None else bool(req.per_order_plans))

    # The unions below exist ONLY because the aggregated plan builds one shared batch per component:
    # a component built once for everybody can only be built one way. Planned apart, every order
    # carries its own — so unioning them here would hand one customer's "build it anyway" to
    # everyone else's build, which is the exact conflation this path removes.
    if not per_order:
        if forced:
            req = req.model_copy(update={"force_build_ids": sorted(set(req.force_build_ids) | forced)})
        if me_te:
            req = req.model_copy(update={"me_te_overrides": {**me_te, **(req.me_te_overrides or {})}})
        if reacts:
            req = req.model_copy(update={"build_reactions_anyway": True})
    inp = prepare_plan_inputs(
        ctx, targets, req, mfg_slots=req.mfg_slots, rx_slots=req.rx_slots,
        missing_recipe_detail=lambda tid: f"queued order {tid} has no recipe")
    if per_order:
        stock = _order_stock(ctx, targets, order_rows) if req.use_stock else {}
        specs = [{"id": o["id"], "type_id": o["product_type_id"], "quantity": o["quantity"],
                  "force_build_ids": _parse_ids(o["force_build_ids"]),
                  "me_te_overrides": _parse_map(o["me_te_overrides"]),
                  "build_reactions": bool(o["build_reactions"]),
                  "margin_pct": o["margin_pct"],
                  "stock": stock.get(o["id"]) if req.use_stock else None}
                 for o in order_rows]
        # The queue-wide ceiling on stock, so first-come-first-served can subtract from something
        # even where every order curated its own boxes.
        on_hand = _stock_for(ctx, targets, order_rows) if req.use_stock else None
        res = plan_queue_per_order(specs, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                                   inp.names, inp.pools, on_hand=on_hand)
    else:
        on_hand = _stock_for(ctx, targets, order_rows) if req.use_stock else None
        res = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names,
                         inp.pools, on_hand=on_hand)
    _mark_already_held(ctx, res, on_hand)
    # Progress measures against the FULL requirement, so it needs the same queue planned with no
    # stock netted off. Computed HERE, off inputs already resolved, rather than in a second request
    # that would repeat every DB read in prepare_plan_inputs — the graph, the names, the blueprints,
    # the contract index, the slot pool — to answer a question this plan is already holding.
    # With no stock enabled the two plans are identical, so that case costs nothing at all.
    if want_full:
        if not on_hand:
            res["_full"] = res
        elif per_order:
            res["_full"] = plan_queue_per_order(
                [{**sp, "stock": None} for sp in specs], inp.mfg, inp.rx, inp.prices, inp.adjusted,
                inp.params, inp.names, inp.pools, on_hand=None)
        else:
            res["_full"] = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted,
                                      inp.params, inp.names, inp.pools, on_hand=None)
    # The recipe tree per ordered product. plan_queue returns aggregated demand — correct for cost
    # and scheduling, but it has no structure, and the UI derives its build STAGES from the tree.
    # Without this the status view (the main screen) had no pipeline at all and lumped every job
    # into one unlabelled bucket, while the preview modal showed the real stages.
    # Per-order plans build the tree with THAT order's own params, because its forced builds and
    # ME/TE are its own — the tree is what the page draws its stages from, and a tree built off a
    # unioned set would show stages the order isn't running.
    if per_order:
        from app.industry.schedule import _order_params
        res["trees"] = [build_plan(sp["type_id"], sp["quantity"], inp.mfg, inp.rx, inp.prices,
                                   inp.adjusted, _order_params(inp.params, sp), inp.names)["tree"]
                        for sp in specs]
    else:
        res["trees"] = [build_plan(t, q, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                                   inp.names)["tree"]
                        for t, q in targets]
    res["_reaction_owner_weights"] = _reaction_owner_weights(res["trees"], order_rows)
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
    priorities = {int(o["id"]): int(o.get("priority") or 0) for o in order_rows}
    queue_priority = max(priorities.values(), default=0)
    queue_ref = "queue:" + ",".join(str(int(o["id"])) for o in order_rows)
    sole_order_id = int(order_rows[0]["id"]) if len(order_rows) == 1 else 0
    for wave in res["schedule"]["waves"]:
        for task in wave.get("tasks") or []:
            task["order_priority"] = priorities.get(int(task.get("order_id") or 0), queue_priority)
            task["handoff_ref"] = (f"order:{int(task['order_id'])}" if task.get("order_id")
                                   else (f"order:{sole_order_id}" if sole_order_id else queue_ref))
            task["handoff_order_id"] = int(task.get("order_id") or sole_order_id)
    if sk is not None:
        res["skill_gaps"] = sk["gaps"]
        # Kept for `install_block`, which has to rank the same way this did. Private: it is a set
        # of character ids per step, not something to ship to a browser — the endpoints pop it.
        res["_eligibility"] = sk.get("eligibility")
    res["skill_time_basis"] = inp.params.skill_time_basis
    from app.industry.graph import _cost_basis
    res["cost_basis"] = _cost_basis(inp.params)
    # `_blend_margin` exists only because a shared batch has no per-order cost to price off — the
    # per-order plan has one, and has already used it. Running it here would replace a real sum with
    # an apportionment of itself.
    if not per_order:
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
        res["reaction_handoff"] = _handoff_ready_reactions(
            ctx, res["install"], res.get("_reaction_owner_weights") or {})
        # Progress rides along for the same reason the checklist does: it is a view OF THIS PLAN.
        # The page fetching it separately meant planning the whole queue a second time.
        try:
            from app.industry.progress import queue_progress
            res["progress"] = queue_progress(ctx, res=full)
        except Exception:
            res["progress"] = None       # never let the progress overlay take the plan down with it
    # Internal only, and it must not reach a browser: sets of character ids, which don't serialise
    # and are nobody's business but the planner's. Popped after `install_block`, its one consumer.
    res.pop("_eligibility", None)
    res.pop("_reaction_owner_weights", None)
    return res


@router.post("/api/industry/queue-plan/compare")
def queue_plan_compare(req: QueuePlanRequest | None = None, ctx: int = Depends(require_context)):
    """Both plans of the SAME queue, side by side: aggregated versus per order.

    Switching a builder from one to the other changes what every quote costs, so the number has to
    come before the switch rather than after it. Same inputs, same options, same stock — only the
    question differs — and the delta is stated as what planning APART costs, because that is the
    direction the decision runs in (the aggregated plan is the cheap default).

    Deliberately not cached and not folded into the plan endpoint: it is twice the work of a page
    load and is read once, when deciding.
    """
    from app.industry.settings import get_per_order_plans
    base = req or QueuePlanRequest()
    agg = _run_queue_plan(ctx, base.model_copy(update={"per_order_plans": False}))
    if agg.get("empty"):
        return {"empty": True}
    sep = _run_queue_plan(ctx, base.model_copy(update={"per_order_plans": True}))

    def _row(res: dict) -> dict:
        m = res.get("metrics") or {}
        return {"net_cost": m.get("net_cost"), "total_cost": m.get("total_cost"),
                "materials_cost": m.get("materials_cost"), "job_cost": m.get("job_cost"),
                "blueprint_cost": m.get("blueprint_cost"),
                "blueprint_parallel_cost": m.get("blueprint_parallel_cost"),
                "leftover_value": m.get("leftover_value"), "price": m.get("price"),
                "job_count": m.get("job_count"), "build_steps": m.get("build_steps"),
                "makespan_hours": m.get("makespan_hours"),
                "first_delivery_hours": m.get("first_delivery_hours")}

    a, b = _row(agg), _row(sep)
    return {
        "aggregated": a,
        "per_order": b,
        # Per-order costs the aggregated plan cannot produce at all — the thing being bought.
        "orders": sep.get("per_order") or [],
        "delta": {k: (None if a.get(k) is None or b.get(k) is None else round(b[k] - a[k], 2))
                  for k in a},
        "delta_pct": {k: (None if not a.get(k) or b.get(k) is None
                          else round((b[k] / a[k] - 1) * 100.0, 2)) for k in a},
        # Which of the two the account is actually planning with today.
        "enabled": get_per_order_plans(ctx),
    }


def _install_skills_on(ctx: int) -> bool:
    """Gated: it changes who the checklist names, and on an account with partial skill data that
    is a visible change to the instruction people follow every day."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("industry_install_skill_aware", ctx)
    except Exception:
        return False


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
    # ...and it must not name somebody who cannot install the job. This list derived its OWN
    # assignment and ignored the skill-aware one the scheduler had already made, so the main screen
    # could say "start this on X" directly above a plan marking that same job blocked for X.
    # Capacity is still decided here — free slots now, not total slots over the whole schedule,
    # which is the deliberate difference — but the RANKING is the shared one, so the two can only
    # disagree about when a job runs, never about who can run it.
    from app.industry.schedule import skill_tier
    elig = res.get("_eligibility") if _install_skills_on(ctx) else None
    tier = skill_tier(elig)
    for t in ready:
        act = t["activity"]
        cands = [(cid, info) for cid, info in avail.items() if info.get(act, 0) > 0]
        # Best skill tier first, most free slots within it (which spreads the work exactly as
        # before among equals). A lower tier is used only when nothing better has a free slot: an
        # assigned job carrying `skill_ok: False` is more useful than an unassigned one, because it
        # says precisely what is wrong. Same rule as `assign_characters`.
        cands.sort(key=lambda kv: (tier(kv[0], t["type_id"]), kv[1].get(act, 0)), reverse=True)
        if cands:
            cid, info = cands[0]
            info[act] -= 1
            t["fits_now"] = True
            t["character_id"] = cid
            t["character_name"] = info["name"]
            if elig is not None:
                # Recomputed for the character actually named, never carried over from whoever the
                # scheduler picked — a stale ✓ is worse than no mark at all.
                tr = tier(cid, t["type_id"])
                t["skill_ok"] = True if tr == 2 else (None if tr == 1 else False)
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
            "is_placeholder": bool(c.get("is_placeholder")),
            "assigned": assigned_by_char.get(cid, 0),
            "jobs": [{"name": t.get("name"), "type_id": t["type_id"], "runs": t["runs"],
                      "activity": t["activity"], "duration_hours": t["duration_hours"],
                      # Which structure to install it in — carried from the routing, since with
                      # group-specific rigs "install this job" is only half an instruction.
                      "site": t.get("site"),
                      # Why this job is that long — see build_tasks. Carried through so the
                      # checklist can answer "everything else is 5h, why is this 2h32m".
                      "why": t.get("why")}
                     for t in ready if t.get("character_id") == cid],
        })
    chars.sort(key=lambda c: (-c["assigned"], c["character_name"] or ""))

    from app.production import capacity_contract
    mfg_total = pool.get("manufacturing_slots", sum(
        c.get("manufacturing_slots", 0) for c in pool.get("characters", [])))
    reaction_total = pool.get("reaction_slots", sum(
        c.get("reaction_slots", 0) for c in pool.get("characters", [])))
    return {
        "ready": ready,
        "free": free,
        "characters": chars,
        "unassigned": [t for t in ready if not t.get("fits_now")],
        "fit_count": sum(1 for t in ready if t.get("fits_now")),
        "makespan_hours": res["metrics"]["makespan_hours"],
        "later_waves": max(0, len(waves) - 1),
        # Manufacturing schedules future waves but does not claim today's EVE slots merely because
        # a build is queued. The shared shape matches Reactions; the model names the real difference.
        "capacity": capacity_contract(
            reservation_model="scheduled",
            manufacturing=(mfg_total, pool["manufacturing_free"]),
            reaction=(reaction_total, pool["reaction_free"])),
    }


def _handoff_ready_reactions(ctx: int, install: dict,
                             owner_weights: dict[int, dict[int, float]] | None = None) -> dict | None:
    """Give ready reaction batches to Reactions; later Manufacturing waves stay unreserved."""
    from app.features import feature_enabled_for
    if not feature_enabled_for("industry_reaction_handoff", ctx):
        return None
    grouped: dict[tuple[int, int], dict] = {}
    for task in install.get("ready") or []:
        if task.get("activity") != "reaction" or not task.get("fits_now"):
            continue
        key = (str(task.get("handoff_ref") or f"order:{int(task.get('order_id') or 0)}"),
               int(task["type_id"]))
        row = grouped.setdefault(key, {"source_ref": key[0],
                                       "order_id": int(task.get("handoff_order_id")
                                                       or task.get("order_id") or 0),
                                       "type_id": key[1], "runs": 0,
                                       "priority": int(task.get("order_priority") or 0)})
        row["runs"] += int(task.get("runs") or 0)
    owner_ids = {int(order_id) for weights in (owner_weights or {}).values()
                 for order_id in weights}
    owner_ids.update(int(t.get("handoff_order_id") or t.get("order_id") or 0)
                     for t in (install.get("ready") or []))
    from app.reactions.orders import (linked_manufacturing_reaction_orders,
                                      sync_manufacturing_reaction_orders)
    if not grouped:
        linked = linked_manufacturing_reaction_orders(ctx, list(owner_ids))
        return {"created": [], "assigned": [], "shortfalls": [], "conflicts": [],
                "orders": linked} if linked else None
    for row in grouped.values():
        # Manufacturing's scheduler may split a stage into many provisional slot-sized tasks.
        # Those rows answer when work could run; they are not an authoritative quantity ledger.
        # The graph-derived owner demand is. Using the task sum here allowed repeated/split rows to
        # inflate a handful of reaction runs into hundreds of thousands before Reactions saw them.
        weights = (owner_weights or {}).get(row["type_id"], {})
        if weights:
            row["runs"] = max(1, int(round(sum(max(0.0, float(v)) for v in weights.values()))))
        if row["source_ref"].startswith("order:") and row["order_id"]:
            row["owners"] = [{"order_id": row["order_id"], "runs": row["runs"]}]
        else:
            row["owners"] = _allocate_owner_runs(
                row["runs"], weights)
        if row["owners"]:
            ids = ",".join(str(o["order_id"]) for o in row["owners"])
            row["source_ref"] = f"shared:{row['type_id']}:{ids}"
            row["order_id"] = row["owners"][0]["order_id"] if len(row["owners"]) == 1 else 0
    return sync_manufacturing_reaction_orders(ctx, list(grouped.values()))


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
            "SELECT id, product_type_id, quantity, force_build_ids, me_te_overrides, "
            "COALESCE(build_reactions,0) AS build_reactions, "
            "COALESCE(source_key,'') AS source_key, COALESCE(source_keys,'') AS source_keys, "
            "COALESCE(output_source_key,'') AS output_source_key, "
            "COALESCE(sources_owned,0) AS sources_owned "
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
                                 "me_te_overrides": {**me_te, **(req.me_te_overrides or {})},
                                 "build_reactions_anyway": any(o["build_reactions"] for o in orders)})
    inp = prepare_plan_inputs(ctx, targets, req, mfg_slots=req.mfg_slots, rx_slots=req.rx_slots,
                              missing_recipe_detail=lambda tid: f"queued order {tid} has no recipe")
    on_hand = _stock_for(ctx, targets, [dict(o) for o in orders]) if req.use_stock else None

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
                "window_h": why.get("hard_h"),
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
