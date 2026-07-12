"""
Optional Redis cache for read-heavy, write-rare data (currently: the /api/characters payload,
invalidated on rescan / character add-remove rather than a bare TTL).

Entirely opt-in via REDIS_URL. When unset (production, today) every function here is a no-op —
callers always have a working DB fallback, so a missing/unreachable Redis degrades to exactly
today's behavior, never an error.
"""
import functools
import json
import logging
import os

log = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")


@functools.lru_cache(maxsize=1)
def _client():
    if not REDIS_URL:
        return None
    try:
        import redis
        c = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        c.ping()
        return c
    except Exception:
        log.exception("redis unavailable, caching disabled")
        return None


def cache_get_json(key: str):
    c = _client()
    if c is None:
        return None
    try:
        raw = c.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        log.exception("cache_get_json failed for %s", key)
        return None


def cache_set_json(key: str, value, ttl: int = 300):
    c = _client()
    if c is None:
        return
    try:
        c.setex(key, ttl, json.dumps(value))
    except Exception:
        log.exception("cache_set_json failed for %s", key)


def cache_invalidate(key: str):
    c = _client()
    if c is None:
        return
    try:
        c.delete(key)
    except Exception:
        log.exception("cache_invalidate failed for %s", key)


def cache_mget_json(keys: list[str]) -> dict[str, object]:
    """Batch GET (one round-trip, not N) — returns {key: value} for whichever keys had a
    cached, non-expired value; a miss is simply absent from the result, not an error."""
    c = _client()
    if c is None or not keys:
        return {}
    try:
        raw_values = c.mget(keys)
        return {k: json.loads(v) for k, v in zip(keys, raw_values) if v is not None}
    except Exception:
        log.exception("cache_mget_json failed")
        return {}


def cache_mset_json(items: dict, ttl: int = 300):
    """Batch SETEX via a pipeline (one round-trip, not N)."""
    c = _client()
    if c is None or not items:
        return
    try:
        pipe = c.pipeline()
        for k, v in items.items():
            pipe.setex(k, ttl, json.dumps(v))
        pipe.execute()
    except Exception:
        log.exception("cache_mset_json failed")


def charlist_key(context_id: int) -> str:
    return f"charlist:{context_id}"
