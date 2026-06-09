"""
Fuel-block basket planner — the multi-product analogue of the single-product
planner in app.planner. The "product" is the whole FUEL_BLOCK_BOM rolled into one
combined P1 demand vector (so the extractor machinery is reused verbatim), and the
factory side runs one line per producible component instead of a single line.

Shares the DB / geo / extractor-assignment core with the single-product path via the
helpers imported from app.planner; only the budget (vectorised across the basket),
the factory placement (any planet type, per-line) and the manufacturing-ME headline
are fuel-block specific.
"""
from __future__ import annotations

from math import ceil

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import fuelblocks
from app.sde import load_pi_data, get_connection
from app.market import fetch_prices
from app.esi import require_context
from app.planner import (
    _build_char_list,
    _build_p0_p1_maps,
    _build_p1_info,
    _build_p1_info_raw,
    _compute_factory_shares,
    _factory_candidates,
    _fetch_planets_and_recs,
    _load_char_planet_config,
    _pick_factory_system,
    _run_extractor_pipeline,
    _set_computed_ext_cap,
    ensure_plan_tables,
)

router = APIRouter()

# Default factory planet types: Barren/Temperate are the smallest planets with the
# smallest link/power-grid footprint, so factory templates fit most easily there.
DEFAULT_FACTORY_PLANET_TYPES = ["Barren", "Temperate"]


def _available_factory_planet_types(con, req) -> list[str]:
    """Distinct planet types present in the factory-system/constellation scope, so the UI
    can offer only types that actually have planets to place factories on."""
    where, params = "", []
    fac_systems = list({*req.chosen_systems, req.factory_system}) if req.factory_system \
        else list(req.chosen_systems)
    if fac_systems:
        where = "WHERE system IN ({})".format(",".join("?" * len(fac_systems)))
        params = fac_systems
    elif req.constellations:
        where = "WHERE constellation IN ({})".format(",".join("?" * len(req.constellations)))
        params = list(req.constellations)
    try:
        rows = con.execute(
            f"SELECT DISTINCT planet_type FROM pp_planets {where} ORDER BY planet_type", params
        ).fetchall()
        return [r["planet_type"] for r in rows if r["planet_type"]]
    except Exception:
        return []


class FuelBlockPlanRequest(BaseModel):
    """Multi-product target: the whole fuel-block basket (FUEL_BLOCK_BOM). Same
    character/system inputs as PlanRequest, minus the single-product type_id and
    factory rate override (the basket has its own factory lines with their own rates)."""
    overproduction_pct: int = 10
    use_existing: bool = True
    constellations: list[str] = []
    preferred_systems: int = 1
    chosen_systems: list[str] = []
    factory_system: str = ""
    factory_character_ids: list[int] = []
    max_jumps: int = 1
    basket_id: int | None = None  # None = built-in fuel block; else a custom basket (pp_baskets)
    block_type: str = "Oxygen"  # cosmetic — PI side identical across racial blocks
    import_components: list[int] = []  # component type_ids to buy/import instead of produce
    # Allowed factory planet types (None → Barren/Temperate: smallest planets, least
    # link/power-grid footprint). Fuel-block lines are P1–P3 so any type physically works.
    factory_planet_types: list[str] | None = None
    # Manufacturing material efficiency (material only — build time ignored). Factors stack
    # MULTIPLICATIVELY: Engineering Complex hull (1%), the Structure Manufacturing ME rig
    # (T1 −2% / T2 −2.4% base × security multiplier), and blueprint ME. The rig's security
    # multiplier is derived from the manufacturing system's true security ("auto") or set
    # explicitly. Affects ONLY the block headline, not the PI production.
    structure_me_pct: float = 0.0
    rig_tier: str = "none"   # none | t1 | t2
    rig_space: str = "auto"  # auto | high | low | null
    bp_me_pct: float = 0.0


def _system_security(system: str | None):
    """Look up a solar system's security status from the SDE (system_geo). Returns None
    if unknown or the column/table is absent."""
    if not system:
        return None
    try:
        con = get_connection()
        row = con.execute("SELECT security FROM system_geo WHERE system=?", (system,)).fetchone()
        con.close()
        return row["security"] if row and row["security"] is not None else None
    except Exception:
        return None


