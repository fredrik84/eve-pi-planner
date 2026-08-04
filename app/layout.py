"""
Factory layout generator.

Produces a single **self-contained factory planet** for a chosen PI product:
one importable EVE Planetary Interaction template that runs the full P1 -> Px
chain on ONE planet, with every tier linked directly to the next.

Topology (the proven compact design, e.g. a SHPC P4 factory):

    Launchpad ── P4 plant ── P3 facility ── P2 facilities ...
                      └────── P3 facility ── P2 facilities ...
                      └────── P3 facility ── P2 facilities ...

- P1 is imported into the launchpad and routed *down through* the P4/P3 pins to
  the P2 facilities that consume it (pins pass through commodities they don't use).
- Each tier's output is routed *up* to the next tier (P2 -> P3 -> P4), and the
  final product is routed back to the launchpad for export.

Facility ratio (compact, fits one level-5 command center):
- 1 facility of the product.
- For a P4 product: 1 facility per P3 input, and 2 facilities per P2 input of each
  P3. P2->P3 is fully balanced (2 x 5/hr = 10/hr = one P3's consumption); the P4
  runs at partial rate (3/hr supplied vs 6/hr wanted) — the standard single-planet
  tradeoff, exactly matching a hand-built SHPC factory.

EVE template JSON (one planet):
    { "CmdCtrLv","Cmt","Diam","Pln",
      "P":[{"La","Lo","H","S"(OUTPUT product type_id|null),"T"(structure type_id)}],
      "L":[{"S"(srcPinIdx),"D"(dstPinIdx),"Lv"}],            # 1-based pin indices
      "R":[{"P":[pinIdx,...path],"Q"(qty/cycle),"T"(commodity type_id)}] }

NOTE: the pin "S" field is the produced item's *type id* (e.g. 9832 Coolant,
2872 SHPC), NOT the SDE schematic_id — verified against real in-game templates.
"""

import math
from typing import Optional

from app.sde import load_pi_data

CMD_CTR_LEVEL = 5

# Keep this share of the command-centre budget FREE in anything we generate. Filling a planet to
# 100% is a trap: our link/spoke costs are an estimate off idealised pin coordinates, the player's
# real placement never matches to the watt, and EVE itself advises leaving ~10% spare. A template
# that fits on paper and not in the client is worse than one facility fewer. Applies to every
# fitting decision (basics, heads, packed factory units) AND to the min_cc advice, so what we
# recommend, what we export, and what the planner assumes can't drift apart.
FIT_HEADROOM = 0.10

# ── CPU / Powergrid model (EVE values; see EVE University) ────────────────────
# Command Center provides (cpu, pg) per upgrade level.
CC_BUDGET = {
    0: (1675, 6000), 1: (7057, 9000), 2: (12136, 12000),
    3: (17215, 15000), 4: (21315, 17000), 5: (25415, 19000),
}
# Per-structure usage (cpu, pg) keyed by the role in `_structure_ids`.
STRUCT_COST = {
    "launchpad": (3600, 700), "basic_if": (200, 800), "adv_if": (500, 700),
    "hitech_if": (1100, 400), "storage": (500, 700), "ecu": (400, 2600),
}
# Per extractor head (the ECU pin's "H"). FLAT — it depends on the number of heads and nothing
# else. It was briefly modelled as a "spoke" whose PG scaled with the planet radius, calibrated to
# one measured Gas CC5 build; that was wrong twice over. EVE charges heads a fixed 110 CPU / 550 PG
# each, and the residual PG that calibration was chasing is LINK cost we were under-counting by
# assuming a Gas planet is Ø40000 (see PLANET_DIAM). Because the invented term grew without bound
# with the radius, feeding a real gas giant's diameter in collapsed the template from 8 basics to 1.
# Planet size belongs in the link model below, where EVE actually puts it.
HEAD_COST = (110, 550)
# A link of length l km costs (cpu, pg) = (15 + 0.2*l, 10 + 0.15*l).
LINK_CPU_BASE, LINK_CPU_PER_KM = 15.0, 0.2
LINK_PG_BASE, LINK_PG_PER_KM = 10.0, 0.15


def compute_resources(template: dict, struct: dict) -> dict:
    """Estimate CPU/PG used by a template vs its command-centre budget.

    Distances use the template's assumed planet diameter (real planets vary, so this
    is an estimate); structures dominate and are exact. Links use the EVE formula on
    the raw lat/lon plane (EVE treats the surface as flat for placement)."""
    role_of = {tid: role for role, tid in struct.items() if role in STRUCT_COST}
    radius = template.get("Diam", 8000.0) / 2.0
    head_cpu, head_pg = HEAD_COST                     # flat per head — planet size affects links only
    cpu = pg = 0.0
    for p in template["P"]:
        role = role_of.get(p["T"])
        if not role:
            continue
        c, g = STRUCT_COST[role]
        cpu += c; pg += g
        if role == "ecu":
            heads = p.get("H", 0) or 0
            cpu += heads * head_cpu; pg += heads * head_pg
    for l in template["L"]:
        a, b = template["P"][l["S"] - 1], template["P"][l["D"] - 1]
        km = math.hypot(a["La"] - b["La"], a["Lo"] - b["Lo"]) * radius
        cpu += LINK_CPU_BASE + LINK_CPU_PER_KM * km
        pg += LINK_PG_BASE + LINK_PG_PER_KM * km
    cpu_max, pg_max = CC_BUDGET.get(template.get("CmdCtrLv", 5), CC_BUDGET[5])
    return {
        "cpu": round(cpu), "pg": round(pg), "cpu_max": cpu_max, "pg_max": pg_max,
        "cpu_pct": round(cpu / cpu_max * 100), "pg_pct": round(pg / pg_max * 100),
        # `over` = physically impossible (>100%). `over_fit` = past the headroom we build to —
        # buildable, but tighter than we're willing to ship. Every fitting loop uses over_fit.
        "over": cpu > cpu_max or pg > pg_max,
        "over_fit": not _fits(cpu, pg, cpu_max, pg_max),
        "cc_level": template.get("CmdCtrLv", 5),
        "headroom_pct": round(FIT_HEADROOM * 100),
        "min_cc": min_cc_for(cpu, pg), "binding": "cpu" if cpu / cpu_max > pg / pg_max else "pg",
    }


def _fits(cpu: float, pg: float, cpu_max: float, pg_max: float) -> bool:
    """Does this draw fit the budget with FIT_HEADROOM to spare?"""
    room = 1.0 - FIT_HEADROOM
    return cpu <= cpu_max * room and pg <= pg_max * room


