'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/profiling.js'), 'utf8');
function harness(options = {}) {
  const sent = [], timers = [], listeners = [];
  const nav = { startTime: 0, loadEventEnd: 1200, domContentLoadedEventEnd: 800, requestStart: 10, responseStart: 60 };
  const sandbox = {
    currentTab: () => 'industry', _features: { page_profiling: { state: 'public' } },
    navigator: { sendBeacon: (url, blob) => { sent.push({ url, blob }); return true; }, ...options.navigator },
    document: { readyState: options.loading ? 'loading' : 'complete' },
    window: { addEventListener: (event, callback) => { assert.equal(event, 'load'); listeners.push(callback); } },
    performance: { now: () => options.now ?? 1250, getEntriesByType: () => [nav] },
    setTimeout: callback => timers.push(callback), Blob,
  };
  if (options.state) sandbox._features.page_profiling.state = options.state;
  vm.createContext(sandbox); vm.runInContext(source, sandbox);
  return { sandbox, sent, timers, listeners, nav, flush: () => { while (timers.length) timers.shift()(); } };
}
(async () => {
  const h = harness();
  h.sandbox.profileInitialPageLoad(); h.sandbox.profileInitialPageLoad(); h.flush();
  h.sandbox.profileInitialPageLoad(); h.flush();
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].url, '/api/telemetry/page-load');
  assert.deepEqual(JSON.parse(await h.sent[0].blob.text()), { page: 'industry', load_ms: 1200, dom_ms: 800, ttfb_ms: 50 });
  assert.equal(h.sent[0].blob.type, 'application/json');
  for (const options of [{ state: 'admin' }, { state: 'hidden' }, { navigator: { doNotTrack: '1' } },
    { navigator: { globalPrivacyControl: true } }, { navigator: { webdriver: true } }, { now: 100000 }]) {
    const disabled = harness(options); disabled.sandbox.profileInitialPageLoad(); disabled.flush();
    assert.equal(disabled.sent.length, 0, JSON.stringify(options));
  }
  const delayed = harness({ loading: true });
  delayed.nav.loadEventEnd = 0;
  delayed.sandbox.profileInitialPageLoad(); delayed.sandbox.profileInitialPageLoad();
  assert.equal(delayed.listeners.length, 1);
  delayed.sandbox.currentTab = () => 'admin';
  delayed.nav.loadEventEnd = 1200; delayed.listeners[0](); delayed.flush();
  assert.equal(JSON.parse(await delayed.sent[0].blob.text()).page, 'industry', 'keep the initial page, not a later SPA tab');
  const optOut = harness({ loading: true }); optOut.sandbox.profileInitialPageLoad();
  optOut.sandbox.navigator.globalPrivacyControl = true; optOut.listeners[0](); optOut.flush();
  assert.equal(optOut.sent.length, 0);
  const unavailable = harness(); unavailable.sandbox.navigator.sendBeacon = () => { throw new Error('unavailable'); };
  unavailable.sandbox.profileInitialPageLoad(); unavailable.flush();
  console.log('Initial browser profiling privacy, timings and single-send behavior passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
