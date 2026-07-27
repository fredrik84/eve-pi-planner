"""Customer-facing build status links.

A builder taking work for someone else has no way to answer "how's my Revelation coming along?"
without screenshots. This mints a per-order link the customer can open with no account, no login and
no idea what EVE PI Planner is: what's being built, which stage it's on, how far through, and when
it should be done.

PRIVACY IS THE WHOLE DESIGN (rule 8 + the share-opsec rule the plan shares already follow). The
payload is deliberately assembled field by field rather than filtered from the plan, and carries:
product name, quantity, the label the builder typed, stage names + run counts, a percentage and an
ETA. It carries NO character names, NO systems or structures, NO ISK anywhere (cost, shopping list
or margin — what the builder pays is not the customer's business), and no other order of the
account. Anything added here in future must clear that bar.

The link is a random opaque id, revocable, and dies with its order.
"""
from __future__ import annotations

import json as _json
import secrets
import time as _time

from fastapi import Depends, HTTPException

from app.db import get_connection
from app.sde import ensure_once
from app.esi import require_context
from app.cache import cache_get_json, cache_set_json

from app.industry._router import router

# A public page anyone can refresh, and each render costs two plans. 60s is well inside the rate at
# which a build's state actually changes (jobs take hours) while stopping a shared link from being
# an amplification lever on the planner.
_STATUS_TTL = 60


