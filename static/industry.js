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
  // (they live in the shared Markets & Logistics settings). indPopulateFacility fills the facility
  // map with your structures, so we can tell from it whether a build structure exists yet.
  await indPopulateFacility();
  indRestoreMarginal();
  indRestoreForceBuild();
  const hasStructure = Object.keys(_indFacilityMap).some(k => k.startsWith('s:'));
  indApplyGate(hasStructure);
  if (!hasStructure) return;

  indLoadSetupSummary();
  indLoadLifetime();
  // If something is already cooking, that's what you came to look at — show the live build first
  // and fold the planner away. With an empty queue there's nothing to check, so lead with planning.
  await indRefreshStatus();
}

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
  indRestoreMarginal();
  indRestoreForceBuild();
  const s = document.getElementById('indSearch');
  if (s) setTimeout(() => s.focus(), 30);
}

function indClosePlanner() {
  const m = document.getElementById('indPlanModal');
  if (m) m.style.display = 'none';
}

// The landing view: what's cooking, what's next, and the pipeline as the centrepiece.
async function indRefreshStatus() {
  const card = document.getElementById('indStatusCard');
  const body = document.getElementById('indStatusBody');
  if (!card || !body) return;
  let orders = [];
  try {
    const r = await fetch('/api/industry/orders');
    if (r.ok) orders = (await r.json()).orders || [];
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
  body.innerHTML = _indLoadingHtml('Checking your build…', 'Pulling job status and re-planning what is left.');
  await indLoadProgress();
  try {
    const r = await fetch('/api/industry/queue-plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prioritize_speed: _indPrioSpeed(), marginal_pct: _indMarginalPct(),
                             force_build: _indForceBuild(), ..._indFacilityBonus() }),
    });
    if (!r.ok) { body.innerHTML = '<p class="pp-warn">Could not plan your queue.</p>'; return; }
    const d = await r.json();
    if (d.empty) { card.style.display = 'none'; if (empty) empty.style.display = ''; return; }
    _indLastPlan = d;
    _indCacheNames(d);
    body.innerHTML = _indStatusHeadline(d)
      + `<div id="indInstall" class="ind-install"></div>`
      + _indRenderPlanBody(d)
      + `<div id="indRunning" class="ind-install"></div>`;
    indLoadInstall();          // "do this now" sits directly under the numbers
    indLoadRunning();          // what's already cooking goes under the pipeline
  } catch (e) { body.innerHTML = `<p class="pp-warn">${_esc(String(e))}</p>`; }
}

