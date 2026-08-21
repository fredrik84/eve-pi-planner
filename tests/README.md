# Test suite

All repository test programs live here so the project root stays reserved for application and
deployment entry points. The tests remain standalone scripts rather than a pytest package: run
them from the repository root so relative `app/` and `static/` reads resolve consistently.

## Common commands

```bash
# Browser-free JavaScript behavior
node tests/test_routing_client.js

# Tests that only need the repository
python3 tests/test_setup_page.py

# Application tests use the local container's dependencies and database
docker compose cp tests web:/srv/app/
docker compose exec -T web python3 tests/test_reactions.py --url http://127.0.0.1:8000
docker compose exec -T web python3 tests/test_industry.py --url http://127.0.0.1:8000
```

The coverage map and notes about local fixture limitations are in
[`docs/workflow.md`](../docs/workflow.md#test-suites).
