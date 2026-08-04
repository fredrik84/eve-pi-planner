"""
Planning algorithm — extractor/factory assignment across characters.

The pure decision-making half of the planner: bipartite feasibility, slot budgeting, the
Bresenham need list, the extractor assignment passes (+ swaps, absorb, waterfill), split
extractors and factory-planet placement. Carved out of `planner.py`, which had grown to ~3,900
lines covering this, the advisor, the dashboard and the orchestration all at once.

This module is a LEAF: it imports nothing from `planner.py` (the orchestration imports FROM here),
which is what keeps the split acyclic. Keep it that way — these helpers take their inputs as
arguments (`con`, `req`, char lists) rather than reaching for request state, so the algorithm
stays testable without a session.
"""
import json as _json
from math import ceil

from app.esi import PI_CHAR_SQL
from app.planner_recommendations import (
    _P0_PLANET_TYPES, _fetch_p0_planets, _system_recommendations,
)

def _max_matching_slots(slot_planet_lists: list[list[dict]]) -> int:
    """
    Maximum bipartite matching: how many slots can be assigned a unique planet, where
    each slot has its own independent planet candidate list. Used for per-character
    feasibility checks where some slots have committed planets.
    """
    planet_to_idx: dict[tuple, int] = {}
    for planets in slot_planet_lists:
        for p in planets:
            k = (p["system"], p["planet_num"])
            if k not in planet_to_idx:
                planet_to_idx[k] = len(planet_to_idx)
    n_planets = len(planet_to_idx)
    adj = [
        [planet_to_idx[(p["system"], p["planet_num"])]
         for p in planets if (p["system"], p["planet_num"]) in planet_to_idx]
        for planets in slot_planet_lists
    ]
    match_planet = [-1] * n_planets

    def augment(slot, seen):
        for p in adj[slot]:
            if p in seen: continue
            seen.add(p)
            if match_planet[p] == -1 or augment(match_planet[p], seen):
                match_planet[p] = slot
                return True
        return False

    return sum(1 for s in range(len(slot_planet_lists)) if augment(s, set()))


def _slot_to_planet_list(slot: dict, planet_lists: dict) -> list[dict]:
    """
    Return the effective planet candidate list for a single extractor slot.
    If the slot already has a committed planet (actual_system / actual_planet_num),
    return just that one planet so the matching pins this slot and doesn't
    consume restricted-list capacity needed by other unplaced slots.
    """
    sys_ = slot.get("actual_system") or slot.get("system") or ""
    num = slot.get("actual_planet_num")
    if num is None:
        num = slot.get("planet_num")
    if sys_ and num is not None:
        return [{"system": sys_, "planet_num": num}]
    p0 = slot.get("p0_name", "")
    return list(planet_lists.get(p0, []))


def _can_add_p0(extractors: list[dict], new_p0: str, restricted: dict) -> bool:
    """
    Return True if new_p0 can be added to this character's extractor list.
    Slots with committed planets are pinned; uncommitted slots use the restricted list.
    """
    slot_lists = [_slot_to_planet_list(s, restricted) for s in extractors if s.get("p0_name")]
    slot_lists.append(list(restricted.get(new_p0, [])))
    return _max_matching_slots(slot_lists) >= len(slot_lists)


# ── Planning algorithm helpers ────────────────────────────────────────────────

