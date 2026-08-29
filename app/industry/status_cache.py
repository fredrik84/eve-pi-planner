"""Short-lived cache for the expensive Manufacturing status-plan response.

The browser asks for the same queue plan whenever Manufacturing opens. Capital queues make that a
real schedule solve, even when no order, stock, job or setting changed since the previous request.
Keep a small L1 copy for single-process/local installs and Redis as L2 for multi-pod production.
Writers bump one per-account generation, invalidating every request-option variant at once.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

from app.cache import cache_get_json, cache_set_json

STATUS_CACHE_TTL = 20
_MAX_LOCAL = 256
_LOCAL: dict[str, tuple[float, dict]] = {}
_GLOBAL_GENERATION_KEY = "ind:status:gen:all"


def _generation_key(context_id: int) -> str:
    return f"ind:status:gen:{int(context_id)}"


def _generation(context_id: int):
    return cache_get_json(_generation_key(context_id)) or 0


def _key(context_id: int, request_options: dict) -> str:
    raw = json.dumps(request_options, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    global_generation = cache_get_json(_GLOBAL_GENERATION_KEY) or 0
    return (f"ind:status:v1:{global_generation}:{int(context_id)}:"
            f"{_generation(context_id)}:{digest}")


def get_status(context_id: int, request_options: dict) -> dict | None:
    key = _key(context_id, request_options)
    now = time.time()
    local = _LOCAL.get(key)
    if local and now - local[0] < STATUS_CACHE_TTL:
        return copy.deepcopy(local[1])
    if local:
        _LOCAL.pop(key, None)
    cached = cache_get_json(key)
    if cached is None:
        return None
    _LOCAL[key] = (now, cached)
    return copy.deepcopy(cached)


def set_status(context_id: int, request_options: dict, value: dict) -> None:
    key = _key(context_id, request_options)
    now = time.time()
    if len(_LOCAL) >= _MAX_LOCAL:
        expired = [k for k, (at, _v) in _LOCAL.items() if now - at >= STATUS_CACHE_TTL]
        for k in expired:
            _LOCAL.pop(k, None)
        if len(_LOCAL) >= _MAX_LOCAL:
            _LOCAL.pop(min(_LOCAL, key=lambda k: _LOCAL[k][0]), None)
    safe = copy.deepcopy(value)
    _LOCAL[key] = (now, safe)
    cache_set_json(key, safe, ttl=STATUS_CACHE_TTL)


def invalidate_status(context_id: int | None = None) -> None:
    """Drop one account locally and bump its shared generation; bare clears local test state."""
    if context_id is None:
        _LOCAL.clear()
        cache_set_json(_GLOBAL_GENERATION_KEY, time.time_ns(), ttl=86400)
        return
    ctx = int(context_id)
    # The shared generation precedes the account id, so match the scoped middle segment.
    marker = f":{ctx}:"
    for key in [k for k in _LOCAL if marker in k]:
        _LOCAL.pop(key, None)
    # A generation avoids needing Redis wildcard deletion and invalidates every option signature.
    cache_set_json(_generation_key(ctx), time.time_ns(), ttl=86400)
