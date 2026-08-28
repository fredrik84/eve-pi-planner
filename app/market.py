import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app.cache import cache_mget_json, cache_mset_json

FUZZWORKS_URL = "https://market.fuzzwork.co.uk/aggregates/"
JITA_STATION = 60003760
THE_FORGE_REGION = 10000002  # Jita's region — where market history that matters trades
CACHE_TTL = 900  # 15 minutes
FAILURE_CACHE_TTL = 60  # do not hammer upstream while it is unavailable
HISTORY_CACHE_TTL = 6 * 3600  # market history only updates ~daily, so cache it hard
HISTORY_DAYS = 7  # average daily traded volume over the last N days

# {type_id: (value, fetched_at)} — L1 cache, per-process. Fast (no network at all) but only
# helps repeat calls hitting the SAME worker — prod runs multiple uvicorn workers with no
# session affinity, so a request can easily land on a worker whose L1 is cold even though
# another worker just fetched the same type_ids seconds ago (confirmed real-world symptom: the
# Reactions shopping list "showing up late" — its own market fetch losing this per-worker
# cache-miss lottery independently of whatever warmed the opportunities-table request's worker).
_cache: dict[int, tuple[float, float]] = {}
_market_cache: dict[int, tuple[dict, float]] = {}
_CACHE_MISS = {"__market_unavailable__": True}


def _is_cached_miss(value) -> bool:
    return isinstance(value, dict) and value == _CACHE_MISS


def _cached_fetch(type_ids: list[int], local_cache: dict, redis_prefix: str, fuzzworks_fn):
    """Shared 3-tier lookup: in-process dict (L1) -> Redis (L2, shared across every worker and
    replica — a no-op if REDIS_URL isn't set, degrading exactly to the old L1-only behavior) ->
    Fuzzworks (L3, the actual network call every cache tier above exists to avoid)."""
    if not type_ids:
        return {}

    now = time.monotonic()
    result: dict[int, object] = {}
    missing: list[int] = []
    for tid in type_ids:
        entry = local_cache.get(tid)
        # A failed upstream lookup is cached briefly too.  Without this, a Fuzzwork outage (or a
        # type omitted from its response) made every page request immediately contact it again.
        ttl = FAILURE_CACHE_TTL if entry and _is_cached_miss(entry[0]) else CACHE_TTL
        if entry and now - entry[1] < ttl:
            if not _is_cached_miss(entry[0]):
                result[tid] = entry[0]
        else:
            missing.append(tid)
    if not missing:
        return result

    redis_keys = [f"{redis_prefix}{tid}" for tid in missing]
    redis_hits = cache_mget_json(redis_keys)
    still_missing = []
    for tid in missing:
        v = redis_hits.get(f"{redis_prefix}{tid}")
        if v is not None:
            local_cache[tid] = (v, now)
            if not _is_cached_miss(v):
                result[tid] = v
        else:
            still_missing.append(tid)
    if still_missing:
        fresh = fuzzworks_fn(still_missing)
        to_cache = {}
        misses_to_cache = {}
        for tid, val in fresh.items():
            local_cache[tid] = (val, now)
            result[tid] = val
            to_cache[f"{redis_prefix}{tid}"] = val
        # Cache every missing answer, not only values returned by Fuzzwork.  A short TTL keeps a
        # transient failure from becoming sticky while preventing request-rate retry storms.
        for tid in still_missing:
            if tid not in fresh:
                local_cache[tid] = (_CACHE_MISS, now)
                misses_to_cache[f"{redis_prefix}{tid}"] = _CACHE_MISS
        cache_mset_json(to_cache, ttl=CACHE_TTL)
        cache_mset_json(misses_to_cache, ttl=FAILURE_CACHE_TTL)
    return result


def fetch_prices(type_ids: list[int]) -> dict[int, float]:
    """
    Fetch Jita sell prices from Fuzzworks (5th-percentile sell orders).
    Results are cached per type ID for 15 minutes.
    Returns {type_id: price}. Missing/failed IDs are omitted.
    """
    return _cached_fetch(type_ids, _cache, "mkt:sell:", _fetch_from_fuzzworks)


def _fetch_from_fuzzworks(type_ids: list[int]) -> dict[int, float]:
    url = f"{FUZZWORKS_URL}?station={JITA_STATION}&types={','.join(str(t) for t in type_ids)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eve-pi-planner/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}

    return {
        int(tid): float(info.get("sell", {}).get("percentile", 0) or 0)
        for tid, info in data.items()
    }


