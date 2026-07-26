"""One place every ESI request goes through.

Before this, each module opened its own httpx client and none of them looked at what ESI was
telling us. That matters more than request volume: **CCP does not ban on how much you ask for, it
bans on burning the error budget.** Every response carries `X-ESI-Error-Limit-Remain` counting down
toward a block, and the correct response to a shrinking budget is to stop until the window resets —
not to keep probing and find out.

Two things make this a shared module rather than a helper per caller:

* **The budget is per source IP, not per process.** Prod runs several replicas behind one egress
  address, so they share one budget. A process-local counter would see a healthy number in each pod
  while the real budget hit zero. It's tracked in Redis so every replica sees the same figure, and
  degrades to a process-local view when Redis is down.
* **CCP asks clients to identify themselves.** A descriptive User-Agent is the difference between
  "we'll email you about this" and a silent block; setting it in one place means a new call site
  can't forget.

Callers get the `httpx.Response` back and keep full control of parsing and error handling — this
wraps the politeness, not the semantics.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from app.cache import cache_get_json, cache_set_json

log = logging.getLogger(__name__)

ESI_BASE = os.getenv("ESI_BASE", "https://esi.evetech.net/latest")

# Identify the app, per CCP's developer guidelines. A contact in the UA is what lets them reach out
# instead of just blocking an anonymous client.
USER_AGENT = os.getenv(
    "ESI_USER_AGENT",
    "eve-pi-planner/1.0 (+https://eve-pi.failed.name; industry & PI planner)",
)

_BUDGET_KEY = "esi:error_budget"
_ERR_FLOOR = 25          # start yielding while fewer than this many errors remain in the window
_MIN_INTERVAL = 0.05     # ~20 req/s ceiling, comfortably under anything ESI objects to

_local = threading.local()
_local_budget = {"remain": 100, "reset_at": 0.0}
_budget_lock = threading.Lock()


def _read_budget() -> dict:
    """Shared error budget. Redis when available so every replica sees the same number."""
    b = cache_get_json(_BUDGET_KEY)
    if isinstance(b, dict) and "remain" in b:
        return b
    with _budget_lock:
        return dict(_local_budget)


def _write_budget(remain: int, reset_at: float) -> None:
    b = {"remain": remain, "reset_at": reset_at}
    with _budget_lock:
        _local_budget.update(b)
    # TTL just past the reset so a stale budget can never pin us in backoff forever.
    cache_set_json(_BUDGET_KEY, b, ttl=max(2, int(reset_at - time.time()) + 5))


def _pre_request_wait() -> None:
    """Yield if the shared budget is nearly spent, and pace requests either way."""
    b = _read_budget()
    remain, reset_at = b.get("remain", 100), b.get("reset_at", 0.0)
    now = time.time()
    if reset_at > now and remain < _ERR_FLOOR:
        # Wait out the window rather than spend the last of the budget. Capped so a bad value can't
        # wedge a worker.
        time.sleep(min(reset_at - now + 1, 90))
    last = getattr(_local, "last_call", 0.0)
    gap = now - last
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _local.last_call = time.time()


def _record(resp: httpx.Response) -> None:
    """Feed the budget from the response headers ESI sends on every call."""
    raw_remain = resp.headers.get("x-esi-error-limit-remain")
    if raw_remain is None:
        # No header — a proxy, a cached response, or a non-ESI host. Leave the budget ALONE:
        # defaulting to 100 here would wipe a genuinely low budget and cancel the backoff, which
        # is the exact opposite of what a missing signal should do.
        return
    try:
        remain = int(raw_remain)
        reset = int(resp.headers.get("x-esi-error-limit-reset", 60))
    except (TypeError, ValueError):
        return
    _write_budget(remain, time.time() + reset)
    if resp.status_code == 420:
        # 420 = you've been error-limited. Nothing to do but wait the window out.
        log.warning("ESI error-limited (420); backing off %ss", reset)
        time.sleep(min(reset + 1, 90))


def request(method: str, url: str, *, token: str | None = None, client: httpx.Client | None = None,
            timeout: float = 20.0, **kw) -> httpx.Response:
    """One ESI call: paced, budget-aware, and identified.

    `url` may be a full URL or a path relative to ESI_BASE. Pass `client` to reuse a connection
    across a batch of calls; otherwise a short-lived one is made.
    """
    if not url.startswith("http"):
        url = f"{ESI_BASE}/{url.lstrip('/')}"
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("User-Agent", USER_AGENT)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _pre_request_wait()
    if client is not None:
        resp = client.request(method, url, headers=headers, **kw)
    else:
        with httpx.Client(timeout=timeout) as c:
            resp = c.request(method, url, headers=headers, **kw)
    _record(resp)
    return resp


def get(url: str, **kw) -> httpx.Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> httpx.Response:
    return request("POST", url, **kw)


def client(timeout: float = 20.0) -> httpx.Client:
    """A client pre-set with the right User-Agent, for callers doing many calls in a loop. Pass it
    to `get`/`post` as `client=` so the budget is still tracked."""
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})


def budget() -> dict:
    """Current shared error budget — for diagnostics and the admin view."""
    b = _read_budget()
    return {"remain": b.get("remain", 100),
            "resets_in": max(0, round(b.get("reset_at", 0.0) - time.time(), 1))}
