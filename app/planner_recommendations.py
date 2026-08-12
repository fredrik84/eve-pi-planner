"""
Planet-DB lookups for the planner: which planets grow a given P0, and
system/constellation recommendations scored by density and jump-distance
clustering. Read-only queries against pp_planets — not part of the
extractor/factory seating algorithm itself (see app/planner.py for that).
"""
import json as _json
import logging
import time

from app.planetary import PLANET_P0_MAP, _NAME_TO_COL

log = logging.getLogger(__name__)

# _system_recommendations is the one uncached hotspot the 2026-07-08 layout/factory-fit Redis
# caching fix didn't cover (see project memory project_eve_pi_planner_frontend_offload). Unlike
# that fix's inputs, none of THIS function's inputs are per-user — p0_names/p0_needs derive from
# the product recipe (fixed per type_id/basket) and constellations/systems/preferred_systems/
# max_jumps/min_density are the request's own scoping choices, all cacheable fleet-wide. The one
# mutable dependency is pp_planets (admin-editable), so this is deliberately NOT the same helper
# as planner.py's _layout_cache_get_or_compute (whose doc comment asserts its cached values are
# pure/never-invalidated static SDE data — that's not true here, reusing it would be exactly the
# "reuse-by-conditional" CLAUDE.md warns against). TTL is hours, not the layout cache's 30 days,
# and _SYSREC_CACHE_VER_KEY is bumped (not scanned) on any pp_planets write — see
# _bump_sysrec_cache_version, called from app.planetary's _invalidate_planetdb_cache.
_SYSREC_CACHE_TTL = 4 * 3600
_SYSREC_CACHE_VER_KEY = "sysrec:ver"
_SYSREC_VER_TTL = 90 * 86400  # generous — only needs to outlive the TTL of entries it versions


def _sysrec_cache_version() -> int:
    from app.cache import cache_get_json
    v = cache_get_json(_SYSREC_CACHE_VER_KEY)
    return int(v) if v is not None else 1


def bump_sysrec_cache_version():
    """Invalidate every cached system-recommendation result in one O(1) write (Redis has no
    wildcard delete via the existing single-key cache_invalidate) by bumping the version prefix
    folded into every cache key — same pattern as planner.py's _LAYOUT_CALC_VER. Call this
    whenever pp_planets changes (see app.planetary._invalidate_planetdb_cache)."""
    from app.cache import cache_set_json
    cache_set_json(_SYSREC_CACHE_VER_KEY, _sysrec_cache_version() + 1, ttl=_SYSREC_VER_TTL)


def _sysrec_cache_key(p0_names, constellations, systems, preferred_systems, max_jumps,
                       min_density, p0_needs) -> str:
    payload = {
        "p0": sorted(p0_names),
        "const": sorted(constellations) if constellations else None,
        "sys": sorted(systems) if systems else None,
        "pref": preferred_systems,
        "mj": max_jumps,
        "dens": min_density,
        # Rounded so float noise (e.g. 4.000000001 vs 4.0 from different call paths) doesn't
        # fragment the cache into near-duplicate keys for what's really the same request.
        "needs": {k: round(v, 2) for k, v in sorted((p0_needs or {}).items())},
    }
    key_body = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sysrec:{_sysrec_cache_version()}:{key_body}"

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

# Confidence-weighted nudge toward real pooled measured yield (pp_planet_yield_avg, see
# app/yield_stats.py) — a planet with SATURATION_SAMPLES+ real samples gets ~full weight toward
# its measured value; fewer samples get a proportionally smaller nudge; zero samples (the vast
# majority of planets — 67 of 5,141 measured as of 2026-07-08) pass through unchanged. Deliberately
# NEVER a flat override of the static richness value (explicit user constraint from Phase 1).
# Applied in Python after the SQL fetch below, not in the SQL itself: the fetch's own
# `ORDER BY {col} DESC LIMIT 300` ranks/truncates by the STATIC value, which is fine (conservative,
# and at current coverage a blended rank would essentially never change the candidate set anyway) —
# doing this arithmetic portably in SQL across SQLite/Postgres is real extra complexity (Postgres
# has no scalar MIN(a,b), only aggregate MIN/LEAST) for no current benefit.
_YIELD_SATURATION_SAMPLES = 30


