"""Shared cache, market reads and PI matching regressions; no external requests or DB writes."""
import random
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, ".")

import httpx
from app import cache, market, markets
from app.planner_algo import _max_matching_slots
from app.planner_recommendations import _system_recommendations_impl


def response(data, page_count=1, status=200):
    return httpx.Response(status, json=data, headers={"X-Pages": str(page_count)},
                          request=httpx.Request("GET", "https://example.invalid/orders"))


class CacheTests(unittest.TestCase):
    def tearDown(self):
        cache._client.cache_clear()

    def test_recovers_after_first_connection_failure(self):
        client = Mock()
        client.ping.side_effect = ConnectionError("startup outage")
        client.get.side_effect = [ConnectionError("startup outage"), b'{"ok": true}']
        cache._client.cache_clear()
        with patch.object(cache, "REDIS_URL", "redis://test"), \
                patch("redis.Redis.from_url", return_value=client) as create, \
                patch.object(cache.log, "exception"):
            self.assertIsNone(cache.cache_get_json("key"))
            self.assertEqual(cache.cache_get_json("key"), {"ok": True})
            self.assertEqual(create.call_count, 1)

    def test_bad_cached_json_does_not_discard_other_hits(self):
        client = Mock()
        client.mget.return_value = [b'{"price": 7}', b'broken', None, b'0']
        with patch.object(cache, "_client", return_value=client), patch.object(cache.log, "exception"):
            self.assertEqual(cache.cache_mget_json(["good", "bad", "missing", "zero"]),
                             {"good": {"price": 7}, "zero": 0})

    def test_disabled_cache_never_constructs_a_client(self):
        cache._client.cache_clear()
        with patch.object(cache, "REDIS_URL", ""), patch("redis.Redis.from_url") as create:
            self.assertEqual(cache.cache_mget_json(["missing"]), {})
            self.assertIsNone(cache.cache_get_json("missing"))
            create.assert_not_called()


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = {}
        self.ttls = {}
        market._history_cache.clear()
        self.addCleanup(market._history_cache.clear)

        def put(items, ttl):
            self.store.update(items)
            self.ttls.update({k: ttl for k in items})

        for module in (market, markets):
            self.stack.enter_context(patch.object(module, "cache_mget_json",
                side_effect=lambda keys: {k: self.store[k] for k in keys if k in self.store}))
            self.stack.enter_context(patch.object(module, "cache_mset_json", side_effect=put))
        self.stack.enter_context(patch.object(markets.esi_http, "client"))

    def test_duplicate_prices_make_one_upstream_lookup_per_type(self):
        fetch = Mock(return_value={34: 2, 35: 3})
        self.assertEqual(market._cached_fetch([34, 34, 35], {}, "price:", fetch), {34: 2, 35: 3})
        fetch.assert_called_once_with([34, 35])

    def test_history_deduplicates_and_preserves_zero_and_failure_ttls(self):
        with patch.object(market, "_fetch_one_history", side_effect=lambda tid: 0 if tid == 34 else None) as fetch:
            self.assertEqual(market.fetch_daily_volume([34, 34, 35]), {34: 0})
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.ttls["mkt:hist:34"], market.HISTORY_CACHE_TTL)
            self.assertEqual(self.ttls["mkt:hist:35"], market.FAILURE_CACHE_TTL)
            market._history_cache.clear()
            self.assertEqual(market.fetch_daily_volume([34, 35]), {34: 0})
            self.assertEqual(fetch.call_count, 2)

    def test_history_error_response_is_missing_not_an_exception(self):
        with patch.object(markets.esi_http, "get", return_value=response({"error": "unavailable"}, status=503)):
            self.assertIsNone(market._fetch_one_history(34))

    def test_history_local_hit_outlives_price_ttl(self):
        market._history_cache[34] = (7.0, 100)
        with patch.object(market.time, "monotonic", return_value=100 + market.CACHE_TTL + 1), \
                patch.object(market, "_fetch_one_history") as fetch:
            self.assertEqual(market.fetch_daily_volume([34]), {34: 7.0})
            fetch.assert_not_called()

    def test_region_failure_does_not_discard_a_different_types_book(self):
        with patch.object(markets.esi_http, "get", side_effect=[
                response({}, status=503), response([{"price": 10, "volume_remain": 20}])]):
            result = markets.fetch_region_market(7, [34, 35])
            self.assertNotIn(34, result)
            self.assertEqual(result[35]["sell_price"], 10)

    def test_region_prices_include_every_page_and_deduplicate_types(self):
        pages = [response([{"price": 100, "volume_remain": 20}], page_count=2),
                 response([{"price": 10, "volume_remain": 20}])]
        with patch.object(markets.esi_http, "get", side_effect=pages) as get:
            result = markets.fetch_region_market(7, [34, 34])
            self.assertEqual(result[34]["sell_price"], 10)
            self.assertEqual(result[34]["sell_volume"], 40)
            self.assertEqual(get.call_count, 2)
            self.assertIn("page=2", get.call_args.args[0])
            self.assertEqual(markets.fetch_region_market(7, [34]), result)
            self.assertEqual(get.call_count, 2)

    def test_failed_later_page_never_publishes_a_partial_book(self):
        with patch.object(markets.esi_http, "get", side_effect=[
                response([{"price": 1, "volume_remain": 10}], page_count=2),
                response({"error": "unavailable"}, status=503)]) as get:
            self.assertEqual(markets.fetch_region_market(7, [34]), {})
            self.assertEqual(markets.fetch_region_market(7, [34]), {})
            self.assertEqual(get.call_count, 2)
            self.assertEqual(self.ttls["mkt:region:7:34"], markets._MARKET_FAILURE_TTL)

    def test_empty_region_is_a_successful_book(self):
        with patch.object(markets.esi_http, "get", return_value=response([])):
            result = markets.fetch_region_market(7, [34])
            self.assertEqual(result[34]["sell_volume"], 0)
            self.assertEqual(self.ttls["mkt:region:7:34"], markets.CACHE_TTL)

    def test_structure_discards_partial_book_too(self):
        with patch.object(markets, "_market_character", return_value={"character_id": 1}), \
                patch.object(markets, "_get_valid_token", return_value="test"), \
                patch.object(markets.log, "warning"), \
                patch.object(markets.esi_http, "get", side_effect=[
                    response([{"price": 1, "volume_remain": 10}], page_count=2),
                    response({}, status=503)]):
            self.assertIsNone(markets._fetch_structure_orders(1, 123))


