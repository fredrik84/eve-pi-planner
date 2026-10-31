'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const text = fs.readFileSync(path.join(__dirname, '../static/admin.js'), 'utf8');
const start = text.indexOf('function renderReferralStats(');
const end = text.indexOf('\nasync function _checkPrometheusStatus(', start);
assert(start >= 0 && end > start);
const section = { style: {} };
const content = { innerHTML: '' };
const sandbox = {
  document: { getElementById: id => id === 'adminReferralSection' ? section : content },
  _esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
};
vm.createContext(sandbox);
vm.runInContext(text.slice(start, end), sandbox);
sandbox.renderReferralStats({ visible: false });
assert.equal(section.style.display, 'none');
sandbox.renderReferralStats({ visible: true, enabled: true, available: true, rows: [
  { label: 'Google', last_7d: 7, last_30d: 30 },
  { label: 'Direct / unknown', last_7d: 3, last_30d: 12 },
  { label: 'Bing', last_7d: 0, last_30d: 0 },
] });
assert.equal(section.style.display, '');
assert.match(content.innerHTML, /Google/);
assert.match(content.innerHTML, /Last 7 days/);
assert.match(content.innerHTML, /Last 30 days/);
assert(!content.innerHTML.includes('Bing'));
assert.match(content.innerHTML, /not unique people or new accounts/);
assert.match(content.innerHTML, /word of mouth/);
sandbox.renderReferralStats({ visible: true, enabled: false, available: true, rows: [] });
assert.match(content.innerHTML, /Collection is off/);
assert.match(content.innerHTML, /No page arrivals recorded yet/);
sandbox.renderReferralStats({ visible: true, available: false, message: '<img src=x>' });
assert(!content.innerHTML.includes('<img'));
assert.match(content.innerHTML, /&lt;img/);
console.log('Referral statistics rendering: all checks passed');
