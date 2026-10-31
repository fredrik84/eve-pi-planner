"""Private numeric profiling: no SQL text, cache keys, URLs, tokens, or visitor identifiers."""
import json
import logging
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import cache, db
from app.features import feature_enabled

router = APIRouter()
log = logging.getLogger(__name__)


def profiling_enabled():
    try:
        value = cache.cache_get_json("profiling:enabled:v1")
        if isinstance(value, bool):
            return value
        value = feature_enabled("page_profiling")
        cache.cache_set_json("profiling:enabled:v1", value, ttl=30)
        return value
    except Exception:
        return False  # Observability must not affect the site when its dependencies fail.


class PageLoad(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Literal["dashboard", "analyze", "planetary", "planner", "layout", "planetdb",
                  "contribute", "howitworks", "reactions", "industry", "indhowitworks", "admin"]
    load_ms: float = Field(ge=0, le=300_000, allow_inf_nan=False)
    dom_ms: float = Field(ge=0, le=300_000, allow_inf_nan=False)
    ttfb_ms: float = Field(ge=0, le=300_000, allow_inf_nan=False)


@router.post("/api/telemetry/page-load", status_code=204, response_class=Response)
def page_load(req: PageLoad, request: Request):
    # No account/session lookup. The only accepted dimensions are fixed page names.
    from app.referrals import _BOT, access_log
    if (not profiling_enabled() or request.headers.get("dnt") == "1"
            or request.headers.get("sec-gpc") == "1"
            or _BOT.search(request.headers.get("user-agent", ""))):
        return Response(status_code=204)
    origin = request.headers.get("origin")
    if origin:
        try:
            same_origin = urlsplit(origin).netloc == request.url.netloc
        except ValueError:
            same_origin = False
        if not same_origin:
            return Response(status_code=403)
    # Component durations are part of the complete load, not independent clocks.
    if req.dom_ms > req.load_ms or req.ttfb_ms > req.load_ms:
        return Response(status_code=422)
    access_log.info(json.dumps({"event": "page_load", **req.model_dump()}, separators=(",", ":")))
    return Response(status_code=204)


# name -> (help, kind, source-field). All values come from aggregate system counters.
DATABASE_METRICS = {
    "connections": ("Current database connections", "gauge", "numbackends"),
    "transactions_committed_total": ("Committed database transactions", "counter", "xact_commit"),
    "transactions_rolled_back_total": ("Rolled-back database transactions", "counter", "xact_rollback"),
    "blocks_hit_total": ("Database shared-buffer cache hits", "counter", "blks_hit"),
    "blocks_read_total": ("Blocks read into shared buffers; may be served by the OS cache", "counter", "blks_read"),
    "tuples_returned_total": ("Database rows scanned", "counter", "tup_returned"),
    "tuples_fetched_total": ("Database rows fetched", "counter", "tup_fetched"),
    "tuples_inserted_total": ("Database rows inserted", "counter", "tup_inserted"),
    "tuples_updated_total": ("Database rows updated", "counter", "tup_updated"),
    "tuples_deleted_total": ("Database rows deleted", "counter", "tup_deleted"),
    "temporary_bytes_total": ("Bytes written to database temporary files", "counter", "temp_bytes"),
    "deadlocks_total": ("Database deadlocks", "counter", "deadlocks"),
}
REDIS_METRICS = {
    "keyspace_hits_total": ("Redis server cache hits, including internal lookups", "counter", "keyspace_hits"),
    "keyspace_misses_total": ("Redis server cache misses", "counter", "keyspace_misses"),
    "evicted_keys_total": ("Redis keys evicted", "counter", "evicted_keys"),
    "expired_keys_total": ("Redis keys expired", "counter", "expired_keys"),
    "commands_processed_total": ("Redis commands processed", "counter", "total_commands_processed"),
    "used_memory_bytes": ("Redis allocated memory", "gauge", "used_memory"),
    "used_memory_peak_bytes": ("Redis peak allocated memory", "gauge", "used_memory_peak"),
    "connected_clients": ("Redis connected clients", "gauge", "connected_clients"),
}


def system_metrics():
    """Cache low-cost read-only PostgreSQL/Redis counters for one minute per environment."""
    if not profiling_enabled():
        return {}
    cached = cache.cache_get_json("profiling:system:v1")
    if isinstance(cached, dict):
        return cached
    metrics = {}

    def put(name, value, help_text, kind="gauge"):
        metrics[name] = {"value": float(value or 0), "help": help_text, "type": kind}

    if db._IS_POSTGRES:
        try:
            with db.get_connection() as con:
                stats = con.execute(
                    "SELECT numbackends, xact_commit, xact_rollback, blks_hit, blks_read, "
                    "tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted, temp_bytes, deadlocks "
                    "FROM pg_stat_database WHERE datname=current_database()"
                ).fetchone()
                sizes = con.execute(
                    "SELECT pg_database_size(current_database()) AS bytes, "
                    "current_setting('max_connections')::int AS max_connections"
                ).fetchone()
                activity = con.execute(
                    "SELECT COUNT(*) FILTER (WHERE state='active') AS active, "
                    "COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_in_transaction "
                    "FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
                ).fetchone()
            for suffix, (help_text, kind, field) in DATABASE_METRICS.items():
                put("evepi_database_" + suffix, stats[field], help_text, kind)
            put("evepi_database_size_bytes", sizes["bytes"], "Current database size on disk")
            put("evepi_database_max_connections", sizes["max_connections"], "Server connection limit")
            put("evepi_database_active_connections", activity["active"], "Other active database connections")
            put("evepi_database_idle_in_transaction_connections", activity["idle_in_transaction"], "Idle-in-transaction connections")
            put("evepi_database_available", 1, "Whether database statistics could be read")
        except Exception:
            log.warning("Database profiling counters unavailable", exc_info=False)
            put("evepi_database_available", 0, "Whether database statistics could be read")
    try:
        client = cache._client()
        if client is not None:
            info = {}
            for section in ("stats", "memory", "clients"):
                info.update(client.info(section))
            for suffix, (help_text, kind, field) in REDIS_METRICS.items():
                put("evepi_redis_" + suffix, info.get(field), help_text, kind)
            keyspace = client.info("keyspace")
            put("evepi_redis_keys", sum(v.get("keys", 0) for v in keyspace.values() if isinstance(v, dict)), "Redis stored keys")
            put("evepi_redis_available", 1, "Whether Redis statistics could be read")
        else:
            put("evepi_redis_available", 0, "Whether Redis statistics could be read")
    except Exception:
        log.warning("Redis profiling counters unavailable", exc_info=False)
        put("evepi_redis_available", 0, "Whether Redis statistics could be read")
    cache.cache_set_json("profiling:system:v1", metrics, ttl=60)
    return metrics