def min_cc_for(cpu: float, pg: float) -> Optional[int]:
    """Lowest Command Center Upgrades level that runs this layout with the standard headroom — the
    skill the player ACTUALLY needs, as opposed to the level they happen to have. None if it
    doesn't fit even at 5. Levels above `min_cc` buy nothing for this template, which is the whole
    point: CCU IV→V is the expensive half of the skill and plenty of colonies never need it."""
    for lvl in range(1, CMD_CTR_LEVEL + 1):
        cpu_max, pg_max = CC_BUDGET[lvl]
        if _fits(cpu, pg, cpu_max, pg_max):
            return lvl
    return None

# Launchpads per factory. Each launchpad holds 10,000 m3, so 3 = 30,000 m3 of P1
# buffer. P1 is routed from every launchpad to each facility so they drain together.
LAUNCHPADS_PER_FACTORY = 3
MAX_LAUNCHPADS = 8

# How many P2 facilities feed one P3 facility per P2-input (balanced: 2x5 = 10/hr).
P2_PER_P3_INPUT = 2
# How many P3 facilities feed the P4 per P3-input (compact single-planet: 1, the
# P4 then runs at partial rate). Set to 2 for a fully balanced — but much larger — line.
P3_PER_P4_INPUT = 1
# Max P2 facilities on a single planet for a flat P2 factory (reference archetype).
P2_FACTORY_CAP = 12

# Extractor (P0→P1) archetype: 1 ECU with N heads → basic factories → launchpad.
EXTRACTOR_HEADS = 10
EXTRACTOR_BASICS = 8

PLANET_DIAM = {
    "Barren": 8000.0, "Temperate": 8000.0, "Lava": 6000.0, "Plasma": 8000.0,
    "Gas": 40000.0, "Ice": 6000.0, "Oceanic": 8000.0, "Storm": 30000.0,
}
# P4 (High-Tech Production Plant) only exists on Barren / Temperate planets.
P4_PLANET_TYPES = ("Barren", "Temperate")

# Geometry — a tight radial cluster keeps links short and cheap. EVE's planet
# coordinate space behaves as a FLAT plane in (lat, lon): the minimum pin separation
# is a fixed ~0.012 in raw sqrt(dLat^2 + dLon^2) with NO cos(lat) compression
# (measured from a hand-built factory — the raw-planar tightest pairs all land on
# 0.0120, while great-circle distances do not). So we place and space pins directly
# in raw (lat, lon) units; MIN_SEP is that floor plus a small margin.
_CENTER_LAT = 1.20
_CENTER_LON = 1.55
MIN_SEP = 0.0124


def _to_latlon(r: float, theta: float) -> tuple[float, float]:
    """Convert a (distance, bearing) offset from centre to (lat, lon) — flat plane."""
    return (_CENTER_LAT + r * math.cos(theta), _CENTER_LON + r * math.sin(theta))


def _enforce_min_sep(pins: list[dict], min_sep: float = MIN_SEP, iterations: int = 200) -> None:
    """
    Deterministic relaxation in raw (lat, lon): push apart any pin pair closer than
    `min_sep`. Symmetric pushes preserve the layout's symmetry while guaranteeing no
    two pins are ever closer than EVE allows.
    """
    pts = [[p["La"], p["Lo"]] for p in pins]
    n = len(pts)
    for _ in range(iterations):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                dx = pts[j][0] - pts[i][0]
                dy = pts[j][1] - pts[i][1]
                d = math.hypot(dx, dy)
                if d >= min_sep:
                    continue
                if d < 1e-9:
                    dx, dy, d = 1e-6, 0.0, 1e-6  # coincident → nudge apart along lat
                push = (min_sep - d) / 2 * 1.02
                ux, uy = dx / d, dy / d
                pts[i][0] -= ux * push; pts[i][1] -= uy * push
                pts[j][0] += ux * push; pts[j][1] += uy * push
                moved = True
        if not moved:
            break
    for p, (x, y) in zip(pins, pts):
        p["La"], p["Lo"] = round(x, 5), round(y, 5)


def _structure_ids(pi_data: dict, planet_type: str) -> dict:
    """Resolve role -> structure type_id for a given planet type from the SDE."""
    name_to_id = pi_data["name_to_id"]
    patterns = {
        "launchpad":      f"{planet_type} Launchpad",
        "basic_if":       f"{planet_type} Basic Industry Facility",
        "adv_if":         f"{planet_type} Advanced Industry Facility",
        "hitech_if":      f"{planet_type} High-Tech Production Plant",
        "storage":        f"{planet_type} Storage Facility",
        "ecu":            f"{planet_type} Extractor Control Unit",
        "command_center": f"Standard {planet_type} Command Center",
    }
    return {role: name_to_id.get(name.lower()) for role, name in patterns.items()}


def _facility_type(struct: dict, tier: int) -> int:
    """Structure type id that produces an item of the given output tier."""
    if tier == 1:
        return struct["basic_if"]
    if tier in (2, 3):
        return struct["adv_if"]
    return struct["hitech_if"]


def _per_hour(sch: dict) -> float:
    return sch["output_qty"] * 3600.0 / sch["cycle_time"]


# ── Factory tree -------------------------------------------------------------

def _build_instances(product_id: int, pi_data: dict) -> list[dict]:
    """
    Build the facility instances for one compact factory. Each instance:
      {idx, product_id, tier, link_parent, consumer}
    - `consumer`: the instance whose recipe eats this output (None for the top
      product, which is exported to a launchpad).
    - `link_parent`: the pin this facility is physically *linked* to (None for the
      top product → the launchpad hub). This is separated from `consumer` so the
      secondary input type can be **chained** through the primary one: e.g. for a
      P3 with inputs A (primary) + B (secondary), the two A facilities link to the
      P3, and each B links to an A sibling — shorter links — while both A and B are
      consumed by the P3 (routed along the link path).
    The first instance (idx 0) is always the top product.
    """
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    instances: list[dict] = []

    def add(pid: int, link_parent: Optional[int], consumer: Optional[int]) -> int:
        idx = len(instances)
        instances.append({"idx": idx, "product_id": pid, "tier": types[pid]["pi_tier"],
                          "link_parent": link_parent, "consumer": consumer})
        return idx

    def multiplicity(parent_tier: int) -> int:
        return P3_PER_P4_INPUT if parent_tier == 4 else P2_PER_P3_INPUT

    def expand(idx: int):
        pid = instances[idx]["product_id"]
        tier = instances[idx]["tier"]
        if tier <= 2:
            return  # P2 consumes P1 (imported) — leaf producer
        m = multiplicity(tier)
        produced = [inp for inp in schematics[pid]["inputs"]
                    if (types[inp["type_id"]].get("pi_tier") or 0) >= 2]
        # Build m parallel columns. The first input type links straight to the consumer;
        # each later input type *chains* onto its column's current tail, so a column is a
        # linear chain (e.g. P3 → type0 → type1 → type2) that stays one facility wide.
        # When m == 1 (a P4's P3 inputs) every type links to the parent so the P3s become
        # separate branches rather than chaining into each other.
        columns: list[Optional[int]] = [None] * m
        for ti, inp in enumerate(produced):
            for j in range(m):
                link_parent = idx if (ti == 0 or m == 1) else columns[j]
                child = add(inp["type_id"], link_parent, idx)
                columns[j] = child
                expand(child)

    root = add(product_id, None, None)
    expand(root)
    return instances


