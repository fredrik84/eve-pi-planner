// ── Industry — step 2, the inputs: what to build and the knobs over it. ─────────────────────
// Product search and picker, quantity, the run itself, the metric tiles, the three controls
// (force-build, marginal threshold, margin) with their live reprice, the quantity sweep
// behind the marginal slider, and the facility picker.

// ── Product search / picker ─────────────────────────────────────────────────────────────────
function indOnSearchInput() {
  clearTimeout(_indSearchTimer);
  const q = document.getElementById('indSearch').value.trim();
  // Typing is not picking: both buttons stay disabled until a result is chosen, and a disabled
  // button gives no feedback at all — so say what's missing instead of leaving the click dead.
  if (!_indPicked || _indPicked.name !== q) _indSetPickHint(q);
  if (q.length < 2) { _indHideResults(); return; }
  _indSearchTimer = setTimeout(() => _indSearch(q), 200);
}

function _indSetPickHint(q) {
  const h = document.getElementById('indPickHint');
  if (h) h.textContent = q.length >= 2 ? 'Pick a product from the list to price it.' : '';
}

// Enter on the search box takes the first match — otherwise typing a full product name and
// hitting Enter looks like it selected something when nothing is selected at all.
function indOnSearchKey(ev) {
  if (ev.key !== 'Enter') return;
  ev.preventDefault();
  const box = document.getElementById('indSearchResults');
  const first = box && box.style.display !== 'none' && box.querySelector('.ind-search-row');
  if (first) first.click();
}

// The rows under a product search box, or the empty state. Shared with the hand-declared blueprint
// picker: both search the same endpoint over the same buildable types, and a product that is
// findable in one box but not the other would be a difference with no reason a user could name.
// `pick` is the global the row calls with the chosen type — the only thing that differs.
function _indSearchRowsHtml(results, pick) {
  if (!results || !results.length) return '<div class="ind-search-empty">No buildable match</div>';
  return results.map(x =>
    `<div class="ind-search-row" onclick="${pick}(${x.type_id}, '${_esc(x.name).replace(/'/g, "\\'")}')">${_esc(x.name)}</div>`
  ).join('');
}

async function _indSearch(q) {
  try {
    const d = await api('/api/industry/search?q=' + encodeURIComponent(q));
    const box = document.getElementById('indSearchResults');
    box.innerHTML = _indSearchRowsHtml(d.results, 'indPick');
    box.style.display = '';
  } catch (e) {}
}

function _indHideResults() {
  const box = document.getElementById('indSearchResults');
  if (box) box.style.display = 'none';
}

function indPick(typeId, name) {
  if (!_indPicked || _indPicked.type_id !== typeId) _indForcedTypes.clear();
  _indPicked = { type_id: typeId, name };
  document.getElementById('indSearch').value = name;
  _indHideResults();
  document.getElementById('indPlanBtn').disabled = false;
  document.getElementById('indQueueBtn').disabled = false;
  document.getElementById('indPickHint').textContent = '';
  _indSyncOptsVisible();
  // Cost and time land under the slider straight away — you shouldn't have to run a full preview
  // to see what the threshold is worth on this product.
  _indLoadSweep(_indQty());
}

let _indQtySweepTimer = null;
function indOnQtyInput() {
  clearTimeout(_indQtySweepTimer);
  _indRenderMarginalLive();                       // marks the read-out stale for the new quantity
  _indQtySweepTimer = setTimeout(() => _indLoadSweep(_indQty()), 400);
}

function _indQty() {
  const el = document.getElementById('indQty');
  return Math.max(1, parseInt(el ? el.value : '1') || 1);
}

// The speed toggle decides whether slow bulk components are bought instead of built — it changes the
// plan, so it re-plans and re-costs like the other knobs. It used to do nothing at all until the next
// Preview, which made unchecking it look broken.
function indOnPrioSpeed() {
  _indSaveSettings();
  if (!_indPicked) return;
  if (document.getElementById('indResult').innerHTML.trim()) indRunPlan();
  else _indLoadSweep(_indQty());
  if (_indStatusVisible()) indRefreshStatus();
}

