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


# ── Per-request memo ───────────────────────────────────────────────────────────────────────────
# Lives here rather than in either feature package because BOTH use it: the reactions evidence
# layer and app/industry's blueprint reads are the same expensive queries, and app/industry must
# not import app/reactions (the dependency runs the other way).
#
# Scoped to the request, never a TTL: a paste or an asset rescan has to show up on the very next
# page load, and a test that seeds rows and reads them back must see its own writes. The scope is
# opened by middleware in app.main, so a direct call — every test, any background job — gets no
# memoisation at all.
import contextvars as _contextvars

_REQUEST_MEMO: _contextvars.ContextVar = _contextvars.ContextVar("pp_request_memo", default=None)


def begin_request_memo() -> None:
    """Open a memo scope for THIS request. Called by the HTTP middleware and nothing else."""
    _REQUEST_MEMO.set({})


def forget_memo(key) -> None:
    """Drop one memoised answer, so a read after a write in the SAME request sees the write.

    The memo is per request and most requests either read or write, so this is only needed where a
    writer reads its own result back — but where that happens, a stale answer is silent and wrong,
    which is worse than the read it saved."""
    store = _REQUEST_MEMO.get()
    if store is not None:
        store.pop(key, None)


def request_memo(key, build):
    """Compute `build()` once per request for `key`, or just call it when no scope is open."""
    store = _REQUEST_MEMO.get()
    if store is None:
        return build()
    if key not in store:
        store[key] = build()
    return store[key]
