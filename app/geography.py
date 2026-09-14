"""Small geography predicates shared by PI import and recommendation code."""

import re


# CCP's canonical solar-system ID range for wormhole space.  J-space has no stable
# stargate topology, so membership must never imply an edge in ``system_jumps``.
WORMHOLE_SYSTEM_ID_MIN = 31_000_000
WORMHOLE_SYSTEM_ID_MAX = 31_999_999
_JSPACE_NAME_RE = re.compile(r"J\d{6}", re.IGNORECASE)


def is_wormhole_system_id(system_id) -> bool:
    try:
        value = int(system_id)
    except (TypeError, ValueError):
        return False
    return WORMHOLE_SYSTEM_ID_MIN <= value <= WORMHOLE_SYSTEM_ID_MAX


def looks_like_jspace_system_name(name: str) -> bool:
    """True for the normal J123456 spelling, including case-insensitive pasted data."""
    return bool(_JSPACE_NAME_RE.fullmatch((name or "").strip()))
