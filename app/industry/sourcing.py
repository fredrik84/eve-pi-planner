"""Material sourcing for one build order — what's already gathered, and what's still missing.

The shopping list answers "what does this build need". It does not answer the question a builder
actually lives with over the following days: *which of it have I already got, and where is it*.
Players solve that in-game by dedicating a corp hangar or a container to each build and hauling
materials into it as they buy them, so the box IS the record. This module reads that record.

Two signals, combined the same way progress combines its own:

* **The bound source.** An order can name one stock source (`pp_industry_orders.source_key`) — the
  container this build pulls from. Whatever is in it counts as sourced, no ticking required. Refresh
  the assets and the checklist moves on its own, which is the version of this feature that costs the
  user nothing to keep up to date.
* **A hand-entered quantity.** For everything the first signal can't see — stock sitting in a
  station you haven't scanned, a courier contract in flight, a corp-mate's promise — the user can
  say how much is accounted for. Stored per (order, material).

The higher of the two wins per material, so ticking something off never hides real contents and a
scan never erases a note.

**Requirement is per ORDER, not the shared queue batch.** The queue deliberately aggregates demand
across orders (one batch of an intermediate serves everybody), which is right for cost and
scheduling and useless here: you cannot haul 40% of a shared batch into one customer's box. So this
plans the order on its own — its own quantity, its own overrides — and the sum across orders can
legitimately exceed what the queue will actually build.
"""

from __future__ import annotations

import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.sde import ensure_once
from app.esi import require_context
from app.industry._router import router


@ensure_once
def ensure_sourcing_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_sourced (
                context_id INTEGER NOT NULL,
                order_id   INTEGER NOT NULL,
                type_id    INTEGER NOT NULL,
                qty        REAL    NOT NULL DEFAULT 0,
                updated_at REAL    NOT NULL,
                PRIMARY KEY (context_id, order_id, type_id)
            )
        """)
        con.commit()
    finally:
        con.close()


def _manual(context_id: int, order_id: int) -> dict[int, float]:
    ensure_sourcing_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT type_id, qty FROM pp_industry_sourced WHERE context_id=? AND order_id=?",
            (context_id, order_id),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(r["type_id"]): float(r["qty"] or 0) for r in rows}


def set_sourced(context_id: int, order_id: int, type_id: int, qty: float) -> None:
    """Record how much of a material is accounted for. `qty <= 0` clears the note entirely rather
    than storing a zero — an explicit "none" and never having said anything mean the same thing."""
    ensure_sourcing_table()
    con = get_connection()
    try:
        if qty <= 0:
            con.execute("DELETE FROM pp_industry_sourced WHERE context_id=? AND order_id=? AND type_id=?",
                        (context_id, order_id, type_id))
        else:
            con.execute(
                "INSERT INTO pp_industry_sourced (context_id, order_id, type_id, qty, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(context_id, order_id, type_id) DO UPDATE SET "
                "qty=excluded.qty, updated_at=excluded.updated_at",
                (context_id, order_id, type_id, float(qty), time.time()))
        con.commit()
    finally:
        con.close()


def clear_order_sourcing(context_id: int, order_id: int) -> None:
    ensure_sourcing_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_sourced WHERE context_id=? AND order_id=?",
                    (context_id, order_id))
        con.commit()
    finally:
        con.close()


def _order_requirement(ctx: int, order) -> list[dict]:
    """The materials THIS order needs bought, priced — its own standalone shopping list.

    Planned with the order's own overrides (`force_build_ids`, `me_te_overrides`) so the list is the
    one the builder will really work from: a component the order forces built appears as its inputs,
    not as itself.
    """
    from app.industry.graph import build_plan, prepare_plan_inputs
    from app.industry.orders import QueuePlanRequest, _parse_ids, _parse_map

    tid, qty = int(order["product_type_id"]), int(order["quantity"])
    req = QueuePlanRequest(
        use_stock=False,                       # the FULL requirement — stock is what we measure
        force_build_ids=_parse_ids(order["force_build_ids"]),
        me_te_overrides=_parse_map(order["me_te_overrides"]),
    )
    inp = prepare_plan_inputs(ctx, [(tid, qty)], req,
                             missing_recipe_detail=lambda t: f"order {t} has no recipe")
    res = build_plan(tid, qty, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names)
    return res.get("shopping_list") or []


def order_sourcing(ctx: int, order_id: int) -> dict:
    """Per-material sourcing state for one order."""
    from app.industry.assets import list_sources, source_name, source_quantities
    from app.industry.orders import ensure_industry_orders_table

    ensure_industry_orders_table()
    con = get_connection()
    try:
        order = con.execute(
            "SELECT id, product_type_id, name, quantity, label, force_build_ids, me_te_overrides, "
            "COALESCE(source_key,'') AS source_key FROM pp_industry_orders WHERE id=? AND context_id=?",
            (order_id, ctx),
        ).fetchone()
    finally:
        con.close()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")

    key = order["source_key"] or ""
    in_source = source_quantities(ctx, key) if key else {}
    manual = _manual(ctx, order_id)

    items = []
    for s in _order_requirement(ctx, order):
        tid = int(s["type_id"])
        need = float(s["qty"])
        held = float(in_source.get(tid, 0.0))
        noted = float(manual.get(tid, 0.0))
        have = min(need, max(held, noted))
        items.append({
            "type_id": tid, "name": s["name"], "required": need,
            "in_source": held, "noted": noted, "sourced": have,
            "remaining": max(0.0, need - have),
            "done": have >= need - 1e-9,
            "unit_price": s.get("unit_price"),
            # What the shortfall costs to buy — the number that decides whether to go shopping now.
            "remaining_cost": (s["unit_price"] * max(0.0, need - have)
                               if s.get("unit_price") is not None else None),
        })
    items.sort(key=lambda r: (r["done"], -(r["remaining_cost"] or 0.0), r["name"]))

    done = sum(1 for i in items if i["done"])
    return {
        "order_id": order_id, "name": order["name"], "quantity": order["quantity"],
        "label": order["label"] or "",
        "source_key": key, "source_name": source_name(ctx, key) if key else None,
        # Offered so the picker can be built without a second call. Containers first: a build is
        # normally pulled from a box, not from a whole hangar.
        "sources": sorted(list_sources(ctx), key=lambda s: (s["kind"] != "container", s["name"])),
        "items": items,
        "totals": {
            "materials": len(items), "sourced": done, "missing": len(items) - done,
            "remaining_cost": sum(i["remaining_cost"] or 0.0 for i in items),
            "pct": round(100.0 * done / len(items), 1) if items else 0.0,
        },
    }


@router.get("/api/industry/orders/{order_id}/sourcing")
def industry_order_sourcing(order_id: int, ctx: int = Depends(require_context)):
    return order_sourcing(ctx, order_id)


class SourcedEdit(BaseModel):
    type_id: int
    qty: float | None = None     # None = all of what this order needs; 0 clears the note


@router.post("/api/industry/orders/{order_id}/sourcing")
def industry_order_sourcing_set(order_id: int, req: SourcedEdit,
                                ctx: int = Depends(require_context)):
    """Note how much of one material is accounted for (`qty: null` = all of it), and return the
    refreshed checklist."""
    cur = order_sourcing(ctx, order_id)           # also 404s if the order isn't the caller's
    qty = req.qty
    if qty is None:
        item = next((i for i in cur["items"] if i["type_id"] == req.type_id), None)
        if item is None:
            raise HTTPException(status_code=400, detail="that material isn't in this order")
        qty = item["required"]
    set_sourced(ctx, order_id, int(req.type_id), float(qty))
    return order_sourcing(ctx, order_id)
