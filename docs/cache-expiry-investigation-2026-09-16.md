# Cache expiry investigation — 2026-09-16

## Scope and changes

Local synthetic account, SQLite, a private disposable Redis, desktop Chromium on the same host.
Manufacturing contains ten Rifters; Reactions is an empty dashboard. This does not represent a
large production queue, Postgres latency, multi-worker contention or a production latency SLO.

- Added plan L1/Redis hit and miss counters, plus recipe graph hit/miss/load attribution.
- Added an optional 25-second revisit to exercise the existing 20-second plan-cache expiry.
- Added isolated Redis to the local benchmark; no shared Redis is flushed.
- Coalesced simultaneous static recipe graph loads within each process. Invalidation takes the
  same lock, so an in-flight load cannot undo a completed clear. Failed builds remain retryable;
  TTL now uses a monotonic clock and begins when construction finishes.

The graph coalescing change is a concurrency optimization, not evidence of a single-user speedup.
Tests cover eight competing readers sharing one build, expiry, failed-build retry, and clear
during a build. Existing 15-minute graph and 20-second plan TTLs remain unchanged.

## Initial expiry probe

| Manufacturing queue-plan | Browser request | Backend | Observed cache state |
| --- | --- | --- | --- |
| First load | 1,106 ms | 1,104 ms | Plan miss, two recipe graph misses |
| Immediate revisit | 14 ms | 12 ms | Plan L1 hit |
| Revisit after 25 seconds | 88 ms | 86 ms | Plan miss, two graph hits, five market L1 hits |

The first request recorded 269 ms of recipe graph construction and 598 ms in two ESI requests.
After expiry, neither appeared as repeated construction/network work; the plan could be recomputed
with cached ingredients. Database execute calls fell from 340 to 238, and fetch operations from
37,545 to 571. Counts include SQLite setup PRAGMAs and per-row iteration, **not just independent
business queries**. Timing categories overlap; never sum them as wall-time components.

Even after expiry, the cached browser plan appeared at 123 ms and the initial server-result view
at 206 ms. Existing cached rendering is doing useful work without lengthening server staleness.

## Decision

A second run with three pairs per page completed all 18 navigations without browser errors.
Manufacturing's first queue-plan request took 1,040 ms; immediate repeats took 11–14 ms.
All three post-expiry requests recorded a plan miss and two recipe graph hits, taking 81–83 ms.
Cached content appeared at 107–123 ms, followed by the initial server-result paint at 194–207 ms.
This confirms the observed distinction within this fixture; it is still too small and synthetic
to establish production tail latency. Redis was enabled, but these Manufacturing repeat visits
hit process L1, so this experiment does not measure cross-worker plan-cache hit performance.

Do not extend the whole-plan TTL based on this evidence. Prioritize first-use recipe/reference
loading and duplicate concurrent misses, then test representative queues on Postgres/Redis.

Version-based permanent recipe caching is deferred: the current SDE importer has no reliable
revision marker and commits some datasets independently. A safe implementation must publish a
revision only after a completed import and make long-lived workers observe it; a deploy SHA is
not a substitute for the database's SDE version. Removing the safety TTL without that lifecycle
would trade occasional latency for potentially indefinite stale recipes.

Reference-feed prewarming/background refresh remains a candidate, not implemented here. It needs
bounded failure handling, shared refresh coordination and measurements on a representative
environment. The existing feeds already cache for hours; merely extending those TTLs does not
solve the first-ever miss.

Reproduce with:

```bash
BENCH_REDIS=1 BENCH_EXPIRY_WAIT_SECONDS=25 BENCH_SAMPLES=3 bash scripts/run_latency_benchmark.sh
```