// A roomy, centered loading card. Planning a capital walks the whole recipe tree and schedules
// hundreds of jobs, so this is on screen long enough to be worth not looking like a stray line of
// text — and sizing it near the finished plan's height stops the page collapsing then jumping.
function _indLoadingHtml(msg, sub) {
  return `<div class="ind-loading"><div class="ind-spinner" aria-hidden="true"></div>`
    + `<div class="ind-loading-msg">${_esc(msg)}</div>`
    + `<div class="ind-loading-sub">${_esc(sub || '')}</div></div>`;
}

// ── Single-product plan ─────────────────────────────────────────────────────────────────────
async function indRunPlan() {
  if (!_indPicked) return;
  const qty = Math.max(1, parseInt(document.getElementById('indQty').value) || 1);
  const out = document.getElementById('indResult');
  out.innerHTML = `<div class="pp-card">${_indLoadingHtml('Planning your build…',
    'Costing every component, deciding build vs buy, then scheduling the jobs across your slots.')}</div>`;
  try {
    let d;
    try {
      d = await apiSend('POST', '/api/industry/plan',
             { type_id: _indPicked.type_id, quantity: qty, prioritize_speed: _indPrioSpeed(),
               marginal_pct: _indMarginalPct(), force_build: _indForceBuild(),
               force_build_ids: _indForceIds(), me_te_overrides: _indMeTeMap(),
               // Cost the preview against the stock the resulting ORDER would count, not the
               // account's whole tick list — those are different plans otherwise.
               ...(_featureActive('industry_plan_sources')
                     ? { source_keys: _indPlanSourceKeys() } : {}),
               ..._indFacilityBonus() });
    } catch (e) {
      out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(e.message || 'Plan failed')}</p></div>`; return;
    }
    _indLastPlan = d;
    out.innerHTML = _indRenderPlan(d, `Build ${qty}× ${_esc(d.target.name)}`);
    indMargCutLabel();
    // The plan renders below a tall form inside a scrolling modal, so on a laptop it can land
    // entirely below the fold — which reads as "the button did nothing". Bring it into view.
    out.scrollIntoView({ behavior: 'smooth', block: 'start' });
    _indRenderMarginLive();     // the quote read-out has a real plan to price now
    _indLoadSweep(qty);
  } catch (e) {
    out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(String(e))}</p></div>`;
  }
}

function _indMetricTiles(m) {
  const tiles = [];
  // When batch rounding overproduces reusable intermediates, lead with the net product cost and
  // show the total outlay + the recyclable leftover credit separately.
  if (m.net_cost != null && (m.leftover_value || 0) > 0.5) {
    tiles.push(['Net cost', fmtIsk(m.net_cost), 'Cost attributable to the finished units, after crediting back reusable leftovers'],
               ['Total spend', fmtIsk(m.total_cost), 'Everything you pay upfront to run this build'],
               ['Leftover credit', fmtIsk(m.leftover_value), 'Value of excess intermediates you can reuse or sell']);
  } else {
    tiles.push(['Total cost', fmtIsk(m.total_cost), '']);
  }
  const steps = m.build_steps != null ? m.build_steps : m.job_count;
  tiles.push(['Materials', fmtIsk(m.materials_cost), ''], ['Job fees', fmtIsk(m.job_cost), '']);
  // Show it whenever a print has to be bought, so Materials + Job fees + Blueprints visibly adds
  // up to the total instead of the total quietly being larger than its parts.
  if (m.blueprint_cost) {
    tiles.push(['Blueprints', fmtIsk(m.blueprint_cost),
      'Blueprint copies this build needs and you don\'t already own — priced from Jita contracts and '
      + 'included in the total. Originals are not charged here: they\'re reusable, so billing one to a '
      + 'single build would badly overstate it.']);
  }
  tiles.push(['Build steps', steps, 'Distinct things to build — each may split into parallel jobs across your slots']);
  if (m.makespan_hours != null) tiles.push(['Makespan', _fmtHours(m.makespan_hours), 'Wall-clock time with jobs running in parallel across your slots']);
  if (m.price != null) tiles.push(['Sell price', `<span data-ind-price>${fmtIsk(_indPriceOf(m))}</span>`,
    `What to quote the customer: ${_indMarginPct()}% over net cost. Adjust with the margin slider above.`]);
  // Turn the percentage into the number it actually means for THIS build.
  if (m.marginal_threshold) tiles.push(['Build threshold', fmtIsk(m.marginal_threshold),
    `Anything that would save less than this by building is bought instead (${m.marginal_pct}% of the build${m.marginal_pct > 0 ? ', floor 5m' : ''}). Adjust with the slider above.`]);
  else if (m.total_job_hours != null) tiles.push(['Total job time', _fmtHours(m.total_job_hours), '']);
  return `<div class="an-stats">` + tiles.map(([l, v, t]) =>
    `<div class="an-stat"${t ? ` title="${_esc(t)}"` : ''}><div class="an-stat-lbl">${l}</div><div class="an-stat-val">${v}</div></div>`).join('') + `</div>`;
}