def _compute_fuelblock_budget(
    char_list: list[dict],
    op_pct: int,
    components: list[dict],
    basket_p1_reqs: dict[int, float],
    pi_data: dict,
    preferred_cids: list[int] | None = None,
    cc_level: int = 5,
) -> tuple[int, int, list[dict], dict[int, int], bool, float]:
    """Vectorized analogue of planner._compute_slot_budget for the fuel-block basket.

    Works in P0-equivalent units exactly like the single-product model
    (kk = 48_000*24 P0/day per extractor slot; 150 P0 per P1). A "basket" is one
    FUEL_BLOCK_BOM (per 40 blocks). For X baskets/day, each factory line c needs
    F_c = X*qty_c/(rate_c*24) planets and the extractors need
    E = X*Σ(P1)*150*op/kk slots; we split the available planets in that ratio and
    derive the sustainable X (whichever side binds).

    Returns (ext_slots, factories_total, factory_lines, factory_shares, auto_mode, X).
    """
    factory_comps = [c for c in components if c["is_factory"]]
    kk = 48_000 * 24
    # Overproduction: extractors must supply op× the P1 need. Clamped to ≥1 (0%) — negative
    # overproduction only over-builds factories the extractors can't feed, collapsing output.
    op = max(1.0, 1 + op_pct / 100)
    sum_p1 = sum(basket_p1_reqs.values())

    rate_of: dict[int, float] = {}
    line_cap: dict[int, float] = {}  # baskets/day delivered per planet of this line
    for c in factory_comps:
        rate = fuelblocks.component_factory_rate(c["type_id"], pi_data, cc_level=cc_level)
        rate_of[c["type_id"]] = rate
        line_cap[c["type_id"]] = (rate * 24 / c["qty"]) if c["qty"] else 0.0

    # Same extractor/factory pool accounting + auto-mode swap as _compute_slot_budget.
    E = sum(
        (min(c["effective_planets"], c["extractor_limit"])
         if c["extractor_limit"] is not None else c["effective_planets"])
        for c in char_list if c["extractor_limit"] != 0
    )
    F = sum(
        c["effective_planets"] if c["extractor_limit"] == 0
        else max(0, c["effective_planets"] - c["extractor_limit"])
        for c in char_list if c["extractor_limit"] is not None
    )
    auto_mode = F == 0 and E > 0
    if auto_mode:
        F, E = E, 0
    N = E + F

    def _alloc(nfac: int) -> dict[int, int]:
        """Max-min fair split of nfac factory planets across lines: each planet goes to
        the line whose current output (baskets/day = count·rate·24/qty) is lowest, so
        identical lines get equal counts and every line gets ≥1 before any doubles up."""
        cnt = {c["type_id"]: 0 for c in factory_comps}
        for _ in range(nfac):
            cnt[min(cnt, key=lambda k: cnt[k] * line_cap[k])] += 1
        return cnt

    def _fac_baskets(cnt: dict[int, int]) -> float:
        return min((cnt[c["type_id"]] * line_cap[c["type_id"]] for c in factory_comps),
                   default=float("inf"))

    # Choose the factory/extractor split directly: factory output rises with the factory
    # count while extractor supply falls with E = N − F, so the sustainable basket rate
    # peaks at their crossover. Scoring by the REALISED (integer, max-min) factory output
    # rather than a continuous ratio avoids over-provisioning extractors when a lumpy line
    # (e.g. Robotics at ~12/hr) can't be split fractionally. Every line needs ≥1 planet to
    # make anything, so below len(lines) factory planets output is 0.
    min_fac = len(factory_comps)
    best_blocks, factories_total, counts = -1.0, 0, {c["type_id"]: 0 for c in factory_comps}
    if N > 0:
        f_lo = min_fac if (min_fac and N > min_fac) else 0
        f_hi = min(F, N) if min_fac == 0 else min(F, N - 1)  # keep ≥1 extractor for the P1s
        for nfac in range(f_lo, max(f_lo, f_hi) + 1):
            cnt = _alloc(nfac)
            fac_b = _fac_baskets(cnt)
            # Extractor-supplied baskets/day. Overproduction (op) makes each basket need op×
            # the P1 extraction, so the same extractor slots sustain fewer factory baskets —
            # shifting the optimal split toward more extractors (extractors overproduce).
            ext_b = (N - nfac) * kk / (sum_p1 * 150 * op) if sum_p1 > 0 else float("inf")
            blocks = min(fac_b, ext_b)
            if blocks > best_blocks:
                best_blocks, factories_total, counts = blocks, nfac, cnt
    ext_slots = N - factories_total
    X = best_blocks if best_blocks not in (-1.0, float("inf")) else 0.0

    factory_lines: list[dict] = []
    for c in factory_comps:
        cnt = counts.get(c["type_id"], 0)
        factory_lines.append({
            "type_id":       c["type_id"],
            "name":          c["name"],
            "tier":          c["tier"],
            "qty_per_run":   c.get("base_qty", c["qty"]),
            "rate_per_hour": round(rate_of[c["type_id"]], 3),
            "count":         cnt,
            "units_per_day": round(cnt * rate_of[c["type_id"]] * 24, 1),
        })

    factory_shares = _compute_factory_shares(
        char_list, factories_total, auto_mode, None, preferred_cids
    )
    return ext_slots, factories_total, factory_lines, factory_shares, auto_mode, X


