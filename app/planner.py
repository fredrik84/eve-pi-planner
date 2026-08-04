"""
Planetary industry planning — plan orchestration.

`_run_plan` and the shared plan math (P1 requirement tracing, factory rate/refill cadence,
per-character footprints). The pieces it drives live in siblings, each imported one way only so
the chain stays acyclic:

    planner_algo  <-  planner  <-  planner_advisor  <-  planner_dashboard

`planner_algo` is the assignment algorithm, `planner_advisor` the "what should I change?"
endpoints, `planner_dashboard` the overview. `planner_store` still holds the CRUD half, and
`planner_models` / `planner_serialization` / `planner_recommendations` are unchanged. This file
was ~3,900 lines carrying all of it at once.
"""
import logging
import time as _time
from fractions import Fraction
from math import gcd, ceil

from fastapi import APIRouter, Cookie, Depends, Request

from app.sde import load_pi_data, get_connection
from app.market import fetch_prices
from app.esi import require_context
from app.planner_models import PlanRequest
from app.planner_serialization import _norm_dist_mode, _norm_split_mode
# The CRUD half (plan-config, shares, profiles, snapshots, colony flags) lives in
# planner_store; the dependency runs one way only, so there is no cycle.
from app.planner_store import ensure_plan_tables
from app.planner_recommendations import _P0_PLANET_TYPES, _p0_col, _blend_value
# The assignment algorithm itself lives in planner_algo; this module orchestrates it. One-way
# again — planner_algo imports nothing from here, so `_run_plan` can be read as the sequence of
# steps it is, without the 1,600 lines of how each step works in between.
from app.planner_algo import (
    _PLANET_P0_PER_DAY, _actual_p0_per_day_by_p0, _assign_factory_planets_to_chars,
    _build_char_list, _build_p1_info, _build_p1_info_raw, _compute_slot_budget,
    _consolidate_split_extractors, _density_estimate, _ext_actual_p0_per_day,
    _ext_leg_qualities, _factory_candidates, _fetch_planets_and_recs,
    _load_char_planet_config, _pick_factory_system, _reinvest_freed_planets,
    _run_extractor_pipeline, _set_computed_ext_cap,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ── Planning core helpers ─────────────────────────────────────────────────────


def _build_p0_p1_maps(pi_data):
    p0_to_p1: dict[int, int] = {}
    for tid, sch in pi_data["schematics"].items():
        if pi_data["types"].get(tid, {}).get("pi_tier") == 1:
            for inp in sch["inputs"]:
                if pi_data["types"].get(inp["type_id"], {}).get("pi_tier") == 0:
                    p0_to_p1[inp["type_id"]] = tid
    return p0_to_p1, {v: k for k, v in p0_to_p1.items()}


def _trace_p1_frac(tid: int, mult: Fraction, types: dict, schematics: dict) -> dict[int, Fraction]:
    """Recursively roll a type down to its tier-1 (P1) inputs, scaled by mult. A tier-1
    input returns itself; tier-0/unknown returns {}. Shared by _compute_p1_reqs (which
    then reduces to an integer ratio) and _compute_p1_fracs (which just floats the result)."""
    tier = types.get(tid, {}).get("pi_tier")
    if tier == 1:
        return {tid: mult}
    if not tier:
        return {}
    sch = schematics.get(tid)
    if not sch:
        return {}
    out_q = sch["output_qty"]
    result: dict[int, Fraction] = {}
    for inp in sch["inputs"]:
        sub = _trace_p1_frac(inp["type_id"], Fraction(inp["quantity"], out_q) * mult, types, schematics)
        for k, v in sub.items():
            result[k] = result.get(k, Fraction(0)) + v
    return result


def _compute_p1_reqs(target_type_id: int, pi_data) -> dict[int, int]:
    schematics, types = pi_data["schematics"], pi_data["types"]
    raw = _trace_p1_frac(target_type_id, Fraction(1), types, schematics)
    if not raw:
        return {}
    lcm_d = 1
    for f in raw.values():
        lcm_d = lcm_d * f.denominator // gcd(lcm_d, f.denominator)
    ints = {k: int(v * lcm_d) for k, v in raw.items()}
    g = list(ints.values())[0]
    for v in ints.values():
        g = gcd(g, v)
    return {k: v // g for k, v in ints.items()}


def _compute_p1_fracs(target_type_id: int, pi_data) -> dict[int, float]:
    """P1 units required per 1 unit of final product."""
    schematics, types = pi_data["schematics"], pi_data["types"]
    return {k: float(v) for k, v in _trace_p1_frac(target_type_id, Fraction(1), types, schematics).items()}


# Factory P1 input buffer model (kept here so _run_plan and /api/my-setup-plan agree —
# the 0.38→0.19 m³ fix would have been a one-liner if these had always been shared).
_P1_VOLUME = 0.19            # m³ per P1 unit (verified in-game)
_FACTORY_LAUNCHPADS = 3      # input-buffer launchpads assumed per factory (30,000 m³)


def _effective_fph(type_id: int, pi_data, override: float | None = None) -> float:
    """Per-factory output rate (units/hr). A P4 factory makes ~0.5/hr over its full P2→P3→P4
    chain; the raw SDE rate only reflects the final step and over-counts P4 ~2×. P1–P3 use the
    SDE rate. A positive override always wins."""
    if override is not None and override > 0:
        return float(override)
    if pi_data["types"].get(type_id, {}).get("pi_tier") == 4:
        return 0.5
    sch = pi_data["schematics"].get(type_id) or {}
    ct = sch.get("cycle_time") or 3600
    return sch.get("output_qty", 1) * 3600.0 / ct


def _factory_refill_hours(products_per_day: float, p1_fracs: dict, factories: int) -> float | None:
    """Hours until a factory's 3-launchpad (30,000 m³) P1 input buffer empties at full
    consumption. None if there are no factories / no consumption."""
    if not factories:
        return None
    p1_m3_per_factory_day = products_per_day * sum(p1_fracs.values()) * _P1_VOLUME / factories
    if p1_m3_per_factory_day <= 0:
        return None
    return round((_FACTORY_LAUNCHPADS * 10_000) / (p1_m3_per_factory_day / 24), 1)


def project_factory_pad(product_tid: int, inputs: list, base_product: float, t0, now: float | None = None) -> float:
    """Projected FINAL product in a factory's launchpad NOW = checkpoint amount + what it made since the
    checkpoint, at the effective product rate (a P4 is throttled to 0.5/hr), capped at when the imported
    P1 would run dry (the factory stops feeding then). Factory planets have no extractor sim_state — the
    on-planet P1→P2→P3→P4 chain can't be line-simmed (the intermediates don't accrue in the launchpad) —
    so without this the Characters tab freezes on a days-old ESI checkpoint. Mirrors the dashboard's
    'In pads now' projection so the two agree."""
    import time as _t
    if now is None:
        now = _t.time()
    pi = load_pi_data()
    fracs = _compute_p1_fracs(product_tid, pi)
    rate_hr = _effective_fph(product_tid, pi)          # products/hr (P4 → 0.5)
    if not fracs or rate_hr <= 0 or not t0:
        return base_product
    elapsed_h = max(0.0, (now - t0) / 3600.0)
    snap = {it.get("type_id"): (it.get("amount", 0) or 0) for it in (inputs or [])}
    tte_h = None                                        # hours until the first P1 input runs dry
    for pid, frac in fracs.items():
        need_per_h = rate_hr * frac                     # P1 consumed/hr (frac = P1 per product)
        if need_per_h <= 0:
            continue
        h = snap.get(pid, 0) / need_per_h
        tte_h = h if tte_h is None else min(tte_h, h)
    fed_h = min(elapsed_h, tte_h) if tte_h is not None else elapsed_h
    return base_product + rate_hr * fed_h




def _char_footprint(con, context_id: int):
    """Per real character: `foot` = systems the char operates in, `occ` = (system, planet_num) already
    colonised. The reachability + no-double-book basis for 'where could this char redeploy'."""
    rows = con.execute("""
        SELECT cp.character_id AS cid, s.name AS system, cp.planet_num AS pn
        FROM pp_char_planets cp
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        JOIN pp_characters c ON c.character_id = cp.character_id
        WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0
    """, (context_id,)).fetchall()
    foot: dict[int, set] = {}
    occ: dict[int, set] = {}
    for r in rows:
        if not r["system"]:
            continue
        foot.setdefault(r["cid"], set()).add(r["system"])
        occ.setdefault(r["cid"], set()).add((r["system"], r["pn"]))
    return foot, occ


def _p0_available_by_char_multi(con, p0_names, foot: dict, occ: dict, blend_on: bool) -> dict:
    """Batch form of `_p0_available_by_char`: ONE pp_planets scan (+ one pp_planet_yield_avg query)
    for a whole set of P0 names, returning `{p0_name: {cid: [avail...]}}`. The redeploy/placements
    paths involve several P0s at once, so the per-P0 version was an N+1 (a full Planet-DB scan each);
    this fetches every needed richness column in one query and partitions per P0/character in Python."""
    cols: dict[str, str] = {}
    for p0 in p0_names:
        c = _p0_col(p0) if p0 else None
        if c:
            cols[p0] = c
    if not cols:
        return {}
    distinct_cols = list(dict.fromkeys(cols.values()))   # dedupe, preserve order
    alias = {c: f"c{i}" for i, c in enumerate(distinct_cols)}
    select_cols = ", ".join(f'"{c}" AS {alias[c]}' for c in distinct_cols)
    where_any = " OR ".join(f'"{c}" > 0' for c in distinct_cols)
    planets = con.execute(
        f"SELECT system, planet_num, planet_type, {select_cols} FROM pp_planets WHERE {where_any}"
    ).fetchall()
    # pp_planet_yield_avg.p0_name stores the COLUMN name (not the display P0 name) — same key the
    # per-P0 version used. One IN() query covers every column we're about to blend.
    measured_by_col: dict[str, dict] = {}
    if blend_on and distinct_cols:
        try:
            qs = ",".join("?" * len(distinct_cols))
            for m in con.execute(
                f"SELECT system, planet_num, p0_name, measured_pct, sample_count "
                f"FROM pp_planet_yield_avg WHERE p0_name IN ({qs})", distinct_cols).fetchall():
                measured_by_col.setdefault(m["p0_name"], {})[(m["system"], m["planet_num"])] = \
                    {"pct": m["measured_pct"], "n": m["sample_count"]}
        except Exception:
            pass
    out: dict[str, dict] = {}
    for p0, col in cols.items():
        a = alias[col]
        valid_types = _P0_PLANET_TYPES.get(p0, [])
        measured = measured_by_col.get(col, {})
        rows = [p for p in planets
                if (p[a] or 0) > 0 and (not valid_types or p["planet_type"] in valid_types)]
        by_char: dict[int, list] = {}
        for cid, systems in foot.items():
            occ_c = occ.get(cid, set())
            avail = [{"system": p["system"], "planet_num": p["planet_num"], "planet_type": p["planet_type"],
                      "richness": round(_blend_value(p[a] or 0, measured.get((p["system"], p["planet_num"]))))}
                     for p in rows
                     if p["system"] in systems and (p["system"], p["planet_num"]) not in occ_c]
            avail.sort(key=lambda x: -x["richness"])
            if avail:
                by_char[cid] = avail
        out[p0] = by_char
    return out


def _p0_available_by_char(con, p0_name: str | None, foot: dict, occ: dict, blend_on: bool) -> dict:
    """Available same-P0 Planet-DB planets per character (cid) — carrying `p0_name`, in a system the
    char operates in, not already colonised by them, richness-sorted (measured-yield blended when the
    flag is on). The 'redeploy this colony to a fresh planet' candidate list. cid-keyed (int). Thin
    single-P0 wrapper over `_p0_available_by_char_multi`."""
    return _p0_available_by_char_multi(
        con, [p0_name] if p0_name else [], foot, occ, blend_on).get(p0_name, {})


_PI_DECAY_K = 0.012   # per hour — matches analysis.js _EXT_DECAY_K (extraction front-loads then decays)


def _pi_ext_eff(days: float) -> float:
    """Average extraction as a fraction of peak over a program of `days` (per-cycle ≈ peak/(1+k·t))."""
    from math import log
    h = max(0.0, days or 0) * 24
    return log(1 + _PI_DECAY_K * h) / (_PI_DECAY_K * h) if h > 0 else 1.0


def pi_lifetime_estimate(context_id: int | None = None) -> dict:
    """Estimated value of the P1 you refined from measured extraction, summed over the recorded
    programs. NOTE pp_colony_yield keeps only the last ~10 programs per colony (bounded storage), so
    this is a recent-history ESTIMATE, not a true all-time total. Per-account when context_id is set;
    service-wide across every account when None. Each program's estimated total P0 = peak_p0_day ×
    program-days × decay-average; valued at the P0's refined P1 (Jita sell). PI has no ISK input cost,
    so this is both 'turnover' and 'net'."""
    con = get_connection()
    try:
        if context_id is not None:
            rows = con.execute(
                "SELECT y.p0_type_id, y.peak_day, y.prog_days, y.install_ts FROM pp_colony_yield y "
                "JOIN pp_characters c ON c.character_id = y.character_id WHERE c.context_id=?",
                (context_id,)).fetchall()
        else:
            rows = con.execute("SELECT p0_type_id, peak_day, prog_days, install_ts FROM pp_colony_yield").fetchall()
    except Exception:
        return {"value": 0.0, "programs": 0, "since": None}
    finally:
        con.close()
    if not rows:
        return {"value": 0.0, "programs": 0, "since": None}
    pi = load_pi_data()
    types, schematics = pi["types"], pi["schematics"]
    # p0_type_id -> (p1_type_id, P1-per-P0) from the tier-1 (single-input P0→P1) basic schematics.
    p0_to_p1: dict[int, tuple] = {}
    for out_id, sch in schematics.items():
        if types.get(out_id, {}).get("pi_tier") == 1 and len(sch.get("inputs", [])) == 1:
            inp = sch["inputs"][0]
            if inp.get("quantity"):
                p0_to_p1[inp["type_id"]] = (out_id, sch["output_qty"] / inp["quantity"])
    p0_totals: dict[int, float] = {}
    since = None
    for r in rows:
        d = r["prog_days"] or 0
        p0_totals[r["p0_type_id"]] = p0_totals.get(r["p0_type_id"], 0.0) + (r["peak_day"] or 0) * d * _pi_ext_eff(d)
        its = r["install_ts"]
        if its and (since is None or its < since):
            since = its
    p1_ids = list({p0_to_p1[p0][0] for p0 in p0_totals if p0 in p0_to_p1})
    prices = fetch_prices(p1_ids) if p1_ids else {}
    value = 0.0
    for p0, tot in p0_totals.items():
        if p0 in p0_to_p1:
            p1_id, ratio = p0_to_p1[p0]
            value += tot * ratio * (prices.get(p1_id, 0.0) or 0.0)
    return {"value": round(value, 2), "programs": len(rows), "since": since}


@router.get("/api/pi-lifetime")
def pi_lifetime(context_id: int = Depends(require_context)):
    """This account's estimated PI produced value (see pi_lifetime_estimate) — recent-history
    estimate, PI has no ISK input cost so value ≈ turnover ≈ net."""
    return pi_lifetime_estimate(context_id)


# ── Main plan runner ──────────────────────────────────────────────────────────

def _run_plan(req: PlanRequest, context_id: int) -> dict:
    pi_data = load_pi_data()
    types, schematics = pi_data["types"], pi_data["schematics"]

    p1_reqs = _compute_p1_reqs(req.type_id, pi_data)
    if not p1_reqs:
        return {"error": "No schematic chain found for this product"}

    _, p1_to_p0 = _build_p0_p1_maps(pi_data)
    sch = schematics.get(req.type_id, {})
    cycle_time = sch.get("cycle_time", 3600)
    output_qty = sch.get("output_qty", 1)
    p1_fracs = _compute_p1_fracs(req.type_id, pi_data)
    sell_price = fetch_prices([req.type_id]).get(req.type_id, 0.0)

    ensure_plan_tables()
    con = get_connection()

    char_rows, planet_rows, has_system_name, config_map = _load_char_planet_config(
        con, context_id, req.type_id)

    sorted_p1, p1_info_raw, all_p0_names = _build_p1_info_raw(p1_reqs, p1_to_p0, types)

    _t0_recs = _time.monotonic()
    p0_planet_lists, p0_planet_lists_global, best_ptypes, sys_recs = _fetch_planets_and_recs(
        con, all_p0_names, req, types, p1_info_raw)
    log.info("plan.fetch_planets_and_recs in %.1fms", (_time.monotonic() - _t0_recs) * 1000)

    fac_db_planets, factory_system_options, sys_fac_capacity = _factory_candidates(
        con, req, only_bt=True)
    for rec in sys_recs:
        rec["factory_capacity"] = {s: sys_fac_capacity.get(s, 0) for s in rec["systems_needed"]}

    # Real per-planet diameter (pp_planets.diameter) keyed by (system, planet_num). Used to size each
    # extractor colony's exported template to its ACTUAL planet — the link power grid scales with
    # radius, so the planet-type default (Gas Ø40000) dropped a basic on the many smaller real planets.
    # Guarded: a pre-populate install without the column just yields an empty map → type-default fallback.
    diam_by_planet: dict[tuple, float] = {}
    try:
        for r in con.execute("SELECT system, planet_num, diameter FROM pp_planets WHERE diameter IS NOT NULL"):
            diam_by_planet[(r["system"], r["planet_num"])] = r["diameter"]
    except Exception:
        pass

    con.close()

    char_planets: dict[int, list] = {}
    for p in planet_rows:
        char_planets.setdefault(p["character_id"], []).append(dict(p))

    p1_info = _build_p1_info(p1_info_raw, best_ptypes, types)
    char_list = _build_char_list(char_rows, config_map, char_planets, with_ccu=True)

    # Factory planet capacity: max Barren/Temperate in the best candidate factory system.
    # Used to cap per-character factory shares so the planner doesn't assign more factories
    # to a character than there are physical planets in the factory system.
    _sys_fac_pre: dict[str, int] = {}
    for _p in fac_db_planets:
        _sys_fac_pre[_p["system"]] = _sys_fac_pre.get(_p["system"], 0) + 1
    if req.factory_system and req.factory_system in _sys_fac_pre:
        _per_char_fac_cap = _sys_fac_pre[req.factory_system]
    elif _sys_fac_pre:
        _per_char_fac_cap = max(_sys_fac_pre.values())
    else:
        _per_char_fac_cap = None

    # Effective per-factory output rate (units/hr) — see _effective_fph (P4 → 0.5/hr to avoid
    # the SDE's ~2× P4 over-count; SDE rate for P1–P3; user override wins).
    effective_fph = _effective_fph(req.type_id, pi_data, req.factory_output_per_hour)

    # Compute slot budget
    ext_slots, factories, factory_shares, auto_mode, p0_per_factory_day, factories_unbudgeted = _compute_slot_budget(
        char_list, req.overproduction_pct, effective_fph,
        cycle_time, output_qty, p1_fracs, _per_char_fac_cap,
        preferred_cids=req.factory_character_ids,
    )

    # Set computed_ext_cap per character, then clamp ext_slots to the real extractor
    # capacity. The equilibrium formula can over-count when factory-only chars
    # (extractor_limit=0) have idle slots beyond their factory cap — those slots can be
    # neither factories nor extractors, so generating demand for them leaves them unplaceable.
    ext_slots = min(ext_slots, _set_computed_ext_cap(char_list, factory_shares, auto_mode))

    prod_per_factory_day = effective_fph * 24
    products_per_day = round(prod_per_factory_day * factories)
    p0_per_day = round(sum(frac * products_per_day * 150 for frac in p1_fracs.values()))
    isk_per_day = round(products_per_day * sell_price, 2)

    # Refill cadence: factory planets import P1 into launchpads (matching the Factory Layout
    # templates). See _factory_refill_hours (0.19 m³/unit, 3-launchpad buffer).
    total_p1_per_day = products_per_day * sum(p1_fracs.values())
    p1_m3_per_factory_day = (total_p1_per_day * _P1_VOLUME / factories) if factories else 0.0
    factory_refill_hours = _factory_refill_hours(products_per_day, p1_fracs, factories)
    needed_at_baseline = ceil(p0_per_day / 48_000) if p0_per_day > 0 else sum(q for _, q in sorted_p1)

    has_planet_db = any(v for v in p0_planet_lists.values())

    # Factory chars that need ALL of the system's B/T planets for their factory share
    # must keep those planets free — their extractor slots avoid B/T. Chars with spare
    # B/T capacity (share < available B/T) may still extract on B/T (e.g. the lone
    # Autotrophs planet) without starving their factories.
    _factory_avoid: set[tuple] | None = None
    _factory_avoid_cids: set[int] | None = None
    if auto_mode and factory_shares and _per_char_fac_cap:
        _best_fac_sys_est = (
            req.factory_system if req.factory_system and req.factory_system in _sys_fac_pre
            else (max(_sys_fac_pre, key=lambda s: _sys_fac_pre[s]) if _sys_fac_pre else None)
        )
        if _best_fac_sys_est:
            _factory_avoid = {
                (p["system"], p["planet_num"])
                for p in fac_db_planets
                if p["system"] == _best_fac_sys_est
            }
            _factory_avoid_cids = {
                cid for cid, share in factory_shares.items()
                if share >= _per_char_fac_cap
            }

    # Distribution method (user-selectable): "stability" gives thinner-deposit resources more
    # extractors so production lands in the recipe ratio (less leftover P1); "need" is the
    # original need-proportional split. density_est=None → _build_need_list uses pure need.
    density_est = (_density_estimate(p1_info, p0_planet_lists, ext_slots, has_planet_db)
                   if _norm_dist_mode(req.distribution_mode) == "stability" else None)

    _t0_pipeline = _time.monotonic()
    assignments, remaining, char_nonfac = _run_extractor_pipeline(
        req, char_list, p1_info, ext_slots, needed_at_baseline,
        p0_planet_lists, p0_planet_lists_global, has_planet_db, has_system_name,
        auto_mode, factory_avoid_cids=_factory_avoid_cids, factory_avoid=_factory_avoid,
        density_est=density_est, reusable_type_ids={req.type_id},
    )
    log.info("plan.extractor_pipeline in %.1fms", (_time.monotonic() - _t0_pipeline) * 1000)

    # Pick best factory system
    sys_fac_count: dict[str, int] = {}
    for p in fac_db_planets:
        sys_fac_count[p["system"]] = sys_fac_count.get(p["system"], 0) + 1
    best_fac_system = _pick_factory_system(req, sys_fac_count)

    _assign_factory_planets_to_chars(
        assignments, char_list, factory_shares, auto_mode,
        fac_db_planets, best_fac_system, char_nonfac, req, has_system_name,
    )

    # Optional split-extraction consolidation (opt-in via split_mode).
    split_mode = _norm_split_mode(req.split_mode)
    split_planets = planets_saved = 0
    if split_mode != "off":
        _total_rel = sum(i["relative_qty"] for i in p1_info) or 1
        # True baseline planet-units needed per P0 = factory P0 consumption / a full planet's
        # daily output (48k/cycle × 24). p0_per_day is what the factories actually eat (NOT the
        # over-extracted amount), so the difference vs placed planets is the reclaimable slack.
        p0_need_pu: dict[str, float] = {}
        for i in p1_info:
            p0_need_pu[i["p0_name"]] = (
                p0_need_pu.get(i["p0_name"], 0.0)
                + (p0_per_day * i["relative_qty"] / _total_rel) / _PLANET_P0_PER_DAY)
        split_planets, planets_saved = _consolidate_split_extractors(
            assignments, p0_need_pu, p0_planet_lists, split_mode)
        if planets_saved > 0:  # always reinvest freed planets into more production
            added_fac, _added_ext = _reinvest_freed_planets(
                assignments, p1_info, p0_planet_lists, fac_db_planets,
                best_fac_system, ext_slots, factories)
            if added_fac:
                factories += added_fac
                products_per_day = round(prod_per_factory_day * factories)
                p0_per_day = round(sum(frac * products_per_day * 150 for frac in p1_fracs.values()))
                isk_per_day = round(products_per_day * sell_price, 2)
                total_p1_per_day = products_per_day * sum(p1_fracs.values())
                p1_m3_per_factory_day = (total_p1_per_day * _P1_VOLUME / factories) if factories else 0.0
                factory_refill_hours = _factory_refill_hours(products_per_day, p1_fracs, factories)

    all_assignments = sorted(assignments, key=lambda a: a["character_name"].lower())
    # Tag each pinned extractor with its real planet diameter so the PI-template export can size the
    # basic-factory count to the actual planet (see diam_by_planet). Only pinned slots have a planet.
    if diam_by_planet:
        for a in all_assignments:
            for e in a["extractors"]:
                d = diam_by_planet.get((e.get("system"), e.get("planet_num")))
                if d:
                    e["diameter"] = d
    total_extractors = sum(len(a["extractors"]) for a in all_assignments)
    total_factory_planets = sum(a["factory_planets"] for a in all_assignments)

    # Placement is the ground truth: a factory the budget asked for but that no character could
    # put on a planet (its planets all taken by extractors, or the system ran out of allowed
    # types) does not produce anything. Re-derive every output stat from the factories that
    # actually got a planet, so products/day can't describe a colony the plan never placed.
    if total_factory_planets < factories:
        factories_unbudgeted += factories - total_factory_planets
        factories = total_factory_planets
        products_per_day = round(prod_per_factory_day * factories)
        p0_per_day = round(sum(frac * products_per_day * 150 for frac in p1_fracs.values()))
        isk_per_day = round(products_per_day * sell_price, 2)
        total_p1_per_day = products_per_day * sum(p1_fracs.values())
        p1_m3_per_factory_day = (total_p1_per_day * _P1_VOLUME / factories) if factories else 0.0
        factory_refill_hours = _factory_refill_hours(products_per_day, p1_fracs, factories)

    # P1 delivery split: every factory planet makes the final product and imports its full P1
    # set, so each P1 splits EVENLY across the placed factory planets. `share` lets the UI turn
    # a pasted P1 stack into whole-unit amounts to drop at each factory.
    _fac_list = [f for a in all_assignments for f in a.get("factory_assignments", [])
                 if not f.get("unplaced")]
    _nfac = len(_fac_list)
    if _nfac:
        _prod_name = types.get(req.type_id, {}).get("name", "?")
        _p1_in = sorted(
            ({"p1_type_id": pid, "p1_name": types.get(pid, {}).get("name", "?"),
              "share": 1.0 / _nfac, "share_pct": round(100.0 / _nfac)}
             for pid in p1_fracs),
            key=lambda x: x["p1_name"])
        for f in _fac_list:
            f.setdefault("product", {"type_id": req.type_id, "name": _prod_name})
            f["p1_inputs"] = [dict(p) for p in _p1_in]

    # Stat aggregation expands split planets into their two legs (each leg = heads × quality).
    quality_vals = [q for a in all_assignments for q in _ext_leg_qualities(a["extractors"])]
    avg_quality_pct = round(sum(quality_vals) / len(quality_vals)) if quality_vals else None
    avg_p0_per_cycle = round(avg_quality_pct / 100 * 48000) if avg_quality_pct else None
    required_avg_p0_per_cycle = (
        round(p0_per_day / total_extractors / 24) if total_extractors else None
    )
    _baseline_p0_per_day = total_extractors * 48_000 * 24
    overproduction_pct = round((_baseline_p0_per_day / p0_per_day - 1) * 100) if p0_per_day > 0 else 0
    _asgn_cc = lambda a: int(a.get("effective_ccu") or a.get("ccu") or 5)  # CC for the basics cap
    _nost = bool(getattr(req, "extractor_no_storage", False))
    _actual_p0_per_day = sum(
        _ext_actual_p0_per_day(a["extractors"], _asgn_cc(a), _nost) for a in all_assignments
    )
    max_supportable_factories = int(_actual_p0_per_day / p0_per_factory_day) if p0_per_factory_day > 0 else 0

    # Supply-limited throughput: products_per_day above assumes 100% factory uptime, but when the
    # extractors can't keep a resource fed the factories run slow. The binding resource is the one
    # with the lowest (actual P0/day extracted ÷ P0/day the recipe needs); the factories can only
    # run at that fraction, so the *real* output is products_per_day × that ratio. Only computed
    # when we have planet quality data (else actual defaults to baseline → no discount).
    supply_ratio = 1.0
    bottleneck_p0 = None
    if avg_quality_pct is not None and products_per_day > 0:
        actual_by_p0: dict[str, float] = {}
        for a in all_assignments:
            for n, v in _actual_p0_per_day_by_p0(a["extractors"], _asgn_cc(a), _nost).items():
                actual_by_p0[n] = actual_by_p0.get(n, 0.0) + v
        needed_by_p0: dict[str, float] = {}
        for info in p1_info:
            pid, p0n = info.get("p1_type_id"), info.get("p0_name")
            if p0n and pid in p1_fracs:
                needed_by_p0[p0n] = needed_by_p0.get(p0n, 0.0) + p1_fracs[pid] * products_per_day * 150
        for n, need in needed_by_p0.items():
            if need <= 0:
                continue
            r = actual_by_p0.get(n, 0.0) / need
            if r < supply_ratio:
                supply_ratio, bottleneck_p0 = r, n
        supply_ratio = max(0.0, min(1.0, supply_ratio))
    supply_limited = supply_ratio < 0.995
    effective_products_per_day = round(products_per_day * supply_ratio)
    effective_isk_per_day = round(effective_products_per_day * sell_price, 2)

    # Per-P1 daily consumption (units/day the factories eat at full rate) = products_per_day ×
    # P1-units-per-product. Lets the PI Planner refill tool turn a pasted P1 stash into "days of
    # production it would sustain".
    for info in p1_info:
        info["units_per_day"] = round(products_per_day * p1_fracs.get(info["p1_type_id"], 0))

    return {
        "product":               {"type_id": req.type_id, "name": types.get(req.type_id, {}).get("name", "?")},
        "p1_requirements":       p1_info,
        "total_extractors_base": sum(q for _, q in sorted_p1),
        "ext_slots":             ext_slots,
        "density_est":           density_est,
        "assignments":           all_assignments,
        "unassigned":            remaining,
        "system_recommendations": sys_recs,
        "chosen_systems":           req.chosen_systems,
        "factory_character_ids":    req.factory_character_ids,
        "factory_system":           best_fac_system,
        "factory_system_options": factory_system_options,
        "factory_planets_needed": total_factory_planets,
        "factories_unplaceable":  factories_unbudgeted,
        "factory_planets_by_system": [
            {"system": s, "count": c, "type": "Barren/Temperate"}
            for s, c in sorted(sys_fac_count.items(), key=lambda x: -x[1])
        ],
        "stats": {
            "cycle_time":               cycle_time,
            "output_qty":               output_qty,
            "factories":                factories,
            "factory_output_per_hour":  req.factory_output_per_hour,
            "effective_factory_output_per_hour": round(effective_fph, 3),
            "overproduction_pct":       overproduction_pct,
            "max_supportable_factories": max_supportable_factories,
            "products_per_day":         products_per_day,
            "supply_limited":           supply_limited,
            "supply_ratio":             round(supply_ratio, 3),
            "effective_products_per_day": effective_products_per_day,
            "effective_isk_per_day":    effective_isk_per_day,
            "bottleneck_p0":            bottleneck_p0,
            "factory_refill_hours":     factory_refill_hours,
            "factory_input_m3_day":     round(p1_m3_per_factory_day),
            "factory_launchpads_assumed": _FACTORY_LAUNCHPADS,
            "sell_price":               round(sell_price, 2),
            "isk_per_day":              isk_per_day,
            "p0_per_day":               p0_per_day,
            "total_extractors":         total_extractors,
            "avg_quality_pct":          avg_quality_pct,
            "avg_p0_per_cycle":         avg_p0_per_cycle,
            "required_avg_p0_per_cycle": required_avg_p0_per_cycle,
            "split_mode":               split_mode,
            "split_planets":            split_planets,
            "planets_saved":            planets_saved,
            "distribution_mode":        _norm_dist_mode(req.distribution_mode),
        },
    }


import os as _os


@router.post("/api/debug/plan")
async def debug_plan(request: "Request", pp_session: str = Cookie(default=None)):
    from fastapi import HTTPException
    if not _os.environ.get("DEBUG_PI"):
        raise HTTPException(status_code=404, detail="Not found")
    debug_ctx = _os.environ.get("DEBUG_CONTEXT_ID")
    if debug_ctx:
        try:
            context_id = int(debug_ctx)
        except ValueError:
            raise HTTPException(status_code=500, detail="Invalid DEBUG_CONTEXT_ID")
    else:
        context_id = require_context(pp_session)

    body = await request.json()
    if body.get("fuelblock"):
        from app.fuelblock_planner import FuelBlockPlanRequest, _run_fuelblock_plan
        req = FuelBlockPlanRequest(**{k: v for k, v in body.items() if k in FuelBlockPlanRequest.model_fields})
        result = _run_fuelblock_plan(req, context_id)
    else:
        req = PlanRequest(**{k: v for k, v in body.items() if k in PlanRequest.model_fields})
        result = _run_plan(req, context_id)
    p1_reqs = result.get("p1_requirements", [])
    # Density-aware target: weight = need / density (thinner deposits get more extractors). Falls
    # back to pure need when no density data, matching the planner's own distribution.
    _dens = result.get("density_est") or {}
    _w = {r["p0_name"]: r["relative_qty"] / max(0.05, _dens.get(r["p0_name"], 1.0)) for r in p1_reqs}
    total_rel = sum(_w.values()) or 1
    ext_slots = result.get("ext_slots", 0)
    actual: dict[str, int] = {}
    out_of_system: list[dict] = []
    for asgn in result.get("assignments", []):
        for e in asgn["extractors"]:
            p0 = e.get("p0_name", "?")
            actual[p0] = actual.get(p0, 0) + 1
            sys_ = e.get("system", "")
            if sys_ and req.chosen_systems and sys_ not in req.chosen_systems:
                out_of_system.append({
                    "character": asgn["character_name"], "p0": p0, "system": sys_,
                    "planet_num": e.get("planet_num"), "is_replace": e.get("is_replace", False),
                })

    n_unassigned_slots = ext_slots - sum(actual.values())

    distribution, all_pass = [], True
    for r in sorted(p1_reqs, key=lambda x: -x["relative_qty"]):
        p0 = r["p0_name"]
        expected_f = ext_slots * _w[p0] / total_rel
        got = actual.get(p0, 0)
        delta = got - expected_f
        # Under-allocation is only a bug when there are still unassigned slots.
        # If n_unassigned_slots == 0 the planner filled everything it could; a deficit
        # means a physical planet constraint forced redistribution to other types.
        # In that case we allow up to 1.5 over-allocation (the natural cascade from
        # constrained redistribution), vs the strict ±1 Bresenham rounding tolerance.
        over_limit = 1.5 if n_unassigned_slots == 0 else 1.01
        ok = delta <= over_limit and (delta >= -1.01 or n_unassigned_slots == 0)
        if not ok:
            all_pass = False
        distribution.append({
            "p0_name": p0, "p1_name": r["p1_name"], "rel": r["relative_qty"],
            "expected": round(expected_f, 2), "actual": got, "delta": round(delta, 2), "ok": ok,
        })

    unassigned_counts: dict[str, int] = {}
    for u in result.get("unassigned", []):
        p0 = u.get("p0_name", "?")
        unassigned_counts[p0] = unassigned_counts.get(p0, 0) + 1

    return {
        "pass": all_pass, "ext_slots": ext_slots,
        "total_assigned": sum(actual.values()),
        "unassigned": unassigned_counts, "distribution": distribution, "out_of_system": out_of_system,
        "fuel_blocks_per_day": result.get("fuel_blocks_per_day"),
        "factory_lines": result.get("factory_lines"),
        "unplaced_factories": result.get("unplaced_factories"),
        "characters": [
            {
                "name": a["character_name"], "extractors": len(a["extractors"]),
                "max": a["effective_planets"], "free": a["free_planets"],
                "by_p0": {
                    p0: sum(1 for e in a["extractors"] if e.get("p0_name") == p0)
                    for p0 in set(e.get("p0_name") for e in a["extractors"])
                },
            }
            for a in result["assignments"]
        ],
    }


@router.post("/api/plan")
def compute_plan(req: PlanRequest, context_id: int = Depends(require_context)):
    return _run_plan(req, context_id)
