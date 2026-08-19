/**
 * TODO (2026-08-19): "Plan to a deadline" finds the LARGEST reaction batch quantity whose
 * estimated_hours (from `POST /api/reactions/orders/preview`) fits inside the hours available
 * before a picked instant — a binary search over quantity, not a new sizing algorithm, since that
 * endpoint already answers "how long for N units" and is trusted/tested on its own.
 *
 * This runs `_rxFindMaxQtyByDeadline` for real, against a FAKE `apiSend` that models
 * `estimated_hours` as a simple step function of quantity (mirroring the real endpoint's shape:
 * whole jobs, ceiling division, so time only increases in discrete steps, never continuously) —
 * proving the search actually finds the boundary and doesn't overshoot it, without needing a live
 * account. `static/reactions.js` is executed whole in a `vm` context, same approach as
 * `test_refill_deadline.js` uses for `refill.js`.
 *
 *     node test_reactions_deadline.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, 'static', 'reactions.js'), 'utf8');
const fails = [];

function check(cond, msg) {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) fails.push(msg);
}

// Model: `jobsUsed` reactors run in parallel, each cycle takes `cycleHours`, and a batch of `qty`
// runs takes ceil(qty / jobsUsed) cycles — the same "whole jobs, ceiling" shape
// `_order_report`'s time loop uses (`math.ceil(tier["runs"] / jobs_used) * cycle_hours`).
function makeModel(jobsUsed, cycleHours, freeSlots) {
  let calls = 0;
  return {
    calls: () => calls,
    apiSend: async (method, url, body) => {
      calls++;
      if (method !== 'POST' || url !== '/api/reactions/orders/preview') throw new Error('unexpected call: ' + url);
      const qty = body.target_qty;
      const estimated_hours = freeSlots <= 0 ? null : Math.ceil(qty / jobsUsed) * cycleHours;
      return {
        order: { type_id: body.type_id, name: 'Test Product', target_qty: qty, top_level_runs: qty },
        materials: [], chain_tiers: [], cost: { material_cost: 0, job_cost: 0, total_cost: 0, cost_per_unit: 0 },
        profit: { client_price: null, price_per_unit: null, profit: null, margin_pct: null },
        time: { tiers: [], free_slots_now: freeSlots, estimated_hours, formula_capped: [], caveat: '' },
        missing_formulas: { complete: false, formulas: [], unresolved: [] },
        stock_covered: [], stale: false,
      };
    },
  };
}

function load(model) {
  const sandbox = {
    console,
    apiSend: model.apiSend,
    _esc: s => String(s),
    _fmtHours: h => String(h),
    _rxOpps: [], _rxOppsLoaded: true, _rxCadenceDays: null,
    _rxLoadOpportunities: async () => [],
    _rxOrderReportBody: () => '',
    _toLocalInput: () => '', _fmtDeadlineBoth: () => '',
    document: { getElementById: () => null, querySelectorAll: () => [] },
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'static/reactions.js' });
  return sandbox;
}

async function main() {
  // 3 reactors free, 4h cycle: exact multiples of 3 finish in whole cycles (qty=3 -> 4h, qty=6 -> 8h).
  {
    const model = makeModel(3, 4, 3);
    const sandbox = load(model);
    // Exactly 3 windows of 4h = 12h available: qty=9 takes ceil(9/3)*4=12h (fits), qty=10 takes
    // ceil(10/3)*4=16h (does not) — 9 is the true boundary.
    const found = await sandbox._rxFindMaxQtyByDeadline(99, 12);
    check(found.fits, 'a batch fits inside 12h against a 3-slot/4h-cycle model');
    check(found.qty === 9, `finds the exact boundary quantity (got ${found.qty}, want 9)`);
    check(found.report.time.estimated_hours === 12, `and its own report agrees on the hours (got ${found.report.time.estimated_hours})`);
    // Confirm qty+1 genuinely does NOT fit, so the search isn't just returning a safe underestimate.
    const oneMore = await sandbox._rxDeadlinePreview(99, found.qty + 1);
    check(oneMore.time.estimated_hours > 12, `and one more unit really would miss the deadline (got ${oneMore.time.estimated_hours}h)`);
    check(model.calls() < 25, `converges well under the iteration cap (used ${model.calls()} calls)`);
  }

  // A larger true boundary, to prove the exponential-doubling phase finds a workable upper bound
  // rather than only ever working for small quantities.
  {
    const model = makeModel(5, 2, 5);
    const sandbox = load(model);
    // ceil(qty/5)*2 <= 100  =>  ceil(qty/5) <= 50  =>  qty <= 250.
    const found = await sandbox._rxFindMaxQtyByDeadline(1, 100);
    check(found.fits, 'a large-quantity boundary is still found');
    check(found.qty === 250, `finds it exactly (got ${found.qty}, want 250)`);
    check(model.calls() < 25, `still within the iteration cap for a big boundary (used ${model.calls()} calls)`);
  }

  // Not even one run fits.
  {
    const model = makeModel(2, 10, 2);
    const sandbox = load(model);
    const found = await sandbox._rxFindMaxQtyByDeadline(1, 5);   // 1 unit needs ceil(1/2)*10 = 10h > 5h available
    check(!found.fits, 'reports "does not fit" rather than a wrong quantity when even 1 unit is too slow');
  }

  // No free reaction slots at all (estimated_hours comes back null, matching the real endpoint's
  // "no_capacity" case) — must be read as "cannot say", not as "instant" (null <= anything is
  // false in JS, so a naive comparison would already get this right, but pin it explicitly).
  {
    const model = makeModel(3, 4, 0);
    const sandbox = load(model);
    const found = await sandbox._rxFindMaxQtyByDeadline(1, 1000);
    check(!found.fits, 'zero free slots is reported as not-fitting, not as "1000 hours is plenty"');
    check(found.report.time.estimated_hours === null, 'because the underlying report genuinely has no estimate');
  }
}

main().then(() => {
  console.log('\n' + (fails.length ? 'FAILED: ' + fails.join('; ') : 'all checks passed'));
  process.exit(fails.length ? 1 : 0);
}).catch(e => { console.error(e); process.exit(1); });