def _assign_fuelblock_factories(
    assignments: list[dict],
    char_list: list[dict],
    factory_lines: list[dict],
    factory_shares: dict[int, int],
    auto_mode: bool,
    fac_pool: list[dict],
    best_fac_system: str | None,
    char_nonfac: dict[int, list],
    req,
    has_system_name: bool,
    fallback_planet_type: str = "Barren",
) -> int:
    """Place the per-line factory planets across characters, tagging each with its
    component product. Fuel-block factories are P1/P2/P3 → they run on any of the chosen
    factory planet types (the pool is already restricted to those). When no concrete DB
    planet is available, the slot is tagged with `fallback_planet_type` (the first chosen
    type) so the generated template still has a real planet type/diameter.
    Mutates assignments; returns the number of factory planets that couldn't be placed."""
    # Expand the per-line counts into a tagged queue (clusters same-component planets).
    queue: list[dict] = []
    for line in factory_lines:
        for _ in range(line["count"]):
            queue.append({"type_id": line["type_id"], "name": line["name"], "tier": line["tier"]})
    qi = 0

    for asgn, char in zip(assignments, char_list):
        cid = char["character_id"]
        share = factory_shares.get(cid, 0)
        cap = max(0, char["effective_planets"] - len(asgn["extractors"]))
        n = min(share, cap)

        char_used = {
            (e["system"], e["planet_num"]) for e in asgn["extractors"]
            if e.get("system") and e.get("planet_num") is not None
        }
        nonfac = [
            p for p in char_nonfac.get(cid, [])
            if (p.get("system_name"), p.get("planet_num")) not in char_used
        ] if req.use_existing else []

        fac_assigns: list[dict] = []
        for _ in range(n):
            if qi >= len(queue):
                break
            comp = queue[qi]
            qi += 1
            existing = None
            if has_system_name and nonfac and best_fac_system:
                for idx, cp in enumerate(nonfac):
                    if cp.get("system_name") == best_fac_system:
                        existing = nonfac.pop(idx)
                        break
            if existing is None and nonfac:  # any spare existing non-extractor planet
                existing = nonfac.pop(0)
            if existing is not None:
                key = (existing.get("system_name"), existing.get("planet_num"))
                if key[0] and key[1] is not None:
                    char_used.add(key)
                fac_assigns.append({
                    "system":      existing.get("system_name") or best_fac_system,
                    "planet_num":  existing.get("planet_num"),
                    "planet_type": existing.get("planet_type", "?"),
                    "is_existing": True, "is_replace": False, "is_new": False,
                    "product":     comp,
                })
            else:
                planet = next(
                    (p for p in fac_pool
                     if (best_fac_system is None or p["system"] == best_fac_system)
                     and (p["system"], p["planet_num"]) not in char_used),
                    None,
                )
                if planet is not None:
                    char_used.add((planet["system"], planet["planet_num"]))
                    fac_assigns.append({
                        "system": planet["system"], "planet_num": planet["planet_num"],
                        "planet_type": planet["planet_type"],
                        "is_existing": False, "is_replace": False, "is_new": True,
                        "product": comp,
                    })
                else:  # no concrete DB planet to pin — tag with the first chosen type
                    fac_assigns.append({
                        "system": best_fac_system, "planet_num": None,
                        "planet_type": fallback_planet_type,
                        "is_existing": False, "is_replace": False, "is_new": True,
                        "product": comp,
                    })

        asgn["factory_assignments"] = fac_assigns
        asgn["factory_planets"] = len(fac_assigns)
        asgn["free_planets"] = max(
            0, char["effective_planets"] - len(asgn["extractors"]) - len(fac_assigns)
        )

    return len(queue) - qi