function _indPrioSpeed() {
  const el = document.getElementById('indPrioSpeed');
  return el ? el.checked : true;
}

// "Build everything": drop BOTH shortcuts that buy components — the saving threshold (including its
// 5m floor, which the slider can't reach) and the speed cap that buys slow bulk batches. Things
// that are outright cheaper to buy are still bought; ignoring marginal savings means small gains
// count, not that paying more to build makes sense.
function _indForceBuild() {
  const el = document.getElementById('indForceBuild');
  return el ? el.checked : false;
}

function indOnForceBuild() {
  const on = _indForceBuild();
  try { localStorage.setItem('indForceBuild', on ? '1' : '0'); } catch (e) {}
  _indSaveSettings();
  // The savings slider and speed toggle have no effect while this is on — grey them out rather
  // than leaving controls that silently do nothing.
  ['indMarginal', 'indPrioSpeed'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = on;
    const f = el && el.closest('.ind-field, .ind-opt-check');
    if (f) f.classList.toggle('ind-opt-muted', on);
  });
  if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan();
  if (_indStatusVisible()) indRefreshStatus();
  _indRenderMarginalLive();
}

// Seed the controls from the account's stored options before the localStorage restore, so what the
// form shows is what a share link or checklist will be planned with. Anything not stored falls
// through to this browser's own last choice.
let _indHasSavedSettings = false;
async function _indApplySavedSettings() {
  let d = null;
  try {
    d = (await api('/api/industry/settings')).settings || null;
  } catch (e) { return; }
  // Read BEFORE the early return below: an account that has never saved a setting is exactly the
  // one that hasn't been through setup, so bailing first would hide the wizard from the only
  // people who need it.
  _indOnboarded = !!(d && d.onboarded);
  // The containers the last build was pointed at, used to pre-fill the picker on the next one.
  _indLastSourceKey = (d && d.last_source_key) || '';
  _indLastSourceKeys = (d && d.last_source_keys) || (_indLastSourceKey ? [_indLastSourceKey] : []);
  _indHasSavedSettings = !!(d && d.updated_at);
  if (!d || !d.updated_at) return;
  const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
  const check = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.checked = !!val; };
  const sel = document.getElementById('indFacility');
  if (sel && d.facility_id && _indFacilityMap[d.facility_id]) {
    sel.value = d.facility_id;
    try { localStorage.setItem('indFacility', d.facility_id); } catch (e) {}
  }
  if (d.margin_pct != null) {
    set('indMargin', d.margin_pct);
    try { localStorage.setItem('indMarginPct', String(d.margin_pct)); } catch (e) {}
  }
  if (d.marginal_pct != null) {
    set('indMarginal', d.marginal_pct);
    try { localStorage.setItem('indMarginalPct', String(d.marginal_pct)); } catch (e) {}
  }
  if (d.force_build != null) {
    check('indForceBuild', d.force_build);
    try { localStorage.setItem('indForceBuild', d.force_build ? '1' : '0'); } catch (e) {}
  }
  check('indPrioSpeed', d.prioritize_speed);
}

// Restore a numeric control from what this browser last used. Both sliders are the same three
// moves: read, refuse anything that doesn't parse, then fire the control's OWN input handler so the
// label and the live reprice follow the value — a restore that set `.value` and stopped left the
// number and the sentence under it disagreeing.
function _indRestoreNum(elId, storageKey, onInput) {
  const el = document.getElementById(elId);
  if (!el) return;
  let v = null;
  try { v = localStorage.getItem(storageKey); } catch (e) {}
  if (v !== null && !isNaN(parseFloat(v))) el.value = parseFloat(v);
  onInput();
}

