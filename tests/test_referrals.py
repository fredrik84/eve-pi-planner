"""Referral attribution uses coarse categories; no visitor IDs, URLs, or query terms persist."""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import referrals
from app.main import app


class ReferralEventTests(unittest.TestCase):
    def test_classification_is_domain_based_and_handles_regional_search(self):
        cases = {
            "": "direct_unknown", "https://www.google.com/search?q=secret": "google",
            "https://www.google.co.uk/": "google", "https://google.se/": "google",
            "https://google.com.evil.example/": "other", "https://evilgoogle.com/": "other",
            "https://docs.google.com/": "other", "https://www.bing.com/": "bing",
            "https://duckduckgo.com/": "duckduckgo", "https://search.yahoo.com/": "yahoo",
            "https://search.brave.com/": "brave", "https://yandex.ru/": "yandex",
            "https://www.baidu.com/": "baidu", "https://old.reddit.com/r/Eve": "reddit",
            "https://discord.com/channels/private": "discord",
            "https://forums.eveonline.com/t/topic": "eve_forums",
            "https://eveindustry.net/planner": "internal",
            "https://www.eveindustry.net/": "internal",
            "https://dev.eveindustry.net/": "internal",
            "https://testserver/": "internal", "javascript:alert(1)": "direct_unknown",
            "https://[broken/": "direct_unknown", "x" * 5000: "direct_unknown",
        }
        for value, expected in cases.items():
            with self.subTest(value=value[:80]):
                self.assertEqual(referrals.classify_referrer(value, "testserver"), expected)

    def test_page_events_and_privacy_controls_through_real_middleware(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        headers = {"referer": "https://www.google.com/search?q=private-search",
                   "user-agent": "Mozilla/5.0", "cookie": "secret=private-cookie"}
        with patch.object(referrals, "collection_enabled", return_value=True), \
                patch.object(referrals.access_log, "info") as emit:
            response = client.get("/", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
            event = json.loads(emit.call_args.args[0])
            self.assertEqual(set(event), {"event", "method", "route", "status", "duration_ms", "kind", "source", "bot"})
            self.assertEqual((event["source"], event["kind"], event["bot"]), ("google", "page", False))
            self.assertNotIn("private", emit.call_args.args[0])
            for control in ({"DNT": "1"}, {"Sec-GPC": "1"}, {"Sec-Purpose": "prefetch"}, {"Purpose": "prefetch"}):
                client.get("/", headers={**headers, **control})
                self.assertEqual(json.loads(emit.call_args.args[0])["kind"], "request")
            client.get("/", headers={**headers, "user-agent": "Googlebot"})
            self.assertTrue(json.loads(emit.call_args.args[0])["bot"])
            client.get("/manufacturing/order/private-record?token=private-token", headers=headers)
            event = json.loads(emit.call_args.args[0])
            self.assertEqual(event["route"], "/manufacturing/order/{record_id}")
            self.assertNotIn("private", emit.call_args.args[0])

    def test_collection_off_or_failure_cannot_break_the_site(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch.object(referrals, "collection_enabled", return_value=False), \
                patch.object(referrals.access_log, "info") as emit:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(json.loads(emit.call_args.args[0])["kind"], "request")
        with patch.object(referrals, "collection_enabled", side_effect=RuntimeError("unavailable")):
            self.assertEqual(client.get("/").status_code, 200)


class ReferralQueryTests(unittest.TestCase):
    def test_real_query_scopes_namespace_and_filters_bots_and_internal_requests(self):
        def reply(request):
            query = request.url.params["query"]
            for required in ('namespace="dev"', '| bot="false"', '| kind="page"', '| source!="internal"', '[30d]'):
                self.assertIn(required, query)
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"source": "google"}, "value": [0, "12"]},
                {"metric": {"source": "private-hostname"}, "value": [0, "99"]},
            ]}})
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            self.assertEqual(referrals._query_counts(client, "http://logs", "dev", 30), {"google": 12})

    def test_admin_endpoint_access_visibility_and_failure_states(self):
        api = FastAPI()
        api.include_router(referrals.router)
        client = TestClient(api)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/admin/referrals").status_code, 403)
        api.dependency_overrides[referrals.require_admin] = lambda: 123
        with patch.object(referrals, "feature_enabled_for", return_value=False), \
                patch.object(referrals, "_query_counts") as query:
            self.assertEqual(client.get("/api/admin/referrals").json(), {"visible": False})
            query.assert_not_called()
        with patch.object(referrals, "feature_enabled_for", return_value=True), \
                patch.object(referrals, "collection_enabled", return_value=True), \
                patch.object(referrals, "cache_get_json", return_value=None), \
                patch.object(referrals, "cache_set_json"), \
                patch.dict(os.environ, {"LOKI_URL": "http://logs", "POD_NAMESPACE": "production"}), \
                patch.object(referrals, "_query_counts", side_effect=[{"google": 2}, {"google": 10}]) as query:
            data = client.get("/api/admin/referrals").json()
            self.assertTrue(data["available"])
            self.assertEqual(data["rows"][0], {"source": "google", "label": "Google", "last_7d": 2, "last_30d": 10})
            self.assertEqual(query.call_count, 2)
            query.side_effect = httpx.ConnectError("private-infrastructure-details")
            data = client.get("/api/admin/referrals").json()
            self.assertFalse(data["available"])
            self.assertNotIn("private", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
