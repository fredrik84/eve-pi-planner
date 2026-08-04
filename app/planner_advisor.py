"""
Planner advisor — the "what should I change?" endpoints.

Placement analysis, factory fit, the derived Current-setup profile, skill ROI, reseat/redeploy
candidates and expansion capacity. These are advice ABOUT an existing setup, as opposed to
`planner.py`, which computes a plan from scratch; they were split out of it for that reason.

Imports the shared plan math from `planner.py` one way only (that module imports nothing from
here), so the chain stays acyclic: planner_algo <- planner <- planner_advisor <- planner_dashboard.
"""
import json as _json

from fastapi import APIRouter, Body, Cookie

from app.sde import load_pi_data, get_connection
from app.market import fetch_prices
from app.esi import session_context_id, ensure_char_tables, PI_CHAR_SQL
from app.planner_store import ensure_profile_tables, _flagged_colonies
from app.planner_recommendations import _P0_PLANET_TYPES, _p0_col
from app.planner import (
    _char_footprint, _compute_p1_fracs, _effective_fph, _factory_refill_hours,
    _p0_available_by_char_multi,
)

router = APIRouter()

@router.post("/api/analyze-placements")
def analyze_placements(body: dict = Body(...), pp_session: str = Cookie(default=None)):
    """For the given P1 type_ids, return — per real character — the Planet-DB planets that character
    could actually colonise for that P1's P0: a planet carrying the P0, in a system the character
    already operates in, not already occupied by them. Lets the Setup Analysis tab validate that a
    suggested 'redeploy to X' move is physically placeable (and name the target planet)."""
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"placements": {}}
    type_ids = [int(t) for t in (body.get("type_ids") or [])]
    if not type_ids:
        return {"placements": {}}
    from app.features import feature_enabled
    blend_on = feature_enabled("measured_yield_blend")
    pi = load_pi_data()
    types, sch = pi["types"], pi["schematics"]
    con = get_connection()
    foot, occ = _char_footprint(con, context_id)
    out = {}
    tid_p0 = {}
    for tid in type_ids:
        inputs = (sch.get(tid) or {}).get("inputs") or []
        if not inputs:
            continue
        p0_name = types.get(inputs[0]["type_id"], {}).get("name")
        if not _p0_col(p0_name or ""):
            continue
        tid_p0[tid] = p0_name
    avail_by_p0 = _p0_available_by_char_multi(con, set(tid_p0.values()), foot, occ, blend_on)
    for tid, p0_name in tid_p0.items():
        by_cid = avail_by_p0.get(p0_name, {})
        out[str(tid)] = {"p0_name": p0_name, "by_char": {str(cid): av for cid, av in by_cid.items()}}
    # Free Barren/Temperate planets each character could host a NEW factory on (any B/T planet works
    # — factories don't need a specific P0). In the char's footprint, not already colonised.
    bt = con.execute(
        "SELECT system, planet_num, planet_type, diameter FROM pp_planets WHERE planet_type IN ('Barren','Temperate')"
    ).fetchall()
    factory_sites = {}
    for cid, systems in foot.items():
        free = [{"system": p["system"], "planet_num": p["planet_num"], "planet_type": p["planet_type"],
                 "diameter": p["diameter"]}
                for p in bt if p["system"] in systems and (p["system"], p["planet_num"]) not in occ.get(cid, set())]
        if free:
            factory_sites[str(cid)] = free
    con.close()
    return {"placements": out, "factory_sites": factory_sites}


@router.post("/api/factory-fit")
def factory_fit(body: dict = Body(...)):
    """For each {type_id, planet_type, ccu}, how many launchpads the product's factory template fits at
    that command-centre level (via _factory_fit_lp: 3 = full, 1-2 = cramped/fewer facilities, 0 = doesn't
    fit at all). Pure layout math, no user data. Lets the 'move a character' tool verify a factory colony
    actually fits the receiving character's CCU before suggesting the move."""
    out = {}
    for it in (body.get("items") or []):
        try:
            tid = int(it["type_id"]); pt = str(it["planet_type"]); ccu = int(it["ccu"])
        except (KeyError, TypeError, ValueError):
            continue
        out[f"{tid}|{pt}|{ccu}"] = _factory_fit_lp(tid, pt, ccu)
    return {"fit": out}


@router.get("/api/my-setup-plan")
def my_setup_plan(pp_session: str = Cookie(default=None), debug_context_id: int | None = None):
    """Derive a 'demand profile' per distinct product the player's DEPLOYED factories build,
    shaped like a saved plan snapshot so the Setup Analysis tab can compare it against the
    player's extractor production (supply). Strictly scoped to the session's context so a
    player only ever sees their own factories."""
    import os as _os
    # Same DEBUG_PI gate as /api/debug/plan below — lets test scripts exercise this against a
    # seeded fixture context without a real session. `debug_context_id` (query param) lets one
    # test run cover several fixture contexts; falls back to the env var for parity with
    # /api/debug/plan's convention. Both no-ops unless DEBUG_PI is set — never set in prod.
    context_id = session_context_id(pp_session)
    if _os.environ.get("DEBUG_PI"):
        env_ctx = _os.environ.get("DEBUG_CONTEXT_ID")
        context_id = debug_context_id or (int(env_ctx) if env_ctx else None) or context_id
    if not context_id:
        return {"plans": []}
    return {"plans": derive_setup_plans(context_id)}


