"""Private profiling contracts: real middleware, bounded browser events, aggregate counters."""
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, ".")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import admin, cache, db, latency, profiling, referrals


class ProfilingTests(unittest.TestCase):
    def test_real_request_records_operation_counts_but_never_queries_keys_or_headers(self):
        app = FastAPI()
        app.add_middleware(latency.LatencyMiddleware)

        @app.middleware("http")
        async def log_request(request, call_next):
            response = await call_next(request)
            referrals.note_access(request, response, 12)
            return response

        @app.get("/api/sample/{record_id}")
        def sample(record_id: str):
            con = sqlite3.connect(":memory:", factory=db._TimedSQLiteConnection)
            try:
                con.execute("SELECT 'private-sql-text'").fetchone()
            finally:
                con.close()
            cache.cache_get_json("private-cache-key")
            return {"ok": True}

        redis = Mock()
        redis.get.return_value = None
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch.object(profiling, "profiling_enabled", return_value=True), \
                patch.object(cache, "_client", return_value=redis), \
                patch.object(referrals.access_log, "info") as emit, \
                patch.dict(os.environ, {"LATENCY_TOKEN": ""}):
            response = client.get("/api/sample/private-record?token=private-token")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("server-timing", response.headers)
        event = json.loads(emit.call_args.args[0])
        self.assertTrue(event["profiled"])
        self.assertEqual(event["db_execute_count"], 1)
        self.assertEqual(event["redis_miss"], 1)
        self.assertEqual(event["route"], "/api/sample/{record_id}")
        self.assertGreaterEqual(event["db_execute_ms"], 0)
        self.assertNotIn("private", emit.call_args.args[0])
        self.assertIsNone(latency._current.get())

    def test_profile_fields_are_allowlisted_even_if_a_timer_name_is_sensitive(self):
        m = latency.Measurements()
        m.add("private-key", 1, 2)
        m.add("db_execute", .012, 3)
        fields = m.profile_fields()
        self.assertEqual(fields["db_execute_count"], 3)
        self.assertEqual(fields["db_execute_ms"], 12)
        self.assertNotIn("private-key", fields)

    def test_browser_event_validates_privacy_and_honours_opt_outs(self):
        app = FastAPI()
        app.include_router(profiling.router)
        client = TestClient(app)
        self.addCleanup(client.close)
        payload = {"page": "industry", "load_ms": 1200, "dom_ms": 800, "ttfb_ms": 90}
        with patch.object(profiling, "profiling_enabled", return_value=True), \
                patch.object(referrals.access_log, "info") as emit:
            self.assertEqual(client.post("/api/telemetry/page-load", json=payload,
                                         headers={"Origin": "http://testserver"}).status_code, 204)
            event = json.loads(emit.call_args.args[0])
            self.assertEqual(event, {"event": "page_load", **payload})
            emit.reset_mock()
            for header in ({"DNT": "1"}, {"Sec-GPC": "1"}, {"user-agent": "Googlebot"}):
                self.assertEqual(client.post("/api/telemetry/page-load", json=payload, headers=header).status_code, 204)
                emit.assert_not_called()
            self.assertEqual(client.post("/api/telemetry/page-load", json=payload,
                                         headers={"Origin": "https://other.example"}).status_code, 403)
            for bad in ({**payload, "page": "/private-record/123"}, {**payload, "url": "https://private"},
                        {**payload, "load_ms": -1}, {**payload, "load_ms": 300001},
                        {**payload, "dom_ms": 1300}):
                self.assertEqual(client.post("/api/telemetry/page-load", json=bad).status_code, 422)
                emit.assert_not_called()
        with patch.object(profiling, "profiling_enabled", return_value=False), \
                patch.object(referrals.access_log, "info") as emit:
            self.assertEqual(client.post("/api/telemetry/page-load", json=payload).status_code, 204)
            emit.assert_not_called()

    def test_database_and_cache_stats_are_numeric_aggregate_and_cached(self):
        con = Mock()
        con.__enter__ = Mock(return_value=con)
        con.__exit__ = Mock(return_value=False)
        stats = {info[2]: 10 for info in profiling.DATABASE_METRICS.values()}
        con.execute.return_value.fetchone.side_effect = [stats, {"bytes": 1000, "max_connections": 100},
                                                       {"active": 2, "idle_in_transaction": 1}]
        redis = Mock()
        redis.info.side_effect = [{"keyspace_hits": 20, "keyspace_misses": 5}, {"used_memory": 500},
                                 {"connected_clients": 3}, {"db0": {"keys": 7}}]
        store = {}
        with patch.object(profiling, "profiling_enabled", return_value=True), \
                patch.object(db, "_IS_POSTGRES", True), patch.object(db, "get_connection", return_value=con), \
                patch.object(cache, "_client", return_value=redis), \
                patch.object(cache, "cache_get_json", side_effect=lambda key: store.get(key)), \
                patch.object(cache, "cache_set_json", side_effect=lambda key, value, ttl: store.__setitem__(key, value)):
            result = profiling.system_metrics()
            self.assertEqual(profiling.system_metrics(), result)
        self.assertEqual(con.execute.call_count, 3)
        self.assertEqual(redis.info.call_count, 4)
        self.assertEqual(result["evepi_database_connections"]["value"], 10)
        self.assertEqual(result["evepi_redis_keyspace_hits_total"]["value"], 20)
        self.assertEqual(result["evepi_redis_keys"]["value"], 7)
        self.assertTrue(all(isinstance(v["value"], float) for v in result.values()))
        self.assertEqual(result["evepi_database_transactions_committed_total"]["type"], "counter")
        self.assertNotIn("query", str(con.execute.call_args_list).lower())

    def test_metrics_remain_token_protected_and_dependency_failures_are_isolated(self):
        app = FastAPI()
        app.include_router(admin.router)
        client = TestClient(app)
        self.addCleanup(client.close)
        values = {key: 1 for key in admin._STATS_TO_METRIC}
        with patch.object(admin, "_PROMETHEUS_ENABLED", True), patch.object(admin, "_PROMETHEUS_TOKEN", "test-token"), \
                patch.object(admin, "admin_stats", return_value=values), \
                patch.object(profiling, "system_metrics", return_value={"evepi_database_connections": {
                    "help": "Connections", "type": "gauge", "value": 5}}) as collect:
            self.assertEqual(client.get("/metrics").status_code, 401)
            self.assertEqual(client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            collect.assert_not_called()
            response = client.get("/metrics", headers={"Authorization": "Bearer test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("evepi_database_connections 5", response.text)
        with patch.object(profiling, "profiling_enabled", return_value=True), \
                patch.object(cache, "cache_get_json", return_value=None), patch.object(cache, "cache_set_json"), \
                patch.object(db, "_IS_POSTGRES", True), patch.object(db, "get_connection", side_effect=RuntimeError("private-secret")), \
                patch.object(cache, "_client", side_effect=RuntimeError("private-secret")):
            result = profiling.system_metrics()
        self.assertEqual(result["evepi_database_available"]["value"], 0)
        self.assertEqual(result["evepi_redis_available"]["value"], 0)
        self.assertNotIn("private-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