// One-line answer to "where am I": overall progress, what's in the cooker, what to do next.
function _indStatusHeadline(d) {
  const p = _indProgress;
  const sim = p && p.simulated
    ? '<div class="ind-sim-banner">Preview mode — this progress is made up so you can see the layout. Nothing here is real.</div>' : '';
  const t = (p && p.totals) || null;
  const tiles = [];
  if (t && t.required) {
    tiles.push(['Progress', `${p.pct}%`, `${t.done} of ${t.required} jobs delivered`]);
    tiles.push(['In the cooker', String(t.running), 'Jobs running right now']);
    tiles.push(['Still to start', String(t.waiting), 'Jobs not started yet']);
  }
  const fd = d.metrics.first_delivery_hours;
  // Two different questions: when can I hand over the first order, vs when am I finished entirely.
  // Only worth splitting when they actually differ.
  if (fd != null && d.metrics.makespan_hours - fd > 0.05) {
    tiles.push(['First delivery', _fmtHours(fd), 'When the first order in line is finished and deliverable']);
    tiles.push(['Whole queue', _fmtHours(d.metrics.makespan_hours), 'When everything queued is finished']);
  } else {
    tiles.push(['Time left', _fmtHours(d.metrics.makespan_hours), 'Wall-clock for what remains, jobs in parallel']);
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
    return `<span class="ind-order-chip ind-oc-${st}" id="oc-${o.id}">${pos}${tag}<b>${o.quantity}×</b> ${_esc(o.name)}`
      + (lbl ? `<span class="ind-oc-state">${lbl}</span>` : '') + eta
      + `<button class="ind-oc-edit" title="Rename or change the quantity" onclick="indEditOrder(${o.id})">✎</button>`
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
    + `</div>`;
}

function indApplyGate(hasStructure) {
  const gate = document.getElementById('indGate');
  const content = document.getElementById('indContent');
  if (!gate || !content) return;
  if (hasStructure) { gate.style.display = 'none'; content.style.display = ''; return; }
  content.style.display = 'none';
  gate.style.display = '';
  gate.innerHTML = `<div class="pp-card"><div class="pp-card-title">Set up manufacturing</div><div class="ind-body">
    <p class="pp-sub">To plan builds, add at least one <b>structure you manufacture in</b> — its rigs set your material &amp; time efficiency, which every cost and time figure depends on.</p>
    <ol class="ind-gate-steps">
      <li>Open <b>Settings → Markets &amp; Logistics</b>.</li>
      <li>Search and add your structure, then hit <b>🔨</b> and turn on <b>Manufacture here</b> with its rig tiers.</li>
      <li>Come back and continue — that's it.</li>
    </ol>
    <div class="ind-gate-actions">
      <button class="ind-primary-btn" onclick="openSettingsModal('markets')">Open Markets &amp; Logistics</button>
      <button class="ind-secondary-btn" onclick="onIndustryTabOpen()">I've added it — continue</button>
    </div>
    <p class="pp-sub ind-gate-note">Only reactions? You don't need this — Manufacturing just stays gated until you build something.</p>
  </div></div>`;
}

// Lifetime manufacturing ledger tiles — shown ONLY once the account has actually completed a
// manufacturing job (opt-in-by-use), so the stats never clutter the tab for someone who hasn't
// used it. Forward-only turnover + net profit from real completions.
async function indLoadLifetime() {
  const el = document.getElementById('indLifetime');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/lifetime');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    if (!d.used) { el.innerHTML = ''; return; }
    const since = d.since ? new Date(d.since * 1000).toLocaleDateString() : '';
    el.innerHTML = `<div class="an-stats">`
      + `<div class="an-stat"><div class="an-stat-lbl">Lifetime turnover${since ? ' · since ' + _esc(since) : ''}</div><div class="an-stat-val">${fmtIsk(d.turnover)}</div></div>`
      + `<div class="an-stat an-ok"><div class="an-stat-lbl">Lifetime net profit</div><div class="an-stat-val">${fmtIsk(d.net_profit)}</div></div>`
      + `<div class="an-stat"><div class="an-stat-lbl">Jobs completed</div><div class="an-stat-val">${d.jobs}</div></div>`
      + `</div>`;
  } catch (e) { el.innerHTML = ''; }
}

// ── Setup & slots (modal) + compact tab summary ─────────────────────────────────────────────
async function indLoadSetupSummary() {
  const sum = document.getElementById('indSetupSummary');
  const rem = document.getElementById('indConnectReminder');
  let slots = null, bp = null;
  try { const r = await fetch('/api/industry/slots'); if (r.ok) slots = await r.json(); } catch (e) {}
  try { const r = await fetch('/api/industry/blueprints'); if (r.ok) bp = await r.json(); } catch (e) {}
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
      rem.innerHTML = `Using default ME/TE. <button class="ind-link-btn" onclick="indOpenSetup()">Connect a character</button> to plan with your real blueprints and slots.`;
    } else {
      rem.style.display = 'none';
    }
  }
}

function indOpenSetup() {
  document.getElementById('indSetupModal').style.display = '';
  indLoadSlots();
  indLoadBlueprints();
  indLoadAssets();
}

// ── Stock on hand (ESI assets) ──────────────────────────────────────────────────────────────
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

