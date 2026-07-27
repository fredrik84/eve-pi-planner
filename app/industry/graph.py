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
    struct_material_mult: float = 1.0         # structure+rig MATERIAL multiplier (facility ME bonus)
    struct_time_mult: float = 1.0             # structure+rig TIME multiplier (facility TE bonus)
    # Industry-skill time reduction, assumed maxed (the standard for anyone building seriously):
    # manufacturing = Industry V (−20%) × Advanced Industry V (−15%) = 0.68; reactions get only
    # Advanced Industry V (−15%) = 0.85. Without these the planner over-states job time by ~30%.
    mfg_skill_time_mult: float = 0.68
    rx_skill_time_mult: float = 0.85
    reaction_material_mult: float = 1.0 - REACTION_ME_REDUCTION
    mfg_cost_index: float = 0.0               # system manufacturing cost index (0.0 = unknown)
    rx_cost_index: float = 0.0                # system reaction cost index
    facility_tax_pct: float = 0.0             # structure facility tax %
    build_margin: float = 0.0                 # build must beat buy by this fraction to be chosen
    max_build_hours: float = 0.0              # >0 → buy any component whose batch would take longer
                                              # than this wall-clock to build (time-priority mode)
    # Marginal-saving buy: don't bother building a component when the ISK it saves over buying is
    # trivial — either as a fraction of the WHOLE product's build cost (many tiny savings add up,
    # but each one isn't worth a job) or as a fraction of the component's own buy price.
    marginal_pct_of_total: float = 0.0        # buy if build saves < this % of total product cost
    min_saving_isk: float = 0.0               # buy if build saves < this absolute ISK (build isn't
                                              # worth the extra job/time for less)
    min_saving_pct: float = 0.0               # buy if build saves < this % of the component's buy price
    # Per-product ME/TE from the account's actual owned blueprints (product_type_id -> (me, te)).
    # When a product is here, its real researched efficiency is used instead of the global me_pct/
    # te_pct fallback. `owned` carries the same map's ownership detail for display.
    me_by_product: dict = field(default_factory=dict)
    owned: dict = field(default_factory=dict)
    # product_type_id -> "owned" | "contract" | "override". Purely for reporting: a plan that
    # silently assumed ME 10 off a contract is one the user can't sanity-check.
    me_source: dict = field(default_factory=dict)
    # Blueprint acquisition cost for types NOT owned — {type_id: {kind, price, runs_per_copy}}.
    # Empty means 'unknown', which leaves the old behaviour untouched.
    bp_acquire: dict = field(default_factory=dict)
    # Per-component override: build these no matter what the shortcuts say. The shortcuts are
    # heuristics about what's WORTH a job, and that's the user's call to make item by item — the
    # cost engine's own verdict (buying is outright cheaper) is untouched by this.
    force_build_ids: set = field(default_factory=set)
    # What to charge over cost when quoting a customer. Priced off NET cost — the leftovers a build
    # over-produces stay with the builder, so billing them to the customer would charge twice.
    margin_pct: float = 0.0

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

# The two recipe graphs are STATIC SDE data — ~4,800 blueprints and every material row — and they
# were rebuilt from scratch on every plan call: 68ms locally, more against Postgres, and the
# manufacturing page runs several plans per load. They're read-only to every consumer (verified: no
# caller mutates a recipe), so one process-level copy serves all of them. The TTL is only a backstop
# for an SDE rebuild under a long-lived process; a deploy restarts the pod anyway.
_GRAPH_CACHE: dict[str, tuple[float, dict]] = {}
_GRAPH_TTL = 900.0


def _cached_graph(key: str, con, loader) -> dict[int, dict]:
    import time as _t
    hit = _GRAPH_CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < _GRAPH_TTL:
        return hit[1]
    graph = loader(con)
    _GRAPH_CACHE[key] = (now, graph)
    return graph


def clear_graph_cache():
    """Drop the cached recipe graphs — for an SDE rebuild that has to take effect immediately."""
    _GRAPH_CACHE.clear()


