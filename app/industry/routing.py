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

**A pin overrides the scoring, never the sanity.** Everything above is inferred from rig coverage,
and a capital builder who runs PARTS in one structure and the HULL in another wants to say so
rather than hope the inference lands there. So an account can pin a rig FAMILY —
`structures.RIG_FAMILIES`, the same taxonomy the rigs are described in, not a second category list
— to one structure, and every job whose product falls in that family is installed there. The pin is
consulted BEFORE the scoring, but it can only ever choose among the candidates that were already
legal for that job's ACTIVITY: a pin naming a structure that doesn't run this activity (or that has
been deleted since) is not honoured, the job falls back to the automatic routing, and the plan says
the pin was not applied (`pin_notes`). A pin that quietly produced a reaction site for a
manufacturing job would be a broken plan, and a pin that silently did nothing would be worse than
no pin at all.

**A pin says WHERE, `covers()` still says WHAT BONUS.** The two rules must not fight. A Raitaru
cannot fit a capital-ship rig, and `fittable_families` says so — but a builder may still legitimately
pin capital ships to that Raitaru: they CAN build there, they just earn no rig bonus for it. So the
pin is never filtered by `fittable_families`; it restricts the candidate list to the pinned site and
that site's own `bonus_for` then answers honestly (role bonus only, where the rig cannot apply).

There was a fourth thing here — `plan_moves`, which listed every station change the routing
implied — and it is gone on purpose. A builder installing jobs in two structures already knows the
parts have to travel; the list told them nothing they would act on, and the build page is short of
room for things that do. `build_sites` still names the structures used, which is what the checklist
needs to say WHERE to install each job.
"""
from app.industry.structures import (
    RIG_FAMILIES, BuildSite, family_for_group, route_job,
)

# The pin rides on the ROUTING flag rather than one of its own: `resolve_job_sites` returns `{}`
# with routing off, so there is nothing for a pin to attach to and a second flag would only create
# a state (pins on, routing off) that cannot do anything. Same audience, same feature.
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


def clean_pins(pins) -> dict[str, str]:
    """The stored pin map, normalised: `{rig family key: "s:<pp_markets row id>"}`.

    Unknown family keys are dropped against the registry — a key left behind by a rename must not
    keep half-applying — and so is a blank target. Everything else is left exactly as saved: whether
    that structure still exists, and whether it can run the job, is decided per JOB against the real
    candidate list, not guessed here.
    """
    out = {}
    for key, val in (pins or {}).items():
        key = str(key)
        val = str(val or "").strip()
        if key in RIG_FAMILIES and val:
            out[key] = val
    return out


def _pin_note(family: str, site_key: str, reason: str) -> dict:
    return {"family": family, "label": RIG_FAMILIES[family]["label"],
            "site_key": site_key, "reason": reason}


def _pinned_site(sites: list[BuildSite], group_id: int | None, activity: str,
                 pins: dict[str, str], unapplied: dict) -> dict | None:
    """The site this job is PINNED to, already costed — or None to fall through to the scoring.

    Two rules, in this order. The family is resolved for THIS ACTIVITY, so a manufacturing pin is
    never even consulted for a reaction; and the pinned key has to be one of the candidates already
    built for that activity, so a structure that was deleted, or that doesn't run this activity, is
    recorded as unapplied and the job routes automatically instead of failing or pretending.

    The bonus is not asserted here: `route_job` over the single pinned candidate returns the same
    shape any routed job has, with that site's own `bonus_for` — so pinning capital ships to a
    Raitaru puts the jobs there and honestly pays only the hull role bonus for them.
    """
    fam = family_for_group(group_id, activity)
    if not fam:
        return None
    key = pins.get(fam)
    if not key:
        return None
    for s in sites:
        if s.key == key:
            site = route_job([s], group_id)
            if site is not None:
                site["pinned"] = fam
            return site
    unapplied.setdefault(fam, _pin_note(fam, key, "unavailable"))
    return None


def resolve_job_sites(context_id: int, targets: list[tuple[int, int]], mfg: dict, rx: dict,
                      groups: dict[int, int], params, facility_id: str | None = None,
                      pins: dict | None = None) -> dict:
    """type_id -> the site that job is built in. `{}` (route nothing, behave exactly as today)
    whenever the flag is off or the account has described no build structure.

    Walked from each target DOWN through its recipe so a component can prefer the site its
    CONSUMER was routed to: on a tie — or a difference inside `ROUTE_NOISE_PCT` — the parts stay
    where they're going to be used instead of earning a haul for a rounding error.

    `pins` (family key -> site key) is checked first and wins over the scoring for jobs in that
    family; everything else routes exactly as it did, consumer tie-break included. Pins that could
    NOT be honoured land in `params.pin_notes` for the plan to report.
    """
    pins = clean_pins(pins)
    params.pin_notes = []
    if not _feature_on(context_id):
        # Nothing is routed at all with the flag off, so a pin has nothing to attach to. Said out
        # loud rather than left for the user to infer from a plan that quietly ignored them.
        params.pin_notes = [_pin_note(f, k, "routing_off") for f, k in sorted(pins.items())]
        return {}
    unapplied: dict[str, dict] = {}
    try:
        from app.markets import build_structures
        structures = build_structures(context_id)
    except Exception:
        return {}                       # the whole routing is degraded; a pin note would misdescribe why
    if not structures:
        params.pin_notes = [_pin_note(f, k, "unavailable") for f, k in sorted(pins.items())]
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
        params.pin_notes = [_pin_note(f, k, "unavailable") for f, k in sorted(pins.items())]
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
            if not sites:
                return
            # The pin decides first — and only among sites already legal for this activity. Falling
            # through to the scoring is what an unhonourable pin does; `prefer` is untouched by it,
            # so everything NOT pinned still follows its consumer exactly as before.
            site = (_pinned_site(sites, groups.get(tid), activity, pins, unapplied) if pins else None)
            if site is None:
                site = route_job(sites, groups.get(tid), prefer=prefer)
            if site is None:
                return
            chosen[tid] = site
        for inp in recipe["inputs"]:
            walk(inp["type_id"], site["key"], stack | {tid})

    for tid, _qty in targets:
        walk(tid, None, frozenset())
    # Only pins this build actually needed are reported: a pin on a family nothing in the plan
    # produces is not a problem, and a notice nobody would act on is not worth its line.
    params.pin_notes = [unapplied[f] for f in sorted(unapplied)]
    return chosen
