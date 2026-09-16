"""Opt-in, request-scoped diagnostics. Never records SQL, keys, URLs or user data.

Infrastructure gate: LATENCY_TOKEN must be configured and supplied as X-Latency-Token.
Durations are cumulative work, NOT additive portions of response wall time (parallel
tasks and nested acquisition/queries can overlap). No background/global telemetry.
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
        secret = os.environ.get("LATENCY_TOKEN", "")
        supplied = dict(scope.get("headers", [])).get(b"x-latency-token", b"")
        if (scope["type"] != "http" or not secret
                or not compare_digest(supplied, secret.encode())):
            return await self.app(scope, receive, send)
        measurements = Measurements()
        token = _current.set(measurements)
        start = perf_counter()

        async def measured_send(message):
            if message["type"] == "http.response.start":
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