_history_cache: dict[int, tuple[float, float]] = {}


def fetch_daily_volume(type_ids: list[int]) -> dict[int, float]:
    """Average daily traded UNITS over the last HISTORY_DAYS days, per type, from ESI market history
    for The Forge (Jita's region). This is real trade VELOCITY — how much actually changes hands —
    the honest basis for "how much can I sell over a run period" (unlike a single order-book depth
    snapshot). Public endpoint, one call per type (parallelised), cached HISTORY_CACHE_TTL since
    history only updates ~daily. Missing/failed IDs are omitted (caller falls back to depth)."""
    if not type_ids:
        return {}
    now = time.monotonic()
    result: dict[int, float] = {}
    missing: list[int] = []
    for tid in type_ids:
        entry = _history_cache.get(tid)
        ttl = FAILURE_CACHE_TTL if entry and _is_cached_miss(entry[0]) else HISTORY_CACHE_TTL
        if entry and now - entry[1] < ttl:
            if not _is_cached_miss(entry[0]):
                result[tid] = entry[0]
        else:
            missing.append(tid)
    if not missing:
        return result

    redis_keys = [f"mkt:hist:{tid}" for tid in missing]
    redis_hits = cache_mget_json(redis_keys)
    still_missing = []
    for tid in missing:
        v = redis_hits.get(f"mkt:hist:{tid}")
        if v is not None:
            _history_cache[tid] = (v, now)
            if not _is_cached_miss(v):
                result[tid] = v
        else:
            still_missing.append(tid)
    if still_missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = dict(zip(still_missing, pool.map(_fetch_one_history, still_missing)))
        to_cache = {}
        misses_to_cache = {}
        for tid, vol in fetched.items():
            if vol is None:
                _history_cache[tid] = (_CACHE_MISS, now)
                misses_to_cache[f"mkt:hist:{tid}"] = _CACHE_MISS
                continue
            _history_cache[tid] = (vol, now)
            result[tid] = vol
            to_cache[f"mkt:hist:{tid}"] = vol
        if to_cache:
            cache_mset_json(to_cache, ttl=HISTORY_CACHE_TTL)
        if misses_to_cache:
            cache_mset_json(misses_to_cache, ttl=FAILURE_CACHE_TTL)
    return result


def _fetch_one_history(type_id: int) -> float | None:
    # ESI (unlike the Fuzzwork fetches elsewhere in this module), so it goes through esi_http and
    # counts against the shared error budget.
    from app import esi_http
    try:
        rows = esi_http.get(
            f"markets/{THE_FORGE_REGION}/history/?datasource=tranquility&type_id={type_id}",
            timeout=10,
        ).json()
    except Exception:
        return None
    if not rows:
        return 0.0
    recent = rows[-HISTORY_DAYS:]  # ESI returns oldest→newest; take the last N days
    vols = [float(r.get("volume", 0) or 0) for r in recent]
    return (sum(vols) / len(vols)) if vols else 0.0


def fetch_market_data(type_ids: list[int]) -> dict[int, dict]:
    """
    Fetch both sides of the Jita order book from Fuzzworks: {buy_price, sell_price, buy_volume,
    sell_volume} per type. buy/sell_price are the 5th-percentile prices (same methodology as
    fetch_prices); buy/sell_volume are the current order-book depth on that side (units sitting
    in orders right now — a liquidity/"how much can this absorb" proxy, NOT a trade-velocity
    figure). Results are cached per type ID for 15 minutes. Missing/failed IDs are omitted.
    """
    return _cached_fetch(type_ids, _market_cache, "mkt:data:", _fetch_market_data_from_fuzzworks)


def _fetch_market_data_from_fuzzworks(type_ids: list[int]) -> dict[int, dict]:
    url = f"{FUZZWORKS_URL}?station={JITA_STATION}&types={','.join(str(t) for t in type_ids)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eve-pi-planner/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}

    result = {}
    for tid, info in data.items():
        buy = info.get("buy", {}) or {}
        sell = info.get("sell", {}) or {}
        result[int(tid)] = {
            "buy_price": float(buy.get("percentile", 0) or 0),
            "sell_price": float(sell.get("percentile", 0) or 0),
            "buy_volume": float(buy.get("volume", 0) or 0),
            "sell_volume": float(sell.get("volume", 0) or 0),
        }
    return result
