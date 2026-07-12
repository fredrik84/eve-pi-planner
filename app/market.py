import json
import time
import urllib.request

from app.cache import cache_mget_json, cache_mset_json

FUZZWORKS_URL = "https://market.fuzzwork.co.uk/aggregates/"
JITA_STATION = 60003760
CACHE_TTL = 900  # 15 minutes

# {type_id: (value, fetched_at)} — L1 cache, per-process. Fast (no network at all) but only
# helps repeat calls hitting the SAME worker — prod runs multiple uvicorn workers with no
# session affinity, so a request can easily land on a worker whose L1 is cold even though
# another worker just fetched the same type_ids seconds ago (confirmed real-world symptom: the
# Reactions shopping list "showing up late" — its own market fetch losing this per-worker
# cache-miss lottery independently of whatever warmed the opportunities-table request's worker).
_cache: dict[int, tuple[float, float]] = {}
_market_cache: dict[int, tuple[dict, float]] = {}


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
        if entry and now - entry[1] < CACHE_TTL:
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
            result[tid] = v
        else:
            still_missing.append(tid)
    if still_missing:
        fresh = fuzzworks_fn(still_missing)
        to_cache = {}
        for tid, val in fresh.items():
            local_cache[tid] = (val, now)
            result[tid] = val
            to_cache[f"{redis_prefix}{tid}"] = val
        cache_mset_json(to_cache, ttl=CACHE_TTL)
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
