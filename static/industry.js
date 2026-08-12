// ── Industry / Manufacturing planner tab ────────────────────────────────────────────────────
// Talks to /api/industry/* (see app/industry/). Read-only make-or-buy: pick a product → cost +
// shopping list + build tree; queue products → plan them all together across your job slots.
// Reuses the shared formatters from utils.js (fmtIsk, _fmtHours, _esc).

let _indPicked = null;        // {type_id, name} currently selected in the picker
let _indSearchTimer = null;

async function onIndustryTabOpen() {
  const tag = document.getElementById('indPreviewTag');
  if (tag) {
    const pub = typeof _features !== 'undefined' && _features.industry && _features.industry.enabled;
    tag.style.display = (!pub && typeof _featuresIsAdmin !== 'undefined' && _featuresIsAdmin) ? '' : 'none';
  }
  // Manufacturing requires at least one structure you build in (it sets your ME/TE). Gate the tool
  // until one exists — but don't re-ask for markets/freight if Reactions already set those up
  // (they live in the shared Structures & Markets settings). indPopulateFacility fills the facility
  // map with your structures, so we can tell from it whether a build structure exists yet.
  await indPopulateFacility();
  indApplyBuildRulesGate();     // the way in to the standing rules, if this account has the surface
  _indRestoringSettings = true;
  await _indApplySavedSettings();
  indRestoreMarginal();
  indRestoreForceBuild();
  indRestoreMargin();
  _indRestoringSettings = false;
  // Nothing has ever saved these — seed the account from what this browser has been using. Without
  // it, a plan run on the user's behalf (a customer's share link, the checklist) keeps using library
  // defaults until they happen to touch a knob, which is not a step anyone knows to take: the share
  // quietly quoted 14d 4h off an un-bonused, buy-everything plan against an 8d 8h build.
  // ...but NOT before the first-run wizard has been through: its Save writes these, and a row
  // written earlier would make the account look established to the onboarded-backfill migration.
  if (!_indHasSavedSettings && _indOnboarded) _indSaveSettings();
  // No structure of your own is a nudge now, not a stop — the generic facility presets are enough
  // to plan with, so the tab loads either way.
  indApplyGate(Object.keys(_indFacilityMap).some(k => k.startsWith('s:')));

  indLoadSetupSummary();     // fire-and-forget: independent of the build status below
  indLoadLifetime();
  // Both are awaited because renders below read their globals — but neither reads the other's, so
  // they go together rather than one after the next. Two serial round trips on the way in to a page
  // whose expensive work hasn't started yet is latency paid for nothing.
  await Promise.all([indLoadBlacklist(), indLoadReactionPolicy()]);
  // If something is already cooking, that's what you came to look at — show the live build first
  // and fold the planner away. With an empty queue there's nothing to check, so lead with planning.
  await indRefreshStatus();
  indRefreshStaleCaches();   // after the paint: never make the page wait on ESI
}

// Bring stale caches up to date on the way in, instead of leaving it to whoever remembers to press
// Refresh. Staleness is silent and the numbers are wrong while it lasts — a stale job cache
// overstates free slots, stale assets tell you to buy what's in your hangar — so making it the
// user's job is making them responsible for a mistake they can't see. Fires AFTER the first paint
// and repaints only if something actually changed; the server decides what's stale and refuses to
// try more than once every few minutes (app/industry/freshness.py).
async function indRefreshStaleCaches() {
  let d = null;
  try { d = await apiSend('POST', '/api/industry/refresh-stale'); } catch (e) { return; }
  if (!d || !(d.refreshed || []).length) return;
  indLoadSetupSummary();
  // Jobs and stock both move the plan and the progress on it; blueprints move ME/TE, which moves
  // every material figure. Any of the three is worth the re-read.
  indRefreshStatus();
}

// The "what to train next" advisor was removed entirely (2026-08-07), engine and all: training
// advice is not about THIS build, and a card suggesting a character start Industry I is not what
// somebody checking on a running build came for. It had been kept behind the removed rendering for
// two months with no caller. If training advice comes back, it belongs on a page about the
// character, not here.

let _indOrders = [];
// The status card is the live view; when it's on screen, a setting change must re-plan it.
function _indStatusVisible() {
  const c = document.getElementById('indStatusCard');
  return !!c && c.style.display !== 'none';
}
// Planning is a deliberate detour from checking on your build, so it opens in a modal rather than
// pushing the live status down the page.
function indOpenPlanner() {
  const m = document.getElementById('indPlanModal');
  if (!m) return;
  m.style.display = '';
  _indRestoringSettings = true;
  indRestoreMarginal();
  indRestoreForceBuild();
  indRestoreMargin();
  _indRestoringSettings = false;
  _indSyncOptsVisible();
  indLoadPlanSources();
  const s = document.getElementById('indSearch');
  if (s) setTimeout(() => s.focus(), 30);
}

// ── Where a container IS ─────────────────────────────────────────────────────────────────────
// A container used to be identified by its own name and its parent hangar and nothing else. With
// cans in several stations that is ambiguous exactly when it matters — picking which box a build
// sources from — so every list that shows containers GROUPS them by the station or structure they
// sit in, with the system named. One helper, because four lists showing the same boxes must not
// each invent their own wording.
const _IND_NO_PLACE = 'Hangars & pasted stock';

// [{label, items}] — containers bucketed by where they are, then everything with no location
// (hangars, pasted stock, a structure we can't see) in one trailing group.
function _indGroupSources(srcs) {
  const groups = new Map();
  (srcs || []).forEach(s => {
    const label = (s.kind === 'container' && s.place) ? s.place : _IND_NO_PLACE;
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(s);
  });
  const placed = [...groups.keys()].filter(k => k !== _IND_NO_PLACE).sort((a, b) => a.localeCompare(b));
  if (groups.has(_IND_NO_PLACE)) placed.push(_IND_NO_PLACE);   // never first: the point is the places
  return placed.map(label => ({
    label,
    items: groups.get(label).sort((a, b) => a.name.localeCompare(b.name)),
  }));
}

function _indSourceLabel(s) {
  return _esc(s.name) + (s.kind === 'container' ? '' : ' (whole hangar)');
}

// The <option> list for one source picker: saved sets first (one pick instead of three), then the
// boxes under an <optgroup> per station/structure.
function _indSourceOptionsHtml(srcs, selected, opts) {
  const o = opts || {};
  let html = `<option value=""${selected ? '' : ' selected'}>${_esc(o.blank || '— track it later —')}</option>`;
  if (o.sets && _indSourceSets.length) {
    html += `<optgroup label="Saved sets">` + _indSourceSets.map(st =>
      `<option value="set:${st.id}">${_esc(st.name)} (${st.keys.length} source${st.keys.length === 1 ? '' : 's'})</option>`).join('') + `</optgroup>`;
  }
  _indGroupSources(srcs).forEach(g => {
    html += `<optgroup label="${_esc(g.label)}">` + g.items.map(s =>
      `<option value="${_esc(s.key)}"${s.key === selected ? ' selected' : ''}>${_indSourceLabel(s)}</option>`).join('')
      + `</optgroup>`;
  });
  if (o.paste) html += '<option value="__paste">Paste what I already have…</option>';
  return html;
}

// One row of the picker. The COMMON case is one box and it costs exactly what it always did: a
// single dropdown, pre-answered. Extra boxes are an explicit "+ another box" away, and only when
// per-plan sources are on.
function _indSourceRowHtml(srcs, selected, onchange, opts) {
  const o = opts || {};
  return `<span class="ind-srcrow"><select ${o.id ? `id="${o.id}" ` : ''}class="ind-srcsel" `
    + `onchange="${onchange}">${_indSourceOptionsHtml(srcs, selected, o)}</select>`
    + (o.removable ? `<button type="button" class="ind-src-del" title="Stop pulling from this box" `
        + `onclick="${o.onremove}">✕</button>` : '') + `</span>`;
}

// Every value currently picked in a rows container, minus the blanks and the pseudo-options.
function _indPickedSources(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return [];
  return Array.from(el.querySelectorAll('select'))
    .map(s => s.value)
    .filter(v => v && v !== '__paste' && v.slice(0, 4) !== 'set:');
}

// A saved set picked in any row expands to its keys — the set is a shortcut for choosing boxes,
// never a second thing a plan can be bound to. One shape of binding keeps every reader honest.
function _indExpandSets(keys, picked) {
  const out = [];
  (keys || []).forEach(k => out.push(k));
  (picked || []).forEach(v => {
    if (v.slice(0, 4) !== 'set:') return;
    const st = _indSourceSets.find(x => String(x.id) === v.slice(4));
    (st ? st.keys : []).forEach(k => out.push(k));
  });
  return out.filter((k, i) => k && out.indexOf(k) === i);
}

// Which box this build's materials come from, chosen while planning it — because "which container
// is this build's" is decided at the same moment as what to build, not afterwards. A director picks
// a corp hangar or container straight off the scanned list; everyone else pastes what they hold,
// which is recorded against the order rather than added to the planner's global stock (stock you
// can't actually draw from is the one error that makes the planner build too little).
let _indPlanSources = [];      // the scanned source list, as the plan modal last saw it

async function indLoadPlanSources() {
  const field = document.getElementById('indPlanSrcField');
  const rows = document.getElementById('indPlanSrcRows');
  if (!field || !rows) return;
  if (!_featureActive('industry_sourcing')) { field.style.display = 'none'; return; }
  let d = {};
  try { d = (await api('/api/industry/assets')) || {}; } catch (e) {}
  _indPlanSources = d.sources || [];
  _indSourceSets = d.sets || [];
  const multi = _featureActive('industry_plan_sources');
  // Pre-filled with what the last build used, minus anything that has since gone. A builder running
  // a can per build answers this on every order and the answer is nearly always the same — so the
  // question is already answered, visibly, and one click to change. With per-plan sources the
  // remembered answer is the whole SET, because a build gathered from a reaction can and a
  // manufacturing can answers it the same way every time too.
  const remembered = (multi && _indLastSourceKeys.length ? _indLastSourceKeys
                                                         : [_indLastSourceKey])
    .filter(k => k && _indPlanSources.some(x => x.key === k));
  const picked = (multi ? remembered : remembered.slice(0, 1));
  rows.innerHTML = (picked.length ? picked : ['']).map((k, i) => _indSourceRowHtml(
    _indPlanSources, k, 'indOnPlanSourceChange()',
    {id: i === 0 ? 'indPlanSrc' : '', paste: i === 0, sets: multi,
     removable: multi && i > 0, onremove: 'indRemovePlanSource(this)'})).join('')
    + (multi ? `<button type="button" class="ind-src-add" onclick="indAddPlanSource()" `
        + `title="This build's materials are gathered from more than one box">+ another box</button>` : '');
  field.style.display = '';
  indOnPlanSourceChange();
}

function indAddPlanSource() {
  const rows = document.getElementById('indPlanSrcRows');
  const btn = rows && rows.querySelector('.ind-src-add');
  if (!btn) return;
  btn.insertAdjacentHTML('beforebegin', _indSourceRowHtml(
    _indPlanSources, '', 'indOnPlanSourceChange()',
    {sets: true, removable: true, onremove: 'indRemovePlanSource(this)', blank: '— pick a box —'}));
}

function indRemovePlanSource(btn) {
  const row = btn && btn.closest('.ind-srcrow');
  if (row) row.remove();
}

// The boxes the plan modal currently has picked, saved sets expanded. Used for BOTH the preview and
// the queued order, so the two cannot be costed against different stock — a preview that promises a
// shopping list the queued build then contradicts is the bug this shares one function to avoid.
function _indPlanSourceKeys() {
  return _indExpandSets(_indPickedSources('indPlanSrcRows'), _indSourceValues('indPlanSrcRows'));
}

function _indSourceValues(containerId) {
  const el = document.getElementById(containerId);
  return el ? Array.from(el.querySelectorAll('select')).map(s => s.value) : [];
}

function indOnPlanSourceChange() {
  const sel = document.getElementById('indPlanSrc');
  const box = document.getElementById('indPlanPaste');
  if (!sel || !box) return;
  box.style.display = sel.value === '__paste' ? '' : 'none';
  if (sel.value === '__paste') {
    const t = document.getElementById('indPlanPasteText');
    if (t) setTimeout(() => t.focus(), 30);
  }
}

function indClosePlanner() {
  const m = document.getElementById('indPlanModal');
  if (m) m.style.display = 'none';
}

// The landing view: what's cooking, what's next, and the pipeline as the centrepiece.
// Every whole-queue request must be planned with the SAME options — the checklist and the plan
// disagreeing about what's ready was exactly the bug that came from two hand-maintained bodies.
// The options are ALSO stored server-side. They shape every number, and a plan run on the user's
// behalf without a browser — a customer's share link, the start-now checklist — has no other way to
// know them; running those with library defaults is what made the share link quote an ETA days off
// the builder's own screen.
let _indSaveSettingsTimer = null;
// True while the form is being seeded. Without this, restoring the controls counts as a change and
// writes the browser's own state back over the account's — including when the read that should have
// corrected it failed.
let _indRestoringSettings = false;
// The account's build options as the settings endpoint wants them. Shared with the wizard's save,
// which needs the same body but awaited rather than debounced.
function _indSettingsBody() {
  const sel = document.getElementById('indFacility');
  return { ..._indFacilityBonus(), prioritize_speed: _indPrioSpeed(),
           marginal_pct: _indMarginalPct(), force_build: _indForceBuild(),
           margin_pct: _indMarginPct(), facility_id: sel ? sel.value : '' };
}

function _indSaveSettings() {
  if (_indRestoringSettings) return;
  clearTimeout(_indSaveSettingsTimer);
  // best-effort: the plan on screen already has these values
  _indSaveSettingsTimer = setTimeout(() => {
    apiSend('PUT', '/api/industry/settings', _indSettingsBody()).catch(() => {});
  }, 600);
}

function _indQueueBody() {
  return { prioritize_speed: _indPrioSpeed(), marginal_pct: _indMarginalPct(),
           force_build: _indForceBuild(), me_te_overrides: _indMeTeMap(),
           margin_pct: _indMarginPct(), ..._indFacilityBonus() };
}

async function indRefreshStatus() {
  const card = document.getElementById('indStatusCard');
  const body = document.getElementById('indStatusBody');
  if (!card || !body) return;
  let orders = [];
  try {
    orders = (await api('/api/industry/orders')).orders || [];
  } catch (e) {}
  _indOrders = orders;
  const empty = document.getElementById('indEmptyCard');
  if (!orders.length) {
    card.style.display = 'none';
    if (empty) empty.style.display = '';     // nothing to check — lead with the call to action
    return;
  }
  card.style.display = '';
  if (empty) empty.style.display = 'none';

  // Paint the last plan for this exact queue straight away, then go and check it. Re-planning a
  // capital build is real work, and staring at a spinner for it on every visit is the difference
  // between a tool you keep open and one you avoid. The orders list is fetched first precisely so
  // the cache can be matched against it — a plan is only shown if it was built for the queue that
  // is actually there now, which also makes it impossible to flash another account's build.
  const cached = _indReadPlanCache(_indQueueSig(orders));
  if (cached) {
    _indLastPlan = cached.plan;
    _indProgress = cached.progress || null;
    _indCacheNames(cached.plan);
    _indPaintStatus(cached.plan, { local: true });
  } else {
    body.innerHTML = _indLoadingHtml('Checking your build…', 'Pulling job status and re-planning what is left.');
  }

  // Progress now rides along with the plan (it is a view OF that plan), so this is ONE request
  // where it used to be two — each of which planned the whole queue independently.
  let d;
  try {
    d = await apiSend('POST', '/api/industry/queue-plan', _indQueueBody());
    if (d.empty) { card.style.display = 'none'; if (empty) empty.style.display = ''; return; }
    _indLastPlan = d;
    // Preview mode's fabricated progress must win over the real thing while it's on.
    if (_indSim === null) _indProgress = (d.progress && !d.progress.empty) ? d.progress : null;
    else await indLoadProgress();
    _indCacheNames(d);
    _indPaintStatus(d);
    _indWritePlanCache(_indQueueSig(orders), d);
  } catch (e) {
    if (!cached) body.innerHTML = `<p class="pp-warn">${_esc(e.message || "Could not plan your queue.")}</p>`;
  }
}

// ── Last-plan cache (paint now, check after) ─────────────────────────────────────────────────
// Keyed on the queue itself: order ids, quantities and the overrides that change what gets built.
// If any of that differs, the cached plan is not a plan of THIS queue and is never shown.
const _IND_CACHE_KEY = 'indPlanCache';
const _IND_CACHE_MAX_AGE = 15 * 60 * 1000;   // beyond this the ETAs are visibly wrong; wait instead

function _indQueueSig(orders) {
  // The running BUILD is part of the key. Without it a deploy that changes how plans are computed
  // — how jobs are split across slots, say — would still be served the pre-deploy plan from this
  // cache for up to fifteen minutes, and the user would reasonably conclude the change didn't ship.
  const src = (document.querySelector('script[src*="industry.js"]') || {}).src || '';
  const build = (src.match(/[?&]v=([a-z0-9]+)/) || [])[1] || '';
  return JSON.stringify([build, (orders || []).map(o => [o.id, o.quantity, o.product_type_id,
                                                         (o.force_build_ids || []).join(','),
                                                         JSON.stringify(o.me_te_overrides || {})])]);
}

function _indReadPlanCache(sig) {
  try {
    const raw = sessionStorage.getItem(_IND_CACHE_KEY);
    if (!raw) return null;
    const c = JSON.parse(raw);
    if (c.sig !== sig || !c.plan) return null;
    if (Date.now() - (c.at || 0) > _IND_CACHE_MAX_AGE) return null;
    return c;
  } catch (e) { return null; }
}

function _indWritePlanCache(sig, plan) {
  try {
    sessionStorage.setItem(_IND_CACHE_KEY, JSON.stringify(
      { sig, at: Date.now(), plan, progress: _indProgress }));
  } catch (e) {}     // a full or disabled sessionStorage just means no head start, never an error
}

// Draw the status card from a plan we already have. Split out from the fetch above because marking
// a step done changes NO part of the plan — not the requirements, not the schedule, not the cost —
// only the progress read off it. Re-planning the whole queue to repaint a tick was seconds of wait
// for an answer already in hand.
function _indPaintStatus(d, opts) {
  const body = document.getElementById('indStatusBody');
  if (!body || !d) return;
  const local = !!(opts && opts.local);
  // The running-jobs list and the sourcing panel are separate fetches, and a hand mark can't change
  // either of them — so a local repaint carries their current markup across instead of paying for
  // two more round trips (the sourcing one plans an order from scratch) to redraw the same thing.
  const keep = local ? {
    running: (document.getElementById('indRunning') || {}).innerHTML || '',
    sourcing: (document.getElementById('indSourcing') || {}).innerHTML || '',
  } : null;
  body.innerHTML = _indStatusHeadline(d)
    + `<div id="indInstall" class="ind-install"></div>`
    + _indRenderPlanBody(d)
    + `<div id="indRunning" class="ind-install"></div>`;
  indRenderInstall(d.install);   // "do this now" — comes with the plan, no second round trip
  indMargCutLabel();             // the bulk control's live readout starts filled in, not blank
  if (keep) {
    const run = document.getElementById('indRunning');
    if (run) run.innerHTML = keep.running;
    const src = document.getElementById('indSourcing');
    if (src) src.innerHTML = keep.sourcing;
    return;
  }
  indLoadRunning();          // what's already cooking goes under the pipeline
  if (_indSourcingOpen !== null) indRenderSourcing();   // the card was just repainted under it
}

// One-line answer to "where am I": overall progress, what's in the cooker, what to do next.
function _indStatusHeadline(d) {
  const p = _indProgress;
  const sim = p && p.simulated
    ? '<div class="ind-sim-banner">Preview mode — this progress is made up so you can see the layout. Nothing here is real.</div>' : '';
  const t = (p && p.totals) || null;
  // Order matters here: how far along → when it lands → what it costs → the job counters behind the
  // headline percentage. Four to a row, so this reads as two tidy lines.
  const tiles = [];
  if (t && t.required) {
    // Weighted by JOB TIME, not run count. Bulk components come in hundreds of short runs while the
    // capital part is a handful of very long ones, so a run-counted percentage told you 71.8% done
    // when what had finished was 57 minutes of a multi-day build. The tooltip carries both numbers
    // and names the unit, because a percentage nobody can reconcile is a percentage nobody trusts.
    const h = p.hours || {};
    const basis = h.total
      ? `${_fmtHours(h.done)} of ${_fmtHours(h.total)} of job time finished`
        + ` · ${t.done} of ${t.required} runs done`
      : `${t.done} of ${t.required} runs done`;
    tiles.push(['Progress', `${p.pct}%`, basis]);
  }
  const fd = d.metrics.first_delivery_hours;
  // Two different questions: when can I hand over the first order, vs when am I finished entirely.
  // Only worth splitting when they actually differ.
  // A half-connected account is never capped on the prints it half-shows (see `prints_known()`), so
  // the schedule assumes as many copies as there are slots and may be optimistic. That fact used to
  // be a banner of its own; it is not something anyone acts on mid-build, so it rides here on the
  // number it actually qualifies and costs no space at all.
  const cov = d.print_coverage;
  const covNote = (cov && cov.prints_counted === false)
    ? ` · Blueprint copies aren't counted${cov.missing ? ` (${cov.missing} character`
        + `${cov.missing === 1 ? '' : 's'} not connected)` : ''}, so jobs of one type are assumed to`
      + ` run in parallel — this may be optimistic.` : '';
  if (fd != null && d.metrics.makespan_hours - fd > 0.05) {
    tiles.push(['First delivery', _fmtHours(fd), 'When the first order in line is finished and deliverable' + covNote]);
    tiles.push(['Whole queue', _fmtHours(d.metrics.makespan_hours), 'When everything queued is finished' + covNote]);
  } else {
    tiles.push(['Time left', _fmtHours(d.metrics.makespan_hours),
                'Wall-clock for what remains, jobs in parallel' + covNote]);
  }
  const m = d.metrics || {};
  if (m.total_cost != null) {
    // Net cost leads: what the finished units really cost once reusable leftovers are credited back.
    if (m.net_cost != null && (m.leftover_value || 0) > 0.5) {
      tiles.push(['Net cost', fmtIsk(m.net_cost),
                  'Cost of the finished units, after crediting back reusable leftovers']);
      tiles.push(['Materials', fmtIsk(m.materials_cost), 'What the shopping list comes to']);
      tiles.push(['Total spend', fmtIsk(m.total_cost), 'Everything this build costs you up front']);
      tiles.push(['Job fees', fmtIsk(m.job_cost), 'Installation fees across every job in the plan']);
    } else {
      tiles.push(['Total cost', fmtIsk(m.total_cost), 'Materials + job fees + any blueprint copies']);
      tiles.push(['Materials', fmtIsk(m.materials_cost), 'What the shopping list comes to']);
      tiles.push(['Job fees', fmtIsk(m.job_cost), 'Installation fees across every job in the plan']);
    }
  }
  if (m.total_cost != null) {
    // The QUEUE's price comes from the server, which prices each order at the margin snapshotted on
    // it. Do NOT recompute it from the planner slider here (and do not tag it `data-ind-price`):
    // the slider sets the margin for NEW builds only, so pricing the whole queue off it made an
    // order's own margin — the thing the customer's share link quotes — invisible on this sheet.
    const price = m.price != null ? m.price : _indPriceOf(m);
    tiles.push(['Sell price', fmtIsk(price),
                m.margin_mixed
                  ? `What to charge across the whole build — each order at its own margin `
                    + `(${m.margin_pct}% blended). Edit an order to change its quote.`
                  : `What to charge at ${m.margin_pct != null ? m.margin_pct : _indMarginPct()}%`
                    + ` over net cost — the figure your customer sees on a shared link`]);
  }
  if (t && t.required) {
    // Runs, not jobs — one job carries many runs, and calling these "jobs" made the counters
    // disagree with the "N jobs" the step list and the checklist talk about.
    tiles.push(['Still to start', String(t.waiting), 'Runs not started yet']);
    tiles.push(['In the cooker', String(t.running), 'Runs installed and running right now']);
  }
  const byOrder = {};
  ((p && p.orders) || []).forEach(o => { byOrder[o.id] = o; });
  // Per-order ETA comes from the plan's own target finish times. Orders of the SAME product are
  // aggregated into one target, so they legitimately share an ETA — don't imply otherwise.
  const tgt = {};
  (d.targets || []).forEach(t => { tgt[t.type_id] = t; });
  const ordered = (_indOrders || []).slice().sort((a, b) => {
    const ra = (tgt[a.product_type_id] || {}).rank, rb = (tgt[b.product_type_id] || {}).rank;
    return (ra === undefined ? 99 : ra) - (rb === undefined ? 99 : rb) || a.id - b.id;
  });
  const chips = ordered.map(o => {
    const op = byOrder[o.id];
    const st = op ? op.status : 'waiting';
    const lbl = op ? (st === 'complete' ? 'done'
      : st === 'building' ? `${op.done_units}/${op.quantity}` : 'not started') : '';
    const tag = (op && op.label) ? `<span class="ind-oc-for" title="This order is for ${_esc(op.label)}">${_esc(op.label)}</span>` : '';
    const T = tgt[o.product_type_id];
    const pos = T && T.rank != null
      ? `<span class="ind-oc-pos" title="Position in line — first in line wins a contested slot">#${T.rank + 1}</span>` : '';
    const eta = st === 'complete' ? ''
      : (T && T.finish_hours ? `<span class="ind-oc-eta" title="Estimated finish for this order">${_fmtHours(T.finish_hours)}</span>` : '');
    // Overrides persisted with the order. Shown because a component being built against the
    // engine's own shortcut is a decision worth seeing — and worth being able to take back.
    const fb = o.force_build || [];
    const forced = fb.length
      ? `<button class="ind-oc-forced" title="Building anyway: ${_esc(fb.map(f => f.name).join(', '))} — click to go back to buying them"`
        + ` onclick="indClearOrderForced(${o.id})">⚒ ${fb.length}</button>` : '';
    // Shown when it differs from what the planner would quote today — otherwise it's noise.
    const mrg = (o.margin_pct != null && Math.abs(o.margin_pct - _indMarginPct()) > 0.01)
      ? `<span class="ind-oc-mrgtag" title="Quoted to the customer at ${o.margin_pct}% over cost">+${o.margin_pct}%</span>` : '';
    // An efficiency you set by hand drives every material figure for this order — show it on the
    // chip rather than only inside the editor.
    const ovMt = (o.me_te_overrides || {})[String(o.product_type_id)];
    const mete = ovMt
      ? `<span class="ind-oc-metetag" title="Planned against your own blueprint: ME ${ovMt[0]}, TE ${ovMt[1]}">`
        + `ME ${ovMt[0]} · TE ${ovMt[1]}</span>` : '';
    // This order makes its own reactions, against the account's standing rule. Same reasoning as
    // the ⚒ tag beside it: an exception you can't see is one you forget you set.
    const rxo = (o.build_reactions && _featureActive('industry_reaction_policy'))
      ? `<span class="ind-oc-rxtag" title="This build makes its own reactions, whatever your account rule says">reacts</span>` : '';
    const share = _featureActive('industry_share')
      ? `<button class="ind-oc-share" title="Share a status link with the customer" onclick="indShareOrder(${o.id})">↗</button>` : '';
    const src = _featureActive('industry_sourcing')
      ? `<button class="ind-oc-src" title="Materials for this build: what's already in the box and what's still to buy"`
        + ` onclick="indOpenSourcing(${o.id})">\u{1F4E6}</button>` : '';
    return `<span class="ind-order-chip ind-oc-${st}" id="oc-${o.id}">${pos}${tag}<b>${o.quantity}×</b> ${_esc(o.name)}`
      + (lbl ? `<span class="ind-oc-state">${lbl}</span>` : '') + eta + mrg + forced + mete + rxo
      + `<button class="ind-oc-edit" title="Rename, change the quantity, margin or blueprint ME/TE" onclick="indEditOrder(${o.id})">✎</button>`
      // The build rules for THIS order, in the same dialog the account's standing rules use. The
      // inline knobs stay for the order's own identity (name, quantity); what a build is *costed
      // against* is a different question and belongs beside the defaults it overrides.
      + (_indRulesActive()
          ? `<button class="ind-oc-edit" title="Build setup for this order — what it inherits and what it overrides"`
            + ` onclick="indOpenRules(${o.id}, ${JSON.stringify(o.label || '')})">⚙</button>` : '')
      + src + share
      + `<button class="ind-oc-del" title="Remove from the build" onclick="indRemoveOrder(${o.id})">✕</button></span>`;
  }).join('');
  return sim
    + `<div class="ind-status-head"><div class="ind-order-chips">${chips}</div>`
    + `<button class="ind-primary-btn" onclick="indOpenPlanner()">Plan a new build</button>`
    + ((_indOrders || []).length > 1
        ? `<button class="ind-secondary-btn" onclick="indOpenOrder()">Reorder</button>` : '')
    + `<button class="ind-secondary-btn" onclick="indRefreshJobs()" title="Pull job status from EVE and re-plan">Refresh</button></div>`
    + `<div class="an-stats">` + tiles.map(([l, v, tip]) =>
        `<div class="an-stat" title="${_esc(tip)}"><div class="an-stat-lbl">${l}</div><div class="an-stat-val">${v}</div></div>`).join('')
    + `</div>`
    // Opened from an order chip; empty until then, and re-rendered in place so the panel survives
    // the status card's own refreshes.
    + `<div id="indSourcing" class="ind-sourcing"></div>`;
}

