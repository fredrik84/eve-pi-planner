"""ESI's public industry cost-index and adjusted-price feeds, used to estimate real reaction job
installation fees: Job Cost = EIV x (System Cost Index + Facility Tax Rate), where EIV
(Estimated Item Value) is the sum of a job's consumed materials valued at their CCP-published
"adjusted price" (a separate, slower-moving reference table — NOT the live Jita price this app
uses everywhere else for actual profitability). Both ESI endpoints return their entire published
table in one call (no per-ID filtering is offered), so caching is one blob each rather than
app.market's per-type-id scheme.

Best-effort throughout: any ESI failure returns an empty/zero result, never raises — a player who
hasn't configured a reaction system (see app.reactions' effective_reaction_settings) never even
calls into this module, and a transient ESI outage for one who has just means job cost silently
reads as 0 for that request, matching pre-feature behavior rather than breaking the page.
"""
import json
import time
import urllib.request

from app.cache import cache_get_json, cache_set_json

ESI_BASE = "https://esi.evetech.net/latest"
_COST_INDEX_TTL = 6 * 3600       # cost indices drift with regional activity — hours-fresh is fine
_ADJUSTED_PRICE_TTL = 24 * 3600  # CCP updates these roughly daily

# (value, fetched_at) — per-process L1, mirrors app.market's rationale: avoids a Redis round
# trip on every single request even when Redis is warm, and is the only cache tier at all when
# REDIS_URL isn't set (e.g. local dev).
_cost_index_cache: tuple[dict[int, float], float] | None = None
_adjusted_price_cache: tuple[dict[int, float], float] | None = None


def _fetch_json(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eve-pi-planner/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _all_reaction_cost_indices() -> dict[int, float]:
    global _cost_index_cache
    now = time.monotonic()
    if _cost_index_cache and now - _cost_index_cache[1] < _COST_INDEX_TTL:
        return _cost_index_cache[0]
    cached = cache_get_json("industry:cost_indices")
    if cached is not None:
        result = {int(k): v for k, v in cached.items()}
        _cost_index_cache = (result, now)
        return result
    data = _fetch_json(f"{ESI_BASE}/industry/systems/?datasource=tranquility")
    if not data:
        return _cost_index_cache[0] if _cost_index_cache else {}
    result = {}
    for row in data:
        sid = row.get("solar_system_id")
        if not sid:
            continue
        for ci in row.get("cost_indices", []):
            if ci.get("activity") == "reaction":
                result[sid] = ci.get("cost_index", 0.0) or 0.0
                break
    cache_set_json("industry:cost_indices", result, ttl=_COST_INDEX_TTL)
    _cost_index_cache = (result, now)
    return result


def fetch_system_cost_index(system_id: int | None) -> float:
    """Reaction cost index for one system (e.g. 0.0192 = 1.92%), 0.0 if unset/unknown/
    unavailable — the safe no-job-cost-effect default."""
    if not system_id:
        return 0.0
    return _all_reaction_cost_indices().get(system_id, 0.0)


def _all_adjusted_prices() -> dict[int, float]:
    global _adjusted_price_cache
    now = time.monotonic()
    if _adjusted_price_cache and now - _adjusted_price_cache[1] < _ADJUSTED_PRICE_TTL:
        return _adjusted_price_cache[0]
    cached = cache_get_json("industry:adjusted_prices")
    if cached is not None:
        result = {int(k): v for k, v in cached.items()}
        _adjusted_price_cache = (result, now)
        return result
    data = _fetch_json(f"{ESI_BASE}/markets/prices/?datasource=tranquility")
    if not data:
        return _adjusted_price_cache[0] if _adjusted_price_cache else {}
    result = {row["type_id"]: row.get("adjusted_price", 0.0) or 0.0 for row in data if "type_id" in row}
    cache_set_json("industry:adjusted_prices", result, ttl=_ADJUSTED_PRICE_TTL)
    _adjusted_price_cache = (result, now)
    return result


def fetch_adjusted_prices(type_ids: list[int]) -> dict[int, float]:
    """{type_id: adjusted_price} for the requested IDs, 0.0 for anything CCP hasn't published a
    price for (some obscure/unlisted types) — never omits a requested ID."""
    all_prices = _all_adjusted_prices()
    return {tid: all_prices.get(tid, 0.0) for tid in type_ids}