def _blend_value(static_value: float, measured: dict | None) -> float:
    if not measured:
        return static_value
    weight = min(1.0, measured["n"] / _YIELD_SATURATION_SAMPLES)
    return static_value * (1 - weight) + measured["pct"] * weight


def _fetch_p0_planets(
    p0_names: list[str], con,
    constellations: list[str],
    systems: list[str] | None = None,
    min_density: int = 0,
) -> dict[str, list[dict]]:
    """Return {p0_name: [planets sorted by value DESC]}. Planets thinner than `min_density`%
    are excluded (the density cap), so the planner can't pile extractors onto deposits it isn't
    worth setting up — at the cost of some residual on resources that then can't reach their need."""
    blend_on = True          # measured-yield blending is permanent (flag retired 2026-08-12)
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
            planets = [dict(r) for r in rows]
        except Exception:
            result[name] = []
            continue
        if blend_on and planets:
            # Isolated from the fetch above on purpose: a problem here (e.g. the yield-avg table
            # not yet migrated on a fresh install) must never discard the already-fetched
            # candidate list — worst case, this P0 just doesn't get the measured-yield nudge.
            try:
                measured = {
                    (m["system"], m["planet_num"]): {"pct": m["measured_pct"], "n": m["sample_count"]}
                    for m in con.execute(
                        "SELECT system, planet_num, measured_pct, sample_count "
                        "FROM pp_planet_yield_avg WHERE p0_name=?", (col,),
                    ).fetchall()
                }
                for p in planets:
                    m = measured.get((p["system"], p["planet_num"]))
                    if m:
                        p["value"] = _blend_value(p["value"], m)
            except Exception:
                pass
        result[name] = planets
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
    """Cached + timed wrapper. This is the uncached-until-now hotspot fired on every /api/plan
    and /api/plan-fuelblock request, including the wizard's debounced live re-run on every
    filter tweak — none of its inputs are per-user, so results are cached fleet-wide (see the
    module-level comment above _SYSREC_CACHE_TTL for why this needs its own cache rather than
    reusing planner.py's _layout_cache_get_or_compute). Logged standalone (not just as part of
    the caller's total) so cost + hit rate under real traffic can be read straight from the logs."""
    from app.cache import cache_get_json, cache_set_json
    t0 = time.monotonic()
    key = _sysrec_cache_key(p0_names, constellations, systems, preferred_systems, max_jumps,
                             min_density, p0_needs)
    cached = cache_get_json(key)
    if cached is not None:
        log.info(
            "sysrec HIT n_p0=%d n_sys=%s pref=%d in %.1fms",
            len(p0_names), len(systems) if systems else "*", preferred_systems,
            (time.monotonic() - t0) * 1000,
        )
        return cached

    result = _system_recommendations_impl(
        p0_names, con, top_n=top_n, constellations=constellations, systems=systems,
        preferred_systems=preferred_systems, max_jumps=max_jumps, min_density=min_density,
        p0_needs=p0_needs,
    )
    log.info(
        "sysrec MISS n_p0=%d n_sys=%s pref=%d in %.1fms",
        len(p0_names), len(systems) if systems else "*", preferred_systems,
        (time.monotonic() - t0) * 1000,
    )
    cache_set_json(key, result, ttl=_SYSREC_CACHE_TTL)
    return result


