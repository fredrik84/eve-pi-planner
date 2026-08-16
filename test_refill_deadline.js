/**
 * "Refill to a deadline" is arithmetic with four ceilings on it (time, launchpad space, the P1 you
 * actually hold, and whole runs), and every one of them changes the number the player types into
 * EVE. A string match cannot tell you the order they were applied in, so this RUNS the real
 * `_deadlineSplit` — `static/refill.js` is executed whole in a `vm` context with stubbed
 * `localStorage`/`document`, which is all it needs at load time.
 *
 * Node lives on the host, not in the web container, so this runs OUTSIDE:
 *
 *     node test_refill_deadline.js
 *
 * Every check here was watched go red against a mutated `_deadlineSplit`: drop the run-flooring,
 * drop the capacity clamp, drop the skip branch, apply the stock scale AFTER the flooring, use
 * plan-total × share instead of the factory's own rate, report the per-factory dry-out under the
 * uniform table, and blame a stock shortfall on the rounding.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'static', 'refill.js'), 'utf8');
const fails = [];

function check(cond, msg) {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) fails.push(msg);
}
const near = (a, b, tol) => Math.abs(a - b) <= (tol === undefined ? 0.5 : tol);

const HOUR = 3600000;
const RUN = 40;          // P1 units a basic factory eats per cycle — what amounts floor to
const RATE = 480;        // units/day of each P1, per factory (20/h)

/** One loaded copy of refill.js, with the plan/inventory/mode state a test wants. */
function load(opts) {
  const o = opts || {};
  const storage = {};
  const sandbox = {
    console,
    localStorage: {
      getItem: k => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
    // No DOM by default: every render path bails on a null element, leaving the math to be
    // inspected. Pass `dom` to capture innerHTML instead and exercise the rendering too.
    document: { getElementById: o.dom || (() => null), querySelectorAll: () => [] },
    _featureActive: () => (o.featureOff ? false : true),
    _esc: s => String(s),
    _fmtHours: h => String(h),
    _fmtIsk: v => String(v),
    _natCompare: (a, b) => String(a).localeCompare(String(b)),
    api: async () => ({}),
    Date,
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'static/refill.js' });
  const facs = (o.facs || [{}]).map((f, i) => ({
    loc: f.loc || `Char · SYS P${i + 1}`,
    char: 'Char',
    shareByTid: f.shareByTid || { '100': 1 / (o.facs || [{}]).length, '200': 1 / (o.facs || [{}]).length },
    rateByTid: 'rateByTid' in f ? f.rateByTid : { '100': RATE, '200': RATE },
    runByTid: 'runByTid' in f ? f.runByTid : { '100': RUN, '200': RUN },
    amt: {},
    inputM3: f.inputM3 === undefined ? null : f.inputM3,
  }));
  // refill.js's state is `let`-declared, so it lives in the context's declarative scope and is NOT
  // reachable as a sandbox property — seed it by assignment inside the context.
  sandbox.__st = {
    model: [{ title: 'Coolant', cols: [{ id: '100', name: 'Electrolytes' }, { id: '200', name: 'Water' }], facs }],
    names: { '100': 'Electrolytes', '200': 'Water' },
    consumption: o.consumption !== undefined ? o.consumption
      : [{ p1_type_id: 100, p1_name: 'Electrolytes', units_per_day: RATE * facs.length },
         { p1_type_id: 200, p1_name: 'Water', units_per_day: RATE * facs.length }],
    stacks: o.stacks || {},
    meta: { productsPerDay: 240, iskPerDay: 1e6, unitLabel: 'units', count: facs.length, refillHours: 100 },
    mode: o.mode || 'deadline',
    deadline: Date.now() + (o.deadlineH === undefined ? 48 : o.deadlineH) * HOUR,
    ignorePads: o.ignorePads === undefined ? true : o.ignorePads,
  };
  vm.runInContext(`
    _refillModel = __st.model; _p1TidToName = __st.names; _planConsumption = __st.consumption;
    _p1Stacks = __st.stacks; _planMeta = __st.meta; _refillMode = __st.mode;
    _refillDeadline = __st.deadline; _refillIgnorePads = __st.ignorePads;
    updateP1Distribution();
  `, sandbox);
  const read = expr => vm.runInContext(expr, sandbox);
  return { sandbox, facs, read, info: read('_deadlineInfo'), uniform: read('_refillUniform') };
}

