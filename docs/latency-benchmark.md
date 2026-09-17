# User latency benchmark

Run the local, authenticated Reactions/Manufacturing benchmark:

```bash
BENCH_SAMPLES=10 bash scripts/run_latency_benchmark.sh
# Natural HTTP caching, without diagnostic response headers:
BENCH_DIAGNOSTICS=0 BENCH_SAMPLES=10 bash scripts/run_latency_benchmark.sh
# Private, disposable Redis; also revisit after the 20-second plan cache expires:
BENCH_REDIS=1 BENCH_EXPIRY_WAIT_SECONDS=25 BENCH_SAMPLES=3 bash scripts/run_latency_benchmark.sh
```

Requires the existing local Docker Compose `web` service and its SDE. The wrapper resets the
**reserved browser-protocol tenant**, adds ten Rifters to its Manufacturing queue, starts a
disposable worktree-backed server, and restores temporary feature overrides on exit. Do not run
it alongside the browser protocol (they share that tenant). It does not seed real accounts,
flush Redis, restart the normal web service, or run duplicate background schedulers. The fixture
remains available afterward; the next fixture run resets it. Use only with local service data.

`BENCH_REDIS=1` starts a private `redis:7-alpine` container on a temporary network, with no
published ports or persistence. Only the disposable benchmark server uses it. Cleanup stops it
and removes the temporary network; all benchmark Redis contents are discarded. Shared Redis is
never flushed. With the option off, the local server's configured Redis behavior is unchanged.

Results are attached as `latency.json` in `browser-tests/artifacts/results/` and in the existing
Playwright HTML report (`browser-tests/artifacts/report/index.html`). Artifacts are gitignored
and overwritten by subsequent Playwright runs; copy a baseline elsewhere before another run.

## What gets measured

| Layer | Measurements |
| --- | --- |
| User-visible frontend | Primary dashboard's first paint opportunity; cached-plan and initial server-result paints separately; FCP, DOMContentLoaded, load, long-task time |
| Browser/network | Document DNS, connection and download durations, TTFB; per-resource start/duration/transfer bytes; observed Chromium HTTP-cache responses |
| Backend | `Server-Timing` response-header latency, excluding streaming response body |
| Database | Connection acquisition, execute, fetch/row conversion, explicit commits; durations and operation counts |
| Server caching | Redis read/write time; read hit/miss/error/bypass counts; request-memo hit/miss/bypass; market L1 positive/negative hits and misses |
| External services | Central ESI request time including pacing/budget work; market upstream-fetch time |

The JSON contains per-navigation samples, sanitized request waterfalls, and p50/p95/min/max per
page/cache state and per API endpoint. Counts in `Server-Timing` use the `desc` parameter, not
`dur`. Cache counters count entries looked up; a Redis batch is one timed read operation but
can have many hits/misses. Negative market-cache hits are successful avoidance of an upstream
retry, not successful price data. No observed cache metric means **unknown/not reached**, not
zero misses. Endpoint metric distributions include only requests that emitted that metric.

`plan_l1_hit`, `plan_redis_hit`, and `plan_miss` identify the whole Manufacturing response cache.
`recipe_graph_hit`, `recipe_graph_miss`, and `recipe_graph_load` distinguish static recipe reuse
from actual graph construction. Concurrent cold graph reads share one construction per worker;
the existing 15-minute safety expiry remains until the importer has a reliable SDE revision marker.

DB timings cover calls through `app.db`, in both SQLite and PostgreSQL. They include driver and
row-conversion overhead, not just DB-server CPU. Acquisition includes pool waiting, connection
creation/liveness and SQLite setup; it can overlap setup queries. Implicit context-manager
commit/rollback, direct connections outside `app.db`, arbitrary executor threads that do not
copy context, SDE `lru_cache`, and OS/Postgres buffer-cache hits are not separately measured.
ESI and market upstream timers can overlap; external retries/pacing are included, not bypassed.

**Do not add these durations into a stacked wall-time breakdown.** Concurrent requests overlap,
and nested operations can be counted in more than one category. Browser request time includes
network and server time; TTFB minus backend time is at best a residual, not pure network time.
Long tasks measure main-thread tasks over 50ms, not total frontend CPU or all rendering time.
External-origin resource timings can be restricted by browser timing permissions.

## Cache matrix

Each sample pair uses a new browser context:

1. **Fresh browser:** empty HTTP cache and localStorage, with an installed login cookie.
2. **Repeat visit:** another navigation in the same context, retaining HTTP cache and localStorage.

Optional `BENCH_EXPIRY_WAIT_SECONDS` (integer 0–60, default 0) adds **after-expiry-wait**: navigate
to a blank page to stop document polling, wait the configured duration, and revisit in the same
context. Use at least 25 seconds to probe the current 20-second plan/dashboard caches. The label
records a wait, not proof that every cache expired; verify the hit/miss counters. Longer-lived
reference/recipe caches remain warm. This increases runtime by two waits per sample pair.

The server is **not reset between navigations**. A new browser is not a cold backend. The local
wrapper starts a new process once, but its DB, Redis and upstream caches may already be warm,
and startup/page requests warm them further. Raw samples retain order so first-hit outliers stay
visible instead of being discarded as warmup. Redis/process hits are observed where instrumented;
DB buffer-cache warmth is unknown. Never use `FLUSHALL` to manufacture a cold result on a shared
service. A genuinely cold-server experiment needs separately isolated DB/Redis/process state.

