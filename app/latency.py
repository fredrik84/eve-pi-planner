"""Opt-in, request-scoped diagnostics. Never records SQL, keys, URLs or user data.

Detailed response headers require LATENCY_TOKEN supplied as X-Latency-Token.
The page_profiling rollout enables private numeric request events separately.
Durations are cumulative work, NOT additive portions of response wall time (parallel
tasks and nested acquisition/queries can overlap). No background SQL tracing.
"""
from contextvars import ContextVar
from functools import wraps
from secrets import compare_digest
from threading import Lock
from time import perf_counter
import os

_current = ContextVar("latency", default=None)


class Measurements:
    def __init__(self):
        self.values = {}
        self.lock = Lock()

    def add(self, name, duration=0, count=1):
        with self.lock:
            elapsed, calls = self.values.get(name, (0, 0))
            self.values[name] = (elapsed + duration, calls + count)

    def header(self, elapsed):
        with self.lock:
            entries = [f'backend;dur={elapsed * 1000:.3f}']
            entries.extend(f'{name};dur={duration * 1000:.3f};desc="{count}"'
                           for name, (duration, count) in sorted(self.values.items()))
        return ", ".join(entries)

    def profile_fields(self):
        durations = ("db_acquire", "db_execute", "db_fetch", "db_commit", "redis_read",
                     "redis_write", "esi_request", "market_upstream", "recipe_graph_load")
        counters = ("redis_hit", "redis_miss", "redis_error", "redis_bypass", "memo_hit",
                    "memo_miss", "memo_bypass", "plan_l1_hit", "plan_redis_hit", "plan_miss",
                    "market_l1_hit", "market_l1_negative_hit", "market_l1_miss",
                    "recipe_graph_hit", "recipe_graph_miss")
        with self.lock:
            result = {"profiled": True}
            for name in durations:
                elapsed, calls = self.values.get(name, (0, 0))
                result[name + "_ms"] = round(elapsed * 1000, 3)
                result[name + "_count"] = calls
            result.update((name, self.values.get(name, (0, 0))[1]) for name in counters)
        return result


def count(name, amount=1):
    scope = _current.get()
    if scope is not None:
        scope.add(name, count=amount)


def timed(name):
    """Sync operation timer; ContextVar follows FastAPI's sync worker threads."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            scope = _current.get()
            if scope is None:
                return fn(*args, **kwargs)
            start = perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                scope.add(name, perf_counter() - start)
        return wrapper
    return decorate


class LatencyMiddleware:
    """Pure ASGI: backend ends at response headers; streaming body is not included."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        secret = os.environ.get("LATENCY_TOKEN", "")
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-latency-token", b"")
        diagnostic = bool(secret and compare_digest(supplied, secret.encode()))
        path = scope.get("path", "")
        profile = False
        if (path and path not in {"/healthz", "/metrics", "/api/telemetry/page-load"}
                and "." not in path.rsplit("/", 1)[-1]
                and headers.get(b"dnt") != b"1" and headers.get(b"sec-gpc") != b"1"):
            from app.profiling import profiling_enabled
            from starlette.concurrency import run_in_threadpool
            profile = await run_in_threadpool(profiling_enabled)
        if not diagnostic and not profile:
            return await self.app(scope, receive, send)
        measurements = Measurements()
        if profile:
            scope["evepi.profiling"] = measurements
        token = _current.set(measurements)
        start = perf_counter()

        async def measured_send(message):
            if message["type"] == "http.response.start" and diagnostic:
                # Timed responses must never become shared/browser cached diagnostics.
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"server-timing", b"cache-control")]
                headers.extend([
                    (b"server-timing", measurements.header(perf_counter() - start).encode()),
                    (b"cache-control", b"private, no-store"),
                ])
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            _current.reset(token)
