# Local latency baseline — 2026-09-16

Five sequential fresh-browser/repeat-visit pairs per page, desktop Chromium, diagnostic mode.
Local Docker server and browser on the same host; SQLite, Redis disabled (observed bypasses).
Reserved synthetic account: empty Reactions dashboard and Manufacturing queue of ten Rifters.
This is harness validation, **not production-user performance or a capacity estimate**.

| Primary display | Fresh browser p50 / p95 | Repeat visit p50 / p95 |
| --- | --- | --- |
| Reactions | 270 / 335 ms | 219 / 230 ms |
| Manufacturing | 186 / 1,332 ms | 128 / 132 ms |

Every repeat visit used 30 browser-cached resources. All five Manufacturing repeat visits
rendered a localStorage-cached plan; the initial server-result paint followed at p50 136 ms,
p95 149 ms. No JavaScript/request errors or observed >50ms main-thread long tasks. With only five
samples, nearest-rank p95 is the maximum, not a stable tail-latency estimate.

The first Manufacturing queue-plan request took 1,151 ms in the browser, with 1,150 ms measured
backend response-header time. Instrumented cumulative work included 645 ms in two ESI calls,
218 ms fetching/converting DB rows, 55 ms executing DB operations, 53 ms acquiring DB connections,
and 45 ms fetching market data. These categories overlap and **must not be added** into a wall-time
pie chart. Later fresh-browser navigations were faster because server state was retained.

This demonstrates why “fresh browser” must not be called “cold cache”: process/DB/upstream caches
were not reset, and Redis was not available in this local run. Redis outcomes are covered by
isolated contract tests, but deployed Redis latency still needs a representative environment run.

Reproduce and inspect raw per-request distributions using [the benchmark guide](latency-benchmark.md).
