// Run the real header/health refresh code without a browser: node tests/test_character_health.js
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
function source(file, name) {
  const src = fs.readFileSync(path.join(root, 'static', file), 'utf8');
  const at = src.indexOf(`function ${name}(`);
  const end = src.indexOf('\n}\n', at);
  assert(at >= 0 && end > at, `missing ${name}`);
  return (src.slice(at - 6, at) === 'async ' ? 'async ' : '') + src.slice(at, end + 3);
}
const banner = { innerHTML: '', hidden: true };
let response;
const opened = [];
const sandbox = {
  document: {
    hidden: false,
    getElementById: id => id === 'characterHealthAlert' ? banner : null,
    documentElement: { classList: { toggle() {} } },
  },
  localStorage: { setItem() {} },
  _esc: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  _isAdmin: false, _isGroupManager: false, _loggedIn: true, _ppCharsData: [],
  _rescanning: false, _lastAutoRefresh: 0,
  currentTab: () => 'planner',
  api: async () => { if (response instanceof Error) throw response; return response; },
  renderCharacters() {}, openSettingsModal: section => opened.push(section), Date,
};
vm.createContext(sandbox);
for (const fn of ['renderCharacterHealthAlert', 'renderHeaderSession', 'refreshCharacterHealth']) {
  vm.runInContext(source('planetary.js', fn), sandbox);
}
vm.runInContext(source('app.js', '_autoRefreshTick'), sandbox);
const toon = (id, name, extra = {}) => ({ character_id: id, name, token_ok: true, ...extra });
const render = (chars, loggedIn = true) => sandbox.renderHeaderSession(loggedIn, chars, 1);

(async () => {
  render([toon(1, 'Healthy')]);
  assert(banner.hidden);
  const dead = toon(2, '<Expired & Alt>', { token_ok: false, scan_failed_at: 123 });
  render([dead, toon(-1, 'Placeholder', { is_dummy: true, token_ok: false })]);
  assert(!banner.hidden);
  assert.match(banner.innerHTML, /Reauthorization required: &lt;Expired &amp; Alt&gt;/);
  assert(!banner.innerHTML.includes('Placeholder'));
  assert(!banner.innerHTML.includes('Data refresh failed'));
  const action = banner.innerHTML.match(/onclick="([^"]+)"/)[1];
  vm.runInContext(action, sandbox);
  assert.deepEqual(opened, ['characters']);
  render([toon(3, 'Temporary failure', { scan_failed_at: 123 })]);
  assert.match(banner.innerHTML, /Data refresh failed: Temporary failure/);
  assert(!banner.innerHTML.includes('Reauthorization required'));
  render([toon(4, 'Reaction alt', { reactions_scope_lost: true })]);
  assert.match(banner.innerHTML, /Reaction tracking disconnected: Reaction alt/);
  render([toon(5, 'Wallet alt', { wallet_only: true, token_ok: false })]);
  assert.match(banner.innerHTML, /Reauthorization required: Wallet alt/);
  render([dead], false);
  assert(banner.hidden && banner.innerHTML === '');

  response = { logged_in: true, characters: [dead], session_character_id: 1 };
  sandbox._autoRefreshTick();
  await new Promise(resolve => setImmediate(resolve));
  assert(!banner.hidden, 'a background failure must surface outside Dashboard and Settings');
  assert.equal(sandbox._ppCharsData[0].character_id, 2);
  const previous = banner.innerHTML;
  response = new Error('temporary network failure');
  await sandbox.refreshCharacterHealth();
  assert.equal(banner.innerHTML, previous);
  response = { logged_in: false, characters: [] };
  await sandbox.refreshCharacterHealth();
  assert.equal(banner.innerHTML, previous, 'an unexpected background logout must preserve the warning');
  response = { logged_in: true, characters: [toon(2, 'Reconnected')], session_character_id: 1 };
  await sandbox.refreshCharacterHealth();
  assert(banner.hidden && banner.innerHTML === '', 'reauthorization or recovery clears the warning');
  console.log('Character health alerts: all checks passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