async function indLoadAssets() {
  const el = document.getElementById('indAssets');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/assets');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    if (!d.connected) {
      el.innerHTML = `<span class="ind-bp-hint">Tell the planner what you already own so it stops asking you to build it, `
        + `and progress can tell what's finished.</span>`
        + _indReauthHtml(d.needs_reauth)
        + `<div class="ind-src-actions">`
        + (d.scannable ? `<button class="ind-bp-btn ind-bp-connect" onclick="indRefreshAssets()">Scan assets</button>` : '')
        + `<button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button></div>`
        + _indPasteFormHtml();
      return;
    }
    const when = d.fetched_at ? new Date(d.fetched_at * 1000).toLocaleString() : '';
    // Every source is opt-in: counting a hangar you can't actually draw from would make the
    // planner build too little, which is worse than ignoring stock altogether.
    const rows = (d.sources || []).map(sc => {
      const sub = sc.kind === 'container'
        ? `container${sc.parent ? ' in ' + _esc(sc.parent) : ''}`
        : sc.kind === 'paste' ? 'pasted' : 'hangar';
      const del = sc.kind === 'paste'
        ? `<button class="ind-src-del" title="Remove this pasted stock" onclick="event.preventDefault();indDeleteSource('${_esc(sc.key)}')">✕</button>` : '';
      return `<label class="ind-src-row"><input type="checkbox" ${sc.enabled ? 'checked' : ''} `
        + `onchange="indToggleSource('${_esc(sc.key)}', this.checked)">`
        + `<span class="ind-src-name">${_esc(sc.name)}</span>`
        + `<span class="ind-src-meta">${sub} · ${sc.item_count} item type${sc.item_count === 1 ? '' : 's'}</span>${del}</label>`;
    }).join('');
    el.innerHTML = `<div class="ind-src-hd"><span class="ind-bp-ok">✓ ${d.enabled_sources} of ${(d.sources || []).length} `
      + `source${(d.sources || []).length === 1 ? '' : 's'} in use · ${d.distinct_types} item type${d.distinct_types === 1 ? '' : 's'} counted`
      + `${when ? ' · scanned ' + _esc(when) : ''}</span>`
      + `<button class="ind-bp-btn" onclick="indRefreshAssets()">Rescan</button></div>`
      + `<p class="ind-src-help">Tick the hangars and containers the planner may take materials from. Nothing is used until you pick it.</p>`
      + `<div class="ind-src-list">${rows || '<span class="ind-bp-hint">No usable hangars found.</span>'}</div>`
      + _indReauthHtml(d.needs_reauth)
      + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button></div>`
      + _indPasteFormHtml();
  } catch (e) { el.innerHTML = ''; }
}

// How corp/shared hangars get in: paste them. ESI can't read a corp hangar for ordinary members,
// so pasting is the path that works for everyone, not a fallback.
function _indPasteFormHtml() {
  return `<div id="indPasteForm" class="ind-paste" style="display:none">
    <p class="ind-src-help">In the EVE client, open the hangar, select all (Ctrl+A), copy (Ctrl+C), paste below.
      Works for any hangar you can see in game — including corp and shared hangars.</p>
    <input type="text" id="indPasteName" placeholder="Name this stock — e.g. Corp hangar: Industry Materials">
    <textarea id="indPasteText" rows="6" placeholder="Tritanium&#9;1 000 000&#10;Pyerite&#9;500 000"></textarea>
    <div class="ind-src-actions">
      <button class="ind-primary-btn" onclick="indSavePaste()">Add as stock</button>
      <button class="ind-bp-btn" onclick="indClosePaste()">Cancel</button>
      <span id="indPasteMsg" class="ind-src-meta"></span>
    </div>
  </div>`;
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
    const r = await fetch('/api/industry/assets/paste', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, text }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) {
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
  try { await fetch('/api/industry/assets/sources/' + encodeURIComponent(key), { method: 'DELETE' }); } catch (e) {}
  indLoadAssets();
  indLoadQueue();
}

async function indToggleSource(key, on) {
  try {
    await fetch('/api/industry/assets/sources', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys: [key], enabled: !!on }),
    });
  } catch (e) {}
  indLoadAssets();
  indLoadQueue();     // stock changes both the plan and the progress numbers
}

async function indRefreshAssets() {
  const el = document.getElementById('indAssets');
  if (el) el.innerHTML = '<span class="ind-bp-hint">Reading assets… (a full asset list can take a moment)</span>';
  let d = null;
  try { const r = await fetch('/api/industry/assets/refresh', { method: 'POST' }); if (r.ok) d = await r.json(); } catch (e) {}
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
    const r = await fetch('/api/industry/blueprints');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    if (d.connected) {
      el.innerHTML = `<span class="ind-bp-ok">✓ ${d.owned_count} blueprint${d.owned_count === 1 ? '' : 's'} detected — using your real ME/TE</span>`
        + `<button class="ind-bp-btn" onclick="indRefreshBlueprints()">Refresh</button>`;
    } else {
      el.innerHTML = `<span class="ind-bp-hint">Connect a character to auto-read your blueprints’ ME/TE and which BPOs you own — no manual entry.</span>`
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
  try { await fetch('/api/industry/blueprints/refresh', { method: 'POST' }); } catch (e) {}
  indLoadBlueprints();
}

// ── Slot pool ───────────────────────────────────────────────────────────────────────────────
async function indLoadSlots() {
  const el = document.getElementById('indSlots');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/slots');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    const chips = (d.characters || []).map(c =>
      `<span class="ind-slot-chip" title="${_esc(c.character_name)}">${_esc(c.character_name)}: `
      + (c.manufacturing_slots ? `${c.manufacturing_free}/${c.manufacturing_slots}<span class="ind-slot-sub">mfg</span>` : '<span class="ind-slot-sub">no mfg</span>')
      + ` · `
      + (c.reaction_slots ? `${c.reaction_free}/${c.reaction_slots}<span class="ind-slot-sub">rx</span>` : '<span class="ind-slot-sub">no rx</span>')
      + `</span>`
    ).join('');
    el.innerHTML = `<div class="ind-slot-tot"><b>${d.manufacturing_free}/${d.manufacturing_slots}</b> manufacturing · `
      + `<b>${d.reaction_free}/${d.reaction_slots}</b> reaction slots free `
      + `<button class="ind-bp-btn" onclick="indRefreshJobs()" title="Re-read running jobs from ESI">Refresh jobs</button></div>`
      + `<div class="ind-slot-chips">${chips || '<span class="pp-sub">No characters — add one to get real slot counts.</span>'}</div>`
      // Never silently drop capacity: say who was left out and why.
      + ((d.excluded || []).length
        ? `<div class="ind-slot-excl"><b>Not used:</b> ` + d.excluded.map(c =>
            `<span title="${_esc(c.reason)}">${_esc(c.character_name)}</span>`).join(', ')
          + `<div class="ind-slot-excl-why">Characters with no slot skills trained (or no skill data) are left out — `
          + `their single free slot would inflate every estimate and send you jobs they can't run.</div></div>`
        : '');
  } catch (e) { el.innerHTML = ''; }
}

