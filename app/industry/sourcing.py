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
* **A pasted inventory.** For everything the first signal can't see — stock in a station you
  haven't scanned, a hangar belonging to someone else, a courier contract already unloaded — the
  user selects the pile in the EVE client and pastes it. A capital build has 50+ distinct
  materials, so anything that asks for one confirmation per material is not a checklist, it's data
  entry; the client's own copy is the fastest true answer available. Stored per (order, material).

The higher of the two wins per material, so ticking something off never hides real contents and a
scan never erases a note.

**Requirement is per ORDER, not the shared queue batch.** The queue deliberately aggregates demand
across orders (one batch of an intermediate serves everybody), which is right for cost and
scheduling and useless here: you cannot haul 40% of a shared batch into one customer's box. So this
plans the order on its own — its own quantity, its own overrides — and the sum across orders can
legitimately exceed what the queue will actually build.
"""

from __future__ import annotations

import json
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


def remember_source_default(context_id: int, keys: list[str]) -> None:
    """Remember this bind as the account's default set, and change nothing else.

    A builder running a can per build answers "which box?" on every single order, and the answer is
    nearly always the one they gave last time — so the next order arrives with it already filled in,
    visible in the picker and one click to change. Remembered as a SET for the same reason it is
    bound as one: someone gathering from a reaction can and a manufacturing can does that on every
    order, not just the one.

    **It deliberately does NOT enable anything.** Under per-plan sources a bound box is stock for
    THAT plan; making it visible to every other plan is a separate decision, and one the user makes
    by putting the box in the other plan's set (or ticking it in Setup).
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, last_source_key, last_source_keys) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "last_source_key=excluded.last_source_key, last_source_keys=excluded.last_source_keys",
            (context_id, keys[0], json.dumps(keys)))
        con.commit()
    except Exception:
        pass                  # a missing default is a lesser problem than a failed binding
    finally:
        con.close()