def derive_setup_plans(context_id: int) -> list[dict]:
    """The actual derivation behind /api/my-setup-plan, factored out so the admin debug endpoint
    (app/admin.py) can run it for an arbitrary context_id without going through session auth."""
    pi = load_pi_data()
    types = pi["types"]
    con = get_connection()
    # Configured factory planets (non-extractor, with a top-tier product) for THIS account only.
    # Hybrid colonies (extraction + a chained P1->P2+ factory on one planet, is_hybrid=1) are
    # deliberately EXCLUDED here, even though the hybrid_colonies flag exists — a hybrid colony's
    # P1 need is self-fed on the same planet (see _hybridReseatSection in analysis.js), but this
    # endpoint's consumption feeds the account-wide burndown/supply comparison (_producersOf in
    # analysis.js), which itself excludes hybrid colonies' output as non-exportable. Including them
    # here manufactured demand that supply could structurally never satisfy — for an account with
    # zero standalone factories (all hybrid), every derived "Current setup" plan read 0% fed
    # regardless of how well the colonies were actually running. Hybrid colonies get their own
    # correct, self-contained analysis via _hybridReseatSection — this endpoint should stay
    # standalone-factories-only.
    rows = con.execute("""
        SELECT c.character_name AS ch, cp.planet_num AS pn, s.name AS system,
               cp.products AS products, cp.pad_inputs AS pad_inputs
        FROM pp_char_planets cp
        JOIN pp_characters c ON c.character_id = cp.character_id
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        WHERE c.context_id = ? AND COALESCE(c.is_dummy, 0) = 0
          AND cp.is_extractor = 0
          AND cp.products IS NOT NULL AND cp.products != '[]'
    """, (context_id,)).fetchall()
    con.close()

    # Group factory planets by their top product type_id. Each factory also carries input_m3 —
    # the P1 already sitting in its launchpads (from pad_inputs, tier-1 only, 0.19 m³/unit) — so
    # the Refill tool can top up only the space that's actually free.
    by_product: dict[int, dict] = {}
    for r in rows:
        try:
            prods = _json.loads(r["products"]) or []
        except Exception:
            prods = []
        try:
            _pin = _json.loads(r["pad_inputs"]) or []
        except Exception:
            _pin = []
        input_m3 = round(sum((x.get("amount", 0) or 0) * 0.19 for x in _pin if (x.get("tier") or 0) == 1), 1)
        for p in prods:
            tid = p.get("type_id")
            if not tid:
                continue
            g = by_product.setdefault(tid, {"name": p.get("name") or f"#{tid}", "factories": []})
            loc = f"{r['ch']} · {r['system'] or '?'}" + (f" P{r['pn']}" if r["pn"] is not None else "")
            g["factories"].append({"loc": loc, "product": g["name"], "input_m3": input_m3})

    plans = []
    for tid, g in by_product.items():
        count = len(g["factories"])
        p1_fracs = _compute_p1_fracs(tid, pi)
        if not p1_fracs:
            continue  # not something that resolves to P1 inputs (shouldn't happen for ≥P2)
        products_per_day = round(count * _effective_fph(tid, pi) * 24)
        p1_prices = fetch_prices(list(p1_fracs.keys()))   # for valuing over-/under-extraction
        consumption = [
            {"p1_type_id": pid, "p1_name": types.get(pid, {}).get("name") or f"#{pid}",
             "units_per_day": round(products_per_day * frac), "sell": round(p1_prices.get(pid, 0.0), 2)}
            for pid, frac in p1_fracs.items()
        ]
        sell = fetch_prices([tid]).get(tid, 0.0)
        # The `count` factories of a product are identical, so each draws an equal 1/count
        # share of every P1 pool. Attaching p1_inputs lets the Refill tool split a pasted P1
        # stash across these planets — same shape the saved-plan snapshots use.
        share = (1.0 / count) if count else 0.0
        fac_p1_inputs = [
            {"p1_type_id": pid, "p1_name": types.get(pid, {}).get("name") or f"#{pid}", "share": share}
            for pid in p1_fracs
        ]
        for f in g["factories"]:
            f["p1_inputs"] = fac_p1_inputs
        plans.append({
            "name": f"Current setup: {g['name']} (×{count})",
            "consumption": consumption,
            "products_per_day": products_per_day,
            "isk_per_day": round(products_per_day * sell, 2) if sell else None,
            "factories_count": count,
            "factory_refill_hours": _factory_refill_hours(products_per_day, p1_fracs, count),
            "unit_label": g["name"],
            "factories": g["factories"],
            "tier": types.get(tid, {}).get("pi_tier") or 0,
        })

    # A player with multiple deployed products (e.g. a Coolant line AND a Mechanical Parts line)
    # otherwise has to switch the Refill tool between each product's plan one at a time, and two
    # products sharing a P1 (e.g. both wanting Precious Metals) have no shared bookkeeping between
    # their separate views — a pasted stack can get double-allocated. This merges every current
    # product into ONE combined entry (factories concatenated, consumption summed per P1) exactly
    # the way a multi-product basket/fuel-block plan already looks to the Refill tool — same flat
    # shape, so it reuses the existing single global-ratio split math with no frontend changes.
    if len(plans) > 1:
        combined_factories = [f for p in plans for f in p["factories"]]
        cons_map: dict[int, dict] = {}
        for p in plans:
            for c in p["consumption"]:
                e = cons_map.setdefault(c["p1_type_id"], {
                    "p1_type_id": c["p1_type_id"], "p1_name": c["p1_name"],
                    "units_per_day": 0, "sell": c.get("sell", 0.0),
                })
                e["units_per_day"] += c["units_per_day"]
        isk_vals = [p["isk_per_day"] for p in plans if p.get("isk_per_day")]
        refill_vals = [p["factory_refill_hours"] for p in plans if p.get("factory_refill_hours") is not None]
        plans.append({
            "name": f"Current setup: All factories combined (×{len(combined_factories)})",
            "consumption": list(cons_map.values()),
            "products_per_day": None,   # mixes incompatible products — no single meaningful unit
            "isk_per_day": round(sum(isk_vals), 2) if isk_vals else None,
            "factories_count": len(combined_factories),
            "factory_refill_hours": min(refill_vals) if refill_vals else None,
            "unit_label": "combined",
            "factories": combined_factories,
            "tier": 99,   # always sorts first (above any real tier 1-4) — the sensible default pick
        })
    # Highest-tier / biggest operations first (the combined entry, tier=99, always wins).
    plans.sort(key=lambda x: (-x["tier"], -x["factories_count"]))
    return plans


# The three caches below all memoize expensive layout-engine geometry computations
# (generate_layout → _enforce_min_sep does an O(pins²) pairwise-distance relaxation) that are
# PURE functions of static PI/SDE data + the layout algorithm itself — never user-specific.
# Confirmed live (2026-07-06): a single fuel-block plan touching ~10 products × up to 5 CC levels
# can cost 60-70s on a cold cache, and with 2+ pod replicas + pod restarts on deploy, the
# in-process dict below was cold far more often than warm. Backed by Redis (versioned key prefix,
# 30-day TTL) so each (product, planet_type, cc[, diameter]) combo is computed ONCE across the
# whole fleet's lifetime, not per-pod-per-cold-start; the in-process dict stays as a zero-latency
# L1 hit for the (common) case of the same combo recurring within one request/process.
# Bump _LAYOUT_CALC_VER if the layout engine's math ever changes, to invalidate stale values.
_LAYOUT_CALC_VER = "v3"   # v3: extractor heads cost a flat 110/550 again (planet size is links only)
_LAYOUT_CALC_TTL = 30 * 86400  # 30 days


def _layout_cache_get_or_compute(kind: str, mem_cache: dict, mem_key: tuple, compute):
    if mem_key in mem_cache:
        return mem_cache[mem_key]
    from app.cache import cache_get_json, cache_set_json
    rkey = f"layoutcalc:{_LAYOUT_CALC_VER}:{kind}:" + ":".join(str(k) for k in mem_key)
    cached = cache_get_json(rkey)
    if cached is not None:
        mem_cache[mem_key] = cached
        return cached
    result = compute()
    mem_cache[mem_key] = result
    cache_set_json(rkey, result, ttl=_LAYOUT_CALC_TTL)
    return result


_UNITS_PER_PLANET: dict = {}   # (product, planet_type, cc) -> factory units the template packs


def _units_per_planet(product: int, planet_type: str, cc: int) -> int:
    """How many factory units of `product` pack onto one planet at command-centre level `cc`
    (the layout engine's max_count). Bigger CC budget → more units fit. Cached (L1 process dict +
    L2 Redis, see _layout_cache_get_or_compute above)."""
    key = (product, planet_type or "Barren", cc)

    def compute():
        from app.layout import generate_layout
        try:
            r = generate_layout(product, planet_type or "Barren", launchpads=3,
                                count=None, cc_level=cc)
            # A factory unit is indivisible: when not even ONE fits the budget, max_count still
            # reports 1 (there's nothing smaller to build). Report 0 instead, or the skill advice
            # reads "this level runs your P4 planet" for a level that cannot host it at all.
            if r["planets"][0]["resources"]["over_fit"]:
                return 0
            return r["summary"]["max_count"]
        except Exception:
            return 0

    return _layout_cache_get_or_compute("units_per_planet", _UNITS_PER_PLANET, key, compute)


def _required_cc_factory(product: int, planet_type: str, cc: int) -> int:
    """Lowest CCU level that still packs as many factory units onto this planet as level `cc`
    does. Levels above it are dead weight for this colony as it stands today."""
    have = _units_per_planet(product, planet_type, cc)
    if have <= 0:
        return cc
    for lvl in range(1, cc):
        if _units_per_planet(product, planet_type, lvl) >= have:
            return lvl
    return cc