// ── Product search / picker ─────────────────────────────────────────────────────────────────
function indOnSearchInput() {
  clearTimeout(_indSearchTimer);
  const q = document.getElementById('indSearch').value.trim();
  if (q.length < 2) { _indHideResults(); return; }
  _indSearchTimer = setTimeout(() => _indSearch(q), 200);
}

async function _indSearch(q) {
  try {
    const r = await fetch('/api/industry/search?q=' + encodeURIComponent(q));
    if (!r.ok) return;
    const d = await r.json();
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

// The speed toggle feeds the same cost/time curve, so flipping it has to re-cost the read-out or it
// would sit there dimmed forever. It deliberately does NOT replan on its own — same as before.
function indOnPrioSpeed() {
  if (_indPicked) _indLoadSweep(_indQty());
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
    const r = await fetch('/api/industry/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: _indPicked.type_id, quantity: qty, prioritize_speed: _indPrioSpeed(),
                             marginal_pct: _indMarginalPct(), force_build: _indForceBuild(),
                             force_build_ids: _indForceIds(), ..._indFacilityBonus() }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(e.detail || 'Plan failed')}</p></div>`; return; }
    const d = await r.json();
    _indLastPlan = d;
    out.innerHTML = _indRenderPlan(d, `Build ${qty}× ${_esc(d.target.name)}`);
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
  // Turn the percentage into the number it actually means for THIS build.
  if (m.marginal_threshold) tiles.push(['Build threshold', fmtIsk(m.marginal_threshold),
    `Anything that would save less than this by building is bought instead (${m.marginal_pct}% of the build, floor 5m). Adjust with the slider above.`]);
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
  indRunPlan();
}

function indUnforceBuildType(typeId) {
  _indForcedTypes.delete(typeId);
  _indSweep = null; _indSweepFailed = null;
  indRunPlan();
}

let _indSweep = null;          // { key, points: [{pct, makespan_hours, total_cost, ...}] }
let _indSweepPending = null;   // key of the request in flight, so a drag can't stack fetches
let _indSweepFailed = null;    // key whose fetch failed — don't leave the line dimmed forever

function _indSweepKey(qty) {
  const f = _indFacilityBonus();
  return [_indPicked ? _indPicked.type_id : 0, qty, _indPrioSpeed() ? 1 : 0,
          f.struct_material_pct, f.struct_time_pct, _indForceIds().sort().join(',')].join('|');
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
    const r = await fetch('/api/industry/plan_sweep', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: _indPicked.type_id, quantity: qty,
                             prioritize_speed: _indPrioSpeed(), force_build_ids: _indForceIds(),
                             ..._indFacilityBonus() }),
    });
    if (!r.ok) { _indSweepFailed = key; _indRenderMarginalLive(); return; }
    const d = await r.json();
    if (_indSweepKey(qty) !== key) return;      // options moved on while it was in flight
    _indSweep = { key, points: d.points || [] };
    _indRenderMarginalLive();
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
    // Dim what's on screen while the curve for the new options is fetched instead of blanking the
    // line — clearing it made the read-out flash out and back on every quantity change or replan.
    el.classList.add('ind-marg-stale');
    if (!el.innerHTML) el.innerHTML = 'Working out what this setting costs…';
    return;
  }
  el.classList.remove('ind-marg-stale');

  const p = _indSweepPoint(pts, _indMarginalPct());
  // Absolutes always, so the line says the same kind of thing whether or not a preview exists. The
  // comparison against the rendered plan is an extra clause, not a replacement for them.
  const parts = [`<b>${_fmtHours(p.makespan_hours)}</b> build time`, `${fmtIsk(p.total_cost)} total`];
  // Only a preview OF THIS product+quantity is a valid baseline — `_indLastPlan` is also set by the
  // whole-queue status plan, and comparing against that would be nonsense.
  const shown = _indLastPlan && _indLastPlan.target && _indLastPlan.metrics
    && _indLastPlan.target.type_id === _indPicked.type_id
    && _indLastPlan.target.quantity === qty ? _indLastPlan : null;
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
    const r = await fetch('/api/markets');
    if (r.ok) {
      const d = await r.json();
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
  return { struct_material_pct: b.me, struct_time_pct: b.te };
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

function _indShopRowHtml(s, allowForce) {
  // "Low saving" is a verdict; on its own it asks the user to trust it. Say what building this one
  // would actually have saved (or cost) and let them overrule it per material.
  let marginal = '';
  if (s.bought_marginal) {
    const sv = s.marginal_saving;
    const why = sv == null ? 'Building this would save too little to be worth a job'
      : sv > 0 ? `Building this batch yourself would save ${fmtIsk(sv)} — under the threshold, so it's bought`
      : `Building this batch yourself would cost ${fmtIsk(-sv)} MORE than buying it`;
    marginal = ` <span class="ind-marginal-badge" title="${_esc(why)}">low saving</span>`
      + (sv != null ? ` <span class="ind-shop-note">${sv > 0 ? `saves ${fmtIsk(sv)} if built`
                                                             : `${fmtIsk(-sv)} dearer to build`}</span>` : '')
      + (allowForce ? ` <button class="ind-shop-force" onclick="indForceBuildType(${s.type_id}, '${_esc(s.name).replace(/'/g, "\\'")}')"`
      + ` title="Build this component in the plan anyway">Build it</button>` : '');
  }
  return `<tr><td>${_esc(s.name)}`
    + `${s.bought_for_speed ? ' <span class="ind-speed-badge" title="Bought instead of built to save time">for speed</span>' : ''}`
    + `${marginal}</td>`
    + `<td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`;
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
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows.map(r => _indShopRowHtml(r, allowForce)).join('')}</tbody></table></div>`;
  });
  // Anything the stage model didn't place (defensive — keeps the list complete no matter what).
  const placed = new Set(model.cols.flatMap(c => c.buys.map(e => e.type_id)));
  const rest = list.filter(s => !placed.has(s.type_id));
  if (rest.length) {
    _indShopStageData['other'] = rest.map(r => ({ name: r.name, qty: r.qty }));
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-other">`
      + `<div class="ind-shop-stage-hd"><span title="Not linked to a build stage — please report this">Not tied to a stage — ${rest.length} item${rest.length > 1 ? 's' : ''}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy('other')">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rest.map(r => _indShopRowHtml(r, allowForce)).join('')}</tbody></table></div>`;
  }
  const totalCost = list.reduce((a, s) => a + (s.line_cost || 0), 0);
  return `<div class="ind-shop-bar"><button class="ind-copy-btn" onclick="indCopyMultibuy()">Copy everything</button>`
    + `<span class="ind-shop-tot">${list.length} items · ${fmtIsk(totalCost)}</span></div>`
    + (allowForce ? _indForcedChipsHtml() : '') + sections;
}

