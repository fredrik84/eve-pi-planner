# Browser acceptance tests

This is the browser layer for the manual protocols in
`docs/production-phases-test-protocol.md`. It runs Chromium in a disposable Playwright container;
the app remains in the normal `web` service.

## Safe local smoke test

```bash
docker compose --profile test run --rm browser-tests
```

The default is read-only. It checks the application shell, SPA routes, browser console/network
failures, and saves a screenshot, video, and trace for failures under `browser-tests/artifacts/`.

Run desktop and mobile projects together:

```bash
docker compose --profile test run --rm browser-tests npm run test:all
```

## Authenticated protocol checks

For the fully automated local protocol, use the wrapper. It resets and seeds a reserved tester
tenant, then runs the protocol project with its session:

```bash
scripts/run_browser_protocol.sh
```

Pass a spec path or Playwright filter arguments to run a focused protocol slice, for example:

```bash
scripts/run_browser_protocol.sh tests/protocol-phase2.spec.js
```

The reserved ids are defined in `scripts/seed_browser_fixture.py`. The reset queries are always
scoped to those ids and never clear a real account.

Every wrapper run also starts a persistent report server. From another machine, browse to:

```text
http://SERVER_HOST_OR_IP:9323
```

Set `BROWSER_REPORT_PORT` if port 9323 is unavailable. The report server shows the latest run and
continues running after the disposable browser container exits:

```bash
BROWSER_REPORT_PORT=9400 scripts/run_browser_protocol.sh
```

To serve an already-generated report without rerunning tests:

```bash
docker compose --profile test up -d test-report
```

To point the non-mutating protocol checks at an already-authenticated dev account instead:

Pass a valid `pp_session` cookie without saving it in a file:

```bash
PP_SESSION='the-cookie-value' \
BROWSER_BASE_URL='https://dev.eveindustry.net' \
docker compose --profile test run --rm browser-tests npm run test:protocol
```

Omit `BROWSER_BASE_URL` to test the local `web` container. Never commit a session token. The
protocol project is the place for automated checks derived from the acceptance document. Keep
steps that install, complete, remove, or clear EVE work out of the default smoke project.

## Inspect results and failures

Browse to the report server above. The underlying HTML is in
`browser-tests/artifacts/report/index.html`. A retained `trace.zip` can be opened at
<https://trace.playwright.dev/> or from a Playwright container.