// A NUDGE, not a wall. This used to block the whole tab until a real build structure was
// configured, which demanded more than the planner actually needs — the Facility dropdown ships
// generic presets (NPC station, T1/T2 rig structures) that cost a build perfectly well — and the
// way out could dead-end: adding a real structure needs structure search, which needs a connected
// market character, which someone who has only ever used PI does not have. Locking a working tool
// behind a prerequisite the user may be unable to satisfy is the worst of both.
function indApplyGate(hasStructure) {
  const gate = document.getElementById('indGate');
  const content = document.getElementById('indContent');
  if (!gate || !content) return;
  // First run gets the setup screen instead. Unlike the old gate this one can always be completed
  // without leaving the page, which is the property that makes blocking acceptable at all.
  if (!_indOnboarded) { _indRenderWizard(hasStructure); return; }
  content.style.display = '';
  if (hasStructure || localStorage.getItem('indFacilityNudge') === 'off') {
    gate.style.display = 'none';
    return;
  }
  gate.style.display = '';
  gate.innerHTML = `<div class="pp-card ind-fac-nudge"><div class="ind-body">
    <p class="pp-sub"><b>Costing against a generic facility.</b> Rigs change the materials and time of
      every job, so pick the closest match in the plan form's <b>Facility</b> list — or add the
      structure you really build in and get its exact ME &amp; TE.</p>
    <div class="ind-gate-actions">
      <button class="ind-primary-btn" onclick="openSettingsModal('markets')">Add my structure</button>
      <button class="ind-secondary-btn" onclick="indDismissFacilityNudge()">Not now</button>
    </div>
    <p class="pp-sub ind-gate-note">In <b>Settings → Structures &amp; Markets</b>: search it, hit <b>🔨</b>,
      turn on <b>Manufacture here</b> with its rig tiers. Searching structures needs a connected
      character — there's a button for that on the same panel. <b>Job slots</b> links to it too if you
      dismiss this.</p>
  </div></div>`;
}

function indDismissFacilityNudge() {
  localStorage.setItem('indFacilityNudge', 'off');
  const gate = document.getElementById('indGate');
  if (gate) gate.style.display = 'none';
}

// ── First-run setup ─────────────────────────────────────────────────────────────────────────
// Mirrors the Reactions onboarding gate (_rxApplyGate): a blocking screen with numbered steps and
// a Save & continue, remembered per ACCOUNT so it shows once rather than once per browser.
//
// One rule shapes the whole thing: **every step must be completable right here.** The old gate
// demanded a build structure, which needs structure search, which needs a market character — a
// chain a PI-only player can't finish, on a screen they couldn't get past. So the required step
// (where you build) is a dropdown that already has a valid answer, the rest are optional, and
// Save & continue is never disabled. Nothing here can strand anyone.
let _indOnboarded = true;      // assume yes until the settings load says otherwise — a failed
                               // fetch must not throw a setup screen at an established user
let _indLastSourceKey = '';    // the stock source the previous build was bound to
let _indLastSourceKeys = [];   // …and the whole remembered set, for a build gathered from several
let _indSourceSets = [];       // named, reusable groups of sources ("reaction stock")

function _indRenderWizard(hasStructure) {
  const gate = document.getElementById('indGate');
  const content = document.getElementById('indContent');
  if (!gate || !content) return;
  content.style.display = 'none';
  gate.style.display = '';

  // The same facility options the plan form uses — built by indPopulateFacility, which has already
  // run. Presets are real answers, not placeholders: an NPC station or a T1-rigged structure costs
  // a build correctly, so nobody needs a structure configured to get a true plan.
  const cur = (document.getElementById('indFacility') || {}).value || '';
  const opts = Object.keys(_indFacilityMap).map(k =>
    `<option value="${_esc(k)}"${k === cur ? ' selected' : ''}>`
    + `${_esc(_indFacilityLabel[k] || k)}${k.startsWith('s:') ? ' — your structure' : ''}</option>`).join('');

  gate.innerHTML =
    `<section class="pp-card ind-wizard">`
    + `<div class="pp-card-title">Set up manufacturing`
    + `<span class="pp-card-hint">— one required step, two worth doing. All of it changeable later.</span></div>`
    + `<div class="ind-body">`

    // Step 1 — where you build (required, and already answered)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">1</span>Where you build</div>`
    + `<div class="rx-onboard-step-b">`
    + `<select id="indWizFacility" class="ind-wiz-sel">${opts}</select>`
    + `<div class="pp-card-hint" style="margin-top:6px">A structure's rigs change the materials and`
    + ` time of every job, so this drives every cost and duration. Pick the closest match — or add`
    + ` the structure you really build in for its exact ME &amp; TE.`
    + (hasStructure ? '' : ` <button class="ind-link-btn" onclick="openSettingsModal('markets')">Add my structure</button>`)
    + `</div></div></div>`

    // Step 2 — characters and the slots they contribute (informational, never blocking)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">2</span>`
    + `Characters &amp; slots<span class="rx-onboard-opt">optional</span></div>`
    + `<div class="rx-onboard-step-b"><div id="indWizSlots"><div class="pp-loading"><span class="pp-spinner"></span> Reading your slots…</div></div>`
    + `<div id="indWizSlotWarn"></div>`
    + `<div class="settings-connect-row" style="margin-top:8px">`
    + `<button class="pp-connect-btn" onclick="indWizConnect()">Connect a character</button>`
    + `<span class="pp-card-hint">Brings its real slots, skills and blueprints. Without one we plan`
    + ` against un-researched blueprints and default skills. ${_connectScopeNote()}</span></div></div></div>`

    // Step 3 — the build system, folded (this is the one people skip, and skipping it is fine)
    + `<div class="rx-onboard-step"><details><summary class="rx-onboard-step-h" style="cursor:pointer">`
    + `<span class="rx-onboard-num">3</span>Build system &amp; fees<span class="rx-onboard-opt">optional</span></summary>`
    + `<div class="rx-onboard-step-b">`
    + `<div class="pp-card-hint" style="margin-bottom:8px">Job installation fees are the system's cost`
    + ` index × the job's value, plus tax. Leave this blank and we count only the 4% SCC surcharge, so`
    + ` fees come out light — everything else in the plan is unaffected.</div>`
    + ((typeof _rxAccountSettingsFormHtml === 'function') ? _rxAccountSettingsFormHtml()
        : `<button class="ind-bp-btn" onclick="openSettingsModal('markets')">Open Structures &amp; Markets</button>`)
    + `</div></details></div>`

    + `<div class="rx-onboard-foot">`
    + `<button class="rx-onboard-connect" onclick="indWizSave()">Save &amp; continue</button>`
    + `<span id="indWizMsg" class="pp-card-hint"></span></div>`
    + `</div></section>`;

  if (typeof _loadRxAccountSettings === 'function') _loadRxAccountSettings();
  _indWizLoadSlots();
}

async function _indWizLoadSlots() {
  const d = await indLoadSlots('indWizSlots');
  const warn = document.getElementById('indWizSlotWarn');
  if (!warn) return;
  // Zero capacity isn't an error and mustn't block — but it does make the plan render a 0h build
  // with an empty "do this now", which looks broken unless it's called out here.
  warn.innerHTML = (d && (d.manufacturing_slots || d.reaction_slots)) ? ''
    : `<p class="pp-warn" style="margin:8px 0 0">No usable job slots yet, so plans will show nothing`
      + ` to start. A character needs Mass Production (or Mass Reactions) trained and its skills`
      + ` connected here. You can carry on and set this up later.</p>`;
}

function indWizConnect() {
  indEsiConnect(() => { _indWizLoadSlots(); indPopulateFacility(); });
}

async function indWizSave() {
  const msg = document.getElementById('indWizMsg');
  const wiz = document.getElementById('indWizFacility');
  const sel = document.getElementById('indFacility');
  // The wizard's dropdown is a second view of the plan form's, so hand the choice over to the
  // control that already owns saving and restoring it rather than inventing a parallel path.
  if (wiz && sel && wiz.value) {
    sel.value = wiz.value;
    try { localStorage.setItem('indFacility', wiz.value); } catch (e) {}
  }
  if (msg) msg.textContent = 'Saving…';
  try {
    await apiSend('PUT', '/api/industry/settings', _indSettingsBody());
    await apiSend('POST', '/api/industry/onboarding/complete');
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  _indOnboarded = true;
  onIndustryTabOpen();     // re-run: set up now, so the gate lifts and the tab loads for real
}

// Lifetime manufacturing ledger tiles — shown ONLY once the account has actually completed a
// manufacturing job (opt-in-by-use), so the stats never clutter the tab for someone who hasn't
// used it. Forward-only turnover + net profit from real completions.
async function indLoadLifetime() {
  const el = document.getElementById('indLifetime');
  if (!el) return;
  try {
    const d = await api('/api/industry/lifetime');
    if (!d.used) { el.innerHTML = ''; return; }
    const since = d.since ? new Date(d.since * 1000).toLocaleDateString() : '';
    el.innerHTML = `<div class="an-stats">`
      + `<div class="an-stat"><div class="an-stat-lbl">Lifetime turnover${since ? ' · since ' + _esc(since) : ''}</div><div class="an-stat-val">${fmtIsk(d.turnover)}</div></div>`
      + `<div class="an-stat an-ok"><div class="an-stat-lbl">Lifetime net profit</div><div class="an-stat-val">${fmtIsk(d.net_profit)}</div></div>`
      + `<div class="an-stat"><div class="an-stat-lbl">Jobs completed</div><div class="an-stat-val">${d.jobs}</div></div>`
      + `</div>`;
  } catch (e) { el.innerHTML = ''; }
}

// ── Job slots (modal) + compact tab summary ─────────────────────────────────────────────────
async function indLoadSetupSummary() {
  const sum = document.getElementById('indSetupSummary');
  const rem = document.getElementById('indConnectReminder');
  let slots = null, bp = null;
  try { slots = await api('/api/industry/slots'); } catch (e) {}
  try { bp = await api('/api/industry/blueprints'); } catch (e) {}
  const txt = (() => {
    const s = slots ? `<b>${slots.manufacturing_free}/${slots.manufacturing_slots}</b> mfg · <b>${slots.reaction_free}/${slots.reaction_slots}</b> rx slots free` : '';
    const b = bp ? (bp.connected ? ` · <span class="ind-bp-ok">${bp.owned_count} blueprints</span>` : '') : '';
    return s + b;
  })();
  if (sum) sum.innerHTML = txt;
  const idle = document.getElementById('indSetupSummaryIdle');
  if (idle) idle.innerHTML = txt;
  if (rem) {
    if (bp && !bp.connected) {
      rem.style.display = '';
      // Points at Settings → Blueprints & formulas, where the connect button now lives — it used to
      // open Setup & slots, which no longer holds the blueprint panel.
      rem.innerHTML = `Using default ME/TE. <button class="ind-link-btn" onclick="openSettingsModal('blueprints')">Connect a character</button> to plan with your real blueprints and reaction formulas.`;
    } else {
      rem.style.display = 'none';
    }
  }
}

// Job slots only. Blueprints, formulas and stock moved to Settings → Blueprints & formulas
// (_loadBlueprintsSettings in planetary.js calls indLoadBlueprints/indLoadAssets on reveal), so
// this no longer fires those two reads — the panels they paint aren't in this modal any more.
function indOpenSetup() {
  document.getElementById('indSetupModal').style.display = '';
  const adm = document.getElementById('indAdminSection');
  // Hidden rather than merely refused: the endpoint is admin-gated anyway, so showing the control
  // to everyone would only offer an action that answers 403.
  if (adm) adm.style.display = (typeof _featuresIsAdmin !== 'undefined' && _featuresIsAdmin) ? '' : 'none';
  indLoadSlots();
}

// Replay the first-run setup screen on your own account. Admin-only, and only useful to an admin:
// everyone who has ever used the tab is marked set-up by the backfill, so there is otherwise no way
// to look at the thing every new user sees first.
async function indResetOnboarding() {
  const msg = document.getElementById('indResetMsg');
  if (msg) msg.textContent = 'Resetting…';
  try { await apiSend('POST', '/api/industry/onboarding/reset'); }
  catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  _indOnboarded = false;
  indCloseSetup();
  onIndustryTabOpen();      // re-runs the gate, which now renders the setup screen
}

// ── Stock on hand (ESI assets) — Settings → Blueprints & formulas ───────────────────────────
// Scanning assets makes plans subtract what you already own, and lets progress report "done"
// without guessing a start date. Opt-in and manual — a full asset list is a heavy ESI call.
// EVE tokens carry only the scopes granted at their last login, so characters connected before the
// assets scope existed hold valid tokens that simply can't read assets. Name them and offer the
// re-auth — one login per character, and the callback stores the new scope set.
function _indReauthHtml(names) {
  if (!names || !names.length) return '';
  return `<div class="ind-reauth"><span class="ind-reauth-txt">`
    + `<b>${names.length} character${names.length === 1 ? '' : 's'}</b> can't be scanned yet: `
    + `${names.map(_esc).join(', ')}. They were connected before asset access was added — `
    + `log each one in once to grant it.</span>`
    + `<button class="ind-bp-btn ind-bp-connect" onclick="indReauthAssets()">Reconnect a character</button></div>`;
}

// One SSO popup for every industry scope: /auth/login?industry=1 requests the unified scope set, so
// a single login brings the character up to date on assets, blueprints and jobs at once. `then` runs
// when the popup reports back; the listener removes itself either way so repeated connects don't
// stack up handlers that re-fire on the next login.
function indEsiConnect(then) {
  const w = window.open('/auth/login?industry=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      then();
    }
  });
}

function indReauthAssets() {
  indEsiConnect(() => { indLoadAssets(); indLoadSetupSummary(); });
}

// The director connect is its OWN flow, and deliberately not the one every other button uses: it
// asks for corporation-wide read access, which EVE gates behind the Director role and which nobody
// else can use. Only someone who has just clicked "connect a director" should ever be shown those
// lines on the consent screen.
function indConnectDirector() {
  const w = window.open('/auth/login?director=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      indLoadAssets();
      indLoadSetupSummary();
    }
  });
}

async function indLoadAssets() {
  const el = document.getElementById('indAssets');
  if (!el) return;
  try {
    const d = await api('/api/industry/assets');
    if (!d.connected) {
      el.innerHTML = `<span class="ind-bp-hint">Tell the planners what you already own — materials `
        + `<b>and reaction formulas</b> — so they stop asking you to buy it, progress can tell `
        + `what's finished, and the reaction concurrency cap knows how many formulas you have.</span>`
        + _indReauthHtml(d.needs_reauth)
        + `<div class="ind-src-actions">`
        + (d.scannable ? `<button class="ind-bp-btn ind-bp-connect" onclick="indRefreshAssets()">Scan assets</button>` : '')
        + `<button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button>`
        + _indCorpScanBtn(d) + `<span id="indCorpMsg" class="ind-src-meta"></span></div>`
        + _indPasteFormHtml();
      return;
    }
    const when = d.fetched_at ? new Date(d.fetched_at * 1000).toLocaleString() : '';
    // Every source is opt-in: counting a hangar you can't actually draw from would make the
    // planner build too little, which is worse than ignoring stock altogether.
    // Grouped by station/structure, so two identically-named cans in different stations are told
    // apart here as well as in the pickers.
    _indSourceSets = d.sets || [];
    const rows = _indGroupSources(d.sources || []).map(g =>
      `<div class="ind-src-group"><div class="ind-src-place">${_esc(g.label)}</div>`
      + g.items.map(sc => {
        const sub = sc.kind === 'container'
          ? `container${sc.parent ? ' in ' + _esc(sc.parent) : ''}`
          : sc.kind === 'paste' ? 'pasted' : sc.corp ? 'corp hangar' : 'hangar';
        const del = sc.kind === 'paste'
          ? `<button class="ind-src-del" title="Remove this pasted stock" onclick="event.preventDefault();indDeleteSource('${_esc(sc.key)}')">✕</button>` : '';
        // "What's in it" is a question the list could not answer — it said where a paste was and
        // how many item types it held, which is not enough to see whether the formula you pasted
        // it for is actually in there (reported 2026-08-08).
        const eid = 'indsrc-' + _srcDomId(sc.key);
        return `<label class="ind-src-row"><input type="checkbox" ${sc.enabled ? 'checked' : ''} `
          + `onchange="indToggleSource('${_esc(sc.key)}', this.checked)">`
          + `<span class="ind-src-name">${_esc(sc.name)}</span>`
          + `<span class="ind-src-meta">${sub} · ${sc.item_count} item type${sc.item_count === 1 ? '' : 's'}</span>`
          + `<button class="ind-src-peek" title="Show what this holds"`
          + ` onclick="event.preventDefault();indToggleSourceItems('${_esc(sc.key)}')">contents</button>`
          + `${del}</label><div class="ind-src-items" id="${eid}" style="display:none"></div>`;
      }).join('') + `</div>`).join('');
    el.innerHTML = `<div class="ind-src-hd"><span class="ind-bp-ok">✓ ${d.enabled_sources} of ${(d.sources || []).length} `
      + `source${(d.sources || []).length === 1 ? '' : 's'} in use · ${d.distinct_types} item type${d.distinct_types === 1 ? '' : 's'} counted`
      + `${when ? ' · scanned ' + _esc(when) : ''}</span>`
      + `<button class="ind-bp-btn" onclick="indRefreshAssets()">Rescan</button></div>`
      + `<p class="ind-src-help">Tick the hangars and containers the planners may take materials from. Nothing is used until you pick it.`
      + ` Any <b>reaction formula</b> in a ticked source counts as one you own, and caps how many reaction jobs can run at once.`
      + (_featureActive('industry_plan_sources')
          ? ` A build with its own containers picked counts <b>those</b> and ignores this list — this is what everything else falls back on.` : '')
      + `</p>`
      + `<div class="ind-src-list">${rows || '<span class="ind-bp-hint">No usable hangars found.</span>'}</div>`
      + _indReauthHtml(d.needs_reauth)
      + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button>`
      + _indCorpScanBtn(d) + `<span id="indCorpMsg" class="ind-src-meta"></span></div>`
      + _indPasteFormHtml();
  } catch (e) { el.innerHTML = ''; }
}

// Reading corp hangars over ESI needs the Director role, which most players don't have — so this is
// offered as an extra button rather than folded into the normal rescan, and its failure mode ("not
// a director") is a plain answer rather than an error.
function _indCorpScanBtn(d) {
  if (!_featureActive('industry_corp_assets')) return '';
  if (!d.corp_scannable) {
    return `<button class="ind-bp-btn" onclick="indConnectDirector()" title="Corp hangars need `
      + `corporation-wide read access, which EVE only grants to a Director. Nobody else is asked `
      + `for it — connect the director character here.">Connect a director</button>`;
  }
  return `<button class="ind-bp-btn" onclick="indRefreshCorpAssets()" title="Read the corp hangars `
    + `and containers of any corp you're a director in">Scan corp hangars</button>`;
}

async function indRefreshCorpAssets() {
  const msg = document.getElementById('indCorpMsg');
  if (msg) msg.textContent = 'Reading corp assets…';
  let d = null;
  try { d = await apiSend('POST', '/api/industry/assets/refresh-corp'); }
  catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  // Say which of the two "nothing happened" cases it was: no role is permanent and actionable,
  // an empty corp hangar is not.
  const note = d.scanned
    ? `Read ${d.corporations.join(', ')}.`
    : (d.no_role || []).length
      ? `${d.no_role.join(', ')} ${d.no_role.length === 1 ? 'is' : 'are'} not a Director — EVE only lets `
        + `directors read corp assets. Paste the hangar instead.`
      : 'No corp hangars found.';
  await indLoadAssets();          // repaints the panel, so the note has to be written after it
  indLoadQueue();
  const m2 = document.getElementById('indCorpMsg');
  if (m2) m2.textContent = note;
}

// How corp/shared hangars get in: paste them. ESI can't read a corp hangar for ordinary members,
// so pasting is the path that works for everyone, not a fallback.
// It is also the ONLY way to declare a reaction formula held outside a character's personal
// hangar — /characters/{id}/blueprints/ never returns those — and the copy used to say "materials"
// throughout, so nobody found it. Formulas are named here on purpose; see test_formula_stock.py.
// Four sentences of justification became one line of instruction — reported 2026-08-08 as
// "extremely long winded". Why a paste is the only way to declare a corp-hangar formula is real,
// but it belongs where someone goes looking for it, not in front of the box every time.
function _indPasteFormHtml() {
  return `<div id="indPasteForm" class="ind-paste" style="display:none">
    <p class="ind-src-help">In game: open the hangar, <b>Ctrl+A</b>, <b>Ctrl+C</b>, paste here.
      Include reaction formulas — a corp or shared hangar can only be declared this way.</p>
    <input type="text" id="indPasteName" placeholder="Name this stock — e.g. Corp hangar: Materials &amp; formulas">
    <textarea id="indPasteText" rows="6" placeholder="Tritanium&#9;1 000 000&#10;Caesarium Cadmide Reaction Formula&#9;1"></textarea>
    <div class="ind-src-actions">
      <button class="ind-primary-btn" onclick="indSavePaste()">Add as stock</button>
      <button class="ind-bp-btn" onclick="indClosePaste()">Cancel</button>
      <span id="indPasteMsg" class="ind-src-meta"></span>
    </div>
  </div>`;
}

// One DOM id per source key: keys carry ':' and '/' (char:123, corp:456, paste:1), neither of
// which can go in an id and be selected back out.
function _srcDomId(key) {
  return String(key).replace(/[^a-zA-Z0-9_-]/g, '_');
}

const _indSrcItems = {};      // key -> rows, so re-opening a source doesn't re-fetch it

async function indToggleSourceItems(key) {
  const el = document.getElementById('indsrc-' + _srcDomId(key));
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = '';
  if (!_indSrcItems[key]) {
    el.innerHTML = '<span class="ind-bp-hint">Reading…</span>';
    try {
      const d = await api('/api/industry/assets/sources/' + encodeURIComponent(key) + '/items');
      _indSrcItems[key] = d.items || [];
    } catch (e) {
      el.innerHTML = `<span class="ind-bp-hint">${_esc(String(e.message || e))}</span>`;
      return;
    }
  }
  const items = _indSrcItems[key];
  if (!items.length) { el.innerHTML = '<span class="ind-bp-hint">Nothing in it.</span>'; return; }
  // Formulas first and marked: they are why most of these pastes exist, and one of them being
  // missing is the thing you came here to check.
  const fx = items.filter(i => i.formula);
  const rest = items.filter(i => !i.formula);
  const row = i => `<div class="ind-src-item${i.formula ? ' ind-src-item-fx' : ''}">`
    + `<span>${_esc(i.name)}</span><span>${Number(i.qty).toLocaleString()}</span></div>`;
  el.innerHTML = (fx.length
      ? `<div class="ind-src-item-hd">${fx.length} reaction formula${fx.length === 1 ? '' : 's'}</div>`
        + fx.map(row).join('') : '')
    + (rest.length
      ? `<div class="ind-src-item-hd">${rest.length} other item type${rest.length === 1 ? '' : 's'}</div>`
        + rest.map(row).join('') : '');
}

// The "why" behind the paste rules is real and belongs where someone goes looking for it, not in
// front of the box every time (reported 2026-08-08: "extremely long winded").
function indTogglePasteHelp(a) {
  const box = a && a.closest('.ind-paste') && a.closest('.ind-paste').querySelector('.ind-paste-help');
  if (!box) return;
  const open = box.style.display === 'none';
  box.style.display = open ? '' : 'none';
  a.textContent = open ? 'Hide details' : 'Details';
}

function indOpenPaste() {
  const f = document.getElementById('indPasteForm');
  if (f) { f.style.display = ''; const t = document.getElementById('indPasteText'); if (t) t.focus(); }
}
function indClosePaste() {
  const f = document.getElementById('indPasteForm');
  if (f) f.style.display = 'none';
}

async function indSavePaste() {
  const name = (document.getElementById('indPasteName') || {}).value || '';
  const text = (document.getElementById('indPasteText') || {}).value || '';
  const msg = document.getElementById('indPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  try {
    const d = (await apiSend('POST', '/api/industry/assets/paste', { name, text })) || {};
    if (d.error) {
      if (msg) msg.textContent = d.error === 'unrecognized' ? "Couldn't match any item names." : 'Nothing readable in that paste.';
      return;
    }
    const skipped = (d.unknown || []).length;
    if (msg) msg.textContent = `Added ${d.added} item type${d.added === 1 ? '' : 's'}${skipped ? ` · ${skipped} name(s) not recognised` : ''}.`;
    indLoadAssets();
    indLoadQueue();
  } catch (e) { if (msg) msg.textContent = String(e); }
}

async function indDeleteSource(key) {
  try { await apiSend('DELETE', '/api/industry/assets/sources/' + encodeURIComponent(key)); } catch (e) {}
  indLoadAssets();
  indLoadQueue();
}

async function indToggleSource(key, on) {
  try {
    await apiSend('POST', '/api/industry/assets/sources', { keys: [key], enabled: !!on });
  } catch (e) {}
  indLoadAssets();
  indLoadQueue();     // stock changes both the plan and the progress numbers
}

async function indRefreshAssets() {
  const el = document.getElementById('indAssets');
  if (el) el.innerHTML = '<span class="ind-bp-hint">Reading assets… (a full asset list can take a moment)</span>';
  let d = null;
  try { d = await apiSend('POST', '/api/industry/assets/refresh'); } catch (e) {}
  if (d && !d.connected && (d.needs_reauth || []).length && el) {
    el.innerHTML = _indReauthHtml(d.needs_reauth) + _indPasteFormHtml();
    return;
  }
  indLoadAssets();
  indLoadQueue();
}

function indCloseSetup() {
  document.getElementById('indSetupModal').style.display = 'none';
  indLoadSetupSummary();   // reflect any changes (e.g. just connected) back on the tab
}

// ── Blueprint auto-read (ME/TE + ownership from ESI) ────────────────────────────────────────
async function indLoadBlueprints() {
  const el = document.getElementById('indBlueprints');
  if (!el) return;
  try {
    const d = await api('/api/industry/blueprints');
    if (d.connected) {
      el.innerHTML = `<span class="ind-bp-ok">✓ ${d.owned_count} blueprint${d.owned_count === 1 ? '' : 's'} and reaction formula${d.owned_count === 1 ? '' : 's'} detected — using your real ME/TE</span>`
        + `<button class="ind-bp-btn" onclick="indRefreshBlueprints()">Refresh</button>`;
    } else {
      el.innerHTML = `<span class="ind-bp-hint">Connect a character to auto-read the blueprints and reaction formulas it owns — their ME/TE and which BPOs you hold, with no manual entry.</span>`
        + `<button class="ind-bp-btn ind-bp-connect" onclick="indConnectBlueprints()">Connect blueprints</button>`;
    }
  } catch (e) { el.innerHTML = ''; }
}

