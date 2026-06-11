"""
Planetary industry planning — extractor assignment across characters.
"""
from fractions import Fraction
from math import gcd, ceil
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Request
from pydantic import BaseModel

from app.sde import load_pi_data, get_connection
from app.planetary import PLANET_P0_MAP, _NAME_TO_COL
from app.market import fetch_prices
from app.esi import require_context, session_context_id

router = APIRouter()

_P0_PLANET_TYPES: dict[str, list[str]] = {}
for _pt, _p0s in PLANET_P0_MAP.items():
    for _p0 in _p0s:
        _P0_PLANET_TYPES.setdefault(_p0, []).append(_pt)


def _p0_col(name: str) -> str | None:
    return _NAME_TO_COL.get(name.strip().lower()) if name else None


# ── DB setup ──────────────────────────────────────────────────────────────────

def ensure_plan_tables():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_plan_config (
            character_id    INTEGER NOT NULL,
            product_type_id INTEGER NOT NULL,
            planet_limit    INTEGER,
            factory_only    INTEGER DEFAULT 0,
            extractor_limit INTEGER,
            PRIMARY KEY (character_id, product_type_id)
        )
    """)
    try:
        con.execute("ALTER TABLE pp_plan_config ADD COLUMN extractor_limit INTEGER")
    except Exception:
        pass
    try:
        # Per-character command-centre level override (NULL = use the ESI value).
        con.execute("ALTER TABLE pp_plan_config ADD COLUMN ccu INTEGER")
    except Exception:
        pass
    con.execute("""
        UPDATE pp_plan_config SET extractor_limit = 0
        WHERE factory_only = 1 AND extractor_limit IS NULL
    """)
    con.commit()
    con.close()


# ── Config endpoints ──────────────────────────────────────────────────────────

class CharConfigEntry(BaseModel):
    character_id: int
    planet_limit: Optional[int] = None
    extractor_limit: Optional[int] = None  # None=all extractor, 0=factory only, N=max extractors
    ccu: Optional[int] = None              # command-centre level override (None = use ESI value)


class SaveConfigRequest(BaseModel):
    configs: list[CharConfigEntry]


@router.get("/api/plan-config/{type_id}")
def get_plan_config(type_id: int, context_id: int = Depends(require_context)):
    ensure_plan_tables()
    con = get_connection()
    char_rows = con.execute("""
        SELECT character_id, character_name,
               1 + interplanetary_consolidation AS max_planets,
               command_center_upgrades AS esi_ccu
        FROM pp_characters WHERE context_id=? ORDER BY character_name
    """, (context_id,)).fetchall()
    saved = {
        r["character_id"]: dict(r)
        for r in con.execute(
            "SELECT character_id, planet_limit, extractor_limit, ccu FROM pp_plan_config WHERE product_type_id=?",
            (type_id,),
        ).fetchall()
    }
    con.close()
    return {"configs": [
        {
            "character_id":    c["character_id"],
            "character_name":  c["character_name"],
            "max_planets":     c["max_planets"],
            "esi_ccu":         c["esi_ccu"],
            "planet_limit":    saved.get(c["character_id"], {}).get("planet_limit"),
            "extractor_limit": saved.get(c["character_id"], {}).get("extractor_limit"),
            "ccu":             saved.get(c["character_id"], {}).get("ccu"),
        }
        for c in char_rows
    ]}


@router.post("/api/plan-config/{type_id}")
def save_plan_config(type_id: int, req: SaveConfigRequest):
    ensure_plan_tables()
    con = get_connection()
    for e in req.configs:
        if e.planet_limit is None and e.extractor_limit is None and e.ccu is None:
            con.execute(
                "DELETE FROM pp_plan_config WHERE character_id=? AND product_type_id=?",
                (e.character_id, type_id),
            )
        else:
            con.execute("""
                INSERT OR REPLACE INTO pp_plan_config
                    (character_id, product_type_id, planet_limit, extractor_limit, factory_only, ccu)
                VALUES (?,?,?,?,?,?)
            """, (
                e.character_id, type_id, e.planet_limit, e.extractor_limit,
                1 if e.extractor_limit == 0 else 0, e.ccu,
            ))
    con.commit()
    con.close()
    return {"ok": True}


# ── Planet DB helpers ─────────────────────────────────────────────────────────

def _fetch_p0_planets(
    p0_names: list[str], con,
    constellations: list[str],
    systems: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Return {p0_name: [planets sorted by value DESC]}."""
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
        try:
            rows = con.execute(
                f"SELECT system, planet_num, planet_type, {col} as value "
                f"FROM pp_planets WHERE {col}>0{where_extra} ORDER BY {col} DESC",
                params_base,
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
) -> list:
    needed_cols = {name: _p0_col(name) for name in p0_names}
    needed_cols = {k: v for k, v in needed_cols.items() if v}
    if not needed_cols:
        return []

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
            sys_data[sname] = {"constellation": row["constellation"] or "?", "p0": {}}
        for p0_name, col in needed_cols.items():
            v = row[col] or 0
            if v > 0:
                ex = sys_data[sname]["p0"].get(p0_name)
                if not ex or v > ex["value"]:
                    sys_data[sname]["p0"][p0_name] = {
                        "value": v, "planet_num": row["planet_num"], "planet_type": row["planet_type"],
                    }

    def merge(sys_names):
        merged = {}
        for sname in sys_names:
            for p0_name, info in sys_data[sname]["p0"].items():
                if p0_name not in merged or info["value"] > merged[p0_name]["value"]:
                    merged[p0_name] = {**info, "system": sname}
        return merged

    def make_result(sys_names, merged):
        covered = [n for n in p0_names if n in merged]
        first_const = sys_data[sys_names[0]]["constellation"]
        same_const = all(sys_data[s]["constellation"] == first_const for s in sys_names)
        return {
            "constellation": first_const, "same_constellation": same_const,
            "coverage": len(covered), "total_p0": len(p0_names),
            "score": sum(d["value"] for d in merged.values()),
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
        m = merge(sys_names)
        if not any(n in m for n in p0_names):
            return
        key = tuple(sorted(sys_names))
        if key in seen:
            return
        seen.add(key)
        r = make_result(list(sys_names), m)
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
    # closeness to the requested system count; then tightness; then richness.
    results.sort(key=lambda r: (
        -r["coverage"],
        0 if r["within_jumps"] else 1,
        abs(len(r["systems_needed"]) - pref),
        r["jumps"] if r["jumps"] is not None else 99,
        -r["score"],
    ))
    return results[:top_n]


# ── Planning core helpers ─────────────────────────────────────────────────────

def _max_matching(p0_names: list[str], planet_lists: dict[str, list]) -> int:
    """Maximum bipartite matching: how many of p0_names can be assigned a unique planet."""
    planet_to_idx: dict[tuple, int] = {}
    for name in p0_names:
        for p in planet_lists.get(name, []):
            k = (p["system"], p["planet_num"])
            if k not in planet_to_idx:
                planet_to_idx[k] = len(planet_to_idx)
    n_planets = len(planet_to_idx)
    adj: list[list[int]] = [
        [planet_to_idx[k]
         for p in planet_lists.get(name, [])
         if (k := (p["system"], p["planet_num"])) in planet_to_idx]
        for name in p0_names
    ]
    match_planet: list[int] = [-1] * n_planets

    def augment(slot: int, seen: set) -> bool:
        for p in adj[slot]:
            if p in seen:
                continue
            seen.add(p)
            if match_planet[p] == -1 or augment(match_planet[p], seen):
                match_planet[p] = slot
                return True
        return False

    return sum(1 for s in range(len(p0_names)) if augment(s, set()))


def _max_matching_slots(slot_planet_lists: list[list[dict]]) -> int:
    """
    Like _max_matching but each slot has its own independent planet candidate list.
    Used for per-character feasibility checks where some slots have committed planets.
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


def _build_p0_p1_maps(pi_data):
    p0_to_p1: dict[int, int] = {}
    for tid, sch in pi_data["schematics"].items():
        if pi_data["types"].get(tid, {}).get("pi_tier") == 1:
            for inp in sch["inputs"]:
                if pi_data["types"].get(inp["type_id"], {}).get("pi_tier") == 0:
                    p0_to_p1[inp["type_id"]] = tid
    return p0_to_p1, {v: k for k, v in p0_to_p1.items()}


def _compute_p1_reqs(target_type_id: int, pi_data) -> dict[int, int]:
    schematics, types = pi_data["schematics"], pi_data["types"]

    def trace(tid: int, mult: Fraction) -> dict[int, Fraction]:
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
            sub = trace(inp["type_id"], Fraction(inp["quantity"], out_q) * mult)
            for k, v in sub.items():
                result[k] = result.get(k, Fraction(0)) + v
        return result

    raw = trace(target_type_id, Fraction(1))
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

    def trace(tid: int, mult: Fraction) -> dict[int, Fraction]:
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
            sub = trace(inp["type_id"], Fraction(inp["quantity"], out_q) * mult)
            for k, v in sub.items():
                result[k] = result.get(k, Fraction(0)) + v
        return result

    return {k: float(v) for k, v in trace(target_type_id, Fraction(1)).items()}


# ── Plan request model ────────────────────────────────────────────────────────

class PlanRequest(BaseModel):
    type_id: int
    overproduction_pct: int = 10
    use_existing: bool = True
    constellations: list[str] = []
    preferred_systems: int = 1
    chosen_systems: list[str] = []
    factory_system: str = ""
    factory_output_per_hour: Optional[float] = None  # override SDE rate; use 0.5 for P4
    factory_character_ids: list[int] = []  # if set, only these chars do factories
    max_jumps: int = 1  # prefer system combos clustered within this many jumps (0–5)
    split_mode: str = "off"  # off | on — split-extraction (consolidate freed planets → factories)
    distribution_mode: str = "stability"  # stability (count ∝ need/density) | need (∝ need)


def _norm_dist_mode(v) -> str:
    # Default to density-aware "stability"; only "need" opts back to pure need-proportional.
    return "need" if v == "need" else "stability"


def _norm_split_mode(v) -> str:
    # Single on/off toggle now. Legacy "conservative"/"aggressive" both map to "on" (the
    # consolidate-then-reinvest behaviour); only "off"/blank stays off.
    return "off" if (not v or v == "off") else "on"


# ── Profiles / shares ─────────────────────────────────────────────────────────

import json as _json
import secrets as _secrets


def ensure_share_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_shares (
            id         TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()


class PlanShareSave(BaseModel):
    payload: dict
    anonymize: bool = True   # default to the safe option (no locatable data stored)


# Field names whose values are sensitive (would let another player locate the
# owner via in-game locator agents). Anonymized shares relabel these consistently.
_SHARE_SYS_STR = {"system", "system_name", "actual_system", "factory_system",
                  "best_fac_system", "fs"}
_SHARE_SYS_LIST = {"systems_needed", "chosen_systems", "systems", "cs"}
_SHARE_CONST_STR = {"constellation"}
_SHARE_CONST_LIST = {"cc", "constellations"}
_SHARE_CHAR_NAME = {"character_name"}
_SHARE_CHAR_ID = {"character_id"}
_SHARE_CHAR_ID_LIST = {"factory_character_ids", "fc"}
_MISS = object()


def _anonymize_share_payload(payload: dict) -> dict:
    """Strip everything that could pin the owner to a place or a character, while
    keeping the plan renderable (economics, counts, P0 types, structure). Systems,
    constellations and characters are relabeled consistently (System A, Pilot 1, …)
    so the shared view still reads coherently. Two-pass so we can also remap
    system-valued *dict keys* (e.g. `factory_capacity` is keyed by system name)."""
    data = _json.loads(_json.dumps(payload))
    sys_map: dict = {}
    const_map: dict = {}
    name_map: dict = {}
    id_map: dict = {}

    def label(m, key, prefix, alpha=True):
        if key in (None, ""):
            return key
        if key not in m:
            i = len(m)
            m[key] = f"{prefix} {chr(65 + i)}" if alpha and i < 26 else f"{prefix} {i + 1}"
        return m[key]

    def collect(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _SHARE_SYS_STR and isinstance(v, str):
                    label(sys_map, v, "System")
                elif k in _SHARE_SYS_LIST and isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            label(sys_map, x, "System")
                elif k in _SHARE_CONST_STR and isinstance(v, str):
                    label(const_map, v, "Constellation")
                elif k in _SHARE_CONST_LIST and isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            label(const_map, x, "Constellation")
                elif k in _SHARE_CHAR_NAME and isinstance(v, str):
                    label(name_map, v, "Pilot", alpha=False)
                elif k in _SHARE_CHAR_ID:
                    label(id_map, v, "char", alpha=False)
                elif k in _SHARE_CHAR_ID_LIST and isinstance(v, list):
                    for x in v:
                        label(id_map, x, "char", alpha=False)
                else:
                    collect(v)
        elif isinstance(node, list):
            for x in node:
                collect(x)

    def repl(k, v):
        if k in _SHARE_SYS_STR and isinstance(v, str):
            return sys_map.get(v, v)
        if k in _SHARE_SYS_LIST and isinstance(v, list):
            return [sys_map.get(x, x) if isinstance(x, str) else x for x in v]
        if k in _SHARE_CONST_STR and isinstance(v, str):
            return const_map.get(v, v)
        if k in _SHARE_CONST_LIST and isinstance(v, list):
            return [const_map.get(x, x) if isinstance(x, str) else x for x in v]
        if k in _SHARE_CHAR_NAME and isinstance(v, str):
            return name_map.get(v, v)
        if k in _SHARE_CHAR_ID:
            return id_map.get(v, v)
        if k in _SHARE_CHAR_ID_LIST and isinstance(v, list):
            return [id_map.get(x, x) for x in v]
        return _MISS

    def apply(node):
        if isinstance(node, dict):
            new = {}
            for k, v in node.items():
                r = repl(k, v)
                if r is not _MISS:
                    new[k] = r
                else:
                    apply(v)
                    new[sys_map.get(k, const_map.get(k, k))] = v   # remap system-keyed dicts
            node.clear()
            node.update(new)
        elif isinstance(node, list):
            for x in node:
                apply(x)

    collect(data)
    apply(data)
    data["anon"] = True
    return data


@router.post("/api/pp-shares")
def save_plan_share(req: PlanShareSave):
    ensure_share_table()
    share_id = _secrets.token_urlsafe(6)
    payload = _anonymize_share_payload(req.payload) if req.anonymize else req.payload
    con = get_connection()
    con.execute("INSERT INTO pp_shares (id, payload) VALUES (?, ?)",
                (share_id, _json.dumps(payload)))
    con.commit()
    con.close()
    return {"id": share_id}


@router.get("/api/pp-shares/{share_id}")
def load_plan_share(share_id: str):
    ensure_share_table()
    con = get_connection()
    row = con.execute("SELECT payload FROM pp_shares WHERE id=?", (share_id,)).fetchone()
    con.close()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Share not found")
    return {"payload": _json.loads(row["payload"])}


def ensure_profile_tables():
    con = get_connection()
    cols = [r[1] for r in con.execute("PRAGMA table_info(pp_profiles)").fetchall()]
    if "context_id" not in cols:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_profiles_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id        INTEGER NOT NULL DEFAULT 1,
                name              TEXT    NOT NULL,
                type_id           INTEGER NOT NULL,
                type_name         TEXT    NOT NULL DEFAULT '',
                factories         INTEGER NOT NULL DEFAULT 15,
                preferred_systems INTEGER NOT NULL DEFAULT 1,
                constellations    TEXT    NOT NULL DEFAULT '[]',
                use_existing      INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(context_id, name)
            )
        """)
        try:
            con.execute("""
                INSERT INTO pp_profiles_new
                    (id, context_id, name, type_id, type_name, factories,
                     preferred_systems, constellations, created_at)
                SELECT id, 1, name, type_id, type_name, factories,
                       preferred_systems, constellations, created_at
                FROM pp_profiles
            """)
            con.execute("DROP TABLE pp_profiles")
        except Exception:
            pass
        con.execute("ALTER TABLE pp_profiles_new RENAME TO pp_profiles")
    else:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_profiles (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id        INTEGER NOT NULL DEFAULT 1,
                name              TEXT    NOT NULL,
                type_id           INTEGER NOT NULL,
                type_name         TEXT    NOT NULL DEFAULT '',
                factories         INTEGER NOT NULL DEFAULT 15,
                preferred_systems INTEGER NOT NULL DEFAULT 1,
                constellations    TEXT    NOT NULL DEFAULT '[]',
                use_existing      INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(context_id, name)
            )
        """)
        for col, defval in [
            ("use_existing",           "INTEGER NOT NULL DEFAULT 1"),
            ("factory_system",         "TEXT NOT NULL DEFAULT ''"),
            ("overproduction_pct",     "INTEGER NOT NULL DEFAULT 20"),
            ("factory_output_per_hour","REAL"),
            ("factory_character_ids",  "TEXT NOT NULL DEFAULT '[]'"),
            ("max_jumps",              "INTEGER NOT NULL DEFAULT 1"),
            ("factory_planet_types",   "TEXT NOT NULL DEFAULT '[]'"),
            ("split_mode",             "TEXT NOT NULL DEFAULT 'off'"),
            ("distribution_mode",      "TEXT NOT NULL DEFAULT 'stability'"),
        ]:
            if col not in cols:
                try:
                    con.execute(f"ALTER TABLE pp_profiles ADD COLUMN {col} {defval}")
                except Exception:
                    pass
    con.commit()
    con.close()


class ProfileSave(BaseModel):
    name: str
    type_id: int
    type_name: str = ""
    overproduction_pct: int = 10
    preferred_systems: int = 1
    constellations: list[str] = []
    use_existing: bool = True
    factory_system: str = ""
    factory_output_per_hour: Optional[float] = None
    factory_character_ids: list[int] = []
    max_jumps: int = 1
    factory_planet_types: list[str] = []
    split_mode: str = "off"
    distribution_mode: str = "stability"


@router.get("/api/profiles")
def list_profiles(pp_session: str = Cookie(default=None)):
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"profiles": []}
    ensure_profile_tables()
    con = get_connection()
    rows = con.execute(
        "SELECT id, name, type_id, type_name, overproduction_pct, preferred_systems, "
        "constellations, use_existing, factory_system, factory_output_per_hour, factory_character_ids, max_jumps, "
        "factory_planet_types, split_mode, distribution_mode "
        "FROM pp_profiles WHERE context_id=? ORDER BY name",
        (context_id,),
    ).fetchall()
    con.close()
    return {"profiles": [
        {**dict(r),
         "constellations":          _json.loads(r["constellations"] or "[]"),
         "use_existing":            bool(r["use_existing"]),
         "factory_system":          r["factory_system"] or "",
         "factory_output_per_hour": r["factory_output_per_hour"],
         "factory_character_ids":   _json.loads(r["factory_character_ids"] or "[]"),
         "factory_planet_types":    _json.loads(r["factory_planet_types"] or "[]"),
         "split_mode":              r["split_mode"] or "off",
         "distribution_mode":       r["distribution_mode"] or "stability"}
        for r in rows
    ]}


@router.post("/api/profiles")
def save_profile(req: ProfileSave, context_id: int = Depends(require_context)):
    ensure_profile_tables()
    con = get_connection()
    con.execute("""
        INSERT OR REPLACE INTO pp_profiles
            (context_id, name, type_id, type_name, overproduction_pct, preferred_systems,
             constellations, use_existing, factory_system, factory_output_per_hour,
             factory_character_ids, max_jumps, factory_planet_types, split_mode, distribution_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (context_id, req.name, req.type_id, req.type_name, req.overproduction_pct,
          req.preferred_systems, _json.dumps(req.constellations),
          1 if req.use_existing else 0, req.factory_system or "",
          req.factory_output_per_hour, _json.dumps(req.factory_character_ids), req.max_jumps,
          _json.dumps(req.factory_planet_types), _norm_split_mode(req.split_mode),
          _norm_dist_mode(req.distribution_mode)))
    con.commit()
    con.close()
    return {"ok": True}


