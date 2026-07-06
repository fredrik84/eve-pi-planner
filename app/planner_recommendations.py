"""
Planet-DB lookups for the planner: which planets grow a given P0, and
system/constellation recommendations scored by density and jump-distance
clustering. Read-only queries against pp_planets — not part of the
extractor/factory seating algorithm itself (see app/planner.py for that).
"""
import logging

from app.planetary import PLANET_P0_MAP, _NAME_TO_COL

log = logging.getLogger(__name__)

_P0_PLANET_TYPES: dict[str, list[str]] = {}
for _pt, _p0s in PLANET_P0_MAP.items():
    for _p0 in _p0s:
        _P0_PLANET_TYPES.setdefault(_p0, []).append(_pt)


def _p0_col(name: str) -> str | None:
    return _NAME_TO_COL.get(name.strip().lower()) if name else None


# Hard cap on candidates returned per P0, independent of min_density. These lists feed the
# planner's bipartite-matching feasibility checks (_can_add_p0/_max_matching_slots in planner.py),
# whose cost scales with candidate-list size — at min_density_pct=0 (the field's default) with a
# couple of full constellations selected, an uncapped list can run into the hundreds per P0 and
# turn a routine plan into a multi-minute one (confirmed live: ~90s on a 2-constellation fuel-block
# plan). Already sorted richest-first, so this is a pure tail-truncation: no realistic extractor
# demand for a single P0 approaches this many candidates (max 6 planets/char), it only ever trims
# the long thin tail that a matching feasibility check never needed anyway.
_MAX_CANDIDATES_PER_P0 = 300


def _fetch_p0_planets(
    p0_names: list[str], con,
    constellations: list[str],
    systems: list[str] | None = None,
    min_density: int = 0,
) -> dict[str, list[dict]]:
    """Return {p0_name: [planets sorted by value DESC]}. Planets thinner than `min_density`%
    are excluded (the density cap), so the planner can't pile extractors onto deposits it isn't
    worth setting up — at the cost of some residual on resources that then can't reach their need."""
    thresh = max(0.01, float(min_density or 0))
    result: dict[str, list[dict]] = {}
    where_extra = ""
    params_base: list = []
    if systems:
        where_extra = " AND system IN ({})".format(",".join("?" * len(systems)))
        params_base = list(systems)
    elif constellations:
        where_extra = " AND constellation IN ({})".format(",".join("?" * len(constellations)))
        params_base = list(constellations)
    for name in p0_names:
        col = _p0_col(name)
        if not col:
            result[name] = []
            continue
        # Only planet TYPES that actually grow this P0 (per PLANET_P0_MAP). Guards against a bad Planet-DB
        # row — e.g. a Plasma planet with a stray Noble Gas value (Noble Gas vs Noble Metals mix-up) —
        # being handed out as a Noble Gas extractor site on a planet that physically can't have it.
        valid_types = _P0_PLANET_TYPES.get(name, [])
        type_filter = (" AND planet_type IN ({})".format(",".join("?" * len(valid_types)))) if valid_types else ""
        try:
            rows = con.execute(
                f"SELECT system, planet_num, planet_type, {col} as value "
                f"FROM pp_planets WHERE {col}>=?{where_extra}{type_filter} "
                f"ORDER BY {col} DESC LIMIT {_MAX_CANDIDATES_PER_P0}",
                [thresh] + params_base + valid_types,
            ).fetchall()
            result[name] = [dict(r) for r in rows]
        except Exception:
            result[name] = []
    return result