// Copy a shopping list (or one stage of it, if `stage` is given) in EVE's Multibuy paste format
// ("Item Name<tab>qty" per line) so it can be pasted straight into the in-game Multibuy window.
function indCopyMultibuy(stage) {
  const list = (stage !== undefined && _indShopStageData[stage]) ? _indShopStageData[stage] : ((_indLastPlan && _indLastPlan.shopping_list) || []);
  if (!list.length) return;
  const text = list.map(s => `${s.name}\t${Math.ceil(s.qty)}`).join('\n');
  const btn = event && event.target;
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = t; }, 1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => {});
  else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} document.body.removeChild(ta); }
}

// Jump from a pipeline "Buy N materials" card down to that exact stage in the shopping list below.
function _indJumpToStage(t) {
  const el = document.getElementById('ind-shop-stage-' + t);
  if (!el) return;
  const details = el.closest('details');
  if (details) details.open = true;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('ind-shop-flash');
  setTimeout(() => el.classList.remove('ind-shop-flash'), 1200);
}

// Compact tree row label (shared by leaves and collapsible nodes).
function _indTreeLabel(n) {
  const badge = n.decision === 'build'
    ? `<span class="ind-badge ind-build">build${n.activity === 'reaction' ? ' rx' : ''}${n.runs ? ' ×' + n.runs : ''}</span>`
    : n.decision === 'buy' ? '<span class="ind-badge ind-buy">buy</span>'
    : '<span class="ind-badge ind-unres">no price</span>';
  const cost = n.unit_cost != null ? `<span class="ind-tree-cost">${fmtIsk((n.unit_cost || 0) * (n.qty || 0))}</span>` : '';
  const owned = n.owned
    ? ` <span class="ind-owned" title="You own this ${n.owned.kind.toUpperCase()} (ME${n.owned.me}/TE${n.owned.te})">${n.owned.kind.toUpperCase()} ME${n.owned.me}</span>` : '';
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

function _indJobChips(g) {
  return g.map(x => `<span class="ind-wave-job">${_esc(x.name)} ×${x.runs}${x.activity === 'reaction' ? ' rx' : ''} · ${_fmtHours(x.dur)}</span>`).join('');
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
      const s = byStage[key] || (byStage[key] = { key, jobs: 0, runs: 0, start: Infinity, batches: new Set(), by: {} });
      s.jobs += 1;
      s.runs += t.runs;
      s.start = Math.min(s.start, w.start_hours);
      s.batches.add(w.start_hours);
      const g = s.by[t.type_id] || (s.by[t.type_id] = { name: t.name || _indName(t.type_id), runs: 0, activity: t.activity, dur: 0 });
      g.runs += t.runs;
      g.dur = Math.max(g.dur, t.duration_hours);
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
      : `<span class="ind-step-when">≈ +${_fmtHours(s.start)}</span>`;
    // Several batches = the same stage restarted as slots freed, not extra decisions to make.
    const batches = s.batches.size > 1
      ? `<span class="ind-step-note">in ${s.batches.size} batches as slots free</span>` : '';
    html += `<div class="ind-step${first ? ' ind-step-now' : ' ind-step-later'}">`
      + `<div class="ind-step-hd"><span class="ind-step-num">${n}</span>${_esc(title)} — ${jobs} ${when}${batches}</div>`
      + _indStepItems(items, first) + `</div>`;
  });
  html += `<div class="ind-step ind-step-done"><div class="ind-step-hd"><span class="ind-step-num">✓</span>Done — ${_esc(d.target ? d.target.name : 'product')} built in ≈ ${_fmtHours(d.metrics.makespan_hours)}</div></div>`;
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
    const owned = e.owned ? `<span class="ind-owned" title="You own this ${e.owned.kind.toUpperCase()}">${e.owned.kind.toUpperCase()}</span>` : '';
    const runs = e.runs ? `<span class="ind-pipe-runs">${e.runs.toLocaleString()}&nbsp;run${e.runs > 1 ? 's' : ''}</span>` : '';
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
    return `<div class="ind-pipe-card ind-pipe-build${cls}" data-tid="${e.type_id}" title="${_esc(e.name)} — ${qty}${e.runs ? ', ' + e.runs + ' runs' : ''}. Hover to trace its chain."><span class="ind-pipe-name">${_esc(e.name)}</span>`
      + `<span class="ind-pipe-meta"><span class="ind-pipe-qty">${qty}</span>${runs}${owned}${state}</span></div>`;
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
    + `<p class="ind-pipe-hint">Each row is a building, each column a stage. Hover a step to trace its whole chain.</p>`
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