STEP = 0.0135   # spacing between consecutive structures (raw lat/lon units)
STRADDLE = 0.0144  # lateral gap between the two facilities of a straddling pair


def _branch_dirs(n: int) -> list[tuple[float, float]]:
    """Cardinal directions for the root's branches, as (dLon, dLat). The left arm
    (-Lon) is reserved for launchpads, so branches use right / up / down first."""
    R, U, D = (1.0, 0.0), (0.0, 1.0), (0.0, -1.0)
    if n <= 1:
        return [R]
    if n == 2:
        return [U, D]
    if n == 3:
        return [R, U, D]
    # fallback: spread remaining branches over the right hemisphere
    return [(math.cos(t), math.sin(t)) for t in
            [(-math.pi / 2 + math.pi * (i + 0.5) / n) for i in range(n)]]


def _layout_product(instances: list[dict]) -> dict[int, tuple[float, float]]:
    """
    Cardinal-arm tidy-tree layout matching a hand-built factory: root product at
    centre; each of the root's branches (the P3s) extends straight out along a
    cardinal direction. Within a branch, depth = distance out (`STEP` per tier) and a
    tidy-tree assigns each facility a lateral slot (`STRADDLE` apart) so subtrees never
    overlap — handling P3s with 2 OR 3 P2 inputs (each primary then carries 1–2 chained
    secondaries). Returns {instance_idx: (lat, lon)} in raw lat/lon units.
    """
    children: dict[int, list[int]] = {i["idx"]: [] for i in instances}
    for inst in instances:
        if inst["link_parent"] is not None:
            children[inst["link_parent"]].append(inst["idx"])

    pos: dict[int, tuple[float, float]] = {0: (0.0, 0.0)}   # (dLon, dLat) offsets
    dirs = _branch_dirs(len(children[0]))

    for bi, broot in enumerate(children[0]):
        u = dirs[bi % len(dirs)]
        perp_v = (-u[1], u[0])
        depth: dict[int, int] = {}
        perp: dict[int, float] = {}
        leaves: list[int] = []

        def assign(node: int, d: int):
            depth[node] = d
            kids = children[node]
            if not kids:
                perp[node] = float(len(leaves))   # next free lateral slot
                leaves.append(node)
            else:
                for k in kids:
                    assign(k, d + 1)
                perp[node] = sum(perp[k] for k in kids) / len(kids)  # centre over children

        assign(broot, 1)
        shift = perp[broot]   # put the branch root on the axis
        for node in depth:
            a = depth[node] * STEP
            pp = (perp[node] - shift) * STRADDLE
            pos[node] = (a * u[0] + pp * perp_v[0], a * u[1] + pp * perp_v[1])

    return {idx: (_CENTER_LAT + dy, _CENTER_LON + dx) for idx, (dx, dy) in pos.items()}


def _path(adj: dict, src: int, dst: int) -> list[int]:
    """Shortest pin path src -> dst along links (unique, since the graph is a tree)."""
    if src == dst:
        return [src]
    from collections import deque
    prev = {src: None}
    q = deque([src])
    while q:
        x = q.popleft()
        if x == dst:
            break
        for y in adj[x]:
            if y not in prev:
                prev[y] = x
                q.append(y)
    path = []
    cur: Optional[int] = dst
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    return list(reversed(path))


def _radial_unit_layout(instances: list[dict], theta: float, base_r: float = 0.02) -> dict[int, tuple]:
    """Lay a production unit out radially from the hub: root at `base_r` in bearing
    `theta`, children fanned outward. Used when several units share one planet."""
    STEP_R = 0.0135
    FAN = 1.15
    children: dict[int, list[int]] = {i["idx"]: [] for i in instances}
    for inst in instances:
        if inst["link_parent"] is not None:
            children[inst["link_parent"]].append(inst["idx"])
    pos: dict[int, tuple] = {}

    def place(node: int, depth: int, ang_c: float, ang_span: float):
        pos[node] = _to_latlon(base_r + depth * STEP_R, ang_c)
        kids = children[node]
        n = len(kids)
        if not n:
            return
        span = min(ang_span, FAN)
        for i, k in enumerate(kids):
            a = ang_c if n == 1 else (ang_c - span / 2 + span * (i + 0.5) / n)
            place(k, depth + 1, a, span / n if n > 1 else span)

    place(instances[0]["idx"], 0, theta, FAN)
    return pos