// ── The plain case: the deadline decides the quantity ─────────────────────────────────────────
console.log('\na deadline, empty pads and stock to spare — the quantity is burn rate × time:');
{
  const t = load({ deadlineH: 48, stacks: { 100: 1e6, 200: 1e6 } });
  const f = t.facs[0];
  check(f.amt['100'] === 960 && f.amt['200'] === 960,
        '48h at 480/day = 960 of each (got ' + f.amt['100'] + '/' + f.amt['200'] + ')');
  check(near(t.info.roundDriftH, 0, 0.01), '960 is a whole number of 40-unit runs, so no rounding drift');
  check(t.info.capped.length === 0 && t.info.skip.length === 0 && !t.info.stockCapped,
        'no ceiling was hit, so nothing is flagged');
  check(t.uniform['100'] === 960, 'the Summary drop matches the only factory');
}

console.log('\nthe amount is floored to whole runs, and the time that costs is reported:');
{
  // 47.5h wants 950 units; the factory eats them 40 at a time, so 920 is the typeable number.
  const t = load({ deadlineH: 47.5, stacks: { 100: 1e6, 200: 1e6 } });
  const f = t.facs[0];
  check(f.amt['100'] === 920, '950 rounds down to 920 = 23 runs (got ' + f.amt['100'] + ')');
  check(near(t.info.roundDriftH, 1.5, 0.01), 'and it says so: 1.5h of dry-out is the rounding (got ' + t.info.roundDriftH + ')');
  check(near(t.info.endurance, 46, 0.01), 'endurance is measured from the ROUNDED amount, not the raw one');
}

// ── Capacity is a hard ceiling ────────────────────────────────────────────────────────────────
console.log('\na deadline the launchpads cannot reach gets the soonest one they CAN:');
{
  const t = load({ deadlineH: 24 * 400, stacks: { 100: 1e7, 200: 1e7 } });
  const f = t.facs[0];
  const capUnits = 30000 / 0.19;
  check(t.info.capped.length === 1, 'the factory is flagged as capacity-capped');
  check(f.amt['100'] + f.amt['200'] <= Math.floor(capUnits) && f.amt['100'] + f.amt['200'] > capUnits - 2 * RUN,
        'the drop fills the pads and does not overflow them (got ' + (f.amt['100'] + f.amt['200']) + ' vs ' + Math.round(capUnits) + ' units of space)');
  check(near(t.info.capped[0].hours, capUnits / (2 * RATE / 24), 1),
        'the reachable deadline is the pad-full endurance (got ' + Math.round(t.info.capped[0].hours) + 'h)');
  check(t.info.capped[0].hours < 24 * 400, '...which is sooner than the one asked for');
}

// ── A deadline shorter than the contents is not a refill ──────────────────────────────────────
console.log('\na factory already stocked past the deadline is a trip you should not make:');
{
  // 10,000 m³ of P1 in the pads ≈ 52,631 units; at 960/day that is ~55 days of burn.
  const t = load({ deadlineH: 24, ignorePads: false, facs: [{ inputM3: 10000 }], stacks: { 100: 1e6, 200: 1e6 } });
  check(t.info.skip.length === 1 && t.facs[0].skip === true, 'it is reported as a skip, not a 0-unit refill');
  check(t.facs[0].amt['100'] === 0, 'and nothing is allocated to it');
  check(near(t.info.comeBackH, (10000 / 0.19) / (2 * RATE / 24), 1),
        'with a come-back time from what it is holding (got ' + Math.round(t.info.comeBackH) + 'h)');
}

console.log('\npad contents count toward the deadline when you are not ignoring them:');
{
  const t = load({ deadlineH: 48, ignorePads: false, facs: [{ inputM3: 190 }], stacks: { 100: 1e6, 200: 1e6 } });
  // 190 m³ = 1,000 units split in burn ratio = 500 of each; 960 − 500 = 460 → 11 runs = 440.
  check(t.facs[0].amt['100'] === 440,
        'what is already in the pad is subtracted before rounding (got ' + t.facs[0].amt['100'] + ')');
}

// ── Your stock is the other ceiling ───────────────────────────────────────────────────────────
console.log('\nnot enough pasted P1 scales the split, and says what you are short:');
{
  // 500 held against a 960 need: the scale lands on 500, which is NOT a whole run — so a build
  // that scaled after flooring would hand back 500 here instead of 480.
  const t = load({ deadlineH: 48, stacks: { 100: 500, 200: 1e6 } });
  const f = t.facs[0];
  check(t.info.stockCapped, 'the shortfall is flagged');
  check(f.amt['100'] === 480, 'scaled to the 500 you hold, then floored to 12 runs (got ' + f.amt['100'] + ')');
  check(f.amt['100'] % RUN === 0 && f.amt['200'] % RUN === 0,
        'both are whole runs — the scale is applied BEFORE the flooring');
  check(t.info.shortfall.some(s => s.tid === '100' && s.short === 460), 'short by 460 Electrolytes, named');
  check(t.info.roundDriftH < t.info.hours - t.info.endurance,
        'and the shortfall is NOT blamed on the rounding (round drift ' + t.info.roundDriftH.toFixed(2)
        + 'h of a ' + (t.info.hours - t.info.endurance).toFixed(2) + 'h gap)');
}