// Seed all three plan controls, with saving suppressed for the duration. Every restore fires a
// handler and every one of those handlers saves, so without the guard merely opening the planner
// writes this browser's remembered knobs back over the account's. Kept as one function because the
// guard is the point: it is not optional, and a fourth control added without it would look fine.
function _indRestoreControls() {
  _indRestoringSettings = true;
  _indRestoreNum('indMarginal', 'indMarginalPct', indOnMarginalInput);
  indRestoreForceBuild();
  _indRestoreNum('indMargin', 'indMarginPct', indOnMarginInput);
  _indRestoringSettings = false;
}

function indRestoreForceBuild() {
  const el = document.getElementById('indForceBuild');
  if (!el) return;
  let v = null;
  try { v = localStorage.getItem('indForceBuild'); } catch (e) {}
  el.checked = v === '1';
  indOnForceBuild();
}

// How much of the build's value a component must save before it's worth building. A genuine
// time-vs-cost preference the math can't settle, so it's one of the few real knobs here — and it
// reports the ISK it resolves to, because "3%" means nothing until you see it's 74m on a capital.
const IND_MARGINAL_DEFAULT = 3;
function _indMarginalPct() {
  const el = document.getElementById('indMarginal');
  return el ? parseFloat(el.value) : IND_MARGINAL_DEFAULT;
}
function indOnMarginalInput() {
  const lbl = document.getElementById('indMarginalPct');
  if (lbl) lbl.textContent = _indMarginalPct() + '%';
  _indRenderMarginalLive();
}
function indOnMarginalChange() {
  try { localStorage.setItem('indMarginalPct', String(_indMarginalPct())); } catch (e) {}
  _indSaveSettings();
  indOnMarginalInput();
  if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan();
  if (_indStatusVisible()) indRefreshStatus();
}
// What to charge over cost. The one number the tool genuinely cannot work out for the user — it's
// their business, their customer, their risk — so it's a knob with a sane default.
const IND_MARGIN_DEFAULT = 10;
function _indMarginPct() {
  const el = document.getElementById('indMargin');
  return el ? parseFloat(el.value) : IND_MARGIN_DEFAULT;
}
function indOnMarginInput() {
  const lbl = document.getElementById('indMarginPct');
  if (lbl) lbl.textContent = _indMarginPct() + '%';
  _indRenderMarginLive();
}
function indOnMarginChange() {
  try { localStorage.setItem('indMarginPct', String(_indMarginPct())); } catch (e) {}
  indOnMarginInput();
  _indSaveSettings();
  // Margin changes the price, not the build — no re-plan needed, just re-price what's on screen.
  if (_indLastPlan && _indLastPlan.metrics) _indRepriceRendered();
}
// The price for the plan currently on screen, at the slider's current position. Recomputed client
// side because margin is pure arithmetic on a cost the server already returned — re-planning a
// capital to multiply by 1.1 would be absurd.
function _indPriceOf(metrics, pct) {
  const net = metrics.net_cost != null ? metrics.net_cost : metrics.total_cost;
  if (net == null) return null;
  return net * (1 + (pct == null ? _indMarginPct() : pct) / 100.0);
}

// `_indLastPlan` is set by the single-product preview AND by the whole-queue status plan, so it is
// only a valid basis for these read-outs when it describes exactly what's picked right now.
// Without this check the margin slider quoted the entire queue's cost against a product the user
// hadn't even chosen yet — a large, unexplained ISK figure sitting under the slider on open.
function _indPlanForPick() {
  const p = _indLastPlan;
  return (p && p.target && p.metrics && _indPicked
          && p.target.type_id === _indPicked.type_id
          && p.target.quantity === _indQty()) ? p : null;
}