def _system_recommendations_impl(
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

    def _bottleneck(covered, vals):
        """(density, p0_name) of the resource that will bind production here.

        A PI chain consumes every input in a fixed ratio, so output is governed by the WEAKEST
        one — the same min() the plan's supply_ratio reports. depth_score sums the resources
        instead, which lets an abundant input mask a starved one: OP7-BP sums to 353 while its
        Reactive Gas depth is 3.2 (one Gas planet at 19%, against a demand for six planets'
        worth), and the plan built from it runs at 23% fed. Ranking needs the min as well.

        `_depth_density` already divides by the demanded planet count K, so a resource with too
        FEW planets is scored down as hard as one with thin planets — both starve the chain the
        same way, and both are what this term is meant to catch."""
        if not covered:
            return 0.0, None
        return min((_depth_density(n, vals), n) for n in covered)

    def make_result(sys_names, merged, vals):
        covered = [n for n in p0_names if n in merged]
        first_const = sys_data[sys_names[0]]["constellation"]
        same_const = all(sys_data[s]["constellation"] == first_const for s in sys_names)
        bott_density, bott_p0 = _bottleneck(covered, vals)
        return {
            "constellation": first_const, "same_constellation": same_const,
            "coverage": len(covered), "total_p0": len(p0_names),
            "score": sum(d["value"] for d in merged.values()),
            "depth_score": sum(needs[n] * _depth_density(n, vals) for n in covered),
            "bottleneck_density": round(bott_density, 1),
            "bottleneck_p0": bott_p0,
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
            return None
        key = tuple(sorted(sys_names))
        if key in seen:
            return None
        seen.add(key)
        r = make_result(list(sys_names), m, vals)
        within, diam = combo_jumps(list(sys_names))
        r["within_jumps"], r["jumps"] = within, diam
        results.append(r)
        return r

    # Coverage first (must supply the P0s); then prefer neighbouring clusters; then closeness
    # to the requested system count; then tightness; then the BOTTLENECK resource, banded; then
    # need-weighted depth richness; with flat best-planet richness as the final tiebreak.
    #
    # The bottleneck sits above depth_score because output is a min() over inputs, not a sum:
    # among equal-coverage systems the summed score picked C3I-D5 (depth 377, binding resource
    # at 2.5) over F18-AY (depth 328, binding at 13.0) — i.e. the one that would run ~5x more
    # starved. It's banded in BOTTLENECK_BAND-wide steps so only a MATERIAL difference in the
    # weakest input outranks overall richness; inside a band the existing depth ordering stands,
    # which keeps this from churning the recommendation list over rounding-level noise.
    BOTTLENECK_BAND = 5.0

    def rank_key(r):
        return (
            -r["coverage"],
            0 if r["within_jumps"] else 1,
            abs(len(r["systems_needed"]) - pref),
            r["jumps"] if r["jumps"] is not None else 99,
            -int(r.get("bottleneck_density", 0) / BOTTLENECK_BAND),
            -r["depth_score"],
            -r["score"],
        )

    for s in all_sys:
        add([s])
    pair_results: list[dict] = []
    if pref >= 2 and len(all_sys) > 1:
        for i, s1 in enumerate(all_sys):
            for s2 in all_sys[i + 1:]:
                r = add([s1, s2])
                if r:
                    pair_results.append(r)
    if pref >= 3 and len(all_sys) <= 80:
        # Exhaustive triples (C(n,3), up to ~82k at the 80-system cap) made the wizard's live
        # "Find Systems" re-run take multiple seconds on a wide multi-constellation selection —
        # confirmed via profiling. Instead of every triple, extend only the BEAM_WIDTH
        # already-ranked best pairs with a third system (~beam_width × n candidates instead of
        # n³) — verified against the exhaustive search on real data (several constellation
        # scopes, two different products, beam widths down to 10) to return identical top-10
        # results every time, since a genuinely great triple's best two members are themselves
        # already a strong pair. ~20x faster at the 80-system cap with a safety margin above
        # the smallest beam width that matched exhaustively in testing.
        BEAM_WIDTH = 30
        pair_results.sort(key=rank_key)
        for pr in pair_results[:BEAM_WIDTH]:
            s1, s2 = pr["systems_needed"]
            for s3 in all_sys:
                if s3 != s1 and s3 != s2:
                    add([s1, s2, s3])

    results.sort(key=rank_key)
    return results[:top_n]
