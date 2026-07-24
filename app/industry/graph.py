"""Industry (manufacturing) make-or-buy engine — Phase 1.

Given a target buildable and a quantity, walk the recipe graph and decide, for every intermediate,
whether it's cheaper to BUILD it (manufacture / react from inputs) or BUY it off the market — then
produce a priced shopping list, a build tree, and cost + time metrics.

The producer graph spans TWO SDE recipe sources so cost totals are honest for capital / T2 / T3
builds that mix both:
  - manufacturing blueprints (`blueprints` / `blueprint_materials`, built by scripts/build_sde.py)
  - reactions (`reactions` / `reaction_inputs` — the same tables app/reactions reads)
Anything with neither recipe (minerals, moon goo, PI, datacores, raw items) is a terminal BUY node.

Pricing reuses the Reactions stack verbatim: materials are priced by
`app.markets.resolve_market_data` (followed local/alliance markets in priority order → Jita
fallback), and job installation fees reuse `app.industry_cost` (EIV × (system cost index +
facility tax + 4% SCC), EIV valued at CCP's adjusted prices).

This module is the read-only cost core. Scheduling, slots, queueing, alerting, and actually
*spawning* reaction orders into the Reactions service are later phases — see
docs/industry-planner-spec.md. For now a reaction node is costed generically here; Phase 3 routes
it through app.reactions for the authoritative economics + slot allocation.
"""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

# Flat 4% SCC surcharge CCP applies to every industry job's estimated item value (EIV), on top of
# the system cost index and facility tax — same constant app.reactions.graph uses.
SCC_SURCHARGE_PCT = 0.04

# Reaction material bonus from the T1 reactor rig (Standup L-Set Reactor Efficiency I): -2.2%
# materials. Matches app.reactions.graph.REACTION_ME_REDUCTION so the two engines agree on
# reaction input quantities. Manufacturing ME comes from the (per-player) blueprint library later;
# for now it's a plan-time parameter.
REACTION_ME_REDUCTION = 0.022


@dataclass
class BuildParams:
    """Knobs the resolver prices against. Phase 1 defaults reproduce a bare, un-researched build
    (ME/TE 0, no structure/rig material bonus) plus the always-on 4% SCC. Later phases populate
    ME/TE per blueprint from the library and the cost indices from the configured build system."""
    me_pct: float = 0.0                       # manufacturing material efficiency (0–10)
    te_pct: float = 0.0                       # time efficiency (0–20)
    struct_material_mult: float = 1.0         # structure+rig material multiplier (manufacturing)
    reaction_material_mult: float = 1.0 - REACTION_ME_REDUCTION
    mfg_cost_index: float = 0.0               # system manufacturing cost index (0.0 = unknown)
    rx_cost_index: float = 0.0                # system reaction cost index
    facility_tax_pct: float = 0.0             # structure facility tax %
    build_margin: float = 0.0                 # build must beat buy by this fraction to be chosen
    # Per-product ME/TE from the account's actual owned blueprints (product_type_id -> (me, te)).
    # When a product is here, its real researched efficiency is used instead of the global me_pct/
    # te_pct fallback. `owned` carries the same map's ownership detail for display.
    me_by_product: dict = field(default_factory=dict)
    owned: dict = field(default_factory=dict)

    def me_te_for(self, type_id: int, activity: str) -> tuple[float, float]:
        """(me_pct, te_pct) for a manufacturing product: its owned-blueprint values if known, else
        the global fallback. Reactions have no blueprint ME/TE (rig-based, via the material mult),
        so they return (0, 0)."""
        if activity != "manufacturing":
            return (0.0, 0.0)
        if type_id in self.me_by_product:
            return self.me_by_product[type_id]
        return (self.me_pct, self.te_pct)


# ── SDE recipe graph loaders ──────────────────────────────────────────────────────────────────