def _required_cc_extractor(planet_type: str, cc: int, no_storage: bool = False) -> int:
    """Lowest CCU level that fits as many Basic Industry Facilities alongside the 10 heads as
    level `cc` does — i.e. refines just as much P1 on-site. Modelled against our MAXIMAL extractor
    archetype, so it's the conservative answer: a player running a smaller colony (fewer basics,
    which is most of them) needs less than this. Only a fallback — prefer the colony's real
    upgrade_level from ESI where we have it."""
    from app.layout import fitted_extractor_basics
    try:
        have = fitted_extractor_basics(planet_type, cc, no_storage)
        for lvl in range(1, cc):
            if fitted_extractor_basics(planet_type, lvl, no_storage) >= have:
                return lvl
    except Exception:
        return cc
    return cc


@router.get("/api/skill-roi")
def skill_roi(pp_session: str = Cookie(default=None)):
    """Endpoint wrapper — resolves the session to a context and defers to `skill_roi_for`, which
    the Industry skill advisor also calls so the PI half of "what should I train" exists once."""
    return skill_roi_for(session_context_id(pp_session))


def skill_roi_for(context_id: int | None) -> dict:
    """Forward-looking 'train these skills for more output' advice for the player's CURRENT
    deployed setup (Setup Analysis tab). Two yield skills:
      • Interplanetary Consolidation — next level = +1 planet ≈ +1 colony's average value/day.
      • Command Center Upgrades — next level = a bigger CC budget → more factory units pack onto
        each FACTORY planet (layout-engine max_count delta × per-unit value). Extractor-side CCU
        gains (more basics) aren't modelled yet.
    Estimates (flat per-unit factory rate); strictly scoped to the session's context."""
    if not context_id:
        return {"suggestions": [], "enough": [], "note": None}
    pi = load_pi_data()
    types = pi["types"]
    con = get_connection()
    chars = con.execute(
        "SELECT character_id AS cid, character_name AS nm, "
        "       COALESCE(interplanetary_consolidation,0) AS ic, "
        "       COALESCE(command_center_upgrades,0) AS ccu "
        "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0 "
                + PI_CHAR_SQL, (context_id,)).fetchall()
    rows = con.execute(
        "SELECT cp.character_id AS cid, cp.is_extractor AS ext, cp.planet_type AS ptype, "
        "       cp.products AS products, COALESCE(cp.upgrade_level,0) AS cclvl "
        "FROM pp_char_planets cp JOIN pp_characters c ON c.character_id=cp.character_id "
        "WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0", (context_id,)).fetchall()
    con.close()

    by_char: dict = {}
    total_used = 0
    fac_products: set = set()
    for p in rows:
        total_used += 1
        d = by_char.setdefault(p["cid"], {"ext": [], "fac": [], "deployed_cc": 0})
        # The command-centre level this colony is ACTUALLY running at, straight from ESI. Observed
        # state beats any model of what "should" fit — see _required_cc below.
        d["deployed_cc"] = max(d["deployed_cc"], int(p["cclvl"] or 0))
        if p["ext"]:
            d["ext"].append(p["ptype"] or "Barren")
        else:
            try:
                prods = _json.loads(p["products"] or "[]")
            except Exception:
                prods = []
            tid = prods[0].get("type_id") if prods else None
            if tid:
                d["fac"].append({"tid": tid, "ptype": p["ptype"] or "Barren"})
                fac_products.add(tid)

    prices = fetch_prices(list(fac_products)) if fac_products else {}
    _unit_val = lambda tid: _effective_fph(tid, pi) * 24 * prices.get(tid, 0.0)   # ISK/day per factory unit

    # Current flat value/day (one unit per factory planet, same model as my_setup_plan), to value
    # the marginal IC planet. Also the single-product label, if the whole setup makes one thing.
    total_value_day = 0.0
    prod_ppd: dict = {}
    for cid, d in by_char.items():
        for f in d["fac"]:
            total_value_day += _unit_val(f["tid"])
            prod_ppd[f["tid"]] = prod_ppd.get(f["tid"], 0.0) + _effective_fph(f["tid"], pi) * 24
    per_planet_value = (total_value_day / total_used) if total_used else 0.0
    single_tid = next(iter(prod_ppd)) if len(prod_ppd) == 1 else None
    single_label = (types.get(single_tid, {}).get("name") if single_tid else None)

    suggestions = []
    enough = []
    for c in chars:
        d = by_char.get(c["cid"])
        if not d:
            continue                                    # idle character — nothing deployed to scale
        n_planets = len(d["ext"]) + len(d["fac"])
        free_slots = max(0, (1 + (c["ic"] or 0)) - n_planets)

        # Interplanetary Consolidation → +1 planet (next level), valued at one colony's average.
        # Only worth training once the slots they ALREADY have are in use — telling someone to
        # train a rank-4 skill for a slot while one sits empty is backwards.
        if c["ic"] < 5 and n_planets > 0 and per_planet_value > 0 and not free_slots:
            sug = {"char": c["nm"], "skill": "Interplanetary Consolidation",
                   "from_lvl": c["ic"], "to_lvl": c["ic"] + 1, "detail": "+1 planet slot",
                   "add_isk_day": round(per_planet_value, 2)}
            if single_tid:
                sug["add_units_day"] = round(prod_ppd[single_tid] / total_used)
                sug["unit_label"] = single_label
            suggestions.append(sug)

        # Command Center Upgrades → more factory units per planet (factory planets only).
        if c["ccu"] < 5 and d["fac"]:
            cc = max(1, min(5, c["ccu"] or 5))
            add_isk = 0.0
            add_units = 0.0
            by_prod: dict = {}
            for f in d["fac"]:
                mc0 = _units_per_planet(f["tid"], f["ptype"], cc)
                mc1 = _units_per_planet(f["tid"], f["ptype"], cc + 1)
                extra = mc1 - mc0
                if mc0 > 0 and extra > 0:
                    add_isk += extra * _unit_val(f["tid"])
                    u = extra * _effective_fph(f["tid"], pi) * 24
                    add_units += u
                    by_prod[f["tid"]] = by_prod.get(f["tid"], 0) + extra
            if add_isk > 0 or add_units > 0:
                sug = {"char": c["nm"], "skill": "Command Center Upgrades",
                       "from_lvl": c["ccu"], "to_lvl": c["ccu"] + 1,
                       "detail": "bigger command centre → more factories per planet",
                       "add_isk_day": round(add_isk, 2)}
                if len(by_prod) == 1:
                    only = next(iter(by_prod))
                    sug["add_units_day"] = round(add_units)
                    sug["unit_label"] = types.get(only, {}).get("name")
                suggestions.append(sug)

        # ── The other direction: what this character's setup actually REQUIRES. ──────────
        # Preferred source is the command-centre level the colonies are ACTUALLY upgraded to
        # (ESI). It can't over-claim — a player who did upgrade to V reports V and gets no
        # advice — and it needs no assumptions about how big their colonies "should" be.
        # Characters are rarely pure-factory, so a modelled fallback (used when the scan predates
        # the column) takes the MAX across every planet they hold, extractors included.
        if c["ccu"] and c["ccu"] > 1 and n_planets:
            cc = max(1, min(5, c["ccu"]))
            deployed = d["deployed_cc"]
            if deployed:
                req, why = deployed, "deployed"
            else:
                req = 1
                for pt in d["ext"]:
                    req = max(req, _required_cc_extractor(pt, cc))
                for f in d["fac"]:
                    req = max(req, _required_cc_factory(f["tid"], f["ptype"], cc))
                why = "modelled"
            if req < cc:
                enough.append({
                    "char": c["nm"], "skill": "Command Center Upgrades",
                    "have_lvl": cc, "need_lvl": req, "basis": why,
                    "detail": (f"every colony runs a level-{req} command centre"
                               if why == "deployed" else
                               f"level {req} runs all {n_planets} of these colonies as they are"),
                    "planets": n_planets, "extractors": len(d["ext"]), "factories": len(d["fac"]),
                })
        # Idle planet slots are a live under-use of a skill already trained — worth more than any
        # training suggestion, since it costs nothing but a deploy.
        if free_slots and n_planets:
            enough.append({
                "char": c["nm"], "skill": "Interplanetary Consolidation",
                "have_lvl": c["ic"], "need_lvl": max(0, n_planets - 1),
                "detail": (f"{free_slots} planet slot{'s' if free_slots > 1 else ''} already "
                           f"trained but not deployed"),
                "planets": n_planets, "free_slots": free_slots,
            })

    # Biggest gains first; keep ISK-bearing ones above pure-unit ones.
    suggestions.sort(key=lambda s: (s.get("add_isk_day") or 0, s.get("add_units_day") or 0), reverse=True)
    note = None
    if fac_products and not prices:
        note = "Market prices unavailable — showing extra output only."
    enough.sort(key=lambda s: (s.get("need_lvl") or 0) - (s.get("have_lvl") or 0))  # biggest gap first
    return {"suggestions": suggestions[:12], "enough": enough[:12], "note": note}


