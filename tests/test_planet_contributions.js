'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function fn(source, name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert(match, name);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}
async function main() {
  const nodes = {}, messages = [];
  let loads = 0, closed = false;
  let result = { queued: true, imported: 2, submitted: 1, unchanged: 3, skipped: 0 };
  const ctx = {
    document: { getElementById: id => nodes[id] ||= { value: 'paste', disabled: false } },
    apiSend: async () => result,
    toast: message => messages.push(message), toastError: () => {},
    closePlanetImport: () => { closed = true; },
    loadPlanets: async () => { loads++; }, loadConstellations: () => {}, setTimeout: () => {},
    _esc: value => String(value).replaceAll('<', '&lt;'),
  };
  vm.createContext(ctx);
  vm.runInContext(fn(fs.readFileSync('static/planetdb.js', 'utf8'), 'submitPlanetImport'), ctx);
  await ctx.submitPlanetImport();
  assert.match(messages.pop(), /Added 2 new planets.*1 existing planet.*3 unchanged/);
  assert.equal(loads, 1, 'mixed submissions refresh the newly added live planets immediately');
  assert.equal(closed, true);
  assert.equal(nodes.ppImportSubmitBtn.disabled, false);
  result = { queued: false, imported: 0, unchanged: 1, skipped: 0 };
  await ctx.submitPlanetImport();
  assert.match(messages.pop(), /0, 1 unchanged/);
  result = { queued: true, imported: 0, submitted: 1, unchanged: 0, skipped: 0 };
  loads = 0;
  await ctx.submitPlanetImport();
  assert.equal(loads, 0, 'queued-only changes have not changed the live database');
  closed = false;
  ctx.apiSend = async () => { throw Error('failed'); };
  await ctx.submitPlanetImport();
  assert.equal(closed, false);
  assert.equal(nodes.ppImportSubmitBtn.disabled, false);

  vm.runInContext(fn(fs.readFileSync('static/admin.js', 'utf8'), 'renderPlanetSubmissions'), ctx);
  ctx.renderPlanetSubmissions([{ id: 1, planet_count: 1, submitter_name: '<pilot>',
    planets: [{ system: '<system>', planet_num: 1, planet_type: 'Barren', exists: true,
      changes: [{ label: 'Base Metals', old: 20, new: 55 }] }] }]);
  assert.match(nodes.planetSubList.innerHTML, /Base Metals: <b>20<\/b> → <b>55<\/b>/);
  assert.match(nodes.planetSubList.innerHTML, /&lt;system>/);
  assert(!nodes.planetSubList.innerHTML.includes('<pilot>'));
  console.log('Planet contributions: all checks passed');
}
main().catch(err => { console.error(err); process.exitCode = 1; });
