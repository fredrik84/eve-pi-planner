"""
Fuel Block bill-of-materials helpers.

Fuel blocks are not a single PI product — they are a fixed-ratio basket of 5 PI
components. The PI side is identical across all four racial fuel blocks (only the
non-PI ice/isotope differs), so a single BOM covers them all.

The basket is modelled as one integrated dependency DAG: components share P0/P1
sub-chains (e.g. Robotics consumes Mechanical Parts; Precious Metals feeds Mech
Parts, Enriched Uranium *and* Robotics), so the combined P1 demand is the weighted
sum of every component's roll-down to tier-1 — NOT five independent plans.

Extractors produce the 7 underlying P1 types; factory planets import P1 and chain
up internally. Oxygen is itself a P1, so it ships straight from the extractor with
no factory line.
"""
from __future__ import annotations

from fractions import Fraction

from app import layout

# Per 40 fuel blocks (one blueprint run). Component name -> qty consumed.
# Verified against the SDE fuel block blueprints (Oxygen/Helium/Nitrogen/Hydrogen
# are identical on the PI side).
FUEL_BLOCK_BOM: dict[str, int] = {
    "Oxygen": 22,            # P1 — extractor-only, ships directly
    "Coolant": 9,            # P2 — factory line
    "Mechanical Parts": 4,   # P2 — factory line
    "Enriched Uranium": 4,   # P3 — factory line
    "Robotics": 1,           # P3 — factory line
}
BLOCKS_PER_RUN = 40

# Sentinel product_type_id used to store/restore per-character config (pp_plan_config)
# for the fuel-block target through the existing single-product machinery. This is the
# real Oxygen Fuel Block type id; the PI side is racial-agnostic so any one works.
FUEL_BLOCK_CONFIG_TYPE_ID = 4312


def resolve_bom(pi_data: dict) -> list[dict]:
    """Resolve FUEL_BLOCK_BOM names to {type_id, name, qty, tier, is_factory}.

    is_factory marks components that need a factory planet (tier > 1); tier-1
    components (Oxygen) are produced directly by extractors.
    """
    name_to_id = pi_data["name_to_id"]
    types = pi_data["types"]
    out: list[dict] = []
    for name, qty in FUEL_BLOCK_BOM.items():
        tid = name_to_id.get(name.lower())
        if tid is None:
            raise ValueError(f"Fuel block component not found in SDE: {name}")
        tier = types.get(tid, {}).get("pi_tier")
        out.append({
            "type_id": tid,
            "name": name,
            "qty": qty,
            "tier": tier,
            "is_factory": tier is not None and tier > 1,
        })
    return out


def resolve_basket_components(items: list[dict], pi_data: dict) -> list[dict]:
    """Resolve a custom basket's items to the same component shape as resolve_bom().

    items: [{type_id, qty}, ...] (qty per basket run). Each must be a PI type at tier ≥ 1
    (P1–P4); tier-1 components are extractor-direct, tier > 1 need a factory line. Unknown
    or tier-0 (P0) types are skipped — the admin API validates before storing, this is a
    defensive backstop.
    """
    types = pi_data["types"]
    out: list[dict] = []
    for it in items:
        tid = it["type_id"]
        t = types.get(tid, {})
        tier = t.get("pi_tier")
        if not tier:  # None or 0 → not a producible PI commodity
            continue
        out.append({
            "type_id": tid,
            "name": t.get("name", "?"),
            "qty": it["qty"],
            "tier": tier,
            "is_factory": tier > 1,
        })
    return out


def _trace_p1(tid: int, mult: Fraction, pi_data: dict) -> dict[int, Fraction]:
    """Recursively roll a type down to its tier-1 (P1) inputs, scaled by mult.

    Mirrors planner._compute_p1_fracs's trace, kept here so the basket can sum
    across components. A tier-1 input returns itself; tier-0/unknown returns {}.
    """
    types, schematics = pi_data["types"], pi_data["schematics"]
    tier = types.get(tid, {}).get("pi_tier")
    if tier == 1:
        return {tid: mult}
    if not tier:
        return {}
    sch = schematics.get(tid)
    if not sch:
        return {}
    out_q = sch["output_qty"]
    res: dict[int, Fraction] = {}
    for inp in sch["inputs"]:
        sub = _trace_p1(inp["type_id"], Fraction(inp["quantity"], out_q) * mult, pi_data)
        for k, v in sub.items():
            res[k] = res.get(k, Fraction(0)) + v
    return res


def compute_basket_p1_reqs(components: list[dict], pi_data: dict) -> dict[int, float]:
    """Combined P1 (tier-1) demand for the whole basket.

    Returns {p1_type_id: units_per_basket} where a "basket" is the FUEL_BLOCK_BOM
    quantities (i.e. per 40 fuel blocks). Each component's roll-down to tier-1 is
    weighted by its BOM qty and summed; shared sub-chains merge naturally so a P1
    feeding several components is counted once with its total demand.
    """
    total: dict[int, Fraction] = {}
    for comp in components:
        for k, v in _trace_p1(comp["type_id"], Fraction(comp["qty"]), pi_data).items():
            total[k] = total.get(k, Fraction(0)) + v
    return {k: float(v) for k, v in total.items()}


