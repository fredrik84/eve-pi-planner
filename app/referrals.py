"""Cookie-free access events and admin-only referral counts from the local Loki store."""
import json
import logging
import math
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends

from app.cache import cache_get_json, cache_set_json
from app.esi import require_admin
from app.features import feature_enabled, feature_enabled_for

router = APIRouter()
log = logging.getLogger(__name__)
access_log = logging.getLogger("evepi.access")
SOURCES = {
    "google": "Google", "bing": "Bing", "duckduckgo": "DuckDuckGo",
    "yahoo": "Yahoo", "brave": "Brave Search", "yandex": "Yandex", "baidu": "Baidu",
    "reddit": "Reddit", "discord": "Discord", "eve_forums": "EVE forums",
    "other": "Other websites", "direct_unknown": "Direct / unknown",
}
_BOT = re.compile(r"bot|crawler|spider|headless|curl|wget|python|uptime|monitor", re.I)
_GOOGLE = re.compile(r"(?:www\.)?google\.(?:com|[a-z]{2}|(?:co|com)\.[a-z]{2})\Z")


def configure_access_logging():
    # Emit pure JSON so Loki can parse it, independently of the human-readable app log format.
    if not access_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        access_log.addHandler(handler)
    access_log.setLevel(logging.INFO)
    access_log.propagate = False


def collection_enabled():
    cached = cache_get_json("referrals:enabled:v1")
    if isinstance(cached, bool):
        return cached
    enabled = feature_enabled("referral_stats")
    cache_set_json("referrals:enabled:v1", enabled, ttl=30)
    return enabled


def classify_referrer(value: str, own_host: str) -> str:
    """Reduce an untrusted header to a fixed category; never retain its path or query."""
    if not value or len(value) > 4096:
        return "direct_unknown"
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
    except ValueError:
        return "direct_unknown"
    if url.scheme not in {"http", "https"} or not host:
        return "direct_unknown"
    if host in {own_host.lower(), "eveindustry.net", "www.eveindustry.net", "dev.eveindustry.net"}:
        return "internal"
    # Only Google's search origin, not docs.google.com or deceptive google.com.evil.tld names.
    if _GOOGLE.fullmatch(host):
        return "google"
    for domain, source in (
        ("bing.com", "bing"), ("duckduckgo.com", "duckduckgo"),
        ("search.yahoo.com", "yahoo"), ("search.brave.com", "brave"),
        ("yandex.com", "yandex"), ("yandex.ru", "yandex"), ("baidu.com", "baidu"),
        ("reddit.com", "reddit"), ("redd.it", "reddit"),
        ("discord.com", "discord"), ("discordapp.com", "discord"),
        ("forums.eveonline.com", "eve_forums"),
    ):
        if host == domain or host.endswith("." + domain):
            return source
    return "other"


def note_access(request, response, duration_ms):
    """Log route templates and coarse sources only; logging must never break a request."""
    try:
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        if not path or path == "/healthz":
            return
        page = (request.method == "GET" and 200 <= response.status_code < 300
                and response.headers.get("content-type", "").startswith("text/html")
                and not path.startswith(("/auth/", "/admin", "/docs", "/redoc")))
        # Prefetches and privacy opt-outs never contribute to referral counts.
        measure = (page and request.headers.get("dnt") != "1"
                   and request.headers.get("sec-gpc") != "1"
                   and "prefetch" not in (request.headers.get("purpose", "") +
                                          request.headers.get("sec-purpose", "")).lower()
                   and collection_enabled())
        event = {
            "event": "http_access", "method": request.method, "route": path,
            "status": response.status_code, "duration_ms": round(duration_ms, 1),
            "kind": "page" if measure else "request",
            "source": classify_referrer(request.headers.get("referer", ""), request.url.hostname or "")
                      if measure else "unmeasured",
            "bot": bool(_BOT.search(request.headers.get("user-agent", ""))),
        }
        access_log.info(json.dumps(event, separators=(",", ":")))
    except Exception:
        log.warning("Access event could not be recorded", exc_info=False)


def _query_counts(client, base_url, namespace, days):
    selector = '{namespace=' + json.dumps(namespace) + ',app="eve-pi-planner"}'
    query = (f'sum by (source) (count_over_time({selector} | json | __error__="" '
             f'| event="http_access" | kind="page" | bot="false" '
             f'| source!="internal" [{days}d]))')
    response = client.get(base_url + "/loki/api/v1/query", params={"query": query})
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "vector":
        raise ValueError("Unexpected log query response")
    counts = {}
    for item in payload["data"]["result"]:
        source = item.get("metric", {}).get("source")
        value = float(item["value"][1])
        if source in SOURCES and math.isfinite(value) and value >= 0:
            counts[source] = int(value)
    return counts


@router.get("/api/admin/referrals")
def referral_stats(context_id: int = Depends(require_admin)):
    if not feature_enabled_for("referral_stats", context_id):
        return {"visible": False}
    enabled = collection_enabled()
    base_url = os.environ.get("LOKI_URL", "").rstrip("/")
    result = {"visible": True, "enabled": enabled, "available": False, "rows": []}
    if not base_url:
        result["message"] = "Log storage is not configured."
        return result
    cached = cache_get_json("referrals:counts:v1")
    if isinstance(cached, list):
        return {**result, "available": True, "rows": cached}
    try:
        with httpx.Client(timeout=5) as client:
            namespace = os.environ.get("POD_NAMESPACE", "production")
            week = _query_counts(client, base_url, namespace, 7)
            month = _query_counts(client, base_url, namespace, 30)
        rows = [{"source": source, "label": label,
                 "last_7d": week.get(source, 0), "last_30d": month.get(source, 0)}
                for source, label in SOURCES.items()]
        rows.sort(key=lambda row: -row["last_30d"])
        cache_set_json("referrals:counts:v1", rows, ttl=300)
        return {**result, "available": True, "rows": rows}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
        log.warning("Referral log query unavailable", exc_info=False)
        return {**result, "message": "Log storage is temporarily unavailable."}