def build_factory_template(product_id: int, struct: dict, planet_type: str,
                           pi_data: dict, copy_label: str = "", n_launchpads: int = LAUNCHPADS_PER_FACTORY,
                           n_trees: int = 1) -> dict:
    """
    Build one factory-planet template. `n_trees` production units share the same
    launchpads on a single planet (1 = the compact cardinal layout; >1 = the units
    radiate around the shared launchpad hub).
    """
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    n_launchpads = max(1, min(MAX_LAUNCHPADS, n_launchpads))
    n_trees = max(1, n_trees)

    pins: list[dict] = []
    lp_pins: list[int] = []
    for _ in range(n_launchpads):
        pins.append({"H": 0, "S": None, "T": struct["launchpad"]})
        lp_pins.append(len(pins))

    # Build each unit's instances + pins, remembering per-unit pin maps.
    units: list[tuple] = []   # (instances, pin_of)
    for _ in range(n_trees):
        insts = _build_instances(product_id, pi_data)
        pin_of: dict[int, int] = {}
        for inst in insts:
            pins.append({"H": 0, "S": inst["product_id"], "T": _facility_type(struct, inst["tier"])})
            pin_of[inst["idx"]] = len(pins)
        units.append((insts, pin_of))

    # ── Positions ──
    if n_trees == 1:
        insts, pin_of = units[0]
        pos = _layout_product(insts)              # cardinal "plus" layout
        lp_dist = 0.0135
        for k, lp in enumerate(lp_pins):          # launchpads in the reserved left arm
            m = (k + 1) // 2
            dy = (m * STRADDLE) * (1 if k % 2 == 1 else -1) if k > 0 else 0.0
            pins[lp - 1]["La"], pins[lp - 1]["Lo"] = _CENTER_LAT + dy, _CENTER_LON - lp_dist
        for inst in insts:
            pins[pin_of[inst["idx"]] - 1]["La"], pins[pin_of[inst["idx"]] - 1]["Lo"] = pos[inst["idx"]]
    else:
        lp_ring = 0.012                            # launchpads cluster at the centre
        for k, lp in enumerate(lp_pins):
            if k == 0:
                la, lo = _CENTER_LAT, _CENTER_LON
            else:
                la, lo = _to_latlon(lp_ring, math.pi + 2 * math.pi * (k - 1) / max(1, n_launchpads - 1))
            pins[lp - 1]["La"], pins[lp - 1]["Lo"] = la, lo
        for ti, (insts, pin_of) in enumerate(units):
            pos = _radial_unit_layout(insts, 2 * math.pi * ti / n_trees, base_r=0.024)
            for inst in insts:
                pins[pin_of[inst["idx"]] - 1]["La"], pins[pin_of[inst["idx"]] - 1]["Lo"] = pos[inst["idx"]]

    # ── Links: launchpad chain + each unit (root→hub, child→link_parent) ──
    links: list[dict] = [{"S": lp, "D": lp_pins[0], "Lv": 0} for lp in lp_pins[1:]]
    for insts, pin_of in units:
        for inst in insts:
            src = lp_pins[0] if inst["link_parent"] is None else pin_of[inst["link_parent"]]
            links.append({"S": src, "D": pin_of[inst["idx"]], "Lv": 0})

    adj: dict[int, set] = {i: set() for i in range(1, len(pins) + 1)}
    for l in links:
        adj[l["S"]].add(l["D"]); adj[l["D"]].add(l["S"])

    # ── Routes per unit ──
    routes: list[dict] = []
    for insts, pin_of in units:
        for inst in insts:
            sch = schematics[inst["product_id"]]
            dst = lp_pins[0] if inst["consumer"] is None else pin_of[inst["consumer"]]
            routes.append({"P": _path(adj, pin_of[inst["idx"]], dst), "Q": sch["output_qty"], "T": inst["product_id"]})
        for inst in insts:
            if inst["tier"] != 2:
                continue
            for inp in schematics[inst["product_id"]]["inputs"]:
                if types[inp["type_id"]].get("pi_tier") != 1:
                    continue
                for lp in lp_pins:
                    routes.append({"P": _path(adj, lp, pin_of[inst["idx"]]), "Q": inp["quantity"], "T": inp["type_id"]})

    tier = types[product_id]["pi_tier"]
    by_tier: dict[int, int] = {}
    for insts, _ in units:
        for inst in insts:
            by_tier[inst["tier"]] = by_tier.get(inst["tier"], 0) + 1

    for p in pins:   # round any positions still carrying full precision
        p["La"], p["Lo"] = round(p["La"], 5), round(p["Lo"], 5)
    _enforce_min_sep(pins)
    unit_label = f" ×{n_trees}" if n_trees > 1 else ""
    # Planet type omitted from the name: PI templates are planet-PORTABLE (a "Lava" extractor imports
    # fine onto a Storm planet), so the type was only ever our sizing default and "(Barren)" just
    # confused users told to place it elsewhere. Zip dedup keys on the template's Pln, not the name.
    cmt = f"P{tier} {types[product_id]['name']} factory{unit_label}{copy_label}"
    template = {
        "CmdCtrLv": CMD_CTR_LEVEL, "Cmt": cmt, "Diam": PLANET_DIAM.get(planet_type, 8000.0),
        "Pln": struct["planet_type_id"], "P": pins, "L": links, "R": routes,
    }
    return {"template": template, "facilities_by_tier": by_tier, "name": cmt}


def build_flat_p2_template(product_id: int, n_fac: int, struct: dict,
                           planet_type: str, pi_data: dict, copy_label: str = "",
                           n_launchpads: int = LAUNCHPADS_PER_FACTORY) -> dict:
    """A flat P2 factory planet: launchpads (hub + chained) + n_fac P2 facilities."""
    types = pi_data["types"]
    sch = pi_data["schematics"][product_id]
    n_launchpads = max(1, min(MAX_LAUNCHPADS, n_launchpads))
    pins, links, routes = [], [], []
    # Launchpads: hub at centre, the rest on a tiny ring around it.
    lp_pins: list[int] = []
    for k in range(n_launchpads):
        if k == 0:
            la, lo = _CENTER_LAT, _CENTER_LON
        else:
            la, lo = _to_latlon(0.0135, 2 * math.pi * (k - 1) / max(1, n_launchpads - 1))
        pins.append({"H": 0, "S": None, "T": struct["launchpad"], "La": round(la, 5), "Lo": round(lo, 5)})
        lp_pins.append(len(pins))
    for lp in lp_pins[1:]:
        links.append({"S": lp, "D": lp_pins[0], "Lv": 0})
    # Facilities on a tight symmetric ring around the hub; the ring radius grows just
    # enough that neighbours stay >= MIN_SEP apart.
    radius = max(0.0135, MIN_SEP / (2 * math.sin(math.pi / max(2, n_fac))))
    for j in range(n_fac):
        la, lo = _to_latlon(radius, 2 * math.pi * j / n_fac)
        pins.append({"H": 0, "S": product_id, "T": struct["adv_if"], "La": round(la, 5), "Lo": round(lo, 5)})
        fac = len(pins)
        links.append({"S": lp_pins[0], "D": fac, "Lv": 0})
        for inp in sch["inputs"]:
            for k in range(n_launchpads):
                path = [lp_pins[0], fac] if k == 0 else [lp_pins[k], lp_pins[0], fac]
                routes.append({"P": path, "Q": inp["quantity"], "T": inp["type_id"]})
        routes.append({"P": [fac, lp_pins[0]], "Q": sch["output_qty"], "T": product_id})
    _enforce_min_sep(pins)
    cmt = f"P2 {types[product_id]['name']} factory{copy_label}"   # planet type omitted (see build_factory_template)
    return {
        "template": {"CmdCtrLv": CMD_CTR_LEVEL, "Cmt": cmt, "Diam": PLANET_DIAM.get(planet_type, 8000.0),
                     "Pln": struct["planet_type_id"], "P": pins, "L": links, "R": routes},
        "facilities_by_tier": {2: n_fac}, "name": cmt,
    }