console.log('\nnothing pasted at all is the "how much do I bring" question, not a table of zeroes:');
{
  const t = load({ deadlineH: 48, stacks: {} });
  check(t.facs[0].amt['100'] === 960, 'the full requirement is shown (got ' + t.facs[0].amt['100'] + ')');
  check(!t.info.stockCapped, 'and it is not reported as a shortfall');
}

// ── Several factories ─────────────────────────────────────────────────────────────────────────
console.log('\nwith several factories each gets its own answer, and Summary fits all of them:');
{
  const t = load({
    deadlineH: 48, ignorePads: false, stacks: { 100: 1e6, 200: 1e6 },
    facs: [{ inputM3: null }, { inputM3: 190 }],
  });
  check(t.facs[0].amt['100'] === 960 && t.facs[1].amt['100'] === 440,
        'the fuller factory is told to take less (got ' + t.facs[0].amt['100'] + '/' + t.facs[1].amt['100'] + ')');
  check(t.uniform['100'] === 440, 'the uniform drop is the smallest of them, so it fits everywhere');
  // The Summary view hands the 440 to BOTH factories, so the empty-padded one runs dry in 22h —
  // the readout under that table has to say 22h, not the per-factory split's 48h.
  check(near(t.info.uniformEndurance, 22, 0.1),
        'the uniform drop has its own dry-out time (got ' + t.info.uniformEndurance.toFixed(1) + 'h)');
  check(t.info.endurance > t.info.uniformEndurance + 20,
        '...and it is not the per-factory split\'s (got ' + t.info.endurance.toFixed(1) + 'h)');
}

console.log('\nthe factory\'s OWN rate wins over the plan total — the case the backend change exists for:');
{
  // A combined "Current setup" plan: 1 Coolant factory and 3 Enriched Uranium factories both eat
  // Precious Metals (tid 100), so `consumption` is the SUM and each factory's share is 1/n of its
  // own product. Plan-total × share would hand the Coolant factory 4× what it burns.
  const t = load({
    deadlineH: 24, stacks: { 100: 1e6, 200: 1e6 },
    consumption: [{ p1_type_id: 100, p1_name: 'Precious Metals', units_per_day: 4 * RATE },
                  { p1_type_id: 200, p1_name: 'Water', units_per_day: RATE }],
    facs: [{ shareByTid: { 100: 1, 200: 1 }, rateByTid: { 100: RATE, 200: RATE } },
           { shareByTid: { 100: 1 / 3 }, rateByTid: { 100: RATE }, runByTid: { 100: RUN } },
           { shareByTid: { 100: 1 / 3 }, rateByTid: { 100: RATE }, runByTid: { 100: RUN } },
           { shareByTid: { 100: 1 / 3 }, rateByTid: { 100: RATE }, runByTid: { 100: RUN } }],
  });
  check(t.facs[0].amt['100'] === 480,
        'the shared-P1 factory gets ITS 480/day, not a quarter of the pooled total (got ' + t.facs[0].amt['100'] + ')');
  check(t.facs[1].amt['100'] === 480, 'and so does each factory of the other product (got ' + t.facs[1].amt['100'] + ')');
  check(t.facs[0].amt['200'] === 480, 'a factory that uses only some of the plan\'s P1s is sized on the ones it uses');
  check(t.facs[1].amt['200'] === undefined, '...and is allocated nothing of the P1 it does not use');
}

console.log('\na plan saved before this feature still works — plan total × share is the fallback rate:');
{
  const t = load({
    deadlineH: 48, stacks: { 100: 1e6, 200: 1e6 },
    facs: [{ rateByTid: {}, runByTid: {}, shareByTid: { 100: 0.5, 200: 0.5 } },
           { rateByTid: {}, runByTid: {}, shareByTid: { 100: 0.5, 200: 0.5 } }],
  });
  check(t.facs[0].amt['100'] === 960,
        'half of a 1,920/day plan total is the same 960 (got ' + t.facs[0].amt['100'] + ')');
  check(near(t.info.roundDriftH, 0, 0.01), 'with no run size known, whole units are the step and nothing is lost');
}

