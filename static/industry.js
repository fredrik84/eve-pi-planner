// ── Industry / Manufacturing planner — the tab shell. ───────────────────────────────────────
// Tab open and the load order, the planner modal, where a container IS (the source pickers
// shared by the modal and Settings), the tab's own settings, and the build-page status card
// with its last-plan cache. The rest of the tab is in industry-*.js, split along
// docs/industry-workflow.md's steps; every name here is a plain global, as they all are.

// ── Industry / Manufacturing planner tab ────────────────────────────────────────────────────
// Talks to /api/industry/* (see app/industry/). Read-only make-or-buy: pick a product → cost +
// shopping list + build tree; queue products → plan them all together across your job slots.
// Reuses the shared formatters from utils.js (fmtIsk, _fmtHours, _esc).

let _indPicked = null;        // {type_id, name} currently selected in the picker
let _indSearchTimer = null;

async function onIndustryTabOpen() {
  // Four sections of one build, as tabs (TODO §41, docs/page-layout-2026-08.md) — restores
  // whichever was last read, same convenience a URL gives a real page, without one: this is a
  // section of the Manufacturing page, not a separate address.
  ppRestoreTab('ind', 'status');
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
  _indRestoringSettings = false;
  _indRestoreControls();
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
  _indRestoreControls();
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
  return _indSourceValues(containerId)
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

// What a rows container is bound to: the boxes picked in it, with any saved set expanded. Every
// reader of a picker goes through this one function — the plan preview, the queued order and a
// saved set are three views of the same choice, and a picker read three ways is three chances for
// them to disagree about which stock a build may count on.
function _indSourceKeys(containerId) {
  return _indExpandSets(_indPickedSources(containerId), _indSourceValues(containerId));
}

// Which box this build's materials come from, chosen while planning it — because "which container
// is this build's" is decided at the same moment as what to build, not afterwards. A director picks
// a corp hangar or container straight off the scanned list; everyone else pastes what they hold,
// which is recorded against the order rather than added to the planner's global stock (stock you
// can't actually draw from is the one error that makes the planner build too little).
let _indPlanSources = [];      // the scanned source list, as the plan modal last saw it
let _indLastOutputKey = '';    // the output box the last build used — same reasoning as _indLastSourceKey

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
  _indRenderOutputPicker();
  indOnPlanSourceChange();
}

// The output box. One row, never a set: a job delivers to exactly ONE container, so offering a
// multi-pick here would be offering something EVE cannot do. Gated with the per-plan sources
// surface because that is the model this belongs to — each build owning its boxes.
function _indRenderOutputPicker() {
  const field = document.getElementById('indPlanOutField');
  const rows = document.getElementById('indPlanOutRows');
  if (!field || !rows) return;
  if (!_featureActive('industry_plan_sources')) { field.style.display = 'none'; return; }
  const remembered = _indLastOutputKey && _indPlanSources.some(x => x.key === _indLastOutputKey)
    ? _indLastOutputKey : '';
  rows.innerHTML = _indSourceRowHtml(_indPlanSources, remembered, '_indOnPlanOutputChange()',
    {id: 'indPlanOut', blank: '— same box as the materials —'});
  field.style.display = '';
  _indOnPlanOutputChange();
}

// Says which box the output will actually go to and WHY — stated, or inherited from the materials
// box. An inherited answer that reads like a stated one is the thing this note exists to prevent.
function _indOnPlanOutputChange() {
  const hint = document.getElementById('indPlanOutHint');
  if (!hint) return;
  const chosen = (document.getElementById('indPlanOut') || {}).value || '';
  if (chosen) {
    hint.textContent = 'Finished items are tracked as belonging to this box.';
    return;
  }
  const inputs = _indPlanSourceKeys();
  if (inputs.length) {
    const src = _indPlanSources.find(x => x.key === inputs[0]);
    hint.textContent = 'Follows the materials box' + (src ? ` — ${src.label || src.name || inputs[0]}` : '') + '.';
  } else {
    hint.textContent = 'No box bound, so output lands wherever you install the job.';
  }
}

