"""The endpoints: product search, a single-product plan, and the marginal sweep behind the
slider."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

from app.industry.graph.costs import build_plan
from app.industry.graph.options import IndustryPlanRequest
from app.industry.graph.resolve import prepare_plan_inputs
@router.get("/api/industry/search")
def industry_search(q: str, ctx: int = Depends(require_context)):
    """Buildable products (manufacturing or reaction) whose name matches `q` — the product picker.
    Shortest names first so an exact-ish match surfaces above longer variants."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    con = get_connection()
    try:
        # LOWER(...) both sides so the match is case-insensitive on Postgres too (its LIKE is
        # case-sensitive, unlike SQLite's) — otherwise "revelation" finds nothing on prod.
        rows = con.execute(
            "SELECT t.type_id, t.name FROM types t "
            "WHERE LOWER(t.name) LIKE ? AND (t.type_id IN (SELECT product_type_id FROM blueprints) "
            "OR t.type_id IN (SELECT output_type_id FROM reactions)) "
            "ORDER BY LENGTH(t.name), t.name LIMIT 25",
            (f"%{q.lower()}%",),
        ).fetchall()
        return {"results": [{"type_id": r["type_id"], "name": r["name"]} for r in rows]}
    finally:
        con.close()


def _cost_basis(params) -> dict:
    """What the job-installation fee was actually costed against, so a missing system cost index is
    visible instead of silently cheap. Job cost is EIV x (index + facility tax + 4% SCC); with no
    configured build system the index term is 0 and the quote is light by that share."""
    return {
        "system_id": params.build_system_id,
        "mfg_index": params.mfg_cost_index,
        "rx_index": params.rx_cost_index,
        "facility_tax_pct": params.facility_tax_pct,
        # A default has to say it is one — a wrong default is harder to notice than an absent one.
        "basis": params.build_system_basis,
    }


def _plan_on_hand(ctx: int, req) -> dict[int, float]:
    """Stock a single-product plan may net off.

    `source_keys` given = this plan owns its sources and counts those boxes only; absent OR EMPTY =
    the account-wide enabled pool, which is what every plan did before plans owned anything. Empty
    counts as absent for the same reason it does on an order: picking no box says nothing about
    where the materials come from, and answering "then you have nothing" would quietly turn the
    picker into a switch that disables stock netting.
    """
    from app.industry.assets import owned_quantities, source_quantities_multi
    keys = getattr(req, "source_keys", None)
    return source_quantities_multi(ctx, keys) if keys else owned_quantities(ctx)


@router.post("/api/industry/plan")
def industry_plan(req: IndustryPlanRequest, ctx: int = Depends(require_context)):
    """Read-only make-or-buy plan for one product+quantity: build tree, priced shopping list, and
    cost/time metrics. Own-account scoped (pricing follows the account's markets)."""
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    targets = [(req.type_id, req.quantity)]
    inp = prepare_plan_inputs(
        ctx, targets, req,
        missing_recipe_detail=lambda _tid: "No manufacturing or reaction recipe for that type")

    # Schedule the single build across the account's real slot pools (manufacturing + separate
    # reaction pool) for an honest MAKESPAN with parallelism — build_plan alone only sums job time
    # serially, which massively overstates wall-clock for a big build (a Nyx's 46 jobs are mostly
    # parallel). plan_queue gives schedule + batched cost + net-cost; build_plan supplies the tree.
    # Local imports avoid a graph↔schedule/assets import cycle.
    from app.industry.schedule import plan_queue
    # Net off what you already own (never the product itself — you asked to build that), from the
    # sources this plan is entitled to: the ones picked for it if any, else the account's tick list.
    on_hand = _plan_on_hand(ctx, req) if req.use_stock else {}
    on_hand.pop(req.type_id, None)
    result = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names,
                        inp.pools, on_hand=on_hand)
    result["target"] = {"type_id": req.type_id, "quantity": req.quantity,
                        "name": inp.names.get(req.type_id, str(req.type_id))}
    result["tree"] = build_plan(req.type_id, req.quantity, inp.mfg, inp.rx, inp.prices,
                                inp.adjusted, inp.params, inp.names)["tree"]
    # Name who installs each job, for every stage — not just the ones startable right now.
    from app.industry.schedule import assign_characters
    from app.industry.slots import _slot_pool
    # Which skills the account is missing to actually INSTALL these jobs. Returns None (and the
    # key is omitted) whenever the feature is off, so a disabled flag costs this endpoint nothing
    # — not a query, not a byte of payload. One call yields both the report and the eligibility
    # that keeps the scheduler from handing a job to someone who can't install it.
    from app.industry.skills import analyze_plan_skills
    sk = analyze_plan_skills(ctx, result.get("requirements") or [], inp.mfg, inp.rx)
    assign_characters(result["schedule"]["waves"], _slot_pool(ctx).get("characters") or [],
                      (sk or {}).get("eligibility"))
    if sk is not None:
        result["skill_gaps"] = sk["gaps"]
    # Whether the job times above came from real scanned skills or the V/V fallback. The number is
    # the same shape either way, so without this the user cannot tell a measurement from a guess.
    result["skill_time_basis"] = inp.params.skill_time_basis
    result["cost_basis"] = _cost_basis(inp.params)
    return result


@router.post("/api/industry/plan_sweep")
def industry_plan_sweep(req: IndustryPlanRequest, ctx: int = Depends(require_context)):
    """Cost + makespan for this product at every stop of the marginal-saving slider.

    The slider is one of the few genuine knobs here, and "3%" says nothing about what it costs you
    — so the UI reads the whole curve once and shows the time saved / ISK spent live as you drag,
    instead of firing a full replan per pixel. `marginal_pct` on the request is ignored: the sweep
    covers every value."""
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    targets = [(req.type_id, req.quantity)]
    # "Build everything" zeroes the threshold AND its floor — that's the point of it, but it would
    # flatten the curve the slider is asking about, so the sweep always resolves params with the
    # normal marginal rule in place.
    req = req.model_copy(update={"force_build": False, "marginal_pct": None})
    inp = prepare_plan_inputs(
        ctx, targets, req,
        missing_recipe_detail=lambda _tid: "No manufacturing or reaction recipe for that type")
    from app.industry.schedule import sweep_marginal
    on_hand = _plan_on_hand(ctx, req) if req.use_stock else {}
    on_hand.pop(req.type_id, None)
    points = sweep_marginal(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                            inp.names, inp.pools, on_hand=on_hand)
    return {"points": points}
