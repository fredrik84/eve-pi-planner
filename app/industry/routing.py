"""Which of the account's structures each job is installed in.

A builder who runs several structures does not run them interchangeably: the Standup M-Set rigs
are group-specific, so one is rigged for capital parts, another for the hull, a third for ammo.
The planner used to quote every job as if one structure's rigs applied to all of it, which
overstates ME/TE on any build that spans families — the normal case for a capital.

This module turns the structures the user has described (`pp_markets` build rows) into a per-job
decision: for each buildable type, the site whose rigs actually cover that product's SDE group
wins, and cost, materials, job time and the schedule all read that one decision through
`BuildParams.struct_mults_for` / `job_fee_rate`.

Three properties it has to keep:
  * **Nothing changes for a single-structure account.** One site, or none, routes every job to the
    same place with the same numbers as before. Narrowed rig families are opt-in; a rig with no
    families declared covers everything (see structures.covers).
  * **Routing can only improve on the flat facility.** The account's currently selected facility
    stays in the running as a candidate, so a job never comes out worse than the plan quotes today.
  * **Fees follow the routing.** Job installation cost is EIV × (system cost index + facility tax +
    SCC), and the index is per SYSTEM — a job routed to another structure is charged that
    structure's system, or the plan's ISK is describing a build that never happens.

There was a fourth thing here — `plan_moves`, which listed every station change the routing
implied — and it is gone on purpose. A builder installing jobs in two structures already knows the
parts have to travel; the list told them nothing they would act on, and the build page is short of
room for things that do. `build_sites` still names the structures used, which is what the checklist
needs to say WHERE to install each job.
"""
from app.industry.structures import BuildSite, route_job

FEATURE_KEY = "industry_rig_routing"


def _feature_on(context_id: int | None = None) -> bool:
    # Local import: app.features imports from app.esi, and this module is reached from the industry
    # router, so a module-level import risks a cycle (same reason skills.py does it this way).
    # Role-aware: a feature parked on `testers` has to actually work for a tester, or the rung is
    # decoration — see feature_enabled_for.
    from app.features import feature_enabled_for
    return feature_enabled_for(FEATURE_KEY, context_id)


def _sites_for(structures: list[dict], params, activity: str) -> list[BuildSite]:
    """The candidate sites for one activity: every configured structure that builds it, plus the
    account's flat selected facility as a floor (unless that facility IS one of the structures, in
    which case adding it back un-narrowed would hand every job the best-case bonus again and undo
    the entire point of this)."""
    from app.industry_cost import fetch_system_cost_index
    from app.industry.graph import REACTION_ME_REDUCTION

    sites: list[BuildSite] = []
    flag = "build_mfg" if activity == "manufacturing" else "build_rx"
    for s in structures:
        if not s.get(flag):
            continue
        me_rig = s["me_rig"] if activity == "manufacturing" else s["rx_me_rig"]
        te_rig = s["te_rig"] if activity == "manufacturing" else s["rx_te_rig"]
        me_fams = (s.get("me_rig_groups") if activity == "manufacturing"
                   else s.get("rx_me_rig_groups")) or ()
        te_fams = (s.get("te_rig_groups") if activity == "manufacturing"
                   else s.get("rx_te_rig_groups")) or ()
        sys_id = s.get("system_id") or params.build_system_id
        tax = s.get("facility_tax_pct")
        sites.append(BuildSite(
            key=f"s:{s['id']}", name=s["name"], activity=activity,
            hull=s.get("hull"), security=s.get("security"),
            me_rig=me_rig, te_rig=te_rig,
            me_families=tuple(me_fams), te_families=tuple(te_fams),
            system_id=sys_id,
            cost_index=fetch_system_cost_index(sys_id, activity),
            tax_pct=params.facility_tax_pct if tax is None else float(tax),
        ))
    if not sites:
        return []
    return sites + [_baseline(params, activity, REACTION_ME_REDUCTION)]


def _baseline(params, activity: str, rx_me_reduction: float) -> BuildSite:
    """The account's flat facility, as a candidate. Its bonuses are given rather than derived —
    for manufacturing they're whatever the facility control resolved to, for reactions they're the
    T1 reactor rig the engine has always assumed — so a plan can never be routed into a worse
    answer than it had before."""
    if activity == "manufacturing":
        flat = (round((1.0 - params.struct_material_mult) * 100.0, 2),
                round((1.0 - params.struct_time_mult) * 100.0, 2))
        ci = params.mfg_cost_index
    else:
        flat = (round(rx_me_reduction * 100.0, 2), 0.0)
        ci = params.rx_cost_index
    return BuildSite(key="account", name="Selected facility", activity=activity,
                     system_id=params.build_system_id, cost_index=ci,
                     tax_pct=params.facility_tax_pct, flat=flat)


def resolve_job_sites(context_id: int, targets: list[tuple[int, int]], mfg: dict, rx: dict,
                      groups: dict[int, int], params, facility_id: str | None = None) -> dict:
    """type_id -> the site that job is built in. `{}` (route nothing, behave exactly as today)
    whenever the flag is off or the account has described no build structure.

    Walked from each target DOWN through its recipe so a component can prefer the site its
    CONSUMER was routed to: on a tie — or a difference inside `ROUTE_NOISE_PCT` — the parts stay
    where they're going to be used instead of earning a haul for a rounding error.
    """
    if not _feature_on(context_id):
        return {}
    try:
        from app.markets import build_structures
        structures = build_structures(context_id)
    except Exception:
        return {}
    if not structures:
        return {}
    # The selected facility is one of these structures: it is already a candidate, correctly
    # narrowed by its own rigs, so the flat un-narrowed copy of it must not shadow that.
    chosen_key = (facility_id or "").strip()
    by_activity = {a: _sites_for(structures, params, a) for a in ("manufacturing", "reaction")}
    if chosen_key.startswith("s:"):
        for act, sites in by_activity.items():
            if any(s.key == chosen_key for s in sites):
                by_activity[act] = [s for s in sites if s.key != "account"]
    if not any(by_activity.values()):
        return {}

    chosen: dict[int, dict] = {}

    def walk(tid: int, prefer: str | None, stack: frozenset):
        if tid in stack:
            return
        recipe = mfg.get(tid) or rx.get(tid)
        if recipe is None:
            return                      # bought / raw: no job, no site
        activity = "manufacturing" if tid in mfg else "reaction"
        site = chosen.get(tid)
        if site is None:
            sites = by_activity.get(activity) or []
            site = route_job(sites, groups.get(tid), prefer=prefer) if sites else None
            if site is None:
                return
            chosen[tid] = site
        for inp in recipe["inputs"]:
            walk(inp["type_id"], site["key"], stack | {tid})

    for tid, _qty in targets:
        walk(tid, None, frozenset())
    return chosen
