import json
import time
import urllib.request

FUZZWORKS_URL = "https://market.fuzzwork.co.uk/aggregates/"
JITA_STATION = 60003760
CACHE_TTL = 900  # 15 minutes

# {type_id: (price, fetched_at)}
_cache: dict[int, tuple[float, float]] = {}


def fetch_prices(type_ids: list[int]) -> dict[int, float]:
    """
    Fetch Jita sell prices from Fuzzworks (5th-percentile sell orders).
    Results are cached per type ID for 15 minutes.
    Returns {type_id: price}. Missing/failed IDs are omitted.
    """
    if not type_ids:
        return {}

    now = time.monotonic()
    result: dict[int, float] = {}
    missing: list[int] = []

    for tid in type_ids:
        entry = _cache.get(tid)
        if entry and now - entry[1] < CACHE_TTL:
            result[tid] = entry[0]
        else:
            missing.append(tid)

    if missing:
        fresh = _fetch_from_fuzzworks(missing)
        for tid, price in fresh.items():
            _cache[tid] = (price, now)
            result[tid] = price

    return result


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


# {type_id: (market_data_dict, fetched_at)} — separate cache from fetch_prices' above (sell-only,
# price-only), since this carries buy AND sell price + volume. Kept as its own function/cache
# rather than folded into fetch_prices, to avoid changing that widely-used function's return shape.
_market_cache: dict[int, tuple[dict, float]] = {}


def fetch_market_data(type_ids: list[int]) -> dict[int, dict]:
    """
    Fetch both sides of the Jita order book from Fuzzworks: {buy_price, sell_price, buy_volume,
    sell_volume} per type. buy/sell_price are the 5th-percentile prices (same methodology as
    fetch_prices); buy/sell_volume are the current order-book depth on that side (units sitting
    in orders right now — a liquidity/"how much can this absorb" proxy, NOT a trade-velocity
    figure). Results are cached per type ID for 15 minutes. Missing/failed IDs are omitted.
    """
    if not type_ids:
        return {}

    now = time.monotonic()
    result: dict[int, dict] = {}
    missing: list[int] = []

    for tid in type_ids:
        entry = _market_cache.get(tid)
        if entry and now - entry[1] < CACHE_TTL:
            result[tid] = entry[0]
        else:
            missing.append(tid)

    if missing:
        fresh = _fetch_market_data_from_fuzzworks(missing)
        for tid, info in fresh.items():
            _market_cache[tid] = (info, now)
            result[tid] = info

    return result


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
