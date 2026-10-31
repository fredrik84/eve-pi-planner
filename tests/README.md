# Test suite

All repository test programs live here so the project root stays reserved for application and
deployment entry points. The tests remain standalone scripts rather than a pytest package: run
them from the repository root so relative `app/` and `static/` reads resolve consistently.

## Common commands

`test_referrals.py` exercises real page middleware, source-domain classification, privacy controls,
admin authorization, environment scoping, and log-query failures without a database or network.
Run it in the application container. `node tests/test_referrals.js` checks the real Admin card
renderer, including empty/error states, escaping, and honest labels for request counts.

```bash
# Browser-free JavaScript behavior
node tests/test_routing_client.js
node tests/test_character_health.js
node tests/test_planet_submission_reviews.js
node tests/test_refill_deadline.js
node tests/test_frontend_cleanup.js

# Tests that only need the repository
python3 tests/test_setup_page.py

# Application tests use the local container's dependencies and database
docker compose cp tests web:/srv/app/
docker compose exec -T web python3 tests/test_reactions.py --url http://127.0.0.1:8000
docker compose exec -T web python3 tests/test_industry.py --url http://127.0.0.1:8000
```

The coverage map and notes about local fixture limitations are in
[`docs/workflow.md`](../docs/workflow.md#test-suites).

CI runs `test_service_refactor.py`, `test_production_allocation.py`, and
`test_market_cache_contract.py` before publishing an image. These use mocks or in-memory fixtures;
they need application dependencies but no application database, Redis, ESI, or market connection.

`test_character_health.js` runs the character warning and background refresh code, including
reauthorization, temporary failures, recovery, and navigation to Characters settings.
`test_storage_projection.py` checks that expired extraction stops filling launchpads and that
revoked tokens invalidate the character cache. Run it in the application container; it needs no
database or ESI connection. `test_alerts.py` also checks the stopped-launchpad behavior through the
live dashboard API with isolated fixture data.

`test_planet_submission_reviews.py` uses a temporary database to check review alerts, account
isolation, authentication, dismissal, and migration of existing submissions. Its JavaScript
companion checks the global banner, refresh behavior, and failed requests.
`test_planet_submission_reviews_postgres.py` runs the same endpoint contracts against PostgreSQL
when `PLANET_REVIEWS_TEST_DSN` points to a disposable test database. Each test creates and removes
its own schema. CI runs both database variants to catch timestamp type errors that SQLite accepts.

`test_planet_import.py` checks planet-type detection across all eight types with blank or
zero-filled CSV/spreadsheet cells, small positive readings, explicit types, ambiguity warnings,
and non-destructive merging. It uses an in-memory database and runs in the app container and CI.

`test_expired_reminders.py` checks the expired-program reminder sequence with an in-memory log,
including a simulated three-week break through the real notification scheduler, program restarts,
failed reads, existing send history and multiple notification channels. Run it in the app container.

`test_refill_deadline.js` runs the real refill allocation and rendering code: deadline ceilings,
inventory versus delivered runtime/value, shared-material totals, character filters, retained pads,
missing inputs/prices, and asynchronous price loading. `test_pi_product_prices.py` checks the
optional public PI price batch without network or a database; run it in the app container.

`test_frontend_cleanup.js` checks that the manual blueprint editor renders overrides only, that
shared-library batch contents still open, and that group-permission mutations retain error recovery.
The regression fails against the pre-cleanup discarded batch renderer. The file-by-file cleanup
inventory and retained follow-ups are in [`docs/code-audit-2026-10-27.md`](../docs/code-audit-2026-10-27.md).

`test_upgrade_policy.py` guards dependency PR checks, the push-only publish/deploy boundary,
matching runtime/CI Python versions, and matching Playwright package/browser-image versions.
`test_optimizer.py --offline` exercises the native HiGHS solver without a running API or SDE;
CI runs these synthetic cases on every dependency PR and branch push.

`test_hybrid_setup.py` and `test_redeploy_candidates.py` require their documented local fixtures:
run `scripts/seed_hybrid_fixture.py` and `scripts/seed_redeploy_fixture.py` in the local test
container before those API suites. Account-deletion fixtures give each account its own colony
program key, including in the account-owned historical ledger.