@router.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, context_id: int = Depends(require_context)):
    ensure_profile_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_profiles WHERE id=? AND context_id=?", (profile_id, context_id))
    con.commit()
    con.close()
    return {"ok": True}


# ── Saved plan snapshots (for the PI Planner refill distribution tool) ─────────
# A snapshot is the computed plan's factory→P1 distribution, stored per context so a player
# can split stacks into their factories without re-running the wizard. Stored server-side
# (cross-device) and named; the frontend also keeps a localStorage copy of the last build.

def ensure_plan_snapshot_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_plan_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id INTEGER NOT NULL,
            name       TEXT NOT NULL,
            snapshot   TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(context_id, name)
        )
    """)
    con.commit()
    con.close()


class PlanSnapshotSave(BaseModel):
    name: str
    snapshot: dict


@router.post("/api/plan-snapshots")
def save_plan_snapshot(req: PlanSnapshotSave, context_id: int = Depends(require_context)):
    from fastapi import HTTPException
    from datetime import datetime, timezone
    name = (req.name or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Plan name is required")
    ensure_plan_snapshot_table()
    con = get_connection()
    con.execute(
        "INSERT OR REPLACE INTO pp_plan_snapshots (context_id, name, snapshot, created_at) "
        "VALUES (?,?,?,?)",
        (context_id, name, _json.dumps(req.snapshot), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.get("/api/plan-snapshots")
def list_plan_snapshots(pp_session: str = Cookie(default=None)):
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"snapshots": []}
    ensure_plan_snapshot_table()
    con = get_connection()
    rows = con.execute(
        "SELECT id, name, snapshot, created_at FROM pp_plan_snapshots WHERE context_id=? ORDER BY name",
        (context_id,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            snap = _json.loads(r["snapshot"])
        except Exception:
            snap = {}
        out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                    "factories": snap.get("factories", [])})
    return {"snapshots": out}


@router.delete("/api/plan-snapshots/{snap_id}")
def delete_plan_snapshot(snap_id: int, context_id: int = Depends(require_context)):
    ensure_plan_snapshot_table()
    con = get_connection()
    con.execute("DELETE FROM pp_plan_snapshots WHERE id=? AND context_id=?", (snap_id, context_id))
    con.commit()
    con.close()
    return {"ok": True}


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
) -> tuple[int, int, dict[int, int], bool, float]:
    """
    Compute how many factory slots vs extractor slots to allocate.
    Returns (ext_slots, factories, factory_shares, auto_mode, p0_per_factory_per_day).
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

    return ext_slots, factories, _compute_factory_shares(char_list, factories, auto_mode, per_char_fac_cap, preferred_cids), auto_mode, p0_per_factory_day


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
        p0s_a = [s["p0_name"] for s in asgn_a["extractors"] if s.get("p0_name")]

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
                p0s_b = [s["p0_name"] for s in asgn_b["extractors"] if s.get("p0_name")]

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
    """Attach system/planet_num/quality_pct to each extractor slot. Mutates assignments."""
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

        for slot in sorted(asgn["extractors"], key=_priority):
            p0_name = slot.get("p0_name")
            if not p0_name:
                continue
            if slot.get("is_existing"):
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
            else:
                planets = p0_planet_lists.get(p0_name, [])
                nonfac_occupied = {
                    (p.get("system_name"), p.get("planet_num"))
                    for p in char_nonfac_ext.get(cid, [])
                    if p.get("system_name") and p.get("planet_num") is not None
                }
                # Factory chars needing all B/T keep those planets free for factories.
                if factory_avoid_cids and cid in factory_avoid_cids and factory_avoid:
                    nonfac_occupied = nonfac_occupied | factory_avoid
                planet = next(
                    (p for p in planets
                     if (p["system"], p["planet_num"]) not in char_used
                     and (p["system"], p["planet_num"]) not in nonfac_occupied),
                    None,
                ) or next(
                    (p for p in planets if (p["system"], p["planet_num"]) not in char_used),
                    None,
                )
                if planet:
                    slot["system"] = planet["system"]
                    slot["planet_num"] = planet["planet_num"]
                    slot["planet_type"] = planet["planet_type"]
                    slot["quality_pct"] = round(planet["value"])
                    char_used.add((planet["system"], planet["planet_num"]))


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