# ── Redeploy advice (Setup Analysis) ─────────────────────────────────────────────
# Two independent "move this extraction elsewhere" signals, each behind its own feature flag:
#   • Same-hotspot proximity (redeploy_proximity) — sharing a planet is NORMAL and fine; the real
#     waste is when extractor HEADS from two colonies on that planet are placed on the SAME resource
#     hotspot (their extraction discs overlap on the same P0). When a whole fleet is deployed at once
#     it's tempting to park every character's heads on the current best spot; that spot depletes for
#     everyone at once. The fix is to spread the heads to different areas of the planet grid, not to
#     abandon the planet. Detected from the ESI head coordinates (lat/lon + head_radius, radians)
#     captured at scan time (pp_char_planets.ext_heads): two heads compete when their discs overlap
#     (great-circle distance < r_a + r_b) AND they pull the same P0.
#   • Reseat exhausted (redeploy_depletion) — a deposit never runs out: its hotspots drift around the
#     planet and replenish, so a reseat (re-survey + re-place heads) generally lifts the peak a little.
#     What we detect is when that stops paying off: across several programs the player has RESEATED
#     (confirmed by the head centroid actually moving between programs, pp_colony_yield.head_centroid)
#     yet the latest peak still can't beat the prior best by more than a hair. Reseating is then
#     exhausted — THIS planet is just tapped — and the real fix is to redeploy to a genuinely richer
#     planet (only when one is free). Not a reason to move on its own; the frontend gates it on the
#     material being short AND a richer free planet existing.
_RESEAT_WINDOW       = 6      # most recent programs considered
_RESEAT_MIN_PROGRAMS = 3      # need this many programs (≥2 reseats) before calling reseating exhausted
_RESEAT_MARGINAL     = 0.05   # latest reseat gaining <5% over the prior best = plateaued (marginal)
_RESEAT_MOVE         = 0.01   # planar (lat,lon) head-centroid shift between programs that = a real reseat
# The overlap that matters is between the two deployments' REACHABLE AREAS, not their current heads —
# reseating just moves heads around within reach, so overlapping reachable areas keep competing for
# the same hotspots and both averages decay the longer they share it. We return every overlap above a
# tiny floor and let the client filter by the user's configured threshold (Settings → General), so
# the cutoff is a live per-browser preference, not baked into the payload. overlap_pct =
# (R_a+R_b − dist)/(R_a+R_b)×100 (footprint radii R, centres `dist` apart) → 100 = coincident areas.
_HOTSPOT_OVERLAP_FLOOR = 1.0
_HOTSPOT_OVERLAP_DEFAULT = 50.0   # the client default (documented here so the two stay in sync)


def _gc_dist(a: tuple, b: tuple) -> float:
    """Great-circle (angular) distance in radians between two (lat, lon) positions. EVE reports planet
    coords in radians on the sphere; head_radius is in the same units, so distance-vs-radius compares
    directly."""
    import math
    la, lo_a = a
    lb, lo_b = b
    v = math.sin(la) * math.sin(lb) + math.cos(la) * math.cos(lb) * math.cos(lo_a - lo_b)
    return math.acos(max(-1.0, min(1.0, v)))


def _footprint(ecu: dict) -> tuple | None:
    """(centre, reach_radius) of one ECU's reachable extraction area, in radians. Centre = the ECU pin
    position (`c`, where the command centre sits) if captured, else the head centroid (older scans).
    reach = how far the deployment reaches = farthest current head from the centre + head_radius (a
    conservative proxy for the area heads can be reseated across; we have no explicit max-reach)."""
    heads = ecu.get("h") or []
    if not heads:
        return None
    c = ecu.get("c")
    if not c:
        c = [sum(h[0] for h in heads) / len(heads), sum(h[1] for h in heads) / len(heads)]
    reach = max(_gc_dist(c, h) for h in heads) + (ecu.get("r") or 0.0)
    return (c, reach) if reach > 0 else None


def _footprint_overlap(ecus_a: list, ecus_b: list) -> dict | None:
    """Worst same-P0 reachable-AREA overlap between two colonies' ECUs. Returns {p0, overlap_pct} for
    the most-overlapping same-P0 footprint pair (above the floor), or None. `ecus_*` = the parsed
    ext_heads list ([{p0, r, c:[lat,lon], h:[[lat,lon],...]}])."""
    best = None
    for ea in ecus_a:
        fa = _footprint(ea)
        if not fa:
            continue
        for eb in ecus_b:
            if ea.get("p0") != eb.get("p0"):
                continue                     # different resource deposits — no competition
            fb = _footprint(eb)
            if not fb:
                continue
            reach = fa[1] + fb[1]
            d = _gc_dist(fa[0], fb[0])
            pct = (reach - d) / reach * 100.0
            if pct >= _HOTSPOT_OVERLAP_FLOOR and (best is None or pct > best["overlap_pct"]):
                best = {"p0": ea.get("p0"), "overlap_pct": round(pct)}
    return best


def _centroid_dist(a: str | None, b: str | None) -> float:
    """Planar distance between two "lat,lon" head centroids (small-angle, same as the layout geometry —
    EVE's planet space is a flat lat/lon plane). 0 when either is missing/unparseable."""
    try:
        la, lo = (float(x) for x in a.split(","))
        lb, lob = (float(x) for x in b.split(","))
        return ((la - lb) ** 2 + (lo - lob) ** 2) ** 0.5
    except Exception:
        return 0.0


