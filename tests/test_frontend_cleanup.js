// The manual-override panel and shared print library have different jobs. Bulk batches belong
// to the shared library; refreshing overrides must not build a second, discarded batch list.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, 'static', file), 'utf8');
function functionSource(file, name) {
  const code = read(file);
  const at = code.indexOf(`function ${name}(`);
  const end = code.indexOf('\n}\n', at);
  assert(at >= 0 && end > at, `missing ${name}`);
  return (code.slice(at - 6, at) === 'async ' ? 'async ' : '') + code.slice(at, end + 3);
}

(async () => {
  const elements = {
    indManualBpSubsec: { style: {} }, indManualBps: { innerHTML: '' },
    'indbpb-batch': { innerHTML: '', style: { display: 'none' } },
  };
  const entries = [
    { id: 1, name: 'Manual hull', quantity: 1, me: 10, te: 20, kind: 'bpo' },
    { id: 2, name: 'Shared formula', batch: 'batch', quantity: 2, kind: 'formula' },
  ];
  const response = { entries };
  Object.defineProperty(response, 'batches', { get() { throw Error('Overrides must not render bulk batches'); } });
  const calls = [];
  const context = {
    document: { getElementById: id => elements[id] || null },
    _featureActive: () => true, _esc: value => String(value), _srcDomId: value => value,
    api: async url => { calls.push(url); return response; },
  };
  vm.createContext(context);
  vm.runInContext(read('industry-blueprints.js'), context);
  await context.indLoadManualBps();
  assert.deepEqual(calls, ['/api/industry/manual-blueprints']);
  assert.match(elements.indManualBps.innerHTML, /Manual hull/);
  assert.doesNotMatch(elements.indManualBps.innerHTML, /Shared formula/);
  assert.match(elements.indManualBps.innerHTML, /indSaveManualBp/);
  context.indToggleBatchItems('batch');
  assert.match(elements['indbpb-batch'].innerHTML, /Shared formula/);
  assert.equal(elements['indbpb-batch'].style.display, '');
  context.indToggleBatchItems('batch');
  assert.equal(elements['indbpb-batch'].style.display, 'none');
  context._featureActive = () => false;
  await context.indLoadManualBps();
  assert.equal(elements.indManualBpSubsec.style.display, 'none');
  assert.equal(calls.length, 1, 'hidden manual edits make no API call');

  // Removing an unused response binding must preserve both permission mutations and error recovery.
  for (const allowed of [true, false]) {
    let reloads = 0;
    const requests = [];
    Object.assign(context, {
      apiSend: async (...args) => { requests.push(args); }, loadGroups: () => { reloads++; }, toastError() {},
    });
    vm.runInContext(functionSource('admin.js', 'toggleGroupPage'), context);
    const checkbox = { checked: allowed, disabled: false };
    await context.toggleGroupPage(7, 'industry', allowed, checkbox);
    assert.equal(requests[0][0], allowed ? 'POST' : 'DELETE');
    assert.equal(reloads, 1);
    context.apiSend = async () => { throw Error('offline'); };
    await context.toggleGroupPage(7, 'industry', allowed, checkbox);
    assert.equal(checkbox.checked, !allowed);
    assert.equal(checkbox.disabled, false);
    assert.equal(reloads, 1);
  }
  console.log('Manual overrides, shared batch contents, feature gating and permission mutations passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