class PlanetMatchingTests(unittest.TestCase):
    def test_matching_equals_exhaustive_search_on_small_candidate_sets(self):
        rng = random.Random(13)

        def exhaustive(slots, taken=frozenset()):
            if not slots:
                return 0
            best = exhaustive(slots[1:], taken)
            for planet in slots[0]:
                key = (planet["system"], planet["planet_num"])
                if key not in taken:
                    best = max(best, 1 + exhaustive(slots[1:], taken | {key}))
            return best

        planets = [{"system": s, "planet_num": i} for s in ("A", "B") for i in range(3)]
        for case in range(100):
            slots = [rng.choices(planets, k=rng.randrange(5)) for _ in range(rng.randrange(7))]
            with self.subTest(case=case):
                self.assertEqual(_max_matching_slots(slots), exhaustive(slots))

    def test_a_pinned_planet_is_not_available_to_another_slot(self):
        a = {"system": "A", "planet_num": 1}
        b = {"system": "A", "planet_num": 2}
        self.assertEqual(_max_matching_slots([[a], [a], [a, b]]), 2)
        self.assertEqual(_max_matching_slots([[a, b], [a]]), 2)


class RecommendationTests(unittest.TestCase):
    def test_low_quality_tails_do_not_change_depth_scores_or_rankings(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE pp_planets (system TEXT, constellation TEXT, planet_num INTEGER, "
                    "planet_type TEXT, aqueous_liquids REAL)")
        con.execute("CREATE TABLE system_geo (system TEXT, system_id INTEGER)")
        con.execute("CREATE TABLE system_jumps (system TEXT, neighbour TEXT)")
        for system, values in (("A", [100, 1, 1]), ("B", [80, 80, 80]), ("C", [70, 70, 70])):
            for pn, value in enumerate(values):
                con.execute("INSERT INTO pp_planets VALUES (?, 'const', ?, 'Barren', ?)",
                            (system, pn, value))
        before = [_system_recommendations_impl(["Aqueous Liquids"], con, preferred_systems=pref)
                  for pref in (1, 2, 3)]
        self.assertEqual(before[0][0]["systems_needed"], ["B"])
        self.assertEqual(before[0][0]["depth_score"], 80)
        for system in ("A", "B", "C"):
            con.executemany("INSERT INTO pp_planets VALUES (?, 'const', ?, 'Barren', 0.5)",
                            [(system, i) for i in range(3, 100)])
        after = [_system_recommendations_impl(["Aqueous Liquids"], con, preferred_systems=pref)
                 for pref in (1, 2, 3)]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