def enable_bound_sources(context_id: int, keys: list[str]) -> None:
    """The LEGACY bind: remember the default AND switch the boxes on account-wide.

    This is what "binding enables" used to mean everywhere — it made the checklist and the shopping
    list agree, at the cost of making that box stock for every other plan on the account too. That
    side effect is what per-plan sources replaces (`remember_source_default` + the order's own set),
    so this path now only serves a caller that sends the old single `source_key` and has therefore
    made no statement about owning its plan's sources. Unbinding still never disables: the tick is
    the user's now, and taking it back is one click in Setup.
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return
    from app.industry.assets import set_sources
    set_sources(context_id, keys, True)
    remember_source_default(context_id, keys)


def enable_bound_source(context_id: int, key: str) -> None:
    """Single-box form of `enable_bound_sources`, kept because every existing caller says it that
    way."""
    enable_bound_sources(context_id, [key] if key else [])


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


def apply_paste(context_id: int, order_id: int, text: str, wanted: set[int]) -> dict:
    """Replace this order's notes with what an inventory paste says is on hand.

    **Replace, not merge.** A paste is a snapshot of a pile as it stands, so a material that has
    since been consumed — or that was never really there — has to drop back to zero. Merging would
    make every past paste a floor the count could never fall below, and the one number nobody can
    correct is the one that quietly hides a shortfall.

    Items the order doesn't need are ignored rather than reported as a problem: people select the
    whole hangar, and a hangar holds more than one build's materials.
    """
    from app.industry.assets import parse_stock_paste

    stock, unknown = parse_stock_paste(text)
    if not stock and not unknown:
        return {"matched": 0, "ignored": 0, "unknown": [], "error": "empty"}

    relevant = {tid: qty for tid, qty in stock.items() if tid in wanted}
    ensure_sourcing_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_sourced WHERE context_id=? AND order_id=?",
                    (context_id, order_id))
        now = time.time()
        for tid, qty in relevant.items():
            if qty > 0:
                con.execute(
                    "INSERT INTO pp_industry_sourced (context_id, order_id, type_id, qty, updated_at) "
                    "VALUES (?,?,?,?,?)", (context_id, order_id, int(tid), float(qty), now))
        con.commit()
    finally:
        con.close()
    return {"matched": len(relevant), "ignored": len(stock) - len(relevant), "unknown": unknown}


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
        # An order that makes its own reactions is gathering reaction INPUTS, not reaction outputs —
        # planning it without this would hand the builder a shopping list for a build they aren't
        # running.
        build_reactions_anyway=bool(order["build_reactions"]),
    )
    inp = prepare_plan_inputs(ctx, [(tid, qty)], req,
                             missing_recipe_detail=lambda t: f"order {t} has no recipe")
    res = build_plan(tid, qty, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names)
    return res.get("shopping_list") or []


def noted_stock_excess(ctx: int, order_rows) -> dict[int, float]:
    """{type_id: qty} — what the builder has SAID they hold that the plan is not already counting.

    "Marked as sourced" was a note to self and nothing more: the queue plan nets off your enabled
    stock and each curated order's bound boxes, but never the notes, so material you had already
    told the app you were holding kept appearing on the shopping list. That is the one place the
    tool argued with something the user had explicitly told it.

    The double-count is the whole difficulty, and the rule is the panel's own (`_item_row`): a note
    and a box are two answers to the same question, so take the better one, never the sum. The box
    is already in the pool, so only the EXCESS of the note over it is new information — a note of
    500 against a box holding 400 adds 100, and a note of 300 against the same box adds nothing.

    Orders are summed because the aggregated queue plan spends one pool. An order with no notes
    contributes nothing, so this is a no-op for anyone who has never used the sourcing panel.
    """
    from app.industry.assets import source_quantities_multi
    from app.industry.orders import order_source_keys

    out: dict[int, float] = {}
    for o in (order_rows or []):
        row = dict(o)
        noted = _manual(ctx, row["id"])
        if not noted:
            continue
        keys = order_source_keys(row) if int(row.get("sources_owned") or 0) else []
        held = source_quantities_multi(ctx, keys) if keys else {}
        for tid, qty in noted.items():
            excess = float(qty or 0.0) - float(held.get(tid, 0.0))
            if excess > 0:
                out[tid] = out.get(tid, 0.0) + excess
    return out


def _item_row(s: dict, in_source: dict[int, float], manual: dict[int, float]) -> dict:
    """One material's sourcing state, from its shopping-list row plus the two "have" signals.

    **This is not a second shopping list.** The two are the same materials seen two ways and they
    legitimately disagree — the queue plan nets off your stock and batches shared components once
    across every order, while this plans one order at its full requirement — so only one of them may
    talk about money. Two priced lists showing different numbers for the same item is how a page
    stops being believed. Hence no unit price, no market, no line cost here; the single exception is
    the SHORTFALL's cost, which is the one number that decides whether to go shopping at all.
    """
    tid = int(s["type_id"])
    need = float(s["qty"])
    held = float(in_source.get(tid, 0.0))
    noted = float(manual.get(tid, 0.0))
    # The box and the note are two answers to the same question, so take the better one: a note
    # never erases what's really in the container, and a rescan never erases a note.
    have = min(need, max(held, noted))
    short = max(0.0, need - have)
    unit = s.get("unit_price")
    return {
        "type_id": tid, "name": s["name"], "required": need,
        "in_source": held, "noted": noted, "sourced": have,
        "remaining": short, "done": have >= need - 1e-9,
        "remaining_cost": unit * short if unit is not None else None,
    }


def order_sourcing(ctx: int, order_id: int) -> dict:
    """Per-material sourcing state for one order."""
    from app.industry.assets import (list_source_sets, list_sources, source_labels, source_name,
                                     source_quantities_multi)
    from app.industry.orders import ensure_industry_orders_table, order_source_keys

    ensure_industry_orders_table()
    con = get_connection()
    try:
        order = con.execute(
            "SELECT id, product_type_id, name, quantity, label, force_build_ids, me_te_overrides, "
            "COALESCE(build_reactions,0) AS build_reactions, "
            "COALESCE(source_key,'') AS source_key, COALESCE(source_keys,'') AS source_keys, "
            "COALESCE(sources_owned,0) AS sources_owned "
            "FROM pp_industry_orders WHERE id=? AND context_id=?",
            (order_id, ctx),
        ).fetchone()
    finally:
        con.close()
    if not order:
        raise HTTPException(status_code=404, detail="order not found")

    keys = order_source_keys(order)
    key = keys[0] if keys else ""
    # Summed across every bound box — an item belongs to exactly one source, so the boxes add up
    # with nothing counted twice — and still capped per material by `_item_row`, which is what keeps
    # the "higher of paste and box wins" rule intact however many boxes there are.
    in_source = source_quantities_multi(ctx, keys)
    manual = _manual(ctx, order_id)

    items = [_item_row(s, in_source, manual) for s in _order_requirement(ctx, order)]
    items.sort(key=lambda r: (r["done"], -(r["remaining_cost"] or 0.0), r["name"]))

    done = sum(1 for i in items if i["done"])
    return {
        "order_id": order_id, "name": order["name"], "quantity": order["quantity"],
        "label": order["label"] or "",
        "source_key": key, "source_name": source_name(ctx, key) if key else None,
        # The full bound set — `source_key`/`source_name` stay as its first element so nothing that
        # only knows about one box has to change.
        "source_keys": keys, "bound": source_labels(ctx, keys),
        # Whether this plan owns its sources yet. An order queued before per-plan sources existed
        # still draws on the account-wide tick list, and the panel says so rather than implying a
        # set it doesn't have.
        "sources_owned": bool(order["sources_owned"]),
        # Offered so the picker can be built without a second call. Containers first: a build is
        # normally pulled from a box, not from a whole hangar. Grouped in the UI by where they are.
        "sources": sorted(list_sources(ctx), key=lambda s: (s["kind"] != "container",
                                                            s.get("place") or "", s["name"])),
        "sets": list_source_sets(ctx),
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
    refreshed checklist. The bulk answer is the paste endpoint below — this one exists for fixing a
    single line after the fact."""
    cur = order_sourcing(ctx, order_id)           # also 404s if the order isn't the caller's
    qty = req.qty
    if qty is None:
        item = next((i for i in cur["items"] if i["type_id"] == req.type_id), None)
        if item is None:
            raise HTTPException(status_code=400, detail="that material isn't in this order")
        qty = item["required"]
    set_sourced(ctx, order_id, int(req.type_id), float(qty))
    return order_sourcing(ctx, order_id)


class SourcedPaste(BaseModel):
    text: str


@router.post("/api/industry/orders/{order_id}/sourcing/paste")
def industry_order_sourcing_paste(order_id: int, req: SourcedPaste,
                                  ctx: int = Depends(require_context)):
    """Set what's been gathered from an EVE inventory paste — select the pile in the client, copy,
    paste. The whole point of the checklist is a capital build's 50-odd materials, which is far too
    many to confirm one at a time."""
    cur = order_sourcing(ctx, order_id)           # 404s if the order isn't the caller's
    res = apply_paste(ctx, order_id, req.text, {i["type_id"] for i in cur["items"]})
    out = order_sourcing(ctx, order_id)
    out["paste"] = res
    return out