def _reseat_status(samples: list) -> dict | None:
    """`samples` = [(peak_day, head_centroid), ...] oldest→newest, one per program (reseat/restart).
    Decide whether reseating this colony has been EXHAUSTED — the player has genuinely reseated it
    several times (heads moved between programs) yet the latest peak still can't beat the prior best by
    more than a hair, so the planet is simply tapped and a richer one is the real fix. Returns detail
    (incl. how many reseats we can confirm from head movement) or None when there's too little history
    OR a recent reseat is still improving it (keep reseating — no escalation)."""
    s = [(p, c) for (p, c) in samples if p and p > 0][-_RESEAT_WINDOW:]
    if len(s) < _RESEAT_MIN_PROGRAMS:
        return None
    peaks = [p for p, _ in s]
    best_prior = max(peaks[:-1])
    latest = peaks[-1]
    recent_gain = (latest - best_prior) / best_prior if best_prior > 0 else 0.0
    if recent_gain >= _RESEAT_MARGINAL:
        return None                       # a recent reseat still beat the prior best → reseating works
    # Marginal/negative gain. Count reseats we can CONFIRM by the head centroid actually moving.
    moves, tracked = 0, False
    for (_p0, c0), (_p1, c1) in zip(s, s[1:]):
        if c0 and c1:
            tracked = True
            if _centroid_dist(c0, c1) > _RESEAT_MOVE:
                moves += 1
    drop = (peaks[0] - latest) / peaks[0] if peaks[0] > 0 else 0.0
    return {"programs": len(s), "peak_first": round(peaks[0]), "peak_last": round(latest),
            "peak_best": round(best_prior), "decline_pct": round(max(drop, 0.0) * 100),
            "recent_gain_pct": round(recent_gain * 100),
            "reseats_confirmed": moves, "reseat_tracked": tracked}


def _colony_reach(ext_heads) -> float | None:
    """Largest reachable-area radius (radians) across a colony's current ECUs — the yardstick that
    tells a reseat (heads shuffled WITHIN reach) from a redeploy (the command centre picked up and
    dropped BEYOND reach). None when there's no current head data to size it from."""
    reach = None
    for ecu in (ext_heads or []):
        fp = _footprint(ecu)
        if fp and (reach is None or fp[1] > reach):
            reach = fp[1]
    return reach


def reseat_redeploy_events(samples, ext_heads=None) -> dict:
    """Date the LAST reseat and the LAST redeploy from a colony's per-program head-centroid history.
    `samples` = [(install_ts, "lat,lon"), ...] oldest→newest (one per extraction program). Each
    program-to-program transition is classified by how far the head centroid moved:
      • d <= _RESEAT_MOVE           → a same-spot program restart (heads didn't really move)
      • _RESEAT_MOVE < d <= reach   → a reseat (heads shuffled within the ECU's reachable area)
      • d > reach                   → a redeploy (the command centre moved beyond extraction range)
    `reach` is the colony's CURRENT reachable-area radius (`_colony_reach`, from `ext_heads`); with
    no current head data reach is unknown, so a far jump can't be told from a reseat and every move
    is counted as a reseat (redeploy stays undated). Returns {"reseat_at": ts|None, "redeploy_at":
    ts|None} — the install_ts of the newer program in the most recent transition of each kind. NOTE
    a redeploy to a DIFFERENT planet starts a fresh (character_id, planet_id) history, so only a
    redeploy to another area of the SAME planet is datable here."""
    s = [(t, c) for (t, c) in (samples or []) if t and c]
    reach = _colony_reach(ext_heads)
    reseat_at = redeploy_at = None
    for (_t0, c0), (t1, c1) in zip(s, s[1:]):
        d = _centroid_dist(c0, c1)
        if d <= _RESEAT_MOVE:
            continue                                  # same spot — a plain restart, not a move
        if reach is not None and d > reach:
            redeploy_at = t1
        else:
            reseat_at = t1
    return {"reseat_at": reseat_at, "redeploy_at": redeploy_at}


