#!/usr/bin/env bash
# Local synthetic tenant only. For a real account use the browser project directly (see docs).
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
benchmark_container="eve-pi-latency-$$"
benchmark_token="$(openssl rand -hex 24)"
case "${BENCH_DIAGNOSTICS:-1}" in
  1) benchmark_client_token="$benchmark_token" ;;
  0) benchmark_client_token="" ;;
  *) echo "BENCH_DIAGNOSTICS must be 0 or 1" >&2; exit 1 ;;
esac
cleanup() {
  docker stop "$benchmark_container" >/dev/null 2>&1 || true
  docker compose exec -T web python3 scripts/seed_browser_fixture.py --restore >/dev/null || true
}
trap cleanup EXIT
docker compose cp scripts/seed_browser_fixture.py web:/srv/app/scripts/seed_browser_fixture.py
docker compose exec -T web python3 scripts/seed_browser_fixture.py --latency
# A disposable process avoids changing the normal local server's environment. Its application
# files are the worktree; DB/Redis remain the local Compose configuration, never flushed.
# Disable lifecycle jobs so a benchmark worker cannot start duplicate background schedulers.
docker compose run -d --rm --no-deps --name "$benchmark_container" \
  -e LATENCY_TOKEN="$benchmark_token" \
  -v "$project_dir/app:/srv/app/app:ro" -v "$project_dir/static:/srv/app/static:ro" \
  web python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --lifespan off >/dev/null
for attempt in $(seq 1 30); do
  if docker exec "$benchmark_container" python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/")' >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" == 30 ]]; then
    echo "Benchmark server did not become ready" >&2
    exit 1
  fi
  sleep 1
done
# Rebuild the small harness so npm scripts and the pinned Playwright image match the worktree.
docker compose --profile test build browser-tests
PP_SESSION=eve-pi-browser-protocol-local LATENCY_TOKEN="$benchmark_client_token" \
  BROWSER_BASE_URL="http://$benchmark_container:8000" \
  docker compose --profile test run --rm --no-deps browser-tests \
  npx playwright test --project=latency "$@"