function _indPlanOutputKey() {
  return (document.getElementById('indPlanOut') || {}).value || '';
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

// The boxes the plan modal currently has picked. Used for BOTH the preview and the queued order, so
// the two cannot be costed against different stock — a preview that promises a shopping list the
// queued build then contradicts is the bug reading the picker in one place avoids.
function _indPlanSourceKeys() {
  return _indSourceKeys('indPlanSrcRows');
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
  // Matches THIS file's tag, and only because no sibling filename ends in `industry.js` — the tab
  // is ten scripts now. A new one named `…industry.js` would silently take over this lookup; every
  // one of them carries the same `?v=`, so the value would still be right, but the coupling is not
  // obvious from here.
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
  if (!d) return;
  const local = !!(opts && opts.local);
  // The order it was showing may have been delivered/cancelled since — fall back rather than
  // spin on a fetch for an id that no longer exists.
  if (_indViewOrderId && !(_indOrders || []).some(o => o.id === _indViewOrderId)) _indViewOrderId = null;
  // The sourcing panel is nested inside the (combined) headline and a hand mark can't change it,
  // so a local repaint carries its current markup across instead of paying for another round trip
  // (it plans an order from scratch) to redraw the same thing. Only meaningful for the combined
  // view — while viewing one order the fetch below is async anyway, so there is nothing to "keep"
  // synchronously the way there is for a plain repaint.
  const keepSourcing = (local && !_indViewOrderId)
    ? ((document.getElementById('indSourcing') || {}).innerHTML || '') : null;
  if (_indViewOrderId) {
    // Sticky across this repaint (TODO §41 follow-up, 2026-08-19) — a background refresh
    // (mark-done, add-to-queue) must not silently snap the reader back to the combined view.
    _indSetViewOrder(_indViewOrderId);
  } else {
    // Status/Blueprints & materials/Build Pipeline are three tabs of the same build (TODO §41) —
    // painted here alongside each other rather than lazily on tab-switch, since all three are a
    // pure-JS pass over `d`, not a fetch: nothing is saved by skipping the ones not currently
    // showing, and painting all of them up front means switching tabs never shows stale content.
    _indPaintStatusHeadline(d);
    _indPaintBlueprints(d);
    _indPaintPipeline(d);
    if (local) {
      const src = document.getElementById('indSourcing');
      if (src) src.innerHTML = keepSourcing;
      return;
    }
  }
  indLoadRunning();          // what's already cooking, in its own tab — unaffected by order-view
  if (_indSourcingOpen !== null) indRenderSourcing();   // the card was just repainted under it
}

function _indPaintStatusHeadline(d) {
  const body = document.getElementById('indStatusBody');
  if (!body || !d) return;
  body.innerHTML = _indOrderViewSelector('status') + _indStatusHeadline(d, _indViewOrderId)
    + `<div id="indInstall" class="ind-install"></div>`;
  // "Do this now" used to come back blank while viewing one order (2026-08-19) — only the
  // whole-queue endpoint (queue-plan) called install_block, so the single-order endpoint
  // (POST /api/industry/plan, see _indSetViewOrder) never had a slot checklist to show. It now
  // computes the same thing for that one build, so both views carry `install` the same way.
  indRenderInstall(d.install);
  indMargCutLabel();             // the bulk control's live readout starts filled in, not blank
}

function _indPaintBlueprints(d) {
  const el = document.getElementById('indBlueprintsBody');
  if (!el || !d) return;
  el.innerHTML = _indOrderViewSelector('blueprints') + _indRenderPlanBody(d);
  const badge = document.getElementById('indBlueprintsTabBadge');
  if (badge) {
    const n = _indBlueprintBadgeCount(d);
    badge.style.display = n > 0 ? '' : 'none';
    badge.textContent = n > 0 ? String(n) : '';
  }
}

function _indPaintPipeline(d) {
  const el = document.getElementById('indPipelineBody');
  if (!el || !d) return;
  el.innerHTML = _indOrderViewSelector('pipeline') + _indRenderPipelineBody(d);
}

// ── Viewing one order alone ─────────────────────────────────────────────────────────────────
// The queue plan is one combined batch — two orders wanting the same product share ONE build, so
// there is no way to isolate "just this order's materials" out of it after the fact. Reported live
// (2026-08-18): with several orders queued, Status/Blueprints/Pipeline all read as one intermixed
// plan, which is the right default for "just start building" but wrong for "let me look at ONE in
// particular." Answered by re-planning that ONE order alone through the existing single-product
// endpoint (`POST /api/industry/plan` — the same one the picker/preview modal already uses), with
// that order's own saved overrides, rather than by requiring `industry_per_order_plans` (which
// still reports combined materials/notices even when it schedules per order — see
// `plan_queue_per_order`'s own docstring) or building a second planning endpoint. NOT on Currently
// running — that tab is what ESI reports installed, which has no "per order" meaning of its own.
let _indViewOrderId = null;   // null = the combined queue view

// A type-to-filter picker, not a bare <select> (unstyled and out of place) and not a row of order
// CARDS (the preferred look, but it doesn't scale — ten queued orders would overflow and make the
// one you want hard to find). Reuses the app's existing product-search look
// (.ind-search-wrap/.ind-search-results/.ind-search-row, industry-plan.js's picker) so it matches
// and stays findable at any queue size. `scope` ('status'/'blueprints'/'pipeline') keeps the three
// copies — one per tab that offers this — from colliding on element ids.
//
// The input's OWN value doubles as the display label for whatever is currently selected (so it
// reads "Revelation" rather than sitting blank once picked) — reported live (2026-08-19): that
// same leftover text was also being read as the search query on the NEXT focus, so re-opening the
// list after picking Revelation filtered everything down to rows matching "revelation", hiding
// "All orders (combined)" and every other order. `onfocus` now selects the text (so typing
// replaces it, the normal combobox affordance) and explicitly filters on an EMPTY query — the
// list always opens full, never pre-filtered by whatever it happened to be showing before.
function _indOrderViewSelector(scope) {
  const orders = _indOrders || [];
  if (orders.length < 2) return '';   // nothing to pick between with 0 or 1 orders queued
  const current = _indViewOrderId ? orders.find(o => o.id === _indViewOrderId) : null;
  const label = current ? current.name : '';
  return `<div class="ind-order-view">
    <div class="ind-search-wrap">
      <input type="text" id="indOrderView-${scope}-input" class="ind-order-view-input" autocomplete="off"
             placeholder="All orders (combined)" value="${_esc(label)}"
             oninput="_indOrderViewFilter('${scope}', this.value)"
             onfocus="this.select(); _indOrderViewFilter('${scope}', '')"
             onblur="setTimeout(() => _indOrderViewHideList('${scope}'), 150)">
      <div class="ind-search-results" id="indOrderView-${scope}-results" style="display:none"></div>
    </div>
  </div>`;
}

// `_indOrders` already arrives in queue order (server: `ORDER BY priority ASC, created_at ASC`),
// so the array position IS the position in line — no separate rank lookup needed, unlike the chip
// row's `#N` (which reads the PLAN's computed rank, `T.rank`, since a chip is about when that
// product actually finishes, not just where its order sits in the list).
//
// Reported live (2026-08-19): two orders of the same product (e.g. two Revelations) are otherwise
// indistinguishable in the list — `label` exists on the order for exactly this ("a customer name,
// a contract, a fleet", see ensure_industry_orders_table's own comment), so it's surfaced here, and
// the position number gives a second, always-present way to tell them apart even with no label set.
// Both are searchable, same as the name.
function _indOrderViewRowsHtml(scope, q) {
  const orders = _indOrders || [];
  const needle = (q || '').trim().toLowerCase();
  const matchAll = !needle || 'all orders (combined)'.includes(needle);
  const rows = orders
    .map((o, i) => ({ o, pos: i + 1 }))
    .filter(({ o }) => !needle || o.name.toLowerCase().includes(needle)
      || (o.label || '').toLowerCase().includes(needle));
  if (!matchAll && !rows.length) return '<div class="ind-search-empty">No matching order</div>';
  return (matchAll ? `<div class="ind-search-row" onclick="_indOrderViewPick('${scope}', null, '')">All orders (combined)</div>` : '')
    + rows.map(({ o, pos }) => `<div class="ind-search-row" onclick="_indOrderViewPick('${scope}', ${o.id}, '${_esc(o.name).replace(/'/g, "\\'")}')">`
        + `<span class="pp-card-hint">#${pos}</span> ${_esc(o.name)}`
        + `${o.quantity > 1 ? ` <span class="pp-card-hint">×${o.quantity.toLocaleString()}</span>` : ''}`
        + `${o.label ? ` <span class="pp-card-hint">— ${_esc(o.label)}</span>` : ''}</div>`).join('');
}

function _indOrderViewFilter(scope, q) {
  const box = document.getElementById(`indOrderView-${scope}-results`);
  if (!box) return;
  box.innerHTML = _indOrderViewRowsHtml(scope, q);
  box.style.display = '';
}

function _indOrderViewHideList(scope) {
  const box = document.getElementById(`indOrderView-${scope}-results`);
  if (box) box.style.display = 'none';
}

function _indOrderViewPick(scope, orderId, name) {
  const input = document.getElementById(`indOrderView-${scope}-input`);
  if (input) input.value = name || '';
  _indOrderViewHideList(scope);
  _indSetViewOrder(orderId || null);
}

async function _indSetViewOrder(orderId) {
  _indViewOrderId = orderId || null;
  if (!_indViewOrderId) {
    // Back to the combined plan already in hand — no fetch, same instant repaint as any other tab.
    if (_indLastPlan) { _indPaintStatusHeadline(_indLastPlan); _indPaintBlueprints(_indLastPlan); _indPaintPipeline(_indLastPlan); }
    return;
  }
  const o = (_indOrders || []).find(x => x.id === _indViewOrderId);
  if (!o) return;
  const statusEl = document.getElementById('indStatusBody');
  const bpEl = document.getElementById('indBlueprintsBody');
  const pipeEl = document.getElementById('indPipelineBody');
  const loading = scope => _indOrderViewSelector(scope) + _indLoadingHtml('Planning this order…', 'Same cost engine, just this one alone.');
  if (statusEl) statusEl.innerHTML = loading('status');
  if (bpEl) bpEl.innerHTML = loading('blueprints');
  if (pipeEl) pipeEl.innerHTML = loading('pipeline');
  let d;
  try {
    d = await apiSend('POST', '/api/industry/plan', {
      type_id: o.product_type_id, quantity: o.quantity,
      prioritize_speed: _indPrioSpeed(), marginal_pct: _indMarginalPct(),
      force_build_ids: o.force_build_ids || [], me_te_overrides: o.me_te_overrides || {},
      margin_pct: o.margin_pct != null ? o.margin_pct : _indMarginPct(),
      build_reactions_anyway: !!o.build_reactions,
      // A plan owns its own sources so the materials shown match what the queued build would
      // actually count — same rule the preview modal follows (industry-plan.js:113).
      ...(_featureActive('industry_plan_sources') && (o.source_keys || []).length
            ? { source_keys: o.source_keys } : {}),
      ..._indFacilityBonus(),
    });
  } catch (e) {
    const err = `<div class="pp-empty">${_esc(e.message || 'Plan failed')}</div>`;
    if (statusEl) statusEl.innerHTML = _indOrderViewSelector('status') + err;
    if (bpEl) bpEl.innerHTML = _indOrderViewSelector('blueprints') + err;
    if (pipeEl) pipeEl.innerHTML = '';
    return;
  }
  // Only paint if the reader hasn't already switched to a different order (or back to combined)
  // while this was in flight.
  if (_indViewOrderId !== o.id) return;
  _indPaintStatusHeadline(d);
  _indPaintBlueprints(d);
  _indPaintPipeline(d);
}

// One order, as the chip that names it in the queue line: position, who it is for, how far along,
// when it lands, every override set on it, and the buttons that act on it. Lifted out of the
// headline because the headline is four tiles and a row of these — all the reading was in here.
// `byOrder` is the live progress keyed by order id, `tgt` the plan's targets keyed by type.
function _indOrderChipHtml(o, byOrder, tgt) {
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
}

// The queue in the order the SCHEDULER put it in, which is the order everything showing position
// must use: the status chips number the line, and the Reorder dialog opens on it. Two sorts is two
// answers to "who is first", and the one the user drags is then not the one they were shown.
// An order whose product isn't among the plan's targets sorts last, ties break on id.
function _indOrdersByRank(targets) {
  const tgt = {};
  (targets || []).forEach(t => { tgt[t.type_id] = t; });
  const rank = o => {
    const r = (tgt[o.product_type_id] || {}).rank;
    return r === undefined ? 99 : r;
  };
  return (_indOrders || []).slice().sort((a, b) => rank(a) - rank(b) || a.id - b.id);
}

// One-line answer to "where am I": overall progress, what's in the cooker, what to do next.
// `viewOrderId` (TODO §41 follow-up, 2026-08-19): while viewing one order's own plan alone, the
// account-wide progress bar and run counters (_indProgress — a separate fetch that has no
// per-order breakdown of its own) don't apply, so they're skipped rather than shown wrong; the
// order-chip row narrows to just that order instead of every queued one.
function _indStatusHeadline(d, viewOrderId) {
  const p = viewOrderId ? null : _indProgress;
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
  // Just the one order's own chip while viewing it alone — showing every queued order's chip under
  // a view that claims to be about ONE of them is exactly the intermixing this feature answers.
  const chips = viewOrderId
    ? ((o => o ? _indOrderChipHtml(o, byOrder, tgt) : '')((_indOrders || []).find(o => o.id === viewOrderId)))
    : _indOrdersByRank(d.targets).map(o => _indOrderChipHtml(o, byOrder, tgt)).join('');
  return sim
    + `<div class="ind-status-head"><div class="ind-order-chips">${chips}</div>`
    + `<button class="ind-primary-btn" onclick="indOpenPlanner()">Plan a new build</button>`
    + (!viewOrderId && (_indOrders || []).length > 1
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