def derive_redeploy_candidates(context_id: int) -> dict:
    """Both redeploy signals for one account (behind the endpoint below, factored out so tests can
    call it for a fixture context). Extractor colonies only — both are extraction concerns."""
    ensure_char_tables()        # make sure the ext_heads column exists before we read it
    con = get_connection()
    planets = con.execute("""
        SELECT cp.character_id AS cid, c.character_name AS nm, cp.planet_id AS pid,
               cp.planet_num AS pn, cp.p0_type_id AS p0, cp.p0_name AS p0name,
               cp.planet_type AS ptype, cp.ext_heads AS ext_heads, COALESCE(s.name, '') AS system
        FROM pp_char_planets cp
        JOIN pp_characters c ON c.character_id = cp.character_id
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0 AND cp.is_extractor=1
    """, (context_id,)).fetchall()
    hist_rows = con.execute("""
        SELECT y.character_id AS cid, y.planet_id AS pid, y.install_ts AS install,
               y.peak_day AS peak, y.head_centroid AS centroid
        FROM pp_colony_yield y
        JOIN pp_characters c ON c.character_id = y.character_id
        WHERE c.context_id=? AND y.peak_day > 0
        ORDER BY y.character_id, y.planet_id, y.install_ts ASC
    """, (context_id,)).fetchall()
    con.close()

    def _loc(r):
        return (r["system"] or "?") + (f" P{r['pn']}" if r["pn"] is not None else "")

    p0name_by = {r["p0"]: r["p0name"] for r in planets if r["p0"]}

    # P0 refines 1:1 into a single P1 — the name the player's own extractor templates use (e.g. Felsic
    # Magma → Silicon). Surface that P1 name so the redeploy card matches the templates, not the raw
    # resource. Built from the tier-1 schematics (a P1 output's sole input is its P0).
    _pi = load_pi_data()
    _types, _sch = _pi["types"], _pi["schematics"]
    p1name_by: dict = {}
    for _out, _s in _sch.items():
        if (_types.get(_out, {}).get("pi_tier") or 0) == 1:
            _inps = _s.get("inputs") or []
            if _inps:
                p1name_by[_inps[0]["type_id"]] = _types.get(_out, {}).get("name")

    # ── Proximity: heads of two colonies on ONE planet overlap the same resource hotspot ──
    by_planet: dict = {}
    for r in planets:
        by_planet.setdefault(r["pid"], []).append(r)
    proximity = []
    unscanned = 0                            # colonies sharing a planet but with no head data yet
    for pid, occ in by_planet.items():
        if len(occ) < 2:
            continue
        parsed = []
        for o in occ:
            try:
                ecus = _json.loads(o["ext_heads"]) if o["ext_heads"] else None
            except Exception:
                ecus = None
            if ecus:
                parsed.append((o, ecus))
            else:
                unscanned += 1
        # Compare every pair of colonies on this planet from DIFFERENT characters.
        for i in range(len(parsed)):
            for j in range(i + 1, len(parsed)):
                oa, ea = parsed[i]
                ob, eb = parsed[j]
                if oa["cid"] == ob["cid"]:
                    continue                 # the game already min-separates one char's own heads
                hit = _footprint_overlap(ea, eb)
                if hit:
                    proximity.append({
                        "planet_id": pid, "location": _loc(oa),
                        "planet_type": oa["ptype"],
                        "p0_name": p0name_by.get(hit["p0"]) or oa["p0name"],
                        "p1_name": p1name_by.get(hit["p0"]),
                        "overlap_pct": hit["overlap_pct"],
                        "characters": sorted({oa["nm"], ob["nm"]}),
                    })
    proximity.sort(key=lambda p: p["overlap_pct"], reverse=True)

    # ── Reseat exhausted: reseated repeatedly (heads moved) but still marginal → planet tapped ──
    # (JSON key stays `depleting` for compatibility; the meaning is now "reseating won't lift it".)
    samples_by: dict = {}
    date_samples_by: dict = {}       # (install_ts, centroid) per program → last reseat/redeploy dates
    for h in hist_rows:
        samples_by.setdefault((h["cid"], h["pid"]), []).append((h["peak"], h["centroid"]))
        date_samples_by.setdefault((h["cid"], h["pid"]), []).append((h["install"], h["centroid"]))
    depleting = []
    detected = set()
    for r in planets:
        status = _reseat_status(samples_by.get((r["cid"], r["pid"]), []))
        if status:
            try:
                _ecus = _json.loads(r["ext_heads"]) if r["ext_heads"] else None
            except Exception:
                _ecus = None
            events = reseat_redeploy_events(date_samples_by.get((r["cid"], r["pid"]), []), _ecus)
            depleting.append({
                "planet_id": r["pid"], "character": r["nm"], "character_id": r["cid"], "location": _loc(r),
                "system": r["system"], "planet_num": r["pn"],
                "p0_name": r["p0name"], "p1_name": p1name_by.get(r["p0"]), **status, **events,
            })
            detected.add((r["cid"], r["pid"]))
    # Manual "a reseat can't reach the target here" marks — a user-asserted reseat-exhausted flag. Add
    # any that the detector didn't already flag, so they get the same concrete redeploy treatment.
    flagged = _flagged_colonies(context_id)
    for r in planets:
        if (r["cid"], r["pid"]) in flagged and (r["cid"], r["pid"]) not in detected:
            depleting.append({
                "planet_id": r["pid"], "character": r["nm"], "character_id": r["cid"], "location": _loc(r),
                "system": r["system"], "planet_num": r["pn"], "p0_name": r["p0name"],
                "p1_name": p1name_by.get(r["p0"]), "programs": 0, "peak_first": 0, "peak_last": 0,
                "peak_best": 0, "decline_pct": 0, "recent_gain_pct": 0, "reseats_confirmed": 0,
                "reseat_tracked": False, "user_flagged": True,
            })
    for d in depleting:
        if (d.get("character_id"), d["planet_id"]) in flagged:
            d["user_flagged"] = True
    depleting.sort(key=lambda d: (bool(d.get("user_flagged")), d["decline_pct"]), reverse=True)

    # Cross-link the two signals. When a character caught in a same-hotspot overlap is ALSO the one
    # whose yield is depleting on that planet, THAT character is the one to move (it needs a fresh
    # deposit anyway) — moving it clears the overlap too, so it takes precedence as the mover. This
    # keeps us from advising the wrong character to relocate, and avoids two disconnected nudges
    # about the same planet.
    depl_keys = {(d["planet_id"], d["character"]) for d in depleting}
    for p in proximity:
        movers = [c for c in p["characters"] if (p["planet_id"], c) in depl_keys]
        if movers:
            p["precedence_character"] = movers[0]
    prox_keys = {(p["planet_id"], c) for p in proximity for c in p["characters"]}
    for d in depleting:
        if (d["planet_id"], d["character"]) in prox_keys:
            d["also_overlapping"] = True

    # Concrete redeploy destinations: for each resource involved, the best FREE same-P0 planet in each
    # mover's OWN systems — so the UI can say "redeploy to <system Pn> (richness)" instead of a vague
    # "fresh planet", and fall back to "relocate on the same planet" when no free one exists. Keyed
    # p0_name → character_name → best planet. Best-effort (never blocks the core advice).
    placements: dict = {}
    try:
        from app.features import feature_enabled
        blend_on = feature_enabled("measured_yield_blend")
        con2 = get_connection()
        foot, occ = _char_footprint(con2, context_id)
        nm_by_cid = {r["cid"]: r["nm"] for r in planets}
        p0_names = ({p["p0_name"] for p in proximity if p.get("p0_name")}
                    | {d["p0_name"] for d in depleting if d.get("p0_name")})
        for p0n, by_char in _p0_available_by_char_multi(con2, p0_names, foot, occ, blend_on).items():
            per_char = {}
            for cid, avail in by_char.items():
                nm = nm_by_cid.get(cid)
                if nm and avail:
                    per_char[nm] = avail[0]      # best available in that character's systems
            if per_char:
                placements[p0n] = per_char
        # The reseat-exhausted colony's OWN current richness, so the UI only calls a redeploy worth it
        # when a free planet is genuinely richer (else it's not — keep reseating / accept it).
        for d in depleting:
            col = _p0_col(d["p0_name"]) if d.get("p0_name") else None
            if col and d.get("system") and d.get("planet_num") is not None:
                row = con2.execute(
                    f'SELECT "{col}" AS r FROM pp_planets WHERE system=? AND planet_num=?',
                    (d["system"], d["planet_num"])).fetchone()
                if row and row["r"]:
                    d["current_richness"] = round(row["r"])
        con2.close()
    except Exception:
        placements = {}

    return {"proximity": proximity, "proximity_unscanned": unscanned, "depleting": depleting,
            "placements": placements}


@router.get("/api/redeploy-candidates")
def redeploy_candidates(pp_session: str = Cookie(default=None), debug_context_id: int | None = None):
    """Setup Analysis 'redeploy this extraction' advice — same-hotspot head overlap and depleting
    deposits. Strictly scoped to the session's context. DEBUG_PI gate (same convention as
    /api/my-setup-plan) lets a test exercise it against a seeded fixture without a real session."""
    import os as _os
    context_id = session_context_id(pp_session)
    if _os.environ.get("DEBUG_PI"):
        env_ctx = _os.environ.get("DEBUG_CONTEXT_ID")
        context_id = debug_context_id or (int(env_ctx) if env_ctx else None) or context_id
    if not context_id:
        return {"proximity": [], "proximity_unscanned": 0, "depleting": [], "placements": {}}
    return derive_redeploy_candidates(context_id)



def _expansion_capacity(context_id: int) -> dict:
    """Spare fleet capacity worth re-planning for: real characters with no colonies (idle), free
    planet slots (max_planets − deployed), and characters whose CCU/IC grew since the last plan was
    saved (vs pp_plan_baseline). All from current data + one baseline row — no skill history stored."""
    try:
        ensure_profile_tables()
        con = get_connection()
        chars = con.execute(
            "SELECT character_id AS cid, character_name AS nm, "
            "       1 + COALESCE(interplanetary_consolidation,0) AS max_planets, "
            "       COALESCE(command_center_upgrades,0) AS ccu, COALESCE(interplanetary_consolidation,0) AS ic "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0 "
                + PI_CHAR_SQL, (context_id,)).fetchall()
        used = {r["character_id"]: r["n"] for r in con.execute(
            "SELECT cp.character_id, COUNT(*) AS n FROM pp_char_planets cp "
            "JOIN pp_characters c ON c.character_id=cp.character_id "
            "WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0 GROUP BY cp.character_id", (context_id,)).fetchall()}
        base_row = con.execute("SELECT skills_json, plan_name FROM pp_plan_baseline WHERE context_id=?", (context_id,)).fetchone()
        con.close()
    except Exception:
        return {"idle_chars": [], "free_slots": 0, "free_slot_chars": [], "skills_grew": [], "plan_name": None}

    baseline = {}
    if base_row and base_row["skills_json"]:
        try:
            baseline = _json.loads(base_row["skills_json"])
        except Exception:
            baseline = {}

    idle, free_chars, grew, free_slots = [], [], [], 0
    for r in chars:
        n = used.get(r["cid"], 0)
        if n == 0:
            idle.append(r["nm"])
        spare = max(0, r["max_planets"] - n)
        if spare > 0 and n > 0:                       # partial: deployed but not full (idle covered above)
            free_chars.append({"name": r["nm"], "used": n, "max": r["max_planets"], "free": spare})
        free_slots += spare
        b = baseline.get(str(r["cid"]))               # [ccu, ic] at last plan save
        if b:
            d_ccu, d_ic = r["ccu"] - (b[0] or 0), r["ic"] - (b[1] or 0)
            if d_ccu > 0 or d_ic > 0:
                grew.append({"name": r["nm"], "ccu_up": max(0, d_ccu), "ic_up": max(0, d_ic)})
    return {"idle_chars": idle, "free_slots": free_slots, "free_slot_chars": free_chars,
            "skills_grew": grew, "plan_name": base_row["plan_name"] if base_row else None,
            "total_used": sum(used.values())}