def _resolve_target_basket(req: "FuelBlockPlanRequest", pi_data: dict):
    """Resolve the planning target into (all_components, meta).

    Built-in fuel block when req.basket_id is None: the BOM is ME-adjusted (manufacturing
    material efficiency reduces the PI consumed per block) and carries racial-block pricing.
    A custom basket (pp_baskets) has no manufacturing step → no ME, and is priced by the
    summed market value of its produced components. Returns (None, None) if the basket is
    missing/empty. meta keys: is_fuelblock, config_type_id, run_size, unit_label, name,
    effective_me_pct, mfg (None for custom), block_price_id (None for custom)."""
    types = pi_data["types"]

    if req.basket_id:
        from app.admin import ensure_basket_tables, BASKET_CONFIG_BASE
        ensure_basket_tables()
        con = get_connection()
        brow = con.execute("SELECT * FROM pp_baskets WHERE id=?", (req.basket_id,)).fetchone()
        items = con.execute(
            "SELECT type_id, qty FROM pp_basket_items WHERE basket_id=?", (req.basket_id,)
        ).fetchall() if brow else []
        con.close()
        if not brow or not items:
            return None, None
        all_components = fuelblocks.resolve_basket_components(
            [{"type_id": r["type_id"], "qty": r["qty"]} for r in items], pi_data)
        for c in all_components:
            c["base_qty"] = c["qty"]  # no ME for custom baskets
        meta = {
            "is_fuelblock":     False,
            "config_type_id":   BASKET_CONFIG_BASE + brow["id"],
            "run_size":         brow["run_size"] or 1,
            "unit_label":       brow["unit_label"] or "sets",
            "name":             brow["name"],
            "effective_me_pct": 0.0,
            "mfg":              None,
            "block_price_id":   None,
        }
        return all_components, meta

    # Built-in fuel block. Manufacturing material efficiency (material only) stacks the
    # structure hull, rig and blueprint factors MULTIPLICATIVELY and reduces PI consumed per
    # block for every material EXCEPT one at the per-run minimum (base qty 1 = Robotics, can't
    # drop below 1 per run), so the same PI output makes more blocks. Rig ME % is resolved
    # from the rig tier + the manufacturing system's security.
    all_components = fuelblocks.resolve_bom(pi_data)
    sec_system = req.factory_system or (req.chosen_systems[0] if req.chosen_systems else None)
    rig_me_pct, sec_label = fuelblocks.resolve_rig(
        req.rig_tier, req.rig_space, _system_security(sec_system))
    me_keep = fuelblocks.me_keep_factor(req.structure_me_pct, rig_me_pct, req.bp_me_pct)
    effective_me_pct = round((1.0 - me_keep) * 100.0, 2)
    fuelblocks.apply_effective_me(all_components, me_keep)
    _block_ids = {"oxygen": 4312, "helium": 4247, "nitrogen": 4051, "hydrogen": 4246}
    meta = {
        "is_fuelblock":     True,
        "config_type_id":   fuelblocks.FUEL_BLOCK_CONFIG_TYPE_ID,
        "run_size":         fuelblocks.BLOCKS_PER_RUN,
        "unit_label":       "fuel blocks",
        "name":             f"{req.block_type} Fuel Block",
        "effective_me_pct": effective_me_pct,
        "mfg": {
            "structure_me_pct": req.structure_me_pct, "rig_tier": req.rig_tier,
            "rig_space": req.rig_space, "rig_me_pct": round(rig_me_pct, 2),
            "sec_label": sec_label, "sec_system": sec_system,
            "bp_me_pct": req.bp_me_pct, "effective_pct": effective_me_pct,
        },
        "block_price_id":   _block_ids.get(req.block_type.lower()),
    }
    return all_components, meta


