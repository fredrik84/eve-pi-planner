// Browser-free checks of the actual global submission review banner and its refresh hooks.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, 'static', name), 'utf8');
function source(file, name) {
  const src = read(file);
  const at = src.indexOf(`function ${name}(`);
  const end = src.indexOf('\n}\n', at);
  assert(at >= 0 && end > at, `missing ${name}`);
  return (src.slice(at - 6, at) === 'async ' ? 'async ' : '') + src.slice(at, end + 3);
}
const banner = {
  innerHTML: '', hidden: true, buttons: [],
  querySelectorAll() {
    this.buttons = [...this.innerHTML.matchAll(/data-review-id="([^"]+)"/g)].map(match => ({
      dataset: { reviewId: match[1] }, disabled: false,
      addEventListener(event, listener) { assert.equal(event, 'click'); this.click = listener; },
    }));
    return this.buttons;
  },
};
let response = { reviews: [] };
let active = true;
let reads = 0;
let ackError = false;
let errors = 0;
const sent = [];
const sandbox = {
  _loggedIn: true,
  _featureActive: () => active,
  document: { hidden: false, getElementById: () => banner },
  _esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  api: async url => {
    assert.equal(url, '/api/planet-submissions/reviews');
    reads++;
    if (response instanceof Error) throw response;
    return response;
  },
  apiSend: async (method, url) => {
    assert.equal(method, 'POST');
    sent.push(url);
    if (ackError) throw new Error('failed');
    response = { reviews: [] };
  },
  toastError: () => { errors++; },
  encodeURIComponent,
  refreshCharacterHealth() {},
  currentTab: () => 'planner',
  _lastAutoRefresh: 0,
};
vm.createContext(sandbox);
vm.runInContext('let _planetReviewsInFlight = null;', sandbox);
for (const fn of ['renderPlanetSubmissionReviews', 'refreshPlanetSubmissionReviews', 'dismissPlanetSubmissionReview']) {
  vm.runInContext(source('planetdb.js', fn), sandbox);
}
vm.runInContext(source('app.js', '_autoRefreshTick'), sandbox);
const review = (status, id = 1) => ({ id, status, planet_count: 3, created_at: '2026-10-19 12:00:00' });
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  response = { reviews: [review('approved'), review('rejected', 2)] };
  await sandbox.refreshPlanetSubmissionReviews();
  assert(!banner.hidden);
  assert.match(banner.innerHTML, /submission #1 was approved/);
  assert.match(banner.innerHTML, /submission #2 was rejected/);
  assert.match(banner.innerHTML, /3 planets · Submitted 2026-10-19 12:00:00 EVE time/);
  assert.equal(sent.length, 0, 'displaying reviews never dismisses them');
  const previous = banner.innerHTML;
  response = new Error('offline');
  await sandbox.refreshPlanetSubmissionReviews();
  assert.equal(banner.innerHTML, previous, 'network failure retains unread results');

  ackError = true;
  const btn = banner.buttons[0];
  await btn.click();
  assert.equal(banner.innerHTML, previous, 'failed acknowledgment keeps the result');
  assert.equal(errors, 1);
  assert.equal(btn.disabled, false);
  ackError = false;
  await sandbox.dismissPlanetSubmissionReview(1, btn);
  assert.equal(sent.at(-1), '/api/planet-submissions/1/acknowledge');
  assert(banner.hidden);

  active = false;
  let before = reads;
  await sandbox.refreshPlanetSubmissionReviews();
  assert.equal(reads, before, 'disabled flag makes no request');
  active = true;
  sandbox._loggedIn = false;
  await sandbox.refreshPlanetSubmissionReviews();
  assert.equal(reads, before, 'logged-out visitors make no request');
  assert(banner.hidden);
  sandbox._loggedIn = true;

  response = { reviews: [review('approved')] };
  sandbox._autoRefreshTick();
  await flush();
  assert(!banner.hidden, 'new reviews appear while on any page');
  assert.equal(reads, before + 1);
  sandbox.document.hidden = true;
  sandbox._autoRefreshTick();
  await flush();
  assert.equal(reads, before + 1, 'hidden pages do not poll');

  sandbox.renderPlanetSubmissionReviews([{ ...review('approved'), id: '<img>', created_at: '<script>' }]);
  assert(!banner.innerHTML.includes('<img>'));
  assert(!banner.innerHTML.includes('<script>'));
  assert(banner.innerHTML.includes('&lt;script&gt;'));

  // Coalesce concurrent refreshes and wait out a stale read before reloading after dismissal.
  let release;
  response = new Promise(resolve => { release = resolve; });
  before = reads;
  const poll = sandbox.refreshPlanetSubmissionReviews();
  const shared = sandbox.refreshPlanetSubmissionReviews();
  assert.equal(reads, before + 1);
  const dismissal = sandbox.dismissPlanetSubmissionReview(1, btn);
  release({ reviews: [review('approved')] });
  await Promise.all([poll, shared, dismissal]);
  assert(banner.hidden, 'a stale poll must not restore an acknowledged result');
  assert.equal(reads, before + 2);

  const boot = source('planetary.js', 'loadCharacters');
  assert(boot.indexOf('refreshPlanetSubmissionReviews()') > boot.indexOf('await _loadFeatures()'),
    'boot loads review results after identity and rollout flags');
  assert.match(read('index.html'), /id="planetSubmissionReviews"[^>]*role="status"[^>]*hidden/);
  console.log('Planet submission review banner: all checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
