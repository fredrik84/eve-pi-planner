#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

# The normal web service intentionally has no source bind mount. Copy only the local fixture
# script, seed its reserved tenant, then launch the disposable browser container.
docker compose cp scripts/seed_browser_fixture.py web:/srv/app/scripts/seed_browser_fixture.py
docker compose exec -T web python3 scripts/seed_browser_fixture.py
restore_fixture_features() {
  docker compose exec -T web python3 scripts/seed_browser_fixture.py --restore >/dev/null || true
}
trap restore_fixture_features EXIT
# Role/feature decisions are deliberately memoized in the app. Restart so the just-added tester is
# visible to the same server process the browser will exercise.
docker compose restart web
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/ >/dev/null; then
    break
  fi
  if [[ "$attempt" == 30 ]]; then
    echo "Local web service did not become ready" >&2
    exit 1
  fi
  sleep 1
done

set +e
if (($#)); then
  PP_SESSION="eve-pi-browser-protocol-local" \
    docker compose --profile test run --rm browser-tests npx playwright test --project=protocol "$@"
else
  PP_SESSION="eve-pi-browser-protocol-local" \
    docker compose --profile test run --rm browser-tests npm run test:protocol
fi
test_status=$?
set -e

# Keep the most recent HTML result available on a headless server after the disposable runner
# exits. Starting it after Playwright also means the report directory definitely exists.
docker compose --profile test up -d test-report
echo "Browser test report: http://<this-server>:${BROWSER_REPORT_PORT:-9323}"
exit "$test_status"
