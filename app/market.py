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