def load_manufacturing_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`. `_load_manufacturing_graph` does the real work."""
    return _cached_graph("mfg", con, _load_manufacturing_graph)


def load_reaction_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`."""
    return _cached_graph("rx", con, _load_reaction_graph)


def _load_manufacturing_graph(con) -> dict[int, dict]:
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


def _load_reaction_graph(con) -> dict[int, dict]:
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
        st = params.struct_time_mult if activity == "manufacturing" else 1.0
        skill = params.mfg_skill_time_mult if activity == "manufacturing" else params.rx_skill_time_mult
        job_seconds = recipe["base_time"] * runs * (1 - te / 100.0) * st * skill
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

class BuildOptions(BaseModel):
    """Everything that shapes HOW a build is costed and scheduled, independent of WHAT is being
    built. Shared base for the one-shot plan (a product + quantity) and the whole-queue plan (the
    account's queued orders), which had drifted into two hand-maintained copies of this list."""
    me_pct: float = 0.0
    te_pct: float = 0.0
    system_id: int | None = None       # None → derive from the account's Reactions build system
    facility_tax_pct: float | None = None
    prioritize_speed: bool = True      # buy slow-to-build bulk components to minimize makespan
    struct_material_pct: float = 0.0   # facility ME bonus (material reduction %)
    struct_time_pct: float = 0.0       # facility TE bonus (time reduction %)
    use_stock: bool = True             # net owned materials off the demand (needs an asset scan)
    marginal_pct: float | None = None  # build only if it saves >= this % of the build (None = default)
    force_build: bool = False          # build everything buildable, ignoring small-saving shortcuts
    force_build_ids: list[int] = []    # build THESE components regardless of the shortcuts
    # Per-product ME/TE the user wants assumed, {"<type_id>": [me, te]} — JSON keys are strings.
    # Wins over everything: it's the user telling us which print they'll actually use.
    me_te_overrides: dict[str, list[int]] = {}
    margin_pct: float | None = None    # markup over net cost for the customer price (None = default)


class IndustryPlanRequest(BuildOptions):
    type_id: int
    quantity: int = 1


# Wall-clock cap (hours) a single component's batch may take to build before the time-priority
# make-or-buy buys it instead. ~1 day: builds fast components, buys the multi-day bulk marathons.
SPEED_BUILD_CAP_HOURS = 24.0

# Default markup when quoting a build to a customer. A starting point, not a recommendation — it's
# the one number here only the builder can know, so it's a knob with a sane default.
MARGIN_DEFAULT_PCT = 10.0

# Marginal-saving threshold (always on): buy a component the cost engine would build if building it
# saves less than this % of the TOTAL product cost. Measured against the whole build — NOT a
# per-component percentage, which doesn't scale (4% of an Ishtar component is a sub-1m difference
# that shouldn't drive a decision, while 4% of a Revelation component is real money). This one
# lens auto-scales: it trims the long tail on a 2.35b capital but barely touches a 147m cruiser.
#
# Raised 0.1% -> 3% once the market-pricing fix let components actually be costed. At 0.1% the
# threshold was ~2m on a Revelation, under the absolute floor below, so it never bound: the planner
# built 18 component types / 471 jobs to save ~70m on a 2.4b hull. 3% (~72m there) keeps the builds
# that genuinely move the number and buys the long tail, which is the stated priority — time first,
# cost competitive but not at the price of days of clicking.
MARGINAL_BUILD_PCT_OF_TOTAL = 3.0
# Absolute floor: a build step isn't worth its slot/time unless it saves at least this much ISK,
# so anything cheaper to build by less than this is bought (prioritize time). This is what makes a
# cheap ship — where most components each save only a little — mostly BUY-and-assemble rather than
# a multi-day build. The effective threshold is max(this, MARGINAL_BUILD_PCT_OF_TOTAL × total): the
# floor governs small hulls (3% of a 147m Ishtar is ~4m, under it) while the percentage governs
# capitals, so one rule covers both ends without a per-component percentage.
MIN_BUILD_SAVING_ISK = 5_000_000


def account_industry_time_mults(context_id: int) -> tuple[float, float]:
    """(manufacturing, reaction) job-time multipliers from the account's best Industry / Advanced
    Industry levels — Industry −4%/level (manufacturing), Advanced Industry −3%/level (all jobs).
    Uses the highest across the account's characters (you build on your best-skilled toon). Falls
    back to V/V (the norm for anyone building) when no skill data is stored yet — a rescan replaces
    the assumption with the real numbers."""
    ind = adv = 0
    try:
        con = get_connection()
        r = con.execute(
            "SELECT COALESCE(MAX(industry),0) AS i, COALESCE(MAX(advanced_industry),0) AS a "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchone()
        con.close()
        ind, adv = int(r["i"] or 0), int(r["a"] or 0)
    except Exception:
        pass
    if ind == 0 and adv == 0:
        ind = adv = 5   # not fetched (or a fresh account) → assume trained, corrected on rescan
    return ((1 - 0.04 * ind) * (1 - 0.03 * adv), (1 - 0.03 * adv))


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
                         system_id: int | None, facility_tax_pct: float | None,
                         max_build_hours: float = 0.0,
                         struct_material_pct: float = 0.0, struct_time_pct: float = 0.0,
                         marginal_pct: float | None = None, force_build: bool = False,
                         force_build_ids: list[int] | None = None,
                         margin_pct: float | None = None) -> BuildParams:
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
    mfg_skill, rx_skill = account_industry_time_mults(context_id)
    return BuildParams(
        me_pct=me_pct, te_pct=te_pct,
        mfg_skill_time_mult=mfg_skill, rx_skill_time_mult=rx_skill,
        mfg_cost_index=fetch_system_cost_index(sid, "manufacturing"),
        rx_cost_index=fetch_system_cost_index(sid, "reaction"),
        facility_tax_pct=tax, me_by_product=me_by_product, owned=owned,
        max_build_hours=max_build_hours,
        struct_material_mult=1.0 - struct_material_pct / 100.0,
        struct_time_mult=1.0 - struct_time_pct / 100.0,
        # User-tunable: how much of the build's value a component must save to be worth building.
        # None keeps the default. This is a genuine time-vs-cost preference the math can't settle,
        # which is why it's a knob at all. min_saving_pct stays 0 (per-component % doesn't scale).
        #
        # force_build drops BOTH shortcuts — the percentage and the absolute floor. Note the floor
        # is why the slider alone can't express this: at 0% the 5m floor still applies, so small
        # components keep getting bought. Building at an outright LOSS is still refused; "ignore
        # marginal savings" means small gains count, not that paying more to build is sensible.
        marginal_pct_of_total=(0.0 if force_build else
                               (MARGINAL_BUILD_PCT_OF_TOTAL if marginal_pct is None
                                else max(0.0, min(25.0, float(marginal_pct))))),
        min_saving_isk=(0.0 if force_build else MIN_BUILD_SAVING_ISK),
        force_build_ids=set(force_build_ids or ()),
        margin_pct=(MARGIN_DEFAULT_PCT if margin_pct is None
                    else max(0.0, min(100.0, float(margin_pct)))),
    )


@dataclass
class PlanInputs:
    """Everything a scheduler run needs, resolved once: recipe graphs, names, live prices and the
    build params + slot pools for this account."""
    mfg: dict
    rx: dict
    names: dict[int, str]
    ids: list[int]
    prices: dict
    adjusted: dict
    params: BuildParams
    pools: dict[str, int]


def prepare_plan_inputs(ctx: int, targets: list[tuple[int, int]], opts: BuildOptions,
                        mfg_slots: int | None = None, rx_slots: int | None = None,
                        missing_recipe_detail=None) -> PlanInputs:
    """Resolve the graphs, prices and parameters for a set of build targets.

    /api/industry/plan and the queue endpoints ran identical 35-line preambles — load both graphs,
    validate the targets, collect the reachable type ids, fetch names, price them, resolve the
    build params, look up blueprint acquisition costs, size the slot pools. Three copies meant a
    fix like the bp_acquire best-effort block had to be made three times (and once wasn't). One
    resolver, and the endpoints are left holding only what actually differs between them.

    `missing_recipe_detail(type_id)` builds the 400 message, since the wording is caller-specific.
    """
    from app.industry.slots import _slot_pool     # local: avoids a graph↔slots import cycle
    from app.industry.settings import apply_account_build_options

    # Anything the caller didn't explicitly set comes from the account's saved build options, so a
    # plan run without a browser (share link, checklist) matches what the user actually builds with.
    opts = apply_account_build_options(ctx, opts)

    con = get_connection()
    try:
        mfg = load_manufacturing_graph(con)
        rx = load_reaction_graph(con)
        ids: set[int] = set()
        for tid, _qty in targets:
            if tid not in mfg and tid not in rx:
                detail = (missing_recipe_detail(tid) if missing_recipe_detail
                          else f"No manufacturing or reaction recipe for type {tid}")
                raise HTTPException(status_code=400, detail=detail)
            ids |= collect_reachable(tid, mfg, rx)
        names = {r["type_id"]: r["name"]
                 for r in con.execute(
                     f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                     tuple(ids))}
    finally:
        con.close()

    id_list = list(ids)
    prices = resolve_market_data(ctx, id_list)
    adjusted = fetch_adjusted_prices(id_list)
    # force_build also drops the speed shortcut: that one buys slow bulk components, which is
    # exactly what someone asking to build everything does not want.
    mbh = 0.0 if opts.force_build else (SPEED_BUILD_CAP_HOURS if opts.prioritize_speed else 0.0)
    params = resolve_build_params(ctx, opts.me_pct, opts.te_pct, opts.system_id,
                                  opts.facility_tax_pct, mbh, opts.struct_material_pct,
                                  opts.struct_time_pct, opts.marginal_pct, opts.force_build,
                                  opts.force_build_ids, opts.margin_pct)
    # What an unowned blueprint would cost to acquire, so building can be priced honestly and the
    # margin-saver can see it. Best-effort: an empty index just leaves the old behaviour.
    try:
        from app.industry.bpc import acquisition_costs, representative_me_te
        params.bp_acquire = acquisition_costs(id_list, params.owned)
    except Exception:
        params.bp_acquire = {}
    # A print you don't own was costed at ME 0 / TE 0 — the un-researched worst case — even though
    # the copy you'd buy is usually researched, and its ME/TE is already sitting in the contract
    # index we just priced against. That inflated both materials and job time on every component
    # bought as a BPC. Owned blueprints still win: your own print is what you'd actually use.
    for tid, info in (params.bp_acquire or {}).items():
        if tid in params.me_by_product or info.get("kind") != "bpc":
            continue
        me_te = representative_me_te(info)
        if me_te:
            params.me_by_product[tid] = me_te
            params.me_source[tid] = "contract"
    for tid in params.owned:
        params.me_source.setdefault(tid, "owned")
    # The user's own call comes last — they know which print they're really using.
    for key, val in (opts.me_te_overrides or {}).items():
        try:
            tid = int(key)
            me, te = float(val[0]), float(val[1])
        except Exception:
            continue
        params.me_by_product[tid] = (max(0.0, min(10.0, me)), max(0.0, min(20.0, te)))
        params.me_source[tid] = "override"

    pool = _slot_pool(ctx)
    pools = {
        "manufacturing": max(1, mfg_slots if mfg_slots is not None else pool["manufacturing_slots"]),
        "reaction": max(1, rx_slots if rx_slots is not None else pool["reaction_slots"]),
    }
    return PlanInputs(mfg=mfg, rx=rx, names=names, ids=id_list, prices=prices,
                      adjusted=adjusted, params=params, pools=pools)


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
    from app.industry.assets import owned_quantities
    # Net off what you already own (never the product itself — you asked to build that).
    on_hand = owned_quantities(ctx) if req.use_stock else {}
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
    assign_characters(result["schedule"]["waves"], _slot_pool(ctx).get("characters") or [])
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
    from app.industry.assets import owned_quantities
    on_hand = owned_quantities(ctx) if req.use_stock else {}
    on_hand.pop(req.type_id, None)
    points = sweep_marginal(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                            inp.names, inp.pools, on_hand=on_hand)
    return {"points": points}