def build_extractor_template(p1_id: int, planet_type: str, struct: dict, pi_data: dict,
                             heads: int, n_basic: int, n_launchpads: int = 1,
                             no_storage: bool = False, diam: float | None = None) -> dict:
    """
    P0→P1 extractor planet: a hub at centre, surrounded by an Extractor Control Unit
    (extracting the P0), `n_basic` Basic Industry Facilities (making the P1) and the
    launchpad(s), all linked to the hub. P0 flows ECU→hub→factories; P1 flows factory→hub→
    launchpad. The hub is a Storage Facility by default; with `no_storage` the launchpad is
    the hub instead (buffers the bursty P0 + exports P1) — one fewer structure, freeing ~700 PG
    so a big/low-CC planet fits another basic, at the cost of a smaller P0 buffer.
    """
    types = pi_data["types"]
    sch = pi_data["schematics"][p1_id]
    p0_id = sch["inputs"][0]["type_id"]
    p0_qty = sch["inputs"][0]["quantity"]
    p1_out = sch["output_qty"]

    hub_role = "launchpad" if no_storage else "storage"
    pins: list[dict] = [{"H": 0, "S": None, "T": struct[hub_role],
                         "La": _CENTER_LAT, "Lo": _CENTER_LON}]   # hub = pin 1
    hub = 1
    ecu_pin = len(pins) + 1
    pins.append({"H": heads, "S": p0_id, "T": struct["ecu"]})     # ECU: heads + P0
    basics = []
    for _ in range(n_basic):
        pins.append({"H": 0, "S": p1_id, "T": struct["basic_if"]})
        basics.append(len(pins))
    # Launchpads: when no_storage the hub IS the first launchpad; add the rest on the ring.
    lps = [hub] if no_storage else []
    extra_lps = (n_launchpads - 1) if no_storage else n_launchpads
    for _ in range(max(0, extra_lps)):
        pins.append({"H": 0, "S": None, "T": struct["launchpad"]})
        lps.append(len(pins))
    if not lps:                                                   # storage mode needs ≥1 launchpad
        pins.append({"H": 0, "S": None, "T": struct["launchpad"]})
        lps.append(len(pins))

    ring = [ecu_pin] + basics + [p for p in lps if p != hub]
    radius = max(0.0145, MIN_SEP / (2 * math.sin(math.pi / max(2, len(ring)))))
    for k, pin in enumerate(ring):
        la, lo = _to_latlon(radius, 2 * math.pi * k / len(ring))
        pins[pin - 1]["La"], pins[pin - 1]["Lo"] = la, lo

    export_lp = lps[0]                                            # hub launchpad when no_storage
    links = [{"S": hub, "D": p, "Lv": 0} for p in ring]
    routes = [{"P": [ecu_pin, hub], "Q": n_basic * p0_qty, "T": p0_id}]   # extraction → hub
    for b in basics:
        routes.append({"P": [hub, b], "Q": p0_qty, "T": p0_id})          # hub → factory (P0)
        p1_path = [b, hub] if export_lp == hub else [b, hub, export_lp]
        routes.append({"P": p1_path, "Q": p1_out, "T": p1_id})           # factory → launchpad (P1)

    _enforce_min_sep(pins)
    for p in pins:
        p["La"], p["Lo"] = round(p["La"], 5), round(p["Lo"], 5)
    # Lead with the P0 — that's what you search/select in-game to find hotspots — then the P1.
    cmt = f"{types[p0_id]['name']} → {types[p1_id]['name']}"   # planet type omitted — templates are portable
    return {
        "template": {"CmdCtrLv": CMD_CTR_LEVEL, "Cmt": cmt,
                     # Real per-planet diameter when known (from pp_planets.diameter) — the link
                     # PG cost scales with radius, so using the planet TYPE default (Gas Ø40000) instead
                     # of the actual planet (often smaller) needlessly dropped a basic. Falls back to the
                     # type default when the caller has no real size.
                     "Diam": float(diam) if diam else PLANET_DIAM.get(planet_type, 8000.0),
                     "Pln": struct["planet_type_id"], "P": pins, "L": links, "R": routes},
        "name": cmt, "p0_id": p0_id, "p0_name": types[p0_id]["name"], "n_basic": n_basic,
        "heads": heads, "n_launchpads": len(lps),
    }


def build_split_extractor_template(p1a_id: int, p1b_id: int, planet_type: str, struct: dict,
                                   pi_data: dict, heads_a: int, heads_b: int,
                                   n_basic_a: int, n_basic_b: int, n_launchpads: int = 1,
                                   diam: float | None = None) -> dict:
    """Split P0→P1 extractor planet: TWO Extractor Control Units sharing one storage hub and
    launchpad, each with its own head count + Basic Industry line, producing two P1s. The host
    planet type must yield both P0s. Mirrors build_extractor_template's hub/ring topology."""
    types = pi_data["types"]
    scha, schb = pi_data["schematics"][p1a_id], pi_data["schematics"][p1b_id]
    p0a, p0a_qty, p1a_out = scha["inputs"][0]["type_id"], scha["inputs"][0]["quantity"], scha["output_qty"]
    p0b, p0b_qty, p1b_out = schb["inputs"][0]["type_id"], schb["inputs"][0]["quantity"], schb["output_qty"]

    pins: list[dict] = [{"H": 0, "S": None, "T": struct["storage"],
                         "La": _CENTER_LAT, "Lo": _CENTER_LON}]   # storage hub = pin 1
    hub = 1

    def _ecu_line(heads, p0_id, p1_id, n_basic):
        ecu = len(pins) + 1
        pins.append({"H": heads, "S": p0_id, "T": struct["ecu"]})
        basics = []
        for _ in range(n_basic):
            pins.append({"H": 0, "S": p1_id, "T": struct["basic_if"]})
            basics.append(len(pins))
        return ecu, basics

    ecu_a, basics_a = _ecu_line(heads_a, p0a, p1a_id, n_basic_a)
    ecu_b, basics_b = _ecu_line(heads_b, p0b, p1b_id, n_basic_b)
    lps = []
    for _ in range(max(1, n_launchpads)):
        pins.append({"H": 0, "S": None, "T": struct["launchpad"]})
        lps.append(len(pins))

    ring = [ecu_a] + basics_a + [ecu_b] + basics_b + lps
    radius = max(0.0145, MIN_SEP / (2 * math.sin(math.pi / max(2, len(ring)))))
    for k, pin in enumerate(ring):
        la, lo = _to_latlon(radius, 2 * math.pi * k / len(ring))
        pins[pin - 1]["La"], pins[pin - 1]["Lo"] = la, lo

    links = [{"S": hub, "D": p, "Lv": 0} for p in ring]
    routes = [
        {"P": [ecu_a, hub], "Q": n_basic_a * p0a_qty, "T": p0a},   # extraction A → storage
        {"P": [ecu_b, hub], "Q": n_basic_b * p0b_qty, "T": p0b},   # extraction B → storage
    ]
    for b in basics_a:
        routes.append({"P": [hub, b], "Q": p0a_qty, "T": p0a})
        routes.append({"P": [b, hub, lps[0]], "Q": p1a_out, "T": p1a_id})
    for b in basics_b:
        routes.append({"P": [hub, b], "Q": p0b_qty, "T": p0b})
        routes.append({"P": [b, hub, lps[0]], "Q": p1b_out, "T": p1b_id})

    _enforce_min_sep(pins)
    for p in pins:
        p["La"], p["Lo"] = round(p["La"], 5), round(p["Lo"], 5)
    cmt = f"Split: {types[p0a]['name']} + {types[p0b]['name']}"   # planet type omitted — templates are portable
    return {
        "template": {"CmdCtrLv": CMD_CTR_LEVEL, "Cmt": cmt,
                     # Real per-planet diameter when known (link PG ∝ radius) — else type default.
                     "Diam": float(diam) if diam else PLANET_DIAM.get(planet_type, 8000.0),
                     "Pln": struct["planet_type_id"], "P": pins, "L": links, "R": routes},
        "name": cmt, "n_basic_a": n_basic_a, "n_basic_b": n_basic_b,
        "heads_a": heads_a, "heads_b": heads_b, "n_launchpads": len(lps),
    }


