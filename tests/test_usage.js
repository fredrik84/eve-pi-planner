'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const planetary = fs.readFileSync('static/planetary.js', 'utf8');
const analysis = fs.readFileSync('static/analysis.js', 'utf8');
const admin = fs.readFileSync('static/admin.js', 'utf8');
function fn(source, name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert(match, name);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}
async function main() {
  let requests = [];
  const send = { _loggedIn: true, _features: { pi_usage_stats: { state: 'public' } }, navigator: {},
    fetch: (...args) => { requests.push(args); return Promise.resolve(); } };
  vm.createContext(send);
  vm.runInContext(fn(planetary, 'recordPiUsage'), send);
  send.recordPiUsage('wizard_started');
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/telemetry/usage');
  assert.deepEqual(JSON.parse(requests[0][1].body), { event: 'wizard_started' });
  for (const control of ['doNotTrack', 'globalPrivacyControl', 'webdriver']) {
    send.navigator[control] = control === 'doNotTrack' ? '1' : true;
    send.recordPiUsage('wizard_started');
    assert.equal(requests.length, 1);
    delete send.navigator[control];
  }
  send._loggedIn = false;
  send.recordPiUsage('wizard_started');
  send._loggedIn = true;
  for (const state of ['admin', 'off', 'tester']) {
    send._features.pi_usage_stats.state = state;
    send.recordPiUsage('wizard_started');
  }
  assert.equal(requests.length, 1);
  send._features.pi_usage_stats.state = 'public';
  send.fetch = () => { throw Error('offline'); };
  assert.doesNotThrow(() => send.recordPiUsage('wizard_started'));
  send.fetch = () => Promise.reject(Error('offline'));
  send.recordPiUsage('wizard_started');
  await new Promise(resolve => setImmediate(resolve));

  const events = [], elements = {};
  const ctx = {
    recordPiUsage: e => events.push(e), _loggedIn: true,
    _wiz: { typeId: 123, lastRecsData: { product: { name: 'Test' } } },
    _planRequest: () => ({ url: '/api/plan', body: {} }),
    apiSend: async () => ({ product: { name: 'Test' } }),
    document: { getElementById: id => elements[id] ||= {}, querySelectorAll: () => [] },
    renderRecommendations: () => {}, renderFinalPlan: () => {}, wizardGo: () => {},
    toast: () => {}, toastError: () => {},
    api: async () => ({ plans: [{ name: 'A' }, { name: 'B' }] }),
    _buildPlanSnapshot: () => ({ name: 'Test' }), _buildPlanPayload: () => ({}),
    renderSavedPlansBar: () => {}, prompt: () => 'Saved', setTimeout: () => {},
  };
  vm.createContext(ctx);
  for (const name of ['wizardFindSystems', 'wizardChooseSystems', 'importCurrentSetupAsPlan']) {
    vm.runInContext(fn(planetary, name), ctx);
  }
  vm.runInContext(fn(analysis, 'savePlanForRefills'), ctx);
  await ctx.wizardFindSystems();
  assert.deepEqual(events.splice(0), ['wizard_started', 'wizard_recommendations']);
  await ctx.wizardChooseSystems([]);
  await ctx.wizardChooseSystems(['Test']);
  assert.deepEqual(events.splice(0), ['wizard_completed', 'wizard_completed']);
  await ctx.wizardChooseSystems(['Test'], false);
  assert.equal(events.length, 0, 'restoring an old plan is not completing a new wizard run');
  await ctx.savePlanForRefills();
  assert.deepEqual(events.splice(0), ['wizard_saved']);
  await ctx.importCurrentSetupAsPlan();
  assert.deepEqual(events.splice(0), ['setup_imported'], 'one import action even when saving two plans');
  let attempt = 0;
  ctx.apiSend = async () => { if (++attempt === 1) throw Error('failed'); };
  await ctx.importCurrentSetupAsPlan();
  assert.deepEqual(events.splice(0), ['setup_imported'], 'a partial import still records successful use');
  ctx.apiSend = async () => { throw Error('failed'); };
  await ctx.wizardFindSystems();
  await ctx.wizardChooseSystems(['Test']);
  await ctx.savePlanForRefills();
  await ctx.importCurrentSetupAsPlan();
  assert.deepEqual(events.splice(0), ['wizard_started'], 'failed actions never count as successes');
  ctx._wiz.typeId = null;
  await ctx.wizardFindSystems();
  ctx.api = async () => ({ plans: [] });
  await ctx.importCurrentSetupAsPlan();
  assert.equal(events.length, 0);

  const section = { style: {} }, content = {};
  ctx.document.getElementById = id => id === 'adminPiUsageSection' ? section : content;
  ctx._featureActive = () => true;
  ctx._esc = value => String(value).replaceAll('<', '&lt;');
  const period = { rows: [{ label: '<test>', accounts: 2, actions: 5 }],
    adoption: { wizard_only: 1, import_only: 1, both: 1, neither: 0 } };
  ctx.api = async () => ({ visible: true, enabled: false, periods: { 7: period, 30: period } });
  vm.runInContext(fn(admin, 'loadPiUsageStats'), ctx);
  await ctx.loadPiUsageStats();
  assert.match(content.innerHTML, /Collection is off/);
  assert.match(content.innerHTML, /7d accounts/);
  assert.match(content.innerHTML, /30d actions/);
  assert.match(content.innerHTML, /&lt;test>/);
  ctx.api = async () => ({ visible: false });
  await ctx.loadPiUsageStats();
  assert.equal(section.style.display, 'none');
  console.log('PI usage measurements: all checks passed');
}
main().catch(err => { console.error(err); process.exitCode = 1; });
