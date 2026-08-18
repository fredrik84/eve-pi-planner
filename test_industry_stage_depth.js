/**
 * TODO §39: the build pipeline staged a node by how far the tree WALK was from the root when it
 * first reached that node, not by how many build layers actually sit below it. Reported live
 * (2026-08-18): Stage 3 grouped Pressurized Oxidizers and Reinforced Carbon Fiber (which genuinely
 * need Stage 2 output) together with Rolled Tungsten Alloy, Dysporite, Caesarium Cadmide and
 * Promethium Mercurite (which need nothing but fuel blocks) — the four simple reactions were direct
 * root ingredients, so the old `Math.max(tier, depth-from-root)` model read their SHALLOW tree
 * position as a LATE stage, however few production steps their own recipe needed.
 *
 * Runs the real `_indComputeTiers`/`_indStageModel` from `static/industry-shopping.js` in a `vm`
 * context — no DOM needed, both functions are pure.
 *
 *     node test_industry_stage_depth.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'static', 'industry-shopping.js'), 'utf8');
const fails = [];

function check(cond, msg) {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) fails.push(msg);
}

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox, { filename: 'static/industry-shopping.js' });

// A node as build_plan's tree shape: type_id, name, decision, qty, runs, inputs: [...].
const buy = (type_id, name, qty) => ({ type_id, name, decision: 'buy', qty, runs: 0, inputs: [] });
const build = (type_id, name, qty, runs, inputs) =>
  ({ type_id, name, decision: 'build', qty, runs, inputs });

// Fuel blocks (bought, Reactions' own product — Manufacturing just consumes it).
const FUEL = buy(4247, 'Nitrogen Fuel Block', 1000);

// Reinforced Carbon Fiber needs a Stage-2 intermediate (Fernite Carbide) plus fuel blocks; that
// intermediate itself needs only fuel blocks — same shape reported live.
const FERNITE_CARBIDE = build(16671, 'Fernite Carbide', 100, 1, [buy(16633, 'Titanium Chromide', 100)]);
const REINFORCED_CARBON_FIBER = build(16672, 'Reinforced Carbon Fiber', 100, 1,
  [FUEL, { ...FERNITE_CARBIDE }]);
const PRESSURIZED_OXIDIZERS = build(16673, 'Pressurized Oxidizers', 100, 1,
  [FUEL, { ...FERNITE_CARBIDE }]);

// The four simple reactions: fuel blocks only, no intermediate — direct ROOT ingredients, which is
// exactly the shape that fooled the old depth-from-root model.
const ROLLED_TUNGSTEN_ALLOY = build(16674, 'Rolled Tungsten Alloy', 100, 1, [{ ...FUEL }]);
const DYSPORITE = build(16675, 'Dysporite', 100, 1, [{ ...FUEL }]);
const CAESARIUM_CADMIDE = build(16676, 'Caesarium Cadmide', 100, 1, [{ ...FUEL }]);
const PROMETHIUM_MERCURITE = build(16677, 'Promethium Mercurite', 100, 1, [{ ...FUEL }]);

const ROOT = build(11000, 'Some T2 Hull', 1, 1, [
  REINFORCED_CARBON_FIBER, PRESSURIZED_OXIDIZERS,
  ROLLED_TUNGSTEN_ALLOY, DYSPORITE, CAESARIUM_CADMIDE, PROMETHIUM_MERCURITE,
]);

const tiersData = vm.runInContext('_indComputeTiers(ROOT, new Set())', Object.assign(sandbox, { ROOT }));
const model = vm.runInContext('_indStageModel(tiersData)', Object.assign(sandbox, { tiersData }));

const tierOf = tid => tiersData.byType[tid].tier;

check(tierOf(16671) === 0, 'Fernite Carbide (needs only fuel blocks) sits at the earliest build tier');
check(tierOf(16674) === 0 && tierOf(16675) === 0 && tierOf(16676) === 0 && tierOf(16677) === 0,
      'the four fuel-block-only reactions sit at that SAME earliest tier, not one stage late');
check(tierOf(16672) === 1 && tierOf(16673) === 1,
      'Reinforced Carbon Fiber and Pressurized Oxidizers sit ONE stage later — they genuinely need Fernite Carbide');
check(tierOf(16672) > tierOf(16674), 'and that later stage is strictly after the simple reactions, not merged with them');
check(tierOf(11000) > tierOf(16672), 'the root (finished hull) is later still — it needs Stage 2\'s output');

const cols = model.cols;
check(cols.length === 3, `three build stages total, got ${cols.length}`);
const stage1Names = (cols[0].builds || []).map(b => b.name).sort();
check(JSON.stringify(stage1Names) ===
      JSON.stringify(['Caesarium Cadmide', 'Dysporite', 'Fernite Carbide', 'Promethium Mercurite', 'Rolled Tungsten Alloy'].sort()),
      `Stage 1 holds every fuel-block-only build together, got ${JSON.stringify(stage1Names)}`);
const stage2Names = (cols[1].builds || []).map(b => b.name).sort();
check(JSON.stringify(stage2Names) === JSON.stringify(['Pressurized Oxidizers', 'Reinforced Carbon Fiber'].sort()),
      `Stage 2 holds exactly the two that need Stage 1's intermediate, got ${JSON.stringify(stage2Names)}`);
check(cols[2].label === 'Finished' && cols[2].builds[0].name === 'Some T2 Hull',
      'the root is its own terminal "Finished" column');

// A shallow single-hop build (no intermediates at all) must still get one "Finished" stage.
const TRIVIAL_ROOT = build(11001, 'Trivial Item', 1, 1, [buy(34, 'Tritanium', 100)]);
const trivialTiers = vm.runInContext('_indComputeTiers(TRIVIAL_ROOT, new Set())',
  Object.assign(sandbox, { TRIVIAL_ROOT }));
const trivialModel = vm.runInContext('_indStageModel(trivialTiers)', Object.assign(sandbox, { trivialTiers }));
check(trivialModel.cols.length === 1 && trivialModel.cols[0].label === 'Finished',
      'a one-hop build (no intermediates) is a single Finished stage, not zero or many');

// A bought material needed by two builds at different stages files under the EARLIER one.
const EARLY_BUILD = build(11002, 'Early Build', 10, 1, [buy(50, 'Shared Bought Material', 10)]);
const LATE_BUILD = build(11003, 'Late Build', 10, 1, [{ ...EARLY_BUILD }, buy(50, 'Shared Bought Material', 5)]);
const MULTI_ROOT = [LATE_BUILD];
const multiTiers = vm.runInContext('_indComputeTiers(MULTI_ROOT, new Set())',
  Object.assign(sandbox, { MULTI_ROOT }));
const multiModel = vm.runInContext('_indStageModel(multiTiers)', Object.assign(sandbox, { multiTiers }));
const sharedStage = multiModel.stageOf[50];
check(sharedStage === multiModel.cols[0].t,
      'a bought material needed by both an early and a late build is filed under the EARLY stage — buy it in time');

console.log('\n' + (fails.length ? 'FAILED: ' + fails.join('; ') : 'all checks passed'));
process.exit(fails.length ? 1 : 0);
