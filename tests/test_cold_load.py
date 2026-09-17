"""Cold market reads overlap without changing pricing precedence or cache isolation."""
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, '.')
from app import cache, latency, markets
from app.reactions import jobs


def market(location, kind='structure'):
    return {'location_id': location, 'kind': kind, 'name': str(location)}


class ParallelBooksTests(unittest.TestCase):
    def test_overlap_preserves_context_priority_and_two_sided_prices(self):
        barrier = threading.Barrier(3)
        scope = latency.Measurements()
        token = latency._current.set(scope)

        def fetch(ctx, location):
            self.assertEqual(ctx, 7)
            self.assertIs(latency._current.get(), scope)
            barrier.wait(timeout=3)  # Serial execution cannot pass this test.
            latency.count('book_test')
            return {34: {'sell_price': 0 if location == 1 else location * 10,
                         'buy_price': location, 'buy_volume': 1, 'sell_volume': 1}}

        try:
            with patch.object(markets, 'effective_markets', return_value=[market(i) for i in [1, 2, 3]]), \
                    patch.object(markets, 'fetch_structure_market', side_effect=fetch), \
                    patch.object(markets, 'fetch_market_data') as jita:
                result = markets.resolve_market_data(7, [34])[34]
                self.assertEqual((result['sell_price'], result['source']), (20, '2'))
                self.assertEqual((result['buy_price'], result['buy_source']), (1, '1'))
                jita.assert_not_called()
                self.assertEqual(scope.values['book_test'][1], 3)
        finally:
            latency._current.reset(token)

    def test_region_boundary_and_stop_after_quotes_resolved(self):
        markets_list = [market(1, 'region'), market(2), market(3)]
        with patch.object(markets, 'effective_markets', return_value=markets_list), \
                patch.object(markets, 'fetch_region_market', return_value={34: {'sell_price': 9, 'buy_price': 8}}), \
                patch.object(markets, 'fetch_structure_market') as structure:
            self.assertEqual(markets.resolve_market_data(7, [34])[34]['source'], '1')
            structure.assert_not_called()

    def test_duplicate_structures_fetch_once_per_batch(self):
        with patch.object(markets, 'fetch_structure_market', return_value={}) as fetch:
            rows = list(markets._market_books(7, [market(1), market(1), market(2)], lambda: [34]))
            self.assertEqual(len(rows), 3)
            self.assertEqual(fetch.call_count, 2)

    def test_concurrent_misses_share_a_book(self):
        store = {}
        started, release = threading.Event(), threading.Event()

        def fetch(*_):
            started.set()
            self.assertTrue(release.wait(3))
            return []

        with patch.object(markets, 'cache_get_json', side_effect=lambda key: store.get(key)), \
                patch.object(markets, 'cache_set_json', side_effect=lambda k, v, ttl: store.__setitem__(k, v)), \
                patch.object(markets, '_fetch_structure_orders', side_effect=fetch) as upstream:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(markets.fetch_structure_market, 7, 100)
                self.assertTrue(started.wait(3))
                second = pool.submit(markets.fetch_structure_market, 7, 100)
                release.set()
                self.assertEqual(first.result(), {})
                self.assertEqual(second.result(), {})
            upstream.assert_called_once()

    def test_request_reuses_book_without_another_redis_read(self):
        token = cache._REQUEST_MEMO.set({})
        try:
            with patch.object(markets, '_locked_structure_market', return_value={34: {}}) as read:
                markets.fetch_structure_market(7, 1)
                markets.fetch_structure_market(7, 1)
                markets.fetch_structure_market(8, 1)
                self.assertEqual(read.call_count, 2)
        finally:
            cache._REQUEST_MEMO.reset(token)


class DashboardPhaseTests(unittest.TestCase):
    def test_partial_cache_cannot_satisfy_full_request(self):
        store = {}
        with patch.object(jobs, 'cache_get_json', side_effect=lambda k: store.get(k)), \
                patch.object(jobs, 'cache_set_json', side_effect=lambda k, v, ttl: store.__setitem__(k, v)), \
                patch.object(jobs, '_get_industry_jobs_uncached', side_effect=[
                    {'pricing_state': 'loading', 'pending_output_value': None},
                    {'pricing_state': 'ready', 'pending_output_value': 123}]) as build:
            self.assertEqual(jobs.get_industry_jobs(7, False)['pricing_state'], 'loading')
            self.assertEqual(jobs.get_industry_jobs(7, False)['pricing_state'], 'loading')
            self.assertEqual(jobs.get_industry_jobs(7)['pending_output_value'], 123)
            self.assertEqual(jobs.get_industry_jobs(7, False)['pending_output_value'], 123)
            self.assertEqual(build.call_count, 2)
            build.assert_any_call(7, include_prices=False)

    def test_invalidation_removes_both_phases(self):
        with patch.object(jobs, 'cache_invalidate') as invalidate, \
                patch('app.industry.status_cache.invalidate_status'):
            jobs._invalidate_dashboard_cache(7)
            self.assertEqual({call.args[0] for call in invalidate.call_args_list},
                             {'rx:dash:7', 'rx:dash:status:7'})


if __name__ == '__main__':
    unittest.main()