function indConnectBlueprints() {
  indEsiConnect(indRefreshBlueprints);
}

async function indRefreshBlueprints() {
  const el = document.getElementById('indBlueprints');
  if (el) el.innerHTML = '<span class="ind-bp-hint">Reading blueprints…</span>';
  try { await apiSend('POST', '/api/industry/blueprints/refresh'); } catch (e) {}
  indLoadBlueprints();
  indLoadSetupSummary();   // this panel now lives in Settings, so the tab's own summary needs telling
}

// ── Blueprints declared by hand — Settings → Blueprints & formulas ──────────────────────────
// The ESI panel above reads PERSONAL blueprints only, so a print in a corp or shared hangar can be
// stated here and nowhere else. Same section on purpose: "what prints do I own" is one question.
// A declaration REPLACES what ESI read for its product (see owned_blueprints' merge rule) — the
// hint in index.html says so, because a user who declares one of three copies has just told the
// plan they hold one.
let _indManualPick = null;      // {type_id, name} chosen in the add row

async function indLoadManualBps() {
  const sec = document.getElementById('indManualBpSubsec');
  const el = document.getElementById('indManualBps');
  if (!el || !sec) return;
  if (!_featureActive('industry_manual_blueprints')) { sec.style.display = 'none'; return; }
  sec.style.display = '';
  let d = null;
  try { d = await api('/api/industry/manual-blueprints'); } catch (e) { el.innerHTML = ''; return; }
  // Batch rows are summarised, not listed one by one: a pasted industry window is a hundred-odd
  // prints and a list that long buries the handful someone typed in themselves.
  const batches = (d.batches || []).map(b => {
    // Where the batch's prints are, when we know it: read out of the paste (the long layout names a
    // structure and a container per print) or asked for at import (the short layout names neither).
    // Display only — nothing in planning, routing or the build pins reads it, and the batch is
    // identified by its NAME, so the title is always the name. One batch usually spans several
    // containers, so a place is only named when there is exactly one of them; otherwise the count.
    const where = b.places > 1 ? ` · in ${b.places} places`
      : (b.structure && b.container) ? ` · in ${b.container}, ${b.structure}`
        : (b.container || b.structure) ? ` · in ${b.container || b.structure}` : '';
    // ...and WHAT is in it. The row said how many prints and where they were, which is enough to
    // know a paste landed and not enough to know whether the formula you pasted it for is in there
    // (reported 2026-08-08: "I can see where it's located, but I cannot see what it contains").
    return `<div class="ind-src-row"><span class="ind-src-name">${_esc(b.name)}</span>`
    + `<span class="ind-src-meta">pasted · ${b.prints} print${b.prints === 1 ? '' : 's'}`
    + ` · ${b.products} product${b.products === 1 ? '' : 's'}${_esc(where)}</span>`
    + `<button class="ind-src-peek" title="Show what this batch declares"`
    + ` onclick="indToggleBatchItems('${_esc(b.batch)}')">contents</button>`
    + `<button class="ind-src-del" title="Remove this pasted batch"`
    + ` onclick="indDeleteManualBpBatch('${_esc(b.batch)}')">✕</button></div>`
    + `<div class="ind-src-items" id="indbpb-${_srcDomId(b.batch)}" style="display:none"></div>`;
  }).join('');
  const rows = (d.entries || []).filter(e => !e.batch).map(e => {
    const runs = e.kind === 'bpo' ? 'original (BPO)' : `${e.runs} run${e.runs === 1 ? '' : 's'}`;
    const pref = e.prefer === 'bpo' ? ' · use the original'
      : e.prefer === 'bpc' ? ' · use the copies' : '';
    // A quantity-0 row declares no print at all — it only carries the BPO-vs-BPC choice, which is
    // how someone whose prints ESI CAN see states a preference without retyping the holding.
    const qty = e.quantity > 0 ? `${e.quantity}× ` : 'preference only · ';
    return `<div class="ind-src-row"><span class="ind-src-name">${_esc(e.name)}</span>`
      + `<span class="ind-src-meta">${qty}ME ${e.me} · TE ${e.te} · ${runs}${_esc(pref)}</span>`
      + `<button class="ind-src-del" title="Remove this declaration"`
      + ` onclick="indDeleteManualBp(${e.id})">✕</button></div>`;
  }).join('');
  const list = batches + rows;
  el.innerHTML = `<div class="ind-src-list">${list || '<span class="ind-bp-hint">Nothing declared yet.</span>'}</div>`
    + _indBpPasteFormHtml()
    + `<div class="ind-manual-bp-add">`
    + `<input id="indManualBpSearch" class="bug-input" placeholder="Product (e.g. Nitrogen Fuel Block)"`
    + ` autocomplete="off" oninput="indManualBpSearch(this.value)">`
    + `<div id="indManualBpResults" class="ind-search-results" style="display:none"></div>`
    + `<label>ME<input id="indManualBpMe" type="number" min="0" max="10" value="0" class="bug-input dummy-num"></label>`
    + `<label>TE<input id="indManualBpTe" type="number" min="0" max="20" value="0" class="bug-input dummy-num"></label>`
    + `<label title="Blank = an original (BPO), which covers any batch and is never consumed">Runs`
    + `<input id="indManualBpRuns" type="number" min="0" placeholder="BPO" class="bug-input dummy-num"></label>`
    + `<label title="How many separate prints you hold. 0 declares no print and only sets the BPO/BPC choice below.">Qty`
    + `<input id="indManualBpQty" type="number" min="0" value="1" class="bug-input dummy-num"></label>`
    + `<label title="When you hold both an original and copies of this product, which should the plan spend? The original costs no copies but runs one job at a time; copies run side by side but are consumed.">Prefer`
    + `<select id="indManualBpPrefer" class="bug-input">`
    + `<option value="">no preference</option><option value="bpo">the original</option>`
    + `<option value="bpc">the copies</option></select></label>`
    + `<button onclick="indSaveManualBp(this)">Add</button>`
    + `<span id="indManualBpMsg" class="bug-status-msg"></span></div>`;
  _indManualPick = null;
  _indBpBatchEntries = {};
  for (const e of d.entries || []) {
    if (e.batch) (_indBpBatchEntries[e.batch] = _indBpBatchEntries[e.batch] || []).push(e);
  }
  _indBpFillStructures();       // async, and the form is usable without it — free text still works
}

// The batch's own prints, already in the payload — no second round trip, and no way for the list
// and the contents to disagree about what was imported.
let _indBpBatchEntries = {};

function indToggleBatchItems(batch) {
  const el = document.getElementById('indbpb-' + _srcDomId(batch));
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = '';
  const entries = (_indBpBatchEntries[batch] || []).slice()
    .sort((a, b) => a.name.localeCompare(b.name));
  if (!entries.length) { el.innerHTML = '<span class="ind-bp-hint">Nothing in it.</span>'; return; }
  // Formulas first and marked: a reaction library is what most of these pastes are for, and one
  // of them being absent is the thing you opened this to check.
  const isFx = e => /Reaction Formula/i.test(e.name || '');
  const fx = entries.filter(isFx);
  const rest = entries.filter(e => !isFx(e));
  const row = e => {
    const runs = e.kind === 'bpo' ? 'BPO' : `${e.runs} run${e.runs === 1 ? '' : 's'}`;
    const me = (e.me || e.te) ? ` · ME ${e.me}/TE ${e.te}` : '';
    return `<div class="ind-src-item${isFx(e) ? ' ind-src-item-fx' : ''}">`
      + `<span>${e.quantity > 1 ? `${e.quantity}× ` : ''}${_esc(e.name)}</span>`
      + `<span>${_esc(runs)}${_esc(me)}</span></div>`;
  };
  el.innerHTML = (fx.length
      ? `<div class="ind-src-item-hd">${fx.length} reaction formula${fx.length === 1 ? '' : 's'}</div>`
        + fx.map(row).join('') : '')
    + (rest.length
      ? `<div class="ind-src-item-hd">${rest.length} other blueprint${rest.length === 1 ? '' : 's'}</div>`
        + rest.map(row).join('') : '');
}

// Pasting the industry window, which is how a real library arrives — one at a time is unusable at
// ~100 formulas per character. One paste per character, named: re-pasting a name replaces THAT
// batch and leaves the others alone (same model as a pasted stock source).
function _indBpPasteFormHtml() {
  return `<div class="ind-paste">
    <p class="ind-src-help">Or paste a whole industry window: in game open <b>Industry →
      Blueprints</b>, <b>Ctrl+A</b>, <b>Ctrl+C</b>, paste here. One paste is one batch — re-pasting
      the same name replaces it.
      <a href="#" onclick="event.preventDefault();indTogglePasteHelp(this)">Details</a></p>
    <div class="ind-paste-help" style="display:none">
      <p class="ind-src-help">Copy it with <b>nothing selected in the tree</b> and every line carries
      the structure and container it sits in — we record that and offer it as the batch name.
      Otherwise pick the structure below; it only labels the batch.</p>
      <p class="ind-src-help">A pasted product's declaration is used <b>instead of</b> what ESI read
      for it, on every character — so paste each character's window, not just one.</p>
    </div>
    <div class="ind-src-actions">
      <label>Where are these?
        <select id="indBpPasteStruct" class="bug-input" onchange="indBpPasteStructChanged()">
          <option value="">— the paste says, or just name it below —</option>
        </select></label>
      <input type="text" id="indBpPasteStructOther" class="bug-input" style="display:none"
             placeholder="Structure name" autocomplete="off">
    </div>
    <input type="text" id="indBpPasteName" placeholder="Name this batch — e.g. Main's industry window">
    <textarea id="indBpPasteText" rows="6" placeholder="Formulas:&#10;4 x Nanotransistors Reaction Formula&#9;0&#9;0&#9;-1&#9;Composite&#10;Amarr Shuttle Blueprint&#9;10&#9;20&#9;-1&#9;Shuttle"></textarea>
    <div class="ind-src-actions">
      <button class="ind-bp-btn" onclick="indPreviewManualBpPaste()">Preview</button>
      <button class="ind-primary-btn" onclick="indImportManualBpPaste()">Import</button>
      <span id="indBpPasteMsg" class="ind-src-meta"></span>
    </div>
  </div>`;
}

// The structure picker's options are the account's own build structures — the same `/api/markets`
// rows `indPopulateFacility` builds the Industry Facility dropdown from, so there is one list of
// "your structures" and not two that can disagree. Plus free text, because prints live in plenty of
// places that are not a build structure, and NOTHING, because typing a batch name still works and
// this must not become a step on the way in.
const IND_BP_OTHER = '__other';
async function _indBpFillStructures() {
  const sel = document.getElementById('indBpPasteStruct');
  if (!sel) return;
  let names = [];
  try {
    const d = await api('/api/markets');
    names = (d.markets || [])
      .filter(m => m.kind === 'structure' && (m.build_mfg || m.build_rx))
      .map(m => m.name);
  } catch (e) { names = []; }
  sel.innerHTML = `<option value="">— the paste says, or just name it below —</option>`
    + names.map(n => `<option value="${_esc(n)}">${_esc(n)}</option>`).join('')
    + `<option value="${IND_BP_OTHER}">Somewhere else…</option>`;
}

function indBpPasteStructChanged() {
  const sel = document.getElementById('indBpPasteStruct');
  const other = document.getElementById('indBpPasteStructOther');
  if (!sel || !other) return;
  other.style.display = sel.value === IND_BP_OTHER ? '' : 'none';
  if (sel.value === IND_BP_OTHER) other.focus();
}

// The structure the user picked for a paste that doesn't carry one. Recorded on the rows and, if
// nothing was typed, used to name the batch. '' means they picked nothing, which is the plain
// typed-name path. It never decides what a re-paste replaces — the name does.
function _indBpPasteStructure() {
  const sel = document.getElementById('indBpPasteStruct');
  const v = sel ? sel.value : '';
  if (v !== IND_BP_OTHER) return v || '';
  const other = document.getElementById('indBpPasteStructOther');
  return other ? (other.value || '').trim() : '';
}

// One sentence for both the preview and the result, so what the import reports can never read as a
// different answer from what the preview promised.
function _indBpPasteSummary(d) {
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const bits = [`${plural(d.formulas || 0, 'formula')}, ${plural(d.blueprints || 0, 'blueprint')}`
    + ` — ${plural(d.prints || 0, 'print')} across ${plural(d.products || 0, 'product')}`];
  const un = (d.unknown || []).concat(d.no_product || []);
  if (un.length) {
    bits.push(`${plural(un.length, 'name')} not recognised: ${un.slice(0, 5).join(', ')}`
      + (un.length > 5 ? '…' : ''));
  }
  if ((d.ignored || []).length) bits.push(`${plural(d.ignored.length, 'line')} skipped`);
  // A window copied with nothing selected names where each print is. It is all ONE batch — the
  // places are reported because they are worth seeing, not because they split anything.
  const locs = d.locations || [];
  if (locs.length) {
    bits.push(`in ${plural(locs.length, 'container')}: `
      + locs.slice(0, 4).map(l => l.name).join(', ') + (locs.length > 4 ? '…' : ''));
  }
  return bits.join(' · ') + '.';
}

async function indPreviewManualBpPaste() {
  const text = (document.getElementById('indBpPasteText') || {}).value || '';
  const msg = document.getElementById('indBpPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  try {
    const d = (await apiSend('POST', '/api/industry/manual-blueprints/paste/preview',
      { name: '', text })) || {};
    // If the window said where its prints are, offer that as the batch name — the name is the
    // batch's identity, so putting it in the box (rather than applying it invisibly) is what lets
    // someone KEEP it across a re-paste after moving the prints somewhere else.
    const nameEl = document.getElementById('indBpPasteName');
    if (nameEl && !nameEl.value.trim() && d.suggested_name && (d.locations || []).length) {
      nameEl.value = d.suggested_name;
    }
    if (msg) {
      msg.textContent = (d.prints ? 'Will declare ' : 'Nothing to declare — ') + _indBpPasteSummary(d);
    }
  } catch (e) { if (msg) msg.textContent = String(e.message || e); }
}