// ── Refusals ──────────────────────────────────────────────────────────────────────────────────
console.log('\nthe refusals:');
{
  const t = load({ deadlineH: -2, stacks: { 100: 1e6, 200: 1e6 } });
  check(t.info && t.info.past === true, 'a deadline in the past is called out rather than computed');
  check(!Object.keys(t.facs[0].amt).length, 'and nothing is allocated against it');
}
{
  const t = load({ deadlineH: 48, featureOff: true, stacks: { 100: 1e6, 200: 1e6 } });
  check(t.read('_deadlineInfo') === null, 'with the flag off there is no deadline result at all');
  check(t.facs[0].amt['100'] > 10000, '...and the fill-up split runs instead (got ' + t.facs[0].amt['100'] + ')');
}
{
  const t = load({ mode: 'full', deadlineH: 48, stacks: { 100: 1e6, 200: 1e6 } });
  check(t.read('_deadlineInfo') === null && t.facs[0].amt['100'] > 10000,
        'and "Fill up" mode is still the old fill-to-full behaviour');
}

// ── The stored deadline is an instant, never a wall clock ─────────────────────────────────────
console.log('\nthe deadline is stored as an instant (a stored local time means something else after DST):');
{
  const t = load({ deadlineH: 48 });
  vm.runInContext("_setRefillDeadline('2026-08-22T14:00')", t.sandbox);
  const stored = vm.runInContext("localStorage.getItem('refillDeadlineMs')", t.sandbox);
  check(/^\d+$/.test(String(stored)), 'localStorage holds epoch ms, not the picker text (got ' + stored + ')');
  check(new Date(Number(stored)).getTime() === new Date('2026-08-22T14:00').getTime(),
        'and it is the instant the LOCAL wall clock named');
}

// ── The readout renders ───────────────────────────────────────────────────────────────────────
// A ReferenceError in a render path is caught by nothing at runtime — the user just sees an empty
// card. These run the real render functions against a captured-innerHTML DOM.
console.log('\nthe controls and the readout render, and say both clocks:');
{
  const els = {};
  const t = load({
    deadlineH: 47.5, stacks: { 100: 1e6, 200: 1e6 },
    dom: id => (els[id] = els[id] || { innerHTML: '', dataset: {}, value: '' }),
  });
  vm.runInContext('_renderRefillControls(); _updateRefillDays();', t.sandbox);
  const ctl = (els.refillControls || {}).innerHTML || '';
  const days = (els.refillDays || {}).innerHTML || '';
  check(/datetime-local/.test(ctl), 'the deadline picker is rendered');
  check(/local ·/.test(ctl) && /EVE/.test(ctl), '...with the local and EVE clocks side by side');
  check(/Run dry at/.test(ctl), '...and the mode toggle names the mode');
  // Pressing a mode button must not move the buttons: the picker is hidden, never unmounted.
  const els3 = {};
  const off = load({ mode: 'full', dom: id => (els3[id] = els3[id] || { innerHTML: '', dataset: {}, value: '' }) });
  vm.runInContext('_renderRefillControls();', off.sandbox);
  const offCtl = (els3.refillControls || {}).innerHTML || '';
  check(/datetime-local/.test(offCtl) && /dist-deadline-pick off/.test(offCtl),
        'in Fill up mode the picker is still in the DOM, only hidden — so the toggle does not reflow');
  check(/runs dry/.test(days), 'the readout says when the factories run dry');
  const els2 = {};
  const skipped = load({
    deadlineH: 24, ignorePads: false, facs: [{ inputM3: 10000 }], stacks: { 100: 1e6, 200: 1e6 },
    dom: id => (els2[id] = els2[id] || { innerHTML: '', dataset: {}, value: '' }),
  });
  vm.runInContext('_renderRefillTables(); _updateRefillDays();', skipped.sandbox);
  const sd = (els2.refillDays || {}).innerHTML || '';
  check(/Nothing to drop/.test(sd) && !/Drop this/.test(sd),
        'and when every factory is skipped it does not tell you to drop anything');
  check(/skip/.test((els2.refillTables || {}).innerHTML || ''),
        '...the Summary row says skip too, not a dash');
  check(/whole runs/.test((els.refillHint || {}).innerHTML || ''), 'and the hint describes deadline mode, not fill-up');
}

console.log('\n' + (fails.length ? 'FAILED: ' + fails.join('; ') : 'all checks passed'));
process.exit(fails.length ? 1 : 0);