def generate_split_extractor_layout(p1a_id: int, p1b_id: int, heads_a: int = 5, heads_b: int = 5,
                                    planet_type: str = "Barren", launchpads: int = 1,
                                    cc_level: Optional[int] = None, diam: Optional[float] = None) -> dict:
    """One importable two-ECU (split) extractor template producing P1 A and P1 B on a single
    planet. The planet type is coerced to one that yields BOTH P0s. Basic-factory counts start
    proportional to each line's heads and shrink to fit the command-centre CPU/PG budget; if it
    still doesn't fit at 1+1 basics the result is flagged over_budget (a CC too low to host two
    ECUs)."""
    pi_data = load_pi_data()
    types = pi_data["types"]
    p0a_name = types[pi_data["schematics"][p1a_id]["inputs"][0]["type_id"]]["name"]
    p0b_name = types[pi_data["schematics"][p1b_id]["inputs"][0]["type_id"]]["name"]
    both = sorted(set(_p0_planets(p0a_name)) & set(_p0_planets(p0b_name)))
    if both and planet_type not in both:
        planet_type = both[0]

    struct = _structure_ids(pi_data, planet_type)
    struct["planet_type_id"] = pi_data["name_to_id"].get(f"planet ({planet_type})".lower())

    cc = CMD_CTR_LEVEL if cc_level is None else max(1, min(5, int(cc_level)))
    lp = max(1, min(MAX_LAUNCHPADS, launchpads))
    ha, hb = max(1, int(heads_a)), max(1, int(heads_b))
    nba = max(1, round(EXTRACTOR_BASICS * ha / EXTRACTOR_HEADS))
    nbb = max(1, round(EXTRACTOR_BASICS * hb / EXTRACTOR_HEADS))

    def _build(a, b):
        t = build_split_extractor_template(p1a_id, p1b_id, planet_type, struct, pi_data, ha, hb,
                                           a, b, n_launchpads=lp, diam=diam)
        t["template"]["CmdCtrLv"] = cc
        return t

    built = _build(nba, nbb)
    while (nba > 1 or nbb > 1) and compute_resources(built["template"], struct)["over_fit"]:
        if nba >= nbb and nba > 1:
            nba -= 1
        elif nbb > 1:
            nbb -= 1
        else:
            nba = max(1, nba - 1)
        built = _build(nba, nbb)

    t = built["template"]
    res = compute_resources(t, struct)
    planet = {
        "id": 1, "name": built["name"], "facilities_by_tier": {1: nba + nbb},
        "structures": {"ECU": 2, "storage": 1, "launchpad": built["n_launchpads"],
                       "basic factory": nba + nbb},
        "pins": len(t["P"]), "links": len(t["L"]), "routes": len(t["R"]),
        "resources": res, "over_budget": res["over"], "template": t,
    }
    sa, sb = pi_data["schematics"][p1a_id], pi_data["schematics"][p1b_id]
    summary = {
        "kind": "split-extractor", "planet_type": planet_type, "valid_planets": both,
        "min_cc": res["min_cc"],
        "launchpads": built["n_launchpads"], "buffer_m3": built["n_launchpads"] * 10000,
        "lines": [
            {"product_id": p1a_id, "product_name": types[p1a_id]["name"], "extracts": p0a_name,
             "heads": ha, "basics": nba,
             "product_per_hour": round(nba * sa["output_qty"] * 3600.0 / sa["cycle_time"], 1)},
            {"product_id": p1b_id, "product_name": types[p1b_id]["name"], "extracts": p0b_name,
             "heads": hb, "basics": nbb,
             "product_per_hour": round(nbb * sb["output_qty"] * 3600.0 / sb["cycle_time"], 1)},
        ],
        "imports_label": (f"Two ECUs in-game: {ha} heads on {p0a_name}, {hb} heads on {p0b_name} "
                          f"(actual yield varies with hotspot placement)"),
    }
    return {"summary": summary, "planets": [planet]}


def _chain_p1s(product_id: int, pi_data: dict) -> list[int]:
    """All distinct P1 type ids consumed anywhere in a product's chain."""
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    found: set[int] = set()

    def rec(tid: int):
        for inp in schematics.get(tid, {}).get("inputs", []):
            t = types[inp["type_id"]].get("pi_tier")
            if t == 1:
                found.add(inp["type_id"])
            elif t and t >= 2:
                rec(inp["type_id"])

    rec(product_id)
    return sorted(found)


def bundle_templates(product_id: int, cc_level: Optional[int] = None,
                     planet_type: Optional[str] = None, no_storage: bool = False) -> list[tuple]:
    """
    All templates needed to produce `product_id`, as (name, template) pairs:
    the factory template for the product itself, plus one P0→P1 extractor template
    for every distinct P1 the chain consumes. For a P1 product it's just the extractor.
    `cc_level` scales every template to that command-centre level (the factory and its
    extractors); `planet_type` applies to the factory only — each extractor keeps the
    planet type its P0 is harvested on. Launchpads default per tier (3 factory / 1 extractor).
    """
    pi_data = load_pi_data()
    tier = pi_data["types"][product_id].get("pi_tier")
    out: list[tuple] = []
    main = generate_layout(product_id, planet_type=planet_type or "Barren", cc_level=cc_level,
                           no_storage=no_storage)
    out.append((main["planets"][0]["name"], main["planets"][0]["template"]))
    if tier and tier >= 2:
        for p1 in _chain_p1s(product_id, pi_data):
            ex = generate_extractor_layout(p1, cc_level=cc_level, no_storage=no_storage)
            out.append((ex["planets"][0]["name"], ex["planets"][0]["template"]))
    return out


def _p0_planets(p0_name: str) -> list[str]:
    """Planet types whose surface yields the given P0 (from the planner's P0 map)."""
    from app.planetary import PLANET_P0_MAP
    return [pt for pt, p0s in PLANET_P0_MAP.items() if p0_name in p0s]


