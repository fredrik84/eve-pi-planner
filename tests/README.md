# Test suite

All repository test programs live here so the project root stays reserved for application and
deployment entry points. The tests remain standalone scripts rather than a pytest package: run
them from the repository root so relative `app/` and `static/` reads resolve consistently.

## Common commands

```bash
# Browser-free JavaScript behavior
node tests/test_routing_client.js
node tests/test_character_health.js
node tests/test_planet_submission_reviews.js

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

`test_expired_reminders.py` checks the expired-program reminder sequence with an in-memory log,
including a simulated three-week break through the real notification scheduler, program restarts,
failed reads, existing send history and multiple notification channels. Run it in the app container.

`test_hybrid_setup.py` and `test_redeploy_candidates.py` require their documented local fixtures:
run `scripts/seed_hybrid_fixture.py` and `scripts/seed_redeploy_fixture.py` in the local test
container before those API suites. Account-deletion fixtures give each account its own colony
program key, including in the account-owned historical ledger.