Manufacturing can display its cached last plan before the first server plan returns. Both times
are recorded. `liveMs` means the **initial server-result paint**, which may itself use backend
caches—not a guarantee that ESI has refreshed. Background freshness checks can repaint later.
Reactions also records `statusMs`: jobs and slots are visible, but financial values are still
loading. Its `liveMs` records the priced dashboard. `readyMs` takes the earliest of cached,
structural and full-result paints; do not mistake structural readiness for prices being ready.
`empty: true` explicitly identifies an empty queue; do not compare that to a populated account.
Network idle (500ms quiet, bounded wait) is a secondary settling observation, not the readiness
definition. Requests still running at collection time are not in completed resource timings.

## Measuring an existing account/environment

### Owner-authorized production browsing

Operators with cluster exec access can run navigation-only checks for an account whose owner
has explicitly approved normal browsing side effects:

```bash
python3 scripts/run_authenticated_benchmark.py --character 'Authorized character name' --samples 1
```

This resolves one account by exact character name and reuses its latest existing session. It
does not create sessions, add an authentication endpoint, enable impersonation globally, or
change account roles. The owner must already have logged in. Credentials are read through a
read-only DB connection and passed in process memory/stdin, not command-line values, files,
Docker environment configuration or reports. It does **not** impose a new expiry on the existing
session or revoke the owner's session after the test. Production URL and namespace are fixed.

Only the latency navigation project runs, with traces/screenshots/video disabled. A browser
fetch/XHR guard allows GET/HEAD and exactly three automatic POSTs without query parameters:
reaction job refresh, industry stale-data refresh, and industry queue planning. Explicit order,
assignment, character and settings mutations, forced refreshes, and beacons are blocked and
reported as failures. The test does not click action buttons or submit forms.

This is a **runner safeguard, not a server-enforced read-only session**. Normal page loads can
refresh ESI snapshots, reconcile saved plans and perform automatic reaction handoffs. The owner
must authorize these normal-browsing effects; it is unsuitable when absolutely no data changes
are permitted. Never run the mutation-heavy protocol suites with this account. A separate test
account is still required for create/edit/delete flow coverage. Reports remain local/gitignored.

The guard has an isolated test: `node tests/test_browse_guard.js`.

### Supplying a session yourself

Do **not** run the seeding wrapper against a deployed environment. Use the browser project only:

```bash
# Supply PP_SESSION securely in the environment; never paste it into source or reports.
BROWSER_BASE_URL=https://your-environment.example BENCH_SAMPLES=10 \
  docker compose --profile test run --rm --no-deps browser-tests \
  npx playwright test --project=latency
```

The account must have access and completed onboarding. Run against a build containing the render
milestones. This performs normal page loads, including the app's automatic refresh POSTs and
existing planning side effects; it is not an inert HTTP GET-only probe. It never clicks force
refresh or adds orders outside the local wrapper. Use an appropriate test account.

For backend attribution, configure a strong `LATENCY_TOKEN` on the target server and supply the
same environment variable to the browser runner. This is an infrastructure diagnostic gate,
default off, like internal metrics—not a public user feature. Requests without the matching token
retain normal response/cache behavior. The runner sends it only on same-origin API fetches, not
to external image services. Do not use it over an untrusted plaintext connection.

**Diagnostic API responses are `private, no-store`**, preventing reuse/leakage of request timings.
Therefore run **without** `LATENCY_TOKEN` as well to measure natural API HTTP caching. Static asset
caching and localStorage remain enabled in diagnostic mode. No routing interception or cache-busting
query parameters are used. Diagnostics add some overhead; compare like-for-like runs.

Traces, screenshots and videos are disabled for this project. Reports exclude cookies, SQL,
cache keys, response bodies and query strings; endpoint numeric IDs are normalized. They still
contain performance measurements, so handle reports as internal diagnostics. No external
analytics or telemetry collector is involved.

This is a sequential desktop Chromium benchmark on the runner's CPU/network, **not a concurrent
load test or real-user monitoring**. Run from representative user locations/devices before making
claims about production user experience. Five samples are a quick smoke baseline; small-sample
p95 is not an SLO. Keep account workload, build, runner and cache mode fixed for comparisons.
No absolute speed threshold is enabled until representative baselines establish one.

## Regression checks

Reactions cold loads request a price-free status view first, then populate financial tiles from
the full response. Unknown values are null, displayed as spinners, never provisional zeroes.
A failed price request preserves the job view and offers a retry. A warm full-dashboard cache
satisfies the first request directly. Partial and full responses have separate caches with the
same 20-second TTL and shared invalidation; this does not extend market-data freshness.

Independent adjacent structure books overlap in a process-wide pool of three workers, with
same-worker cache-miss locking and request-local reuse. Quotes still resolve in configured
priority order, independently for buy and sell. Bounded lookahead can fetch up to two books
that ultimately are not needed. Region requests and ESI pacing remain unchanged. The two-phase
dashboard repeats structural reads/reconciliation on a cold miss; it improves visible readiness
without claiming to remove all backend work.

`python tests/test_cold_load.py` checks overlap, precedence, cache isolation and invalidation;
`node tests/test_reaction_progressive.js` checks loaders, deferred values, failure and response races.

`python tests/test_latency.py` checks disabled/authorized diagnostics, concurrency/thread context,
exception cleanup, SQLite/Postgres timing, and cache hit/miss/negative-cache accounting without
real DB or Redis access. It is included in the backend CI gate. Normal smoke and protocol projects
exclude `@latency`; no diagnostic traffic or benchmark workload runs implicitly.
