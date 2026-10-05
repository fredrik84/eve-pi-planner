const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const context = vm.createContext({
  localStorage: { getItem: () => null },
  _featureActive: () => true,
  _esc: s => String(s),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/analysis.js'), 'utf8'), context);
const run = source => vm.runInContext(source, context);
const value = source => JSON.parse(JSON.stringify(run(source)));
run(`
  _redeploy = { proximity: [
    { characters: ['A', 'B'], overlap_pct: 60 },
    { characters: ['B', 'C'], overlap_pct: 90 },
    { characters: ['C', 'D'], overlap_pct: 5 },
    { characters: ['D', 'E'], overlap_pct: 3 },
  ].map(p => ({ ...p, planet_id: 1, p0_name: 'Aqueous Liquids', p1_name: 'Water', location: 'Test P1' })) };
  _extSupplyOf = () => 100;
  _producersOf = () => ['A', 'B', 'C', 'D', 'E'].map((char, i) => ({
    char, character_id: i + 1, planet_id: 1, extPerDay: (i + 1) * 10, perDay: (i + 1) * 10,
  }));
  _rescanBtn = () => '';
`);
assert.deepEqual(value("_overlapClustersFor('Water').map(c => c.characters)"), [['A', 'B', 'C']],
  'weak links cannot pull remote colonies into a strong overlap group');
assert.deepEqual(value("[..._overlapBlockedKeys('Water')].sort()"), ['1|A', '1|B', '1|C'],
  'weakly linked colonies remain eligible for reseat advice');
assert.deepEqual(value("_overlapClustersFor('Oxygen')"), [], 'resource isolation');
const rows = "[{ t: 1, name: 'Water', have: 100, need: 1000 }]";
const move = value(`_redeployPlan(${rows}).chosen[0]`);
assert.equal(move.character, 'A');
assert.deepEqual(move.neighbours, ['B'], 'A is not told to avoid transitive neighbour C');
assert.equal(move.overlap_pct, 60, 'A does not inherit the stronger B–C percentage');
assert.equal(move.rec, 18, 'recovery estimate excludes unrelated group members');
const html = run(`_renderRedeployUrgent(${rows})`);
assert.ok(html.includes('estimated footprint overlap 60%'));
assert.ok(html.includes('does not confirm shared hotspots'));
assert.ok(!html.includes('the two fight'));
assert.ok(!html.includes('<b>C</b>'));
run("_colonyFlags.add('1|1')");
assert.deepEqual(value(`_redeployPlan(${rows}).chosen[0].neighbours`), ['B'],
  'user-flagged relocation also lists only direct neighbours');
run("_colonyFlags.clear(); _redeploy.depleting = [{ character: 'A', character_id: 1, planet_id: 1, p1_name: 'Water', reseat_tracked: true, reseats_confirmed: 2 }]");
assert.deepEqual(value(`_redeployPlan(${rows}).chosen[0].neighbours`), ['B'],
  'automatic relocation also lists only direct neighbours');
run("_redeploy.proximity = [{ characters: ['A', 'B'], overlap_pct: 39, planet_id: 1, p1_name: 'Water' }]");
assert.deepEqual(value("_overlapClustersFor('Water')"), [], 'below-threshold pair is excluded');
run('_redeploy.proximity[0].overlap_pct = 40');
assert.equal(run("_overlapClustersFor('Water').length"), 1, 'threshold boundary is included');
run('_redeploy = null');
assert.deepEqual(value("_overlapClustersFor('Water')"), [], 'missing scan data is safe');
console.log('Setup Analysis overlap regression tests passed');