def _run_fuelblock_plan(req: "FuelBlockPlanRequest", context_id: int) -> dict:
    """Multi-product planner for a basket (built-in fuel block or a custom pp_baskets one).
    The 'product' is the whole basket rolled into one combined P1 demand vector (so the
    extractor machinery is reused verbatim), and the factory side runs one line per
    producible component."""
    pi_data = load_pi_data()
    types = pi_data["types"]

    all_components, meta = _resolve_target_basket(req, pi_data)
    if all_components is None:
        return {"error": "Basket not found or empty"}
    import_ids = set(req.import_components or [])
    config_type_id = meta["config_type_id"]
    effective_me_pct = meta["effective_me_pct"]

    # Imported components are bought/hauled in — excluded from BOTH the factory lines and
    # the P1 roll-down (no extractor demand for their sub-chain), freeing those planets.
    components = [c for c in all_components if c["type_id"] not in import_ids]
    imported_comps = [c for c in all_components if c["type_id"] in import_ids]
    basket_p1 = fuelblocks.compute_basket_p1_reqs(components, pi_data)
    if not fuelblocks.compute_basket_p1_reqs(all_components, pi_data):
        return {"error": "Could not resolve the basket recipe from SDE"}

    _, p1_to_p0 = _build_p0_p1_maps(pi_data)

    ensure_plan_tables()
    con = get_connection()

    char_rows, planet_rows, has_system_name, config_map = _load_char_planet_config(
        con, context_id, config_type_id)

    # Combined P1 info (heaviest first), each P1 mapped to its source P0.
    sorted_p1, p1_info_raw, all_p0_names = _build_p1_info_raw(basket_p1, p1_to_p0, types)

    p0_planet_lists, p0_planet_lists_global, best_ptypes, sys_recs = _fetch_planets_and_recs(
        con, all_p0_names, req, types, p1_info_raw)

    # Factory candidates restricted to the chosen planet types (default Barren/Temperate:
    # smallest footprint). Fuel-block lines are P1/P2/P3, so any type physically works.
    allowed_types = req.factory_planet_types or list(DEFAULT_FACTORY_PLANET_TYPES)
    fac_pool, factory_system_options, sys_fac_capacity = _factory_candidates(
        con, req, allowed_types=allowed_types)
    for rec in sys_recs:
        rec["factory_capacity"] = {s: sys_fac_capacity.get(s, 0) for s in rec["systems_needed"]}

    # Planet types actually present in the factory scope, so the UI can show which are
    # selectable (and grey out types with no planets in the chosen systems/constellations).
    available_planet_types = _available_factory_planet_types(con, req)

    con.close()

    char_planets: dict[int, list] = {}
    for p in planet_rows:
        char_planets.setdefault(p["character_id"], []).append(dict(p))

    p1_info = _build_p1_info(p1_info_raw, best_ptypes, types)
    char_list = _build_char_list(char_rows, config_map, char_planets, with_ccu=True)

    # Factories pack best on the highest command-centre levels, so order characters by
    # effective CCU (stable) — auto-consolidation then lands factory planets on the best
    # command centres first, and extraction is unaffected by ordering.
    char_list.sort(key=lambda c: -c["effective_ccu"])

    # Size the budget at the best available CC level (factories prefer the best planets);
    # the realised per-line output below corrects for any planets that land on lower CCs.
    plan_cc = max((c["effective_ccu"] for c in char_list), default=5)

    ext_slots, factories_total, factory_lines, factory_shares, auto_mode, baskets_per_day = \
        _compute_fuelblock_budget(
            char_list, req.overproduction_pct, components, basket_p1, pi_data,
            preferred_cids=req.factory_character_ids, cc_level=plan_cc,
        )

    ext_slots = min(ext_slots, _set_computed_ext_cap(char_list, factory_shares, auto_mode))

    # Stats: combined P0/day at the planned basket rate. The components are already in their
    # effective per-run consumption, so output units = baskets × run_size directly.
    fuel_blocks_per_day = round(baskets_per_day * meta["run_size"])
    p0_per_day = round(baskets_per_day * sum(basket_p1.values()) * 150)
    needed_at_baseline = ceil(p0_per_day / 48_000) if p0_per_day > 0 else sum(round(q) for _, q in sorted_p1)

    has_planet_db = any(v for v in p0_planet_lists.values())

    assignments, remaining, char_nonfac = _run_extractor_pipeline(
        req, char_list, p1_info, ext_slots, needed_at_baseline,
        p0_planet_lists, p0_planet_lists_global, has_planet_db, has_system_name,
        auto_mode, assignment_extra=lambda c: {"effective_ccu": c["effective_ccu"]},
    )

    # Pick best factory system (most candidate planets among chosen/all).
    sys_fac_count: dict[str, int] = {}
    for p in fac_pool:
        sys_fac_count[p["system"]] = sys_fac_count.get(p["system"], 0) + 1
    best_fac_system = _pick_factory_system(req, sys_fac_count)

    unplaced_factories = _assign_fuelblock_factories(
        assignments, char_list, factory_lines, factory_shares, auto_mode,
        fac_pool, best_fac_system, char_nonfac, req, has_system_name,
        fallback_planet_type=allowed_types[0] if allowed_types else "Barren",
    )

    # Realised factory output: each placed factory planet runs at its host character's
    # effective CCU, so with mixed command-centre levels the per-line throughput (and
    # the sustainable basket rate) reflect where the planets actually landed.
    ccu_by_cid = {c["character_id"]: c["effective_ccu"] for c in char_list}
    bom_qty = {c["type_id"]: c["qty"] for c in components}
    line_units: dict[int, float] = {l["type_id"]: 0.0 for l in factory_lines}
    line_planets: dict[int, int] = {l["type_id"]: 0 for l in factory_lines}
    for a in assignments:
        host_ccu = ccu_by_cid.get(a["character_id"], 5)
        for f in a.get("factory_assignments", []):
            tid = f["product"]["type_id"]
            rate = fuelblocks.component_factory_rate(tid, pi_data, cc_level=host_ccu)
            f["ccu"] = host_ccu
            f["rate_per_hour"] = round(rate, 2)
            line_units[tid] = line_units.get(tid, 0.0) + rate * 24
            line_planets[tid] = line_planets.get(tid, 0) + 1
    for l in factory_lines:
        t = l["type_id"]
        l["count"] = line_planets.get(t, 0)
        l["units_per_day"] = round(line_units.get(t, 0.0), 1)
        if line_planets.get(t):
            l["rate_per_hour"] = round(line_units[t] / 24 / line_planets[t], 2)
    fac_baskets = min((line_units[t] / bom_qty[t] for t in line_units if bom_qty.get(t)),
                      default=float("inf"))
    _sum_p1 = sum(basket_p1.values())
    ext_baskets = ext_slots * 48_000 * 24 / (_sum_p1 * 150) if _sum_p1 > 0 else 0.0
    realized_baskets = min(fac_baskets, ext_baskets)
    if realized_baskets != float("inf"):
        baskets_per_day = realized_baskets
        fuel_blocks_per_day = round(baskets_per_day * meta["run_size"])
        p0_per_day = round(baskets_per_day * _sum_p1 * 150)
    # Per-line production needed (effective, ME-adjusted) to feed the manufactured blocks.
    for l in factory_lines:
        l["need_per_day"] = round(baskets_per_day * bom_qty.get(l["type_id"], 0))

    all_assignments = sorted(assignments, key=lambda a: a["character_name"])
    total_extractors = sum(len(a["extractors"]) for a in all_assignments)
    total_factory_planets = sum(a["factory_planets"] for a in all_assignments)

    # Factory planets that wanted a real planet in the factory system but couldn't get one
    # of the allowed types (tagged with the fallback type, planet_num=None). This is the
    # "too few factory planets" shortage — it clears when the user widens the planet types
    # (or scans more planets). Only meaningful when a concrete factory system was chosen.
    factory_planets_unpinned = sum(
        1 for a in all_assignments for f in a.get("factory_assignments", [])
        if f.get("planet_num") is None
    ) if best_fac_system else 0

    quality_vals = [
        slot["quality_pct"] for a in all_assignments for slot in a["extractors"]
        if slot.get("quality_pct") is not None
    ]
    avg_quality_pct = round(sum(quality_vals) / len(quality_vals)) if quality_vals else None
    avg_p0_per_cycle = round(avg_quality_pct / 100 * 48000) if avg_quality_pct else None
    _actual_p0_per_day = sum(
        slot.get("quality_pct", 100) / 100 * 48_000 * 24
        for a in all_assignments for slot in a["extractors"]
    )

    # ISK estimate (best-effort). Fuel block → its sell price × blocks/day. Custom basket
    # has no single final product → summed market value of the produced components/day.
    if meta["is_fuelblock"]:
        _bid = meta["block_price_id"]
        unit_price = fetch_prices([_bid]).get(_bid, 0.0) if _bid else 0.0
        isk_per_day = round(fuel_blocks_per_day * unit_price, 2)
        sell_price = round(unit_price, 2)
    else:
        _prices = fetch_prices([c["type_id"] for c in components])
        isk_per_day = round(sum(baskets_per_day * c["qty"] * _prices.get(c["type_id"], 0.0)
                                for c in components), 2)
        sell_price = 0.0

    result = {
        "fuelblock":             True,
        "block_type":            req.block_type if meta["is_fuelblock"] else None,
        "unit_label":            meta["unit_label"],
        "product":               {"type_id": config_type_id, "name": meta["name"]},
        "bom":                   [
            {"type_id": c["type_id"], "name": c["name"], "qty": c["base_qty"],
             "tier": c["tier"], "is_factory": c["is_factory"],
             "imported": c["type_id"] in import_ids}
            for c in all_components
        ],
        "imported":              [
            {"type_id": c["type_id"], "name": c["name"], "qty_per_run": c["base_qty"],
             "tier": c["tier"], "units_per_day": round(baskets_per_day * c["qty"])}
            for c in imported_comps
        ],
        "fuel_blocks_per_day":   fuel_blocks_per_day,
        "factory_lines":         factory_lines,
        "p1_requirements":       p1_info,
        "total_extractors_base": round(sum(q for _, q in sorted_p1)),
        "ext_slots":             ext_slots,
        "assignments":           all_assignments,
        "unassigned":            remaining,
        "unplaced_factories":    unplaced_factories,
        "system_recommendations": sys_recs,
        "chosen_systems":        req.chosen_systems,
        "factory_character_ids": req.factory_character_ids,
        "factory_system":        best_fac_system,
        "factory_system_options": factory_system_options,
        "factory_planet_types":  allowed_types,
        "available_planet_types": available_planet_types,
        "factory_planets_unpinned": factory_planets_unpinned,
        "factory_planets_needed": total_factory_planets,
        "factory_planets_by_system": [
            {"system": s, "count": c, "type": "Any"}
            for s, c in sorted(sys_fac_count.items(), key=lambda x: -x[1])
        ],
        "stats": {
            "fuel_blocks_per_day":      fuel_blocks_per_day,
            "blocks_per_run":           meta["run_size"],
            "unit_label":               meta["unit_label"],
            "factories":                factories_total,
            "products_per_day":         fuel_blocks_per_day,
            "isk_per_day":              isk_per_day,
            "sell_price":               sell_price,
            "p0_per_day":               p0_per_day,
            "total_extractors":         total_extractors,
            "avg_quality_pct":          avg_quality_pct,
            "avg_p0_per_cycle":         avg_p0_per_cycle,
            "actual_p0_per_day":        round(_actual_p0_per_day),
            "overproduction_pct":       max(0, req.overproduction_pct),
            "plan_cc":                  plan_cc,
            "ccu_mixed":                len({c["effective_ccu"] for c in char_list}) > 1,
            "material_efficiency_pct":  effective_me_pct,
        },
    }
    if meta["mfg"]:
        result["mfg"] = meta["mfg"]
    return result


@router.post("/api/plan-fuelblock")
def compute_fuelblock_plan(req: FuelBlockPlanRequest, context_id: int = Depends(require_context)):
    return _run_fuelblock_plan(req, context_id)


@router.get("/api/fuelblock-bom")
def fuelblock_bom():
    """The fuel-block basket components — drives the wizard's import toggles."""
    pi_data = load_pi_data()
    return {"components": [
        {"type_id": c["type_id"], "name": c["name"], "qty": c["qty"],
         "tier": c["tier"], "is_factory": c["is_factory"]}
        for c in fuelblocks.resolve_bom(pi_data)
    ]}
