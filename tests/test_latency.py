"""Isolated diagnostic contracts: no real DB, Redis, server or secrets required."""
import asyncio
import os
import sqlite3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

sys.path.insert(0, ".")
from app import cache, db, latency, market
from app.industry.graph import sde as recipe_sde
from app.industry import status_cache


class RecipeCacheTests(unittest.TestCase):
    def tearDown(self):
        recipe_sde.clear_graph_cache()

    def test_concurrent_loads_are_coalesced(self):
        recipe_sde.clear_graph_cache()
        entered, release = Event(), Event()

        def build(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError('loader not released')
            return {1: {'inputs': []}}

        loader = Mock(side_effect=build)
        with ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(recipe_sde._cached_graph, 'mfg', None, loader)
            self.assertTrue(entered.wait(5))
            others = [pool.submit(recipe_sde._cached_graph, 'mfg', None, loader) for _ in range(7)]
            release.set()
            value = first.result()
            self.assertTrue(all(f.result() is value for f in others))
        loader.assert_called_once()

    def test_expiry_clear_and_failed_load(self):
        recipe_sde.clear_graph_cache()
        loader = Mock(side_effect=[{1: {}}, {2: {}}, RuntimeError('failed'), {3: {}}])
        with patch.object(recipe_sde.time, 'monotonic', return_value=100):
            self.assertEqual(recipe_sde._cached_graph('rx', None, loader), {1: {}})
        with patch.object(recipe_sde.time, 'monotonic', return_value=100 + recipe_sde._GRAPH_TTL):
            self.assertEqual(recipe_sde._cached_graph('rx', None, loader), {2: {}})
        recipe_sde.clear_graph_cache()
        with self.assertRaises(RuntimeError):
            recipe_sde._cached_graph('rx', None, loader)
        self.assertEqual(recipe_sde._cached_graph('rx', None, loader), {3: {}})

    def test_clear_cannot_be_undone_by_inflight_load(self):
        recipe_sde.clear_graph_cache()
        entered, release, clearing = Event(), Event(), Event()

        def build(_):
            entered.set()
            if not release.wait(5):
                raise AssertionError('loader not released')
            return {1: {}}

        def clear():
            clearing.set()
            recipe_sde.clear_graph_cache()

        with ThreadPoolExecutor(max_workers=2) as pool:
            load = pool.submit(recipe_sde._cached_graph, 'mfg', None, build)
            self.assertTrue(entered.wait(5))
            invalidation = pool.submit(clear)
            self.assertTrue(clearing.wait(5))
            release.set()
            load.result()
            invalidation.result()
        self.assertEqual(recipe_sde._GRAPH_CACHE, {})


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.measurements = latency.Measurements()
        self.token = latency._current.set(self.measurements)

    def tearDown(self):
        latency._current.reset(self.token)

    def test_sqlite_connection_and_cursor_paths(self):
        con = sqlite3.connect(':memory:', factory=db._TimedSQLiteConnection)
        try:
            con.execute('CREATE TABLE test (n INT)')
            con.executemany('INSERT INTO test VALUES (?)', [(1,), (2,)])
            self.assertEqual(con.execute('SELECT n FROM test').fetchall(), [(1,), (2,)])
            self.assertEqual(list(con.cursor().execute('SELECT n FROM test')), [(1,), (2,)])
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('SELECT secret FROM missing')
            con.commit()
            self.assertEqual(self.measurements.values['db_execute'][1], 5)
            self.assertGreaterEqual(self.measurements.values['db_fetch'][1], 3)
            self.assertEqual(self.measurements.values['db_commit'][1], 1)
            self.assertNotIn('secret', self.measurements.header(1))
        finally:
            con.close()

    def test_disabled_timer_does_not_read_clock(self):
        token = latency._current.set(None)
        try:
            with patch.object(latency, 'perf_counter', side_effect=AssertionError('unexpected timer')):
                self.assertEqual(latency.timed('work')(lambda: 7)(), 7)
                latency.count('memo_hit')
        finally:
            latency._current.reset(token)

    def test_recipe_and_plan_cache_counters(self):
        recipe_sde.clear_graph_cache()
        try:
            recipe_sde._cached_graph('mfg', None, lambda _: {})
            recipe_sde._cached_graph('mfg', None, lambda _: self.fail('unexpected reload'))
        finally:
            recipe_sde.clear_graph_cache()
        with patch.object(status_cache, '_LOCAL', {}), \
                patch.object(status_cache, '_key', return_value='test'), \
                patch.object(status_cache, 'cache_get_json', side_effect=[None, {'ok': True}]):
            self.assertIsNone(status_cache.get_status(1, {}))
            result = status_cache.get_status(1, {})
            result['ok'] = False
            self.assertEqual(status_cache.get_status(1, {}), {'ok': True})
        for name in ['recipe_graph_miss', 'recipe_graph_hit', 'recipe_graph_load',
                     'plan_miss', 'plan_redis_hit', 'plan_l1_hit']:
            self.assertEqual(self.measurements.values[name][1], 1, name)

    def test_postgres_counts_once_and_rolls_back_errors(self):
        raw = Mock()
        cursor = raw.cursor.return_value
        cursor.description = [('n',)]
        cursor.fetchall.return_value = [(7,)]
        con = db._PgConn(raw)
        self.assertEqual(con.execute('SELECT ? AS n', (7,)).fetchall()[0]['n'], 7)
        con.cursor().execute('SELECT 1')
        con.executemany('INSERT INTO t VALUES (?)', [(1,)])
        cursor.execute.side_effect = ValueError('failure')
        with self.assertRaises(ValueError):
            con.execute('bad')
        raw.rollback.assert_called_once()
        self.assertEqual(self.measurements.values['db_execute'][1], 4)

    def test_cache_outcomes_and_memo(self):
        client = Mock()
        client.get.side_effect = [b'0', None, ValueError('bad')]
        client.mget.return_value = [b'false', None, b'broken']
        with patch.object(cache, '_client', return_value=client), patch.object(cache.log, 'exception'):
            self.assertEqual(cache.cache_get_json('private-key'), 0)
            self.assertIsNone(cache.cache_get_json('private-key'))
            self.assertIsNone(cache.cache_get_json('private-key'))
            cache.cache_mget_json(['a', 'b', 'c'])
        with patch.object(cache, '_client', return_value=None):
            cache.cache_mget_json(['a', 'b'])
        memo_token = cache._REQUEST_MEMO.set({})
        try:
            self.assertEqual(cache.request_memo('secret', lambda: 9), 9)
            self.assertEqual(cache.request_memo('secret', lambda: 10), 9)
        finally:
            cache._REQUEST_MEMO.reset(memo_token)
        for name, count in [('redis_hit', 2), ('redis_miss', 2), ('redis_error', 2),
                            ('redis_bypass', 2), ('memo_hit', 1), ('memo_miss', 1)]:
            self.assertEqual(self.measurements.values[name][1], count, name)
        self.assertNotIn('secret', self.measurements.header(1))
        self.assertNotIn('private-key', self.measurements.header(1))

    def test_market_warm_and_negative_cache(self):
        local = {}
        fetch = Mock(return_value={1: {'buy': 2}})
        with patch.object(market, 'cache_mget_json', return_value={}), \
                patch.object(market, 'cache_mset_json'):
            market._cached_fetch([1, 2], local, 'test:', fetch)
            market._cached_fetch([1, 2], local, 'test:', fetch)
        fetch.assert_called_once()
        for name, count in [('market_l1_hit', 1), ('market_l1_negative_hit', 1),
                            ('market_l1_miss', 2), ('market_upstream', 1)]:
            self.assertEqual(self.measurements.values[name][1], count)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, supplied=b'', fail=False):
        sent = []

        @latency.timed('work')
        def work():
            latency.count('memo_hit')

        async def app(scope, receive, send):
            await asyncio.to_thread(work)
            await asyncio.sleep(0)
            if fail:
                raise ValueError('failure')
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'cache-control', b'public'), (b'etag', b'abc')]})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async def send(message):
            sent.append(message)

        await latency.LatencyMiddleware(app)(
            {'type': 'http', 'headers': [(b'x-latency-token', supplied)]}, None, send)
        self.assertIsNone(latency._current.get())
        return dict(sent[0]['headers'])

    async def test_default_off_and_wrong_token(self):
        with patch.dict(os.environ, {'LATENCY_TOKEN': ''}):
            self.assertNotIn(b'server-timing', await self.request(b'anything'))
        with patch.dict(os.environ, {'LATENCY_TOKEN': 'secret'}):
            headers = await self.request(b'wrong')
            self.assertNotIn(b'server-timing', headers)
            self.assertEqual(headers[b'cache-control'], b'public')

    async def test_concurrent_requests_and_worker_context(self):
        with patch.dict(os.environ, {'LATENCY_TOKEN': 'secret'}):
            results = await asyncio.gather(*(self.request(b'secret') for _ in range(10)))
        for headers in results:
            self.assertIn(b'memo_hit;dur=0.000;desc="1"', headers[b'server-timing'])
            self.assertIn(b'backend;dur=', headers[b'server-timing'])
            self.assertEqual(headers[b'cache-control'], b'private, no-store')
            self.assertEqual(headers[b'etag'], b'abc')
            self.assertNotIn(b'secret', headers[b'server-timing'])

    async def test_exception_resets_context(self):
        with patch.dict(os.environ, {'LATENCY_TOKEN': 'secret'}):
            with self.assertRaises(ValueError):
                await self.request(b'secret', fail=True)
        self.assertIsNone(latency._current.get())


if __name__ == '__main__':
    unittest.main()
