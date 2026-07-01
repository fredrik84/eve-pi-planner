"""
Pure serialization/formatting helpers for the planner endpoints (app/planner.py):
distribution/split-mode normalization, share anonymization, and fleet-staleness
comparison. No DB writes, no distribution-algorithm logic.
"""
import json as _json

from app.esi import PI_CHAR_SQL


def _norm_dist_mode(v) -> str:
    # Default to density-aware "stability"; only "need" opts back to pure need-proportional.
    return "need" if v == "need" else "stability"


def _norm_split_mode(v) -> str:
    # Single on/off toggle now. Legacy "conservative"/"aggressive" both map to "on" (the
    # consolidate-then-reinvest behaviour); only "off"/blank stays off.
    return "off" if (not v or v == "off") else "on"


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


def _fleet_fingerprint(con, context_id: int) -> dict:
    """{char_id: [ccu, ic]} for the context's real characters — the state a saved plan depends on."""
    return {str(r["character_id"]): [r["command_center_upgrades"] or 0, r["interplanetary_consolidation"] or 0]
            for r in con.execute(
                "SELECT character_id, command_center_upgrades, interplanetary_consolidation "
                "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0 "
                + PI_CHAR_SQL, (context_id,)).fetchall()}


def _plan_staleness(saved: dict, current: dict) -> dict:
    """A saved plan is stale once the fleet it assumed has changed — toons added/removed or skills
    trained. Re-running would then place colonies differently. Returns {stale, reason}."""
    if not saved:
        return {"stale": False, "reason": ""}          # pre-feature profile → can't compare, don't nag
    added = [c for c in current if c not in saved]
    removed = [c for c in saved if c not in current]
    changed = [c for c in current if c in saved and current[c] != saved[c]]
    if not (added or removed or changed):
        return {"stale": False, "reason": ""}
    parts = []
    if added:   parts.append(f"{len(added)} character{'s' if len(added) != 1 else ''} added")
    if removed: parts.append(f"{len(removed)} removed")
    if changed: parts.append(f"{len(changed)} skill change{'s' if len(changed) != 1 else ''}")
    return {"stale": True, "reason": " · ".join(parts) + " since saved"}