def generate_extractor_layout(p1_id: int, planet_type: str = "Barren", launchpads: int = 1,
                              heads: int = EXTRACTOR_HEADS, n_basic: int = EXTRACTOR_BASICS,
                              cc_level: Optional[int] = None, no_storage: bool = False,
                              diam: Optional[float] = None) -> dict:
    """One importable P0→P1 extractor template for a chosen P1 product. `cc_level` sets the
    command-centre level (CPU/PG budget); all 10 extractor heads are always kept, and only the
    basic (P1) factory count is scaled down to fit a lower level. Defaults to CMD_CTR_LEVEL."""
    pi_data = load_pi_data()
    types = pi_data["types"]
    sch = pi_data["schematics"][p1_id]
    p0_name = types[sch["inputs"][0]["type_id"]]["name"]

    valid = _p0_planets(p0_name)
    if valid and planet_type not in valid:
        planet_type = valid[0]

    struct = _structure_ids(pi_data, planet_type)
    struct["planet_type_id"] = pi_data["name_to_id"].get(f"planet ({planet_type})".lower())

    cc = CMD_CTR_LEVEL if cc_level is None else max(1, min(5, int(cc_level)))
    lp = max(1, min(MAX_LAUNCHPADS, launchpads))

    def _build(h, nb, at_cc):
        b = build_extractor_template(p1_id, planet_type, struct, pi_data, h, nb, n_launchpads=lp,
                                     no_storage=no_storage, diam=diam)
        b["template"]["CmdCtrLv"] = at_cc
        return b

    def _fit(at_cc: int, want_heads: int, want_basics: int):
        """Largest layout that fits level `at_cc` with headroom. Keeps all the extractor heads
        (full P0 extraction) and scales ONLY the basic (P1) factories down — a lower-CC planet
        extracts the same P0 but converts fewer P1 on-site, so the planner places more of them.
        Heads are the last resort: at the 1-basic floor the ECU and its heads alone can still
        blow a small budget (CC1/CC2, or a big planet with expensive links), and dropping
        one costs real extraction, so we do it only to avoid exporting a template the game will
        refuse to build."""
        h, nb = max(1, want_heads), max(1, want_basics)
        b = _build(h, nb, at_cc)
        while nb > 1 and compute_resources(b["template"], struct)["over_fit"]:
            nb -= 1
            b = _build(h, nb, at_cc)
        while h > 1 and compute_resources(b["template"], struct)["over_fit"]:
            h -= 1
            b = _build(h, nb, at_cc)
        return h, nb, b

    req_heads = max(1, heads)
    heads, n_basic, built = _fit(cc, heads, n_basic)
    # What every OTHER command-centre level would give on this same planet. This is the answer to
    # "how far do I actually need to train Command Center Upgrades?" — the levels are far from
    # equal in training time, and a player who only needs 4 basics should be able to see that
    # level III already covers it rather than assuming V is the price of entry.
    p1_per_basic_hr = sch["output_qty"] * 3600.0 / sch["cycle_time"]
    ladder = []
    for lvl in range(1, CMD_CTR_LEVEL + 1):
        lh, lnb, lb = (heads, n_basic, built) if lvl == cc else _fit(lvl, req_heads, EXTRACTOR_BASICS)
        lres = compute_resources(lb["template"], struct)
        ladder.append({"cc": lvl, "heads": lh, "basics": lnb,
                       "product_per_hour": round(lnb * p1_per_basic_hr, 1),
                       "pg_pct": lres["pg_pct"], "cpu_pct": lres["cpu_pct"],
                       "over": lres["over"]})
    t = built["template"]
    planet = {
        "id": 1, "name": built["name"],
        "facilities_by_tier": {1: built["n_basic"]},
        "structures": {"ECU": 1, "storage": 1, "launchpad": built["n_launchpads"], "basic factory": built["n_basic"]},
        "pins": len(t["P"]), "links": len(t["L"]), "routes": len(t["R"]),
        "resources": compute_resources(t, struct), "template": t,
    }
    summary = {
        "product_name": types[p1_id]["name"], "product_id": p1_id, "kind": "extractor",
        "top_tier": 1, "planet_type": planet_type, "valid_planets": valid,
        "extracts": p0_name, "heads": heads, "heads_requested": req_heads,
        "min_cc": planet["resources"]["min_cc"], "cc_ladder": ladder,
        "launchpads": built["n_launchpads"], "buffer_m3": built["n_launchpads"] * 10000,
        "facilities_by_tier": {"P1": built["n_basic"]},
        "product_per_hour": round(built["n_basic"] * sch["output_qty"] * 3600.0 / sch["cycle_time"], 1),
        "imports_label": f"Extracts {p0_name} (set up {heads} extractor heads in-game)",
        "imports": [],
        "economics": _economics(p1_id, built["n_basic"] * sch["output_qty"] * 3600.0 / sch["cycle_time"], []),
    }
    return {"summary": summary, "planets": [planet]}


_FITTED_BASICS_CACHE: dict[tuple, int] = {}


def fitted_extractor_basics(planet_type: str, cc: int, no_storage: bool = False) -> int:
    """How many Basic Industry Facilities fit alongside the 10 extractor heads on this planet
    type at this command-centre level (power-grid limited). 8 basics = full conversion of a
    100%-quality planet's extraction; fewer (low CC, or a big planet whose links eat the
    grid) means the planet refines less P1 on-site. `no_storage` drops the storage hub (buffer in
    the launchpad), freeing ~700 PG so another basic often fits. Cached (≤ 8 types × 5 CC × 2).
    Builds the template directly (no planet-type coercion) — the basic-facility cost is
    recipe-independent, so any P1 schematic gives the right count for the planet type."""
    cc = max(1, min(5, int(cc)))
    key = (planet_type, cc, no_storage)
    if key in _FITTED_BASICS_CACHE:
        return _FITTED_BASICS_CACHE[key]
    n = EXTRACTOR_BASICS
    try:
        pi_data = load_pi_data()
        p1_id = next(tid for tid, t in pi_data["types"].items() if t.get("pi_tier") == 1)
        struct = _structure_ids(pi_data, planet_type)
        struct["planet_type_id"] = pi_data["name_to_id"].get(f"planet ({planet_type})".lower())

        def _build(nb):
            b = build_extractor_template(p1_id, planet_type, struct, pi_data, EXTRACTOR_HEADS, nb,
                                         n_launchpads=1, no_storage=no_storage)
            b["template"]["CmdCtrLv"] = cc
            return b

        built = _build(n)
        while n > 1 and compute_resources(built["template"], struct)["over_fit"]:
            n -= 1
            built = _build(n)
    except Exception:
        n = EXTRACTOR_BASICS
    _FITTED_BASICS_CACHE[key] = n
    return n


def _p1_imports_per_factory(product_id: int, pi_data: dict) -> dict[int, float]:
    """P1 units/hour imported by one compact factory of this product."""
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    instances = _build_instances(product_id, pi_data)
    imports: dict[int, float] = {}
    for inst in instances:
        if inst["tier"] != 2:
            continue
        sch = schematics[inst["product_id"]]
        cph = 3600.0 / sch["cycle_time"]
        for inp in sch["inputs"]:
            if types[inp["type_id"]].get("pi_tier") == 1:
                imports[inp["type_id"]] = imports.get(inp["type_id"], 0.0) + inp["quantity"] * cph
    return imports