function _indRenderMarginLive() {
  const el = document.getElementById('indMarginLive');
  if (!el) return;
  // Prefer the rendered plan; fall back to the threshold sweep so the quote appears as soon as a
  // product is picked, instead of the slider sitting blank until a Preview is run.
  const plan = _indPlanForPick();
  let m = plan && plan.metrics, estimate = false;
  if (!m && _indPicked) {
    const pts = _indSweep && _indSweep.key === _indSweepKey(_indQty()) ? _indSweep.points : null;
    if (pts && pts.length) { m = _indSweepPoint(pts, _indMarginalPct()); estimate = true; }
  }
  const price = m ? _indPriceOf(m) : null;
  if (price == null) { el.style.display = 'none'; el.innerHTML = ''; return; }
  const net = m.net_cost != null ? m.net_cost : m.total_cost;
  el.style.display = '';
  el.innerHTML = `Quote <b>${fmtIsk(price)}</b> · you keep ${fmtIsk(price - net)}`
    + (estimate ? ' <span class="ind-marg-est">estimate</span>' : '');
}

// The sliders only mean anything against a chosen product — their read-outs are blank until one is
// picked, which leaves two labelled controls that look broken. Hide them until there's a product.
function _indSyncOptsVisible() {
  document.querySelectorAll('#indPlanModal .ind-field-marg').forEach(f => {
    f.style.display = _indPicked ? '' : 'none';
  });
}

// Live re-price without a round trip: the price tiles and the margin read-out both derive from the
// rendered plan's cost.
function _indRepriceRendered() {
  _indRenderMarginLive();
  document.querySelectorAll('[data-ind-price]').forEach(el => {
    const m = _indLastPlan && _indLastPlan.metrics;
    if (m) el.textContent = fmtIsk(_indPriceOf(m));
  });
}

// ── Live slider feedback ──────────────────────────────────────────────────────────────────────
// The threshold is a time-vs-cost trade, and a bare "4%" tells you nothing about either side of it.
// So as soon as a product is picked (before any preview is run) we fetch the whole curve — cost +
// makespan at every slider stop — and read it locally as the handle moves. Dragging then shows the
// actual consequence instantly, with no replan per pixel; letting go still runs the real plan.
// Components the user has overridden to BUILD despite a shortcut buying them (id -> name). Reset
// when the product changes — an override is about this build, not a standing preference.
let _indForcedTypes = new Map();

function _indForceIds() { return [..._indForcedTypes.keys()]; }

// Un-force a component. There is no single-component counterpart: every "build it anyway" path —
// one chip or the whole strip — goes through `_indForceBuildMany`, which sets the overrides and
// replans ONCE. A one-at-a-time version replanned per component, so it was only ever correct for
// the case that already had a caller.
function indUnforceBuildType(typeId) {
  _indForcedTypes.delete(typeId);
  _indSweep = null; _indSweepFailed = null;
  return _indKeepScroll(() => indRunPlan());
}

let _indSweep = null;          // { key, points: [{pct, makespan_hours, total_cost, ...}] }
let _indSweepPending = null;   // key of the request in flight, so a drag can't stack fetches
let _indSweepFailed = null;    // key whose fetch failed — don't leave the line dimmed forever

function _indMeTeMap() {
  const out = {};
  Object.keys(_indMeTe).forEach(k => { out[String(k)] = _indMeTe[k]; });
  return out;
}

function _indSweepKey(qty) {
  const f = _indFacilityBonus();
  return [_indPicked ? _indPicked.type_id : 0, qty, _indPrioSpeed() ? 1 : 0,
          f.struct_material_pct, f.struct_time_pct, f.facility_id || '',
          _indForceIds().sort().join(','),
          JSON.stringify(_indMeTeMap())].join('|');
}

async function _indLoadSweep(qty) {
  if (!_indPicked) return;
  const key = _indSweepKey(qty);
  if (_indSweep && _indSweep.key === key) { _indRenderMarginalLive(); return; }
  if (_indSweepPending === key) return;
  if (_indSweepFailed === key) return;      // already tried and failed for these options
  _indSweepPending = key;
  _indRenderMarginalLive();      // shows the pending state (or dims the previous numbers)
  try {
    let d;
    try {
      d = await apiSend('POST', '/api/industry/plan_sweep',
             { type_id: _indPicked.type_id, quantity: qty,
               prioritize_speed: _indPrioSpeed(), force_build_ids: _indForceIds(),
               me_te_overrides: _indMeTeMap(), ..._indFacilityBonus() });
    } catch (e) { _indSweepFailed = key; _indRenderMarginalLive(); return; }
    if (_indSweepKey(qty) !== key) return;      // options moved on while it was in flight
    _indSweep = { key, points: d.points || [] };
    _indRenderMarginalLive();
    _indRenderMarginLive();       // the quote can be estimated off the sweep now
  } catch (e) {
    // A nicety, not the plan — on failure the read-out steps aside and the static hint stands alone.
    _indSweepFailed = key;
    _indRenderMarginalLive();
  } finally {
    if (_indSweepPending === key) _indSweepPending = null;
  }
}