def _compute_factory_shares(
    char_list: list[dict],
    factories: int,
    auto_mode: bool,
    per_char_fac_cap: int | None = None,
    preferred_cids: list[int] | None = None,
) -> dict[int, int]:
    """
    Distribute factory planet slots across factory-eligible characters.
    per_char_fac_cap: max factory planets any single char can physically place
    (= Barren/Temperate count in the best factory system). Prevents greedy
    over-allocation that produces unplaceable overflow.
    preferred_cids: in auto mode, these characters host factories first (in the
    given order); remaining factories spill onto other chars only if needed. Lets
    the user steer WHICH chars get factories without making them factory-only.
    """
    fac_only = [(c, c["effective_planets"]) for c in char_list if c["extractor_limit"] == 0]
    if auto_mode:
        lim_ext = [(c, c["effective_planets"]) for c in char_list]
        if preferred_cids:
            pref_rank = {cid: i for i, cid in enumerate(preferred_cids)}
            # Stable sort: preferred chars first (in given order), others keep order.
            lim_ext.sort(key=lambda cc: pref_rank.get(cc[0]["character_id"], len(pref_rank)))
    else:
        lim_ext = [
            (c, max(0, c["effective_planets"] - c["extractor_limit"]))
            for c in char_list if c["extractor_limit"] not in (None, 0)
        ]

    shares: dict[int, int] = {}
    rem = factories

    for c, cap in fac_only:
        effective_cap = min(cap, per_char_fac_cap) if per_char_fac_cap else cap
        give = min(effective_cap, rem)
        shares[c["character_id"]] = give
        rem -= give

    n_lim = len(lim_ext)
    if n_lim > 0 and rem > 0:
        if auto_mode:
            # Consolidate onto the FEWEST characters, but spread evenly among those
            # chosen so no single char is packed to the full B/T count unless required.
            # Packing a char to per_char_fac_cap reserves ALL the system's B/T planets,
            # which can sterilise that char's remaining slots from extracting a P0 that
            # only grows on a B/T planet. An even spread keeps B/T headroom for extraction.
            caps = [min(cap, per_char_fac_cap) if per_char_fac_cap else cap for c, cap in lim_ext]
            total_cap = sum(caps)
            give_total = min(rem, total_cap)
            cap_unit = per_char_fac_cap or (max(caps) if caps else 1)
            if preferred_cids:
                # Spread across ALL the user-chosen chars (they lead lim_ext). Extend to
                # more chars only if the chosen ones can't physically hold all factories.
                n_pref = sum(1 for c, _ in lim_ext if c["character_id"] in set(preferred_cids))
                n_chars = n_pref
                while n_chars < n_lim and sum(caps[:n_chars]) < give_total:
                    n_chars += 1
                n_chars = max(1, n_chars)
            else:
                # No preference: consolidate onto the minimum number of chars.
                n_chars = max(1, -(-give_total // cap_unit)) if cap_unit else n_lim
            n_chars = min(n_chars, n_lim)
            base, extra = divmod(give_total, n_chars)
            for i, (c, cap) in enumerate(lim_ext):
                if i >= n_chars or rem <= 0:
                    break
                want = base + (1 if i < extra else 0)
                give = min(caps[i], want, rem)
                shares[c["character_id"]] = give
                rem -= give
            # Any leftover (from cap clipping) spills onto the next available chars
            if rem > 0:
                for i, (c, cap) in enumerate(lim_ext):
                    if rem <= 0:
                        break
                    cur = shares.get(c["character_id"], 0)
                    room = caps[i] - cur
                    if room > 0:
                        add = min(room, rem)
                        shares[c["character_id"]] = cur + add
                        rem -= add
        else:
            base, extra = divmod(rem, n_lim)
            for i, (c, cap) in enumerate(lim_ext):
                effective_cap = min(cap, per_char_fac_cap) if per_char_fac_cap else cap
                give = min(effective_cap, base + (1 if i < extra else 0))
                shares[c["character_id"]] = give
                rem -= give

    return shares


def _compute_slot_budget(
    char_list: list[dict],
    op_pct: int,
    factory_output_per_hour: float | None,
    cycle_time: int,
    output_qty: int,
    p1_fracs: dict,
    per_char_fac_cap: int | None = None,
    preferred_cids: list[int] | None = None,
) -> tuple[int, int, dict[int, int], bool, float, int]:
    """
    Compute how many factory slots vs extractor slots to allocate.
    Returns (ext_slots, factories, factory_shares, auto_mode, p0_per_factory_per_day,
    factories_unbudgeted).
    """
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

    cycles_per_day = int(86400 / cycle_time)
    if factory_output_per_hour is not None and factory_output_per_hour > 0:
        prod_per_factory_day = factory_output_per_hour * 24
    else:
        prod_per_factory_day = float(cycles_per_day * output_qty)

    p0_per_factory_day = prod_per_factory_day * sum(p1_fracs.values()) * 150
    kk = 48_000 * 24  # baseline P0/day per extractor slot at 100% quality
    op = 1 + op_pct / 100

    auto_mode = F == 0 and E > 0
    if auto_mode:
        F, E = E, 0

    if p0_per_factory_day > 0 and (E + F) > 0:
        denom = p0_per_factory_day * op + kk
        factories = max(1, int((E + F) * kk / denom)) if denom > 0 else F
        factories = min(factories, F)
        ext_slots = E + F - factories
    else:
        factories, ext_slots = 1, E

    shares = _compute_factory_shares(char_list, factories, auto_mode, per_char_fac_cap, preferred_cids)

    # The equilibrium formula budgets factories against raw planet slots, but a share can only
    # be handed to a character who can physically host it: per_char_fac_cap clips each char at
    # the factory system's Barren/Temperate count (one colony per planet per char), and in
    # explicit mode only chars with a non-zero extractor_limit can absorb the remainder. When
    # both run out the leftover factories are dropped on the floor — budgeting output for a
    # planet nobody can colonise inflates products_per_day. Report the placeable count instead,
    # and hand the shortfall back so the UI can say why.
    placeable = sum(shares.values())
    factories_unbudgeted = max(0, factories - placeable)
    if factories_unbudgeted and placeable > 0:
        factories = placeable
        ext_slots = E + F - factories

    return ext_slots, factories, shares, auto_mode, p0_per_factory_day, factories_unbudgeted


def _density_estimate(p1_info, p0_planet_lists, ext_slots, has_planet_db) -> dict[str, float]:
    """Per-P0 achievable density as a fraction of a full bar (1.0 = 100% ≈ 48k/cycle), taken from
    the richest planets that P0 would actually use. A resource on thinner deposits produces less
    per extractor, so it needs proportionally MORE extractors to keep production in the recipe
    ratio — which minimises leftover P1 from one input under-performing."""
    if not has_planet_db:
        return {info["p0_name"]: 1.0 for info in p1_info}
    total_rel = sum(info["relative_qty"] for info in p1_info) or 1
    est: dict[str, float] = {}
    for info in p1_info:
        name = info["p0_name"]
        planets = p0_planet_lists.get(name, [])
        if not planets:
            est[name] = 1.0
            continue
        base_n = max(1, round(ext_slots * info["relative_qty"] / total_rel))  # ~planets it'd use
        top = planets[:base_n]
        est[name] = max(0.05, sum(p["value"] for p in top) / len(top) / 100.0)
    return est


def _build_need_list(
    p1_info: list[dict],
    ext_slots: int,
    needed_at_baseline: int,
    p0_caps: dict[str, int],
    scarcity_bonus: dict[str, float],
    density_est: dict[str, float] | None = None,
) -> list[dict]:
    """Build Bresenham-ordered list of extractor slots to fill.

    With density_est, each P0's slot weight is scaled UP when its planets are thin
    (relative_qty / density), so a low-density input gets more extractors and production lands in
    the recipe ratio despite uneven planet quality — minimising leftover P1. Without it, weight =
    relative_qty (the original need-proportional behaviour)."""
    if density_est:
        weight = {info["p0_name"]: info["relative_qty"] / max(0.05, density_est.get(info["p0_name"], 1.0))
                  for info in p1_info}
    else:
        weight = {info["p0_name"]: info["relative_qty"] for info in p1_info}
    total_w = sum(weight.values()) or 1
    accum = {info["p0_name"]: 0.0 for info in p1_info}
    p0_counts = {info["p0_name"]: 0 for info in p1_info}
    need_list = []
    for i in range(ext_slots):
        for info in p1_info:
            accum[info["p0_name"]] += weight[info["p0_name"]] / total_w
        capped = [inf for inf in p1_info if p0_counts[inf["p0_name"]] < p0_caps[inf["p0_name"]]]
        pool = capped if capped else p1_info
        best = max(pool, key=lambda inf: accum[inf["p0_name"]] + scarcity_bonus[inf["p0_name"]])
        accum[best["p0_name"]] -= 1.0
        p0_counts[best["p0_name"]] += 1
        need_list.append({
            "p0_type_id":       best["p0_type_id"],
            "p0_name":          best["p0_name"],
            "p1_type_id":       best["p1_type_id"],
            "p1_name":          best["p1_name"],
            "planet_types":     best["planet_types"],
            "best_planet_type": best["best_planet_type"],
            "relative_qty":     best["relative_qty"],
            "is_extra":         i >= needed_at_baseline,
        })
    return need_list


def _run_swap_pass(
    assignments: list[dict],
    char_list: list[dict],
    remaining: list[dict],
    p0_planet_lists: dict,
    char_nonfac: dict[int, list],
    p1_info: list[dict],
    allow_synthetic: bool,
    factory_avoid_cids: set[int] | None = None,
    factory_avoid: set[tuple] | None = None,
) -> list[dict]:
    """
    Resolve infeasible extractor slots by swapping P0 types between characters.
    allow_synthetic: when remaining is empty, try to create synthetic swap candidates.
    Returns updated remaining list; mutates assignments in-place.
    """
    def restricted(cid):
        nk = {(p.get("system_name"), p.get("planet_num"))
              for p in char_nonfac.get(cid, [])
              if p.get("system_name") and p.get("planet_num") is not None}
        # Factory chars that need ALL system B/T planets keep them free for factories.
        if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
            nk = nk | factory_avoid
        return ({n: [p for p in pl if (p["system"], p["planet_num"]) not in nk]
                 for n, pl in p0_planet_lists.items()} if nk else p0_planet_lists)

    p0_info_by_name = {info["p0_name"]: info for info in p1_info if info.get("p0_name")}

    def make_slot(info):
        return {
            "p0_type_id": info["p0_type_id"], "p0_name": info["p0_name"],
            "p1_type_id": info["p1_type_id"], "p1_name": info["p1_name"],
            "planet_types": info["planet_types"], "best_planet_type": info["best_planet_type"],
            "relative_qty": info["relative_qty"], "is_extra": False,
        }

    for asgn_a, char_a in zip(assignments, char_list):
        if char_a["extractor_limit"] == 0:
            continue
        free_a = char_a["computed_ext_cap"] - len(asgn_a["extractors"])
        if free_a <= 0:
            continue

        rest_a = restricted(char_a["character_id"])

        cand_x: list[tuple] = []
        for ri, slot in enumerate(remaining):
            p0 = slot.get("p0_name")
            if p0 and not _can_add_p0(asgn_a["extractors"], p0, rest_a):
                cand_x.append((ri, slot, p0, False))
        if not cand_x and allow_synthetic and not remaining:
            for info_x in sorted(p1_info, key=lambda i: -i["relative_qty"]):
                p0 = info_x.get("p0_name")
                if p0 and not _can_add_p0(asgn_a["extractors"], p0, rest_a):
                    cand_x.append((None, make_slot(info_x), p0, True))
                    break

        swap_done = False
        for ri_x, slot_x, p0_x, synthetic in cand_x:
            for asgn_b, char_b in zip(assignments, char_list):
                if char_b["character_id"] == char_a["character_id"] or char_b["extractor_limit"] == 0:
                    continue
                if char_b["computed_ext_cap"] != len(asgn_b["extractors"]):
                    continue
                rest_b = restricted(char_b["character_id"])

                for ei, ext_y in enumerate(asgn_b["extractors"]):
                    p0_y = ext_y.get("p0_name")
                    if not p0_y or p0_y == p0_x or p0_y not in p0_info_by_name:
                        continue
                    if not _can_add_p0(asgn_a["extractors"], p0_y, rest_a):
                        continue
                    ext_b_without_ei = [e for j, e in enumerate(asgn_b["extractors"]) if j != ei]
                    if not _can_add_p0(ext_b_without_ei, p0_x, rest_b):
                        continue
                    replace_ptype = ext_y.get("existing_ptype") or ext_y.get("planet_type")
                    asgn_b["extractors"][ei] = {
                        **slot_x, "is_existing": False, "is_replace": True, "replace_ptype": replace_ptype,
                    }
                    asgn_a["extractors"].append({**make_slot(p0_info_by_name[p0_y]), "is_existing": False, "is_replace": False})
                    if not synthetic and ri_x is not None:
                        remaining.pop(ri_x)
                    swap_done = True
                    break
                if swap_done:
                    break
            if swap_done:
                break

    return remaining


def _assign_extractors(
    assignments: list[dict],
    char_list: list[dict],
    need_list: list[dict],
    char_spare_planets: dict[int, list],
    char_nonfac: dict[int, list],
    req,
    p0_planet_lists: dict,
    has_planet_db: bool,
    has_system_name: bool,
    p1_info: list[dict],
    factory_avoid_cids: set[int] | None = None,
    factory_avoid: set[tuple] | None = None,
) -> list[dict]:
    """Run all extractor assignment passes. Returns unassigned slots."""
    remaining = list(need_list)

    # Pass 1: match existing extractor planets that already produce the needed P0
    if req.use_existing:
        for asgn, char in zip(assignments, char_list):
            if asgn["factory_only"]:
                continue
            cid = char["character_id"]
            spare = char_spare_planets[cid]
            used = set()
            # Factory chars avoid using Barren/Temperate factory planets as extractors
            is_factory_char = bool(factory_avoid_cids and cid in factory_avoid_cids)
            for i, planet in enumerate(spare):
                if len(asgn["extractors"]) >= char["computed_ext_cap"]:
                    break
                if not planet.get("is_extractor"):
                    continue
                p0_id = planet.get("p0_type_id")
                if not p0_id:
                    continue
                # Skip if this planet is reserved for factory assignment
                if is_factory_char and factory_avoid:
                    pkey = (planet.get("system_name", ""), planet.get("planet_num"))
                    if pkey in factory_avoid:
                        continue
                for j, need in enumerate(remaining):
                    if need["p0_type_id"] != p0_id:
                        continue
                    planet_sys = planet.get("system_name", "")
                    in_chosen = (
                        not has_system_name or not req.chosen_systems or planet_sys in req.chosen_systems
                    )
                    if not in_chosen and has_planet_db and need.get("p0_name"):
                        if not _can_add_p0(asgn["extractors"], need["p0_name"], p0_planet_lists):
                            break
                    asgn["extractors"].append({
                        **need,
                        "is_existing":       in_chosen,
                        "is_replace":        not in_chosen,
                        "existing_ptype":    planet.get("planet_type"),
                        "actual_system":     planet.get("system_name") or "",
                        "actual_planet_num": planet.get("planet_num"),
                    })
                    remaining.pop(j)
                    used.add(i)
                    break
            char_spare_planets[cid] = [p for i, p in enumerate(spare) if i not in used]

    # Swap pass before Pass 2
    if has_planet_db:
        remaining = _run_swap_pass(
            assignments, char_list, remaining, p0_planet_lists, char_nonfac, p1_info,
            allow_synthetic=False, factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
        )

    # Pass 2: fill remaining free extractor slots
    for asgn, char in zip(assignments, char_list):
        if asgn["factory_only"]:
            continue
        cid = char["character_id"]
        max_ext = char["computed_ext_cap"]
        free = max_ext - len(asgn["extractors"])
        spare = char_spare_planets.get(cid, [])
        n_empty = char["effective_planets"] - len(char["planets"])
        nonfac_keys: set[tuple] = set()
        if has_system_name:
            nonfac_keys = {
                (p.get("system_name"), p.get("planet_num"))
                for p in char_nonfac.get(cid, [])
                if p.get("system_name") and p.get("planet_num") is not None
            }
        # Factory chars needing all B/T also avoid those planets for extractors
        if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
            nonfac_keys |= factory_avoid

        while free > 0 and remaining:
            n_already_new = sum(1 for e in asgn["extractors"] if not e.get("is_existing") and not e.get("is_replace"))
            using_empty = n_already_new < n_empty
            current_p0s = [s["p0_name"] for s in asgn["extractors"] if s.get("p0_name")]
            current_p0s_set = set(current_p0s)
            found_idx = None

            if has_planet_db:
                restricted = (
                    {name: [p for p in planets if (p["system"], p["planet_num"]) not in nonfac_keys]
                     for name, planets in p0_planet_lists.items()}
                    if nonfac_keys else p0_planet_lists
                )
                for allow_dup in (False, True):
                    for i, cand in enumerate(remaining):
                        p0_name = cand.get("p0_name")
                        if not allow_dup and p0_name in current_p0s_set:
                            continue
                        if p0_name and _can_add_p0(asgn["extractors"], p0_name, restricted):
                            found_idx = i
                            break
                    if found_idx is not None:
                        break
            else:
                found_idx = 0

            if found_idx is None:
                break

            need = remaining.pop(found_idx)
            if using_empty:
                asgn["extractors"].append({**need, "is_existing": False, "is_replace": False})
            elif spare:
                reuse = spare.pop(0)
                asgn["extractors"].append({**need, "is_existing": False, "is_replace": True, "replace_ptype": reuse.get("planet_type")})
            else:
                asgn["extractors"].append({**need, "is_existing": False, "is_replace": False})
            free -= 1

    # Post-pass-2 swap
    if has_planet_db:
        remaining = _run_swap_pass(
            assignments, char_list, remaining, p0_planet_lists, char_nonfac, p1_info,
            allow_synthetic=True, factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
        )

    return remaining


def _absorb_remaining(
    assignments: list[dict],
    char_list: list[dict],
    remaining: list[dict],
    p0_planet_lists: dict,
    char_nonfac: dict[int, list],
    p1_info: list[dict],
    density_est: dict[str, float] | None,
    has_system_name: bool,
    factory_avoid_cids: set[int] | None = None,
    factory_avoid: set[tuple] | None = None,
) -> list[dict]:
    """Re-target genuinely-unplaceable extractor slots onto a free reachable planet of a
    *different* P0, so a usable planet slot isn't left dangling.

    A min-density cap can make a thin-deposit P0 (e.g. Reactive Gas) unplaceable for a
    character that still has spare extractor capacity and could colonise a richer planet.
    Rather than report the slot as unassigned, grow the most under-produced *placeable* P0
    there (density-weighted deficit) — minimal added residual, and the planet gets used.
    Mutates assignments; returns the slots that still can't be placed anywhere."""
    if not remaining:
        return remaining
    infos = [i for i in p1_info if i.get("p0_name")]
    if not infos:
        return remaining
    dens = density_est or {}
    total_rel = sum(i["relative_qty"] for i in infos) or 1

    def _q(name):
        return max(0.05, dens.get(name, 1.0))

    prod = {i["p0_name"]: 0.0 for i in infos}
    for asgn in assignments:
        for e in asgn["extractors"]:
            n = e.get("p0_name")
            if n in prod:
                prod[n] += _q(n)

    def deficit(name, rel):
        tp = sum(prod.values()) or 1
        return rel / total_rel - prod[name] / tp

    for asgn, char in zip(assignments, char_list):
        if asgn["factory_only"] or not remaining:
            continue
        free = char["computed_ext_cap"] - len(asgn["extractors"])
        if free <= 0:
            continue
        cid = char["character_id"]
        nonfac_keys: set[tuple] = set()
        if has_system_name:
            nonfac_keys = {
                (p.get("system_name"), p.get("planet_num"))
                for p in char_nonfac.get(cid, [])
                if p.get("system_name") and p.get("planet_num") is not None
            }
        if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
            nonfac_keys |= factory_avoid
        restricted = (
            {name: [p for p in planets if (p["system"], p["planet_num"]) not in nonfac_keys]
             for name, planets in p0_planet_lists.items()}
            if nonfac_keys else p0_planet_lists
        )
        while free > 0 and remaining:
            placeable = [i for i in infos
                         if _can_add_p0(asgn["extractors"], i["p0_name"], restricted)]
            if not placeable:
                break
            best = max(placeable, key=lambda i: deficit(i["p0_name"], i["relative_qty"]))
            remaining.pop()
            asgn["extractors"].append({
                "p0_type_id":       best["p0_type_id"],
                "p0_name":          best["p0_name"],
                "p1_type_id":       best["p1_type_id"],
                "p1_name":          best["p1_name"],
                "planet_types":     best["planet_types"],
                "best_planet_type": best["best_planet_type"],
                "relative_qty":     best["relative_qty"],
                "is_extra":         True,
                "is_existing":      False,
                "is_replace":       False,
                "is_absorbed":      True,
            })
            prod[best["p0_name"]] += _q(best["p0_name"])
            free -= 1
    return remaining


def _waterfill_new_slots(
    new_slots: list[tuple],
    char_used_map: dict[int, set],
    nonfac_occ_map: dict[int, set],
    p0_planet_lists: dict,
) -> None:
    """Lever 1 — per-character planet assignment that gives each shared planet to the
    resource that needs it most.

    Picking planets in a fixed resource order hands a planet type's richest planets to
    whichever resource is processed first — *systematically* across every character — so
    when two P0s share a planet type the loser is starved onto thin planets (e.g. Complex
    Organisms dropping to quality 3 while a co-resource sits on the shared 61). This pass
    runs a regret heuristic per character: repeatedly place the slot with the largest gap
    between its best and next-best still-free planet, so a resource whose alternative is
    catastrophic (61 → 3) claims the shared planet over one whose alternative is fine
    (61 → 60). Per-character planet uniqueness is respected; planets still reuse freely
    across different characters. Mutates the slot dicts in place."""
    by_char: dict[int, list] = {}
    for cid, slot in new_slots:
        if slot.get("p0_name"):
            by_char.setdefault(cid, []).append(slot)

    for cid, slots in by_char.items():
        used = char_used_map[cid]
        occ = nonfac_occ_map.get(cid, set())

        def candidates(slot):
            # Free planets first (value-descending), factory-reserved planets only as a
            # last resort — matching the old soft-avoid, so extraction doesn't poach a
            # factory planet while a free one exists.
            free, soft = [], []
            for p in p0_planet_lists.get(slot.get("p0_name"), []):
                k = (p["system"], p["planet_num"])
                if k in used:
                    continue
                (soft if k in occ else free).append(p)
            return free + soft

        pending = list(slots)
        while pending:
            choice = None  # (regret, slot, planet)
            for slot in pending:
                cands = candidates(slot)
                if not cands:
                    continue
                regret = cands[0]["value"] - (cands[1]["value"] if len(cands) > 1 else 0)
                if choice is None or regret > choice[0]:
                    choice = (regret, slot, cands[0])
            if choice is None:
                break  # no remaining slot can be placed for this character
            _, slot, pl = choice
            slot["system"] = pl["system"]
            slot["planet_num"] = pl["planet_num"]
            slot["planet_type"] = pl["planet_type"]
            slot["quality_pct"] = round(pl["value"])
            used.add((pl["system"], pl["planet_num"]))
            pending.remove(slot)


def _attach_extractor_planet_details(
    assignments: list[dict],
    char_list: list[dict],
    char_nonfac: dict[int, list],
    char_nonfac_ext: dict[int, list],
    p0_planet_lists: dict,
    p0_planet_lists_global: dict,
    req,
    auto_mode: bool,
    factory_avoid_cids: set[int] | None = None,
    factory_avoid: set[tuple] | None = None,
) -> None:
    """Attach system/planet_num/quality_pct to each extractor slot. Mutates assignments.

    Existing (already-built) colonies are pinned to their real planets per character;
    new slots are then placed by a global need-balanced water-fill (_waterfill_new_slots,
    lever 1) so resources sharing a planet type don't get starved onto thin planets."""
    char_used_map: dict[int, set] = {}
    nonfac_occ_map: dict[int, set] = {}
    new_slots_global: list[tuple] = []
    for asgn, char in zip(assignments, char_list):
        actual_ext = len(asgn["extractors"])
        cid = char["character_id"]
        max_ext_cap = char["computed_ext_cap"]
        total_non_ext = char["effective_planets"] - actual_ext
        existing_factory = min(len(char_nonfac.get(cid, [])), total_non_ext)
        configured_factory = max(0, char["effective_planets"] - max_ext_cap)
        if char["extractor_limit"] is None and not auto_mode:
            factory_planets = existing_factory
        else:
            factory_planets = max(configured_factory, existing_factory)
        asgn["factory_planets"] = factory_planets
        asgn["free_planets"] = total_non_ext - factory_planets

        char_used: set[tuple] = set()

        def _avail(slot, _used=char_used) -> int:
            p0 = slot.get("p0_name")
            if not p0:
                return 999
            src = p0_planet_lists_global if (slot.get("is_existing") and not req.chosen_systems) else p0_planet_lists
            return sum(1 for p in src.get(p0, []) if (p["system"], p["planet_num"]) not in _used)

        def _priority(slot):
            return -1 if (slot.get("is_existing") and not req.chosen_systems) else _avail(slot)

        nonfac_occupied = {
            (p.get("system_name"), p.get("planet_num"))
            for p in char_nonfac_ext.get(cid, [])
            if p.get("system_name") and p.get("planet_num") is not None
        }
        # Factory chars needing all B/T keep those planets free for factories.
        if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
            nonfac_occupied = nonfac_occupied | factory_avoid

        # Pass 1: pin existing (already-built) colonies to their real planets; defer the
        # new slots to the global need-balanced water-fill below.
        for slot in sorted([s for s in asgn["extractors"] if s.get("is_existing")], key=_priority):
            p0_name = slot.get("p0_name")
            if not p0_name:
                continue
            actual_sys = slot.get("actual_system") or ""
            actual_num = slot.get("actual_planet_num")
            if actual_sys and actual_num is not None and (actual_sys, actual_num) not in char_used:
                slot["system"] = actual_sys
                slot["planet_num"] = actual_num
                char_used.add((actual_sys, actual_num))
                pp_entry = next(
                    (p for p in p0_planet_lists_global.get(p0_name, [])
                     if p["system"] == actual_sys and p["planet_num"] == actual_num),
                    None,
                )
                if pp_entry:
                    slot["quality_pct"] = round(pp_entry["value"])
            else:
                src = p0_planet_lists if req.chosen_systems else p0_planet_lists_global
                planet = next(
                    (p for p in src.get(p0_name, []) if (p["system"], p["planet_num"]) not in char_used),
                    None,
                )
                if planet:
                    slot["system"] = planet["system"]
                    slot["planet_num"] = planet["planet_num"]
                    slot["quality_pct"] = round(planet["value"])
                    char_used.add((planet["system"], planet["planet_num"]))

        char_used_map[cid] = char_used
        nonfac_occ_map[cid] = nonfac_occupied
        for slot in asgn["extractors"]:
            if not slot.get("is_existing") and slot.get("p0_name"):
                new_slots_global.append((cid, slot))

    _waterfill_new_slots(new_slots_global, char_used_map, nonfac_occ_map, p0_planet_lists)


# ── Split-extraction consolidation (opt-in) ──────────────────────────────────────
#
# A planet can host TWO extractor control units, splitting its 10-head budget between two
# P0 deposits and feeding two Basic Industry lines → two P1s. This pass merges pairs of a
# *single* character's one-P0 extractor planets into one such split planet when the two P0s
# can be drawn from one physical planet, freeing a planet slot.
#
# Feasibility is accounted in PLANET-units (10 heads = 1 planet), the SAME quality-agnostic
# 48k-baseline the slot budget uses — so a conservative split preserves exactly the baseline
# production the non-split plan targeted (quality shortfalls, which the planner already
# surfaces separately, are neither created nor hidden here). Conservative commits a merge
# only when every P0 still meets its baseline planet-need with ≤10 heads (minimal heads to
# cover each leg's deficit, leftover heads spread as buffer); aggressive packs into 10 heads
# even when that underfills a leg (heads ∝ need). Per-leg quality is recorded for display and
# the P0/day stat; head counts are guidance — actual yield depends on heatmap placement +
# depletion, which is not a static number.
_PU_PER_PLANET_DAY = 4_800 * 24    # P0/day from one extractor head at 100% richness (stats only)
_PLANET_P0_PER_DAY = 48_000 * 24   # P0/day from a full 10-head planet at 100% richness


def _slot_planet_type(e: dict) -> str | None:
    return e.get("planet_type") or e.get("existing_ptype") or e.get("replace_ptype") or e.get("best_planet_type")


def _ext_leg_qualities(extractors: list[dict]) -> list[int]:
    """Quality values for averaging, expanding a split planet into its two legs."""
    out: list[int] = []
    for e in extractors:
        if e.get("split"):
            out += [leg["quality_pct"] for leg in e.get("legs", []) if leg.get("quality_pct") is not None]
        elif e.get("quality_pct") is not None:
            out.append(e["quality_pct"])
    return out


def _basics_factor(planet_type: str | None, cc: int, no_storage: bool = False) -> float:
    """Fraction of full on-planet P1 refining the planet can actually do: 8 Basic Industry
    Facilities fully convert a 100%-quality planet's extraction; fewer fit on a low-CC or big
    planet (links eat the grid), so it refines proportionally less P1 on-site. 1.0 if
    unknown. `no_storage` (buffer in the launchpad, drop the storage hub) frees ~700 PG so more
    basics fit. A planet's effective P1 output is then min(quality, basics-factor) — whichever of
    extraction richness or on-site refining is the bottleneck."""
    if not planet_type:
        return 1.0
    try:
        from app.layout import fitted_extractor_basics
        return max(0.125, min(1.0, fitted_extractor_basics(planet_type, cc, no_storage) / 8.0))
    except Exception:
        return 1.0


def _ext_actual_p0_per_day(extractors: list[dict], cc: int = 5, no_storage: bool = False) -> float:
    """Effective P0/day refined to P1, capped by on-planet basics (min of quality & basics
    factor). Split legs are counted as heads × quality (the basics cap isn't modelled per leg)."""
    total = 0.0
    for e in extractors:
        if e.get("split"):
            for leg in e.get("legs", []):
                total += leg.get("heads", 0) * leg.get("quality_pct", 100) / 100.0 * _PU_PER_PLANET_DAY
        else:
            eff = min(e.get("quality_pct", 100) / 100.0, _basics_factor(_slot_planet_type(e), cc, no_storage))
            total += eff * 48_000 * 24
    return total


def _actual_p0_per_day_by_p0(extractors: list[dict], cc: int = 5, no_storage: bool = False) -> dict[str, float]:
    """Effective P0/day per resource (P0 name), capped by on-planet basics — so a resource sitting
    on low-CC/big planets that can't refine all its P0 shows as the binding bottleneck."""
    out: dict[str, float] = {}
    for e in extractors:
        if e.get("split"):
            for leg in e.get("legs", []):
                n = leg.get("p0_name")
                if n:
                    out[n] = out.get(n, 0.0) + leg.get("heads", 0) * leg.get("quality_pct", 100) / 100.0 * _PU_PER_PLANET_DAY
        else:
            n = e.get("p0_name")
            if n:
                eff = min(e.get("quality_pct", 100) / 100.0, _basics_factor(_slot_planet_type(e), cc, no_storage))
                out[n] = out.get(n, 0.0) + eff * 48_000 * 24
    return out


def _consolidate_split_extractors(
    assignments: list[dict],
    p0_need_pu: dict[str, float],
    p0_planet_lists: dict,
    mode: str,
) -> tuple[int, int]:
    """Merge compatible single-P0 extractor planets on each character into split planets.

    p0_need_pu: required production units per P0 name (1 pu = one head @ 100% richness).
    Returns (split_planets, planets_saved). Mutates `assignments` in place: a merged pair
    becomes one entry with `split=True` and a `legs` list. No-op for mode 'off'."""
    if mode == "off":
        return 0, 0

    # (system, planet_num) -> richness value, per P0, to read a host planet's quality for the
    # *other* leg's resource.
    rich_idx: dict[str, dict[tuple, float]] = {}
    for p0, planets in p0_planet_lists.items():
        rich_idx[p0] = {(p["system"], p["planet_num"]): p["value"] for p in planets}

    def _host_quality(p0_name: str, system: str, planet_num) -> float | None:
        v = rich_idx.get(p0_name, {}).get((system, planet_num))
        return round(v) if v is not None else None

    # Current production per P0 in planet-units (one extractor planet = 1.0, quality-agnostic).
    pu_out: dict[str, float] = {}
    for a in assignments:
        for e in a["extractors"]:
            if e.get("split") or not e.get("p0_name") or e.get("quality_pct") is None:
                continue
            pu_out[e["p0_name"]] = pu_out.get(e["p0_name"], 0.0) + 1.0

    # Effective floor per P0: never drop below its fair-share baseline, and never reduce a P0
    # that the plan already places BELOW its share (a scarce type, capped by planet count) —
    # for those the floor is what's already there, so a split can't shed it.
    eff_need = {p0: min(p0_need_pu.get(p0, 0.0), cur) for p0, cur in pu_out.items()}

    def _need(p0):  # required planet-units (floor)
        return eff_need.get(p0, 0.0)

    splits = 0
    for a in assignments:
        progress = True
        while progress:
            progress = False
            plain = [
                e for e in a["extractors"]
                if not e.get("split") and e.get("p0_name") and e.get("quality_pct") is not None
                and e.get("system") and e.get("planet_num") is not None
            ]
            merged = None
            for i in range(len(plain)):
                for j in range(i + 1, len(plain)):
                    A, B = plain[i], plain[j]
                    if A["p0_name"] == B["p0_name"]:
                        continue
                    host = _pick_split_host(A, B, _host_quality)
                    if not host:
                        continue
                    leg = _solve_split_heads(A, B, host, pu_out, _need)
                    if not leg:
                        continue
                    merged = (A, B, host, leg)
                    break
                if merged:
                    break
            if not merged:
                continue

            A, B, host, (headsA, headsB, qA_host, qB_host) = merged
            # Roll production back for the two dedicated planets (−1.0 each), forward for the
            # split legs (heads/10 of a planet each).
            pu_out[A["p0_name"]] = pu_out.get(A["p0_name"], 0.0) - 1.0 + headsA / 10.0
            pu_out[B["p0_name"]] = pu_out.get(B["p0_name"], 0.0) - 1.0 + headsB / 10.0

            split_entry = {
                "split":       True,
                "system":      host["system"],
                "planet_num":  host["planet_num"],
                "planet_type": host["planet_type"],
                "is_existing": bool(host.get("is_existing")),
                "legs": [
                    {
                        "p0_type_id": A["p0_type_id"], "p0_name": A["p0_name"],
                        "p1_type_id": A["p1_type_id"], "p1_name": A["p1_name"],
                        "best_planet_type": A.get("best_planet_type"),
                        "heads": headsA, "quality_pct": qA_host,
                    },
                    {
                        "p0_type_id": B["p0_type_id"], "p0_name": B["p0_name"],
                        "p1_type_id": B["p1_type_id"], "p1_name": B["p1_name"],
                        "best_planet_type": B.get("best_planet_type"),
                        "heads": headsB, "quality_pct": qB_host,
                    },
                ],
            }
            # Replace A in place; drop B. (Order within the list is cosmetic.)
            idxA = a["extractors"].index(A)
            a["extractors"][idxA] = split_entry
            a["extractors"].remove(B)
            splits += 1
            progress = True

    # Merging frees planet slots → refresh each character's free-slot count so the display (and
    # any reinvestment pass) sees the reclaimed capacity.
    if splits:
        for a in assignments:
            a["free_planets"] = max(
                0, a.get("effective_planets", 0) - len(a["extractors"]) - a.get("factory_planets", 0))
    return splits, splits


def _reinvest_freed_planets(assignments, p1_info, p0_planet_lists, fac_db_planets,
                            best_fac_system, ext_slots, factories) -> tuple[int, int]:
    """Aggressive reinvestment: fill the planet slots freed by split-consolidation with extra
    factory + extractor planets (in the plan's equilibrium ext:fac ratio) so reclaimed
    overproduction capacity produces MORE rather than just leaving fewer planets in use.
    Concrete planets are placed so the per-character view stays consistent. Single-product
    only — factory assignments carry no per-line product (the basket/fuel-block planner has
    its own _reinvest_fuelblock_greedy, which needs multi-line + per-CCU rate awareness this
    doesn't). Returns (added_factories, added_extractors); the caller rescales throughput
    from the new totals."""
    total_free = sum(max(0, a.get("free_planets", 0)) for a in assignments)
    P = ext_slots + factories
    if total_free <= 0 or P <= 0:
        return 0, 0
    want_fac = round(total_free * factories / P)  # split freed slots by the ext:fac equilibrium

    total_rel = sum(i["relative_qty"] for i in p1_info) or 1
    info_by_p0 = {i["p0_name"]: i for i in p1_info}
    supply = {i["p0_name"]: 0.0 for i in p1_info}
    target = {i["p0_name"]: P * i["relative_qty"] / total_rel for i in p1_info}
    for a in assignments:
        for e in a["extractors"]:
            if e.get("split"):
                for leg in e["legs"]:
                    if leg["p0_name"] in supply:
                        supply[leg["p0_name"]] += leg["heads"] / 10.0
            elif e.get("p0_name") in supply:
                supply[e["p0_name"]] += 1.0

    fac_pool = [p for p in fac_db_planets if (not best_fac_system or p["system"] == best_fac_system)]
    added_fac = added_ext = 0
    for a in assignments:
        free = max(0, a.get("free_planets", 0))
        if free <= 0:
            continue
        used = {(e.get("system"), e.get("planet_num")) for e in a["extractors"]}
        used |= {(f.get("system"), f.get("planet_num")) for f in a.get("factory_assignments", [])}
        while free > 0:
            placed = False
            if added_fac < want_fac:  # owe a factory slot, and a B/T planet is free for this char
                cand = next((p for p in fac_pool if (p["system"], p["planet_num"]) not in used), None)
                if cand:
                    fa = {
                        "system": cand["system"], "planet_num": cand["planet_num"],
                        "planet_type": cand["planet_type"], "is_new": True, "reinvest": True,
                    }
                    a.setdefault("factory_assignments", []).append(fa)
                    a["factory_planets"] = a.get("factory_planets", 0) + 1
                    used.add((cand["system"], cand["planet_num"]))
                    added_fac += 1
                    free -= 1
                    placed = True
            if not placed:  # extractor of the most under-supplied P0 with a reachable free planet
                for p0 in sorted(supply, key=lambda n: supply[n] - target[n]):
                    inf = info_by_p0[p0]
                    cand = next((p for p in p0_planet_lists.get(p0, [])
                                 if (p["system"], p["planet_num"]) not in used), None)
                    if cand:
                        a["extractors"].append({
                            "p0_type_id": inf["p0_type_id"], "p0_name": p0,
                            "p1_type_id": inf["p1_type_id"], "p1_name": inf["p1_name"],
                            "planet_types": inf["planet_types"], "best_planet_type": inf["best_planet_type"],
                            "relative_qty": inf["relative_qty"], "is_existing": False, "is_replace": False,
                            "reinvest": True, "system": cand["system"], "planet_num": cand["planet_num"],
                            "planet_type": cand["planet_type"], "quality_pct": round(cand["value"]),
                        })
                        used.add((cand["system"], cand["planet_num"]))
                        supply[p0] += 1.0
                        added_ext += 1
                        free -= 1
                        placed = True
                        break
            if not placed:
                break  # nothing else fits on this character's remaining free slots
        a["free_planets"] = free
    return added_fac, added_ext


def _pick_split_host(A: dict, B: dict, host_quality) -> dict | None:
    """Choose which of the two planets can host both resources. Prefer A's planet if its
    type also yields B (and we know B's richness there), else B's planet for A. Returns a
    host dict {system, planet_num, planet_type, qA, qB, is_existing} or None."""
    ptA, ptB = _slot_planet_type(A), _slot_planet_type(B)
    # A's planet hosting B?
    if ptA and ptA in _P0_PLANET_TYPES.get(B["p0_name"], []):
        qB = host_quality(B["p0_name"], A["system"], A["planet_num"])
        if qB is not None:
            return {"system": A["system"], "planet_num": A["planet_num"], "planet_type": ptA,
                    "qA": A["quality_pct"], "qB": qB, "is_existing": A.get("is_existing")}
    # B's planet hosting A?
    if ptB and ptB in _P0_PLANET_TYPES.get(A["p0_name"], []):
        qA = host_quality(A["p0_name"], B["system"], B["planet_num"])
        if qA is not None:
            return {"system": B["system"], "planet_num": B["planet_num"], "planet_type": ptB,
                    "qA": qA, "qB": B["quality_pct"], "is_existing": B.get("is_existing")}
    return None


def _solve_split_heads(A, B, host, pu_out, need) -> tuple | None:
    """Allocate the 10-head budget across the two legs, OUTPUT-PRESERVING:
    returns (headsA, headsB, qA, qB) only if the legs can cover each P0's floor within 10
    heads, else None. (Reinvesting the freed planet is a separate step, not
    underproducing here.) qA/qB are the host planet's richness per leg."""
    qA, qB = host["qA"], host["qB"]
    if qA <= 0 or qB <= 0:
        return None
    # Planet-units left for each P0 once its dedicated planet (1.0) is removed.
    outA_without = pu_out.get(A["p0_name"], 0.0) - 1.0
    outB_without = pu_out.get(B["p0_name"], 0.0) - 1.0
    defA = max(0.0, need(A["p0_name"]) - outA_without)  # planet-units the split leg must supply
    defB = max(0.0, need(B["p0_name"]) - outB_without)
    # Heads to cover each deficit (10 heads = 1 planet; ≥1 so it's a genuine two-resource planet).
    headsA = max(1, ceil(defA * 10.0))
    headsB = max(1, ceil(defB * 10.0))
    if headsA + headsB > 10:
        return None
    # Spread leftover heads as buffer, proportional to relative demand.
    spare = 10 - headsA - headsB
    if spare > 0:
        relA = A.get("relative_qty", 1) or 1
        relB = B.get("relative_qty", 1) or 1
        addA = round(spare * relA / (relA + relB))
        headsA += addA
        headsB += spare - addA
    return headsA, headsB, qA, qB


def _assign_factory_planets_to_chars(
    assignments: list[dict],
    char_list: list[dict],
    factory_shares: dict[int, int],
    auto_mode: bool,
    fac_db_planets: list[dict],
    best_fac_system: str | None,
    char_nonfac: dict[int, list],
    req,
    has_system_name: bool,
) -> None:
    """
    Assign specific factory planets to each character.
    Overflow from one character (e.g. their extractors block factory planets) propagates
    to any factory-eligible character with spare capacity. Mutates assignments.
    """
    # Cap factory_planets to each char's precomputed share
    for asgn, char in zip(assignments, char_list):
        cid = char["character_id"]
        if auto_mode:
            asgn["factory_planets"] = min(asgn["factory_planets"], factory_shares.get(cid, 0))
        elif cid in factory_shares:
            asgn["factory_planets"] = min(asgn["factory_planets"], factory_shares[cid])

    for asgn, char in zip(assignments, char_list):
        total_non_ext = char["effective_planets"] - len(asgn["extractors"])
        asgn["free_planets"] = max(0, total_non_ext - asgn["factory_planets"])

    factory_eligible_ids = {
        c["character_id"] for c in char_list
        if c["extractor_limit"] == 0 or c["character_id"] in factory_shares or auto_mode
    }

    def pick(asgn: dict, char: dict, count: int) -> tuple[list[dict], int]:
        """Pick up to count factory planets. Returns (placed, unplaced_count)."""
        cid = char["character_id"]
        char_fac_used: set[tuple] = {
            (e["system"], e["planet_num"])
            for e in asgn["extractors"]
            if e.get("system") and e.get("planet_num") is not None
        }
        # Include already-assigned factory planets so the overflow pass doesn't
        # re-assign the same planet that was placed in the first pass.
        char_fac_used.update(
            (f["system"], f["planet_num"])
            for f in asgn.get("factory_assignments", [])
            if f.get("system") and f.get("planet_num") is not None
        )
        nonfac = [] if not req.use_existing else [
            p for p in char_nonfac.get(cid, [])
            if (p.get("system_name"), p.get("planet_num")) not in char_fac_used
        ]
        if char["extractor_limit"] is None and not auto_mode:
            count = min(count, len(nonfac))

        fac_assigns = []
        for _ in range(count):
            existing = None
            if has_system_name and nonfac and best_fac_system:
                for idx, cp in enumerate(nonfac):
                    if cp.get("system_name") == best_fac_system:
                        existing = nonfac.pop(idx)
                        break
            if existing:
                key = (existing.get("system_name"), existing.get("planet_num"))
                if key[0] and key[1] is not None:
                    char_fac_used.add(key)
                fac_assigns.append({
                    "system":      existing.get("system_name") or best_fac_system or None,
                    "planet_num":  existing.get("planet_num"),
                    "planet_type": existing.get("planet_type", "?"),
                    "is_existing": True, "is_replace": False, "is_new": False,
                })
            else:
                planet = next(
                    (p for p in fac_db_planets
                     if (best_fac_system is None or p["system"] == best_fac_system)
                     and (p["system"], p["planet_num"]) not in char_fac_used),
                    None,
                )
                if planet:
                    char_fac_used.add((planet["system"], planet["planet_num"]))
                    fac_assigns.append({
                        "system": planet["system"], "planet_num": planet["planet_num"],
                        "planet_type": planet["planet_type"],
                        "is_existing": False, "is_replace": False, "is_new": True,
                    })
                else:
                    fac_assigns.append({
                        "planet_type": "Barren", "is_existing": False, "is_replace": False,
                        "is_new": True, "unplaced": True,
                    })

        placed = [f for f in fac_assigns if not f.get("unplaced")]
        return placed, len(fac_assigns) - len(placed)

    overflow = 0

    # First pass: assign base shares
    for asgn, char in zip(assignments, char_list):
        fac_count = asgn["factory_planets"]
        if fac_count <= 0:
            asgn["factory_assignments"] = []
            continue
        placed, unplaced = pick(asgn, char, fac_count)
        if unplaced:
            overflow += unplaced
            asgn["factory_planets"] -= unplaced
            asgn["free_planets"] += unplaced
        asgn["factory_assignments"] = placed

    # Second pass: absorb overflow into any factory-eligible char with spare capacity
    if overflow > 0:
        for asgn, char in zip(assignments, char_list):
            if overflow <= 0:
                break
            if char["character_id"] not in factory_eligible_ids:
                continue
            spare = char["effective_planets"] - len(asgn["extractors"]) - asgn["factory_planets"]
            take = min(overflow, max(0, spare))
            if take <= 0:
                continue
            placed, still_unplaced = pick(asgn, char, take)
            if placed:
                asgn["factory_assignments"].extend(placed)
                asgn["factory_planets"] += len(placed)
                asgn["free_planets"] = max(0, asgn["free_planets"] - len(placed))
                overflow -= len(placed)
            overflow += still_unplaced


# ── Shared plan helpers (single-product + fuel-block basket) ───────────────────

def _load_char_planet_config(con, context_id: int, config_type_id: int):
    """Load characters, their planets, and per-product config for a plan run.
    config_type_id selects the pp_plan_config rows (the real product id, or the
    fuel-block sentinel). Returns (char_rows, planet_rows, has_system_name, config_map)."""
    # NOTE: character_name here is a tie-break for the PLANNING algorithm's processing order
    # (which character gets a marginal/scarce slot in a tight scenario), not a display list —
    # deliberately left as SQLite's default (BINARY) ordering rather than the natural-sort used
    # for actual character-list displays elsewhere. Changing it reshuffles who gets the leftover
    # slot in capacity-constrained fixtures (confirmed via test_distribution.py's DE-IHK case)
    # without being "more correct" either way — so leave the tie-break alone and only fix display
    # ordering (GET /api/characters, GET /api/plan-config/{id}, and the frontend result sort).
    char_rows = con.execute(f"""
        SELECT character_id, character_name,
               1 + interplanetary_consolidation AS max_planets,
               command_center_upgrades AS ccu
        FROM pp_characters WHERE context_id=?
              {PI_CHAR_SQL}
        ORDER BY (1 + interplanetary_consolidation) DESC, character_name
    """, (context_id,)).fetchall()

    try:
        planet_rows = con.execute("""
            SELECT cp.character_id, cp.planet_type, cp.is_extractor, cp.p0_type_id,
                   cp.products, COALESCE(ss.name, '') as system_name, cp.planet_num
            FROM pp_char_planets cp
            JOIN pp_characters pc ON pc.character_id = cp.character_id
            LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
            WHERE pc.context_id=?
        """, (context_id,)).fetchall()
        has_system_name = True
    except Exception:
        planet_rows = con.execute(
            "SELECT cp.character_id, cp.planet_type, cp.is_extractor, cp.p0_type_id, cp.products "
            "FROM pp_char_planets cp JOIN pp_characters pc ON pc.character_id=cp.character_id "
            "WHERE pc.context_id=?", (context_id,),
        ).fetchall()
        has_system_name = False

    config_rows = con.execute(
        "SELECT character_id, planet_limit, extractor_limit, ccu "
        "FROM pp_plan_config WHERE product_type_id=?",
        (config_type_id,),
    ).fetchall()
    config_map = {r["character_id"]: dict(r) for r in config_rows}
    return char_rows, planet_rows, has_system_name, config_map


def _build_p1_info_raw(p1_demand: dict, p1_to_p0: dict, types: dict):
    """Sort the P1 demand vector heaviest-first and map each P1 to its source P0.
    Returns (sorted_p1, p1_info_raw, all_p0_names)."""
    sorted_p1 = sorted(p1_demand.items(), key=lambda x: -x[1])
    p1_info_raw = []
    for p1_id, qty in sorted_p1:
        p0_id = p1_to_p0.get(p1_id)
        p0_name = types.get(p0_id, {}).get("name") if p0_id else None
        p1_info_raw.append((p1_id, qty, p0_id, p0_name))
    all_p0_names = [p0_name for _, _, _, p0_name in p1_info_raw if p0_name]
    return sorted_p1, p1_info_raw, all_p0_names


def _fetch_planets_and_recs(con, all_p0_names, req, types, p1_info_raw):
    """Fetch the scoped + global P0 planet lists, the best planet type per P0, and the
    system recommendations (annotated with p1_name). Shared by both plan paths."""
    _min_density = getattr(req, "min_density_pct", 0) or 0
    p0_planet_lists = _fetch_p0_planets(
        all_p0_names, con, constellations=req.constellations,
        systems=req.chosen_systems if req.chosen_systems else None,
        min_density=_min_density,
    )
    # Global list (for existing colonies you already run) ignores the cap — you keep those.
    p0_planet_lists_global = _fetch_p0_planets(all_p0_names, con, [], None)
    best_ptypes = {
        name: (planets[0]["planet_type"] if planets else None)
        for name, planets in p0_planet_lists.items()
    }

    # Per-P0 demand weight (relative_qty) so recommendations score depth by how much of each
    # resource the recipe actually needs.
    p0_needs: dict[str, float] = {}
    for _p1_id, qty, _p0_id, p0_name in p1_info_raw:
        if p0_name:
            p0_needs[p0_name] = p0_needs.get(p0_name, 0.0) + float(qty)

    if req.chosen_systems:
        sys_recs = _system_recommendations(
            all_p0_names, con, constellations=None,
            systems=req.chosen_systems, preferred_systems=len(req.chosen_systems),
            min_density=_min_density, p0_needs=p0_needs,
        )
    else:
        sys_recs = _system_recommendations(
            all_p0_names, con, constellations=req.constellations or None,
            preferred_systems=req.preferred_systems, max_jumps=req.max_jumps,
            min_density=_min_density, p0_needs=p0_needs,
        )

    p0_to_p1_name = {p0_name: types.get(p1_id, {}).get("name", "?")
                     for p1_id, _, _, p0_name in p1_info_raw if p0_name}
    for rec in sys_recs:
        for asgn in rec["assignments"]:
            asgn["p1_name"] = p0_to_p1_name.get(asgn["p0_name"], "")
    return p0_planet_lists, p0_planet_lists_global, best_ptypes, sys_recs


# Planet types a factory can never go on (too large — links overflow the grid). Gas is Ø40000.
_FACTORY_EXCLUDE_TYPES = ("Gas",)
# Smallest-diameter-first ordering for factory placement (Lava/Ice Ø6000 < the Ø8000 group < Storm Ø30000).
_FACTORY_SIZE_RANK_SQL = "WHEN 'Lava' THEN 0 WHEN 'Ice' THEN 0 WHEN 'Storm' THEN 8 WHEN 'Gas' THEN 9 ELSE 1"


def _factory_candidates(con, req, only_bt: bool = False, allowed_types: list[str] | None = None):
    """Factory-planet DB candidates + per-system options for the UI picker.
    `allowed_types` restricts the pool to those planet types (smallest planets first in the
    placement order); pass e.g. ['Barren','Temperate'] for the fuel-block default. When
    None, any planet type qualifies. `only_bt=True` is shorthand for the B/T restriction
    (single-product factories). Returns (fac_pool, factory_system_options, sys_fac_capacity)."""
    if only_bt and allowed_types is None:
        allowed_types = ["Barren", "Temperate"]
    # Factory layouts are link-heavy, so a planet's diameter decides whether one fits. Gas (Ø40000) is
    # so large the links overflow the grid — it can NEVER host a factory, so drop it whatever was asked
    # for. The rest sort smallest-first (least CPU/PG, most efficient), with the giant Storm (Ø30000)
    # last so it's only used when nothing smaller is left. Extractors are pinned to the planets carrying
    # their P0 and get first pick elsewhere; factories take what's left, preferring the compact planets.
    if allowed_types is not None:
        allowed_types = [t for t in allowed_types if t not in _FACTORY_EXCLUDE_TYPES]
        if allowed_types:
            type_clause = "planet_type IN ({})".format(",".join("?" * len(allowed_types)))
            type_params = list(allowed_types)
        else:
            type_clause, type_params = "1=0", []         # nothing eligible after dropping the giants
    else:
        type_clause = "planet_type NOT IN ({})".format(",".join("?" * len(_FACTORY_EXCLUDE_TYPES)))
        type_params = list(_FACTORY_EXCLUDE_TYPES)

    fac_filter, fac_params = "", []
    fac_systems = list({*req.chosen_systems, req.factory_system}) if req.factory_system else list(req.chosen_systems)
    if fac_systems:
        fac_filter = " AND system IN ({})".format(",".join("?" * len(fac_systems)))
        fac_params = fac_systems
    elif req.constellations:
        fac_filter = " AND constellation IN ({})".format(",".join("?" * len(req.constellations)))
        fac_params = list(req.constellations)

    where = f"WHERE {type_clause}{fac_filter}"
    # Prefer the genuinely smallest planets (real diameter when known — least link footprint, most likely
    # to fit a factory); unknown-diameter planets sort last, then fall back to the per-type size rank.
    order = (f"ORDER BY (diameter IS NULL), diameter, "
             f"CASE planet_type {_FACTORY_SIZE_RANK_SQL} END, system, planet_num")
    fac_params = type_params + fac_params
    try:
        fac_pool = [dict(r) for r in con.execute(
            f"SELECT system, planet_num, planet_type, diameter FROM pp_planets {where} {order}",
            fac_params,
        ).fetchall()]
    except Exception:
        # diameter column may not exist yet (pre-populate) — fall back without it.
        try:
            fac_pool = [dict(r) for r in con.execute(
                f"SELECT system, planet_num, planet_type FROM pp_planets {where} "
                f"ORDER BY CASE planet_type {_FACTORY_SIZE_RANK_SQL} END, system, planet_num",
                fac_params,
            ).fetchall()]
        except Exception:
            fac_pool = []

    try:
        chosen_consts = []
        if req.chosen_systems:
            cs_ph = ",".join("?" * len(req.chosen_systems))
            chosen_consts = [r["constellation"] for r in con.execute(
                f"SELECT DISTINCT constellation FROM pp_planets WHERE system IN ({cs_ph})",
                req.chosen_systems,
            ).fetchall() if r["constellation"]]
        elif req.constellations:
            chosen_consts = list(req.constellations)
        if chosen_consts:
            cc_ph = ",".join("?" * len(chosen_consts))
            opt_type = f"{type_clause} AND " if type_clause else ""
            fac_opts_rows = con.execute(
                f"SELECT system, constellation, COUNT(*) as cnt FROM pp_planets "
                f"WHERE {opt_type}constellation IN ({cc_ph}) "
                f"GROUP BY system ORDER BY cnt DESC",
                type_params + chosen_consts,
            ).fetchall()
            factory_system_options = [
                {"system": r["system"], "constellation": r["constellation"], "count": r["cnt"]}
                for r in fac_opts_rows
            ]
        else:
            factory_system_options = []
        sys_fac_capacity = {o["system"]: o["count"] for o in factory_system_options}
    except Exception:
        factory_system_options = []
        sys_fac_capacity = {}
    return fac_pool, factory_system_options, sys_fac_capacity


def _build_p1_info(p1_info_raw, best_ptypes, types):
    """Expand p1_info_raw tuples into the rich p1_info dicts used downstream."""
    p1_info = []
    for p1_id, qty, p0_id, p0_name in p1_info_raw:
        p1_info.append({
            "p1_type_id":       p1_id,
            "p1_name":          types.get(p1_id, {}).get("name", "?"),
            "p0_type_id":       p0_id,
            "p0_name":          p0_name,
            "relative_qty":     qty,
            "planet_types":     _P0_PLANET_TYPES.get(p0_name, []) if p0_name else [],
            "best_planet_type": best_ptypes.get(p0_name),
        })
    return p1_info


def _build_char_list(char_rows, config_map, char_planets, with_ccu: bool):
    """Build the working character list (excluding planet_limit=0 chars).

    effective_planets is capped at EVE's hard limit of 6, NOT the trained max, so the
    planet field can model a higher Interplanetary Consolidation skill than the
    character currently has (what-if planning); default (no override) = trained max.
    with_ccu adds effective_ccu (per-character command-centre level, clamped 1–5) used
    by the fuel-block factory-throughput model: per-char override → ESI skill (only
    when ≥1, since command_center_upgrades is often 0/unfetched) → assume 5."""
    char_list = []
    for r in char_rows:
        cid = r["character_id"]
        cfg = config_map.get(cid, {})
        planet_limit = cfg.get("planet_limit")
        if planet_limit is not None and planet_limit == 0:
            continue
        extractor_limit = cfg.get("extractor_limit")
        effective = min(planet_limit, 6) if planet_limit is not None else r["max_planets"]
        entry = {
            **dict(r),
            "effective_planets": effective,
            "extractor_limit":   extractor_limit,
            "planets":           char_planets.get(cid, []),
        }
        if with_ccu:
            eff_ccu = cfg.get("ccu")
            if eff_ccu is None:
                eff_ccu = r["ccu"] if (r["ccu"] and r["ccu"] >= 1) else 5
            entry["effective_ccu"] = max(1, min(5, int(eff_ccu)))
        char_list.append(entry)
    return char_list


def _set_computed_ext_cap(char_list, factory_shares, auto_mode):
    """Set computed_ext_cap per character (extractor slots after factories carved out)
    and return the clamp total (sum of caps) used to bound ext_slots."""
    for c in char_list:
        ext_lim = c["extractor_limit"]
        cid = c["character_id"]
        if ext_lim is None and not auto_mode:
            c["computed_ext_cap"] = c["effective_planets"]
        elif ext_lim == 0:
            c["computed_ext_cap"] = 0
        else:
            c["computed_ext_cap"] = max(0, c["effective_planets"] - factory_shares.get(cid, 0))
    return sum(c["computed_ext_cap"] for c in char_list)


def _planet_product_type_ids(raw) -> set:
    """Parse pp_char_planets.products (JSON list of {type_id, name}, highest-tier output
    only — see esi.py _fetch_planets) into a set of type_ids. Empty/unparseable -> no
    known committed product (a genuinely idle non-extractor slot)."""
    if not raw:
        return set()
    try:
        return {int(p["type_id"]) for p in _json.loads(raw) if p.get("type_id") is not None}
    except Exception:
        return set()


def _run_extractor_pipeline(
    req, char_list, p1_info, ext_slots, needed_at_baseline,
    p0_planet_lists, p0_planet_lists_global, has_planet_db, has_system_name,
    auto_mode, assignment_extra=None, factory_avoid_cids=None, factory_avoid=None,
    density_est=None, reusable_type_ids=None,
):
    """Shared extractor-assignment core. Builds the per-character candidate views, the
    P0 slot caps and the Bresenham need list, then runs the two-pass extractor
    assignment and attaches concrete planet details.

    char_nonfac_ext: a char's idle factory planets are only "reserved" (kept off the
    extractor candidate list) when the char actually has factory slots carved out this
    plan (effective_planets > computed_ext_cap). Pure-extractor chars repurpose idle
    existing factory planets — essential for scarce P0s. factory_avoid(_cids) keeps the
    factory system's B/T planets free for maxed factory chars.

    reusable_type_ids: the set of type_ids this plan's factory planets will actually
    produce (e.g. {req.type_id} for a single-product plan, or a basket's component
    type_ids). A non-extractor planet already committed to a DIFFERENT real product
    (per its last ESI scan) is still that player's colony — it must keep blocking new
    extractor placement there (handled below via the unfiltered char_nonfac/
    char_nonfac_ext) — but it must NOT be silently claimed as "spare factory capacity"
    for this unrelated plan. So the RETURNED char_nonfac (what
    _assign_factory_planets_to_chars / _assign_fuelblock_factories treat as reusable)
    is filtered to planets with no known product, or one matching reusable_type_ids;
    the internal char_nonfac used for occupancy/counting stays unfiltered. None
    (caller didn't scope it) preserves the old product-agnostic behaviour for both.
    Returns (assignments, remaining, char_nonfac_reusable)."""
    def _reusable(p):
        tids = _planet_product_type_ids(p.get("products"))
        return not tids or reusable_type_ids is None or bool(tids & reusable_type_ids)

    char_nonfac: dict[int, list] = {
        c["character_id"]: [p for p in c["planets"] if not p.get("is_extractor")]
        for c in char_list
    }
    char_nonfac_reusable: dict[int, list] = {
        cid: [p for p in planets if _reusable(p)]
        for cid, planets in char_nonfac.items()
    }
    _reserve_cids = {
        c["character_id"] for c in char_list
        if c["effective_planets"] > c["computed_ext_cap"]
    }
    char_nonfac_ext: dict[int, list] = {
        cid: (pl if cid in _reserve_cids else [])
        for cid, pl in char_nonfac.items()
    }

    def _blocked_keys(cid):
        ks = {
            (p.get("system_name"), p.get("planet_num"))
            for p in char_nonfac_ext.get(cid, [])
            if p.get("system_name") and p.get("planet_num") is not None
        }
        if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
            ks = ks | factory_avoid
        return ks

    # P0 slot caps: each extractor-capable char can host at most min(extractor cap,
    # distinct unblocked planets it can reach); the same planet can be colonised by
    # multiple characters independently.
    if has_planet_db:
        def _p0_slot_cap(info):
            p0_name = info["p0_name"]
            planets = {(p["system"], p["planet_num"]) for p in p0_planet_lists.get(p0_name, [])}
            if not planets:
                return ext_slots
            cap = 0
            for c in char_list:
                if c["computed_ext_cap"] <= 0:
                    continue
                reachable = planets - _blocked_keys(c["character_id"])
                cap += min(c["computed_ext_cap"], len(reachable))
            return cap
        p0_caps = {info["p0_name"]: _p0_slot_cap(info) for info in p1_info}
    else:
        p0_caps = {info["p0_name"]: ext_slots for info in p1_info}

    _p0_planet_n = {
        info["p0_name"]: max(1, len(p0_planet_lists.get(info["p0_name"], [])))
        for info in p1_info
    } if has_planet_db else {info["p0_name"]: 1 for info in p1_info}
    scarcity_bonus = {name: 0.01 / n for name, n in _p0_planet_n.items()}

    need_list = _build_need_list(p1_info, ext_slots, needed_at_baseline, p0_caps, scarcity_bonus, density_est)

    char_spare_planets: dict[int, list] = {c["character_id"]: list(c["planets"]) for c in char_list}
    assignments = [
        {
            "character_id":      c["character_id"],
            "character_name":    c["character_name"],
            "max_planets":       c["max_planets"],
            "effective_planets": c["effective_planets"],
            "ccu":               c["ccu"],
            "effective_ccu":     c.get("effective_ccu"),  # for the on-planet basics refining cap
            "factory_only":      c["extractor_limit"] == 0,
            "extractor_limit":   c["extractor_limit"],
            "extractors":        [],
            **(assignment_extra(c) if assignment_extra else {}),
        }
        for c in char_list
    ]

    remaining = _assign_extractors(
        assignments, char_list, need_list, char_spare_planets, char_nonfac_ext,
        req, p0_planet_lists, has_planet_db, has_system_name, p1_info,
        factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
    )
    # When a min-density cap makes a thin P0 unplaceable, absorb the dangling slot onto a
    # free reachable planet of another resource instead of leaving the planet slot wasted.
    if has_planet_db and remaining and getattr(req, "min_density_pct", 0):
        remaining = _absorb_remaining(
            assignments, char_list, remaining, p0_planet_lists, char_nonfac_ext,
            p1_info, density_est, has_system_name,
            factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
        )
    _attach_extractor_planet_details(
        assignments, char_list, char_nonfac, char_nonfac_ext,
        p0_planet_lists, p0_planet_lists_global, req, auto_mode,
        factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
    )
    return assignments, remaining, char_nonfac_reusable


def _pick_factory_system(req, sys_fac_count: dict[str, int]):
    """Pick the factory system: explicit override, else the chosen/known system with
    the most candidate planets."""
    if req.factory_system:
        return req.factory_system
    if req.chosen_systems and sys_fac_count:
        in_chosen = {s: sys_fac_count[s] for s in req.chosen_systems if s in sys_fac_count}
        return max(in_chosen, key=lambda s: in_chosen[s]) if in_chosen else req.chosen_systems[0]
    if req.chosen_systems:
        return req.chosen_systems[0]
    if sys_fac_count:
        return max(sys_fac_count, key=lambda s: sys_fac_count[s])
    return None