def _ext_actual_p0_per_day(extractors: list[dict]) -> float:
    """Quality-adjusted P0/day, counting a split leg as heads × quality (not a full planet)."""
    total = 0.0
    for e in extractors:
        if e.get("split"):
            for leg in e.get("legs", []):
                total += leg.get("heads", 0) * leg.get("quality_pct", 100) / 100.0 * _PU_PER_PLANET_DAY
        else:
            total += e.get("quality_pct", 100) / 100.0 * 48_000 * 24
    return total


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
                    leg = _solve_split_heads(A, B, host, pu_out, _need, mode)
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
                            best_fac_system, ext_slots, factories,
                            make_factory_product=None) -> tuple[int, int]:
    """Aggressive reinvestment: fill the planet slots freed by split-consolidation with extra
    factory + extractor planets (in the plan's equilibrium ext:fac ratio) so reclaimed
    overproduction capacity produces MORE rather than just leaving fewer planets in use.
    Concrete planets are placed so the per-character view stays consistent.

    make_factory_product: optional callable → a {type_id, name} dict tagging each reinvested
    factory to a production line (fuel-block/basket), or None for the single-product planner
    (factory assignments carry no per-line product). Returns (added_factories, added_extractors);
    the caller rescales throughput from the new totals."""
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
                product = make_factory_product() if (cand and make_factory_product) else None
                if cand and (product is not None or make_factory_product is None):
                    fa = {
                        "system": cand["system"], "planet_num": cand["planet_num"],
                        "planet_type": cand["planet_type"], "is_new": True, "reinvest": True,
                    }
                    if product is not None:
                        fa["product"] = product
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