function _indSweepPoint(pts, pct) {
  return pts.reduce((best, c) => Math.abs(c.pct - pct) < Math.abs(best.pct - pct) ? c : best, pts[0]);
}

function _indRenderMarginalLive() {
  const el = document.getElementById('indMarginalLive');
  if (!el) return;
  // Nothing to say with no product picked, and the slider is inert under "Build everything".
  if (!_indPicked || _indForceBuild()) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';

  const qty = _indQty();
  const pts = _indSweep && _indSweep.key === _indSweepKey(qty) ? _indSweep.points : null;
  if (!pts || !pts.length) {
    if (_indSweepFailed === _indSweepKey(qty)) { el.style.display = 'none'; el.innerHTML = ''; return; }
    // Keep the LINE (blanking it made the read-out flash in and out on every change) but never the
    // stale NUMBERS: figures from the previous product or quantity sitting next to a fresh plan read
    // as the tool contradicting itself.
    el.classList.add('ind-marg-stale');
    el.innerHTML = 'Working out what this setting costs…';
    return;
  }
  el.classList.remove('ind-marg-stale');

  const p = _indSweepPoint(pts, _indMarginalPct());
  // Absolutes always, so the line says the same kind of thing whether or not a preview exists. The
  // comparison against the rendered plan is an extra clause, not a replacement for them.
  const parts = [`<b>${_fmtHours(p.makespan_hours)}</b> build time`, `${fmtIsk(p.total_cost)} total`];
  // Only a preview OF THIS product+quantity is a valid baseline — `_indLastPlan` is also set by the
  // whole-queue status plan, and comparing against that would be nonsense.
  const shown = _indPlanForPick();
  const basePct = shown ? shown.metrics.marginal_pct : null;
  const base = basePct == null ? null : _indSweepPoint(pts, basePct);
  // Same resolved ISK threshold = literally the same plan, whatever the percentages read.
  if (base && base.threshold !== p.threshold) {
    const dh = base.makespan_hours - p.makespan_hours;      // + = this setting finishes sooner
    const dc = p.total_cost - base.total_cost;              // + = this setting costs more
    const time = Math.abs(dh) < 0.05 ? 'same time'
      : `<b class="${dh > 0 ? 'ind-live-good' : 'ind-live-bad'}">${_fmtHours(Math.abs(dh))} ${dh > 0 ? 'faster' : 'slower'}</b>`;
    const cost = Math.abs(dc) < 1 ? 'same cost'
      : `${dc > 0 ? '+' : '−'}${fmtIsk(Math.abs(dc))} ${dc > 0 ? 'more' : 'less'}`;
    parts.push(`${time}, ${cost} than the plan below`);
  } else if (base) {
    parts.push('applied below');
  } else {
    parts.push('estimate — <b>Preview</b> for the full plan');
  }
  el.innerHTML = parts.join(' · ');
}

