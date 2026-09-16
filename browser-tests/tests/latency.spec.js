// Explicit opt-in project; ordinary smoke/protocol runs never run a benchmark.
const { test, expect } = require('@playwright/test');
const { installSession } = require('./helpers');
const { installBrowseGuard } = require('./browse-guard');
const fs = require('node:fs/promises');
const path = require('node:path');

function distribution(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const percentile = p => sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)];
  return { n: sorted.length, min: sorted[0], p50: percentile(0.5), p95: percentile(0.95), max: sorted.at(-1) };
}

test('user page latency and cache matrix @latency', async ({ browser, baseURL }, testInfo) => {
  expect(Boolean(process.env.PP_SESSION), 'Set PP_SESSION for a configured account').toBe(true);
  const samples = Number(process.env.BENCH_SAMPLES || 5);
  const expiryWait = Number(process.env.BENCH_EXPIRY_WAIT_SECONDS || 0);
  expect(Number.isInteger(expiryWait) && expiryWait >= 0 && expiryWait <= 60,
    'BENCH_EXPIRY_WAIT_SECONDS: 0–60').toBe(true);
  const cacheStates = ['fresh-browser', 'repeat-visit'];
  if (expiryWait) cacheStates.push('after-expiry-wait');
  expect(Number.isInteger(samples) && samples >= 1 && samples <= 50, 'BENCH_SAMPLES: 1–50').toBe(true);
  test.setTimeout(samples * 2 * (cacheStates.length * 90_000 + expiryWait * 1000) + 30_000);
  const results = [];
  const origin = new URL(baseURL).origin;
  // No account/character IDs, query strings, cookies, bodies or arbitrary URL paths in reports.
  const label = url => {
    const parsed = new URL(url);
    if (parsed.origin !== origin) return 'external-resource';
    // Distinguish dashboard loading from its concurrent background refresh.
    if (parsed.pathname === '/api/reactions/jobs/refresh') return parsed.pathname;
    return parsed.pathname.replace(/\b\d+\b/g, ':id').split('/').slice(0, 4).join('/');
  };

  try {
    for (const route of ['reactions', 'manufacturing']) {
      for (let sample = 0; sample < samples; sample++) {
        const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
        try {
          const page = await context.newPage();
          await installSession(page);
          if (process.env.BENCH_BROWSE_ONLY === '1') await page.addInitScript(installBrowseGuard);
          await page.addInitScript(({ token, origin }) => {
            window.__ppLatency = { marks: {}, longTasks: [] };
            performance.setResourceTimingBufferSize(2000);
            // Timestamp after a paint opportunity, not merely after innerHTML was assigned.
            window.__ppLatencyMark = name => {
              requestAnimationFrame(() => requestAnimationFrame(() => {
                window.__ppLatency.marks[name] ??= performance.now();
              }));
            };
            new PerformanceObserver(list => {
              for (const entry of list.getEntries()) window.__ppLatency.longTasks.push(entry.duration);
            }).observe({ type: 'longtask', buffered: true });
            // Routing interception disables Chromium's HTTP cache. Wrap fetch instead and NEVER
            // forward the diagnostic secret to ESI, icons or any other external origin.
            if (token) {
              const original = window.fetch;
              window.fetch = function(input, init) {
                const url = new URL(input instanceof Request ? input.url : input, location.href);
                if (url.origin === origin && url.pathname.startsWith('/api/')) {
                  const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
                  headers.set('X-Latency-Token', token);
                  init = { ...init, headers };
                }
                return original.call(this, input, init);
              };
            }
          }, { token: process.env.LATENCY_TOKEN || '', origin });
          let failures = [];
          let cachedResponses = 0;
          const cdp = await context.newCDPSession(page);
          await cdp.send('Network.enable');
          cdp.on('Network.requestServedFromCache', () => cachedResponses++);
          page.on('pageerror', () => failures.push('javascript-error'));
          page.on('requestfailed', request => failures.push(`request-failed:${label(request.url())}`));
          page.on('response', response => {
            if (response.status() >= 400) failures.push(`http-${response.status()}:${label(response.url())}`);
          });
          for (const cacheState of cacheStates) {
            if (cacheState === 'after-expiry-wait') {
              // Close the document so background polling cannot silently keep caches warm.
              await page.goto('about:blank');
              await new Promise(resolve => setTimeout(resolve, expiryWait * 1000));
            }
            failures = [];
            cachedResponses = 0;
            const response = await page.goto(`/${route}`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
            expect(response.ok()).toBe(true);
            await page.waitForFunction(route => {
              const marks = window.__ppLatency?.marks || {};
              return marks[`${route}-live`] !== undefined || marks[`${route}-empty`] !== undefined;
            }, route, { timeout: 60_000 });
            let settled = true;
            try { await page.waitForLoadState('networkidle', { timeout: 15_000 }); }
            catch { settled = false; }
            const measurement = await page.evaluate(() => {
              const nav = performance.getEntriesByType('navigation')[0];
              const resources = performance.getEntriesByType('resource').map(r => ({
                url: r.name, startMs: r.startTime, durationMs: r.duration,
                ttfbMs: r.responseStart > 0 ? r.responseStart - r.requestStart : null,
                transferBytes: r.transferSize,
                serverTiming: r.serverTiming.map(t => ({ name: t.name, durationMs: t.duration, count: Number(t.description) || null })),
              }));
              return {
                marks: window.__ppLatency.marks,
                blockedWrites: window.__ppBlockedWrites || [],
                documentTtfbMs: nav.responseStart - nav.requestStart,
                documentDnsMs: nav.domainLookupEnd - nav.domainLookupStart,
                documentConnectMs: nav.connectEnd - nav.connectStart,
                documentDownloadMs: nav.responseEnd - nav.responseStart,
                domContentLoadedMs: nav.domContentLoadedEventEnd,
                loadMs: nav.loadEventEnd || null,
                fcpMs: performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
                observedUntilMs: performance.now(),
                longTaskMs: window.__ppLatency.longTasks.reduce((a, b) => a + b, 0),
                resources,
              };
            });
            const liveMs = measurement.marks[`${route}-live`] ?? measurement.marks[`${route}-empty`];
            failures.push(...measurement.blockedWrites);
            const cachedMs = measurement.marks[`${route}-cached`] ?? null;
            measurement.resources = measurement.resources.map(({ url, ...r }) => ({ endpoint: label(url), ...r }));
            if (process.env.LATENCY_TOKEN && !measurement.resources.some(r => r.serverTiming.some(t => t.name === 'backend'))) {
              failures.push('missing-server-timing:check-LATENCY_TOKEN');
            }
            const row = {
              route, sample: sample + 1, cacheState, serverCacheState: 'not-reset; observed counters only',
              readyMs: cachedMs === null ? liveMs : Math.min(cachedMs, liveMs), liveMs, cachedMs,
              empty: measurement.marks[`${route}-empty`] !== undefined,
              browserCacheResponses: cachedResponses, settled, failures: [...failures], ...measurement,
            };
            results.push(row);
            console.log(`${route} ${cacheState} #${sample + 1}: visible ${row.readyMs.toFixed(0)}ms; live ${liveMs.toFixed(0)}ms; browser cache ${cachedResponses}`);
            expect(failures, 'Errors invalidate the latency sample').toEqual([]);
          }
        } finally { await context.close(); }
      }
    }
  } finally {
    const summary = [];
    for (const route of ['reactions', 'manufacturing']) {
      for (const cacheState of cacheStates) {
        const rows = results.filter(r => r.route === route && r.cacheState === cacheState && !r.failures.length);
        const api = rows.flatMap(r => r.resources).filter(r => r.endpoint.startsWith('/api/'));
        const endpoints = [...new Set(api.map(r => r.endpoint))].sort().map(endpoint => {
          const requests = api.filter(r => r.endpoint === endpoint);
          const metrics = [...new Set(requests.flatMap(r => r.serverTiming.map(t => t.name)))];
          return { endpoint, requestMs: distribution(requests.map(r => r.durationMs)),
            ttfbMs: distribution(requests.map(r => r.ttfbMs)),
            server: Object.fromEntries(metrics.map(name => {
              const observations = requests.map(r => r.serverTiming.find(t => t.name === name)).filter(Boolean);
              return [name, { durationMs: distribution(observations.map(t => t.durationMs)),
                totalCount: observations.reduce((sum, t) => sum + (t.count || 0), 0) }];
            })) };
        });
        summary.push({ route, cacheState, readyMs: distribution(rows.map(r => r.readyMs)),
          liveMs: distribution(rows.map(r => r.liveMs)), longTaskMs: distribution(rows.map(r => r.longTaskMs)), endpoints });
      }
    }
    const report = {
      schemaVersion: 1, measuredAt: new Date().toISOString(), browser: browser.version(),
      requestedSamples: samples, diagnosticMode: Boolean(process.env.LATENCY_TOKEN),
      browseOnly: process.env.BENCH_BROWSE_ONLY === '1',
      expiryWaitSeconds: expiryWait,
      complete: results.length === samples * 2 * cacheStates.length && results.every(r => !r.failures.length),
      caveats: [
        'Sequential desktop Chromium, no network/CPU throttling; this runner location is not every user.',
        'Fresh browser clears HTTP cache and localStorage, NOT Redis, process, DB or upstream caches.',
        'Repeat visit retains HTTP cache and localStorage; server cache state is observed, never flushed.',
        'Diagnostic API responses use no-store, so run without LATENCY_TOKEN for natural API HTTP caching.',
        'Server durations are cumulative work and may overlap; do not subtract summed work from wall time.',
        'Ready means primary dashboard paint, live means initial server result paint, not ESI-fresh data.',
        'Background refreshes may continue; networkidle is a secondary observation, not readiness.',
        'Small-sample p95 is descriptive, not a statistically robust SLO or concurrency/load test.',
      ], summary, samples: results,
    };
    const file = testInfo.outputPath('latency.json');
    await fs.mkdir(path.dirname(file), { recursive: true });
    await fs.writeFile(file, JSON.stringify(report, null, 2));
    await testInfo.attach('latency', { path: file, contentType: 'application/json' });
    console.log(JSON.stringify(summary.map(({ endpoints, ...row }) => row), null, 2));
  }
});
