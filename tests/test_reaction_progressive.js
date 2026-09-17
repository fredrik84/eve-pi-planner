const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/reactions.js', 'utf8');
const loader = source.slice(source.indexOf('function _loadReactionsDashboard()'), source.indexOf('let _rxLifetime ='));
const tiles = source.slice(source.indexOf('  const pricesLoading ='), source.indexOf('  // Overall completion'));
const refresh = source.slice(source.indexOf('function _rxRefreshJobs('), source.indexOf('// Adopt an orphan running job'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(api) {
  const painted = [];
  const ctx = { document: { getElementById: () => ({ innerHTML: '' }) }, window: {},
    _rxLastDashboardData: null, _rxDashboardLoadId: 0, _rxCadenceDays: 1,
    _renderReactionsDashboard: data => painted.push(data),
    _rxErr: e => e, _esc: s => s, api: url => url.includes('lifetime') ? Promise.resolve(null) : api(url) };
  vm.createContext(ctx);
  vm.runInContext(loader, ctx);
  return { ctx, painted };
}

(async () => {
  for (const state of ['loading', 'error', 'ready']) {
    const ctx = { data: { pricing_state: state, pending_isk_committed: 123 },
      usedSlots: 1, totalSlots: 10, pendingCount: 2, timeLeftVal: '2h', timeLeftLbl: 'Time left',
      _dashTile: (value, label) => `${label}:${value}`, _fmtIsk: v => `${v} ISK` };
    vm.createContext(ctx);
    vm.runInContext(tiles + '\nglobalThis.html = overviewTiles;', ctx);
    if (state === 'loading') {
      assert.match(ctx.html, /pp-spinner/);
      assert.match(ctx.html, /Updating prices/);
      assert.doesNotMatch(ctx.html, /ISK/);
    } else if (state === 'error') {
      assert.match(ctx.html, /Unavailable/);
      assert.match(ctx.html, /Retry prices/);
      assert.doesNotMatch(ctx.html, /ISK/);
    } else assert.match(ctx.html, /123 ISK/);
  }
  let finish;
  const full = new Promise(resolve => { finish = resolve; });
  const h = harness(url => url.includes('include_prices')
    ? Promise.resolve({ pricing_state: 'loading', characters: [] }) : full);
  const done = h.ctx._loadReactionsDashboard();
  await tick();
  assert.equal(h.painted[0].pricing_state, 'loading');
  finish({ pricing_state: 'ready', pending_isk_committed: 123 });
  await done;
  assert.equal(h.painted.at(-1).pending_isk_committed, 123);

  const failed = harness(url => url.includes('include_prices')
    ? Promise.resolve({ pricing_state: 'loading', total_slots: 10 }) : Promise.reject(Error('offline')));
  await failed.ctx._loadReactionsDashboard();
  assert.equal(failed.painted.at(-1).pricing_state, 'error');
  assert.equal(failed.painted.at(-1).total_slots, 10);
  let retryFinished;
  // Keep the independent lifetime call out of this deferred price-response probe.
  failed.ctx.api = url => url.includes('lifetime') ? Promise.resolve(null)
    : new Promise(resolve => { retryFinished = resolve; });
  const retried = failed.ctx._loadReactionsDashboard();
  assert.equal(failed.painted.at(-1).pricing_state, 'loading');
  retryFinished({ pricing_state: 'ready', total_slots: 10 });
  await retried;
  assert.equal(failed.painted.at(-1).pricing_state, 'ready');

  let stale;
  const racing = harness(url => url.includes('include_prices')
    ? Promise.resolve({ pricing_state: 'loading' }) : new Promise(resolve => { stale = resolve; }));
  const pending = racing.ctx._loadReactionsDashboard();
  await tick();
  ++racing.ctx._rxDashboardLoadId;
  stale({ pricing_state: 'ready', pending_isk_committed: 999 });
  await pending;
  assert.equal(racing.painted.length, 1, 'old response must not repaint a newer view');

  let refreshed;
  const refreshing = harness(() => new Promise(resolve => { refreshed = resolve; }));
  refreshing.ctx.apiSend = () => Promise.resolve({ characters_refreshed: 1 });
  vm.runInContext(refresh, refreshing.ctx);
  const refreshDone = refreshing.ctx._rxRefreshJobs(false);
  await tick();
  ++refreshing.ctx._rxDashboardLoadId;
  refreshed({ pricing_state: 'ready', pending_isk_committed: 456 });
  await refreshDone;
  assert.equal(refreshing.painted.length, 0, 'refresh must not overwrite a newer dashboard load');
  console.log('Progressive dashboard: loading, completion, failure and stale-response checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
