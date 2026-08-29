#!/usr/bin/env python3
"""Manufacturing's short-lived status cache: isolation, copies and write invalidation wiring."""
import inspect
try: import _bootstrap  # noqa: F401
except ModuleNotFoundError: from tests import _bootstrap  # noqa: F401

from app.industry import status_cache as C


def main() -> int:
    failed = []

    def check(ok, label):
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failed.append(label)

    redis = {}
    real_get, real_set = C.cache_get_json, C.cache_set_json
    try:
        C.cache_get_json = lambda key: redis.get(key)
        C.cache_set_json = lambda key, value, ttl=300: redis.__setitem__(key, value)
        C.invalidate_status()

        opts = {"force_build": True, "force_build_ids": [57479]}
        payload = {"metrics": {"jobs": 31}, "schedule": {"waves": []}}
        C.set_status(7, opts, payload)
        hit = C.get_status(7, opts)
        check(hit == payload, "an identical Manufacturing request reuses its computed status")
        hit["metrics"]["jobs"] = 999
        check(C.get_status(7, opts)["metrics"]["jobs"] == 31,
              "callers receive a copy and cannot corrupt the cached plan")
        check(C.get_status(7, {**opts, "force_build": False}) is None,
              "request options are part of the cache key")

        C.set_status(8, opts, {"account": 8})
        C.invalidate_status(7)
        check(C.get_status(7, opts) is None, "a write invalidates every cached variant for its account")
        check(C.get_status(8, opts) == {"account": 8}, "invalidation never crosses accounts")

        from app.industry import orders, jobs, progress, sourcing
        endpoint = inspect.getsource(orders.queue_plan)
        check("get_status(ctx, cache_options)" in endpoint and "set_status(ctx, cache_options, res)" in endpoint,
              "the expensive queue status endpoint reads and fills the cache")
        writers = "\n".join((inspect.getsource(orders.create_order),
                              inspect.getsource(orders.reorder_orders),
                              inspect.getsource(orders.update_order),
                              inspect.getsource(orders.delete_order),
                              inspect.getsource(jobs.refresh_manufacturing_jobs),
                              inspect.getsource(progress.industry_mark_done),
                              inspect.getsource(sourcing.industry_order_sourcing_set),
                              inspect.getsource(sourcing.industry_order_sourcing_paste)))
        check(writers.count("invalidate_status(") >= 8,
              "queue, job, progress and sourcing writes all invalidate the status")
        from app.reactions import jobs as reaction_jobs
        check("invalidate_status(context_id)" in inspect.getsource(
                  reaction_jobs._invalidate_dashboard_cache),
              "reaction progress and handoff writes invalidate Manufacturing too")
    finally:
        C.cache_get_json, C.cache_set_json = real_get, real_set
        C.invalidate_status()

    if failed:
        print(f"\n{len(failed)} FAILED")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