function _indRenderPlan(d, title) {
  const unres = (d.unresolved && d.unresolved.length)
    ? `<p class="pp-warn">${d.unresolved.length} material(s) had no market price — cost is a floor.</p>` : '';
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
      ${unres}
      ${_indBlueprintWarn(d)}
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

function _indBlueprintWarn(d) {
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
  return `<div class="ind-bp-note"><b>No blueprint found for ${miss.length === 1 ? 'this' : 'these'}</b>`
    + `<div class="ind-bp-rows">${rows}</div>`
    + `<div class="ind-bp-warn-sub">Blueprints in corp hangars aren't visible here, so prints you `
    + `already have can still show up in this list. Nothing is blocked and no build decision was `
    + `changed — it's a price so you can compare against a local seller. Copy prices, where copies `
    + `are listed, are included in the total above.</div></div>`;
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
    const r = await fetch('/api/industry/bpc?type_ids=' + ids.join(','));
    if (r.ok) d = await r.json();
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
  // The queue plan carries one tree per ordered product (`trees`); a single-product preview carries
  // one (`tree`). Either way the tier walk merges them by type, matching the aggregated demand.
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, new Set((d.shopping_list || []).map(x => x.type_id)))
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  const stageModel = _indStageModel(tiersData);
  const unres = (d.unresolved && d.unresolved.length)
    ? `<p class="pp-warn">${d.unresolved.length} material(s) had no market price — cost is a floor.</p>` : '';
  return unres + _indBlueprintWarn(d)
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
  try {
    const r = await fetch('/api/industry/orders', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_type_id: _indPicked.type_id, quantity: qty,
                             label: (document.getElementById('indLabel') || {}).value || '' }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.detail || 'Could not queue'); return; }
    document.getElementById('indResult').innerHTML = '';
    const lb = document.getElementById('indLabel');
    if (lb) lb.value = '';          // don't inherit the last customer on the next order
    indClosePlanner();
    await indLoadQueue();
    indLoadInstall();
    await indRefreshStatus();     // adding re-plans the whole queue together — the reason to queue
  } catch (e) { alert(String(e)); }
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
    const r = await fetch('/api/industry/progress' + (_indSim === null ? '' : '?simulate=' + _indSim));
    _indProgress = r.ok ? await r.json() : null;
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
    const r = await fetch('/api/industry/orders/reorder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: _indOrderDraft.map(o => o.id) }),
    });
    if (!r.ok) { if (msg) msg.textContent = await _indErrText(r); return; }
  } catch (e) { if (msg) msg.textContent = String(e); return; }
  indCloseOrder();
  await indRefreshStatus();     // order changes ranks, ETAs and first delivery
}