async function indImportManualBpPaste() {
  const name = (document.getElementById('indBpPasteName') || {}).value || '';
  const text = (document.getElementById('indBpPasteText') || {}).value || '';
  const msg = document.getElementById('indBpPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Importing…';
  let d = null;
  try {
    d = (await apiSend('POST', '/api/industry/manual-blueprints/paste',
      { name, text, structure: _indBpPasteStructure() })) || {};
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  const r = d.imported || {};
  await indLoadManualBps();       // repaints the panel, so the note has to be written after it
  const m2 = document.getElementById('indBpPasteMsg');
  if (m2) {
    m2.textContent = r.error === 'empty' ? 'Nothing readable in that paste.'
      : r.error === 'unrecognized' ? "Couldn't match any blueprint names. " + _indBpPasteSummary(r)
        : `Imported "${r.name}" — ` + _indBpPasteSummary(r);
  }
  indLoadQueue();                 // a declared print moves both the ME/TE and the print cap
}

async function indDeleteManualBpBatch(batch) {
  try {
    await apiSend('DELETE', '/api/industry/manual-blueprints/batches/' + encodeURIComponent(batch));
  } catch (e) {}
  indLoadManualBps();
  indLoadQueue();
}

async function indManualBpSearch(q) {
  const box = document.getElementById('indManualBpResults');
  if (!box) return;
  _indManualPick = null;
  if (!q || q.trim().length < 2) { box.style.display = 'none'; return; }
  try {
    const d = await api('/api/industry/search?q=' + encodeURIComponent(q.trim()));
    if (!d.results || !d.results.length) {
      box.innerHTML = '<div class="ind-search-empty">No buildable match</div>';
      box.style.display = ''; return;
    }
    box.innerHTML = d.results.map(x =>
      `<div class="ind-search-row" onclick="indManualBpPick(${x.type_id}, '${_esc(x.name).replace(/'/g, "\\'")}')">${_esc(x.name)}</div>`
    ).join('');
    box.style.display = '';
  } catch (e) { box.style.display = 'none'; }
}

function indManualBpPick(typeId, name) {
  _indManualPick = { type_id: typeId, name: name };
  const inp = document.getElementById('indManualBpSearch');
  if (inp) inp.value = name;
  const box = document.getElementById('indManualBpResults');
  if (box) box.style.display = 'none';
}

async function indSaveManualBp(btn) {
  const msg = document.getElementById('indManualBpMsg');
  if (!_indManualPick) { if (msg) msg.textContent = 'Pick a product first'; return; }
  const num = (id, dflt) => {
    const v = document.getElementById(id);
    const n = v ? parseInt(v.value, 10) : NaN;
    return isNaN(n) ? dflt : n;
  };
  const runsEl = document.getElementById('indManualBpRuns');
  // Blank runs is the whole encoding for "this is an original" — null on the wire, -1 in the row.
  const runs = (runsEl && runsEl.value.trim() !== '') ? num('indManualBpRuns', 0) : null;
  const prefEl = document.getElementById('indManualBpPrefer');
  if (btn) btn.disabled = true;
  try {
    await apiSend('POST', '/api/industry/manual-blueprints', {
      type_id: _indManualPick.type_id,
      me: num('indManualBpMe', 0), te: num('indManualBpTe', 0),
      runs: runs, quantity: num('indManualBpQty', 1),
      prefer: prefEl ? prefEl.value : '',
    });
  } catch (e) {
    if (msg) msg.textContent = String(e.message || e);
    if (btn) btn.disabled = false;
    return;
  }
  indLoadManualBps();
}

async function indDeleteManualBp(id) {
  try { await apiSend('DELETE', '/api/industry/manual-blueprints/' + id); } catch (e) {}
  indLoadManualBps();
}

// ── Slot pool ───────────────────────────────────────────────────────────────────────────────
// Mounted in two places — the Job slots modal, and step 2 of the first-run wizard — so the markup lives
// in one function. Returns the loaded pool so a caller can react to it (the wizard warns when the
// account has no usable slots at all).
async function indLoadSlots(target) {
  const el = document.getElementById(target || 'indSlots');
  if (!el) return null;
  try {
    const d = await api('/api/industry/slots');
    el.innerHTML = _indSlotsHtml(d);
    return d;
  } catch (e) { el.innerHTML = ''; return null; }
}

function _indSlotsHtml(d) {
    // A placeholder's slots are DECLARED, not read from ESI — say so wherever they're counted, so
    // a pool total is never mistaken for measured capacity.
    const ph = c => c.is_placeholder ? '<span class="pp-char-dummy-badge">placeholder</span> ' : '';
    const chips = (d.characters || []).map(c =>
      `<span class="ind-slot-chip" title="${_esc(c.character_name)}${c.is_placeholder ? ' — placeholder character; these slots are the ones you declared, not read from ESI' : ''}">${ph(c)}${_esc(c.character_name)}: `
      + (c.manufacturing_slots ? `${c.manufacturing_free}/${c.manufacturing_slots}<span class="ind-slot-sub">mfg</span>` : '<span class="ind-slot-sub">no mfg</span>')
      + ` · `
      + (c.reaction_slots ? `${c.reaction_free}/${c.reaction_slots}<span class="ind-slot-sub">rx</span>` : '<span class="ind-slot-sub">no rx</span>')
      + `</span>`
    ).join('');
    return `<div class="ind-slot-tot"><b>${d.manufacturing_free}/${d.manufacturing_slots}</b> manufacturing · `
      + `<b>${d.reaction_free}/${d.reaction_slots}</b> reaction slots free `
      + `<button class="ind-bp-btn" onclick="indRefreshJobs()" title="Re-read running jobs from ESI">Refresh jobs</button></div>`
      + `<div class="ind-slot-chips">${chips || '<span class="pp-sub">No characters — add one to get real slot counts.</span>'}</div>`
      // Never silently drop capacity: say who was left out and why.
      + ((d.excluded || []).length
        ? `<div class="ind-slot-excl"><b>Not used:</b> ` + d.excluded.map(c =>
            `<span title="${_esc(c.reason)}">${_esc(c.character_name)}</span>`).join(', ')
          + `<div class="ind-slot-excl-why">Characters with no slot skills trained (or no skill data), and `
          + `placeholders with no slots declared, are left out — `
          + `their single free slot would inflate every estimate and send you jobs they can't run.</div></div>`
        : '');
}

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

async function _indSearch(q) {
  try {
    const d = await api('/api/industry/search?q=' + encodeURIComponent(q));
    const box = document.getElementById('indSearchResults');
    if (!d.results || !d.results.length) { box.innerHTML = '<div class="ind-search-empty">No buildable match</div>'; box.style.display = ''; return; }
    box.innerHTML = d.results.map(x =>
      `<div class="ind-search-row" onclick="indPick(${x.type_id}, '${_esc(x.name).replace(/'/g, "\\'")}')">${_esc(x.name)}</div>`
    ).join('');
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
function indRestoreMarginal() {
  const el = document.getElementById('indMarginal');
  if (!el) return;
  let v = null;
  try { v = localStorage.getItem('indMarginalPct'); } catch (e) {}
  if (v !== null && !isNaN(parseFloat(v))) el.value = parseFloat(v);
  indOnMarginalInput();
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
function indRestoreMargin() {
  const el = document.getElementById('indMargin');
  if (!el) return;
  let v = null;
  try { v = localStorage.getItem('indMarginPct'); } catch (e) {}
  if (v !== null && !isNaN(parseFloat(v))) el.value = parseFloat(v);
  indOnMarginInput();
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

// Build this component anyway. The plan re-runs (and the slider curve is re-fetched) because
// forcing one component changes the batch every other decision was weighed against.
function indForceBuildType(typeId, name) {
  _indForcedTypes.set(typeId, name || String(typeId));
  _indSweep = null; _indSweepFailed = null;
  return indRunPlan();      // returned so a caller can await the repaint (see _indKeepScroll)
}

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

// Walk the build tree once into { byType (with tier + type_id), tiers (tier -> [entries]), maxT }.
// Shared by the pipeline visualization and the per-stage shopping list so their stage numbering
// always matches — a "Buy" card in the pipeline links to the exact same stage in the list below.
// `bought` = the type_ids that actually appear on the plan's shopping list, and it OVERRIDES the
// tree's own decision. The tree comes from build_plan (pure cost-optimal) while the shopping list
// comes from plan_queue, which additionally flips components to "buy" for speed or negligible
// saving — so the tree alone would show a build step for something you're really purchasing. When
// a type is bought we also stop descending: its sub-materials aren't yours to make any more.
function _indComputeTiers(tree, bought) {
  const buys = bought instanceof Set ? bought : new Set(bought || []);
  // A queue can hold several products, so accept either one root or a list of them. Aggregated
  // demand is shared across them, which is exactly what the tier walk already merges by type_id.
  const roots = Array.isArray(tree) ? tree.filter(Boolean) : (tree ? [tree] : []);
  const byType = {};
  const inputsOf = {};      // type_id -> Set of type_ids it consumes
  const consumersOf = {};   // type_id -> Set of type_ids that consume it
  const walk = ((n, depth) => {
    if (!n) return;
    const isBought = buys.has(n.type_id) && depth > 0;   // the root is always built
    const e = byType[n.type_id] || (byType[n.type_id] = { type_id: n.type_id, name: n.name, decision: n.decision, activity: n.activity, owned: n.owned, qty: 0, runs: 0, tier: depth });
    e.qty += n.qty || 0;
    e.tier = Math.max(e.tier, depth);
    if (isBought) {
      e.decision = 'buy';
      e.runs = 0;
      return;                                            // bought ⇒ a leaf; don't expand its inputs
    }
    e.runs += n.runs || 0;
    if (n.decision === 'build' || n.decision === 'buy') e.decision = n.decision;
    (n.inputs || []).forEach(c => {
      (inputsOf[n.type_id] || (inputsOf[n.type_id] = new Set())).add(c.type_id);
      (consumersOf[c.type_id] || (consumersOf[c.type_id] = new Set())).add(n.type_id);
      walk(c, depth + 1);
    });
  });
  roots.forEach(r => walk(r, 0));
  const tiers = {};
  Object.values(byType).forEach(e => (tiers[e.tier] = tiers[e.tier] || []).push(e));
  const maxT = Object.keys(tiers).length ? Math.max(...Object.keys(tiers).map(Number)) : 0;
  return { byType, tiers, maxT, inputsOf, consumersOf };
}

// The stage model both the pipeline and the shopping list render from, so their stage numbering
// can never drift apart.
//
// Only BUILDS define a stage — buying isn't a step in the production chain, it's a prerequisite of
// one. Bought materials therefore don't get columns of their own (which spread the pipeline out
// with a leading "Stage 0" that was nothing but purchases); each one is filed under the stage that
// actually consumes it. A material needed by several stages is filed under the EARLIEST one
// (deepest tier), so the list reads "buy this before you start stage N".
function _indStageModel(tiersData) {
  const { byType, tiers, consumersOf } = tiersData;
  const buildTiers = Object.keys(tiers)
    .map(Number)
    .filter(t => (tiers[t] || []).some(e => e.decision === 'build'))
    .sort((a, b) => b - a);                       // deepest first = left to right
  if (!buildTiers.length) return { cols: [], stageOf: {} };

  const deepest = buildTiers[0];
  const stageOf = {};                             // bought type_id -> stage tier it belongs to
  Object.values(byType).forEach(e => {
    if (e.decision === 'build') return;
    let best = null;
    (consumersOf[e.type_id] || []).forEach(cid => {
      const c = byType[cid];
      if (!c || c.decision !== 'build') return;
      if (best === null || c.tier > best) best = c.tier;   // earliest consuming stage
    });
    // No build consumer (shouldn't happen — bought nodes are leaves): park it at the first stage.
    stageOf[e.type_id] = best === null ? deepest : best;
  });

  const cols = buildTiers.map((t, i) => ({
    t,
    index: i,
    label: t === 0 ? 'Finished' : `Stage ${i + 1}`,
    // "For Finished" reads wrong in the shopping list — name the act, not the column.
    shopLabel: t === 0 ? 'the final build' : `Stage ${i + 1}`,
    builds: (tiers[t] || []).filter(e => e.decision === 'build'),
    buys: Object.values(byType).filter(e => e.decision !== 'build' && stageOf[e.type_id] === t),
  }));
  return { cols, stageOf };
}

function _indShopRowHtml(s) {
  // "Low saving" is a verdict; on its own it asks the user to trust it. Say what building this one
  // would actually have saved (or cost) and let them overrule it per material.
  // Explains why this row is on the list. The ACTION lives in the decision strip above the plan
  // (_indMarginalBar) and only there — this list is collapsed by default and stage-grouped, which
  // is a fine place to look something up and a terrible place to hide a decision.
  let marginal = '';
  if (s.bought_marginal) {
    const sv = s.marginal_saving;
    const why = sv == null ? 'Building this would save too little to be worth a job'
      : sv > 0 ? `Building this batch yourself would save ${fmtIsk(sv)} — under the threshold, so it's bought`
      : `Building this batch yourself would cost ${fmtIsk(-sv)} MORE than buying it`;
    marginal = ` <span class="ind-marginal-badge" title="${_esc(why)}">low saving</span>`
      + (sv != null ? ` <span class="ind-shop-note">${sv > 0 ? `saves ${fmtIsk(sv)} if built`
                                                             : `${fmtIsk(-sv)} dearer to build`}</span>` : '');
  }
  // A blacklisted material is on the list because of a standing rule, not because building lost on
  // cost — without saying so the plan just looks like it got the make-or-buy call wrong.
  const never = s.blacklisted
    ? ` <button class="ind-never-badge" onclick="indBlacklist(${s.type_id}, false)"`
      + ` title="On your always-buy list. Click to let the planner decide again.">always buy</button>` : '';
  // …and the same for the reaction policy, one rung coarser: bought because this account doesn't
  // run that kind of reaction, not because the make-or-buy math came out that way.
  const rxp = s.reaction_policy
    ? ` <span class="ind-never-badge ind-rxp-badge" title="Bought because your builds don't run this`
      + ` kind of reaction. Change that in the strip above the plan.">not reacted</span>` : '';
  return `<tr><td>${_esc(s.name)}`
    + `${s.bought_for_speed ? ' <span class="ind-speed-badge" title="Bought instead of built to save time">for speed</span>' : ''}`
    + `${never}${rxp}${marginal}</td>`
    + `<td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`;
}

// ── Always-buy blacklist ────────────────────────────────────────────────────────────────────
// The account's standing "never build this" list. It changes what every plan does, so like the
// forced-build overrides it is shown next to the shopping list it produces rather than buried in a
// settings panel — a rule you can't see is one you can't remember setting.
let _indBlacklist = [];

async function indLoadBlacklist() {
  if (!_featureActive('industry_blacklist')) return;
  try { _indBlacklist = ((await api('/api/industry/blacklist')) || {}).items || []; }
  catch (e) { _indBlacklist = []; }
}

async function indBlacklist(typeId, add) {
  try {
    _indBlacklist = ((await apiSend('POST', '/api/industry/blacklist',
                                    { type_id: typeId, add: !!add })) || {}).items || [];
  } catch (e) { toastError(e, 'Could not save'); return; }
  _indSweep = null; _indSweepFailed = null;      // the make-or-buy mix moved, so cost and time did
  return _indKeepScroll(() => _indReplanCurrent());
}

function _indBlacklistChipsHtml() {
  if (!_featureActive('industry_blacklist') || !_indBlacklist.length) return '';
  return `<div class="ind-forced-bar ind-never-bar"><span class="ind-forced-lbl">Always bought:</span>`
    + _indBlacklist.map(b =>
        `<button class="ind-forced-chip" onclick="indBlacklist(${b.type_id}, false)" title="Let the planner decide again">`
        + `${_esc(b.name)} <span class="ind-forced-x">✕</span></button>`).join('')
    + `<span class="ind-src-meta">An order set to build one of these anyway still builds it.</span></div>`;
}

// ── Which reactions this account's builds run ───────────────────────────────────────────────
// A standing way of operating, like the always-buy list, one rung coarser: a builder who doesn't
// run reactions shouldn't have to blacklist every output by hand.
//
// It belongs with the make-or-buy CONTROLS (beside the "worth building instead?" strip), never in
// the notice stack — that block was trimmed to what a builder acts on, and a decision surface is
// not a notice. Hence one row: the switch, and the per-family detail folded BEHIND it, because
// "we don't react" is the common case and "…except biochemicals" is the rare one.
//
// The labels come from the server registry (`categories`), never from here.
let _indRxPolicy = null;
let _indRxCatsOpen = false;

async function indLoadReactionPolicy() {
  if (!_featureActive('industry_reaction_policy')) { _indRxPolicy = null; return; }
  try { _indRxPolicy = await api('/api/industry/reaction-policy'); }
  catch (e) { _indRxPolicy = null; }
}

async function indSetReactionPolicy(body) {
  try { _indRxPolicy = await apiSend('POST', '/api/industry/reaction-policy', body); }
  catch (e) { toastError(e, 'Could not save'); return; }
  _indSweep = null; _indSweepFailed = null;      // the make-or-buy mix moved, so cost and time did
  return _indKeepScroll(() => _indReplanCurrent());
}

function indToggleReactionCats() {
  _indRxCatsOpen = !_indRxCatsOpen;
  return _indKeepScroll(() => _indReplanCurrent());
}

function indSetReactionCat(key, buy) {
  const cur = new Set(((_indRxPolicy || {}).policy || {}).buy_categories || []);
  if (buy) cur.add(key); else cur.delete(key);
  return indSetReactionPolicy({ buy_categories: [...cur] });
}

function _indReactionPolicyBar(d) {
  if (!_featureActive('industry_reaction_policy') || !_indRxPolicy) return '';
  // With the consolidated surface on, this strip STOPS being a control and becomes a statement of
  // what is in force with a way through to the thing that set it. Two places to change one rule is
  // how the sprawl started; the summary keeps the manifesto's "report what the shortcut cost"
  // without the disguised-control problem that hid this setting in the first place.
  if (_indRulesActive()) return _indRxSummaryBar(d);
  const pol = _indRxPolicy.policy || {};
  const cats = _indRxPolicy.categories || [];
  const runs = pol.build_reactions !== false;
  const bought = new Set(pol.buy_categories || []);

  // What the convenience cost — the same rule the low-saving strip follows: report it, don't take
  // it quietly. Signed as "what BUILDING these saves", so one figure reads correctly both ways.
  const rp = (d && d.reaction_policy) || null;
  let delta = '';
  if (rp && rp.isk) {
    const n = (rp.items || []).length;
    const what = `${n} reaction output${n === 1 ? '' : 's'}`;
    delta = rp.overridden
      ? `<span class="ind-rxp-delta ind-rxp-good" title="This build makes its own reactions, against your standing rule.">`
        + `reacting ${what} here saves ${fmtIsk(rp.isk)} on this build</span>`
      : rp.isk > 0
        ? `<span class="ind-rxp-delta" title="What buying these instead of reacting them adds to THIS build's cost — the floor under any price you quote off it.">`
          + `buying ${what} in adds ${fmtIsk(rp.isk)} to this build</span>`
        : `<span class="ind-rxp-delta ind-rxp-good" title="Buying these is cheaper than reacting them for this build.">`
          + `buying ${what} in saves ${fmtIsk(-rp.isk)} on this build</span>`;
  }

  // The state in one phrase. Only ever about THIS build — the Reactions tab is a separate feature
  // with its own slot planning, and turning reactions off here says nothing about it.
  const some = cats.filter(c => bought.has(c.key));
  const state = !runs ? 'bought in, not made here'
    : some.length ? `${some.map(c => c.label.toLowerCase()).join(', ')} bought in`
    : 'made here';

  const detail = (runs && _indRxCatsOpen)
    ? `<div class="ind-rxp-cats">` + cats.map(c =>
        `<label class="ind-rxp-cat" title="${_esc(c.description || '')}">`
        + `<input type="checkbox" ${bought.has(c.key) ? 'checked' : ''}`
        + ` onchange="indSetReactionCat('${_esc(c.key)}', this.checked)">`
        + ` buy ${_esc(c.label)}</label>`).join('')
      + `<span class="ind-src-meta">An order can still be set to make its own.</span></div>` : '';

  return `<div class="ind-forced-bar ind-rxp-bar">`
    + `<span class="ind-forced-lbl">Reactions for this build:</span>`
    + `<button class="ind-forced-chip${runs ? '' : ' ind-rxp-off'}"`
    + ` onclick="indSetReactionPolicy({ build_reactions: ${!runs} })"`
    + ` title="${runs ? 'This account runs its own reactions. Click if you buy the outputs instead.'
                     : 'Reaction outputs are bought and their sub-steps drop off the plan. Click to react them yourself again.'}">`
    + `${_esc(state)}</button>`
    + (runs ? `<button class="ind-link-btn" onclick="indToggleReactionCats()">`
              + `${_indRxCatsOpen ? 'hide families' : 'by family…'}</button>` : '')
    + delta + `</div>` + detail;
}

// ── The borderline components, and the decision about them ──────────────────────────────────
// The engine buys anything whose saving is too small to be worth a job. That's a judgement about
// the user's time, so it's theirs to overrule — but the evidence for overruling it (what building
// each one would actually save) sat inside the shopping list, which is collapsed by default and
// grouped by stage, and in the queued view carried no button at all. A decision nobody can see is
// not a decision they get to make.
//
// So it lives here instead: one strip, above the plan, listing ONLY the borderline items. Not a
// second copy of the shopping list — that list keeps the "low saving" badge as an explanation of
// why a row is there, and nothing else. One place to decide, one place to look things up.
function _indMarginalBar(d) {
  const rows = (d.shopping_list || [])
    .filter(s => s.bought_marginal && (s.marginal_saving || 0) > 0)
    .sort((a, b) => b.marginal_saving - a.marginal_saving);
  if (!rows.length) return '';
  const total = rows.reduce((a, s) => a + s.marginal_saving, 0);
  // Every borderline component is listed, not a top-six: the slider selects across the whole list,
  // and a control that says "builds 9 of 14" above six visible chips is asking to be misread.
  const show = rows.slice(0, 24);
  const chips = show.map(s =>
    `<button class="ind-marg-chip" id="margchip-${s.type_id}" data-sav="${s.marginal_saving}"`
    + ` onclick="indBuildAnyway(${s.type_id}, '${_esc(s.name).replace(/'/g, "\\'")}')"`
    // Say what it costs and what it buys, and stop there. An earlier version ended "which is your
    // call, not ours", which reads as the tool bracing for blame rather than helping you decide.
    + ` title="Build ${_esc(s.name)} yourself instead of buying it: saves ${fmtIsk(s.marginal_saving)}, costs you one more job">`
    + `${_esc(s.name)} <span class="ind-marg-save">+${fmtIsk(s.marginal_saving)}</span></button>`).join('');
  const more = rows.length > show.length
    ? `<button class="ind-link-btn" onclick="indOpenShoppingList()">+${rows.length - show.length} more in the shopping list</button>` : '';

  // Take them in bulk instead of one at a time. This is NOT a second make-or-buy threshold — the
  // saving-% slider decides what gets suggested here; this decides how many of those suggestions
  // you accept in one go, over the list that slider already produced. A builder who would take any
  // job worth 10m shouldn't have to click seven chips to say so.
  _indMargRows = rows.map(s => ({ type_id: s.type_id, name: s.name, saving: s.marginal_saving }));
  const max = Math.ceil(rows[0].marginal_saving);
  // Where the slider was left last time, so a refresh doesn't silently move it back and change what
  // the strip appears to be offering. Clamped, because the next build's savings are different
  // numbers entirely. Restoring the POSITION applies nothing on its own — you still press the button.
  let start = parseFloat(localStorage.getItem('indMargCut'));
  if (!(start >= 0) || start > max) start = max;
  const bulk = (rows.length > 1 && max > 0)
    ? `<div class="ind-marg-bulk">`
      + `<label class="ind-src-meta">Build everything worth more than`
      + ` <input type="range" id="indMargCut" min="0" max="${max}" step="${Math.max(1, Math.round(max / 200))}"`
      + ` value="${start}" oninput="indMargCutLabel()"></label>`
      + `<span id="indMargCutInfo" class="ind-marg-cutinfo"></span>`
      + `<button class="ind-marg-apply" onclick="indBuildAllAbove()">Build these</button></div>` : '';

  return `<div class="ind-marg-bar"><span class="ind-marg-lbl">Worth building instead?</span>`
    + `<span class="ind-src-meta">${rows.length} component${rows.length > 1 ? 's are' : ' is'} bought`
    + ` because each saves little on its own — ${fmtIsk(total)} in total. Click one to build it,`
    + ` or take several at once below. Building some changes the shared batch, so a different set`
    + ` can be borderline afterwards — that is the plan re-costing itself, not new work appearing.</span>`
    + `<div class="ind-marg-chips">${chips}${more}</div>${bulk}</div>`;
}

// The borderline components currently on screen, so the bulk control can act on them without
// re-deriving the list from a plan that may already have been replaced.
let _indMargRows = [];

function _indMargCut() {
  const el = document.getElementById('indMargCut');
  return el ? parseFloat(el.value) : Infinity;
}

function _indMargAbove() {
  const cut = _indMargCut();
  return _indMargRows.filter(r => r.saving >= cut);
}

// Live feedback while dragging — and the LIST is the feedback, not just a counter. Dragging marks
// exactly which chips the button would take, so what you're about to accept is the thing you're
// looking at. A number saying "builds 3 of 7" over an unchanged row of chips reads as a control
// that isn't connected to anything.
function indMargCutLabel() {
  const info = document.getElementById('indMargCutInfo');
  if (!info) return;
  const cut = _indMargCut();
  const picked = _indMargAbove();
  const gain = picked.reduce((a, r) => a + r.saving, 0);
  info.textContent = picked.length
    ? `${fmtIsk(cut)} — builds ${picked.length} of ${_indMargRows.length}, saving ${fmtIsk(gain)}`
    : `${fmtIsk(cut)} — nothing that high`;
  const btn = document.querySelector('.ind-marg-apply');
  if (btn) btn.disabled = !picked.length;
  document.querySelectorAll('.ind-marg-chip').forEach(el => {
    const sav = parseFloat(el.getAttribute('data-sav'));
    el.classList.toggle('ind-marg-in', isFinite(sav) && sav >= cut);
    el.classList.toggle('ind-marg-out', isFinite(sav) && sav < cut);
  });
  try { localStorage.setItem('indMargCut', String(cut)); } catch (e) {}
}

// One press, then keep going: building these makes their own inputs a bulk demand, which can make
// THOSE worth building too. The server iterates to a fixpoint so the answer is stable — after this
// there is nothing left above the cut-off — rather than leaving the user to chase a list that
// regrows each time they accept its advice.
async function indBuildAllAbove() {
  const picked = _indMargAbove();
  if (!picked.length) return;
  // No queue yet (the preview): nothing to iterate against, so take the single pass.
  if (!_indStatusVisible() || !(_indOrders || []).length) {
    return _indKeepScroll(() => _indForceBuildMany(picked));
  }
  const btn = document.querySelector('.ind-marg-apply');
  if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
  let d;
  try {
    d = await apiSend('POST', '/api/industry/orders/force-above',
                      { ..._indQueueBody(), min_saving: _indMargCut() });
  } catch (e) { toastError(e, 'Could not save'); if (btn) btn.textContent = 'Build these'; return; }
  const n = (d.added || []).length;
  if (n) {
    toast(`Building ${n} more component${n === 1 ? '' : 's'}`
          + (d.rounds > 1 ? ` — ${d.rounds} passes, since building some made others worth building` : ''));
  }
  return _indKeepScroll(() => indRefreshStatus());
}

// Overrule the buy-it shortcut for one component. Where that override is STORED depends on whether
// there's a queue yet: a queued build keeps it on the order (the queue unions force_build_ids
// across orders, so one order carries it for the whole batch — and the ⚒ tag on that order chip is
// how you take it back), while the preview keeps it in the session map until the build is queued.
async function indBuildAnyway(typeId, name) {
  return _indKeepScroll(() => _indForceBuildMany([{ type_id: typeId, name }]));
}

// One or many, one round trip and ONE re-plan either way. Overruling seven components must not mean
// seven requests each re-planning the whole queue against a batch the next one is about to change.
async function _indForceBuildMany(items) {
  if (!items.length) return;
  if (!_indStatusVisible() || !(_indOrders || []).length) {
    items.forEach(i => _indForcedTypes.set(i.type_id, i.name || String(i.type_id)));
    _indSweep = null; _indSweepFailed = null;
    return indRunPlan();
  }
  const order = _indOrders[0];
  const ids = [...new Set([...(order.force_build_ids || []), ...items.map(i => i.type_id)])];
  try { await apiSend('PATCH', `/api/industry/orders/${order.id}`, { force_build_ids: ids }); }
  catch (e) { toastError(e, 'Could not save'); return; }
  // Building them changes the batch every other decision was weighed against, so the plan really
  // does have to re-run — but see _indKeepScroll for why you don't get thrown to the top.
  return indRefreshStatus();
}

// Re-planning replaces the whole card, and while it's being fetched the page is a short spinner —
// so the browser leaves you at the top of a document that just lost most of its height. Anyone
// overruling one borderline component usually wants to overrule the next one too, and re-finding
// the strip each time is what makes that tedious. Hold the scroll position across the repaint.
async function _indKeepScroll(run) {
  const y = window.scrollY;
  try { await run(); } finally { window.scrollTo(0, y); }
}

// The components the user overruled, with a way back — once forced they vanish from the shopping
// list (they're built now), so without this the override would be invisible and unrepeatable.
function _indForcedChipsHtml() {
  if (!_indForcedTypes.size) return '';
  return `<div class="ind-forced-bar"><span class="ind-forced-lbl">Building anyway:</span>`
    + [..._indForcedTypes.entries()].map(([tid, name]) =>
        `<button class="ind-forced-chip" onclick="indUnforceBuildType(${tid})" title="Go back to buying it">`
        + `${_esc(name)} <span class="ind-forced-x">✕</span></button>`).join('')
    + `</div>`;
}

// The shopping list grouped by the stage that needs each material — buy just what the next step
// needs, or everything at once. Grouping/labels match the pipeline's "Buy N materials" cards
// exactly, since both render from the same _indStageModel().
function _indShoppingSections(d, model, allowForce) {
  const list = d.shopping_list || [];
  if (!list.length) return '<p class="pp-sub">Nothing to buy — built entirely from stock/recipes.</p>';
  const byId = {};
  list.forEach(s => { byId[s.type_id] = s; });
  _indShopStageData = {};
  let sections = '';
  let listed = 0;
  model.cols.forEach(col => {
    const rows = col.buys.map(e => byId[e.type_id]).filter(Boolean);
    if (!rows.length) return;
    listed += rows.length;
    _indShopStageData[col.t] = rows.map(r => ({ name: r.name, qty: r.qty }));
    const stageCost = rows.reduce((a, s) => a + (s.line_cost || 0), 0);
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-${col.t}">`
      + `<div class="ind-shop-stage-hd"><span>For ${_esc(col.shopLabel || col.label)} — ${rows.length} item${rows.length > 1 ? 's' : ''} · ${fmtIsk(stageCost)}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy(${col.t})">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows.map(_indShopRowHtml).join('')}</tbody></table></div>`;
  });
  // Anything the stage model didn't place (defensive — keeps the list complete no matter what).
  const placed = new Set(model.cols.flatMap(c => c.buys.map(e => e.type_id)));
  const rest = list.filter(s => !placed.has(s.type_id));
  if (rest.length) {
    _indShopStageData['other'] = rest.map(r => ({ name: r.name, qty: r.qty }));
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-other">`
      + `<div class="ind-shop-stage-hd"><span title="Not linked to a build stage — please report this">Not tied to a stage — ${rest.length} item${rest.length > 1 ? 's' : ''}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy('other')">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rest.map(_indShopRowHtml).join('')}</tbody></table></div>`;
  }
  const totalCost = list.reduce((a, s) => a + (s.line_cost || 0), 0);
  return `<div class="ind-shop-bar"><button class="ind-copy-btn" onclick="indCopyMultibuy()">Copy everything</button>`
    + `<span class="ind-shop-tot">${list.length} items · ${fmtIsk(totalCost)}</span></div>`
    + (allowForce ? _indForcedChipsHtml() : '') + _indBlacklistChipsHtml() + sections;
}

// Copy a shopping list (or one stage of it, if `stage` is given) in EVE's Multibuy paste format
// ("Item Name<tab>qty" per line) so it can be pasted straight into the in-game Multibuy window.
function indCopyMultibuy(stage) {
  const list = (stage !== undefined && _indShopStageData[stage]) ? _indShopStageData[stage] : ((_indLastPlan && _indLastPlan.shopping_list) || []);
  if (!list.length) return;
  _indCopyText(list.map(s => `${s.name}\t${Math.ceil(s.qty)}`).join('\n'));
}

// Jump from a pipeline "Buy N materials" card down to that exact stage in the shopping list below.
// Open the (collapsed) shopping list and scroll to it — the place to read the rest of the
// borderline rows, since the strip above only carries as many as you can decide from at a glance.
function indOpenShoppingList() {
  const bar = document.querySelector('.ind-shop-bar');
  const details = bar && bar.closest('details');
  if (details) details.open = true;
  (details || bar || {}).scrollIntoView && (details || bar).scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function _indJumpToStage(t) {
  const el = document.getElementById('ind-shop-stage-' + t);
  if (!el) return;
  const details = el.closest('details');
  if (details) details.open = true;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('ind-shop-flash');
  setTimeout(() => el.classList.remove('ind-shop-flash'), 1200);
}

// The blueprint chip for a step, and the one place that decides what a blueprint noun may sit next
// to. Three different numbers meet here and none of them is interchangeable:
//   * RUNS NEEDED — how many units this build wants. It belongs to the build, never to the print.
//   * RUNS PER COPY — what the copy you hold actually carries. A capital BPC is 1 run, always.
//   * COPIES TO BUY — how many contracts cover the shortfall.
// Rendering the first of those beside the word "BPC" is how a Phoenix builder was told the plan had
// found him a 2-run capital copy. It had not: he had ordered two hulls, off a 1-run copy, and the
// scheduler had correctly split it into two 1-run jobs. So the chip states the copy's OWN runs,
// and when the batch needs more than the copy carries it says so on the chip rather than leaving
// the run count next door to be misread as the copy's.
function _indOwnedBpChip(owned, runsNeeded) {
  if (!owned) return '';
  const kind = String(owned.kind || '').toUpperCase();
  const me = owned.me != null ? ` ME${owned.me}` : '';
  if (kind !== 'BPC') {
    return ` <span class="ind-owned" title="You own this ${kind} — an original, so it never runs out">${kind}${me}</span>`;
  }
  // `runs` is the coverage of EVERY copy you hold for this product, summed — so with more than one
  // the chip has to say so, or "BPC ME10 · 60 runs" reads as one enormous ME10 copy when it is
  // really three copies of mixed research, best first.
  const have = Math.max(0, Math.round(owned.runs || 0));
  const n = Math.max(1, Math.round(owned.copy_count || 1));
  const need = Math.max(0, Math.round(runsNeeded || 0));
  const short = need > have ? need - have : 0;
  const runTxt = `${have} run${have === 1 ? '' : 's'}`;
  const copyTxt = n > 1 ? ` across ${n} copies` : '';
  const tip = `You own ${n > 1 ? `${n} copies of this BPC carrying ${runTxt} in total`
    : `this BPC and it carries ${runTxt}`}`
    + (n > 1 ? `; the best-researched are used first (ME${owned.me} is the best of them)` : '')
    + (need ? `. This build needs ${need} run${need === 1 ? '' : 's'}` : '')
    + (short ? `, so ${short} more must come from further copies` : '')
    + '.';
  return ` <span class="ind-owned${short ? ' ind-owned-short' : ''}" title="${tip}">`
    + `BPC${me} · ${runTxt}${copyTxt}${short ? ` · ${short} short` : ''}</span>`;
}

// Compact tree row label (shared by leaves and collapsible nodes).
function _indTreeLabel(n) {
  const badge = n.decision === 'build'
    ? `<span class="ind-badge ind-build">build${n.activity === 'reaction' ? ' rx' : ''}${n.runs ? ' ×' + n.runs : ''}</span>`
    : n.decision === 'buy' ? '<span class="ind-badge ind-buy">buy</span>'
    : '<span class="ind-badge ind-unres">no price</span>';
  const cost = n.unit_cost != null ? `<span class="ind-tree-cost">${fmtIsk((n.unit_cost || 0) * (n.qty || 0))}</span>` : '';
  const owned = _indOwnedBpChip(n.owned, n.runs);
  return `<span class="ind-tree-name">${_esc(n.name)}</span> <span class="ind-tree-qty">×${Math.round(n.qty).toLocaleString()}</span> ${badge}${owned}${cost}`;
}

// Collapsible tree via native nested <details>: a node WITH built children folds (open only near
// the top so a deep build isn't a wall of text); leaves render as plain rows. Indent comes from
// the nesting, not per-row padding.
function _indTreeNode(n, depth) {
  const kids = (n.inputs || []).filter(c => c.decision === 'build' || (c.inputs && c.inputs.length));
  const leaves = (n.inputs || []).filter(c => !(c.decision === 'build' || (c.inputs && c.inputs.length)));
  if (!kids.length && !leaves.length) return `<div class="ind-tree-leaf">${_indTreeLabel(n)}</div>`;
  const open = depth < 1 ? ' open' : '';
  const childHtml = kids.map(c => _indTreeNode(c, depth + 1)).join('')
    + leaves.map(c => `<div class="ind-tree-leaf">${_indTreeLabel(c)}</div>`).join('');
  return `<details class="ind-tree-node"${open}><summary class="ind-tree-sum">${_indTreeLabel(n)}</summary>`
    + `<div class="ind-tree-kids">${childHtml}</div></details>`;
}

// ME/TE actually used per build step, keyed by type_id — filled from the plan's requirements so a
// job chip can show what it was costed at. An assumed efficiency that isn't visible is an invisible
// input to every number on the page.
let _indReqMeTe = {};
// The user's own ME/TE per product. NOT cleared when the product changes: it's a fact about a
// blueprint ("the copy I use is ME 10"), not about one build.
let _indMeTe = {};

const _IND_ME_SRC = {
  owned: 'from your own blueprint',
  // Deliberately worded as YOUR statement, not as a reading — a typed number and an ESI-read one
  // are different kinds of evidence and the chip is where that difference is visible.
  declared: 'the blueprint you declared by hand',
  contract: 'assumed from the contract copy this plan buys',
  override: 'you set this',
  default: 'un-researched — no blueprint of yours and none listed',
};

function _indMeTeChip(typeId) {
  const r = _indReqMeTe[typeId];
  if (!r || r.me_source === 'reaction') return '';     // reactions have no blueprint ME/TE
  const src = _IND_ME_SRC[r.me_source] || '';
  return `<button class="ind-mete ind-mete-${r.me_source}" id="mete-${typeId}"`
    + ` title="ME ${r.me}% materials / TE ${r.te}% time — ${_esc(src)}. Click to set what you'll really use."`
    + ` onclick="indEditMeTe(${typeId})">ME ${r.me} · TE ${r.te}</button>`;
}

// Two per-job controls that both amount to "the plan is wrong about this one, and I'd know":
// I'm further along than you think, and I never build this — see indCycleDone / indBlacklist below.
// Two corrections you can make to a job from the step list. Both are real buttons with words on
// them: the first cut used bare dimmed glyphs, which on a chip that already carries a name, a run
// count, a duration and an ME/TE tag were effectively invisible.
function _indJobActions(x) {
  let html = '';
  if (_featureActive('industry_manual_done')) {
    html += _indDoneBtn(_indProgTypeMap()[x.type_id], x.type_id, 'ind-job-act');
  }
  if (_featureActive('industry_blacklist')) {
    html += `<button class="ind-job-act ind-job-never" onclick="indBlacklist(${x.type_id}, true)"`
      + ` title="Always buy ${_esc(x.name)} instead of building it — on every build, until you undo it">always buy</button>`;
  }
  return html;
}

function _indJobChips(g) {
  return g.map(x => {
    const blocked = x.blocked || [];
    // Name the assignee, and say plainly when that assignee can't actually install it — an
    // instruction you can't follow is worse than no instruction, so it's marked inline rather
    // than left to the skills panel further up the page.
    const who = (x.who && x.who.length)
      ? `<span class="ind-wave-who${blocked.length ? ' ind-wave-who-blocked' : ''}" title="${
          blocked.length
            ? 'Missing skills: ' + _esc(blocked.join(', ')) + ' cannot install this job — see the missing-skills panel above'
            : 'Install this on ' + _esc(x.who.join(', '))
        }">on ${_esc(x.who.join(', '))}${blocked.length ? ' ⚠' : ''}</span>`
      : '';
    const t = _indProgTypeMap()[x.type_id];
    const isDone = t && t.required_runs > 0 && t.done_runs >= t.required_runs;
    return `<span class="ind-wave-job${isDone ? ' ind-wave-job-done' : ''}">`
      + `${_esc(x.name)} ×${x.runs}${x.activity === 'reaction' ? ' rx' : ''} · ${_fmtHours(x.dur)}`
      + who + _indMeTeChip(x.type_id) + _indJobActions(x) + `</span>`;
  }).join('');
}

// Progress is three-valued, so the control is too. Where a step stands right now, as far as the
// user is concerned: what ESI measured and what they said by hand, already combined by the server.
// A part-done step counts as running — some of it has happened, none of it is finished.
function _indDoneState(t) {
  if (!t || !t.required_runs) return 'none';
  if (t.done_runs >= t.required_runs) return 'done';
  if (t.running_runs > 0 || t.done_runs > 0 || t.manual_state === 'running') return 'running';
  return 'none';
}

// One click advances the step: not started → running → done → not started. The wrap-around is the
// point — a misclick has to be undoable with more clicks, never a dead end, and "done" was already
// the state you could take back.
function indCycleDone(typeId) {
  const st = _indDoneState(_indProgTypeMap()[typeId]);
  if (st === 'none') return _indPostDone(typeId, null, 'running');
  if (st === 'running') return _indPostDone(typeId, null, 'done');
  return _indPostDone(typeId, 0);                 // done → back to not started
}

// The step's button, wherever it appears — the pipeline card has its own click target, everything
// else uses this. **The label is the NEXT state, not the current one**: it reads "run" on a step
// that hasn't started and "done" once it is running, so the button says what pressing it does. Only
// the finished state names itself, because there the press is an undo.
function _indDoneBtn(t, typeId, cls) {
  const st = _indDoneState(t);
  const byHand = t && t.manual_state;
  if (st === 'done') {
    return `<button class="${cls} ind-job-done-on" onclick="indCycleDone(${typeId})" title="${
      byHand === 'done' ? 'You marked this done — click to start over'
                        : 'Already done. Click if that is wrong.'}">✓ done</button>`;
  }
  if (st === 'running') {
    return `<button class="${cls} ind-job-run-on" onclick="indCycleDone(${typeId})"`
      + ` title="This one is running${byHand === 'running' ? ' (you said so)' : ''} — click when it has finished">done</button>`;
  }
  return `<button class="${cls}" onclick="indCycleDone(${typeId})"`
    + ` title="Say this step is running — for work we can't see, like a job installed on a character that isn't connected">run</button>`;
}

// Repaint FIRST, save second. Ticking a step changes nothing the plan computes, so the browser can
// work out the new numbers itself — `max(observed, manual)`, the same rule the server applies — and
// redraw from the plan already on screen. The write still goes out, and its authoritative answer
// replaces the local guess when it lands, but nobody waits for a capital build to be re-planned
// twice over just to watch a card turn green.
async function _indPostDone(typeId, runs, state) {
  state = state || 'done';
  // Does this mark FINISH the step? "Do this now" and the pipeline's stage gating are computed
  // server-side and arrive on the plan (`d.install`) — the local fast path can recompute progress
  // because that is just max(observed, manual), but it cannot work out what became installable as a
  // result, and recomputing the checklist here is exactly what item 16 removed so the plan and the
  // checklist could never disagree. So a completing mark gives up the fast path and re-plans: that
  // is the one case where the whole point of the click is that the NEXT stage should appear.
  const completes = _indMarkCompletesStep(typeId, runs, state);
  const painted = !completes && _indApplyDoneLocally(typeId, runs, state);
  if (painted) _indPaintStatus(_indLastPlan, { local: true });
  try {
    const fresh = await apiSend('POST', '/api/industry/progress/done', { type_id: typeId, runs, state });
    _indProgress = (fresh && !fresh.empty) ? fresh : _indProgress;
    // Nothing was painted locally (no plan in hand, preview mode, or a mark that completes a step
    // and therefore needs the server's view of what is ready next) — fall back to the full path.
    if (painted) _indPaintStatus(_indLastPlan, { local: true }); else indRefreshStatus();
  } catch (e) {
    toastError(e, 'Could not save');
    indRefreshStatus();      // the local guess is now a lie — go and get the truth
  }
}

// Apply a mark to the progress we already hold, exactly as the server would. Returns false when
// there's nothing to apply to, in which case the caller falls back to a full refresh.
function _indApplyDoneLocally(typeId, runs, state) {
  const p = _indProgress;
  if (!p || !p.types || !_indLastPlan) return false;
  // Preview mode's numbers are fabricated, so editing them would be editing fiction — leave that
  // path exactly as it was and let the full refresh re-fetch the simulation.
  if (_indSim !== null) return false;
  const t = p.types.find(x => x.type_id === typeId);
  if (!t || !t.required_runs) return false;
  const need = t.required_runs;
  // `runs === null` is "all of it"; a number is that many; 0 clears the mark. The `observed_*`
  // counts are what the server measured with no mark at all, which is what makes this computable
  // without asking it — and what enforces the same precedence here: a mark is folded in with
  // max(), so it can move the step forward and never back over a measured signal.
  const cleared = runs === 0;
  const manDone = (!cleared && state !== 'running')
    ? (runs === null ? need : Math.max(0, Math.min(need, runs))) : 0;
  const manRun = (!cleared && state === 'running') ? need : 0;
  const observed = t.observed_runs != null ? t.observed_runs : 0;
  const obsRun = t.observed_running_runs != null ? t.observed_running_runs : (t.running_runs || 0);
  t.manual_runs = manDone;
  t.manual_state = cleared ? '' : state;
  t.done_runs = Math.min(need, Math.max(observed, manDone));
  t.running_runs = Math.min(Math.max(obsRun, manRun), Math.max(0, need - t.done_runs));
  t.waiting_runs = Math.max(0, need - t.done_runs - t.running_runs);
  t.pct = need ? Math.round(1000 * t.done_runs / need) / 10 : 0;
  // Headline counters are sums over the types, so they follow from the same edit.
  const sum = k => p.types.reduce((a, x) => a + (x[k] || 0), 0);
  p.totals = { required: sum('required_runs'), done: sum('done_runs'),
               running: sum('running_runs'), waiting: sum('waiting_runs') };
  // Same weighting the server uses (_weighted_pct): by job time, falling back to runs when no
  // schedule times are known. Recomputing this the old run-counted way would flash a different
  // number for the second it takes the real response to land.
  const hTot = p.types.reduce((a, x) => a + (x.job_hours || 0), 0);
  const hDone = p.types.reduce((a, x) => a + (x.required_runs ? (x.done_runs / x.required_runs) * (x.job_hours || 0) : 0), 0);
  p.hours = { total: Math.round(hTot * 100) / 100, done: Math.round(hDone * 100) / 100 };
  p.runs_pct = p.totals.required ? Math.round(1000 * p.totals.done / p.totals.required) / 10 : 0;
  p.pct = hTot > 0 ? Math.round(1000 * hDone / hTot) / 10 : p.runs_pct;
  // Order chips are units, not runs, and only the order FOR this product can move. Anything subtler
  // (a product already covered by stock, say) is corrected a moment later by the real response.
  (p.orders || []).forEach(o => {
    if (o.product_type_id !== typeId) return;
    o.done_units = Math.min(o.quantity, t.done_runs * (t.output_qty || 1));
    o.running_units = Math.min(t.running_runs * (t.output_qty || 1),
                               Math.max(0, o.quantity - o.done_units));
    o.pct = o.quantity ? Math.round(1000 * o.done_units / o.quantity) / 10 : 0;
    o.status = o.done_units >= o.quantity ? 'complete'
      : (o.running_units > 0 || o.done_units > 0) ? 'building' : 'waiting';
  });
  return true;
}

// Half a step is a real state — you install five of the twelve runs, they finish, the rest are
// waiting on a slot. But it is the RARE state, so it costs an extra click and the common
// "this one's finished" stays a single click on the card. The way in is the card's own run count,
// because "12 runs" is already the number you'd be correcting.
function indEditDoneRuns(ev, typeId, required, current) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }   // don't also toggle the whole card
  const el = document.getElementById('pruns-' + typeId);
  if (!el) return;
  const wrap = document.createElement('span');
  wrap.className = 'ind-pipe-partedit';
  wrap.onclick = e => e.stopPropagation();
  wrap.innerHTML = `<input type="number" min="0" max="${required}" value="${current || 0}" id="pdone-${typeId}">`
    + `<span class="ind-pipe-partof">of ${required}</span>`
    + `<button class="ind-srcq-btn" onclick="indApplyDoneRuns(${typeId}, ${required})">set</button>`;
  el.replaceWith(wrap);
  const inp = document.getElementById('pdone-' + typeId);
  if (inp) { inp.focus(); inp.select();
    inp.onkeydown = e => { if (e.key === 'Enter') indApplyDoneRuns(typeId, required); }; }
}

function indApplyDoneRuns(typeId, required) {
  const inp = document.getElementById('pdone-' + typeId);
  const n = Math.max(0, Math.min(required, parseInt((inp || {}).value, 10) || 0));
  // All of them means ALL of them — store the sentinel, not the number, so the mark survives the
  // plan's run count changing later.
  _indPostDone(typeId, n >= required ? null : n);
}

// Edit in place on the chip: two numbers, and the plan re-runs against them. This is the "editing
// the plan" half — the same override can be set before a plan exists via the same map.
function indEditMeTe(typeId) {
  const el = document.getElementById('mete-' + typeId);
  if (!el) return;
  const cur = _indReqMeTe[typeId] || { me: 0, te: 0 };
  const wrap = document.createElement('span');
  wrap.className = 'ind-mete-edit';
  wrap.innerHTML = `ME <input type="number" min="0" max="10" value="${cur.me}" id="mete-me-${typeId}">`
    + ` TE <input type="number" min="0" max="20" value="${cur.te}" id="mete-te-${typeId}">`
    + ` <button class="ind-mete-ok" onclick="indApplyMeTe(${typeId})">Apply</button>`
    + (_indMeTe[typeId] ? ` <button class="ind-mete-clr" onclick="indClearMeTe(${typeId})" title="Back to what the plan works out for itself">reset</button>` : '');
  el.replaceWith(wrap);
}

function indApplyMeTe(typeId) {
  const me = parseFloat((document.getElementById('mete-me-' + typeId) || {}).value);
  const te = parseFloat((document.getElementById('mete-te-' + typeId) || {}).value);
  if (isNaN(me) || isNaN(te)) return;
  _indMeTe[typeId] = [Math.max(0, Math.min(10, me)), Math.max(0, Math.min(20, te))];
  _indSweep = null; _indSweepFailed = null;      // efficiency moves both cost and time
  _indReplanCurrent();
}

function indClearMeTe(typeId) {
  delete _indMeTe[typeId];
  _indSweep = null; _indSweepFailed = null;
  _indReplanCurrent();
}

// Whichever view is on screen. The overrides feed both the preview and the queue plan, so the one
// showing has to be the one that re-runs.
function _indReplanCurrent() {
  const out = document.getElementById('indResult');
  const jobs = [];
  if (_indPicked && out && out.innerHTML.trim()) jobs.push(indRunPlan());
  if (_indStatusVisible()) jobs.push(indRefreshStatus());
  return Promise.all(jobs);      // awaitable, so a caller can hold the scroll until it's repainted
}

function _indStepItems(g, open) {
  return `<details class="ind-step-items"${open ? ' open' : ''}><summary>show items</summary>`
    + `<div class="ind-wave-jobs">${_indJobChips(g)}</div></details>`;
}

function _indStepsHtml(d, model) {
  const waves = (d.schedule && d.schedule.waves) || [];
  if (!waves.length) return '';
  const shop = d.shopping_list || [];

  // Collapse the schedule onto STAGES. A wave is a scheduler artifact — jobs unlocking as slots
  // free — so a 20-wave plan used to render 20 "steps", which is noise: you don't do 20 different
  // things, you work through a handful of stages and refill slots as they open. One step per stage,
  // with the batch count folded into it as a note.
  const stageOfType = {};
  (model.cols || []).forEach((c, i) => c.builds.forEach(b => { stageOfType[b.type_id] = i; }));
  const byStage = {};
  waves.forEach(w => {
    (w.tasks || []).forEach(t => {
      const key = stageOfType[t.type_id] === undefined ? 'x' : stageOfType[t.type_id];
      const s = byStage[key] || (byStage[key] = { key, jobs: 0, runs: 0, start: Infinity, end: 0, longest: 0, batches: new Set(), by: {} });
      s.jobs += 1;
      s.runs += t.runs;
      s.start = Math.min(s.start, w.start_hours);
      // A step is an OFFSET into one wall clock, not a length — and the header used to show the
      // offset alone. On a real 2× Phoenix queue that read "Finished — 2 jobs ≈ +14h 34m" above
      // "Done — built in ≈ 13d 12h": the 12d 21h the Phoenix job itself runs for appeared nowhere
      // except inside the collapsed "show items" fold. Carry the longest job and the moment the
      // stage has fully landed, so the steps account for the total instead of contradicting it.
      s.end = Math.max(s.end, w.start_hours + t.duration_hours);
      s.longest = Math.max(s.longest, t.duration_hours);
      s.batches.add(w.start_hours);
      const g = s.by[t.type_id] || (s.by[t.type_id] = { name: t.name || _indName(t.type_id), runs: 0, activity: t.activity, dur: 0, who: [], type_id: t.type_id });
      g.runs += t.runs;
      g.dur = Math.max(g.dur, t.duration_hours);
      // Who installs it. A type's runs can be split across several toons' slots, so collect them
      // all — "who do I log in as" is the question every stage after the first left unanswered.
      if (t.character_name && !g.who.includes(t.character_name)) g.who.push(t.character_name);
      // `skill_ok === false` means the scheduler had to fall back to someone who provably can't
      // install this job (nobody who can had a free slot). Strictly false — undefined means the
      // check didn't run, and "not checked" must never render as a problem.
      if (t.character_name && t.skill_ok === false) {
        (g.blocked || (g.blocked = [])).includes(t.character_name) || g.blocked.push(t.character_name);
      }
    });
  });
  const stages = Object.values(byStage).sort((a, b) => a.start - b.start);
  if (!stages.length) return '';

  let n = 0;
  let html = '<div class="ind-steps"><div class="ind-steps-title">Step by step</div>';
  if (shop.length) {
    n++;
    html += `<div class="ind-step"><div class="ind-step-hd"><span class="ind-step-num">${n}</span>Buy your materials</div>`
      + `<div class="ind-step-body">${shop.length} item${shop.length > 1 ? 's' : ''} · ${fmtIsk(d.metrics.materials_cost)} — full list below.</div></div>`;
  }
  stages.forEach((s, i) => {
    n++;
    const col = model.cols[s.key];
    const title = col ? col.label : 'Remaining jobs';
    const items = Object.values(s.by).sort((a, b) => b.dur - a.dur);
    const jobs = `${s.jobs} job${s.jobs > 1 ? 's' : ''} · ${s.runs.toLocaleString()} run${s.runs > 1 ? 's' : ''}`;
    const first = i === 0 && s.start <= 0.01;
    const when = first
      ? '<span class="ind-step-tag">do this now</span>'
      : `<span class="ind-step-when">starts +${_fmtHours(s.start)}</span>`;
    // The two numbers that make the step add up: how long its longest job runs for, and when the
    // whole step has landed. Without them a reader can only add the start offsets, which on any
    // build with one dominant job lands nowhere near the total.
    const runs = `<span class="ind-step-when" title="The longest job in this step runs ${_fmtHours(s.longest)}. Everything in this step has landed ${_fmtHours(s.end)} after you start the build.">runs ${_fmtHours(s.longest)} · all landed by +${_fmtHours(s.end)}</span>`;
    // Several batches = the same stage restarted as slots freed, not extra decisions to make.
    const batches = s.batches.size > 1
      ? `<span class="ind-step-note">in ${s.batches.size} batches as slots free</span>` : '';
    // A stage finishes as a unit — nothing in the next one can start until all of it has landed —
    // so it is worth being able to say so in one click instead of stepping through every job.
    // Offered only on a stage that has steps we can mark, and only where the mark can do anything.
    const stageMark = (_featureActive('industry_manual_done') && s.key !== 'x'
                       && _indStageTypeIds(s.key).length)
      ? `<button class="ind-link-btn ind-step-done-all" onclick="indMarkStageDone(${JSON.stringify(s.key)})"`
        + ` title="Mark every job in this step finished, and move the checklist on to the next one">mark stage done</button>`
      : '';
    html += `<div class="ind-step${first ? ' ind-step-now' : ' ind-step-later'}">`
      + `<div class="ind-step-hd"><span class="ind-step-num">${n}</span>${_esc(title)} — ${jobs} ${when}${runs}${batches}${stageMark}</div>`
      + _indStepItems(items, first) + `</div>`;
  });
  // Say what the total MEASURES, and name the step that drives it. Two numbers on one screen that
  // disagree with no explanation is the defect this line exists to close: the steps are start
  // times on one wall clock, so they were never meant to be added, and the build's length is
  // whichever step lands last — usually the final assembly job, running for days on its own.
  const driver = stages.reduce((a, b) => (b.end > a.end ? b : a), stages[0]);
  const driverCol = model.cols[driver.key];
  const driverName = driverCol ? driverCol.label : 'the last step';
  html += `<div class="ind-step ind-step-done"><div class="ind-step-hd"><span class="ind-step-num">✓</span>Done — ${_esc(d.target ? d.target.name : 'product')} built in ≈ ${_fmtHours(d.metrics.makespan_hours)}</div>`
    + `<div class="ind-step-body">Wall-clock from installing the first job, with everything running in parallel — the times above are points on that same clock, not lengths to add up.`
    + ` ${_esc(driverName)} is what sets it: it starts at +${_fmtHours(driver.start)} and its longest job runs ${_fmtHours(driver.longest)}.</div></div>`;
  return html + '</div>';
}

// The build as a PRODUCTION MATRIX: stage columns flow left→right (raw/reacted on the left,
// finished product on the right) and each ROW is a building — the reaction structure, the
// manufacturing structure, and the market you buy from. A persistent labelled row per building is
// the point: you read across a row to see everything one structure does, and down a column to see
// what a stage needs from each. Reactions row sits on top because it's what happens first.
// Hovering a card traces its whole chain in both directions.
let _indPipeGraph = { inputsOf: {}, consumersOf: {} };

function _indPipelineHtml(d, tiersData, model) {
  const roots = d.trees || (d.tree ? [d.tree] : []);
  if (!roots.length || !roots.some(t => (t.inputs || []).length)) return '';
  const { inputsOf, consumersOf } = tiersData;
  _indPipeGraph = { inputsOf: inputsOf || {}, consumersOf: consumersOf || {} };

  const cols = model.cols;
  if (!cols.length) return '';

  const isRx = e => e.activity === 'reaction';
  const isMfg = e => e.activity !== 'reaction';
  // Row per building, in the order the work actually happens: react → manufacture → (buy feeds both).
  const rows = [
    { key: 'rx', title: 'Reactions', sub: _indBuildingLabel('reaction') || 'reaction structure',
      pick: c => c.builds.filter(isRx) },
    { key: 'mfg', title: 'Manufacturing', sub: _indBuildingLabel('manufacturing') || 'your structure',
      pick: c => c.builds.filter(isMfg) },
    { key: 'buy', title: 'Buy', sub: 'from market', pick: c => c.buys },
  ].filter(r => cols.some(c => r.pick(c).length));

  // No "build" tag on the card — the row it sits in already says Reactions vs Manufacturing, so
  // repeating it just costs width. Qty and runs are what actually differ per card.
  const prog = _indProgTypeMap();
  const buildCard = e => {
    // The pipeline card is the most-read surface in the tab and the one where this went wrong:
    // "2 runs" (the batch) sat directly beside a bare "BPC", which reads as a 2-run copy.
    const owned = _indOwnedBpChip(e.owned, e.runs);
    const runs = e.runs ? `<span class="ind-pipe-runs" id="pruns-${e.type_id}">${e.runs.toLocaleString()}&nbsp;run${e.runs > 1 ? 's' : ''}</span>` : '';
    const qty = `×${Math.round(e.qty).toLocaleString()}`;
    // Live state from real ESI jobs, when we have it — the pipeline doubles as a progress board.
    // Three states you can read at a glance: done (green border), in the cooker (accent + glow),
    // waiting (greyed back). Anything with no progress data at all keeps the neutral card.
    const p = prog[e.type_id];
    let state = '', cls = '';
    if (p && p.required_runs) {
      if (p.done_runs >= p.required_runs) {
        state = '<span class="ind-pipe-state ind-st-done">✓ done</span>'; cls = ' ind-pipe-is-done';
      } else if (p.running_runs > 0) {
        state = `<span class="ind-pipe-state ind-st-run">${p.running_runs} cooking</span>`; cls = ' ind-pipe-is-run';
        if (p.done_runs > 0) state += `<span class="ind-pipe-state ind-st-part">${p.done_runs}/${p.required_runs}</span>`;
      } else if (p.done_runs > 0) {
        state = `<span class="ind-pipe-state ind-st-part">${p.done_runs}/${p.required_runs}</span>`; cls = ' ind-pipe-is-run';
      } else {
        state = '<span class="ind-pipe-state ind-st-wait">waiting</span>'; cls = ' ind-pipe-is-wait';
      }
    }
    // The card already IS the progress readout for this step, so it's also where you correct it:
    // each click advances it one state, wrapping back round so a misclick costs clicks and not
    // data. A tick tucked into the step-by-step chips was the first cut and too small to read,
    // never mind aim at.
    const markable = _featureActive('industry_manual_done') && p && p.required_runs;
    const st = markable ? _indDoneState(p) : 'none';
    const onclick = markable ? ` onclick="indCycleDone(${e.type_id})"` : '';
    const nextTip = st === 'done' ? ' Click to set it back to not started.'
      : st === 'running' ? ' Click when it has finished.'
      : ' Click to say it is running.';
    const tip = `${_esc(e.name)} — ${qty}${e.runs ? ', ' + e.runs + ' runs' : ''}. Hover to trace its chain.`
      + (markable ? nextTip : '');
    // The run count doubles as the way in to a partial mark when there's more than one run to
    // split. One run can't be half done, so it stays plain text there.
    const runsCell = (markable && p.required_runs > 1)
      ? `<span class="ind-pipe-runs ind-pipe-runs-edit" id="pruns-${e.type_id}"`
        + ` onclick="indEditDoneRuns(event, ${e.type_id}, ${p.required_runs}, ${p.done_runs})"`
        + ` title="${p.done_runs} of ${p.required_runs} runs done \u2014 click to set how many, rather than the whole step">`
        // Label stays the run count: how many are DONE is already on this card as its state
        // badge, and the same number twice on a card this small reads as two different ones.
        + `${p.required_runs.toLocaleString()}&nbsp;runs</span>`
      : runs;
    return `<div class="ind-pipe-card ind-pipe-build${cls}${markable ? ' ind-pipe-markable' : ''}"${onclick}`
      + ` data-tid="${e.type_id}" title="${tip}">`
      + `<span class="ind-pipe-name">${_esc(e.name)}</span>`
      + `<span class="ind-pipe-meta"><span class="ind-pipe-qty">${qty}</span>${runsCell}${owned}${state}</span></div>`;
  };
  const buyCard = (buys, t) => {
    const names = buys.slice(0, 25).map(b => b.name).join(', ') + (buys.length > 25 ? '…' : '');
    const members = buys.map(b => b.type_id).join(',');
    return `<div class="ind-pipe-card ind-pipe-buys" data-members="${members}" title="${_esc(names)} — click to jump to this stage's shopping list" onclick="_indJumpToStage(${t})"><span class="ind-pipe-name">Buy ${buys.length} material${buys.length > 1 ? 's' : ''}</span>`
      + `<span class="ind-pipe-meta">in shopping list ↓</span></div>`;
  };

  // Header row: empty corner over the building labels, then one label per stage.
  let html = `<div class="ind-pipe-corner"></div>`;
  cols.forEach((col, i) => {
    // With live progress, the counter becomes "done/total jobs" for the stage instead of a bare
    // count of things to build.
    let count = col.builds.length ? `<span>${col.builds.length}</span>` : '';
    if (col.builds.length && Object.keys(prog).length) {
      let need = 0, did = 0;
      col.builds.forEach(b => { const p = prog[b.type_id]; if (p) { need += p.required_runs; did += p.done_runs; } });
      if (need) count = `<span class="${did >= need ? 'ind-hd-done' : ''}" title="${did} of ${need} jobs done in this stage">${did}/${need}</span>`;
    }
    html += `<div class="ind-pipe-hd${col.t === 0 ? ' ind-pipe-hd-final' : ''}${i < cols.length - 1 ? ' ind-pipe-hd-flow' : ''}">${col.label}${count}</div>`;
  });

  // One grid row per building; empty cells keep every stage aligned across the rows.
  rows.forEach(r => {
    html += `<div class="ind-pipe-rowlbl ind-row-${r.key}"><span class="ind-pipe-rowname">${r.title}</span>`
      + `<span class="ind-pipe-rowsub" title="${_esc(r.sub)}">${_esc(r.sub)}</span></div>`;
    cols.forEach(col => {
      const mine = r.pick(col);
      let cards = '';
      if (mine.length) {
        if (r.key === 'buy') {
          cards = buyCard(mine, col.t);
        } else {
          const sorted = mine.slice().sort((a, b) => (b.qty || 0) - (a.qty || 0));
          cards = sorted.slice(0, 10).map(buildCard).join('');
          if (sorted.length > 10) cards += `<div class="ind-pipe-more">+${sorted.length - 10} more</div>`;
        }
      }
      html += `<div class="ind-pipe-cell ind-row-${r.key}${col.t === 0 ? ' ind-pipe-final' : ''}">${cards}</div>`;
    });
  });

  return `<details class="ind-details" open><summary>Build pipeline</summary>`
    + `<p class="ind-pipe-hint">Each row is a building, each column a stage. Hover a step to trace its`
    + ` whole chain${_featureActive('industry_manual_done') ? ', or click one to step it on: not started → running → done' : ''}.</p>`
    + `<div class="ind-pipe-scroll"><div class="ind-pipe" style="--ind-cols:${cols.length}">${html}</div></div></details>`;
}

// The type_ids a card stands for: a build card is one type, a condensed buy card is many.
function _indCardTids(card) {
  if (!card) return [];
  if (card.dataset.tid) return [Number(card.dataset.tid)];
  return (card.dataset.members || '').split(',').filter(Boolean).map(Number);
}

// Walk an edge map transitively from a set of seeds, returning everything reachable (excluding the
// seeds). Cycle-guarded via the visited set.
function _indReach(seeds, edges) {
  const seen = new Set();
  const stack = [...seeds];
  while (stack.length) {
    const cur = stack.pop();
    (edges[cur] || []).forEach(n => { if (!seen.has(n)) { seen.add(n); stack.push(n); } });
  }
  seeds.forEach(s => seen.delete(s));
  return seen;
}

// Hover trace: dim the pipeline, then light up the hovered step plus its WHOLE chain in both
// directions — everything it ultimately feeds (so hovering stage 3 lights stage 2 *and* stage 1)
// and everything that ultimately feeds it, not just the immediate neighbours.
function _indPipeHover(card) {
  const grid = card.closest('.ind-pipe');
  if (!grid) return;
  const { inputsOf, consumersOf } = _indPipeGraph;
  const self = new Set(_indCardTids(card));
  const feeds = _indReach(self, consumersOf);   // downstream — all it ends up in
  const fedBy = _indReach(self, inputsOf);      // upstream — everything that goes into it
  grid.classList.add('ind-pipe-focus');
  grid.querySelectorAll('.ind-pipe-card').forEach(c => {
    c.classList.remove('ind-hi-self', 'ind-hi-out', 'ind-hi-in');
    if (c === card) { c.classList.add('ind-hi-self'); return; }
    const tids = _indCardTids(c);
    if (tids.some(t => feeds.has(t))) c.classList.add('ind-hi-out');
    else if (tids.some(t => fedBy.has(t))) c.classList.add('ind-hi-in');
  });
}

function _indPipeClearHover(grid) {
  if (!grid) return;
  grid.classList.remove('ind-pipe-focus');
  grid.querySelectorAll('.ind-pipe-card').forEach(c => c.classList.remove('ind-hi-self', 'ind-hi-out', 'ind-hi-in'));
}

// Delegated once at the document level — the pipeline is re-rendered via innerHTML on every plan,
// so per-element listeners would be lost each time.
document.addEventListener('mouseover', e => {
  if (!e.target.closest) return;
  const card = e.target.closest('.ind-pipe-card');
  if (card) { _indPipeHover(card); return; }
  // Moved into the pipeline but not onto a card (gap/lane label) — drop the trace.
  const grid = e.target.closest('.ind-pipe');
  if (grid) _indPipeClearHover(grid);
});
document.addEventListener('mouseout', e => {
  if (!e.target.closest) return;
  const grid = e.target.closest('.ind-pipe');
  if (grid && !grid.contains(e.relatedTarget)) _indPipeClearHover(grid);
});

// ── The notice stack, and the bar it has to clear ────────────────────────────────────────────
// ONE block above the plan, never a column of coloured banners. Everything in here either corrects
// a number the builder would otherwise believe (times, fees, an unpriced material) or names money
// they are spending (copies bought). A notice a reader will not act on is not worth its space,
// however true it is — which is what removed the "this schedule assumes unlimited blueprint
// copies" paragraph and the standalone "no skill data yet" box.

// Job times come from the account's REAL Industry / Advanced Industry levels (see
// account_industry_time_mults). When no character has been scanned there are no real levels to use
// and V/V stands in — say so, because an optimistic time that looks identical to a measured one is
// exactly the number people promise deliveries on.
function _indSkillBasisWarn(d) {
  if (d.skill_time_basis !== 'assumed') return '';
  return `<div class="ind-note-line">Job times assume Industry V and Advanced Industry V \u2014 no `
    + `character has been scanned for skills yet. Rescan to plan against your real training.</div>`;
}

// Job installation fee = EIV x (system cost index + facility tax + 4% SCC). With no build system
// configured the INDEX term is missing, so the fee is light by exactly that share \u2014 the SCC and any
// tax are still charged, so this is an understatement, not a zero. Worth saying plainly: the index
// runs from 0.14% to 17.25% across New Eden, so in a busy system it is most of the fee.
function _indCostBasisWarn(d) {
  const cb = d.cost_basis;
  // A DEFAULTED system is not the same as a configured one, and saying nothing about it would make
  // an assumption look like a fact. A structure you build in is a good answer; Jita is a reference
  // and will be wrong for a null-sec builder, so it says so and offers the fix either way.
  if (cb && cb.system_id && (cb.basis === 'structure' || cb.basis === 'reference')) {
    const what = cb.basis === 'structure'
      ? 'the system of a structure you build in'
      : 'Jita as a reference \u2014 your real index is probably lower';
    return `<div class="ind-note-line">Job fees are costed against ${what}, because no build system `
      + `is set. `
      + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Set your build system</button>`
      + ` to quote the real one.</div>`;
  }
  if (!cb || cb.system_id) return '';
  // Points at Structures & Markets, which is where the system actually lives (the planner reads the
  // account's reaction system + facility tax \u2014 account_build_defaults). It used to open Setup &
  // slots, which holds blueprints, stock and job slots and no way whatsoever to set a system: an
  // instruction that leads somewhere it can't be carried out is worse than no instruction.
  return `<div class="ind-note-line">Job fees exclude the system cost index \u2014 no build system is `
    + `set, so only the 4% SCC${cb.facility_tax_pct ? ' and facility tax' : ''} are counted. `
    + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Set your build system</button>`
    + ` (Structures &amp; Markets \u2192 <i>your reaction/build system</i>) for a true install cost.</div>`;
}

// The default reaction policy changed on 2026-08-05 (build hybrid polymers and biochemicals, buy
// composites & intermediates). It clears the notice bar because it moved what a build COSTS for an
// account that changed nothing — the net cost, and so the floor under every quote off it, is not the
// number they were looking at last week — and because the fix is one click away. It is deliberately
// not a second policy control: it says what moved and points at the row right below it.
//
// Shown ONLY to accounts still on the default (`defaulted` from the server — a stored policy was
// never touched by this change), and only until dismissed. Dismissal is localStorage, like
// `indFacilityNudge`: an acknowledgement of a one-off announcement is not worth a settings column,
// and the worst case of a new browser is one more line to close.
function _indRxDefaultNote() {
  if (!_indRxPolicy || !_indRxPolicy.defaulted) return '';
  try { if (localStorage.getItem('indRxDefaultNote') === 'off') return ''; } catch (e) {}
  return `<div class="ind-note-line">Reaction default changed: hybrid polymers and biochemicals are `
    + `now built (they feed later steps directly), composites &amp; intermediates bought. Set your `
    + `own under <i>Reactions for this build</i> below. `
    + `<button class="ind-link-btn" onclick="indDismissRxDefaultNote(this)">Dismiss</button></div>`;
}

function indDismissRxDefaultNote(btn) {
  try { localStorage.setItem('indRxDefaultNote', 'off'); } catch (e) {}
  const line = btn && btn.closest('.ind-note-line');
  const box = line && line.closest('.ind-notes');
  if (line) line.remove();
  // The block is one container; an empty one would leave its padding behind as a stray bar.
  if (box && !box.querySelector('.ind-note-line')) box.remove();
}

// A pin the plan could NOT honour. Silence here would be the worst outcome: the user stated where a
// family is built, the plan built it somewhere else, and nothing on the screen says which. One line,
// naming the family and what happened, because the fix (re-pin it, or turn the structure back on)
// is theirs to make.
function _indPinNote(d) {
  const rows = d.build_pins_unapplied || [];
  if (!rows.length) return '';
  const off = rows.some(r => r.reason === 'routing_off');
  const names = rows.map(r => r.label).join(', ');
  return `<div class="ind-note-line">Not built where you pinned it: <b>${_esc(names)}</b> — `
    + (off ? `per-structure routing is off for this account, so every job used your selected facility.`
           : `that structure isn't available for those jobs (removed, or it doesn't run that `
             + `activity), so the plan routed them automatically.`)
    + `</div>`;
}

// The one block. `withSkills` is the only thing that differs between the two renderers: the modal
// checks whether your characters can install the jobs this plan schedules, the live build page
// does not (and never has — don't "fix" that by quietly adding a panel to the busiest screen).
function _indNotices(d, withSkills) {
  const unres = (d.unresolved && d.unresolved.length)
    ? `<div class="ind-note-line">${d.unresolved.length} material(s) had no market price — cost is `
      + `a floor.</div>` : '';
  const body = unres + _indRxDefaultNote() + _indSkillBasisWarn(d) + _indCostBasisWarn(d)
    + _indCopyShortWarn(d) + _indParallelCopyNote(d) + _indPrintLimitNote(d)
    + _indMissingBpWarn(d) + _indPinNote(d) + (withSkills ? _indSkillWarn(d) : '');
  return body ? `<div class="ind-notes">${body}</div>` : '';
}

function _indRenderPlan(d, title) {
  _indReqMeTe = {};
  (d.requirements || []).forEach(r => { _indReqMeTe[r.type_id] = { me: r.me, te: r.te, me_source: r.me_source }; });
  const leftovers = (d.leftovers && d.leftovers.length)
    ? `<details class="ind-details"><summary>Reusable leftovers (${d.leftovers.length}) — ${fmtIsk(d.metrics.leftover_value || 0)} credited</summary>`
      + d.leftovers.map(l => `<div class="ind-tree-row"><span class="ind-tree-name">${_esc(l.name)}</span> `
        + `<span class="ind-tree-qty">×${Math.round(l.qty).toLocaleString()}</span>`
        + (l.value ? `<span class="ind-tree-cost">${fmtIsk(l.value)}</span>` : '') + `</div>`).join('') + `</details>` : '';
  const boughtIds = new Set((d.shopping_list || []).map(s => s.type_id));
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, boughtIds)
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  const stageModel = _indStageModel(tiersData);
  const allRoots = d.trees || (d.tree ? [d.tree] : []);
  const treeKids = allRoots.flatMap(t => (t.inputs || []).map(c => _indTreeNode(c, 0))).join('');
  const tree = treeKids
    ? `<details class="ind-details"><summary>Debug: full build tree</summary><div class="ind-tree">${treeKids}</div></details>` : '';
  return `<div class="pp-card">
    <h2 class="pp-card-title">${title}</h2>
    <div class="ind-body">
      ${_indMetricTiles(d.metrics)}
      ${_indNotices(d, true)}
      ${_indMarginalBar(d)}
      ${_indReactionPolicyBar(d)}
      ${_indStepsHtml(d, stageModel)}
      ${_indPipelineHtml(d, tiersData, stageModel)}
      <details class="ind-details" open><summary>Shopping list (${(d.shopping_list || []).length})</summary>${_indShoppingSections(d, stageModel, true)}</details>
      ${tree}
      ${leftovers}
    </div>
  </div>`;
}

// The plan's contents without the card chrome — the status view supplies its own heading and tiles,
// so it renders the pipeline/steps/shopping list directly rather than a card inside a card.
// You can't install a manufacturing job without the blueprint. Say so plainly, and be explicit
// that the quoted cost excludes it — a capital BPC is a large, invisible addition otherwise.
// Each rendered warning gets its own instance id. The plan is rendered in TWO places — the status
// view and the preview modal — and both can show the same missing blueprint at once. With a plain
// `bpcpx-<type_id>` the two blocks collide, getElementById returns whichever is first in the
// document (the status view, sitting behind the modal), and the modal's row sits on "checking
// contracts…" forever. That was a real bug, not a hypothetical.
let _indBpcSeq = 0;

// The prints the plan is SHORT of and will not buy for you. A reaction formula is durable — it is
// reused by every build after this one — so the plan says what another one is worth in TIME and
// leaves the spend to the builder. Nothing here is in any cost on the page.
//
// ONE line. It used to be a headed block with a row per step plus a paragraph of explanation, and
// above it a second block ("this schedule assumes unlimited blueprint copies") for the state where
// the account's blueprint picture is incomplete. That second one is GONE: it was prose saying a
// number might be optimistic, with nothing to do about it on a page already too dense. The
// coverage gate itself (`print_coverage` / `prints_known()`) is untouched — a half-connected
// account is still never capped — and the fact now rides in the build-time tile's tooltip
// (`_indStatusHeadline`) rather than a banner.
function _indPrintLimitNote(d) {
  const rows = (d.print_limits || []).filter(r => (r.extra || 0) > 0);
  if (!rows.length) return '';
  // The best trade in the list leads, because it is the one worth acting on; the rest are in the
  // tooltip, where looking something up costs nothing and reading it costs no space.
  const best = rows.slice().sort((a, b) => (b.hours - b.hours_if_held) - (a.hours - a.hours_if_held))[0];
  const detail = rows.map(r => `${r.name}: ${r.held} ${r.noun}${r.held === 1 ? '' : 's'} held, `
    + `${r.jobs} job${r.jobs === 1 ? '' : 's'} at ${_fmtHours(r.hours)} — +${r.extra} → `
    + `${_fmtHours(r.hours_if_held)}`).join('\n');
  return `<div class="ind-note-line" title="${_esc(detail)}">`
    + `${rows.length} step${rows.length === 1 ? '' : 's'} run in fewer jobs than your slots allow — `
    + `a print is locked while a job runs on it. Best: <b>+${best.extra} ${_esc(best.noun)}`
    + `${best.extra === 1 ? '' : 's'}</b> of ${_esc(best.name)} would take it from `
    + `${_fmtHours(best.hours)} to ${_fmtHours(best.hours_if_held)}. Nothing was bought for these.`
    + `</div>`;
}

// A print is LOCKED while a job runs on it, so two jobs of one type at the same moment need two
// prints. Where the plan buys them to keep your slots busy, that is a purchase nobody asked for —
// so the SPEND stays on the page, on its own line, and never disappears into the blueprint cost
// beside it. Buying copies to cover RUNS you're short of and buying them to fill SLOTS are two
// different decisions, and only one of them is about being able to build the thing at all.
// One line, not a headed block with a row per type: the number that matters is the total ISK, and
// which types it was spent on is a lookup (the tooltip), not a decision.
function _indParallelCopyNote(d) {
  const rows = (d.blueprint_parallel || []).filter(r => (r.copies || 0) > 0);
  if (!rows.length) return '';
  const total = rows.reduce((s, r) => s + (r.cost || 0), 0);
  const copies = rows.reduce((s, r) => s + r.copies, 0);
  const detail = rows.map(r => `${r.name}: ${r.jobs} job${r.jobs === 1 ? '' : 's'} at once, `
    + `${r.copies} extra cop${r.copies === 1 ? 'y' : 'ies'}${r.covered ? '' : ' (estimated)'} `
    + `— ${fmtIsk(r.cost)}`).join('\n');
  return `<div class="ind-note-line ind-note-spend" title="${_esc(detail)}">`
    + `<b>${fmtIsk(total)}</b> of the total is ${copies} blueprint cop${copies === 1 ? 'y' : 'ies'} `
    + `bought so ${rows.length === 1 ? 'this step runs' : 'these steps run'} in parallel — they buy `
    + `speed, not the ability to build.</div>`;
}

// Owning a COPY is not owning the blueprint for any batch size: it carries a fixed number of runs.
// This says so, because "you have the blueprint" while sixteen of twenty runs have nowhere to come
// from is the kind of quiet wrong that gets found at the industry terminal.
function _indCopyShortWarn(d) {
  const short = (d.requirements || []).filter(r => (r.runs_short || 0) > 0);
  if (!short.length) return '';
  const rows = short.map(r => {
    const have = (r.blueprint && r.blueprint.runs) || 0;
    // All three numbers, each named, on one line: what the BUILD needs, what the COPY carries, and
    // how many COPIES that leaves to buy. Two of the three used to be shown as bare counts either
    // side of the word "copy", which is exactly ambiguous enough to read as a run count on the
    // print itself.
    const buy = r.copies_to_buy
      ? `<span class="ind-bp-px">buy ${r.copies_to_buy} more cop${r.copies_to_buy === 1 ? 'y' : 'ies'}</span>`
      : `<span class="ind-bp-px">${r.runs_short} run${r.runs_short > 1 ? 's' : ''} short</span>`;
    return `<div class="ind-bp-row2"><span class="ind-bp-nm">${_esc(r.name)}`
      + `<span class="ind-bp-need">build needs ${r.runs} run${r.runs > 1 ? 's' : ''} · `
      + `your copy carries ${have} · ${r.runs_short} run${r.runs_short > 1 ? 's' : ''} short</span></span>`
      + buy + `</div>`;
  }).join('');
  return `<div class="ind-note-block"><b>Your blueprint ${short.length === 1 ? 'copy runs' : 'copies run'} out</b>`
    + `<div class="ind-bp-rows">${rows}</div>`
    + `<div class="ind-bp-warn-sub">A copy carries a fixed number of runs, so the rest of the batch `
    + `needs more copies — those are priced into the total above, at contract prices.</div></div>`;
}

function _indMissingBpWarn(d) {
  const miss = (d.metrics && d.metrics.missing_blueprints) || [];
  if (!miss.length) return '';
  const inst = ++_indBpcSeq;
  const rows = miss.map(m => {
    // How many runs this build needs of it — the thing that decides how many copies you must buy,
    // since a copy carries a fixed number of runs and one contract is one item.
    const need = m.runs_needed ? `<span class="ind-bp-need">${m.runs_needed} run${m.runs_needed > 1 ? 's' : ''} needed</span>` : '';
    return `<div class="ind-bp-row2"><span class="ind-bp-nm">${_esc(m.name)}${need}</span>`
    + `<span class="ind-bp-px" id="bpcpx-${inst}-${m.type_id}">checking contracts…</span></div>`;
  }).join('');
  // Fill the prices in after render — a cold contract index is a background scan, so the warning
  // must never wait on it.
  setTimeout(() => indLoadBpcPrices(inst, miss.map(m => m.type_id), miss), 0);
  // No nagging about roles or connecting more characters — the user can't act on that and doesn't
  // need to be told. Just say the list may be incomplete and give them the price.
  return `<div class="ind-note-block"><b>No blueprint found for ${miss.length === 1 ? 'this' : 'these'}</b>`
    + `<div class="ind-bp-rows">${rows}</div>`
    + `<div class="ind-bp-warn-sub">Blueprints in corp hangars aren't visible here, so prints you `
    + `already have can still show up in this list — it's a price so you can compare against a `
    + `local seller. Copy prices, where copies are listed, are included in the total above.</div></div>`;
}

// Skills you don't have to install the jobs this plan schedules. The server sends `skill_gaps` only
// while the `required_skills` feature is on, so the absent key — not a flag check here — is what
// keeps this silent when the feature is off.
//
// Stays quiet when there's nothing to say. A plan every character can already install produces no
// panel at all; this is a blocker list, not a skill sheet.
const _INDSK_ROMAN = ['0', 'I', 'II', 'III', 'IV', 'V'];
const _indSkLvl = n => _INDSK_ROMAN[n] || String(n);

function _indSkillWarn(d) {
  const g = d.skill_gaps;
  if (!g) return '';                       // feature off — server omitted the key
  // NOTHING is said unless a step is actually blocked. The two info states this used to render —
  // "the SDE hasn't backfilled blueprint_skills yet" and a bare "no skill data yet for X" box on a
  // plan with no gaps at all — were both banners about our own state of knowledge, on a page where
  // the space belongs to what the builder has to do. The unknown-characters line survives INSIDE a
  // real gap report, where it qualifies a finding they are already reading.
  if (!g.blocked_steps) return '';
  const unknown = g.characters_without_data || [];
  // A character we've never read skills for is a DIFFERENT answer from one who lacks the skills,
  // and it's the one the user can fix.
  const unknownNote = unknown.length
    ? `<div class="ind-sk-sub">No skill data yet for ${unknown.map(_esc).join(', ')} — `
      + `rescan ${unknown.length === 1 ? 'that character' : 'those characters'} to include `
      + `them in this check.</div>`
    : '';
  const summary = (g.missing || []).map(m =>
    `<span class="ind-sk-chip" title="Needed for ${m.steps} build step${m.steps === 1 ? '' : 's'}">`
    + `${_esc(m.name)} ${_indSkLvl(m.level)}</span>`).join('');
  const rows = (g.steps || []).map(s => {
    const who = s.character_name
      ? `<span class="ind-sk-who">closest: ${_esc(s.character_name)}</span>`
      : `<span class="ind-sk-who">no character with skill data</span>`;
    const miss = s.missing.map(m =>
      `<span class="ind-sk-miss">${_esc(m.name)} <b>${_indSkLvl(m.need)}</b>`
      + `<span class="ind-sk-have">have ${_indSkLvl(m.have)}</span></span>`).join('');
    return `<div class="ind-sk-row"><span class="ind-sk-nm">${_esc(s.name)}</span>${who}`
      + `<span class="ind-sk-misses">${miss}</span></div>`;
  }).join('');
  const n = g.blocked_steps;
  return `<div class="ind-note-block ind-note-block-skill"><b>Missing skills for ${n} build step${n === 1 ? '' : 's'}</b>`
    + `<div class="ind-sk-chips">${summary}</div>`
    + `<details class="ind-details"><summary>Which steps, and who comes closest</summary>`
    + `<div class="ind-sk-rows">${rows}</div></details>`
    + `<div class="ind-sk-sub">Skills don't pool across characters — one character installs one `
    + `job, so each step is checked against whichever of your characters comes closest.</div>`
    + unknownNote + `</div>`;
}

// Public-contract blueprint prices. Shows what's listed right now, and falls back to what they have
// historically gone for — blueprints sell out constantly, so "nothing listed today" is the normal
// case and still deserves an answer.
async function indLoadBpcPrices(inst, ids, miss) {
  if (!ids || !ids.length) return;
  const byId = {};
  (miss || []).forEach(m => { byId[m.type_id] = m; });
  let d = null;
  try {
    d = await api('/api/industry/bpc?type_ids=' + ids.join(','));
  } catch (e) {}
  const scanning = d && d.scan && d.scan.busy;
  ids.forEach(id => {
    const el = document.getElementById('bpcpx-' + inst + '-' + id);
    if (!el) return;
    const info = d && d.prices && d.prices[id];
    const bpc = info && info.bpc;
    if (bpc && bpc.live && bpc.live.count) {
      // Prefer what the plan actually worked out: the cheapest COMBINATION covering the runs this
      // build needs, and how many contracts that is. A single cheapest price is misleading when one
      // copy doesn't carry enough runs.
      const need = byId[id] || {};
      if (need.cost != null && need.copies) {
        el.innerHTML = `<b>${fmtIsk(need.cost)}</b> for ${need.copies} cop${need.copies === 1 ? 'y' : 'ies'}`
          + `<span class="ind-bp-sub2">${need.covered === false
              ? `only ${bpc.live.count} listed — not enough runs, rest estimated`
              : `covers ${need.runs_needed} run${need.runs_needed > 1 ? 's' : ''} · ${bpc.live.count} listed`}</span>`;
      } else {
        const runs = bpc.live.median_per_run ? ` · ${fmtIsk(bpc.live.median_per_run)}/run` : '';
        el.innerHTML = `<b>${fmtIsk(bpc.live.cheapest)}</b> cheapest now`
          + `<span class="ind-bp-sub2">${bpc.live.count} on contract · median ${fmtIsk(bpc.live.median)}${runs}</span>`;
      }
    } else if (bpc && bpc.history && bpc.history.count) {
      const days = Math.max(0, Math.round((Date.now() / 1000 - bpc.history.last_seen) / 86400));
      el.innerHTML = `<b>≈ ${fmtIsk(bpc.history.median)}</b> estimated`
        + `<span class="ind-bp-sub2">none listed now · ${bpc.history.count} seen historically, `
        + `last ${days === 0 ? 'today' : days + 'd ago'}</span>`;
    } else if (info && info.bpo && (info.bpo.live || info.bpo.history)) {
      const b = info.bpo.live || info.bpo.history;
      el.innerHTML = `<span class="ind-bp-sub2">no copies seen — originals from ${fmtIsk(b.cheapest)}</span>`;
    } else {
      el.innerHTML = `<span class="ind-bp-sub2">${scanning
        ? 'indexing Jita contracts — check back in a few minutes' : 'no contracts seen for this yet'}</span>`;
    }
  });
}

function _indRenderPlanBody(d) {
  _indReqMeTe = {};
  (d.requirements || []).forEach(r => { _indReqMeTe[r.type_id] = { me: r.me, te: r.te, me_source: r.me_source }; });
  // The queue plan carries one tree per ordered product (`trees`); a single-product preview carries
  // one (`tree`). Either way the tier walk merges them by type, matching the aggregated demand.
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, new Set((d.shopping_list || []).map(x => x.type_id)))
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  const stageModel = _indStageModel(tiersData);
  // Skill blockers belong HERE, not only in the preview modal. `_indSkillWarn` now says nothing
  // unless a step is genuinely blocked, so this adds a line to the build page exactly when nobody
  // on the account can install one of the jobs it is telling you to start — which is the one moment
  // it is worth the space. The queue plan has always carried `skill_gaps` (_run_queue_plan); it was
  // simply never rendered, so the blocker was visible while planning and invisible while building.
  return _indNotices(d, true)
    + _indMarginalBar(d)
    + _indReactionPolicyBar(d)
    + _indPipelineHtml(d, tiersData, stageModel)
    + _indStepsHtml(d, stageModel)
    + `<details class="ind-details"><summary>Shopping list (${(d.shopping_list || []).length})</summary>`
    + _indShoppingSections(d, stageModel) + `</details>`;
}

// Task waves only carry type_id; keep a name cache from the last plan's shopping/tree so waves read nicely.
let _indNameCache = {};
function _indName(tid) { return _indNameCache[tid] || ('#' + tid); }
function _indCacheNames(d) {
  (d.shopping_list || []).forEach(s => { _indNameCache[s.type_id] = s.name; });
  (d.targets || []).forEach(t => { _indNameCache[t.type_id] = t.name; });
  const walk = n => { if (!n) return; _indNameCache[n.type_id] = n.name; (n.inputs || []).forEach(walk); };
  walk(d.tree);
}

// ── Build queue ─────────────────────────────────────────────────────────────────────────────
async function indAddToQueue() {
  if (!_indPicked) return;
  const qty = Math.max(1, parseInt(document.getElementById('indQty').value) || 1);
  const srcSel = (document.getElementById('indPlanSrc') || {}).value || '';
  const pasted = srcSel === '__paste'
    ? ((document.getElementById('indPlanPasteText') || {}).value || '') : '';
  const keys = _indPlanSourceKeys();
  try {
    const order = await apiSend('POST', '/api/industry/orders',
      { product_type_id: _indPicked.type_id, quantity: qty,
        label: (document.getElementById('indLabel') || {}).value || '',
        // The overrides were decided against THIS product — they ride along, or
        // queueing would silently undo every one of them.
        force_build_ids: _indForceIds(), me_te_overrides: _indMeTeMap(),
        margin_pct: _indMarginPct(),
        // `source_keys` is only sent when per-plan sources are on, and sending it is what tells the
        // server this plan owns its stock. Without the flag the old single field goes up alone and
        // the account-wide behaviour is untouched.
        ...(_featureActive('industry_plan_sources')
              ? { source_keys: keys }
              : { source_key: srcSel === '__paste' ? '' : srcSel }) });
    // A paste is per-order sourcing, not planner stock: it says what's already gathered for THIS
    // build, so it lands on the order's checklist and nowhere else. Best-effort — the order itself
    // is already queued, and failing the whole action over the checklist would be the wrong trade.
    if (pasted.trim() && order && order.id) {
      try { await apiSend('POST', `/api/industry/orders/${order.id}/sourcing/paste`, { text: pasted }); }
      catch (e) {}
    }
    document.getElementById('indResult').innerHTML = '';
    _indForcedTypes.clear();       // they live on the order now
    const lb = document.getElementById('indLabel');
    if (lb) lb.value = '';          // don't inherit the last customer on the next order
    const pt = document.getElementById('indPlanPasteText');
    if (pt) pt.value = '';          // and don't carry one build's materials onto the next
    indClosePlanner();
    await indLoadQueue();
    await indRefreshStatus();     // adding re-plans the whole queue together — the reason to queue
  } catch (e) { toastError(e, 'Could not queue'); }
}

// Live queue progress, keyed by type_id — populated by indLoadProgress() and read by the pipeline
// so its cards/stages can show what's actually done rather than just what's planned.
let _indProgress = null;

// Preview mode: null = off, otherwise a 0-100 completion to fabricate. Kept in the Setup modal
// rather than the main flow — it's a way to SEE the live views before you have live data, not a
// setting anyone should leave on. The server writes nothing for it.
let _indSim = null;

function indSimToggle() {
  const on = document.getElementById('indSimOn');
  const sl = document.getElementById('indSim');
  if (sl) sl.disabled = !(on && on.checked);
  _indSim = (on && on.checked) ? parseFloat(sl.value) : null;
  indSimInput();
  indLoadQueue();
  if (_indStatusVisible()) indRefreshStatus();
}
function indSimInput() {
  const sl = document.getElementById('indSim');
  const lbl = document.getElementById('indSimPct');
  if (lbl && sl) lbl.textContent = `${sl.value}% complete`;
}
function indSimChange() {
  const on = document.getElementById('indSimOn');
  const sl = document.getElementById('indSim');
  if (!(on && on.checked)) return;
  _indSim = parseFloat(sl.value);
  indLoadQueue();
  if (_indStatusVisible()) indRefreshStatus();
}

async function indLoadProgress() {
  try {
    _indProgress = await api('/api/industry/progress' + (_indSim === null ? '' : '?simulate=' + _indSim));
    if (_indProgress && _indProgress.empty) _indProgress = null;
  } catch (e) { _indProgress = null; }
  return _indProgress;
}

function _indProgTypeMap() {
  const m = {};
  ((_indProgress && _indProgress.types) || []).forEach(t => { m[t.type_id] = t; });
  return m;
}

// The queue no longer has a card of its own — orders render as chips in the status header, so
// "reload the queue" and "refresh the status" are the same operation.
async function indLoadQueue() { return indRefreshStatus(); }


// ── Per-order overrides + customer share links ───────────────────────────────────────────────
// Take back every "build it anyway" on one order. All of them at once: they were made together in
// one preview, and a per-component undo on a chip in a header row is more UI than the case wants.
async function indClearOrderForced(orderId) {
  try {
    await apiSend('PATCH', `/api/industry/orders/${orderId}`, { force_build_ids: [] });
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();     // the overrides change what gets built, so the whole plan moves
}

// A link the customer can open with no account: what's being built, which stage, how far, when.
// Minting is idempotent server-side, so this is also "show me the link I already made".
async function indShareOrder(orderId) {
  const box = document.getElementById('indShareBox');
  try {
    const d = await apiSend('POST', `/api/industry/orders/${orderId}/share`);
    const url = `${location.origin}/b/${d.share_id}`;
    if (!box) { prompt('Share this with your customer:', url); return; }
    box.style.display = '';
    box.innerHTML = `<span class="ind-share-lbl">Customer link</span>`
      + `<input class="ind-share-url" id="indShareUrl" readonly value="${_esc(url)}" onclick="this.select()">`
      + `<button class="ind-copy-btn" onclick="indCopyShare()">Copy</button>`
      + `<a class="ind-share-open" href="${_esc(url)}" target="_blank" rel="noopener">Open</a>`
      + `<button class="ind-share-revoke" onclick="indRevokeShare(${orderId})" title="Kill this link — the customer's page stops working">Revoke</button>`
      + `<button class="ind-share-x" onclick="document.getElementById('indShareBox').style.display='none'">✕</button>`;
  } catch (e) { toastError(e, 'Could not create the link'); }
}

function indCopyShare() {
  const el = document.getElementById('indShareUrl');
  if (!el) return;
  navigator.clipboard.writeText(el.value).then(() => {
    const b = el.parentElement.querySelector('.ind-copy-btn');
    if (b) { b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1500); }
  }).catch(() => el.select());
}

async function indRevokeShare(orderId) {
  if (!await ppConfirm('Revoke this link? The customer\'s page will stop working immediately.')) return;
  try {
    await apiSend('DELETE', `/api/industry/orders/${orderId}/share`);
  } catch (e) { /* revoking is best-effort from the UI's point of view */ }
  const box = document.getElementById('indShareBox');
  if (box) { box.innerHTML = '<span class="ind-share-lbl">Link revoked.</span>'; setTimeout(() => { box.style.display = 'none'; }, 2000); }
}

// ── Queue order ─────────────────────────────────────────────────────────────────────────────
// Position is not cosmetic: the scheduler ranks by it, so the first order wins a contested slot and
// its finish time is the "first delivery" number.
let _indOrderDraft = [];

function indOpenOrder() {
  const m = document.getElementById('indOrderModal');
  if (!m) return;
  // Start from the order the status view shows, which is already rank-sorted.
  const tgt = {};
  ((_indLastPlan && _indLastPlan.targets) || []).forEach(t => { tgt[t.type_id] = t; });
  _indOrderDraft = (_indOrders || []).slice().sort((a, b) => {
    const ra = (tgt[a.product_type_id] || {}).rank, rb = (tgt[b.product_type_id] || {}).rank;
    return (ra === undefined ? 99 : ra) - (rb === undefined ? 99 : rb) || a.id - b.id;
  });
  m.style.display = '';
  _indRenderOrderList();
}

function indCloseOrder() {
  const m = document.getElementById('indOrderModal');
  if (m) m.style.display = 'none';
}

function _indRenderOrderList() {
  const el = document.getElementById('indOrderList');
  if (!el) return;
  const byOrder = {};
  ((_indProgress && _indProgress.orders) || []).forEach(o => { byOrder[o.id] = o; });
  el.innerHTML = _indOrderDraft.map((o, i) => {
    const p = byOrder[o.id] || {};
    return `<div class="ind-ord-row">`
      + `<span class="ind-ord-pos">${i + 1}</span>`
      + `<span class="ind-ord-nm"><b>${o.quantity}×</b> ${_esc(o.name)}`
      + (p.label ? `<span class="ind-oc-for">${_esc(p.label)}</span>` : '') + `</span>`
      + `<button class="ind-ord-btn" title="Move up" ${i === 0 ? 'disabled' : ''} onclick="indMoveOrder(${i}, -1)">▲</button>`
      + `<button class="ind-ord-btn" title="Move down" ${i === _indOrderDraft.length - 1 ? 'disabled' : ''} onclick="indMoveOrder(${i}, 1)">▼</button>`
      + `</div>`;
  }).join('');
}

function indMoveOrder(i, d) {
  const j = i + d;
  if (j < 0 || j >= _indOrderDraft.length) return;
  const a = _indOrderDraft;
  [a[i], a[j]] = [a[j], a[i]];
  _indRenderOrderList();
}

// Group identical products together. They're built as one shared batch regardless, so this is about
// reading the queue, not about changing what gets built.
function indGroupByProduct() {
  const seen = [];
  _indOrderDraft.forEach(o => { if (!seen.includes(o.product_type_id)) seen.push(o.product_type_id); });
  _indOrderDraft.sort((a, b) =>
    seen.indexOf(a.product_type_id) - seen.indexOf(b.product_type_id) || a.id - b.id);
  _indRenderOrderList();
}

async function indSaveOrderOrder() {
  const msg = document.getElementById('indOrderMsg');
  if (msg) msg.textContent = 'Saving…';
  try {
    await apiSend('POST', '/api/industry/orders/reorder', { order: _indOrderDraft.map(o => o.id) });
  } catch (e) { if (msg) msg.textContent = e.message; return; }
  indCloseOrder();
  await indRefreshStatus();     // order changes ranks, ETAs and first delivery
}

// Edit in place rather than in a dialog: it's two fields, and re-planning the whole queue just to
// show an edit form would be a needless several-second wait.
// What ME/TE this order's OWN blueprint is being planned at, and where that came from. An order
// keeps its overrides in `me_te_overrides`; without one the plan resolves owned print → contract
// copy → un-researched, and `_indReqMeTe` carries whatever it landed on.
function _indOrderMeTe(o) {
  const ov = (o.me_te_overrides || {})[String(o.product_type_id)];
  if (ov) return { me: ov[0], te: ov[1], source: 'override', overridden: true };
  const r = _indReqMeTe[o.product_type_id];
  if (r) return { me: r.me, te: r.te, source: r.me_source, overridden: false };
  return { me: 0, te: 0, source: 'default', overridden: false };
}

function indEditOrder(id) {
  const chip = document.getElementById('oc-' + id);
  const o = (_indOrders || []).find(x => x.id === id);
  if (!chip || !o) return;
  const p = ((_indProgress && _indProgress.orders) || []).find(x => x.id === id) || {};
  const mt = _indOrderMeTe(o);
  // A print you own or bought yourself needn't match anything the plan could see. Editing it here
  // covers the top-level blueprint; components keep their own chips on the plan below.
  const meTe = mt.source === 'reaction' ? '' :
      `<span class="ind-oc-mete" title="Blueprint efficiency for ${_esc(o.name)} — ${_esc(_IND_ME_SRC[mt.source] || '')}. `
    + `Set what your own copy actually is.">ME `
    + `<input type="number" min="0" max="10" step="1" class="ind-oc-mt" id="oce-me-${id}" value="${mt.me}" `
    + `onwheel="indWheelStep(event, this)"> TE `
    + `<input type="number" min="0" max="20" step="2" class="ind-oc-mt" id="oce-te-${id}" value="${mt.te}" `
    + `onwheel="indWheelStep(event, this)">`
    + (mt.overridden
        ? ` <button class="ind-mete-clr" title="Back to what the plan works out for itself" `
          + `onclick="indClearOrderMeTe(${id})">reset</button>` : '')
    + `</span>`;
  chip.classList.add('ind-oc-editing');
  chip.innerHTML = `<span class="ind-oc-name">${_esc(o.name)}</span>`
    + `<input type="number" min="1" class="ind-oc-qty" id="oce-qty-${id}" value="${o.quantity}" title="Quantity">`
    + `<input type="text" maxlength="60" class="ind-oc-lbl" id="oce-lbl-${id}" `
    + `value="${_esc(p.label || '')}" placeholder="For — customer, contract…">`
    // The margin this order is quoted at. It's snapshotted per order — the planner's slider sets it
    // for NEW builds only, deliberately, so a quote a customer is holding can't move under them.
    // That leaves editing it here as the only way to change one, so here it is.
    + `<span class="ind-oc-margin"><input type="number" min="0" max="100" step="0.5" class="ind-oc-mrg" `
    + `id="oce-mrg-${id}" value="${o.margin_pct != null ? o.margin_pct : _indMarginPct()}" `
    + `onwheel="indWheelStep(event, this)" `
    + `title="Margin over net cost for this customer's quote — scroll to adjust">%</span>`
    + meTe
    // The per-order exception to the account's reaction rule. It lives here rather than on a panel
    // of its own: it is the same kind of per-order override the ME/TE and margin beside it are, and
    // it is only ever worth showing while you are editing that order.
    + (_featureActive('industry_reaction_policy')
        ? `<label class="ind-oc-rx" title="Make this order's reaction steps yourself, whatever your account rule says">`
          + `<input type="checkbox" id="oce-rx-${id}" ${o.build_reactions ? 'checked' : ''}> reacts</label>` : '')
    + `<button class="ind-oc-ok" onclick="indSaveOrder(${id})">Save</button>`
    + `<button class="ind-oc-cancel" onclick="indRefreshStatus()">Cancel</button>`;
  const q = document.getElementById('oce-lbl-' + id);
  if (q) q.focus();
}

// A number input only responds to the wheel while focused, which nobody discovers, and the margin is
// a nudge-until-it-looks-right number. Swallowing the scroll matters as much as stepping the value:
// without preventDefault the card scrolls out from under the cursor mid-adjustment. Rounding to the
// step keeps 12.5 + 0.5 from landing on 12.999999999999998.
function indWheelStep(ev, el) {
  ev.preventDefault();
  const step = parseFloat(el.step) || 1;
  const min = el.min === '' ? -Infinity : parseFloat(el.min);
  const max = el.max === '' ? Infinity : parseFloat(el.max);
  const cur = parseFloat(el.value);
  const base = isNaN(cur) ? (isFinite(min) ? min : 0) : cur;
  const next = base + (ev.deltaY < 0 ? step : -step);
  el.value = Math.min(max, Math.max(min, Math.round(next / step) * step));
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

// Drop the order's own-blueprint override and let the plan resolve it again. Component overrides
// on the same order are untouched.
async function indClearOrderMeTe(id) {
  const o = (_indOrders || []).find(x => x.id === id);
  if (!o) return;
  const next = { ...(o.me_te_overrides || {}) };
  delete next[String(o.product_type_id)];
  try {
    await apiSend('PATCH', '/api/industry/orders/' + id, { me_te_overrides: next });
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();
}

async function indSaveOrder(id) {
  const qty = parseInt((document.getElementById('oce-qty-' + id) || {}).value, 10);
  const label = (document.getElementById('oce-lbl-' + id) || {}).value || '';
  const body = { label };
  if (!isNaN(qty) && qty >= 1) body.quantity = qty;
  const mrg = parseFloat((document.getElementById('oce-mrg-' + id) || {}).value);
  if (!isNaN(mrg)) body.margin_pct = Math.max(0, Math.min(100, mrg));
  const rx = document.getElementById('oce-rx-' + id);
  if (rx) body.build_reactions = rx.checked;
  // ME/TE goes only when it actually MOVED. The inputs are seeded with whatever the plan resolved,
  // so sending them unconditionally would turn every rename into a permanent override pinning
  // today's guess — and the plan could then never improve on it (e.g. once you own the print).
  const o = (_indOrders || []).find(x => x.id === id);
  const me = parseFloat((document.getElementById('oce-me-' + id) || {}).value);
  const te = parseFloat((document.getElementById('oce-te-' + id) || {}).value);
  if (o && !isNaN(me) && !isNaN(te)) {
    const was = _indOrderMeTe(o);
    const m = Math.max(0, Math.min(10, me)), t = Math.max(0, Math.min(20, te));
    if (m !== was.me || t !== was.te) {
      body.me_te_overrides = { ...(o.me_te_overrides || {}), [String(o.product_type_id)]: [m, t] };
    }
  }
  try {
    await apiSend('PATCH', '/api/industry/orders/' + id, body);
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();     // quantity changes the whole plan, so re-plan
}

async function indRemoveOrder(id) {
  try { await apiSend('DELETE', '/api/industry/orders/' + id); } catch (e) {}
  await indLoadQueue();
  await indRefreshStatus();
}

// ── "To install now" checklist + in-progress jobs ───────────────────────────────────────────
async function indRefreshJobs() {
  try { await apiSend('POST', '/api/industry/jobs/refresh'); } catch (e) {}
  indLoadSlots();
  indLoadSetupSummary();
  await indRefreshStatus();   // redraws install / pipeline / running from the fresh job data
}

// "Do this now", written as instructions rather than a status. We know which characters have free
// slots and the plan knows which jobs are ready, so the checklist names WHO installs WHAT instead
// of reporting that "a slot" is free somewhere and leaving you to work it out.
// Collapse a character's assigned jobs to one line per PRODUCT. A big batch is split into one job
// per free slot, so a character can be handed a dozen identical installs — listing each separately
// turns one action ("start 12 of these") into twelve lines. Grouped, the checklist stays readable
// whether the plan has 18 jobs or 300. Longest job first, since that's what gates the stage.
// Does this plan install jobs in more than one structure? Only then is naming the building on
// every job worth the space — one structure means the answer is the same every line.
function _indIsMultiSite() {
  return ((_indLastPlan && _indLastPlan.build_sites) || []).length > 1;
}

// The routing's station changes ("Parts to move") are NOT rendered, and the plan no longer
// computes them. A builder whose jobs are routed to two structures already knows the parts have
// to travel; a list restating it was a panel that changed nothing anyone does.

function _indGroupJobs(jobs) {
  const by = {};
  (jobs || []).forEach(j => {
    const g = by[j.type_id] || (by[j.type_id] = {
      type_id: j.type_id, name: j.name || ('#' + j.type_id), activity: j.activity,
      count: 0, minRuns: Infinity, maxRuns: 0, totalRuns: 0, dur: 0, runsList: [], why: j.why,
      // Which structure to install it in. Only present when the plan is routed across several.
      site: j.site,
      // …and whether it is there because the user PINNED that family, not because it scored best.
      sitePinned: j.site_pinned,
    });
    g.count += 1;
    g.minRuns = Math.min(g.minRuns, j.runs);
    g.maxRuns = Math.max(g.maxRuns, j.runs);
    g.totalRuns += j.runs;
    g.runsList.push(j.runs);
    g.dur = Math.max(g.dur, j.duration_hours || 0);
  });
  return Object.values(by).map(g => ({
    ...g,
    // Bucket the jobs by run count, biggest batch first. An uneven split used to render as a
    // range ("165–166") or a total, and both leave the reader doing arithmetic to work out what
    // to actually type into the industry window. The buckets ARE the instruction: start 8 jobs
    // of 165 runs and 1 of 166. Usually one or two buckets, since the splitter divides a batch
    // across free slots, but nothing here assumes that.
    buckets: Object.entries(g.runsList.reduce((m, r) => (m[r] = (m[r] || 0) + 1, m), {}))
      .map(([runs, n]) => ({ runs: Number(runs), n }))
      .sort((a, b) => b.n - a.n || b.runs - a.runs),
  })).sort((a, b) => b.dur - a.dur);
}

// One labelled line per slot POOL. The two pools used to be bare pip strips sitting side by side
// with nothing naming them, so they read as one row of dots and the reaction pips looked like
// manufacturing jobs spilling into reaction slots. The name and the count carry the meaning; the
// pips are decoration on top of it.
function _indSlotRow(label, cls, used, free, assigned, slots) {
  if (!slots) return '';
  // The pips already say how many are busy, filling and free — spelling all three out again in
  // words was longer than the card is wide. The label disambiguates the pool, one number gives the
  // count at a glance, and the full breakdown stays in the tooltip.
  return `<div class="ind-slotrow ind-slotrow-${cls}" title="${assigned} to start · ${used} busy`
    + ` · ${slots} ${label.toLowerCase()} slot${slots > 1 ? 's' : ''}">`
    + `<span class="ind-slotlbl">${label}</span>`
    + `<span class="ind-slotset">${_indSlotPips(used, free, assigned, cls)}</span>`
    + `<span class="ind-slotnum"><b>${assigned}</b>/${slots}</span></div>`;
}

function _indSlotPips(used, free, assigned, cls) {
  const total = used + free;
  let out = '';
  for (let i = 0; i < total; i++) {
    const k = i < used ? 'busy' : (i < used + assigned ? 'fill' : 'open');
    out += `<span class="ind-pip ind-pip-${k} ind-pip-${cls}"></span>`;
  }
  return out || '<span class="ind-pip-none">no slots</span>';
}

// `d` is the install block from the plan response. It's passed in rather than fetched: asking the
// server for it meant re-planning the whole queue, which was the slowest thing on the page.
function indRenderInstall(d) {
  const el = document.getElementById('indInstall');
  if (!el) return;
  try {
    if (!d) { el.innerHTML = ''; return; }
    if (d.empty || !d.ready || !d.ready.length) { el.innerHTML = ''; return; }

    const doers = (d.characters || []).filter(c => c.assigned > 0);
    const cards = doers.map(c => {
      const groups = _indGroupJobs(c.jobs);
      const jobs = groups.map(g => {
        // Say exactly what to install. Neither "9× 165–166 runs each" nor a 1,486-run total
        // tells you what to type — both hand the reader a division problem. One entry per
        // distinct run count does: "8× 165 runs · 1× 166 runs" is nine installs, spelled out.
        const each = `<span class="ind-do-runs" title="${g.count} job${g.count > 1 ? 's' : ''} · `
          + `${g.totalRuns.toLocaleString()} runs in total">`
          + g.buckets.map(b => (g.count > 1 ? `<b>${b.n}×</b> ${b.runs}` : `<b>${b.runs}</b>`)
              + ` run${b.runs > 1 ? 's' : ''}`).join(' · ')
          + `</span>`;
        // Why this job is that long. "Everything else is 5h, why is this one 2h32m" has one
        // answer — something needs it sooner — and it is unanswerable from the screen otherwise.
        const w = g.why || {};
        const why = w.bound_by === 'consumer' && w.needed_by_name
          ? ` — held to this because ${_esc(w.needed_by_name)} needs it then`
          : w.bound_by === 'pace' ? ` — matched to the plan's pace (${_fmtHours(w.pace_h)})`
          : '';
        const dur = `<span class="ind-do-dur" title="${w.runs_per_job || 1} run(s) per job${_esc(why)}">`
          + `${_fmtHours(g.dur)}${why ? ' <span class="ind-do-why">?</span>' : ''}</span>`;
        // Where to install it. With group-specific rigs the plan may spread a build over several
        // structures, and "install 40 runs" without naming the building is half an instruction.
        // A PINNED step says so: "I chose this building" and "the tool worked it out" are different
        // facts about the same line, and only one of them is worth arguing with.
        const where = g.site && _indIsMultiSite()
          ? `<span class="ind-do-site${g.sitePinned ? ' ind-do-site-pin' : ''}" title="Install in `
            + `${_esc(g.site)}${g.sitePinned ? ' — you pinned this family here' : ''}">`
            + `@ ${_esc(g.site)}${g.sitePinned ? ' (pinned)' : ''}</span>` : '';
        return `<li class="ind-do-job"><span class="ind-do-name">${_esc(g.name)}</span>${each}`
          + where
          + `<span class="ind-do-act ind-do-${g.activity}">${g.activity === 'reaction' ? 'reaction' : 'industry'}</span>`
          + dur + `</li>`;
      }).join('');
      const mUsed = c.manufacturing_slots - c.manufacturing_free;
      const rUsed = c.reaction_slots - c.reaction_free;
      const mAss = c.jobs.filter(j => j.activity !== 'reaction').length;
      const rAss = c.jobs.filter(j => j.activity === 'reaction').length;
      return `<div class="ind-do-char">
        <div class="ind-do-hd"><span class="ind-do-who">${c.is_placeholder ? '<span class="pp-char-dummy-badge" title="Placeholder character — not connected to ESI; its slots are the ones you declared">placeholder</span> ' : ''}${_esc(c.character_name)}</span>
          <span class="ind-do-count">start ${c.assigned} job${c.assigned > 1 ? 's' : ''}`
          + (groups.length < c.assigned ? ` · ${groups.length} product${groups.length > 1 ? 's' : ''}` : '')
          + `</span></div>
        <div class="ind-do-slots">
          ${_indSlotRow('Industry', 'mfg', mUsed, c.manufacturing_free, mAss, c.manufacturing_slots)}
          ${_indSlotRow('Reactions', 'rx', rUsed, c.reaction_free, rAss, c.reaction_slots)}
        </div>
        <ul class="ind-do-jobs">${jobs}</ul></div>`;
    }).join('');

    const blocked = (d.unassigned || []).length;
    const wait = blocked
      ? `<div class="ind-do-blocked">${blocked} more job${blocked > 1 ? 's are' : ' is'} ready but every slot is busy — `
        + `they start as jobs finish.</div>` : '';
    const later = d.later_waves
      ? `<div class="pp-sub ind-later">Then ${d.later_waves} more round${d.later_waves > 1 ? 's' : ''} unlock as these finish · about ${_fmtHours(d.makespan_hours)} to the end.</div>` : '';

    el.innerHTML = doers.length
      ? `<h3 class="ind-do-title">Do this now</h3><div class="ind-do-grid">${cards}</div>${wait}${later}`
      : `<h3 class="ind-do-title">Nothing to start yet</h3>${wait}${later}`;
  } catch (e) { el.innerHTML = ''; }
}

// ── Per-order material sourcing ─────────────────────────────────────────────────────────────
// "What have I already got for this build, and what's still to buy." The answer comes mostly from
// the container the build is being gathered into — bind one and the list keeps itself up to date —
// with a hand-entered quantity for everything ESI can't see.
let _indSourcingOpen = null;      // order id, so the panel can be re-rendered after an edit

async function indOpenSourcing(orderId) {
  if (_indSourcingOpen === orderId) { indCloseSourcing(); return; }   // the chip toggles it
  _indSourcingOpen = orderId;
  _indSrcPasteMsg = '';          // last build's paste result is not this one's
  const el = document.getElementById('indSourcing');
  if (el) el.innerHTML = _indLoadingHtml('Working out what this build needs…');
  await indRenderSourcing();
}

function indCloseSourcing() {
  _indSourcingOpen = null;
  const el = document.getElementById('indSourcing');
  if (el) el.innerHTML = '';
}

let _indSourcingData = null;

async function indRenderSourcing() {
  const el = document.getElementById('indSourcing');
  if (!el || _indSourcingOpen === null) return;
  let d;
  try { d = await api(`/api/industry/orders/${_indSourcingOpen}/sourcing`); }
  catch (e) { el.innerHTML = `<p class="pp-warn">${_esc(e.message || 'Could not read this build.')}</p>`; return; }
  _indSourcingData = d;

  _indSourceSets = d.sets || [];
  const multi = _featureActive('industry_plan_sources');
  // One row per bound box, plus a blank row to add the next — grouped by station/structure so two
  // cans with the same name in different stations are distinguishable. Without the flag this is the
  // single dropdown it has always been.
  const bound = multi ? (d.source_keys || []) : [d.source_key || ''];
  const picker = (bound.length ? bound : ['']).map((k, i) => _indSourceRowHtml(
      d.sources || [], k, 'indBindSources()',
      {sets: multi, blank: i === 0 ? '— not tracked in a container —' : '— pick a box —',
       removable: multi && i > 0, onremove: 'indRemoveBoundSource(this)'})).join('')
    + (multi ? `<button type="button" class="ind-src-add" onclick="indAddBoundSource()" `
        + `title="This build is gathered from more than one box — a reaction can and a manufacturing can, say">+ another box</button>`
        + (bound.length > 1 ? ` <button type="button" class="ind-bp-btn ind-copy-sm" onclick="indSaveSourceSet()" `
            + `title="Save these boxes as a named set you can pick in one go next time">Save as set…</button>` : '')
      : '');

  // NO material table here. The shopping list below is already that table, and two lists of the same
  // materials — inevitably showing different quantities, since the queue's list nets off stock and
  // batches shared components across orders — is worse than one. What this panel knows that the
  // shopping list cannot is per-BUILD state: which box this one pulls from and how far along the
  // gathering is. So it shows that, and the shortfall stays one collapsed click away for the moment
  // you want to read it without scrolling.
  const short = (d.items || []).filter(i => !i.done);
  const missing = short.length
    ? `<details class="ind-src-missing"><summary>${short.length} still short</summary>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Short</th><th></th></tr></thead><tbody>`
      + short.map(i => `<tr><td>${_esc(i.name)}`
          + (i.sourced > 0 ? ` <span class="ind-src-meta">${Math.round(i.sourced).toLocaleString()} of `
              + `${Math.round(i.required).toLocaleString()}${i.noted > 0 ? ', noted by you' : ' in the box'}</span>` : '')
          + `</td><td class="ind-num">${Math.round(i.remaining).toLocaleString()}</td>`
          // Kept for correcting a single line after a paste; the paste is how the list gets filled.
          + `<td>${i.noted > 0 ? `<button class="ind-srcq-btn" onclick="indSetSourced(${i.type_id}, 0)"`
              + ` title="Forget what was noted for this one">clear</button>` : ''}</td></tr>`).join('')
      + `</tbody></table></details>`
    : `<p class="ind-src-help ind-src-allin">Everything this build needs is accounted for.</p>`;

  // Where each bound box actually is, spelled out under the picker — a closed dropdown shows only
  // the container's name, and the whole point is that its name is not enough to place it.
  const where = (multi && (d.bound || []).length)
    ? `<div class="ind-src-where ind-src-meta">Gathered into ` + d.bound.map(b =>
        `<span class="ind-src-box">${_esc(b.name)}${b.place ? ` <span class="ind-src-place-in">${_esc(b.place)}</span>` : ''}`
        + `${b.missing ? ' <span class="pp-warn">(no longer in your assets)</span>' : ''}</span>`).join(', ')
      + `</div>`
    : '';

  const t = d.totals || {};
  el.innerHTML = `<div class="ind-srcpanel">
      <div class="ind-srcpanel-hd">
        <span class="ind-srcpanel-title">Materials for ${d.quantity}× ${_esc(d.name)}`
        + (d.label ? ` <span class="ind-oc-for">${_esc(d.label)}</span>` : '') + `</span>
        <button class="ind-oc-del" onclick="indCloseSourcing()" title="Close">✕</button>
      </div>
      <div class="ind-srcpanel-bar">
        <label class="ind-src-meta">Pulling from
          <span id="indBoundSrcRows" class="ind-srcrows">${picker}</span></label>
        <span class="ind-shop-tot">${t.sourced} of ${t.materials} sourced`
        + (t.remaining_cost ? ` · ${fmtIsk(t.remaining_cost)} still to buy` : '') + `</span>
        <button class="ind-copy-btn ind-copy-sm" onclick="indCopyMissing()">Copy what's missing</button>
        <button class="ind-bp-btn" onclick="indOpenSourcePaste()">Paste what you've got</button>
      </div>
      ${where}
      <p class="ind-src-help">How far along the gathering is for this one build. Anything in the
        containers you pick counts automatically — rescan your assets after hauling and this moves on
        its own; for stock we can't see, paste it from the EVE client. ${multi
          ? `Those boxes are <b>this build's</b> stock: the plan below counts them and no others, so
             another build can only spend them if you pick them for it too.`
          : `Picking a container also lets the planner spend it, so the shopping list below stops
             asking you to buy what's already in there (untick it under Setup → stock if you'd
             rather it didn't).`}</p>
      <div id="indSrcPaste" class="ind-paste" style="display:none">
        <p class="ind-src-help">Select the materials in your hangar or container (Ctrl+A), copy
          (Ctrl+C) and paste below. This <b>replaces</b> what you've noted so far — it's a snapshot of
          what you have now — and anything this build doesn't need is ignored.</p>
        <textarea id="indSrcPasteText" rows="6" placeholder="Tritanium&#9;1 000 000&#10;Morphite&#9;2 400"></textarea>
        <div class="ind-src-actions">
          <button class="ind-primary-btn" onclick="indApplySourcePaste()">Apply</button>
          <button class="ind-bp-btn" onclick="indCloseSourcePaste()">Cancel</button>
          <span id="indSrcPasteMsg" class="ind-src-meta">${_esc(_indSrcPasteMsg)}</span>
        </div>
      </div>
      ${missing}
    </div>`;
}

// Kept outside the panel's HTML because applying a paste re-renders the whole thing — the result of
// what you just did must not be wiped by the redraw that shows it.
let _indSrcPasteMsg = '';

function indOpenSourcePaste() {
  const f = document.getElementById('indSrcPaste');
  if (!f) return;
  f.style.display = '';
  const t = document.getElementById('indSrcPasteText');
  if (t) t.focus();
}

function indCloseSourcePaste() {
  _indSrcPasteMsg = '';
  const f = document.getElementById('indSrcPaste');
  if (f) f.style.display = 'none';
}

async function indApplySourcePaste() {
  const text = (document.getElementById('indSrcPasteText') || {}).value || '';
  const msg = document.getElementById('indSrcPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  let d;
  try {
    d = await apiSend('POST', `/api/industry/orders/${_indSourcingOpen}/sourcing/paste`, { text });
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  const p = d.paste || {};
  // Say what was ignored as well as what matched: a paste that matched nothing is almost always the
  // wrong hangar, and silence would leave the user staring at an unchanged list.
  _indSrcPasteMsg = p.error === 'empty'
    ? "Couldn't read that paste."
    : `Matched ${p.matched} of this build's materials`
      + (p.ignored ? ` · ignored ${p.ignored} item${p.ignored === 1 ? '' : 's'} it doesn't need` : '')
      + ((p.unknown || []).length ? ` · ${p.unknown.length} name(s) not recognised` : '') + '.';
  await indRenderSourcing();
  if (p.matched) indOpenSourcePaste();     // leave it open so the outcome is visible next to the list
}

function indAddBoundSource() {
  const rows = document.getElementById('indBoundSrcRows');
  const btn = rows && rows.querySelector('.ind-src-add');
  if (!btn) return;
  btn.insertAdjacentHTML('beforebegin', _indSourceRowHtml(
    ((_indSourcingData || {}).sources) || [], '', 'indBindSources()',
    {sets: true, removable: true, onremove: 'indRemoveBoundSource(this)', blank: '— pick a box —'}));
}

async function indRemoveBoundSource(btn) {
  const row = btn && btn.closest('.ind-srcrow');
  if (row) row.remove();
  await indBindSources();
}

// Save the whole picked set, not one box. Sent as `source_keys`, which is also what tells the
// server this plan owns its stock from now on — so what the checklist measures and what the plan is
// allowed to count can never drift apart.
async function indBindSources() {
  const keys = _indExpandSets(_indPickedSources('indBoundSrcRows'),
                              _indSourceValues('indBoundSrcRows'));
  const body = _featureActive('industry_plan_sources')
    ? { source_keys: keys } : { source_key: keys[0] || '' };
  try { await apiSend('PATCH', `/api/industry/orders/${_indSourcingOpen}`, body); }
  catch (e) { toastError(e, 'Could not save'); return; }
  await indRenderSourcing();
  // The bound set is this build's stock, so the queue plan and the shopping list below are now out
  // of date by exactly the contents of those boxes.
  if (keys.length) indRefreshStatus();
}

async function indSaveSourceSet() {
  const keys = _indExpandSets(_indPickedSources('indBoundSrcRows'),
                              _indSourceValues('indBoundSrcRows'));
  if (!keys.length) return;
  const name = window.prompt('Name this set of containers — e.g. "Reaction stock"');
  if (!name || !name.trim()) return;
  try { await apiSend('POST', '/api/industry/source-sets', { name: name.trim(), keys }); }
  catch (e) { toastError(e, 'Could not save the set'); return; }
  await indRenderSourcing();
}

async function indSetSourced(typeId, qty) {
  try {
    await apiSend('POST', `/api/industry/orders/${_indSourcingOpen}/sourcing`,
                  { type_id: typeId, qty });
  } catch (e) { toastError(e, 'Could not save'); return; }
  indRenderSourcing();
}

// The shortfall in EVE Multibuy format — the actual point of the checklist is walking to the market
// with what's left, not admiring what you already have.
function indCopyMissing() {
  const items = ((_indSourcingData || {}).items || []).filter(i => i.remaining > 0);
  if (!items.length) return;
  _indCopyText(items.map(i => `${i.name}\t${Math.ceil(i.remaining)}`).join('\n'));
}

function _indCopyText(text) {
  // `window.event` explicitly, not the bare implicit global: it is deprecated, and reading it as a
  // free variable is exactly the shape of reference `no-undef` exists to catch (see
  // scripts/lint_js.mjs). Behaviour is identical — the callers are inline onclick handlers.
  const btn = (window.event && window.event.target) || null;
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = t; }, 1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => {});
  else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} document.body.removeChild(ta); }
}

async function indLoadRunning() {
  const el = document.getElementById('indRunning');
  if (!el) return;
  try {
    const d = await api('/api/industry/jobs');
    if (!d.jobs || !d.jobs.length) { el.innerHTML = ''; return; }
    const rows = d.jobs.map(j => {
      const ends = j.end_date ? new Date(j.end_date) : null;
      const left = ends ? Math.max(0, (ends - Date.now()) / 3.6e6) : null;
      return `<div class="ind-install-row"><span class="ind-tree-name">${_esc(j.name)}</span> `
        + `<span class="ind-tree-qty">×${j.runs}</span> `
        + `<span class="ind-run-char">${_esc(j.character_name)}</span> `
        + `<span class="ind-tree-cost">${left != null ? (left > 0 ? _fmtHours(left) + ' left' : 'ready') : _esc(j.status)}</span></div>`;
    }).join('');
    el.innerHTML = `<h3 class="ind-install-title">In progress — ${d.jobs.length} job(s)</h3>${rows}`;
  } catch (e) { el.innerHTML = ''; }
}


// wire the search input once the DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  const s = document.getElementById('indSearch');
  if (s) {
    s.addEventListener('input', indOnSearchInput);
    s.addEventListener('keydown', indOnSearchKey);
    s.addEventListener('blur', () => setTimeout(_indHideResults, 150));
  }
  const ps = document.getElementById('indPrioSpeed');
  // Re-run the current plan when the speed priority flips, so the effect is immediate.
  if (ps) ps.addEventListener('change', () => { if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan(); });
});

// ── Build rules: one renderer, two modes ──────────────────────────────────────────────────────
// Configuration used to live in eight strips down the plan card plus six write paths, and none of
// the strips read as a setting — the app's own author could not find the reaction control. This is
// the single surface over them (GET/POST /api/industry/build-setup), rendered in two modes:
//
//   account — Settings → Build rules. The standing rules.
//   order   — the per-order dialog. The SAME sections, each stating what it inherits, storing only
//             what this build actually changed.
//
// Two modes rather than two components on purpose: a global and its override that are rendered by
// different code are a pair that will drift, and the drift shows up as a plan that disagrees with
// the screen that configured it.
let _indRules = null;          // {account, available, order}
let _indRulesMode = 'account';
let _indRulesOrderId = null;

function _indRulesActive() { return _featureActive('industry_build_setup'); }

async function _indLoadRules(orderId) {
  const q = orderId != null ? `?order_id=${encodeURIComponent(orderId)}` : '';
  _indRules = await api('/api/industry/build-setup' + q);
  return _indRules;
}

// ── Section renderers. Each returns '' when its feature is off, so a closed section is absent
// rather than shown and refused by the POST.
function _indRuleRow(label, body, note) {
  return `<div class="ind-rule-row"><div class="ind-rule-lbl">${_esc(label)}</div>`
    + `<div class="ind-rule-ctl">${body}`
    + (note ? `<div class="ind-src-meta">${note}</div>` : '') + `</div></div>`;
}

// In order mode a field either INHERITS or is CHANGED, and it has to say which — an inherited value
// shown as a plain choice is how an override gets made by accident.
function _indInheritTag(field) {
  if (_indRulesMode !== 'order') return '';
  const on = ((_indRules.order || {}).overridden || []).includes(field);
  return on ? `<span class="ind-rule-tag ind-rule-changed">changed for this build</span>`
            : `<span class="ind-rule-tag">inherited</span>`;
}

function _indRulesFacility(a) {
  const f = a.facility || {};
  return _indRuleRow('Where you build',
    `<div class="ind-src-meta">Structure and rigs come from <b>Settings → Structures &amp; Markets</b>`
    + ` — a rig changes the materials and time of every job.</div>`
    + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indCloseRules();openSettingsModal('markets')">Open Structures &amp; Markets</button></div>`,
    f.facility_id ? `Currently: <b>${_esc(f.facility_id)}</b>`
      + (f.struct_material_pct != null ? ` · ME ${f.struct_material_pct}%` : '')
      + (f.struct_time_pct != null ? ` · TE ${f.struct_time_pct}%` : '')
      : 'No structure chosen — jobs are costed without a rig bonus.');
}

function _indRulesThreshold(a) {
  const t = a.threshold || {};
  const pct = t.marginal_pct == null ? 3 : t.marginal_pct;
  return _indRuleRow('Worth building?',
    `<label class="ind-opt-check"><input type="checkbox" id="ir-force" ${t.force_build ? 'checked' : ''}`
    + ` onchange="indRulesForceToggle(this)"> Build everything — ignore small savings and slow batches, and react your own materials</label>`
    + `<div class="ind-field ind-field-marg" id="ir-marg-wrap"${t.force_build ? ' style="opacity:.45"' : ''}>`
    + `<label for="ir-marg">Build only if it saves <b id="ir-marg-pct">${pct}%</b> of the build</label>`
    + `<input type="range" id="ir-marg" min="0" max="10" step="0.5" value="${pct}"`
    + ` ${t.force_build ? 'disabled' : ''} oninput="document.getElementById('ir-marg-pct').textContent=this.value+'%'"></div>`
    + `<label class="ind-opt-check"><input type="checkbox" id="ir-speed" ${t.prioritize_speed ? 'checked' : ''}>`
    + ` Prioritize speed — buy slow bulk materials to finish sooner</label>`,
    'At 0% nothing is bought for being a small saving — there is no hidden floor under it.');
}

function _indRulesReactions(a) {
  if (!_indRules.available.reactions) return '';
  const p = (a.reactions || {}).policy || {};
  const cats = (a.reactions || {}).categories || [];
  const bought = new Set(p.buy_categories || []);
  if (_indRulesMode === 'order') {
    const on = ((_indRules.order || {}).values || {}).build_reactions;
    return _indRuleRow('Reactions',
      `<label class="ind-opt-check"><input type="checkbox" id="ir-rx-own" ${on ? 'checked' : ''}>`
      + ` React this build's own materials, whatever the account rule says</label> ${_indInheritTag('build_reactions')}`,
      `Account rule: ${bought.size ? _esc([...bought].join(', ')) + ' bought in' : 'all reactions made here'}.`);
  }
  return _indRuleRow('Reactions',
    `<label class="ind-opt-check"><input type="checkbox" id="ir-rx-run" ${p.build_reactions !== false ? 'checked' : ''}`
    + ` onchange="indRulesRxToggle(this)"> This account runs its own reactions</label>`
    + `<div id="ir-rx-cats" class="ind-rule-checks"${p.build_reactions === false ? ' style="display:none"' : ''}>`
    + cats.map(c => `<label class="ind-opt-check" title="${_esc(c.description || '')}">`
        + `<input type="checkbox" class="ir-rx-cat" data-key="${_esc(c.key)}" ${bought.has(c.key) ? 'checked' : ''}>`
        + ` buy ${_esc(c.label)} instead</label>`).join('') + `</div>`,
    (a.reactions || {}).defaulted
      ? '<b>Still on the default</b> — composites &amp; intermediates are bought in. That is a default, not a choice you made.'
      : '');
}

// One list, one rule per component. Two lists ("never build" / "build anyway") were one list
// wearing two hats: a component is always-buy, always-build, or left to the cost engine, and it
// cannot be two of those. A row per type with a two-way toggle makes the exclusion visible and
// makes changing your mind one click instead of a delete here and an add there.
function _indRulesComponents(a) {
  if (!_indRules.available.components || _indRulesMode === 'order') return '';
  const items = (a.components || {}).items || [];
  const rows = items.length
    ? `<div class="ind-rule-list">` + items.map(it =>
        `<div class="ind-rule-item"><span class="ind-rule-item-name">${_esc(it.name)}</span>`
        + `<span class="ind-rule-seg">`
        + `<button class="ind-rule-segbtn${it.rule === 'build' ? ' on' : ''}"`
        + ` onclick="indRulesSetComponent(${it.type_id}, 'build')"`
        + ` title="Build this whatever the shortcuts say">always build</button>`
        + `<button class="ind-rule-segbtn${it.rule === 'buy' ? ' on' : ''}"`
        + ` onclick="indRulesSetComponent(${it.type_id}, 'buy')"`
        + ` title="Always buy this, whatever the cost engine works out">always buy</button>`
        + `</span>`
        + `<button class="ind-link-btn" onclick="indRulesSetComponent(${it.type_id}, null)"`
        + ` title="Remove the rule — let the cost engine decide again">×</button></div>`).join('')
      + `</div>`
    : `<div class="ind-src-meta">No overrules — the cost engine decides every component.</div>`;
  return _indRuleRow('Components',
    rows
    + `<div class="ind-rule-add">`
    + `<input type="text" id="ir-comp-q" placeholder="Add a component…" autocomplete="off"`
    + ` oninput="indRulesCompSearch()" onkeydown="if(event.key==='Escape')indRulesCompClear()">`
    + `<div class="ind-rule-sugg" id="ir-comp-sugg"></div></div>`,
    'A rule here applies to every build. A single order can still overrule it for itself.');
}

let _indCompSearchT = null;
function indRulesCompClear() {
  const el = document.getElementById('ir-comp-sugg');
  if (el) el.innerHTML = '';
}

function indRulesCompSearch() {
  clearTimeout(_indCompSearchT);
  _indCompSearchT = setTimeout(async () => {
    const q = (document.getElementById('ir-comp-q') || {}).value || '';
    const box = document.getElementById('ir-comp-sugg');
    if (!box) return;
    if (q.trim().length < 2) { box.innerHTML = ''; return; }
    let hits = [];
    try { hits = (await api('/api/industry/search?q=' + encodeURIComponent(q.trim()))) || []; }
    catch (e) { box.innerHTML = ''; return; }
    const have = new Set(((_indRules.account.components || {}).items || []).map(i => i.type_id));
    box.innerHTML = (hits.items || hits).slice(0, 8).filter(h => !have.has(h.type_id)).map(h =>
      `<button class="ind-rule-sugg-hit" onclick="indRulesAddComponent(${h.type_id})">`
      + `${_esc(h.name)}</button>`).join('') || `<div class="ind-src-meta">No match.</div>`;
  }, 220);
}

function indRulesAddComponent(typeId) {
  const q = document.getElementById('ir-comp-q');
  if (q) q.value = '';
  indRulesCompClear();
  // New rules default to "always buy": that is what the old blacklist meant, so an existing habit
  // keeps working, and it is the safer of the two — buying something you could have built costs
  // ISK, while building something you cannot costs a plan that will not run.
  return indRulesSetComponent(typeId, 'buy');
}

// `rule` null removes the overrule entirely. Written straight through rather than collected on
// Save: this list is edited by clicking, and a click that looks like it took effect has to have.
async function indRulesSetComponent(typeId, rule) {
  const items = ((_indRules.account.components || {}).items || [])
    .filter(i => i.type_id !== typeId);
  if (rule) items.push({ type_id: typeId, rule });
  try {
    const res = await apiSend('POST', '/api/industry/build-setup', { components: { items } });
    _indRules.account = res.account;
    _indRulesRepaintOpen();
  } catch (e) { toastError(e, 'Could not save'); }
}

// Repaint whichever surface is showing — the Settings pane or the order dialog.
function _indRulesRepaintOpen() {
  const pane = document.getElementById('settingsSecBuildrulesBody');
  const modal = document.getElementById('indRulesModal');
  if (modal && modal.style.display !== 'none') { _indRulesPaint(); return; }
  if (pane) {
    pane.innerHTML = _indRulesSectionsHtml()
      + `<div class="ind-src-actions"><button class="ind-bp-btn ind-bp-btn-primary" onclick="indSaveRules()">Save build rules</button>`
      + `<span class="ind-src-meta" id="ir-saved"></span></div>`;
  }
}

function _indRulesJobLength(a) {
  if (!_indRules.available.job_length || _indRulesMode === 'order') return '';
  const d = (a.job_length || {}).max_reaction_job_days;
  return _indRuleRow('Longest reaction job',
    `<input type="number" id="ir-joblen" min="0" step="0.5" style="width:80px" value="${d == null ? '' : d}"> days`,
    'Blank or 0 = no ceiling. A reaction has no per-job run cap, so a big batch will otherwise sit in one slot for weeks.');
}

function _indRulesSources(a) {
  if (!_indRules.available.sources) return '';
  const srcs = (a.sources || {}).sources || [];
  const chosen = _indRulesMode === 'order'
    ? new Set(((_indRules.order || {}).values || {}).source_keys || [])
    : new Set(srcs.filter(s => s.enabled).map(s => s.key));
  if (!srcs.length) {
    return _indRuleRow('Materials from',
      `<div class="ind-src-meta">No boxes scanned yet — set them up under <b>Settings → Blueprints &amp; formulas</b>.</div>`);
  }
  return _indRuleRow('Materials from',
    `<div class="ind-rule-checks">` + srcs.map(s => `<label class="ind-opt-check"><input type="checkbox" class="ir-src" data-key="${_esc(s.key)}"`
      + ` ${chosen.has(s.key) ? 'checked' : ''}> ${_esc(s.name)}`
      + (s.place ? ` <span class="ind-src-meta">${_esc(s.place)}</span>` : '')
      + (s.corp ? ` <span class="ind-src-meta">corp</span>` : '') + `</label>`).join('')
      + `</div>`
      + (_indRulesMode === 'order' ? ' ' + _indInheritTag('source_keys') : ''),
    'Stock in a ticked box is spent before anything is bought.');
}

function _indRulesMargin(a) {
  const m = _indRulesMode === 'order'
    ? ((_indRules.order || {}).values || {}).margin_pct
    : (a.margin || {}).margin_pct;
  return _indRuleRow('Customer margin',
    `<input type="number" id="ir-margin" min="0" max="100" step="0.5" style="width:80px"`
    + ` value="${m == null ? '' : m}"> % over net cost `
    + (_indRulesMode === 'order' ? _indInheritTag('margin_pct') : ''),
    'Blank = the account default. Priced off NET cost, so over-production you keep is not billed twice.');
}

function _indRulesSectionsHtml() {
  const a = _indRules.account;
  return [_indRulesFacility(a), _indRulesThreshold(a), _indRulesReactions(a),
          _indRulesComponents(a), _indRulesJobLength(a), _indRulesSources(a),
          _indRulesMargin(a)].filter(Boolean).join('');
}

function indRulesForceToggle(cb) {
  const w = document.getElementById('ir-marg-wrap');
  const r = document.getElementById('ir-marg');
  if (r) r.disabled = cb.checked;
  if (w) w.style.opacity = cb.checked ? '.45' : '';
}

function indRulesRxToggle(cb) {
  const c = document.getElementById('ir-rx-cats');
  if (c) c.style.display = cb.checked ? '' : 'none';
}

// ── Account mode: the Settings section ────────────────────────────────────────────────────────
async function _loadBuildRulesSettings() {
  const body = document.getElementById('settingsSecBuildrulesBody');
  if (!body) return;
  _indRulesMode = 'account';
  _indRulesOrderId = null;
  try { await _indLoadRules(null); }
  catch (e) { body.innerHTML = `<div class="ind-src-meta">Could not load build rules.</div>`; return; }
  body.innerHTML = _indRulesSectionsHtml()
    + `<div class="ind-src-actions"><button class="ind-bp-btn ind-bp-btn-primary" onclick="indSaveRules()">Save build rules</button>`
    + `<span class="ind-src-meta" id="ir-saved"></span></div>`;
}

// ── Order mode: the dialog ────────────────────────────────────────────────────────────────────
async function indOpenRules(orderId, label) {
  const m = document.getElementById('indRulesModal');
  if (!m) return;
  _indRulesMode = orderId == null ? 'account' : 'order';
  _indRulesOrderId = orderId;
  m.style.display = 'flex';
  document.getElementById('indRulesTitle').firstChild.textContent =
    orderId == null ? 'Build setup ' : `Build setup — ${label || ('order #' + orderId)} `;
  document.getElementById('indRulesBody').innerHTML = `<div class="ind-loading">Loading…</div>`;
  try { await _indLoadRules(orderId); }
  catch (e) { document.getElementById('indRulesBody').innerHTML =
    `<div class="ind-src-meta">Could not load build rules.</div>`; return; }
  _indRulesPaint();
}

function _indRulesPaint() {
  const hint = document.getElementById('indRulesHint');
  if (hint) {
    hint.innerHTML = _indRulesMode === 'order'
      ? `This build inherits your account rules. Only what you change here is stored on the order — `
        + `everything else follows <b>Settings → Build rules</b> and keeps following it when you change it there.`
      : `Your standing rules. Every build follows these unless it overrides one.`;
  }
  const body = document.getElementById('indRulesBody');
  if (body) body.innerHTML = _indRulesSectionsHtml();
}

function indCloseRules() {
  const m = document.getElementById('indRulesModal');
  if (m) m.style.display = 'none';
}

// ── Save. Account mode writes the consolidated patch; order mode PATCHes the order, because an
// override belongs to the order row and must not touch the account's standing rules.
function _indRulesReadForm() {
  const val = id => { const e = document.getElementById(id); return e ? e.value : null; };
  const on = id => { const e = document.getElementById(id); return e ? e.checked : null; };
  const cats = [...document.querySelectorAll('.ir-rx-cat')].filter(c => c.checked)
    .map(c => c.dataset.key);
  const srcs = [...document.querySelectorAll('.ir-src')].filter(c => c.checked)
    .map(c => c.dataset.key);
  return { val, on, cats, srcs };
}

async function indSaveRules() {
  const f = _indRulesReadForm();
  try {
    if (_indRulesMode === 'order') {
      const body = {};
      const rx = f.on('ir-rx-own');
      if (rx !== null) body.build_reactions = rx;
      const mg = f.val('ir-margin');
      body.margin_pct = (mg === '' || mg === null) ? null : parseFloat(mg);
      if (_indRules.available.sources) body.source_keys = f.srcs;
      await apiSend('PATCH', `/api/industry/orders/${_indRulesOrderId}`, body);
      toast('Saved for this build', 'success');
      indCloseRules();
      if (typeof indRefreshStatus === 'function') indRefreshStatus();
      return;
    }
    const patch = {};
    patch.threshold = { marginal_pct: parseFloat(f.val('ir-marg')),
                        force_build: f.on('ir-force'), prioritize_speed: f.on('ir-speed') };
    const mg = f.val('ir-margin');
    patch.margin = { margin_pct: (mg === '' || mg === null) ? null : parseFloat(mg) };
    if (_indRules.available.reactions) {
      patch.reactions = { build_reactions: f.on('ir-rx-run'), buy_categories: f.cats };
    }
    if (_indRules.available.job_length) {
      const d = f.val('ir-joblen');
      patch.job_length = { max_reaction_job_days: (d === '' || d === null) ? null : parseFloat(d) };
    }
    if (_indRules.available.sources && document.querySelector('.ir-src')) {
      patch.sources = { keys: f.srcs, enabled: true };
    }
    const res = await apiSend('POST', '/api/industry/build-setup', patch);
    _indRules.account = res.account;
    const saved = document.getElementById('ir-saved');
    if (saved) { saved.textContent = 'Saved.'; setTimeout(() => { saved.textContent = ''; }, 2500); }
    toast('Build rules saved', 'success');
    indCloseRules();
    // The rules that were just changed are the ones the plan is priced against, so re-run it.
    if (_indPicked && (document.getElementById('indResult') || {}).innerHTML) indRunPlan();
  } catch (e) { toastError(e, 'Could not save'); }
}

// ── Read-only summaries, shown in place of the old strips once Build rules is on ───────────────
// Each states what is in force and what it cost THIS build, and links into the section that set it.
// The cost half is not decoration: a rule that quietly removes a whole sub-chain from the shopping
// list — as the reaction policy did, taking a Revelation's carbon fibre from ~26k to 6.4k — has to
// be legible where the number it changed is being read.
function _indRuleSummary(label, state, cost, onclick) {
  return `<div class="ind-forced-bar ind-rule-summary">`
    + `<span class="ind-forced-lbl">${_esc(label)}:</span> <span>${state}</span>`
    + (cost ? ` <span class="ind-rxp-delta">${cost}</span>` : '')
    + ` <button class="ind-link-btn" onclick="${onclick}">change</button></div>`;
}

function _indRxSummaryBar(d) {
  const pol = _indRxPolicy.policy || {};
  const cats = _indRxPolicy.categories || [];
  const bought = new Set(pol.buy_categories || []);
  const some = cats.filter(c => bought.has(c.key));
  const state = pol.build_reactions === false ? 'bought in, not made here'
    : some.length ? `${_esc(some.map(c => c.label.toLowerCase()).join(', '))} bought in`
    : 'made here';
  const rp = (d && d.reaction_policy) || null;
  let cost = '';
  if (rp && rp.isk) {
    const n = (rp.items || []).length;
    const what = `${n} reaction output${n === 1 ? '' : 's'}`;
    cost = rp.overridden ? `reacting ${what} here saves ${fmtIsk(rp.isk)}`
      : rp.isk > 0 ? `buying ${what} in adds ${fmtIsk(rp.isk)} to this build`
      : `buying ${what} in saves ${fmtIsk(-rp.isk)}`;
  }
  return _indRuleSummary('Reactions', state, cost, "openSettingsModal('buildrules')");
}

// Does a mark FINISH a step? Only a completing mark needs the server's view of what became
// installable — see _indPostDone. A partial ("3 of 12 runs"), a "running" mark and an un-mark all
// leave the stage gating exactly where it was, so they keep the fast local repaint.
function _indMarkCompletesStep(typeId, runs, state) {
  if (state !== 'done') return false;                 // running / clearing never unblocks anything
  const t = (_indProgTypeMap() || {})[typeId];
  if (!t || !t.required_runs) return true;            // nothing to reason with — take the safe path
  if (runs === 0) return false;                       // an un-mark
  const marked = runs === null || runs === undefined ? t.required_runs : runs;
  const observed = t.observed_runs != null ? t.observed_runs : 0;
  return Math.max(observed, marked) >= t.required_runs;
}

// Mark every step of a stage done in one action. A stage completes as a unit — nothing in the next
// one can start until all of it has landed — so ticking it out step by step was busywork whose
// intermediate states were all wrong. One call, one re-plan, and the checklist moves on.
async function indMarkStageDone(stageIdx) {
  const ids = _indStageTypeIds(stageIdx);
  if (!ids.length) return;
  const model = _indStageModelForPlan();
  const label = ((model.cols || [])[stageIdx] || {}).label || 'this stage';
  if (!await ppConfirm(`Mark ${label} done? ${ids.length} step${ids.length === 1 ? '' : 's'} `
        + `will be marked finished — click any of them again to correct it.`,
        { okLabel: 'Mark stage done', danger: false })) return;
  try {
    const fresh = await apiSend('POST', '/api/industry/progress/done',
                                { type_ids: ids, state: 'done' });
    _indProgress = (fresh && !fresh.empty) ? fresh : _indProgress;
  } catch (e) { toastError(e, 'Could not save'); }
  // Always the full path: the whole point is that the next stage becomes startable, and only a
  // re-planned `install` block knows that.
  indRefreshStatus();
}

// The stage model for the plan currently on screen, derived the same way the pipeline derives it.
function _indStageModelForPlan() {
  const d = _indLastPlan;
  if (!d) return { cols: [], stageOf: {} };
  const boughtIds = new Set((d.shopping_list || []).map(s => s.type_id));
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, boughtIds)
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  return _indStageModel(tiersData);
}

// The BUILT steps a stage owns. `cols` is indexed by stage, and each column already carries the
// builds that belong to it — the same list the pipeline column renders, so the button can never
// mark a different set of steps from the ones shown under it.
function _indStageTypeIds(stageIdx) {
  const col = (_indStageModelForPlan().cols || [])[stageIdx];
  if (!col) return [];
  const need = {};
  ((_indProgress && _indProgress.types) || []).forEach(t => { need[t.type_id] = t.required_runs; });
  return (col.builds || []).map(b => b.type_id).filter(t => need[t]);
}

// Show the way in to the standing rules on the plan form itself. A page whose every number is
// shaped by rules it never mentions is how those rules went unfound in the first place — so this
// is a control, not a sentence in a hint nobody reads.
function indApplyBuildRulesGate() {
  const b = document.getElementById('indBuildRulesBtn');
  if (b) b.style.display = _indRulesActive() ? '' : 'none';
}