_PACKED_RATE_CACHE: dict = {}   # (type_id, planet_type, cc_level) -> product_per_hour


def _packed_rate(type_id: int, planet_type: str, cc_level: int) -> float:
    """Per-PLANET output/hr of one fully-packed compact factory = single-facility
    rate × max facilities that fit the command-centre budget at `cc_level`. Heavy
    (builds templates up to ~40 times). Delegates to generate_layout so the budget
    and the importable Factory Layout templates agree on throughput.

    Backed by the same Redis-shared cache as planner.py's layout-fit functions (L1
    process dict + L2 Redis, see _layout_cache_get_or_compute) — this call was missed
    by the 2026-07-06 fix (it lived behind a plain in-process @lru_cache, cold on every
    pod restart, never shared across replicas) despite running on EVERY plan/
    recommendation request via _compute_fuelblock_budget, not just when a system is
    actually chosen. Local import to avoid a circular import (planner.py doesn't import
    from this module, but keeping this defensive matches the pattern used elsewhere in
    this codebase for cross-module helper reuse)."""
    from app.planner import _layout_cache_get_or_compute
    key = (type_id, planet_type, cc_level)

    def compute():
        return layout.generate_layout(type_id, planet_type, cc_level=cc_level)["summary"]["product_per_hour"]

    return _layout_cache_get_or_compute("packed_rate", _PACKED_RATE_CACHE, key, compute)


def component_factory_rate(type_id: int, planet_type: str = "Barren",
                           cc_level: int = 5) -> float:
    """Per-planet effective output/hr for a packed factory of this component at the
    given command-centre level.

    Lower CC levels fit fewer facilities, so the rate drops (e.g. Coolant ~110/hr at
    CC5, ~95/hr at CC4). Used so the plan's factory-planet counts reflect each
    character's actual command-centre upgrades.
    """
    return _packed_rate(type_id, planet_type, max(1, min(5, int(cc_level))))


# ── Manufacturing material efficiency (fuel-block headline only) ────────────────
# Structure Manufacturing ME rig base reductions (the rig family that covers fuel
# blocks) and the security multiplier applied to all structure rigs. ME affects ONLY
# the block headline (how many blocks the same PI output manufactures), not PI production.
_RIG_BASE = {"none": 0.0, "t1": 2.0, "t2": 2.4}
_SEC_MULT = {"high": 1.0, "low": 1.9, "null": 2.1}


def sec_band(security) -> tuple[str, float, str]:
    """Classify a system security into (space_key, rig multiplier, label)."""
    r = round(security, 1)
    if r >= 0.5:
        return "high", 1.0, "high-sec"
    if r > 0.0:
        return "low", 1.9, "low-sec"
    return "null", 2.1, "null/WH"


def resolve_rig(rig_tier: str, rig_space: str, sec) -> tuple[float, str]:
    """Resolve the Structure Manufacturing ME rig's material-efficiency % and a security
    label. rig_space='auto' derives the multiplier from the system security (sec, from the
    SDE); else it's explicit. sec=None (unknown system) falls back to high-sec."""
    base = _RIG_BASE.get((rig_tier or "none").lower(), 0.0)
    if (rig_space or "auto").lower() == "auto":
        if sec is None:
            mult, label = 1.0, "auto (no system → high-sec)"
        else:
            _key, mult, band = sec_band(sec)
            label = f"auto · {band}"
    else:
        mult = _SEC_MULT.get(rig_space.lower(), 1.0)
        label = {"high": "high-sec", "low": "low-sec", "null": "null/WH"}.get(rig_space.lower(), rig_space)
    return base * mult, label


def me_keep_factor(*me_pcts: float) -> float:
    """Multiplicative material-efficiency 'keep' fraction (1.0 = no reduction). The ME
    factors (structure hull, rig, blueprint) stack multiplicatively; each % is clamped to
    [0, 90] and the product is floored at 0.1 (a 90% total-reduction cap)."""
    keep = 1.0
    for v in me_pcts:
        keep *= 1.0 - max(0.0, min(90.0, v)) / 100.0
    return max(0.1, keep)


def apply_effective_me(components: list[dict], me_keep: float) -> None:
    """Stamp base_qty and set the effective per-run qty = max(1, base × me_keep) on each
    component. The per-run minimum of 1 (EVE's max(runs, …) rule) means a base-1 component
    (Robotics) gets NO ME benefit — verified against in-game data. The same PI output then
    makes more blocks, and the factory balance correctly shifts toward relatively more
    Robotics."""
    for c in components:
        c["base_qty"] = c["qty"]
        c["qty"] = max(1.0, c["qty"] * me_keep)