def load_manufacturing_graph(con) -> dict[int, dict]:
    """product_type_id -> {blueprint_type_id, output_qty, base_time, max_runs, inputs}. If a
    product is (rarely) made by more than one blueprint, keep the lowest blueprint id so the graph
    is deterministic."""
    mats: dict[int, list[dict]] = {}
    for r in con.execute("SELECT blueprint_type_id, type_id, quantity FROM blueprint_materials"):
        mats.setdefault(r["blueprint_type_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute(
        "SELECT blueprint_type_id, product_type_id, output_qty, base_time, max_runs FROM blueprints"
    ):
        prod = r["product_type_id"]
        if prod in graph and graph[prod]["blueprint_type_id"] <= r["blueprint_type_id"]:
            continue
        graph[prod] = {
            "blueprint_type_id": r["blueprint_type_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["base_time"] or 0,
            "max_runs": r["max_runs"] or 0,
            "inputs": mats.get(r["blueprint_type_id"], []),
        }
    return graph


def load_reaction_graph(con) -> dict[int, dict]:
    """product_type_id -> {reaction_id, output_qty, base_time, inputs}. Same tables app/reactions
    reads; loaded directly here (no cross-package import) to keep the make-or-buy core standalone.
    First formula wins on the rare double-output product."""
    inputs: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        prod = r["output_type_id"]
        if prod in graph:
            continue
        graph[prod] = {
            "reaction_id": r["reaction_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["cycle_time"] or 0,
            "inputs": inputs.get(r["reaction_id"], []),
        }
    return graph


# ── EVE material / time formulas ──────────────────────────────────────────────────────────────

def effective_material_qty(base_qty: int, runs: int, me_pct: float, bonus_mult: float) -> int:
    """CCP's per-job material formula: max(runs, ceil(round(baseQty·runs·(1−ME/100)·bonus, 2))).
    The max(runs, …) floor means a material never drops below 1 per run however good the ME."""
    if base_qty <= 0 or runs <= 0:
        return 0
    q = base_qty * runs * (1 - me_pct / 100.0) * bonus_mult
    return max(runs, math.ceil(round(q, 2)))


def _producer(type_id: int, mfg: dict, rx: dict) -> tuple[str | None, dict | None]:
    if type_id in mfg:
        return "manufacturing", mfg[type_id]
    if type_id in rx:
        return "reaction", rx[type_id]
    return None, None


def collect_reachable(target: int, mfg: dict, rx: dict) -> set[int]:
    """Every type_id reachable from the target through the producer graph (target + all recipe
    inputs, recursively), cycle-guarded — the set to price in one market call."""
    seen: set[int] = set()

    def walk(tid: int):
        if tid in seen:
            return
        seen.add(tid)
        _, recipe = _producer(tid, mfg, rx)
        if recipe:
            for inp in recipe["inputs"]:
                walk(inp["type_id"])

    walk(target)
    return seen


# ── Make-or-buy resolution ────────────────────────────────────────────────────────────────────

def resolve_unit_costs(mfg: dict, rx: dict, prices: dict, adjusted: dict,
                       params: BuildParams) -> dict[int, dict]:
    """Bottom-up unit cost + build/buy decision for every reachable node. Memoized per type_id,
    cycle-guarded (a recipe cycle degrades to buy). Unit build cost uses a representative single
    run for the decision; the top-down explode uses the real per-job quantities."""
    memo: dict[int, dict] = {}

    def unit(type_id: int, stack: frozenset) -> dict:
        if type_id in memo:
            return memo[type_id]
        buy = (prices.get(type_id) or {}).get("sell_price") or None
        activity, recipe = _producer(type_id, mfg, rx)
        build_uc = None
        if recipe and type_id not in stack:
            me, _te = params.me_te_for(type_id, activity)
            mult = (params.struct_material_mult if activity == "manufacturing"
                    else params.reaction_material_mult)
            ci = params.mfg_cost_index if activity == "manufacturing" else params.rx_cost_index
            inner = stack | {type_id}
            total_in = 0.0
            eiv = 0.0
            ok = True
            for inp in recipe["inputs"]:
                child = unit(inp["type_id"], inner)
                if child["unit_cost"] is None:
                    ok = False
                    break
                qty = effective_material_qty(inp["quantity"], 1, me, mult)
                total_in += child["unit_cost"] * qty
                eiv += inp["quantity"] * adjusted.get(inp["type_id"], 0.0)
            if ok:
                job = eiv * (ci + params.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT)
                build_uc = (total_in + job) / recipe["output_qty"]

        if build_uc is not None and buy is not None:
            decision = "build" if build_uc < buy * (1 - params.build_margin) else "buy"
        elif build_uc is not None:
            decision = "build"
        elif buy is not None:
            decision = "buy"
        else:
            decision = "unresolved"
        unit_cost = (build_uc if decision == "build"
                     else buy if decision == "buy" else None)
        node = {
            "type_id": type_id, "activity": activity, "buildable": recipe is not None,
            "decision": decision, "unit_cost": unit_cost,
            "build_unit_cost": build_uc, "buy_unit_cost": buy,
            "source": (prices.get(type_id) or {}).get("source"),
        }
        memo[type_id] = node
        return node

    return memo, unit


def build_plan(target: int, quantity: int, mfg: dict, rx: dict, prices: dict, adjusted: dict,
               params: BuildParams, names: dict[int, str]) -> dict:
    """Full read-only plan: resolve unit costs, then explode the target quantity top-down into a
    build tree, an aggregated priced shopping list, a job list, and cost + time metrics. The root
    is always built (that's what the user asked to make), even if buying it would be cheaper —
    that comparison is still surfaced on the root node."""
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    unit(target, frozenset())  # populate the memo from the target down

    shopping: dict[int, float] = {}
    jobs: list[dict] = []
    totals = {"materials_cost": 0.0, "job_cost": 0.0, "job_seconds": 0.0, "leftover_value": 0.0}

    def explode(type_id: int, qty: float, is_root: bool = False) -> dict:
        node = memo[type_id]
        activity, recipe = _producer(type_id, mfg, rx)
        do_build = node["decision"] == "build" or (is_root and node["buildable"])
        if not do_build or recipe is None:
            price = node["buy_unit_cost"]
            line = (price or 0.0) * qty
            if node["decision"] != "unresolved":
                shopping[type_id] = shopping.get(type_id, 0.0) + qty
                totals["materials_cost"] += line
            return {
                "type_id": type_id, "name": names.get(type_id, str(type_id)),
                "decision": "unresolved" if node["decision"] == "unresolved" else "buy",
                "qty": qty, "unit_cost": price, "line_cost": line if price else None,
                "source": node.get("source"),
            }

        me, te = params.me_te_for(type_id, activity)
        mult = (params.struct_material_mult if activity == "manufacturing"
                else params.reaction_material_mult)
        ci = params.mfg_cost_index if activity == "manufacturing" else params.rx_cost_index
        output_qty = recipe["output_qty"]
        runs = max(1, math.ceil(qty / output_qty))
        produced = runs * output_qty
        if produced > qty:   # batch-rounding overproduction is reusable inventory, credit it back
            totals["leftover_value"] += (produced - qty) * (node["build_unit_cost"] or 0.0)

        children = []
        eiv = 0.0
        for inp in recipe["inputs"]:
            need = effective_material_qty(inp["quantity"], runs, me, mult)
            # EIV (the job-cost basis) uses BASE quantities × runs — ME never reduces it.
            eiv += inp["quantity"] * runs * adjusted.get(inp["type_id"], 0.0)
            children.append(explode(inp["type_id"], need))
        job_cost = eiv * (ci + params.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT)
        job_seconds = recipe["base_time"] * runs * (1 - te / 100.0)
        totals["job_cost"] += job_cost
        totals["job_seconds"] += job_seconds
        jobs.append({
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "activity": activity, "runs": runs, "output_qty": output_qty,
            "produced": produced, "job_cost": job_cost, "job_seconds": job_seconds,
        })
        return {
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "decision": "build", "activity": activity, "qty": qty, "runs": runs,
            "produced": produced, "excess": produced - qty,
            "unit_cost": node["build_unit_cost"], "buy_unit_cost": node["buy_unit_cost"],
            "job_cost": job_cost, "owned": params.owned.get(type_id), "inputs": children,
        }

    tree = explode(target, quantity, is_root=True)

    shopping_list = sorted(
        (
            {
                "type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                "unit_price": (prices.get(tid) or {}).get("sell_price"),
                "source": (prices.get(tid) or {}).get("source"),
                "line_cost": ((prices.get(tid) or {}).get("sell_price") or 0.0) * qty,
            }
            for tid, qty in shopping.items()
        ),
        key=lambda r: r["line_cost"], reverse=True,
    )
    unresolved = [s["type_id"] for s in shopping_list if s["unit_price"] is None]
    total_cost = totals["materials_cost"] + totals["job_cost"]
    return {
        "target": {"type_id": target, "name": names.get(target, str(target)), "quantity": quantity},
        "tree": tree,
        "shopping_list": shopping_list,
        "jobs": jobs,
        "metrics": {
            "materials_cost": round(totals["materials_cost"], 2),
            "job_cost": round(totals["job_cost"], 2),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(totals["leftover_value"], 2),
            "net_cost": round(total_cost - totals["leftover_value"], 2),
            "job_count": len(jobs),
            "total_job_hours": round(totals["job_seconds"] / 3600.0, 2),
        },
        "unresolved": unresolved,
    }


# ── Endpoint ──────────────────────────────────────────────────────────────────────────────────

class IndustryPlanRequest(BaseModel):
    type_id: int
    quantity: int = 1
    me_pct: float = 0.0
    te_pct: float = 0.0
    system_id: int | None = None       # None → derive from the account's Reactions build system
    facility_tax_pct: float | None = None


def account_build_defaults(context_id: int) -> tuple[int | None, float]:
    """Zero-config build context: reuse the system + facility tax the account already configured
    for Reactions as the default build location, so the Industry planner gets real cost indices
    and tax with no separate setup. Least-effort by design — the player configured this once; a
    dedicated industry build-system override can come later. Returns (system_id, facility_tax_pct);
    (None, 0.0) if Reactions was never configured (safe no-cost-effect default)."""
    try:
        from app.reactions.settings import effective_reaction_settings, _resolve_system_id
        s = effective_reaction_settings(context_id)
        sid = _resolve_system_id(s.get("reaction_system"))
        return sid, (s.get("facility_tax_pct") or 0.0)
    except Exception:
        return None, 0.0


def resolve_build_params(context_id: int, me_pct: float, te_pct: float,
                         system_id: int | None, facility_tax_pct: float | None) -> BuildParams:
    """Build the resolver's params, auto-deriving the build system + tax from the account's
    Reactions settings when the request didn't override them — so the caller needn't supply a
    system id or tax by hand."""
    d_sid, d_tax = account_build_defaults(context_id)
    sid = system_id if system_id is not None else d_sid
    tax = facility_tax_pct if facility_tax_pct is not None else d_tax
    # Auto per-product ME/TE from the account's real owned blueprints (empty if not connected).
    try:
        from app.industry.blueprints import owned_blueprints
        owned = owned_blueprints(context_id)
    except Exception:
        owned = {}
    me_by_product = {p: (o["me"], o["te"]) for p, o in owned.items()}
    return BuildParams(
        me_pct=me_pct, te_pct=te_pct,
        mfg_cost_index=fetch_system_cost_index(sid, "manufacturing"),
        rx_cost_index=fetch_system_cost_index(sid, "reaction"),
        facility_tax_pct=tax, me_by_product=me_by_product, owned=owned,
    )


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


@router.post("/api/industry/plan")
def industry_plan(req: IndustryPlanRequest, ctx: int = Depends(require_context)):
    """Read-only make-or-buy plan for one product+quantity: build tree, priced shopping list, and
    cost/time metrics. Own-account scoped (pricing follows the account's markets)."""
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    con = get_connection()
    try:
        mfg = load_manufacturing_graph(con)
        rx = load_reaction_graph(con)
        if req.type_id not in mfg and req.type_id not in rx:
            raise HTTPException(status_code=400, detail="No manufacturing or reaction recipe for that type")
        ids = collect_reachable(req.type_id, mfg, rx)
        names = {r["type_id"]: r["name"]
                 for r in con.execute(
                     f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                     tuple(ids))}
    finally:
        con.close()

    prices = resolve_market_data(ctx, list(ids))
    adjusted = fetch_adjusted_prices(list(ids))
    params = resolve_build_params(ctx, req.me_pct, req.te_pct, req.system_id, req.facility_tax_pct)

    # Schedule the single build across the account's real slot pools (manufacturing + separate
    # reaction pool) for an honest MAKESPAN with parallelism — build_plan alone only sums job time
    # serially, which massively overstates wall-clock for a big build (a Nyx's 46 jobs are mostly
    # parallel). plan_queue gives schedule + batched cost + net-cost; build_plan supplies the tree.
    # Local imports avoid a graph↔schedule/slots import cycle.
    from app.industry.schedule import plan_queue
    from app.industry.slots import _slot_pool
    pool = _slot_pool(ctx)
    pools = {"manufacturing": max(1, pool["manufacturing_slots"]),
             "reaction": max(1, pool["reaction_slots"])}
    result = plan_queue([(req.type_id, req.quantity)], mfg, rx, prices, adjusted, params, names, pools)
    result["target"] = {"type_id": req.type_id, "name": names.get(req.type_id, str(req.type_id)),
                        "quantity": req.quantity}
    result["tree"] = build_plan(req.type_id, req.quantity, mfg, rx, prices, adjusted, params, names)["tree"]
    return result