function _indErrText(r) {
  return r.status === 404 || r.status === 405
    ? 'Not available yet — a deploy may still be rolling out.'
    : `Could not save (HTTP ${r.status})`;
}

// Edit in place rather than in a dialog: it's two fields, and re-planning the whole queue just to
// show an edit form would be a needless several-second wait.
function indEditOrder(id) {
  const chip = document.getElementById('oc-' + id);
  const o = (_indOrders || []).find(x => x.id === id);
  if (!chip || !o) return;
  const p = ((_indProgress && _indProgress.orders) || []).find(x => x.id === id) || {};
  chip.classList.add('ind-oc-editing');
  chip.innerHTML = `<span class="ind-oc-name">${_esc(o.name)}</span>`
    + `<input type="number" min="1" class="ind-oc-qty" id="oce-qty-${id}" value="${o.quantity}" title="Quantity">`
    + `<input type="text" maxlength="60" class="ind-oc-lbl" id="oce-lbl-${id}" `
    + `value="${_esc(p.label || '')}" placeholder="For — customer, contract…">`
    + `<button class="ind-oc-ok" onclick="indSaveOrder(${id})">Save</button>`
    + `<button class="ind-oc-cancel" onclick="indRefreshStatus()">Cancel</button>`;
  const q = document.getElementById('oce-lbl-' + id);
  if (q) q.focus();
}