def _economics(output_id: int, out_per_hour: float, import_pairs: list[tuple]) -> dict:
    """ISK/day from Jita sell prices: gross output value, P1 input cost, and net."""
    from app.market import fetch_prices
    prices = fetch_prices([output_id] + [t for t, _ in import_pairs])
    op = prices.get(output_id, 0.0)
    revenue = out_per_hour * 24 * op
    cost = sum(ph * 24 * prices.get(t, 0.0) for t, ph in import_pairs)
    return {
        "output_price": round(op, 2),
        "revenue_per_day": round(revenue),
        "input_cost_per_day": round(cost),
        "profit_per_day": round(revenue - cost),
    }


def default_launchpads(tier: int) -> int:
    """Sensible launchpad default: extractors need little buffer (1), factories more (3)."""
    return 1 if tier == 1 else LAUNCHPADS_PER_FACTORY


def generate_layout(product_id: int, planet_type: str = "Barren",
                    launchpads: Optional[int] = None, count: Optional[int] = None,
                    cc_level: Optional[int] = None, no_storage: bool = False,
                    diam_override: Optional[float] = None) -> dict:
    """
    Generate ONE importable template for the product (you replicate it across as many
    planets as you want — every planet is identical):
      - P1 product  → extractor template (ECU → basic factories → launchpad).
      - P2/P3/P4    → self-contained factory (P1 imported, chained up to Px).
    `count` packs that many production units onto the SAME planet sharing the
    launchpads (P2: factories; P3/P4: chains). `count=None` → the max that fits the
    command-centre budget. `launchpads` sets the P1 input buffer; both default per tier.
    `cc_level` sets the command-centre level (CPU/PG budget) — lower levels fit fewer
    facilities, so `max_count` and the output rate drop; defaults to CMD_CTR_LEVEL.
    """
    pi_data = load_pi_data()
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    cc = CMD_CTR_LEVEL if cc_level is None else max(1, min(5, int(cc_level)))

    tier = types.get(product_id, {}).get("pi_tier")
    if tier not in (1, 2, 3, 4):
        raise ValueError("product must be a P1, P2, P3 or P4 item")
    if launchpads is None:
        launchpads = default_launchpads(tier)
    launchpads = max(1, min(MAX_LAUNCHPADS, int(launchpads)))

    if tier == 1:
        return generate_extractor_layout(product_id, planet_type, launchpads=launchpads, cc_level=cc,
                                         no_storage=no_storage, diam=diam_override)

    if tier == 4 and planet_type not in P4_PLANET_TYPES:
        planet_type = "Barren"   # High-Tech Production Plant is Barren/Temperate only

    struct = _structure_ids(pi_data, planet_type)
    struct["planet_type_id"] = pi_data["name_to_id"].get(f"planet ({planet_type})".lower())

    def _build(c):
        if tier == 2:
            b = build_flat_p2_template(product_id, c, struct, planet_type, pi_data, "", launchpads)
        else:
            b = build_factory_template(product_id, struct, planet_type, pi_data, "", launchpads, n_trees=c)
        b["template"]["CmdCtrLv"] = cc  # CPU/PG budget the packing must fit
        if diam_override and diam_override > 0:
            b["template"]["Diam"] = float(diam_override)   # real planet size → real link costs/packing
        return b

    # Largest count that still fits the command-centre budget.
    max_count = 1
    for c in range(1, 41):
        if compute_resources(_build(c)["template"], struct)["over_fit"]:
            break
        max_count = c

    count = max_count if (count is None or int(count) < 1) else int(count)
    built = _build(count)

    if tier == 2:
        cph = 3600.0 / schematics[product_id]["cycle_time"]
        per_imports = {
            inp["type_id"]: inp["quantity"] * cph * count
            for inp in schematics[product_id]["inputs"] if types[inp["type_id"]].get("pi_tier") == 1
        }
    else:
        per1 = _p1_imports_per_factory(product_id, pi_data)
        per_imports = {tid: rate * count for tid, rate in per1.items()}

    planet = _planet_entry(1, built, struct)
    product_rate = _effective_output_rate(product_id, pi_data) * count

    summary = {
        "product_name": types[product_id]["name"],
        "product_id": product_id,
        "kind": "factory",
        "top_tier": tier,
        "planet_type": planet_type,
        "launchpads": launchpads,
        "count": count,
        "max_count": max_count,
        # Lowest CCU level that runs THIS template (at this count) — levels above it add nothing
        # unless you also raise the count.
        "min_cc": planet["resources"]["min_cc"],
        "buffer_m3": launchpads * 10000,
        "facilities_by_tier": {f"P{t}": v for t, v in sorted(planet["facilities_by_tier"].items())},
        "product_per_hour": round(product_rate, 3),
        "imports_label": "P1 to load into the launchpads",
        "imports": [
            {"name": types[tid]["name"], "type_id": tid, "per_hour": round(rate, 1)}
            for tid, rate in sorted(per_imports.items(), key=lambda x: -x[1])
        ],
        "economics": _economics(product_id, product_rate, list(per_imports.items())),
    }
    return {"summary": summary, "planets": [planet]}


def _effective_output_rate(product_id: int, pi_data: dict) -> float:
    """
    Effective top-product output/hour for one compact factory, accounting for the
    P3->P4 under-provisioning. P2/P3 factories are balanced (full rate); a P4
    factory is throttled by P3 supply (P3_PER_P4_INPUT facilities per P3 input).
    """
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    tier = types[product_id]["pi_tier"]
    sch = schematics[product_id]
    if tier != 4:
        return _per_hour(sch)
    # P4: limited by slowest P3 supply vs demand.
    cph = 3600.0 / sch["cycle_time"]
    worst = 1.0
    for inp in sch["inputs"]:
        if types[inp["type_id"]].get("pi_tier") != 3:
            continue
        demand = inp["quantity"] * cph                       # P3 wanted per hour
        supply = P3_PER_P4_INPUT * _per_hour(schematics[inp["type_id"]])
        worst = min(worst, supply / demand)
    return _per_hour(sch) * worst


def _planet_entry(pid: int, built: dict, struct: dict) -> dict:
    t = built["template"]
    by_tier = built["facilities_by_tier"]
    lp = sum(1 for p in t["P"] if p["T"] == struct["launchpad"])
    fac = sum(1 for p in t["P"] if p["S"] is not None)
    return {
        "id": pid,
        "name": built["name"],
        "planet_type": None,  # set by caller context via Cmt; kept for compatibility
        "facilities_by_tier": by_tier,
        "structures": {"launchpad": lp, "facility": fac,
                       **{f"P{tier}": n for tier, n in sorted(by_tier.items())}},
        "pins": len(t["P"]),
        "links": len(t["L"]),
        "routes": len(t["R"]),
        "resources": compute_resources(t, struct),
        "template": t,
    }
