# Service refactoring and health — 2026-09-15

Follow-up to the production review, using `120fcc8` as the baseline. This pass reviewed the PI
planner and recommendations, market/cache infrastructure, character/account handling, alerts,
configuration, access control, and supporting tests. Changes focus on demonstrated defects and
duplicate work; no UI flow, authentication policy, schema, or external-notification behavior changed.

## Changes

- **PI feasibility:** bipartite matching traverses candidate planets lazily instead of building
  an index of every candidate for each check. Planet identity and pinned-slot behavior are unchanged.
- **PI recommendations:** retain each system's top-K densities once, and reject duplicate
  combinations before merging their resource lists. Lower-density tails cannot contribute to a
  combination's top K. Scoring, candidate limits, beam width, and J-space handling are unchanged.
- **Market caching:** prices, order-book aggregates, and market history now use one cache lookup
  implementation. It deduplicates type IDs, preserves zero-valued results, and keeps the existing
  success/failure lifetimes. History still fetches missing types with at most eight workers.
- **Complete market books:** region and structure markets share pagination. A failed later page
  invalidates the whole fetch rather than publishing incomplete prices or volumes. Region failures
  are cached for 60 seconds; successfully empty books keep their normal success lifetime.
- **History errors:** an HTTP error body is treated as unavailable history, rather than crashing
  when code tries to slice the error object as a list of days.
- **Redis recovery:** a failed initial ping no longer permanently caches an unavailable client.
  Connections are lazy, and later operations can reconnect. One malformed JSON entry in a batch
  read no longer discards the other valid hits.
- **Cleanup:** removed six unused imports from admin, ESI, notification, and planner-storage
  modules. Preserved imports used by quoted annotations and intentional `app.sde` re-exports.
- **Test health:** fixed account-deletion fixture key collisions and reaction formula fixtures
  that inadvertently removed the current job-capacity snapshot. Seeded the documented local
  hybrid/redeploy fixtures before their API suites.
- **CI:** the image-publishing job now depends on an isolated backend test job covering the new
  shared-service contracts, production allocation, and market-cache contracts. The existing
  advisory frontend lint job retains its prior behavior.

## Measurements and verification

Synthetic medians from three runs in the local application container:

| Check | Before | After |
| --- | ---: | ---: |
| 3,000 feasibility calls, six slots sharing 300 planet candidates | 1.1873s | 0.0237s |
| Recommendations, 50 systems × 36 planets, three requested systems | 0.0697s | 0.0482s |

These are helper benchmarks, not page-load measurements. Matching results were identical on 500
seeded comparisons with the original function and matched exhaustive search on 100 small graphs.
Recommendation payloads were identical across 18 configurations varying system count, requested
combination size and density threshold. The benchmark used Aqueous Liquids, Base Metals and Noble
Metals with needs 2, 0.5 and 1 respectively.

`test_service_refactor.py` has 15 passing tests; eight regressions failed before the fixes.
The three CI suites also pass without an application database, Redis URL, or upstream calls.

Passing functional coverage includes:

- Manufacturing's **1,130 checks**, full Reactions, production allocation, order cadence, profit
  clock, reaction job fees, market caching, stock reservations, and settings memoization.
- PI optimizer, minimum-CC layouts, J-space behavior, hybrid setups, redeploy candidates, refill
  rates, factory drain, PI lifetime accounting and reseat events.
- Alerts and alert cadence, configuration import/export, character disconnect, account deletion,
  audit, database pool, fresh-table migration guards, system search, Planet DB protection, page
  access, feature/API surface, routing, setup-page and navigation checks.
- Python compilation, frontend lint and `git diff --check`. The workflow YAML parses and the
  publication job depends on the backend contract job.

Tests ran against the modified local Docker stack. No production fixture writes or manual
external notifications were used. The epoch-precision suite reported its SQLite skip, so this run
does not establish PostgreSQL timestamp behavior.

## Remaining limitations

The initial broader run had six failing suites. The hybrid and redeploy suites passed after their
documented fixture setup; account deletion passed after its fixture repair. The remaining three
also fail on the baseline:

- `test_sde_migration.py`: the local constellation/region table is empty.
- `test_distribution.py`: the three known local fuel-block fixture cases fail; two embedded
  checks also report import skips. Planner verification here comes from the other functional
  suites and direct old/new comparisons, not those unavailable cases.
- `test_ui_notes.py`: seven assertions expect older Reactions note text and a removed helper.
  Reconcile these with the current UI contract before treating them as a deployment gate.

The previous review's full-Reactions failure is now resolved by the capacity-fixture correction.
Its separate levelling, standalone-cadence and blueprint-paste failures remain recorded in
`production-code-health-2026-09.md`; this pass did not rework those production contracts.

Large orchestration functions remain in character listing, PI dashboard, and the plan entry
points. They warrant targeted extraction with behavioral coverage when changed next; no wholesale
module movement was needed for the measured improvements here. Database integration coverage still
needs a dedicated Postgres fixture environment before the broader suites can become CI gates.
