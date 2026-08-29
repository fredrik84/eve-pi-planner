"""`prepare_plan_inputs` — the ONE resolver every plan path goes through — with the account
snapshot it caches and the params it resolves."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

from app.industry.graph.params import BuildParams, blend_me_te
from app.industry.graph.sde import collect_reachable, load_manufacturing_graph, load_reaction_graph
from app.industry.graph.options import (
    BuildOptions,
    MARGINAL_BUILD_PCT_OF_TOTAL,
    MARGIN_DEFAULT_PCT,
    MIN_BUILD_SAVING_ISK,
    SPEED_BUILD_CAP_HOURS,
    account_build_defaults,
    account_industry_time_mults,
)
# ── Per-account snapshot ──────────────────────────────────────────────────────────────────────
# Measured on prod before this existed: `resolve_build_params` cost 1.3-4.9s of a page load whose
# actual PLANNING was 30ms. All of it is per-account reads — the blueprint holding, its coverage,
# declared products, stock formula prints, the account's skills and its build defaults — and none
# of them change between the two calls a single page load makes, so the whole cost was paid twice
# for an identical answer.
#
# Cached here rather than caching `resolve_build_params` itself, deliberately: that function also
# takes the REQUEST's knobs (me_pct, marginal_pct, force_build, the per-order id lists), so keying a
# cache on all of them would either explode or — far worse — hand back a params object built with
# somebody else's ME. What is cached is only the part that depends on nothing but the account.
#
# The TTL is short and the invalidation is explicit: anything that writes one of these reads calls
# `clear_account_snapshot`. The TTL is the backstop for what that misses, not the mechanism.
_ACCOUNT_CACHE: dict[int, tuple[float, dict]] = {}
_ACCOUNT_TTL = 90.0


def clear_account_snapshot(context_id: int | None = None):
    """Drop a context's cached account reads — or every context when called bare. Called by the
    writes that can change any of them: a blueprint or asset refresh, a skills scan, a settings
    save. A miss here costs one slow plan; a stale hit costs a plan built on numbers the user has
    just changed and can see are wrong, so this errs towards dropping too much."""
    if context_id is None:
        _ACCOUNT_CACHE.clear()
        from app.industry.status_cache import invalidate_status
        invalidate_status()
    else:
        _ACCOUNT_CACHE.pop(int(context_id), None)
        from app.industry.status_cache import invalidate_status
        invalidate_status(context_id)


def _account_snapshot(context_id: int) -> dict:
    """Everything `resolve_build_params` reads that depends only on the ACCOUNT."""
    import time as _t
    hit = _ACCOUNT_CACHE.get(context_id)
    now = _t.time()
    if hit and now - hit[0] < _ACCOUNT_TTL:
        return hit[1]

    defaults = account_build_defaults(context_id, with_basis=True)
    # Auto per-product ME/TE from the account's real owned blueprints (empty if not connected).
    try:
        from app.industry.blueprints import owned_blueprints, blueprint_coverage
        owned = owned_blueprints(context_id)
        # ...and how much of the account that holding actually covers. Everything that counts PRINTS
        # (how many jobs of a type can run at once) is only allowed to trust it when every character
        # is cached — see BuildParams.prints_known.
        coverage = blueprint_coverage(context_id)
    except Exception:
        owned, coverage = {}, {"characters": 0, "cached": 0, "missing": 0, "complete": False}
    # ...except for the products the user DECLARED, which are known one product at a time and do not
    # wait on the other characters' scopes — see BuildParams.prints_known.
    try:
        from app.industry.blueprints import declared_products
        declared = declared_products(owned)
    except Exception:
        declared = set()
    # Reaction formulas the account keeps in a hangar or container instead of a personal blueprint
    # list — counted from ENABLED stock and from the prints their real industry jobs were installed
    # on. Same cap, different evidence; see formula_print_floor for the precedence and the
    # de-duplication.
    stock_prints: dict = {}
    try:
        from app.features import feature_enabled_for
        if feature_enabled_for("industry_formulas_from_stock", context_id):
            from app.industry.blueprints import formula_print_floor
            stock_prints = formula_print_floor(context_id, owned)
    except Exception:
        stock_prints = {}

    snap = {"defaults": defaults, "owned": owned, "coverage": coverage, "declared": declared,
            "stock_prints": stock_prints,
            "skills": account_industry_time_mults(context_id, with_basis=True)}
    _ACCOUNT_CACHE[context_id] = (now, snap)
    return snap


def resolve_build_params(context_id: int, me_pct: float, te_pct: float,
                         system_id: int | None, facility_tax_pct: float | None,
                         max_build_hours: float = 0.0,
                         struct_material_pct: float = 0.0, struct_time_pct: float = 0.0,
                         marginal_pct: float | None = None, force_build: bool = False,
                         force_build_ids: list[int] | None = None,
                         margin_pct: float | None = None,
                         never_build_ids: list[int] | None = None) -> BuildParams:
    """Build the resolver's params, auto-deriving the build system + tax from the account's
    Reactions settings when the request didn't override them — so the caller needn't supply a
    system id or tax by hand."""
    snap = _account_snapshot(context_id)
    d_sid, d_tax, basis = snap["defaults"]
    sid = system_id if system_id is not None else d_sid
    tax = facility_tax_pct if facility_tax_pct is not None else d_tax
    if system_id is not None:
        basis = "request"          # the caller named a system; nothing was defaulted
    owned, coverage = snap["owned"], snap["coverage"]
    declared, stock_prints = snap["declared"], snap["stock_prints"]
    # The per-product aggregate is a runs-weighted figure over the WHOLE holding, not the best copy:
    # crediting a 20-run batch with the ME of a 5-run copy under-states its materials. The per-JOB
    # value comes off the copies themselves (BuildParams.me_te_for / build_tasks). This map is only
    # the fallback for a product with no copies to read.
    #
    # NOT cached with the rest: it takes the caller's me_pct/te_pct as the fallback, so it is a
    # function of the REQUEST as well as the account. It is pure CPU over data already in hand,
    # which is not what made this function slow.
    me_by_product = {p: blend_me_te(o.get("copies") or [o], None, (me_pct, te_pct))
                     for p, o in owned.items()}
    mfg_skill, rx_skill, skill_basis = snap["skills"]
    _marg = (MARGINAL_BUILD_PCT_OF_TOTAL if marginal_pct is None
             else max(0.0, min(25.0, float(marginal_pct))))
    return BuildParams(
        me_pct=me_pct, te_pct=te_pct,
        mfg_skill_time_mult=mfg_skill, rx_skill_time_mult=rx_skill, skill_time_basis=skill_basis,
        mfg_cost_index=fetch_system_cost_index(sid, "manufacturing"),
        rx_cost_index=fetch_system_cost_index(sid, "reaction"),
        facility_tax_pct=tax, me_by_product=me_by_product, owned=owned,
        blueprint_coverage=coverage, declared_prints=declared, stock_prints=stock_prints,
        build_system_id=sid, build_system_basis=basis,
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
        marginal_pct_of_total=(0.0 if force_build else _marg),
        # The floor is the DEFAULT policy's small-build half, not a second opinion the user cannot
        # reach. Asking for 0% says "build anything that saves anything at all", so keeping a 5m
        # floor that went on buying the long tail made the control lie about what it did. Dropped
        # whenever the user asks for zero — by the slider or by the checkbox; every other setting
        # keeps it, which is what still makes a cheap hull buy-and-assemble.
        min_saving_isk=(0.0 if (force_build or _marg <= 0.0) else MIN_BUILD_SAVING_ISK),
        force_build_ids=set(force_build_ids or ()),
        never_build_ids=set(never_build_ids or ()),
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
        names = {}
        groups = {}
        # group_id comes along for the ride: it's what decides whether a structure's rigs cover a
        # given job (app.industry.structures), and it is the ONLY taxonomy the SDE build imports —
        # there is no groups table, so no category and no tech level.
        for r in con.execute(
                f"SELECT type_id, name, group_id FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                tuple(ids)):
            names[r["type_id"]] = r["name"]
            groups[r["type_id"]] = r["group_id"]
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
                                  opts.force_build_ids, opts.margin_pct,
                                  # A target is never blacklisted out of its own build: you asked for
                                  # it. Only components can be forced onto the shopping list.
                                  [t for t in opts.never_build_ids
                                   if t not in {tid for tid, _q in targets}])
    # The account's standing reaction policy. Set on the resolved params rather than threaded
    # through resolve_build_params' positional list, same as job_sites and bp_acquire. `type_groups`
    # is what a produced type is matched to a category by, and it is the map already fetched above —
    # the SDE's only taxonomy, shared with rig routing.
    params.buy_all_reactions = bool(opts.buy_all_reactions)
    params.buy_reaction_categories = {str(k) for k in (opts.buy_reaction_categories or ())}
    # "Build everything" means everything, reactions included. The standing policy and this checkbox
    # were independent, so a user who ticked a box labelled *build everything* still had every
    # reaction bought at market — and, worse, silently: pricing a reaction at its Jita price is what
    # the parent's build cost is then compared against, so the components ABOVE it lose make-or-buy
    # too and their whole sub-chain leaves the plan. Measured on a Revelation: the policy took
    # Reinforced Carbon Fiber demand from ~26k to 6.4k, because buying the goo made Core Temperature
    # Regulator look dearer to build than to buy. The per-order override stays exactly as it was.
    params.build_reactions_anyway = bool(opts.build_reactions_anyway or opts.force_build)
    params.type_groups = groups
    # The reaction job-length ceiling, in hours. THE one flag gate for the feature, placed here
    # because every plan path resolves its params through this function: with the flag off the field
    # stays 0.0 and the scheduler is byte-for-byte the one that shipped before it existed. Days in,
    # hours out — days is the unit a builder says it in, hours is what the scheduler already thinks
    # in everywhere else.
    if opts.max_reaction_job_days:
        from app.features import feature_enabled_for
        if feature_enabled_for("industry_job_length_policy", ctx):
            params.max_reaction_job_hours = max(0.0, float(opts.max_reaction_job_days) * 24.0)
    # Ordering a reaction is the newer, more specific instruction — you asked for it, so the policy
    # does not get to buy it out of its own build. Same carve-out the blacklist gets above.
    params.reaction_policy_exempt_ids = {tid for tid, _q in targets}
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
        if info.get("kind") != "bpc":
            continue
        me_te = representative_me_te(info)
        if not me_te:
            continue
        # Recorded for EVERY type, owned or not: it is what a batch bigger than the copies you hold
        # is built off, so it has to be known even when the owned copies win the first runs.
        params.buy_me_te[tid] = me_te
        if tid not in params.me_by_product:
            params.me_by_product[tid] = me_te
            params.me_source[tid] = "contract"
    # Where an owned product's ME/TE came from. Two provenances, never blurred:
    #
    #   "owned"    — READ from `GET /characters/{id}/blueprints/`. A measurement of a print the
    #                account provably holds.
    #   "declared" — the user TYPED it (`pp_industry_blueprints`). Not a measurement, and reported
    #                as its own thing so the plan can say so; claiming ESI-grade knowledge for a
    #                number somebody entered is the one thing this feature must not do.
    #
    # A declaration outranks the ESI read for its product, and that is already settled upstream:
    # `owned_blueprints` REPLACES a declared product's copies rather than adding to them (see its
    # docstring for why item-level reconciliation is impossible). The justification for that
    # ordering belongs here too, because it is a policy and not a mechanism: ESI is measured truth
    # about a print the user really holds, but it can only ever see their PERSONAL hangar, and the
    # print a corp-hangar builder will actually install is one it structurally cannot see. So the
    # two are not competing descriptions of one print — the declaration is usually about a
    # DIFFERENT, better-researched print, and the whole reason the user bothered to type it is that
    # we were wrong. The per-order override still wins over both, unchanged: it names one order's
    # print, which is the more specific statement, and this one is account-level.
    for tid, own in params.owned.items():
        params.me_source.setdefault(tid, "declared" if own.get("source") == "manual" else "owned")
    # The user's own call comes last — they know which print they're really using.
    for key, val in (opts.me_te_overrides or {}).items():
        try:
            tid = int(key)
            me, te = float(val[0]), float(val[1])
        except Exception:
            continue
        params.me_by_product[tid] = (max(0.0, min(10.0, me)), max(0.0, min(20.0, te)))
        params.me_source[tid] = "override"

    # Per-job build site. Resolved LAST: it reads the resolved params (build system, tax, the flat
    # facility bonus) and hands back the routing every downstream number is then computed from.
    # `{}` when the flag is off or the account described no build structure — i.e. today's plan.
    from app.industry.routing import resolve_job_sites
    params.job_sites = resolve_job_sites(ctx, targets, mfg, rx, groups, params, opts.facility_id,
                                         opts.build_pins)

    pool = _slot_pool(ctx)
    pools = {
        "manufacturing": max(1, mfg_slots if mfg_slots is not None else pool["manufacturing_slots"]),
        "reaction": max(1, rx_slots if rx_slots is not None else pool["reaction_slots"]),
    }
    return PlanInputs(mfg=mfg, rx=rx, names=names, ids=id_list, prices=prices,
                      adjusted=adjusted, params=params, pools=pools)