def _solve_split_heads(A, B, host, pu_out, need, mode) -> tuple | None:
    """Allocate the 10-head budget across the two legs, OUTPUT-PRESERVING for both modes:
    returns (headsA, headsB, qA, qB) only if the legs can cover each P0's floor within 10
    heads, else None. (Aggressive's extra value is reinvesting the freed planet, not
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
    char_rows = con.execute("""
        SELECT character_id, character_name,
               1 + interplanetary_consolidation AS max_planets,
               command_center_upgrades AS ccu
        FROM pp_characters WHERE context_id=?
        ORDER BY (1 + interplanetary_consolidation) DESC, character_name
    """, (context_id,)).fetchall()

    try:
        planet_rows = con.execute("""
            SELECT cp.character_id, cp.planet_type, cp.is_extractor, cp.p0_type_id,
                   COALESCE(ss.name, '') as system_name, cp.planet_num
            FROM pp_char_planets cp
            JOIN pp_characters pc ON pc.character_id = cp.character_id
            LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
            WHERE pc.context_id=?
        """, (context_id,)).fetchall()
        has_system_name = True
    except Exception:
        planet_rows = con.execute(
            "SELECT cp.character_id, cp.planet_type, cp.is_extractor, cp.p0_type_id "
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
    p0_planet_lists = _fetch_p0_planets(
        all_p0_names, con, constellations=req.constellations,
        systems=req.chosen_systems if req.chosen_systems else None,
    )
    p0_planet_lists_global = _fetch_p0_planets(all_p0_names, con, [], None)
    best_ptypes = {
        name: (planets[0]["planet_type"] if planets else None)
        for name, planets in p0_planet_lists.items()
    }

    if req.chosen_systems:
        sys_recs = _system_recommendations(
            all_p0_names, con, constellations=None,
            systems=req.chosen_systems, preferred_systems=len(req.chosen_systems),
        )
    else:
        sys_recs = _system_recommendations(
            all_p0_names, con, constellations=req.constellations or None,
            preferred_systems=req.preferred_systems, max_jumps=req.max_jumps,
        )

    p0_to_p1_name = {p0_name: types.get(p1_id, {}).get("name", "?")
                     for p1_id, _, _, p0_name in p1_info_raw if p0_name}
    for rec in sys_recs:
        for asgn in rec["assignments"]:
            asgn["p1_name"] = p0_to_p1_name.get(asgn["p0_name"], "")
    return p0_planet_lists, p0_planet_lists_global, best_ptypes, sys_recs


def _factory_candidates(con, req, only_bt: bool = False, allowed_types: list[str] | None = None):
    """Factory-planet DB candidates + per-system options for the UI picker.
    `allowed_types` restricts the pool to those planet types (Barren first in the
    placement order); pass e.g. ['Barren','Temperate'] for the fuel-block default. When
    None, any planet type qualifies. `only_bt=True` is shorthand for the B/T restriction
    (single-product factories). Returns (fac_pool, factory_system_options, sys_fac_capacity)."""
    if only_bt and allowed_types is None:
        allowed_types = ["Barren", "Temperate"]
    # Build the planet_type IN (...) restriction (None → unrestricted)
    if allowed_types:
        type_clause = "planet_type IN ({})".format(",".join("?" * len(allowed_types)))
        type_params = list(allowed_types)
    else:
        type_clause, type_params = "", []

    fac_filter, fac_params = "", []
    fac_systems = list({*req.chosen_systems, req.factory_system}) if req.factory_system else list(req.chosen_systems)
    if fac_systems:
        fac_filter = " AND system IN ({})".format(",".join("?" * len(fac_systems)))
        fac_params = fac_systems
    elif req.constellations:
        fac_filter = " AND constellation IN ({})".format(",".join("?" * len(req.constellations)))
        fac_params = list(req.constellations)

    if type_clause:
        where = f"WHERE {type_clause}{fac_filter}"
        order = "ORDER BY CASE planet_type WHEN 'Barren' THEN 0 ELSE 1 END, system, planet_num"
        fac_params = type_params + fac_params
    else:
        where = f"WHERE 1=1{fac_filter}"
        order = "ORDER BY system, planet_num"
    try:
        fac_pool = [dict(r) for r in con.execute(
            f"SELECT system, planet_num, planet_type FROM pp_planets {where} {order}",
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


def _run_extractor_pipeline(
    req, char_list, p1_info, ext_slots, needed_at_baseline,
    p0_planet_lists, p0_planet_lists_global, has_planet_db, has_system_name,
    auto_mode, assignment_extra=None, factory_avoid_cids=None, factory_avoid=None,
    density_est=None,
):
    """Shared extractor-assignment core. Builds the per-character candidate views, the
    P0 slot caps and the Bresenham need list, then runs the two-pass extractor
    assignment and attaches concrete planet details.

    char_nonfac_ext: a char's idle factory planets are only "reserved" (kept off the
    extractor candidate list) when the char actually has factory slots carved out this
    plan (effective_planets > computed_ext_cap). Pure-extractor chars repurpose idle
    existing factory planets — essential for scarce P0s. factory_avoid(_cids) keeps the
    factory system's B/T planets free for maxed factory chars.
    Returns (assignments, remaining, char_nonfac)."""
    char_nonfac: dict[int, list] = {
        c["character_id"]: [p for p in c["planets"] if not p.get("is_extractor")]
        for c in char_list
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
    _attach_extractor_planet_details(
        assignments, char_list, char_nonfac, char_nonfac_ext,
        p0_planet_lists, p0_planet_lists_global, req, auto_mode,
        factory_avoid_cids=factory_avoid_cids, factory_avoid=factory_avoid,
    )
    return assignments, remaining, char_nonfac


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

    p0_planet_lists, p0_planet_lists_global, best_ptypes, sys_recs = _fetch_planets_and_recs(
        con, all_p0_names, req, types, p1_info_raw)

    fac_db_planets, factory_system_options, sys_fac_capacity = _factory_candidates(
        con, req, only_bt=True)
    for rec in sys_recs:
        rec["factory_capacity"] = {s: sys_fac_capacity.get(s, 0) for s in rec["systems_needed"]}

    con.close()

    char_planets: dict[int, list] = {}
    for p in planet_rows:
        char_planets.setdefault(p["character_id"], []).append(dict(p))

    p1_info = _build_p1_info(p1_info_raw, best_ptypes, types)
    char_list = _build_char_list(char_rows, config_map, char_planets, with_ccu=False)

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

    # Effective per-factory output rate (units/hr). A P4 factory produces ~0.5/hr over
    # its full P2→P3→P4 chain; the raw SDE rate (output_qty × cycles) only reflects the
    # final step and over-counts P4 by ~2×, making factory count and products/day
    # disagree (e.g. 9 factories but 216/day, which really needs 18). For P1–P3 the SDE
    # rate is correct. A user override always wins.
    _tier = types.get(req.type_id, {}).get("pi_tier")
    if req.factory_output_per_hour is not None and req.factory_output_per_hour > 0:
        effective_fph = req.factory_output_per_hour
    elif _tier == 4:
        effective_fph = 0.5
    else:
        effective_fph = output_qty * 3600.0 / cycle_time

    # Compute slot budget
    ext_slots, factories, factory_shares, auto_mode, p0_per_factory_day = _compute_slot_budget(
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

    # Refill cadence: factory planets import P1 into launchpads (the practical setup,
    # matching the Factory Layout templates). P1 = 0.38 m³; assume 3 launchpads
    # (30,000 m³) of input buffer. How long until that buffer empties?
    _P1_VOLUME = 0.38
    _FACTORY_LAUNCHPADS = 3
    total_p1_per_day = products_per_day * sum(p1_fracs.values())
    p1_m3_per_factory_day = (total_p1_per_day * _P1_VOLUME / factories) if factories else 0.0
    factory_buffer_m3 = _FACTORY_LAUNCHPADS * 10_000
    factory_refill_hours = (
        round(factory_buffer_m3 / (p1_m3_per_factory_day / 24), 1)
        if p1_m3_per_factory_day > 0 else None
    )
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

    assignments, remaining, char_nonfac = _run_extractor_pipeline(
        req, char_list, p1_info, ext_slots, needed_at_baseline,
        p0_planet_lists, p0_planet_lists_global, has_planet_db, has_system_name,
        auto_mode, factory_avoid_cids=_factory_avoid_cids, factory_avoid=_factory_avoid,
        density_est=density_est,
    )

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
                factory_refill_hours = (
                    round(factory_buffer_m3 / (p1_m3_per_factory_day / 24), 1)
                    if p1_m3_per_factory_day > 0 else None)

    all_assignments = sorted(assignments, key=lambda a: a["character_name"])
    total_extractors = sum(len(a["extractors"]) for a in all_assignments)
    total_factory_planets = sum(a["factory_planets"] for a in all_assignments)

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
    _actual_p0_per_day = sum(
        _ext_actual_p0_per_day(a["extractors"]) for a in all_assignments
    )
    max_supportable_factories = int(_actual_p0_per_day / p0_per_factory_day) if p0_per_factory_day > 0 else 0

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

    total_unassigned = sum(len(a.get("extractors", [])) for a in result.get("assignments", []))
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