// Facility presets → structure/rig material (ME) + time (TE) bonuses. Approximate real setups; the
// value shown in the label is what's applied, so pick the one closest to your structure.
const IND_FACILITIES = [
  { id: 'none', label: 'NPC station — no bonus', short: 'NPC station', me: 0, te: 0 },
  { id: 't1_me', label: 'Structure + T1 ME rig — ME 3% / TE 15%', short: 'T1 ME structure', me: 3, te: 15 },
  { id: 't1_te', label: 'Structure + T1 TE rig — ME 1% / TE 34%', short: 'T1 TE structure', me: 1, te: 34 },
  { id: 't2_me_null', label: 'Structure + T2 ME rig, null/WH — ME 6% / TE 15%', short: 'T2 ME structure', me: 6, te: 15 },
  { id: 't2_te_null', label: 'Structure + T2 TE rig, null/WH — ME 1% / TE 44%', short: 'T2 TE structure', me: 1, te: 44 },
];
let _indFacilityMap = {};     // option value → {me, te}
let _indFacilityLabel = {};   // option value → short display name, for the "in <building>" tag
let _indRxFacilityLabel = null;   // name of the account's configured reaction-build structure, if any
async function indPopulateFacility() {
  const sel = document.getElementById('indFacility');
  if (!sel) return;
  _indFacilityMap = {};
  _indFacilityLabel = {};
  _indRxFacilityLabel = null;
  let structOpts = '';
  try {
    {
      const d = await api('/api/markets');
      (d.markets || []).filter(m => m.kind === 'structure' && m.build_mfg && m.mfg_bonus).forEach(m => {
        const val = 's:' + m.id;
        _indFacilityMap[val] = { me: m.mfg_bonus.me, te: m.mfg_bonus.te };
        _indFacilityLabel[val] = m.name;
        structOpts += `<option value="${val}">${_esc(m.name)} — ME ${m.mfg_bonus.me}% / TE ${m.mfg_bonus.te}%</option>`;
      });
      const rx = (d.markets || []).find(m => m.kind === 'structure' && m.build_rx);
      if (rx) _indRxFacilityLabel = rx.name;
    }
  } catch (e) {}
  const presetOpts = IND_FACILITIES.map(f => { _indFacilityMap['p:' + f.id] = { me: f.me, te: f.te }; _indFacilityLabel['p:' + f.id] = f.short; return `<option value="p:${f.id}">${_esc(f.label)}</option>`; }).join('');
  sel.innerHTML = (structOpts ? `<optgroup label="Your build structures">${structOpts}</optgroup>` : '')
    + `<optgroup label="Generic presets">${presetOpts}</optgroup>`;
  const saved = localStorage.getItem('indFacility');
  if (saved && _indFacilityMap[saved]) sel.value = saved;
  else if (structOpts) sel.selectedIndex = 0;   // default to your first real structure when you have one
}
function _indFacilityBonus() {
  const sel = document.getElementById('indFacility');
  const b = _indFacilityMap[sel ? sel.value : ''] || { me: 0, te: 0 };
  // The ID travels with the percentages: when the selection is one of your own structures the
  // planner routes each job to whichever structure's rigs actually cover it, and it can only know
  // that this selection IS that structure (rather than a flat preset) from the id.
  return { struct_material_pct: b.me, struct_time_pct: b.te,
           facility_id: (sel && sel.value) || null };
}
// Which building a job actually happens in — the selected facility for manufacturing, or the
// account's configured reaction structure for reactions (a separate, independently-set structure).
function _indBuildingLabel(activity) {
  if (activity === 'reaction') return _indRxFacilityLabel;
  const sel = document.getElementById('indFacility');
  return _indFacilityLabel[sel ? sel.value : ''] || null;
}
function indOnFacilityChange() {
  const sel = document.getElementById('indFacility');
  if (sel) { try { localStorage.setItem('indFacility', sel.value); } catch (e) {} }
  _indSaveSettings();
  if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan();
  else _indLoadSweep(_indQty());   // no preview to replan — just re-cost the slider read-out
}

let _indLastPlan = null;   // last rendered plan, for the shopping-list copy features
let _indShopStageData = {};   // stage key (tier) -> [{name, qty}], for per-stage multibuy copy

// wire the search input once the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  const s = document.getElementById('indSearch');
  if (s) {
    s.addEventListener('input', indOnSearchInput);
    s.addEventListener('keydown', indOnSearchKey);
    s.addEventListener('blur', () => setTimeout(_indHideResults, 150));
  }
  // The speed toggle is NOT wired here. It carries `onchange="indOnPrioSpeed()"` in index.html,
  // and that handler already re-runs the plan under exactly this condition — plus saves the
  // setting, refreshes the status card, and loads the sweep when no plan is on screen. A second
  // listener doing a subset of it meant every flip fired TWO `POST /api/industry/plan` requests
  // that raced over which response painted the card.
});