@ensure_once
def ensure_industry_shares_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_shares (
                share_id   TEXT PRIMARY KEY,
                context_id INTEGER NOT NULL,
                order_id   INTEGER NOT NULL,
                created_at REAL NOT NULL,
                revoked    INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ind_shares_order ON pp_industry_shares (order_id)")
        con.commit()
    finally:
        con.close()


def _share_for_order(con, order_id: int, ctx: int):
    return con.execute(
        "SELECT * FROM pp_industry_shares WHERE order_id=? AND context_id=? AND revoked=0",
        (order_id, ctx),
    ).fetchone()


@router.post("/api/industry/orders/{order_id}/share")
def create_order_share(order_id: int, ctx: int = Depends(require_context)):
    """Mint (or return) the customer link for one queued order. Idempotent — asking twice gives the
    same link, so a builder who shared it once can re-copy it without invalidating what the customer
    already has."""
    from app.industry.orders import ensure_industry_orders_table, _order_row
    ensure_industry_orders_table()
    ensure_industry_shares_table()
    con = get_connection()
    try:
        _order_row(con, order_id, ctx)          # 404s unless it's the caller's order
        row = _share_for_order(con, order_id, ctx)
        if row:
            return {"share_id": row["share_id"], "created_at": row["created_at"]}
        sid = secrets.token_urlsafe(12)
        con.execute(
            "INSERT INTO pp_industry_shares (share_id, context_id, order_id, created_at, revoked) "
            "VALUES (?,?,?,?,0)", (sid, ctx, order_id, _time.time()))
        con.commit()
        return {"share_id": sid, "created_at": _time.time()}
    finally:
        con.close()


@router.delete("/api/industry/orders/{order_id}/share")
def revoke_order_share(order_id: int, ctx: int = Depends(require_context)):
    """Kill the link. Revoked rather than deleted so a link that leaked can't be resurrected by an
    id collision, and so the customer gets a clear 'no longer available' instead of a blank page."""
    ensure_industry_shares_table()
    con = get_connection()
    try:
        con.execute("UPDATE pp_industry_shares SET revoked=1 WHERE order_id=? AND context_id=?",
                    (order_id, ctx))
        con.commit()
        return {"revoked": order_id}
    finally:
        con.close()


@router.get("/api/industry/orders/{order_id}/share")
def get_order_share(order_id: int, ctx: int = Depends(require_context)):
    """The order's live link, if it has one — so the UI can show 'shared' state without minting."""
    ensure_industry_shares_table()
    con = get_connection()
    try:
        row = _share_for_order(con, order_id, ctx)
        return {"share_id": row["share_id"] if row else None,
                "created_at": row["created_at"] if row else None}
    finally:
        con.close()


def _stage_of_types(target_id: int, mfg: dict, rx: dict) -> dict[int, int]:
    """type_id → stage number, 1 = the deepest components, N = the final assembly.

    Depth-from-the-product is the honest ordering: a component's inputs are always built before it,
    so counting back from the target gives stages that really are sequential. `_depths` already
    computes exactly this walk for the scheduler; reusing it keeps the customer's stage numbering
    identical to the builder's pipeline instead of inventing a second notion of a stage."""
    from app.industry.schedule import _depths
    depth = _depths([target_id], mfg, rx)
    if not depth:
        return {}
    deepest = max(depth.values())
    return {tid: deepest - d + 1 for tid, d in depth.items()}


def _order_plan(ctx: int, product_type_id: int, quantity: int, force_ids: list[int],
                me_te: dict | None = None):
    """This order's OWN plan — requirements and stages for just what the customer ordered.

    Not the queue plan: that aggregates every order's demand into shared batches, so its run counts
    would both misstate this customer's build and quietly disclose how much other work the builder
    has on. The queue plan is still what supplies the ETA, because contention with the rest of the
    queue is real and the customer feels it."""
    from app.industry.graph import BuildOptions, prepare_plan_inputs
    from app.industry.schedule import plan_queue
    opts = BuildOptions(use_stock=False, force_build_ids=force_ids, me_te_overrides=me_te or {})
    inp = prepare_plan_inputs(
        ctx, [(product_type_id, quantity)], opts,
        missing_recipe_detail=lambda tid: f"order {tid} has no recipe")
    res = plan_queue([(product_type_id, quantity)], inp.mfg, inp.rx, inp.prices, inp.adjusted,
                     inp.params, inp.names, inp.pools, on_hand=None)
    return res, inp


def build_status(share_id: str) -> dict:
    """The customer-facing payload for a share link. See the module docstring for what may go in it.

    Raises 404 for an unknown, revoked, or orphaned link — a deleted order takes its link with it.
    """
    cached = cache_get_json(f"indshare:{share_id}")
    if cached is not None:
        return cached

    ensure_industry_shares_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT context_id, order_id FROM pp_industry_shares WHERE share_id=? AND revoked=0",
            (share_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="This build link is no longer available.")
        ctx, order_id = int(row["context_id"]), int(row["order_id"])
        order = con.execute(
            "SELECT id, product_type_id, name, quantity, COALESCE(label,'') AS label, status, "
            "COALESCE(force_build_ids,'') AS force_build_ids, "
            "COALESCE(me_te_overrides,'') AS me_te_overrides "
            "FROM pp_industry_orders WHERE id=? AND context_id=?", (order_id, ctx)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="This build link is no longer available.")
    finally:
        con.close()

    tid, qty = int(order["product_type_id"]), int(order["quantity"])
    try:
        force_ids = [int(v) for v in _json.loads(order["force_build_ids"] or "[]")]
    except Exception:
        force_ids = []
    # The order's own ME/TE choices shape its run counts and times, so the customer's view has to be
    # planned with them — otherwise the page's stages disagree with the builder's.
    try:
        me_te = _json.loads(order["me_te_overrides"] or "{}")
        me_te = me_te if isinstance(me_te, dict) else {}
    except Exception:
        me_te = {}

    res, inp = _order_plan(ctx, tid, qty, force_ids, me_te)
    stage_of = _stage_of_types(tid, inp.mfg, inp.rx)

    # Progress comes from the same ledgers the builder's own view reads, so the customer can never
    # be shown a rosier number than the builder sees.
    from app.industry.progress import _done_by_type, _running_by_type, _epoch
    since = _epoch(ctx)
    done_runs = _done_by_type(ctx, since)
    running_runs = _running_by_type(ctx, since)
    from app.industry.assets import owned_quantities
    owned = owned_quantities(ctx)

    stages: dict[int, dict] = {}
    for req in res.get("requirements", []):
        rtid = int(req["type_id"])
        need = int(req["runs"])
        if need <= 0:
            continue
        oq = int(req["output_qty"]) or 1
        # Same two-signal rule as the builder's progress view: owning the output proves it's built,
        # the ledger remembers it after it's consumed — take whichever is higher, capped at need.
        d = min(max(done_runs.get(rtid, 0), int(owned.get(rtid, 0) // oq)), need)
        r = min(running_runs.get(rtid, 0), max(0, need - d))
        st = stages.setdefault(stage_of.get(rtid, 1), {"required": 0, "done": 0, "running": 0,
                                                       "items": []})
        st["required"] += need
        st["done"] += d
        st["running"] += r
        st["items"].append({"name": req["name"], "runs": need, "done_runs": d, "running_runs": r})

    stage_list = []
    for n in sorted(stages):
        st = stages[n]
        pct = round(100.0 * st["done"] / st["required"], 1) if st["required"] else 0.0
        st["items"].sort(key=lambda i: -i["runs"])
        stage_list.append({
            "stage": len(stage_list) + 1,          # renumbered so gaps never show as "Stage 4 of 3"
            "name": "Final assembly" if n == max(stages) else f"Stage {len(stage_list) + 1}",
            "required_runs": st["required"], "done_runs": st["done"], "running_runs": st["running"],
            "pct": pct,
            "state": "complete" if pct >= 100 else "building" if st["done"] or st["running"] else "waiting",
            "items": st["items"][:12],             # the headline components, not an inventory
        })

    total_req = sum(s["required_runs"] for s in stage_list)
    total_done = sum(s["done_runs"] for s in stage_list)
    pct = round(100.0 * total_done / total_req, 1) if total_req else 0.0
    current = next((s["stage"] for s in stage_list if s["state"] != "complete"), None)

    # Units of the finished product actually in hand, so a part-delivered order reads honestly.
    units_done = min(int(owned.get(tid, 0)), qty)
    complete = order["status"] == "done" or (units_done >= qty and qty > 0)

    # ETA from the WHOLE queue's schedule: what the customer waits for includes the builder's other
    # commitments competing for the same slots. Falls back to this order's own makespan.
    eta = None
    try:
        from app.industry.orders import QueuePlanRequest, _run_queue_plan
        qres = _run_queue_plan(ctx, QueuePlanRequest())
        if not qres.get("empty"):
            for t in qres.get("targets", []):
                if int(t["type_id"]) == tid:
                    eta = t.get("finish_hours")
                    break
    except Exception:
        eta = None
    if eta is None:
        eta = res["metrics"]["makespan_hours"]

    now = _time.time()
    payload = {
        "product": order["name"],
        "quantity": qty,
        "label": order["label"],
        "status": "complete" if complete else "building" if total_done or any(
            s["running_runs"] for s in stage_list) else "waiting",
        "pct": 100.0 if complete else pct,
        "units_done": units_done,
        "jobs_done": total_done,
        "jobs_total": total_req,
        "current_stage": None if complete else current,
        "stages": stage_list,
        "eta_hours": 0.0 if complete else eta,
        "eta_at": None if complete else now + (eta or 0) * 3600.0,
        "updated_at": now,
    }
    cache_set_json(f"indshare:{share_id}", payload, ttl=_STATUS_TTL)
    return payload


@router.get("/api/industry/build-status/{share_id}")
def public_build_status(share_id: str):
    """PUBLIC — no session. Deliberately so: the whole point is a customer with no account. Returns
    only the customer-facing fields assembled in `build_status`."""
    return build_status(share_id)