def _system_recommendations(
    p0_names: list[str], con, top_n: int = 5,
    constellations: list[str] | None = None,
    systems: list[str] | None = None,
    preferred_systems: int = 1,
    max_jumps: int = 1,
    min_density: int = 0,
    p0_needs: dict[str, float] | None = None,
) -> list:
    thresh = max(0.01, float(min_density or 0))  # density cap: a planet must beat this to count
    needed_cols = {name: _p0_col(name) for name in p0_names}
    needed_cols = {k: v for k, v in needed_cols.items() if v}
    if not needed_cols:
        return []
    # Depth-aware scoring: a system's quality for a P0 is the average density of the *top-K*
    # planets it offers (K scales with that resource's demand), not just the single richest.
    # So a high-need resource (e.g. Reactive Gas) backed by one rich planet + a thin tail no
    # longer ranks like one with several rich planets — the planner would otherwise need many
    # thin colonies to cover the demand. Falls back to flat per-P0 weight if needs aren't given.
    DEPTH_PER_NEED = 3
    needs = {n: max(0.0, float((p0_needs or {}).get(n, 1.0))) for n in p0_names}
    depth_k = {n: max(1, round(needs[n] * DEPTH_PER_NEED)) for n in p0_names}

    col_list = ", ".join(needed_cols.values())
    col_filter = " OR ".join(f"{col}>0" for col in needed_cols.values())
    query = (
        f"SELECT system, constellation, planet_num, planet_type, {col_list} "
        f"FROM pp_planets WHERE ({col_filter})"
    )
    params: list = []
    if systems:
        query += " AND system IN ({})".format(",".join("?" * len(systems)))
        params = list(systems)
    elif constellations:
        query += " AND constellation IN ({})".format(",".join("?" * len(constellations)))
        params = list(constellations)

    try:
        rows = con.execute(query, params).fetchall()
    except Exception:
        return []

    sys_data: dict[str, dict] = {}
    for row in rows:
        sname = row["system"]
        if sname not in sys_data:
            sys_data[sname] = {"constellation": row["constellation"] or "?", "p0": {}, "vals": {}}
        for p0_name, col in needed_cols.items():
            v = row[col] or 0
            if v >= thresh:
                sys_data[sname]["vals"].setdefault(p0_name, []).append(v)
                ex = sys_data[sname]["p0"].get(p0_name)
                if not ex or v > ex["value"]:
                    sys_data[sname]["p0"][p0_name] = {
                        "value": v, "planet_num": row["planet_num"], "planet_type": row["planet_type"],
                    }

    def merge(sys_names):
        merged, vals = {}, {}
        for sname in sys_names:
            for p0_name, info in sys_data[sname]["p0"].items():
                if p0_name not in merged or info["value"] > merged[p0_name]["value"]:
                    merged[p0_name] = {**info, "system": sname}
            for p0_name, vlist in sys_data[sname]["vals"].items():
                vals.setdefault(p0_name, []).extend(vlist)
        for vlist in vals.values():
            vlist.sort(reverse=True)
        return merged, vals

    def _depth_density(p0_name, vals):
        """Mean density of the top-K planets for this resource (K ∝ its demand); missing
        depth counts as 0, so a shallow pool for a high-need resource scores low."""
        k = depth_k.get(p0_name, 1)
        top = (vals.get(p0_name) or [])[:k]
        return sum(top) / k if k else 0.0

    def make_result(sys_names, merged, vals):
        covered = [n for n in p0_names if n in merged]
        first_const = sys_data[sys_names[0]]["constellation"]
        same_const = all(sys_data[s]["constellation"] == first_const for s in sys_names)
        return {
            "constellation": first_const, "same_constellation": same_const,
            "coverage": len(covered), "total_p0": len(p0_names),
            "score": sum(d["value"] for d in merged.values()),
            "depth_score": sum(needs[n] * _depth_density(n, vals) for n in covered),
            "systems_needed": sorted(sys_names),
            "missing": [n for n in p0_names if n not in merged],
            "assignments": [
                {"p0_name": n, "p1_name": "", "system": merged[n]["system"],
                 "planet_num": merged[n]["planet_num"], "planet_type": merged[n]["planet_type"],
                 "value": merged[n]["value"]}
                for n in p0_names if n in merged
            ],
        }

    all_sys = list(sys_data.keys())
    results, seen = [], set()
    pref = max(1, preferred_systems)

    # ── Neighbour (jump-distance) awareness for multi-system combos ──
    # Combos whose systems cluster within `max_jumps` are preferred; non-adjacent
    # combos still appear (fall-back). Single-system combos are trivially "within".
    adj: dict[str, set] = {}
    if pref >= 2 and max_jumps > 0:
        try:
            for r in con.execute("SELECT system, neighbour FROM system_jumps"):
                adj.setdefault(r["system"], set()).add(r["neighbour"])
        except Exception:
            # system_jumps is populated by scripts/populate_geo.py — legitimately absent in a
            # fresh dev checkout that hasn't run it yet, but in production the Dockerfile's boot
            # chain guarantees this table exists before traffic is served, so a failure here is a
            # real regression, not a routine routing quirk. Log it instead of swallowing silently.
            log.exception("system_jumps lookup failed (jump-distance clustering degraded to none)")
            adj = {}
    pair_dist: dict[frozenset, int] = {}
    if adj:
        cand = set(all_sys)
        for a in all_sys:                       # BFS from each candidate up to max_jumps
            seen_d = {a: 0}
            frontier = [a]
            for d in range(1, max_jumps + 1):
                nf = []
                for x in frontier:
                    for y in adj.get(x, ()):
                        if y not in seen_d:
                            seen_d[y] = d
                            nf.append(y)
                frontier = nf
                if not frontier:
                    break
            for b, dd in seen_d.items():
                if b != a and b in cand:
                    pair_dist[frozenset((a, b))] = min(dd, pair_dist.get(frozenset((a, b)), dd))

    def combo_jumps(members):
        """(within_jumps, diameter) — is the combo a cluster within max_jumps?"""
        if len(members) <= 1:
            return True, 0
        edges = {m: set() for m in members}
        ds = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                d = pair_dist.get(frozenset((members[i], members[j])))
                if d is not None:
                    edges[members[i]].add(members[j])
                    edges[members[j]].add(members[i])
                    ds.append(d)
        stack, comp = [members[0]], {members[0]}
        while stack:
            for y in edges[stack.pop()]:
                if y not in comp:
                    comp.add(y); stack.append(y)
        return (len(comp) == len(members), (max(ds) if ds else None))

    def add(sys_names):
        m, vals = merge(sys_names)
        if not any(n in m for n in p0_names):
            return
        key = tuple(sorted(sys_names))
        if key in seen:
            return
        seen.add(key)
        r = make_result(list(sys_names), m, vals)
        within, diam = combo_jumps(list(sys_names))
        r["within_jumps"], r["jumps"] = within, diam
        results.append(r)

    for s in all_sys:
        add([s])
    if pref >= 2 and len(all_sys) > 1:
        for i, s1 in enumerate(all_sys):
            for s2 in all_sys[i + 1:]:
                add([s1, s2])
    if pref >= 3 and len(all_sys) <= 80:
        for i, s1 in enumerate(all_sys):
            for j, s2 in enumerate(all_sys[i + 1:], i + 1):
                for s3 in all_sys[j + 1:]:
                    add([s1, s2, s3])

    # Coverage first (must supply the P0s); then prefer neighbouring clusters; then
    # closeness to the requested system count; then tightness; then need-weighted depth
    # richness (a system with rich planets at the depth each resource needs beats one that's
    # only rich at the very top), with flat best-planet richness as the final tiebreak.
    results.sort(key=lambda r: (
        -r["coverage"],
        0 if r["within_jumps"] else 1,
        abs(len(r["systems_needed"]) - pref),
        r["jumps"] if r["jumps"] is not None else 99,
        -r["depth_score"],
        -r["score"],
    ))
    return results[:top_n]
