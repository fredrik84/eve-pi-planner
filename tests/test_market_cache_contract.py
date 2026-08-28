#!/usr/bin/env python3
"""Market cache hits, including upstream failures, must make zero upstream calls."""
import sys

sys.path.insert(0, ".")


def check(cond, message):
    print(("  ok   " if cond else "  FAIL ") + message)
    if not cond:
        raise AssertionError(message)


def test_fuzzwork_failure_is_negative_cached():
    import app.market as market

    saved_mget, saved_mset = market.cache_mget_json, market.cache_mset_json
    store, upstream_calls = {}, []
    try:
        market._cache.clear()
        market.cache_mget_json = lambda keys: {k: store[k] for k in keys if k in store}
        market.cache_mset_json = lambda values, ttl: store.update(values)

        def unavailable(type_ids):
            upstream_calls.append(list(type_ids))
            return {}

        check(market._cached_fetch([34], market._cache, "test:sell:", unavailable) == {},
              "failed lookup falls back without a price")
        market._cache.clear()  # simulate the next request landing on another worker
        check(market._cached_fetch([34], market._cache, "test:sell:", unavailable) == {},
              "shared negative cache returns the same fallback")
        check(upstream_calls == [[34]], "cross-worker repeat calls Fuzzwork only once")
    finally:
        market.cache_mget_json, market.cache_mset_json = saved_mget, saved_mset
        market._cache.clear()


def test_structure_failure_is_negative_cached():
    import app.markets as markets

    saved = markets.cache_get_json, markets.cache_set_json, markets._fetch_structure_orders
    store, upstream_calls = {}, []
    try:
        markets.cache_get_json = lambda key: store.get(key)
        markets.cache_set_json = lambda key, value, ttl: store.__setitem__(key, value)

        def unreadable(context_id, structure_id):
            upstream_calls.append((context_id, structure_id))
            return None

        markets._fetch_structure_orders = unreadable
        check(markets.fetch_structure_market(7, 1028858195912) == {},
              "unreadable structure falls through without prices")
        check(markets.fetch_structure_market(7, 1028858195912) == {},
              "negative cache returns the same fallback")
        check(upstream_calls == [(7, 1028858195912)], "repeated lookup calls ESI only once")
        markets.fetch_structure_market(8, 1028858195912)
        check(upstream_calls[-1] == (8, 1028858195912),
              "one account's access failure does not poison another account")
    finally:
        markets.cache_get_json, markets.cache_set_json, markets._fetch_structure_orders = saved


def test_history_failure_is_negative_cached():
    import app.market as market

    saved = market.cache_mget_json, market.cache_mset_json, market._fetch_one_history
    store, upstream_calls = {}, []
    try:
        market._history_cache.clear()
        market.cache_mget_json = lambda keys: {k: store[k] for k in keys if k in store}
        market.cache_mset_json = lambda values, ttl: store.update(values)

        def unavailable(type_id):
            upstream_calls.append(type_id)
            return None

        market._fetch_one_history = unavailable
        check(market.fetch_daily_volume([34]) == {}, "failed history lookup has no fake volume")
        market._history_cache.clear()  # another worker
        check(market.fetch_daily_volume([34]) == {}, "shared history failure cache is used")
        check(upstream_calls == [34], "cross-worker repeat calls history ESI only once")
    finally:
        market.cache_mget_json, market.cache_mset_json, market._fetch_one_history = saved
        market._history_cache.clear()


def test_warm_market_caches_never_call_upstream():
    import app.market as market
    import app.markets as markets

    saved_market = market.cache_mget_json, market.cache_mset_json
    saved_structure = markets.cache_get_json, markets._fetch_structure_orders
    try:
        market._market_cache.clear()
        market.cache_mget_json = lambda keys: {
            "test:data:34": {"buy_price": 4.0, "sell_price": 5.0}}
        market.cache_mset_json = lambda values, ttl: None

        def forbidden_fuzzwork(_):
            raise AssertionError("warm Fuzzwork cache contacted upstream")

        got = market._cached_fetch([34], market._market_cache, "test:data:", forbidden_fuzzwork)
        check(got[34]["sell_price"] == 5.0, "warm shared Fuzzwork cache supplies market data")

        markets.cache_get_json = lambda key: {
            "34": {"buy_price": 4.0, "sell_price": 5.0}}

        def forbidden_esi(*_):
            raise AssertionError("warm structure cache contacted ESI")

        markets._fetch_structure_orders = forbidden_esi
        got = markets.fetch_structure_market(7, 1028858195912)
        check(got[34]["sell_price"] == 5.0, "warm structure cache supplies market data")
    finally:
        market.cache_mget_json, market.cache_mset_json = saved_market
        market._market_cache.clear()
        markets.cache_get_json, markets._fetch_structure_orders = saved_structure


if __name__ == "__main__":
    test_fuzzwork_failure_is_negative_cached()
    test_structure_failure_is_negative_cached()
    test_history_failure_is_negative_cached()
    test_warm_market_caches_never_call_upstream()
    print("all checks passed")