async function indSaveOrder(id) {
  const qty = parseInt((document.getElementById('oce-qty-' + id) || {}).value, 10);
  const label = (document.getElementById('oce-lbl-' + id) || {}).value || '';
  const body = { label };
  if (!isNaN(qty) && qty >= 1) body.quantity = qty;
  try {
    const r = await fetch('/api/industry/orders/' + id, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.detail || 'Could not save'); return; }
  } catch (e) { alert(String(e)); return; }
  await indRefreshStatus();     // quantity changes the whole plan, so re-plan
}

async function indRemoveOrder(id) {
  try { await fetch('/api/industry/orders/' + id, { method: 'DELETE' }); } catch (e) {}
  await indLoadQueue();
  indLoadInstall();
  await indRefreshStatus();
}

// ── "To install now" checklist + in-progress jobs ───────────────────────────────────────────
async function indRefreshJobs() {
  try { await fetch('/api/industry/jobs/refresh', { method: 'POST' }); } catch (e) {}
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
function _indGroupJobs(jobs) {
  const by = {};
  (jobs || []).forEach(j => {
    const g = by[j.type_id] || (by[j.type_id] = {
      type_id: j.type_id, name: j.name || ('#' + j.type_id), activity: j.activity,
      count: 0, minRuns: Infinity, maxRuns: 0, totalRuns: 0, dur: 0, runsList: [],
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

function _indSlotPips(used, free, assigned, cls) {
  const total = used + free;
  let out = '';
  for (let i = 0; i < total; i++) {
    const k = i < used ? 'busy' : (i < used + assigned ? 'fill' : 'open');
    out += `<span class="ind-pip ind-pip-${k} ind-pip-${cls}"></span>`;
  }
  return out || '<span class="ind-pip-none">no slots</span>';
}

async function indLoadInstall() {
  const el = document.getElementById('indInstall');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/to-install');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
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
        return `<li class="ind-do-job"><span class="ind-do-name">${_esc(g.name)}</span>${each}`
          + `<span class="ind-do-act ind-do-${g.activity}">${g.activity === 'reaction' ? 'reaction' : 'industry'}</span>`
          + `<span class="ind-do-dur">${_fmtHours(g.dur)}</span></li>`;
      }).join('');
      const mUsed = c.manufacturing_slots - c.manufacturing_free;
      const rUsed = c.reaction_slots - c.reaction_free;
      const mAss = c.jobs.filter(j => j.activity !== 'reaction').length;
      const rAss = c.jobs.filter(j => j.activity === 'reaction').length;
      return `<div class="ind-do-char">
        <div class="ind-do-hd"><span class="ind-do-who">${_esc(c.character_name)}</span>
          <span class="ind-do-count">start ${c.assigned} job${c.assigned > 1 ? 's' : ''}`
          + (groups.length < c.assigned ? ` · ${groups.length} product${groups.length > 1 ? 's' : ''}` : '')
          + `</span></div>
        <div class="ind-do-slots">
          ${c.manufacturing_slots ? `<span class="ind-slotset" title="${mUsed} busy · ${mAss} to start · ${c.manufacturing_slots} industry slots">${_indSlotPips(mUsed, c.manufacturing_free, mAss, 'mfg')}</span>` : ''}
          ${c.reaction_slots ? `<span class="ind-slotset" title="${rUsed} busy · ${rAss} to start · ${c.reaction_slots} reaction slots">${_indSlotPips(rUsed, c.reaction_free, rAss, 'rx')}</span>` : ''}
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

async function indLoadRunning() {
  const el = document.getElementById('indRunning');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/jobs');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
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
    s.addEventListener('blur', () => setTimeout(_indHideResults, 150));
  }
  const ps = document.getElementById('indPrioSpeed');
  // Re-run the current plan when the speed priority flips, so the effect is immediate.
  if (ps) ps.addEventListener('change', () => { if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan(); });
});