_FACTORY_FIT: dict = {}   # (product, planet_type, ccu) -> max launchpads (3..1) the real template fits, 0 if none


def _factory_fit_lp(product: int, planet_type: str, ccu: int | None, diameter: float | None = None) -> int:
    """How many launchpads the product's factory template ACTUALLY fits on this planet at this
    command-centre level — by generating the layout and reading its CPU/PG budget, not by assuming.
    `diameter` (km) uses the planet's REAL size (per-planet from pp_planets); without it the flat
    per-type PLANET_DIAM is used. 3 = full layout; <3 = cramped; 0 = doesn't fit at all. Cached
    (L1 process dict + L2 Redis, see _layout_cache_get_or_compute above)."""
    cc = ccu or 5
    key = (product, planet_type, cc, round(diameter) if diameter else 0)

    def compute():
        from app.layout import generate_layout
        for lp in (3, 2, 1):
            try:
                r = generate_layout(product, planet_type, launchpads=lp, count=1, cc_level=cc, diam_override=diameter)
                res = (r.get("planets") or [{}])[0].get("resources") or {}
                if not res.get("over"):
                    return lp
            except Exception:
                continue
        return 0

    return _layout_cache_get_or_compute("factory_fit", _FACTORY_FIT, key, compute)


# The layout's CPU/PG model is calibrated at ~8000 km (a real hand-built factory) and its link cost is
# weak, so it stays optimistic far past where it's been validated (it claims even a 73,000 km Gas planet
# fits). Never place a factory above this hard ceiling — twice the calibrated size — no matter what the
# model says. Below it, the per-product / per-CCU cap (_factory_pack_max_diameter) governs.
_FACTORY_DIAM_CEILING = 16000.0

_FACTORY_PACK_MAXDIAM: dict = {}   # (product, ccu) -> largest real diameter the TYPE-packed factory fits


def _factory_pack_max_diameter(product: int, ccu: int | None) -> float:
    """Largest REAL planet diameter (km) on which the factory still fits — at the facility count the
    exported template actually packs (computed at the calibrated B/T size). Place a factory only on a
    planet at/under this and the unchanged type-based export is guaranteed to fit the real planet.
    Cached (L1 process dict + L2 Redis, see _layout_cache_get_or_compute above) — this was the
    dominant cost in a confirmed-live 90s+ fuel-block plan (binary search × generate_layout's O(pins²)
    geometry relaxation, repeated per product × CC level, cold on every pod start)."""
    cc = ccu or 5
    key = (product, cc)

    def compute():
        from app.layout import generate_layout
        try:
            n = generate_layout(product, "Barren", launchpads=3, count=None, cc_level=cc)["summary"]["max_count"]
        except Exception:
            n = 1

        def fits(d):
            try:
                r = generate_layout(product, "Barren", launchpads=3, count=n, cc_level=cc, diam_override=d)
                return not ((r.get("planets") or [{}])[0].get("resources") or {}).get("over")
            except Exception:
                return False

        lo, hi = 8000.0, 250000.0      # 8000 = the calibrated size where `n` fits by construction
        if fits(hi):
            return hi
        for _ in range(8):
            mid = (lo + hi) / 2
            if fits(mid):
                lo = mid
            else:
                hi = mid
        return lo

    return _layout_cache_get_or_compute("pack_maxdiam", _FACTORY_PACK_MAXDIAM, key, compute)


def _factory_full_ccu(product: int, planet_type: str) -> int | None:
    """Lowest command-centre level at which the full 3-launchpad layout fits (what to train CCU to)."""
    for cc in range(1, 6):
        if _factory_fit_lp(product, planet_type, cc) >= 3:
            return cc
    return None


def _setup_products(context_id: int, pi: dict, con=None) -> list[dict]:
    """The distinct products the player's deployed factories build, most factories first — drives the
    'plan for this product' dropdown on the Spare-capacity card. Accepts a shared connection (from a
    caller that's about to also call _expansion_deploys per product) to avoid opening a fresh one."""
    owns_con = con is None
    if owns_con:
        con = get_connection()
    rows = con.execute(
        "SELECT cp.products FROM pp_char_planets cp JOIN pp_characters c ON c.character_id=cp.character_id "
        "WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0 AND cp.is_extractor=0 "
        "AND cp.products IS NOT NULL AND cp.products != '[]'", (context_id,)).fetchall()
    if owns_con:
        con.close()
    cnt: dict[int, int] = {}
    for r in rows:
        for p in (_json.loads(r["products"]) or []):
            t = p.get("type_id")
            if t:
                cnt[t] = cnt.get(t, 0) + 1
    types = pi["types"]
    return sorted(
        [{"type_id": t, "name": types.get(t, {}).get("name") or f"#{t}", "count": c} for t, c in cnt.items()],
        key=lambda x: -x["count"])


def _expansion_deploys(context_id: int, pi: dict, chosen_product: int | None = None, con=None) -> list[dict]:
    """Concrete 'deploy this colony here' cards for spare capacity — the Analysis-style answer to
    'I added a toon / freed a slot, now what'. Each free planet slot goes to the material that most
    helps the setup (tightest supply/demand first, re-evaluated as we add), pinned to a real free
    planet in a system the fleet already runs (idle/new toons join the fleet's systems). Bottleneck
    relief before scale, so a supply-limited setup gets fed before you add factories. Read-only/no plan.
    Accepts a shared connection — callers computing this per-product in a loop (expansion()) should
    pass one in to avoid opening a fresh connection on every iteration."""
    types, sch = pi["types"], pi["schematics"]
    owns_con = con is None
    if owns_con:
        con = get_connection()
    rows = con.execute("""
        SELECT c.character_id AS cid, c.character_name AS nm,
               1 + COALESCE(c.interplanetary_consolidation, 0) AS maxp,
               c.command_center_upgrades AS ccu,
               s.name AS sys, cp.planet_num AS pn, cp.is_extractor AS ext,
               cp.products AS products, cp.sim_state AS sim_state
        FROM pp_characters c
        LEFT JOIN pp_char_planets cp ON cp.character_id = c.character_id
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        WHERE c.context_id = ? AND COALESCE(c.is_dummy, 0) = 0
    """, (context_id,)).fetchall()

    prod_count: dict[int, int] = {}   # product type_id -> deployed factory planet count
    supply: dict[int, float] = {}
    fleet_sys: set = set()
    occ: dict[int, set] = {}          # cid -> {(system, planet_num)}
    cap: dict[int, dict] = {}         # cid -> {nm, used, maxp}
    for r in rows:
        cap.setdefault(r["cid"], {"nm": r["nm"], "used": 0, "maxp": r["maxp"], "ccu": r["ccu"]})
        if r["pn"] is None:
            continue                  # char with no colonies (idle) — a row with null planet
        cap[r["cid"]]["used"] += 1
        if r["sys"]:
            fleet_sys.add(r["sys"])
            occ.setdefault(r["cid"], set()).add((r["sys"], r["pn"]))
        if not r["ext"] and r["products"]:
            for p in (_json.loads(r["products"]) or []):
                tid = p.get("type_id")
                if tid:
                    prod_count[tid] = prod_count.get(tid, 0) + 1
        elif r["ext"] and r["sim_state"]:
            ss = _json.loads(r["sim_state"] or "null")
            for o in ((ss or {}).get("outputs") or []):
                rate = o.get("rate_sustained", o.get("rate", 0)) or 0
                supply[o["type_id"]] = supply.get(o["type_id"], 0.0) + rate * 86400

    if not prod_count or not fleet_sys:
        if owns_con:
            con.close()
        return []

    # Balance one product's chain — the caller's chosen product if it exists, else the DOMINANT one (most
    # factory planets). F = its factories; D0 = one factory's per-material P1 demand. Effective output =
    # min(factories, the most-limiting input's supply ÷ D0): so a factory helps only while supply has
    # headroom, an extractor only while a material is binding.
    product = chosen_product if (chosen_product in prod_count) else max(prod_count, key=prod_count.get)
    F = prod_count[product]
    fr = _compute_p1_fracs(product, pi)
    ppd_fac = _effective_fph(product, pi) * 24.0
    D0 = {pid: ppd_fac * frac for pid, frac in fr.items() if ppd_fac * frac > 0}
    if not D0:
        if owns_con:
            con.close()
        return []
    pname = types.get(product, {}).get("name") or f"#{product}"
    occ_all = set().union(*occ.values()) if occ else set()   # planets any char already colonises

    free_planets: dict[int, list] = {}      # P0 planets per binding material, richest first
    ph = ",".join("?" * len(fleet_sys))
    for tid in D0:
        inputs = (sch.get(tid) or {}).get("inputs") or []
        p0 = types.get(inputs[0]["type_id"], {}).get("name") if inputs else None
        col = _p0_col(p0) if p0 else None
        if not col:
            free_planets[tid] = []
            continue
        vt = _P0_PLANET_TYPES.get(p0, [])
        tf = " AND planet_type IN ({})".format(",".join("?" * len(vt))) if vt else ""
        ps = con.execute(
            f'SELECT system, planet_num, planet_type, "{col}" AS r FROM pp_planets '
            f'WHERE "{col}" > 0{tf} AND system IN ({ph})', vt + list(fleet_sys)).fetchall()
        free_planets[tid] = sorted(
            [{"p0": p0, "system": p["system"], "planet_num": p["planet_num"],
              "planet_type": p["planet_type"], "richness": round(p["r"] or 0)} for p in ps],
            key=lambda x: ((x["system"], x["planet_num"]) in occ_all, -x["richness"]))   # empty first
    # Free factory planets: smallest types only (Barren/Temperate), in the fleet's systems, empties first.
    bt = con.execute(
        f"SELECT system, planet_num, planet_type FROM pp_planets "
        f"WHERE planet_type IN ('Barren','Temperate') AND system IN ({ph})", list(fleet_sys)).fetchall()
    factory_planets = sorted(
        [{"system": p["system"], "planet_num": p["planet_num"], "planet_type": p["planet_type"]} for p in bt],
        key=lambda x: (x["system"], x["planet_num"]) in occ_all)
    if owns_con:
        con.close()

    S = dict(supply)
    used: set = set()
    free_cap = {cid: max(0, c["maxp"] - c["used"]) for cid, c in cap.items()}
    PER = 7680.0                  # ~one 100%-richness extractor's P1/day (richness-scaled below)
    deploys: list[dict] = []
    f_add = 0

    def place(planets):
        """First placeable planet — a char with a free slot that already runs that system, else an idle
        toon joining the fleet. Returns (cid, planet) or None."""
        for pl in planets:
            key = (pl["system"], pl["planet_num"])
            if key in used:
                continue
            # a char can host it if it already runs that system (or is idle), and doesn't already have a
            # colony on that exact planet (one colony per planet per character).
            cid = next((c for c in free_cap if free_cap[c] > 0 and key not in occ.get(c, set()) and
                        (pl["system"] in {s for (s, _) in occ.get(c, set())} or not occ.get(c))), None)
            if cid is not None:
                return cid, pl
        return None

    while sum(free_cap.values()) > 0:
        bottleneck = min(S.get(m, 0.0) / D0[m] for m in D0)   # factories the supply can feed
        if F + f_add + 1e-9 < bottleneck:
            # Supply has headroom → another factory turns the surplus into more product.
            r = place(factory_planets)
            if not r:
                break
            cid, pl = r
            free_cap[cid] -= 1; used.add((pl["system"], pl["planet_num"])); f_add += 1
            host_ccu = cap[cid].get("ccu")
            # VERIFY the actual fit at this pilot's CCU on this planet type — don't just assume low CCU is
            # a problem. Only warn when the full layout genuinely won't fit (cramped = a later redeploy).
            fit_lp = _factory_fit_lp(product, pl["planet_type"], host_ccu)
            deploys.append({"kind": "factory", "char": cap[cid]["nm"], "system": pl["system"],
                            "planet_num": pl["planet_num"], "planet_type": pl["planet_type"],
                            "richness": None, "p0": None, "p1": pname, "add_per_day": round(ppd_fac),
                            "fed_pct": None, "host_ccu": host_ccu, "fit_lp": fit_lp,
                            "ccu_low": fit_lp < 3,
                            "train_to": _factory_full_ccu(product, pl["planet_type"]) if fit_lp < 3 else None})
        else:
            # A material is binding → an extractor for it lifts the bottleneck.
            m = min(D0, key=lambda x: S.get(x, 0.0) / D0[x])
            r = place(free_planets.get(m, []))
            if not r:
                break
            cid, pl = r
            free_cap[cid] -= 1; used.add((pl["system"], pl["planet_num"]))
            S[m] += PER * min(1.0, (pl["richness"] or 0) / 100.0)
            deploys.append({"kind": "extractor", "char": cap[cid]["nm"], "system": pl["system"],
                            "planet_num": pl["planet_num"], "planet_type": pl["planet_type"],
                            "richness": pl["richness"], "p0": pl["p0"],
                            "p1": types.get(m, {}).get("name") or f"#{m}",
                            "fed_pct": round(supply.get(m, 0.0) / (F * D0[m]) * 100) if F else 0})
    return deploys


@router.get("/api/expansion")
def expansion(pp_session: str = Cookie(default=None)):
    """Spare-capacity ADVICE for Setup Analysis: free slots + concrete 'deploy this here' cards per
    product (the dashboard now shows only the status counts and links here). Read-only / no plan."""
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"free_slots": 0}
    ex = _expansion_capacity(context_id)
    if (ex.get("free_slots") or 0) > 0:
        try:
            pi = load_pi_data()
            con = get_connection()
            prods = _setup_products(context_id, pi, con=con)
            ex["products"] = prods
            by_prod = {str(p["type_id"]): _expansion_deploys(context_id, pi, p["type_id"], con=con) for p in prods}
            con.close()
            ex["deploys_by_product"] = by_prod
            ex["deploys"] = by_prod.get(str(prods[0]["type_id"]), []) if prods else []
        except Exception:
            ex["deploys"] = []
    return ex


