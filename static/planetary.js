// ── Planetary Planning ────────────────────────────────────────────────────────

const PP_TYPE_CLASS = {
  Barren: 'ptype-barren', Lava: 'ptype-lava', Oceanic: 'ptype-oceanic',
  Gas: 'ptype-gas', Ice: 'ptype-ice', Storm: 'ptype-storm',
  Temperate: 'ptype-temperate', Plasma: 'ptype-plasma',
};

const P0_LABEL = {
  aqueous_liquids: 'Aqueous Liq', autotrophs: 'Autotrophs', base_metals: 'Base Metals',
  carbon_compounds: 'Carbon Comp', complex_organisms: 'Complex Org', felsic_magma: 'Felsic Magma',
  heavy_metals: 'Heavy Metals', ionic_solutions: 'Ionic Sol', micro_organisms: 'Microorg',
  noble_gas: 'Noble Gas', noble_metals: 'Noble Metals', non_cs_crystals: 'Non-CS Xtal',
  planktic_colonies: 'Planktic Col', reactive_gas: 'Reactive Gas', suspended_plasma: 'Susp Plasma',
};

let _ppPlanets = [];
let _ppActiveCols = [];
let _ppProducts = [];  // {type_id, name, tier}

// ── Wizard state ──────────────────────────────────────────────────────────────

// Factory output rate is now derived automatically from the product's chain (the
// planner knows the exact units/hour a factory makes — 0.5/hr for a P4, etc.), so the
// manual override field was removed. Always returns null → backend auto-derives.
function _factoryRate() {
  const el = document.getElementById('targetFactoryRate');
  if (!el) return null;
  const v = parseFloat((el.value || '').trim().replace(',', '.'));
  return Number.isFinite(v) && v > 0 ? v : null;
}

// Max jumps for neighbour-aware system suggestions (only meaningful for 2+ systems).
function _maxJumps() {
  const el = document.getElementById('targetMaxJumps');
  const v = el ? parseInt(el.value, 10) : 1;
  return Number.isFinite(v) ? Math.max(0, Math.min(5, v)) : 1;
}

// Show the Max-jumps field only when spreading across 2+ systems.
function ppToggleMaxJumps() {
  const n = parseInt((document.getElementById('targetSystems') || {}).value, 10) || 1;
  const show = n >= 2 ? '' : 'none';
  const f = document.getElementById('targetMaxJumps');
  const l = document.getElementById('targetMaxJumpsLabel');
  if (f) f.style.display = show;
  if (l) l.style.display = show;
}

// Synthetic "product" for the fuel-block basket. The sentinel id is the real Oxygen
// Fuel Block type id, reused so per-character config persists via the normal machinery.
const FUEL_BLOCK_TYPE_ID = 4312;
const FUEL_BLOCK_LABEL = 'Fuel Blocks (basket)';

let _wiz = {
  step: 1,
  typeId: null,
  productName: '',
  fuelblock: false,     // true for any multi-product basket target (fuel block OR custom)
  basketId: null,       // custom basket id (null = built-in fuel block)
  inlineBasket: null,   // basket snapshot from a shared link when the basket isn't ours
  lastRecsData: null,   // /api/plan result from step 2 (no chosen_systems)
  lastPlanData: null,   // /api/plan result from step 3 (with chosen_systems)
  chosenSystems: [],
  factorySystem: '',    // override for factory system ('' = auto)
  factoryCharIds: [],   // if non-empty, only these character IDs do factories
  importComponents: [], // fuel-block component type_ids to buy/import, not produce
  factoryPlanetTypes: ['Barren', 'Temperate'], // allowed factory planet types (fuel block)
  splitMode: 'off',     // off | on — split-extraction (reuse planets → more factories)
  distMode: 'stability', // stability (count ∝ need/density) | need (∝ need)
  minDensity: 0,        // ignore planets thinner than this % (plan + system recs); 0 = off
};

let _fbBom = null;   // cached fuel-block basket components (from /api/fuelblock-bom)
let _baskets = [];   // cached custom baskets (from /api/baskets)

// The custom basket id for a product-picker name, or null for fuel block / single products.
function _basketIdFor(name) {
  for (const opt of document.getElementById('productList').options)
    if (opt.value === name) return opt.dataset.basketId ? parseInt(opt.dataset.basketId) : null;
  return null;
}

// A basket's per-character-config "type id" is a sentinel = BASKET_CONFIG_BASE + id
// (mirrors app/admin.py), so profiles/shares storing that id encode the basket — no extra
// field needed. Returns the basket id from a stored tid, or null.
const BASKET_CONFIG_BASE = 2000000000;
const _basketIdFromTid = tid => (tid > BASKET_CONFIG_BASE ? tid - BASKET_CONFIG_BASE : null);
const _basketById = id => _baskets.find(b => b.id === id) || null;
// Self-contained basket definition embedded in a share link so a recipient who can't see
// the (private) basket can still re-run/tweak the plan.
function _basketSnapshot(id) {
  const b = _basketById(id);
  if (!b) return null;
  return {
    name: b.name, run_size: b.run_size, unit_label: b.unit_label,
    items: b.items.map(i => ({ type_id: i.type_id, qty: i.qty })),
  };
}

function wizardGo(n) {
  _wiz.step = n;
  for (let i = 1; i <= 3; i++) {
    const pg = document.getElementById(`wizPage${i}`);
    if (pg) pg.style.display = (i === n) ? '' : 'none';
    const dot = document.getElementById(`wizDot${i}`);
    if (!dot) continue;
    dot.classList.toggle('active', i === n);
    dot.classList.toggle('done', i < n);
    if (i < n) {
      dot.style.cursor = 'pointer';
      dot.onclick = () => wizardGo(i);
    } else {
      dot.style.cursor = 'default';
      dot.onclick = null;
    }
  }
  // Re-render step 2 from stored data when navigating back
  if (n === 2 && _wiz.lastRecsData) renderRecommendations(_wiz.lastRecsData);
}

async function onPlanetaryTabOpen() {
  await loadPiProducts();
  loadCharacters();
  loadConstellations();
  ppToggleMaxJumps();
  renderSavedPlansBar();
  await _tryRestoreFromHash();
}

function onPlanetDbTabOpen() {
  if (!Object.keys(_ppConstRegions).length) loadConstellations();  // region map for search
  loadPlanets();
}

// ── Dashboard (logged-in overview) ────────────────────────────────────────────
let _dashLanded = false;   // auto-land on the dashboard once per page load (logged-in, no saved tab)

async function onDashboardTabOpen() {
  const el = document.getElementById('dashboardContent');
  if (el && !el.dataset.loaded) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading overview…</div>';
  try {
    const data = await (await fetch('/api/dashboard')).json();
    renderDashboard(data);
    if (el) el.dataset.loaded = '1';
  } catch (e) {
    if (el) el.innerHTML = '<section class="pp-card"><div class="pp-card-body"><div class="pp-empty">Failed to load dashboard.</div></div></section>';
  }
}

// Global rescan (header button): pull FRESH colony data from ESI for every character, then
// repaint the data-driven views — so a just-filled launchpad / new routes / expiry show up.
// (A plain re-read of stored data would change nothing.)
async function rescanAll(btn) {
  const ids = (_ppCharsData || []).filter(c => !c.is_dummy && c.character_id > 0).map(c => c.character_id);
  const orig = btn ? btn.textContent : '';
  if (btn) btn.disabled = true;
  let failed = 0;
  for (let i = 0; i < ids.length; i++) {
    if (btn) btn.textContent = `Rescanning ${i + 1}/${ids.length}…`;
    try { const r = await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' }); if (!r.ok) failed++; }
    catch (e) { failed++; }
  }
  if (typeof loadCharacters === 'function') await loadCharacters();   // refresh _ppCharsData + header
  if (typeof onDashboardTabOpen === 'function') await onDashboardTabOpen();
  if (typeof renderAnalysis === 'function' && _analyzeSnaps.length) renderAnalysis();
  if (btn) { btn.disabled = false; btn.textContent = orig || 'Rescan'; }
  if (failed) alert(`${failed} of ${ids.length} character${ids.length !== 1 ? 's' : ''} could not be rescanned — usually an expired ESI token (red dot in Characters).`);
}

function _dashTile(val, lbl, cls) {
  return `<div class="an-stat"><div class="an-stat-val${cls ? ' ' + cls : ''}">${val}</div><div class="an-stat-lbl">${lbl}</div></div>`;
}

function renderDashboard(data) {
  const el = document.getElementById('dashboardContent');
  if (!el) return;
  if (!data || !data.logged_in) {
    el.innerHTML = `<section class="pp-card"><div class="pp-card-title">Dashboard</div>
      <div class="pp-card-body"><div class="pp-empty">Log in with ESI (Characters tab) to see your PI overview.</div></div></section>`;
    return;
  }
  const t = data.totals || {}, facs = data.factories || [], top = data.top_pi;
  const runtime = (t.runtime_hours != null) ? _fmtHours(t.runtime_hours) : '—';
  const tiles = [
    _dashTile(runtime, 'Runtime left (soonest)'),
    _dashTile(_fmtIsk(t.pads_value || 0), top ? `In pads now · top ${_esc(top.name)}` : 'In pads now'),
    _dashTile(_fmtIsk(t.current_run_value || 0), 'Run value (from current inputs)'),
    _dashTile(_fmtIsk(t.value_per_day || 0), 'Value / day'),
  ].join('');
  const rows = facs.length ? facs.map(f => {
    const cls = f.fill_pct >= 50 ? 'an-bar-ok' : f.fill_pct >= 20 ? 'an-bar-warn' : 'an-bar-bad';
    const empty = !f.hours_left;
    return `<div class="dash-fac-row">
      <div class="dash-fac-id"><div class="dash-fac-name">${_esc(f.product)}</div><div class="dash-fac-loc">${_esc(f.loc)}</div></div>
      <div class="an-bar-track"><div class="an-bar-fill ${cls}" style="width:${Math.max(2, f.fill_pct)}%"></div></div>
      <div class="dash-fac-pct">${f.fill_pct}%</div>
      <div class="dash-fac-time${empty ? ' dash-fac-empty' : ''}">${empty ? 'empty' : _fmtHours(f.hours_left)}</div>
      <div class="dash-fac-val">${f.run_value ? _fmtIsk(f.run_value) : '–'}</div>
    </div>`;
  }).join('') : '<div class="pp-empty">No factory planets found. Deploy factories, then refresh on the Characters tab.</div>';
  // Warnings only, grouped per character — a card that appears solely when something's amiss.
  const issues = data.issues || [];
  const issuesHtml = issues.length ? `
    <section class="pp-card dash-issues">
      <div class="pp-card-title">Needs attention <span class="pp-card-hint">— ${issues.length} character${issues.length !== 1 ? 's' : ''} need a look</span></div>
      <div class="pp-card-body">${issues.map(c =>
        `<div class="dash-issue dash-issue-${c.severity === 'high' ? 'high' : 'warn'}">
          <div class="dash-issue-char">${_esc(c.char)}</div>
          <ul class="dash-issue-items">${c.items.map(i => `<li class="dash-il-${i.severity === 'high' ? 'high' : 'warn'}">${_esc(i.msg)}</li>`).join('')}</ul>
        </div>`).join('')}</div>
    </section>` : '';
  el.innerHTML = issuesHtml + `
    <section class="pp-card">
      <div class="pp-card-title">Overview <span class="pp-card-hint">— your PI at a glance · Rescan in the top bar pulls fresh data</span></div>
      <div class="pp-card-body"><div class="an-stats">${tiles}</div></div>
    </section>
    <section class="pp-card">
      <div class="pp-card-title">Factories <span class="pp-card-hint">— launchpad fill &amp; time to empty (${facs.length})</span></div>
      <div class="pp-card-body">
        ${facs.length ? '<div class="dash-fac-head"><span>Factory</span><span>Fill</span><span>%</span><span>Runs out</span><span>Run value</span></div>' : ''}
        <div class="dash-fac-list">${rows}</div>
      </div>
    </section>`;
}

// ── ESI / Characters ──────────────────────────────────────────────────────────

let _esiConfigured = false;
let _loggedIn = false;
let _isAdmin = false;
let _ppCharsData = [];   // last /api/characters payload, for the Setup Analysis tab

async function loadCharacters() {
  try {
    const resp = await fetch('/api/characters');
    const data = await resp.json();
    _esiConfigured = data.configured;
    _loggedIn = data.logged_in || false;
    _isAdmin = data.is_admin || false;
    _ppCharsData = data.characters || [];
    renderCharacters(data.characters || [], _loggedIn);
    renderHeaderSession(_loggedIn, data.characters || [], data.session_character_id);
    await loadProfiles();
  } catch (e) {
    console.error('Failed to load characters:', e);
  }
}

function renderHeaderSession(loggedIn, chars, sessionCharId) {
  const el = document.getElementById('headerActions');
  if (!el) return;
  if (!loggedIn) {
    el.innerHTML = _esiConfigured
      ? `<button class="header-login-btn" onclick="esiLogin()">Login</button>`
      : '';
    const dt = document.getElementById('dashboardNavTab');
    if (dt) dt.style.display = 'none';
    return;
  }
  const char = chars.find(c => c.character_id === sessionCharId);
  const name = char ? char.name : 'Unknown';
  // Admin access is the sidebar "Admin" item now (no duplicate header button).
  el.innerHTML =
    `<button class="header-bug-btn" onclick="rescanAll(this)" title="Re-scan every character's colonies from ESI">Rescan</button>`
    + `<button class="header-bug-btn" onclick="openBugModal()">Report bug</button>`
    + `<span class="header-session">${name} · <a href="/auth/logout" class="header-logout">Log out</a></span>`;
  const navTab = document.getElementById('adminNavTab');
  if (navTab) navTab.style.display = _isAdmin ? '' : 'none';
  const dashTab = document.getElementById('dashboardNavTab');
  if (dashTab) dashTab.style.display = '';   // the overview is for logged-in players
  // First load for a logged-in player with no remembered tab → land on the dashboard.
  if (!_dashLanded) {
    _dashLanded = true;
    const isShare = window.__SHARE_ID__ || /^\/s\//.test(location.pathname);
    if (!localStorage.getItem('activeTab') && !isShare && typeof switchTab === 'function') switchTab('dashboard');
  }
  const mb = document.getElementById('manageBasketsBtn');
  if (mb) mb.style.display = loggedIn ? '' : 'none';   // user-owned baskets need a login
}

// ── Bug reporting ─────────────────────────────────────────────────────────────

function openBugModal() {
  document.getElementById('bugTitle').value = '';
  document.getElementById('bugDesc').value = '';
  document.getElementById('bugStatus').textContent = '';
  const btn = document.getElementById('bugSubmitBtn');
  btn.disabled = false; btn.textContent = 'Submit';
  document.getElementById('bugModal').style.display = 'flex';
  document.getElementById('bugTitle').focus();
}
function closeBugModal() { document.getElementById('bugModal').style.display = 'none'; }

async function submitBug() {
  const title = document.getElementById('bugTitle').value.trim();
  const description = document.getElementById('bugDesc').value.trim();
  const msg = document.getElementById('bugStatus');
  if (!title || !description) { msg.textContent = 'Title and description required.'; return; }
  const btn = document.getElementById('bugSubmitBtn');
  btn.disabled = true; btn.textContent = 'Submitting…'; msg.textContent = '';
  try {
    const resp = await fetch('/api/bugs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, description }),
    });
    if (resp.status === 401) { msg.textContent = 'Log in to report a bug.'; btn.disabled = false; btn.textContent = 'Submit'; return; }
    if (!resp.ok) { const d = await resp.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${resp.status}`); }
    btn.textContent = '✓ Thanks!';
    setTimeout(closeBugModal, 900);
  } catch (e) {
    msg.textContent = 'Failed: ' + e.message; btn.disabled = false; btn.textContent = 'Submit';
  }
}

let _bugFilter = '';
function openBugAdmin() { switchTab('admin'); }  // bug reports now live in the Admin tab

// ── Admin tab ───────────────────────────────────────────────────────────────────
function onAdminTabOpen() {
  if (!_isAdmin) { switchTab('planetary'); return; }
  loadPlanetSubmissions();
  loadBugs();
  loadAdmins();
}

async function loadPlanetSubmissions() {
  const list = document.getElementById('planetSubList');
  if (!list) return;
  list.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span>Loading…</div>';
  try {
    const resp = await fetch('/api/planet-submissions?status=pending');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderPlanetSubmissions(data.submissions || []);
  } catch (e) {
    list.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function renderPlanetSubmissions(subs) {
  const cEl = document.getElementById('psubCount');
  if (cEl) cEl.textContent = subs.length ? `${subs.length} pending` : '';
  const list = document.getElementById('planetSubList');
  if (!subs.length) { list.innerHTML = '<div class="pp-empty">No pending submissions.</div>'; return; }
  list.innerHTML = subs.map(s => {
    const when = (s.created_at || '').replace('T', ' ').slice(0, 16);
    const newN = s.planets.filter(p => !p.exists).length;
    const ovN  = s.planets.filter(p => p.exists).length;
    const chips = s.planets.map(p =>
      `<span class="psub-chip ${p.exists ? 'psub-chip-ov' : 'psub-chip-new'}" `
      + `title="${p.exists ? 'updates existing planet (blank cells keep current values)' : 'new planet'}">`
      + `${_esc(p.system)} P${p.planet_num} · ${_esc(p.planet_type)}</span>`).join('');
    return `<div class="bug-item">
      <div class="bug-item-head">
        <span class="bug-item-title">${s.planet_count} planet${s.planet_count === 1 ? '' : 's'}</span>
        <span class="bug-item-meta">${_esc(s.submitter_name || '?')} · ${_esc(when)}</span>
      </div>
      <div class="psub-summary">${newN} new · ${ovN} update</div>
      <div class="psub-chips">${chips}</div>
      <div class="bug-item-actions">
        <button class="bug-act bug-act-done" onclick="reviewPlanetSubmission(${s.id},'approve')">Approve all</button>
        <button class="bug-act bug-act-ignore" onclick="reviewPlanetSubmission(${s.id},'reject')">Reject all</button>
      </div>
    </div>`;
  }).join('');
}

async function reviewPlanetSubmission(id, action) {
  if (action === 'reject' && !confirm('Reject and discard this submission?')) return;
  try {
    const resp = await fetch(`/api/planet-submissions/${id}/${action}`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    if (action === 'approve') {
      if (data.errors && data.errors.length)
        alert(`Imported ${data.imported}, skipped ${data.skipped}.\n\nWarnings:\n` + data.errors.join('\n'));
      if (typeof loadPlanets === 'function') loadPlanets(true);
      if (typeof loadConstellations === 'function') loadConstellations();
    }
    loadPlanetSubmissions();
  } catch (e) { alert('Failed: ' + e.message); }
}

async function loadAdmins() {
  const el = document.getElementById('adminList');
  if (!el) return;
  try {
    const data = await (await fetch('/api/admins')).json();
    const boot = (data.bootstrap || []).map(n =>
      `<div class="admin-row"><span class="admin-name">${_esc(n)}</span>`
      + `<span class="admin-tag">permanent</span></div>`).join('');
    const rows = (data.admins || []).map(a =>
      `<div class="admin-row"><span class="admin-name">${_esc(a.character_name)}</span>`
      + `<span class="admin-meta">${a.added_by ? 'by ' + _esc(a.added_by) : ''}</span>`
      + `<button class="bug-act bug-act-ignore" onclick="removeAdmin('${_esc(a.character_name).replace(/'/g, "\\'")}')">Remove</button></div>`).join('');
    el.innerHTML = boot + rows || '<div class="pp-empty">No admins.</div>';
  } catch (e) { el.innerHTML = `<div class="pp-empty">Failed: ${_esc(e.message)}</div>`; }
}

async function addAdmin() {
  const inp = document.getElementById('adminNameInput');
  const status = document.getElementById('adminAddStatus');
  const name = inp.value.trim();
  if (!name) return;
  status.textContent = 'Adding…';
  try {
    const resp = await fetch('/api/admins', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character_name: name }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    inp.value = ''; status.textContent = '';
    loadAdmins();
  } catch (e) { status.textContent = e.message; }
}

async function removeAdmin(name) {
  if (!confirm(`Remove admin "${name}"?`)) return;
  try {
    const resp = await fetch(`/api/admins/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    loadAdmins();
  } catch (e) { alert('Failed: ' + e.message); }
}

// ── Production baskets (user-owned + global) ──────────────────────────────────
// Any logged-in user can create private baskets (visible only to them). Admins also
// manage the shared global baskets via the "Make global" toggle. The manager lives in a
// modal (#basketModal) reachable from the product step and the Admin tab.
let _basketEditId = null;            // null = creating a new basket
let _basketEditItems = [];           // [{type_id, name, tier, qty}]

// A basket is editable by this account if it's the account's own private basket, or it's
// a global basket and the account is an admin.
const _basketEditable = b => b.owned || (b.global && _isAdmin);

function openBasketModal() {
  const m = document.getElementById('basketModal');
  if (!m) return;
  m.style.display = 'flex';
  const gr = document.getElementById('basketGlobalRow');
  if (gr) gr.style.display = _isAdmin ? '' : 'none';   // only admins can publish globals
  resetBasketEditor();
  loadBasketManager();
}
function closeBasketModal() {
  const m = document.getElementById('basketModal');
  if (m) m.style.display = 'none';
}

async function loadBasketManager() {
  await _refreshBaskets();           // keeps the wizard picker in sync too
  const el = document.getElementById('basketList');
  if (!el) return;
  if (!_baskets.length) { el.innerHTML = '<div class="pp-empty">No baskets yet — create one below.</div>'; return; }
  el.innerHTML = _baskets.map(b => {
    const items = b.items.map(i => `${_esc(i.name)}×${i.qty}`).join(', ');
    const badge = b.global
      ? '<span class="basket-tag basket-tag-global">global</span>'
      : (b.owned ? '<span class="basket-tag basket-tag-mine">mine</span>' : '');
    const actions = _basketEditable(b)
      ? `<button class="bug-act" onclick="editBasket(${b.id})">Edit</button>
         <button class="bug-act bug-act-ignore" onclick="deleteBasket(${b.id}, '${_esc(b.name).replace(/'/g, "\\'")}')">Delete</button>`
      : '';
    return `<div class="basket-row">
      <div class="basket-row-head">
        <span class="admin-name">${_esc(b.name)}</span>${badge}
        <span class="admin-meta">run ${b.run_size} · ${_esc(b.unit_label)}</span>
        ${actions}
      </div>
      <div class="basket-row-items">${_esc(items)}</div>
    </div>`;
  }).join('');
}

function resetBasketEditor() {
  _basketEditId = null;
  _basketEditItems = [];
  document.getElementById('basketEditorTitle').textContent = 'New basket';
  document.getElementById('basketName').value = '';
  document.getElementById('basketRunSize').value = '1';
  document.getElementById('basketUnitLabel').value = 'sets';
  document.getElementById('basketItemSearch').value = '';
  document.getElementById('basketStatus').textContent = '';
  const g = document.getElementById('basketGlobal');
  if (g) g.checked = false;
  renderBasketEditorItems();
}

function editBasket(id) {
  const b = _basketById(id);
  if (!b) return;
  _basketEditId = id;
  _basketEditItems = b.items.map(i => ({ type_id: i.type_id, name: i.name, tier: i.tier, qty: i.qty }));
  document.getElementById('basketEditorTitle').textContent = `Editing: ${b.name}`;
  document.getElementById('basketName').value = b.name;
  document.getElementById('basketRunSize').value = b.run_size;
  document.getElementById('basketUnitLabel').value = b.unit_label;
  document.getElementById('basketStatus').textContent = '';
  const g = document.getElementById('basketGlobal');
  if (g) g.checked = !!b.global;       // editing a global keeps it global (admin only)
  renderBasketEditorItems();
  document.getElementById('basketName').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function renderBasketEditorItems() {
  const el = document.getElementById('basketItems');
  if (!el) return;
  if (!_basketEditItems.length) { el.innerHTML = '<div class="pp-empty">No components yet — search above to add.</div>'; return; }
  el.innerHTML = _basketEditItems.map((it, i) => `<div class="basket-item-row">
    <span class="basket-item-name">${_esc(it.name)} ${_ptypeSpan('P' + it.tier)}</span>
    <input type="number" min="0.01" step="1" value="${it.qty}" class="bug-input basket-num"
           oninput="_setBasketItemQty(${i}, this.value)">
    <button class="bug-act bug-act-ignore" onclick="_removeBasketItem(${i})">✕</button>
  </div>`).join('');
}

function _setBasketItemQty(i, val) { if (_basketEditItems[i]) _basketEditItems[i].qty = parseFloat(val) || 0; }
function _removeBasketItem(i) { _basketEditItems.splice(i, 1); renderBasketEditorItems(); }

function onBasketItemSearch() {
  const q = document.getElementById('basketItemSearch').value.trim().toLowerCase();
  const box = document.getElementById('basketItemResults');
  if (!q) { box.style.display = 'none'; return; }
  const have = new Set(_basketEditItems.map(it => it.type_id));
  const hits = _ppProducts
    .filter(p => p.tier >= 1 && p.name.toLowerCase().includes(q) && !have.has(p.type_id))
    .slice(0, 12);
  box.innerHTML = hits.length
    ? hits.map(p => `<div class="layout-result" onmousedown="addBasketItem(${p.type_id})">${_esc(p.name)} ${_ptypeSpan('P' + p.tier)}</div>`).join('')
    : '<div class="layout-result-empty">No P1–P4 match</div>';
  box.style.display = '';
}
function hideBasketItemResults() { const b = document.getElementById('basketItemResults'); if (b) b.style.display = 'none'; }

function addBasketItem(typeId) {
  const p = _ppProducts.find(x => x.type_id === typeId);
  if (!p) return;
  _basketEditItems.push({ type_id: p.type_id, name: p.name, tier: p.tier, qty: 1 });
  document.getElementById('basketItemSearch').value = '';
  hideBasketItemResults();
  renderBasketEditorItems();
}

async function saveBasket() {
  const status = document.getElementById('basketStatus');
  const name = document.getElementById('basketName').value.trim();
  const run_size = parseInt(document.getElementById('basketRunSize').value) || 1;
  const unit_label = document.getElementById('basketUnitLabel').value.trim() || 'sets';
  const items = _basketEditItems
    .filter(it => it.qty > 0)
    .map(it => ({ type_id: it.type_id, qty: it.qty }));
  if (!name) { status.textContent = 'Name required'; return; }
  if (!items.length) { status.textContent = 'Add at least one component'; return; }
  const g = document.getElementById('basketGlobal');
  const make_global = !!(g && g.checked && _isAdmin);
  status.textContent = 'Saving…';
  try {
    const url = _basketEditId ? `/api/baskets/${_basketEditId}` : '/api/baskets';
    const resp = await fetch(url, {
      method: _basketEditId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, run_size, unit_label, items, make_global }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    resetBasketEditor();
    loadBasketManager();
  } catch (e) { status.textContent = e.message; }
}

async function deleteBasket(id, name) {
  if (!confirm(`Delete basket "${name}"? It will no longer be selectable in the planner.`)) return;
  try {
    const resp = await fetch(`/api/baskets/${id}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    if (_basketEditId === id) resetBasketEditor();
    loadBasketManager();
  } catch (e) { alert('Failed: ' + e.message); }
}
function filterBugs(status, btn) {
  _bugFilter = status;
  document.querySelectorAll('.bug-filter').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  loadBugs();
}

async function loadBugs() {
  const list = document.getElementById('bugAdminList');
  list.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span>Loading…</div>';
  try {
    const url = '/api/bugs' + (_bugFilter ? `?status=${_bugFilter}` : '');
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderBugs(data.bugs || [], data.counts || {});
  } catch (e) {
    list.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function renderBugs(bugs, counts) {
  const cEl = document.getElementById('bugAdminCounts');
  cEl.textContent = `${counts.open || 0} open · ${counts.complete || 0} done · ${counts.ignored || 0} ignored`;
  const list = document.getElementById('bugAdminList');
  if (!bugs.length) { list.innerHTML = '<div class="pp-empty">No reports.</div>'; return; }
  list.innerHTML = bugs.map(b => {
    const when = (b.created_at || '').replace('T', ' ').slice(0, 16);
    const actions = [];
    if (b.status !== 'complete') actions.push(`<button class="bug-act bug-act-done" onclick="setBugStatus(${b.id},'complete')">Complete</button>`);
    if (b.status !== 'ignored')  actions.push(`<button class="bug-act bug-act-ignore" onclick="setBugStatus(${b.id},'ignored')">Ignore</button>`);
    if (b.status !== 'open')     actions.push(`<button class="bug-act" onclick="setBugStatus(${b.id},'open')">Reopen</button>`);
    return `<div class="bug-item bug-${b.status}">
      <div class="bug-item-head">
        <span class="bug-badge bug-badge-${b.status}">${b.status}</span>
        <span class="bug-item-title">${_esc(b.title)}</span>
        <span class="bug-item-meta">${_esc(b.character_name || '?')} · ${_esc(when)}</span>
      </div>
      <div class="bug-item-desc">${_esc(b.description)}</div>
      <div class="bug-item-actions">${actions.join('')}</div>
    </div>`;
  }).join('');
}

async function setBugStatus(id, status) {
  try {
    const resp = await fetch(`/api/bugs/${id}/status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    loadBugs();
  } catch (e) { alert('Failed: ' + e.message); }
}

function renderCharacters(chars, loggedIn) {
  const list = document.getElementById('characterList');
  list.innerHTML = '';
  const dummyCard = document.getElementById('dummyCharCard');
  if (dummyCard) dummyCard.style.display = loggedIn ? '' : 'none';

  const addBtn     = document.getElementById('esiLoginBtn');
  const refreshBtn = document.getElementById('ppRefreshBtn');

  if (!_esiConfigured) {
    if (addBtn) {
      addBtn.style.display = '';
      addBtn.style.opacity = '0.4';
      addBtn.style.cursor = 'not-allowed';
      addBtn.title = 'Set EVE_CLIENT_ID and EVE_CLIENT_SECRET in .env';
      addBtn.onclick = () => alert('ESI not configured.\n\nRegister an app at https://developers.eveonline.com\nthen set EVE_CLIENT_ID and EVE_CLIENT_SECRET in .env and redeploy.');
    }
    if (refreshBtn) refreshBtn.style.display = 'none';
  } else if (loggedIn) {
    if (addBtn) {
      addBtn.style.display = '';
      addBtn.style.opacity = '';
      addBtn.style.cursor = '';
      addBtn.title = 'Add another character via EVE SSO';
      addBtn.onclick = esiLogin;
    }
    if (refreshBtn) refreshBtn.style.display = '';
  } else {
    // Not logged in: hide character management buttons
    if (addBtn)     addBtn.style.display     = 'none';
    if (refreshBtn) refreshBtn.style.display = 'none';
  }

  if (!loggedIn) {
    list.innerHTML = '<div class="pp-empty">Login to view characters.</div>';
    return;
  }
  if (!chars.length) {
    list.innerHTML = '<div class="pp-empty">No characters added yet.</div>';
    return;
  }

  chars.forEach(c => {
    const row = document.createElement('div');
    row.className = 'pp-char-row' + (c.is_dummy ? ' pp-char-dummy' : '');
    if (c.is_dummy) {
      const mpOpts = [1, 2, 3, 4, 5, 6].map(n => `<option${n === c.max_planets ? ' selected' : ''}>${n}</option>`).join('');
      const ccuOpts = [1, 2, 3, 4, 5].map(n => `<option${n === c.ccu ? ' selected' : ''}>${n}</option>`).join('');
      row.innerHTML = `
        <div class="pp-char-header">
          <span class="pp-char-name"><span class="pp-char-dummy-badge" title="Placeholder character — no ESI, contributes planet slots + CCU only">placeholder</span> ${_esc(c.name)}</span>
          <button class="pp-char-del" title="Remove placeholder" data-id="${c.character_id}">✕</button>
        </div>
        <div class="pp-char-meta">
          <label class="pp-dummy-field">planets <select data-f="max_planets">${mpOpts}</select></label>
          <label class="pp-dummy-field">CCU <select data-f="ccu">${ccuOpts}</select></label>
        </div>`;
      row.querySelectorAll('select[data-f]').forEach(sel =>
        sel.addEventListener('change', () => editDummyField(c.character_id, sel.dataset.f, parseInt(sel.value))));
      row.querySelector('.pp-char-del').addEventListener('click', async () => {
        await fetch(`/api/characters/${c.character_id}`, { method: 'DELETE' });
        loadCharacters();
      });
      list.appendChild(row);
      return;
    }
    const tokenDot = c.token_ok
      ? '<span title="Token valid" style="color:#5ecf80;font-size:10px">●</span>'
      : '<span title="Token expired — re-add character" style="color:#e06060;font-size:10px">●</span>';
    const planets    = c.planets || [];
    const extractors = planets.filter(p => p.is_extractor);
    const factories  = planets.filter(p => !p.is_extractor);
    const used = planets.length;
    const nP0  = new Set(extractors.map(p => p.p0_name).filter(Boolean)).size;
    const nSys = new Set(planets.map(p => p.system).filter(Boolean)).size;
    const delHtml = loggedIn
      ? `<button class="pp-char-del" title="Remove character" data-id="${c.character_id}">✕</button>`
      : '';

    const _byloc = (a, b) => ((a.system || '~').localeCompare(b.system || '~')) || ((a.planet_num ?? 1e9) - (b.planet_num ?? 1e9));
    const planetRows = planets.length
      ? [...planets].sort(_byloc).map(p => {
          const loc = `${p.system ? _esc(p.system) + ' ' : ''}${p.planet_num != null ? 'P' + p.planet_num : ''}`.trim() || '—';
          const builds = (p.products || []).map(x => _esc(x.name)).join(', ');
          const what = p.is_extractor
            ? `<span class="pp-pl-extract">→ ${_esc(p.p0_name || '?')}</span>${builds ? `<span class="pp-pl-build"> → ${builds}</span>` : ''}`
            : (builds
                ? `<span class="pp-pl-build">→ ${builds}</span>`
                : `<span class="pp-pl-factory">factory${p.num_pins ? ' · ' + p.num_pins + ' pins' : ''}</span>`);
          // Estimated launchpad contents for this planet (simulated forward from the last scan).
          const pad = (p.pads || []).length
            ? `<span class="pp-pl-pad" title="Estimated launchpad contents — simulated forward from the last Refresh (ESI only reports a stale checkpoint).">${p.pads.map(x => `<b>${x.amount.toLocaleString()}</b> ${_esc(x.name)}`).join(' · ')}</span>`
            : '';
          const cc = p.upgrade_level ? `<span class="pp-pl-cc" title="Command center level">CC${p.upgrade_level}</span>` : '';
          return `<div class="pp-pl-row"><span class="pp-pl-loc">${loc}</span>${_ptypeSpan(p.planet_type)}${what}${pad}${cc}</div>`;
        }).join('')
      : '<div class="pp-pl-empty">No colonies scanned — set them up in-game, then hit Refresh.</div>';

    const stats = `<div class="pp-char-stats">
        <span title="Colonies in use / max planets">${used}/${c.max_planets} planets</span>
        <span>${extractors.length} extractor${extractors.length !== 1 ? 's' : ''}</span>
        <span>${factories.length} factor${factories.length !== 1 ? 'ies' : 'y'}</span>
        ${nP0 ? `<span title="Distinct P0 resources extracted">${nP0} P0 type${nP0 !== 1 ? 's' : ''}</span>` : ''}
        ${nSys ? `<span title="Distinct systems">${nSys} system${nSys !== 1 ? 's' : ''}</span>` : ''}
        <span title="Command Center Upgrades level">CCU ${c.ccu}</span>
        ${c.planetology != null ? `<span title="Planetology skill">Planetology ${c.planetology}</span>` : ''}
        ${c.adv_planetology != null ? `<span title="Advanced Planetology skill">Adv ${c.adv_planetology}</span>` : ''}
      </div>`;

    row.innerHTML = `
      <details class="pp-char-fold">
        <summary class="pp-char-header">
          <span class="pp-char-name">${tokenDot} ${_esc(c.name)}</span>
          <span class="pp-char-summary">${used} pl · ${extractors.length} ext · ${factories.length} fac</span>
          ${delHtml}
        </summary>
        <div class="pp-char-body">
          ${stats}
          <div class="pp-char-planet-list">${planetRows}</div>
        </div>
      </details>`;
    if (loggedIn) {
      const del = row.querySelector('.pp-char-del');
      if (del) del.addEventListener('click', async (e) => {
        e.preventDefault(); e.stopPropagation();
        if (!confirm(`Remove ${c.name}?`)) return;
        await fetch(`/api/characters/${c.character_id}`, { method: 'DELETE' });
        loadCharacters();
      });
    }
    list.appendChild(row);
  });
  renderMaterialsSummary(chars);
}

// Roll up the (simulated) launchpad contents across every character → a "what I have on hand"
// list, with one-click copy / send into the PI Planner inventory box.
let _lastMaterials = [];
function _aggregateMaterials(chars) {
  const totals = {};
  (chars || []).forEach(c => (c.planets || []).forEach(p => (p.pads || []).forEach(it => {
    if (it.name && it.amount) totals[it.name] = (totals[it.name] || 0) + it.amount;
  })));
  return Object.entries(totals).sort((a, b) => b[1] - a[1]);  // [[name, amount], …]
}
function _materialsText(entries) { return entries.map(([n, a]) => `${n}\t${a}`).join('\n'); }

function renderMaterialsSummary(chars) {
  const el = document.getElementById('ppMaterialsSummary');
  if (!el) return;
  const entries = _aggregateMaterials(chars);
  _lastMaterials = entries;
  if (!entries.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';
  el.innerHTML = `
    <div class="pp-mat-head">
      <span class="pp-mat-title">In launchpads <span class="pp-char-pads-est">~est</span>
        <span class="pp-mat-sub">${entries.length} material${entries.length !== 1 ? 's' : ''}, all characters</span></span>
      <span class="pp-mat-actions">
        <button class="pp-add-btn" onclick="copyMaterials(this)" title="Copy as a tab-separated list to paste into the PI Planner inventory box">Copy</button>
        <button class="pp-add-btn" onclick="sendMaterialsToPlanner()" title="Fill the PI Planner inventory box with this and switch there">Send to PI Planner →</button>
      </span>
    </div>
    <div class="pp-mat-list">${entries.map(([n, a]) =>
      `<span class="pp-mat-item"><span class="pp-mat-name">${_esc(n)}</span><b>${a.toLocaleString()}</b></span>`).join('')}</div>`;
}

function copyMaterials(btn) {
  const txt = _materialsText(_lastMaterials);
  if (!txt) return;
  const done = () => { const t = btn.textContent; btn.textContent = '✓ Copied'; setTimeout(() => { btn.textContent = t; }, 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done).catch(() => prompt('Copy:', txt));
  else { prompt('Copy:', txt); }
}

function sendMaterialsToPlanner() {
  const inv = document.getElementById('inv');
  if (inv) inv.value = _materialsText(_lastMaterials);
  if (typeof switchTab === 'function') switchTab('planner');
  if (typeof syncRefillFromInventory === 'function') syncRefillFromInventory();
}

async function addDummyCharacters(btn) {
  const count = parseInt(document.getElementById('dummyCount').value) || 1;
  const max_planets = parseInt(document.getElementById('dummyMaxPlanets').value) || 6;
  const ccu = parseInt(document.getElementById('dummyCcu').value) || 5;
  const name_prefix = (document.getElementById('dummyPrefix').value || 'Alt').trim() || 'Alt';
  const status = document.getElementById('dummyStatus');
  status.textContent = 'Adding…';
  btn.disabled = true;
  try {
    const resp = await fetch('/api/characters/dummy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, max_planets, ccu, name_prefix }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    const d = await resp.json();
    status.textContent = `Added ${d.count}`;
    await loadCharacters();
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
    setTimeout(() => { status.textContent = ''; }, 2500);
  }
}

async function editDummyField(id, field, value) {
  const body = {}; body[field] = value;
  try {
    await fetch(`/api/characters/dummy/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { /* best-effort; reload reflects truth */ }
  loadCharacters();
}

async function refreshAllPlanets(btn) {
  const chars = document.querySelectorAll('.pp-char-del');
  const ids = Array.from(chars).map(b => parseInt(b.dataset.id)).filter(id => id > 0);  // skip placeholders
  btn.textContent = `Refreshing 0/${ids.length}…`;
  btn.disabled = true;
  let failed = 0;
  for (let i = 0; i < ids.length; i++) {
    btn.textContent = `Refreshing ${i + 1}/${ids.length}…`;
    try {
      const resp = await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' });
      if (!resp.ok) failed++;
    } catch (e) { failed++; }
  }
  btn.disabled = false;
  btn.textContent = 'Refresh';
  if (failed) alert(`${failed} of ${ids.length} character${ids.length !== 1 ? 's' : ''} could not be refreshed — usually an expired token (red dot). Re-add those characters via ESI to renew access.`);
  loadCharacters();
}

function esiLogin() {
  const w = window.open('/auth/login', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      loadCharacters();
    }
  });
}

// ── Product autocomplete ──────────────────────────────────────────────────────

async function loadPiProducts() {
  if (_ppProducts.length) return;
  try {
    const resp = await fetch('/api/pi-products');
    const data = await resp.json();
    _ppProducts = data.products || [];
    const dl = document.getElementById('productList');
    dl.innerHTML = '';
    // Fuel-block basket: a multi-product target, listed first for discoverability.
    const fbOpt = document.createElement('option');
    fbOpt.value = FUEL_BLOCK_LABEL;
    fbOpt.dataset.typeId = FUEL_BLOCK_TYPE_ID;
    fbOpt.dataset.tier = 'basket';
    dl.appendChild(fbOpt);
    _ppProducts.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.dataset.typeId = p.type_id;
      opt.dataset.tier = p.tier;
      dl.appendChild(opt);
    });
    // Custom production baskets (admin-defined, global), listed as basket targets too.
    await _refreshBaskets();
  } catch (e) { console.error('Failed to load products:', e); }
}

// Fetch custom baskets and (re)render their <option>s in the product datalist. Called on
// load and after an admin creates/edits/deletes a basket so the picker stays current.
async function _refreshBaskets() {
  try { _baskets = (await (await fetch('/api/baskets')).json()).baskets || []; }
  catch (e) { _baskets = []; }
  const dl = document.getElementById('productList');
  if (!dl) return;
  dl.querySelectorAll('option[data-basket-id]').forEach(o => o.remove());
  _baskets.forEach(b => {
    const opt = document.createElement('option');
    opt.value = b.name;
    opt.dataset.typeId = b.config_type_id;  // sentinel id for per-character config
    opt.dataset.basketId = b.id;
    opt.dataset.tier = 'basket';
    dl.appendChild(opt);
  });
}

// ── Constellation filter ──────────────────────────────────────────────────────

let _ppConstRegions = {};    // constellation -> region
let _ppConstByRegion = {};   // region -> [constellations]  (only regions in the Planet DB)
let _ppRegion = '';          // currently displayed region
let _ppSelected = new Set(); // selected constellation names (the filter)

async function loadConstellations() {
  try {
    const resp = await fetch('/api/constellations');
    const data = await resp.json();
    _ppConstRegions = data.regions || {};
    _ppConstByRegion = {};
    (data.constellations || []).forEach(c => {
      const r = _ppConstRegions[c] || 'Other';
      (_ppConstByRegion[r] ||= []).push(c);
    });
    Object.values(_ppConstByRegion).forEach(a => a.sort());
    try { _ppSelected = new Set(JSON.parse(localStorage.getItem('ppConstellations') || '[]')); }
    catch { _ppSelected = new Set(); }
    const regions = Object.keys(_ppConstByRegion).sort();
    const savedRegion = localStorage.getItem('ppRegion') || '';
    _ppRegion = regions.includes(savedRegion) ? savedRegion
      : (regions.find(r => _ppConstByRegion[r].some(c => _ppSelected.has(c))) || regions[0] || '');
    renderConstellations();
  } catch (e) { console.error('Failed to load constellations:', e); }
}

// Render the region dropdown + ONLY the chosen region's constellations (10-region
// Planet DBs are too heavy to render all at once).
function renderConstellations() {
  const card = document.getElementById('ppLocationCard');
  const list = document.getElementById('ppConstellationList');
  const filterEl = document.getElementById('ppRegionFilter');
  if (!card || !list) return;
  const regions = Object.keys(_ppConstByRegion).sort();
  if (!regions.length) { card.style.display = 'none'; return; }
  card.style.display = '';

  if (filterEl) {
    filterEl.innerHTML = `<span class="pp-region-label">Region</span>
      <select id="ppRegionSelect" class="pp-region-select">
        ${regions.map(r => `<option value="${_esc(r)}" ${r === _ppRegion ? 'selected' : ''}>${_esc(r)} (${_ppConstByRegion[r].length})</option>`).join('')}
      </select>`;
    filterEl.style.display = '';
    filterEl.querySelector('select').addEventListener('change', e => onRegionChange(e.target.value));
  }

  list.innerHTML = '';
  (_ppConstByRegion[_ppRegion] || []).forEach(c => {
    const label = document.createElement('label');
    label.className = 'pp-const-row';
    label.innerHTML = `<input type="checkbox" class="pp-const-cb" value="${_esc(c)}" ${_ppSelected.has(c) ? 'checked' : ''}> ${_esc(c)}`;
    label.querySelector('input').addEventListener('change', _onConstToggle);
    list.appendChild(label);
  });

  const body = document.getElementById('ppLocationBody');
  if (body && _ppSelected.size) {
    body.classList.remove('collapsed');
    body.style.maxHeight = 'none';
    const toggle = document.getElementById('ppLocationToggle');
    if (toggle) toggle.textContent = '▲';
  }
}

// Choosing a region filters to it: select all of that region's (owned) constellations.
function onRegionChange(region) {
  _ppRegion = region;
  _ppSelected = new Set(_ppConstByRegion[region] || []);
  _persistConstellations();
  renderConstellations();
}

function _onConstToggle(e) {
  if (e.target.checked) _ppSelected.add(e.target.value);
  else _ppSelected.delete(e.target.value);
  _persistConstellations();
}

function ppSelectAllConstellations(checked) {
  (_ppConstByRegion[_ppRegion] || []).forEach(c => {
    if (checked) _ppSelected.add(c); else _ppSelected.delete(c);
  });
  _persistConstellations();
  renderConstellations();
}

function _persistConstellations() {
  localStorage.setItem('ppConstellations', JSON.stringify([..._ppSelected]));
  localStorage.setItem('ppRegion', _ppRegion);
}

// Restore a selection (from a profile or share) and show the region it belongs to.
function _applyConstellationSelection(arr) {
  _ppSelected = new Set(arr || []);
  const owning = (arr || []).map(c => _ppConstRegions[c]).filter(Boolean);
  if (owning.length && _ppConstByRegion[owning[0]]) _ppRegion = owning[0];
  _persistConstellations();
  renderConstellations();
}

function ppToggleLocation() {
  const body   = document.getElementById('ppLocationBody');
  const toggle = document.getElementById('ppLocationToggle');
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (toggle) toggle.textContent = collapsed ? '▼' : '▲';
  if (!collapsed) body.style.maxHeight = body.scrollHeight + 'px';
  else body.style.maxHeight = '';
}

function getSelectedConstellations() {
  return [..._ppSelected];
}

// ── Planet list ───────────────────────────────────────────────────────────────

let _ppLoaded = false;

async function loadPlanets(force = false) {
  // Re-render instantly from cache on tab switches; only hit the network when
  // forced (after import/clear) or on the first load.
  if (!force && _ppLoaded) { filterPlanets(); return; }
  const container = document.getElementById('planetDbContent');
  container.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span>Loading planets…</div>';
  try {
    const resp = await fetch('/api/planets');
    const data = await resp.json();
    _ppPlanets = data.planets || [];
    _ppLoaded = true;
    filterPlanets();
  } catch (e) {
    console.error('Failed to load planets:', e);
    container.innerHTML = '<div class="pp-empty">Failed to load planets — check your connection and reopen this tab.</div>';
  }
}

// Debounce search input so we don't re-render the whole table on every keystroke.
let _ppFilterTimer = null;
function filterPlanetsDebounced() {
  clearTimeout(_ppFilterTimer);
  _ppFilterTimer = setTimeout(filterPlanets, 120);
}

// Filter the Planet DB table by system / planet type / constellation / region.
function filterPlanets() {
  const q = (document.getElementById('planetSearch')?.value || '').trim().toLowerCase();
  const terms = q.split(/\s+/).filter(Boolean);
  const filtered = !terms.length ? _ppPlanets : _ppPlanets.filter(p => {
    const region = _ppConstRegions[p.constellation] || '';
    const hay = `${p.system} ${p.planet_type} ${p.constellation} ${region} p${p.planet_num}`.toLowerCase();
    return terms.every(t => hay.includes(t));
  });
  renderPlanetTable(filtered);
  const el = document.getElementById('planetSearchCount');
  if (el) el.textContent = !_ppPlanets.length ? ''
    : (filtered.length === _ppPlanets.length ? `${_ppPlanets.length} planets`
       : `${filtered.length} of ${_ppPlanets.length}`);
}

// Lazy render: large Planet DBs (thousands of rows) freeze the page if built in
// one pass, so we render in chunks and append more as the user scrolls.
const _PP_CHUNK = 200;
let _ppRender = null;  // { rows, cursor, tbody, cols, wrap }

function renderPlanetTable(planets) {
  const container = document.getElementById('planetDbContent');
  _ppRender = null;

  if (!planets.length) {
    container.innerHTML = '<div class="pp-empty">No planets in database. Use Import to load your remote-sensing data.</div>';
    return;
  }

  const p0Keys = Object.keys(P0_LABEL);
  _ppActiveCols = p0Keys.filter(col => planets.some(p => (p[col] || 0) > 0));

  let head = '<tr>';
  ['System', 'Planet', 'Type', 'Constellation'].forEach(h => { head += `<th class="left">${h}</th>`; });
  _ppActiveCols.forEach(col => { head += `<th>${_esc(P0_LABEL[col])}</th>`; });
  head += '</tr>';

  container.innerHTML =
    `<div class="pp-planet-table-wrap"><table class="pp-planet-table">` +
    `<thead>${head}</thead><tbody></tbody></table></div>` +
    `<div class="pp-db-toolbar"><span>${planets.length} planet${planets.length !== 1 ? 's' : ''}</span></div>`;

  const wrap = container.querySelector('.pp-planet-table-wrap');
  const tbody = container.querySelector('tbody');
  _ppRender = { rows: planets, cursor: 0, tbody, cols: _ppActiveCols, wrap };
  _ppRenderChunk();

  wrap.onscroll = () => {
    if (!_ppRender) return;
    if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 300) _ppRenderChunk();
  };
}

function _ppRenderChunk() {
  const r = _ppRender;
  if (!r || r.cursor >= r.rows.length) return;
  const end = Math.min(r.cursor + _PP_CHUNK, r.rows.length);
  let html = '';
  for (let i = r.cursor; i < end; i++) {
    const p = r.rows[i];
    html += '<tr>' +
      `<td class="left">${_esc(p.system)}</td>` +
      `<td class="left">${_esc(String(p.planet_num))}</td>` +
      `<td class="left planet-type ${PP_TYPE_CLASS[p.planet_type] || ''}">${_esc(p.planet_type)}</td>` +
      `<td class="left">${_esc(p.constellation)}</td>`;
    for (const col of r.cols) {
      const v = p[col] || 0;
      html += v > 0 ? `<td class="p0-val">${v}</td>` : '<td class="p0-zero">—</td>';
    }
    html += '</tr>';
  }
  r.tbody.insertAdjacentHTML('beforeend', html);
  r.cursor = end;
  // Keep filling until the container is scrollable (so on-scroll loading can kick in).
  if (r.cursor < r.rows.length && r.wrap.scrollHeight <= r.wrap.clientHeight + 1) _ppRenderChunk();
}

async function clearPlanets() {
  if (!confirm('Clear all planet data?')) return;
  await fetch('/api/planets', { method: 'DELETE' });
  _ppPlanets = [];
  renderPlanetTable([]);
}

// ── Import modal ──────────────────────────────────────────────────────────────

function showPlanetImport() {
  document.getElementById('ppImportModal').style.display = 'flex';
  document.getElementById('ppImportText').value = '';
  document.getElementById('ppImportHint').textContent =
    'Paste your full spreadsheet including the header row.\n' +
    'Accepts tab-separated or comma-separated export.\n\n' +
    'Columns: System, Planet, then one column per P0 resource (header drives mapping).\n' +
    'Type and Constellation are optional — Constellation is derived from the system name, ' +
    'and Type is inferred from which P0 columns the planet fills.';
  document.getElementById('ppImportText').focus();
}

function closePlanetImport() {
  document.getElementById('ppImportModal').style.display = 'none';
}

async function submitPlanetImport() {
  const text = document.getElementById('ppImportText').value.trim();
  if (!text) return;
  const btn = document.getElementById('ppImportSubmitBtn');
  btn.disabled = true;
  btn.textContent = 'Importing…';
  try {
    const resp = await fetch('/api/planets/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    const warn = (data.errors && data.errors.length) ? '\n\nWarnings:\n' + data.errors.join('\n') : '';
    const importBtn = document.getElementById('ppImportBtn');
    if (data.queued) {
      // Non-admin: held for admin review, nothing written to the live DB yet.
      alert(`Thanks! ${data.submitted} planet${data.submitted === 1 ? '' : 's'} submitted for review. `
            + `An admin will approve them before they appear in the Planet DB.` + warn);
      importBtn.textContent = `✓ submitted`;
      closePlanetImport();
    } else {
      if (warn) alert(`Imported ${data.imported}, skipped ${data.skipped}.${warn}`);
      closePlanetImport();
      await loadPlanets(true);
      loadConstellations();
      importBtn.textContent = `✓ ${data.imported}`;
    }
    setTimeout(() => importBtn.textContent = 'Import', 2500);
  } catch (e) {
    alert('Import failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Import';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('ppImportModal').addEventListener('click', e => {
    if (e.target === document.getElementById('ppImportModal')) closePlanetImport();
  });

  // Wheel-scroll support for number inputs
  ['targetSystems', 'targetMaxJumps'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('wheel', e => {
      e.preventDefault();
      const min = parseInt(el.min) || 1;
      const max = parseInt(el.max) || 9999;
      const cur = parseInt(el.value) || parseInt(el.defaultValue) || min;
      el.value = Math.min(max, Math.max(min, cur + (e.deltaY < 0 ? 1 : -1)));
    }, { passive: false });
  });
});

function showAddPlanet() {
  alert('Manual planet entry coming soon.');
}

// ── Character roles ───────────────────────────────────────────────────────────

let _rolesTimeout = null;

async function _loadProductConfig(typeId, name) {
  _wiz.typeId = typeId;
  _wiz.productName = name;
  document.getElementById('ppRolesHint').textContent = '— ' + name;
  document.getElementById('ppRolesCard').style.display = '';
  await renderBasketToggles();
  try {
    const resp = await fetch(`/api/plan-config/${typeId}`);
    const data = await resp.json();
    renderRoles(data.configs, typeId);
  } catch (e) { console.error('Failed to load roles:', e); }
}

// ── Manufacturing material efficiency (fuel blocks only) ──────────────────────
let _mfgWired = false;

const _MFG_IDS = ['mfgStructure', 'mfgRig', 'mfgSpace', 'mfgBpme'];
const _RIG_BASE = { none: 0, t1: 2.0, t2: 2.4 };
const _SEC_MULT = { high: 1.0, low: 1.9, null: 2.1 };

// Plan-request values (server resolves 'auto' security from the system).
function _mfgValues() {
  return {
    structure_me_pct: parseFloat(document.getElementById('mfgStructure')?.value) || 0,
    rig_tier:         document.getElementById('mfgRig')?.value || 'none',
    rig_space:        document.getElementById('mfgSpace')?.value || 'auto',
    bp_me_pct:        parseFloat(document.getElementById('mfgBpme')?.value) || 0,
  };
}

// Raw select values, for localStorage / share persistence.
function _mfgRaw() {
  const o = {};
  _MFG_IDS.forEach(id => { const e = document.getElementById(id); if (e) o[id] = e.value; });
  return o;
}

function updateMfgEff() {
  const v = _mfgValues();
  const el = document.getElementById('mfgEff');
  if (!el) return;
  if (v.rig_space === 'auto' && v.rig_tier !== 'none') {
    // True % depends on the factory system's security (resolved server-side on run).
    const struct = 1 - v.structure_me_pct / 100, bp = 1 - v.bp_me_pct / 100;
    const lo = (1 - struct * (1 - _RIG_BASE[v.rig_tier] * 1.0 / 100) * bp) * 100;
    const hi = (1 - struct * (1 - _RIG_BASE[v.rig_tier] * 2.1 / 100) * bp) * 100;
    el.textContent = `= ${lo.toFixed(1)}–${hi.toFixed(1)}% (auto: depends on system sec)`;
  } else {
    const rig = _RIG_BASE[v.rig_tier] * (_SEC_MULT[v.rig_space] || 1);
    let keep = 1;
    [v.structure_me_pct, rig, v.bp_me_pct].forEach(x => keep *= 1 - Math.max(0, Math.min(90, x)) / 100);
    el.textContent = `= ${((1 - keep) * 100).toFixed(1)}% less materials`;
  }
  try { localStorage.setItem('ppMfg', JSON.stringify(_mfgRaw())); } catch (e) {}
}

function initMfgInputs() {
  if (!_mfgWired) {
    try {
      const s = JSON.parse(localStorage.getItem('ppMfg') || 'null');
      if (s) _MFG_IDS.forEach(id => { if (s[id] != null) { const e = document.getElementById(id); if (e) e.value = s[id]; } });
    } catch (e) {}
    _MFG_IDS.forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.addEventListener('input', updateMfgEff); el.addEventListener('change', updateMfgEff); }
    });
    _mfgWired = true;
  }
  updateMfgEff();
}

// Per-component import toggles + manufacturing efficiency — only for the built-in fuel-block
// basket. Custom baskets have no racial-block manufacturing step, so no ME / import toggles.
async function renderBasketToggles() {
  const card = document.getElementById('ppBasketCard');
  const list = document.getElementById('ppBasketList');
  const mfg = document.getElementById('ppMfgCard');
  const builtinFb = _wiz.fuelblock && !_wiz.basketId && !_wiz.inlineBasket;
  if (mfg) mfg.style.display = builtinFb ? '' : 'none';
  if (builtinFb) initMfgInputs();
  if (!builtinFb) { card.style.display = 'none'; return; }
  if (!_fbBom) {
    try { _fbBom = (await (await fetch('/api/fuelblock-bom')).json()).components || []; }
    catch (e) { _fbBom = []; }
  }
  card.style.display = '';
  list.innerHTML = _fbBom.map(c => {
    const checked = _wiz.importComponents.includes(c.type_id) ? 'checked' : '';
    const tierLbl = c.is_factory ? `P${c.tier} factory` : `P${c.tier} extractor`;
    return `<label class="pp-basket-item">
      <input type="checkbox" class="pp-basket-cb" data-tid="${c.type_id}" ${checked}>
      <span class="pp-basket-name">${_esc(c.name)}</span>
      <span class="pp-basket-tier">${tierLbl} · ${c.qty}/run</span>
    </label>`;
  }).join('');
  list.querySelectorAll('.pp-basket-cb').forEach(cb => {
    cb.addEventListener('change', () => {
      const tid = parseInt(cb.dataset.tid);
      const set = new Set(_wiz.importComponents);
      cb.checked ? set.add(tid) : set.delete(tid);
      _wiz.importComponents = [...set];
    });
  });
}

async function onProductChange() {
  const name = document.getElementById('targetProduct').value.trim();
  const typeId = _productTypeId(name);
  const card = document.getElementById('ppRolesCard');
  if (!typeId) {
    card.style.display = 'none';
    document.getElementById('ppBasketCard').style.display = 'none';
    _wiz.typeId = null; _wiz.fuelblock = false; _wiz.basketId = null; _wiz.inlineBasket = null;
    return;
  }
  _wiz.basketId = _basketIdFor(name);
  _wiz.inlineBasket = null;  // a manual pick always resolves a real (or built-in) basket
  _wiz.fuelblock = (typeId === FUEL_BLOCK_TYPE_ID || _wiz.basketId != null);
  _wiz.importComponents = [];  // fresh selection — produce everything by default
  await _loadProductConfig(typeId, name);
}

const _planetOpts = sel => [0,1,2,3,4,5,6]
  .map(n => `<option value="${n}"${n === sel ? ' selected' : ''}>${n}${n === 0 ? ' (off)' : ''}</option>`).join('');
const _ccuOpts = sel => [5,4,3,2,1]
  .map(n => `<option value="${n}"${n === sel ? ' selected' : ''}>${n}</option>`).join('');

function _markWhatif(planSel) {
  planSel.classList.toggle('pp-role-whatif',
    (parseInt(planSel.value) || 0) > parseInt(planSel.dataset.realMax || '6'));
}

function ppToggleRoles() {
  const body = document.getElementById('ppRolesBody');
  const toggle = document.getElementById('ppRolesToggle');
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (toggle) toggle.textContent = collapsed ? '▼' : '▲';
  body.style.maxHeight = collapsed ? '' : body.scrollHeight + 'px';
}

function _ppRolesSummary(configs, fb) {
  const planets = configs.reduce((s, c) =>
    s + ((c.planet_limit != null) ? c.planet_limit : c.max_planets), 0);
  let str = `${configs.length} chars · ${planets} planets`;
  if (fb) {
    const lv = new Set(configs.map(c => (c.ccu != null) ? c.ccu : ((c.esi_ccu && c.esi_ccu >= 1) ? c.esi_ccu : 5)));
    str += ` · CC ${lv.size === 1 ? [...lv][0] : 'mixed'}`;
  }
  return str;
}

function renderRoles(configs, typeId) {
  const list = document.getElementById('ppRolesList');
  const fb = _wiz.fuelblock;
  list.innerHTML = '';
  list.classList.toggle('with-cc', fb);

  const hint = document.getElementById('ppRolesHint');
  if (hint) hint.textContent = '— ' + _ppRolesSummary(configs, fb) + ' (defaults; click to tweak)';

  // Fleet-wide "set all" shortcut.
  const bar = document.createElement('div');
  bar.className = 'pp-role-setall';
  bar.innerHTML = `
    <span class="pp-setall-label">Set all</span>
    <select class="pp-setall-planets" title="Plan every character with this many planets">${_planetOpts(6)}</select><span class="pp-role-unit">pl</span>
    ${fb ? `<select class="pp-setall-ccu" title="Plan every character at this Command Center level">${_ccuOpts(5)}</select><span class="pp-role-unit">CC</span>` : ''}
    <button class="pp-setall-apply" type="button">Apply</button>
    <button class="pp-setall-reset" type="button" title="Reset every character back to their trained values (planets${fb ? ' and Command Center level' : ''}, all extractor-capable)">Reset to characters</button>`;
  bar.querySelector('.pp-setall-apply').addEventListener('click', () => {
    const pv = bar.querySelector('.pp-setall-planets').value;
    const cv = fb ? bar.querySelector('.pp-setall-ccu').value : null;
    list.querySelectorAll('.pp-role-row').forEach(row => {
      const ps = row.querySelector('.pp-role-planets');
      if (ps) { ps.value = pv; _markWhatif(ps); }
      const cs = row.querySelector('.pp-role-ccu');
      if (cs && cv != null) cs.value = cv;
    });
    scheduleRoleSave(typeId);
  });
  // Reset every row to the character's own trained values (planets = trained max, CC = the
  // ESI/trained level, factory-only off). This clears all per-character overrides.
  bar.querySelector('.pp-setall-reset').addEventListener('click', () => {
    list.querySelectorAll('.pp-role-row').forEach(row => {
      const ps = row.querySelector('.pp-role-planets');
      if (ps) { ps.value = ps.dataset.realMax; _markWhatif(ps); }
      const cs = row.querySelector('.pp-role-ccu');
      if (cs) cs.value = cs.dataset.def;              // back to the character's CC level
      const fc = row.querySelector('.pp-role-fac-cb');
      if (fc) fc.checked = false;
    });
    scheduleRoleSave(typeId);
  });
  list.appendChild(bar);

  // Column header.
  const head = document.createElement('div');
  head.className = 'pp-role-head';
  head.innerHTML = `<span class="pp-role-name">Character</span><span>Planets</span>${fb ? '<span>CC</span>' : ''}<span>Fac</span>`;
  list.appendChild(head);

  // Compact one-line rows. "Plan with" planets dropdown shows the trained max as "/N";
  // amber when set above it (a what-if for untrained Interplanetary Consolidation).
  configs.forEach(cfg => {
    const row = document.createElement('div');
    row.className = 'pp-role-row';
    row.dataset.charId = cfg.character_id;
    row.dataset.maxPlanets = cfg.max_planets;
    const planVal = (cfg.planet_limit !== null && cfg.planet_limit !== undefined)
      ? cfg.planet_limit : cfg.max_planets;
    const facOnly = cfg.extractor_limit === 0;
    const esiDef = (cfg.esi_ccu && cfg.esi_ccu >= 1) ? cfg.esi_ccu : 5;
    const ccuVal = (cfg.ccu != null) ? cfg.ccu : esiDef;
    const ccuCell = fb
      ? `<select class="pp-role-ccu" data-def="${esiDef}" title="Command Center level — fewer facilities fit at lower levels, so factory output drops">${_ccuOpts(ccuVal)}</select>`
      : '';
    row.innerHTML = `
      <span class="pp-role-name" title="${_esc(cfg.character_name)}">${cfg.character_name}</span>
      <span class="pp-role-ctl">
        <select class="pp-role-planets" data-real-max="${cfg.max_planets}"
                title="Planets to plan with (0 = exclude). Set above the trained ${cfg.max_planets} to model training Interplanetary Consolidation higher.">${_planetOpts(planVal)}</select>
        <span class="pp-role-trained-mini">/${cfg.max_planets}</span>
      </span>
      ${fb ? `<span class="pp-role-ctl">${ccuCell}</span>` : ''}
      <span class="pp-role-ctl"><input type="checkbox" class="pp-role-fac-cb" ${facOnly ? 'checked' : ''} title="Factory only — no extractors"></span>`;
    const planSel = row.querySelector('.pp-role-planets');
    _markWhatif(planSel);
    planSel.addEventListener('change', () => { _markWhatif(planSel); scheduleRoleSave(typeId); });
    row.querySelector('.pp-role-fac-cb').addEventListener('change', () => scheduleRoleSave(typeId));
    const ccuSel = row.querySelector('.pp-role-ccu');
    if (ccuSel) ccuSel.addEventListener('change', () => scheduleRoleSave(typeId));
    list.appendChild(row);
  });
}

function scheduleRoleSave(typeId) {
  if (_rolesTimeout) clearTimeout(_rolesTimeout);
  _rolesTimeout = setTimeout(() => saveRoles(typeId), 500);
}

async function saveRoles(typeId) {
  const rows = document.querySelectorAll('#ppRolesList .pp-role-row');
  const configs = Array.from(rows).map(row => {
    const maxPl = parseInt(row.dataset.maxPlanets);
    const pl = parseInt(row.querySelector('.pp-role-planets').value);
    const facOnly = row.querySelector('.pp-role-fac-cb').checked;
    // Only persist a CCU override when it differs from the natural default, so an
    // untouched picker doesn't pin the level against future ESI updates.
    const ccuSel = row.querySelector('.pp-role-ccu');
    let ccu = null;
    if (ccuSel && ccuSel.value !== ccuSel.dataset.def) ccu = parseInt(ccuSel.value);
    return {
      character_id:    parseInt(row.dataset.charId),
      // null only when using exactly the trained max (so it follows future training);
      // any other value — fewer, or a higher what-if — is stored explicitly.
      planet_limit:    (pl === maxPl) ? null : pl,
      extractor_limit: facOnly ? 0 : null,
      ccu,
    };
  });
  try {
    await fetch(`/api/plan-config/${typeId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ configs }),
    });
  } catch (e) { console.error('Failed to save roles:', e); }
}

// ── Profiles ──────────────────────────────────────────────────────────────────

let _ppProfiles = [];

async function loadProfiles() {
  try {
    const resp = await fetch('/api/profiles');
    const data = await resp.json();
    _ppProfiles = data.profiles || [];
    renderProfilesBar(_ppProfiles);
  } catch (e) { console.error('Failed to load profiles:', e); }
}

function renderProfilesBar(profiles) {
  const bar = document.getElementById('ppProfilesBar');
  const sel = document.getElementById('ppProfileSelect');
  if (!bar || !sel) return;
  if (!_loggedIn || !profiles.length) { bar.style.display = 'none'; return; }
  bar.style.display = '';
  sel.innerHTML = profiles.map(p => {
    const op = p.overproduction_pct ?? 10;
    const sys = p.preferred_systems || 1;
    const label = `${p.name}  —  ${p.type_name || '?'} · +${op}% overprod · ${sys} sys`;
    return `<option value="${p.id}">${_esc(label)}</option>`;
  }).join('');
}

function ppLoadSelectedProfile() {
  const sel = document.getElementById('ppProfileSelect');
  const id = sel && parseInt(sel.value);
  const profile = _ppProfiles.find(p => p.id === id);
  if (profile) _applyProfile(profile);
}

async function ppDeleteSelectedProfile() {
  const sel = document.getElementById('ppProfileSelect');
  const id = sel && parseInt(sel.value);
  const profile = _ppProfiles.find(p => p.id === id);
  if (!profile) return;
  if (!confirm(`Delete profile "${profile.name}"?`)) return;
  await fetch(`/api/profiles/${id}`, { method: 'DELETE' });
  loadProfiles();
}

async function _applyProfile(profile) {
  const basket = _basketById(_basketIdFromTid(profile.type_id));
  const isFuelBlock = profile.type_id === FUEL_BLOCK_TYPE_ID;
  const isFB = isFuelBlock || basket != null;
  _wiz.fuelblock = isFB;
  _wiz.basketId = basket ? basket.id : null;
  const prod = _ppProducts.find(p => p.type_id === profile.type_id);
  if (isFuelBlock) document.getElementById('targetProduct').value = FUEL_BLOCK_LABEL;
  else if (basket) document.getElementById('targetProduct').value = basket.name;
  else if (prod) document.getElementById('targetProduct').value = prod.name;
  document.getElementById('targetOverprod').value = profile.overproduction_pct ?? 10;
  document.getElementById('targetSystems').value = profile.preferred_systems;
  const _mdEl = document.getElementById('targetMinDensity');
  if (_mdEl) _mdEl.value = _wiz.minDensity || 0;
  const _mjEl = document.getElementById('targetMaxJumps');
  if (_mjEl) _mjEl.value = profile.max_jumps ?? 1;
  ppToggleMaxJumps();
  document.getElementById('targetUseExisting').checked = profile.use_existing !== false;
  const _frEl = document.getElementById('targetFactoryRate');
  if (_frEl) _frEl.value = profile.factory_output_per_hour ?? '';
  if (profile.constellations && profile.constellations.length) {
    _applyConstellationSelection(profile.constellations);
  }
  _wiz.chosenSystems = [];
  _wiz.factorySystem = profile.factory_system || '';
  _wiz.factoryCharIds = profile.factory_character_ids || [];
  _wiz.factoryPlanetTypes = (profile.factory_planet_types && profile.factory_planet_types.length)
    ? profile.factory_planet_types : ['Barren', 'Temperate'];
  _wiz.splitMode = (profile.split_mode && profile.split_mode !== 'off') ? 'on' : 'off';
  _wiz.distMode = (profile.distribution_mode === 'need') ? 'need' : 'stability';
  _wiz.minDensity = parseInt(profile.min_density_pct) || 0;
  _wiz.lastRecsData = null;
  _wiz.lastPlanData = null;
  if (isFuelBlock) await _loadProductConfig(FUEL_BLOCK_TYPE_ID, FUEL_BLOCK_LABEL);
  else if (basket) await _loadProductConfig(profile.type_id, basket.name);
  else if (prod) await _loadProductConfig(profile.type_id, prod.name);
  wizardGo(1);
}

async function wizardSaveProfile() {
  if (!_wiz.typeId) { alert('No product selected.'); return; }
  // Suggest a name from what's being planned: the product/basket, plus the region if chosen.
  const base = (_wiz.productName || 'Plan').replace(/\s*\(basket\)\s*$/i, '');
  const region = (typeof _ppRegion !== 'undefined' && _ppRegion) ? ' · ' + _ppRegion : '';
  const name = prompt('Profile name:', base + region);
  if (!name || !name.trim()) return;
  const payload = {
    name: name.trim(),
    type_id: _wiz.typeId,
    type_name: _wiz.productName,
    overproduction_pct: parseInt(document.getElementById('targetOverprod').value) || 10,
    preferred_systems: parseInt(document.getElementById('targetSystems').value) || 1,
    max_jumps: _maxJumps(),
    constellations: getSelectedConstellations(),
    use_existing: document.getElementById('targetUseExisting').checked,
    factory_system: _wiz.factorySystem || '',
    factory_output_per_hour: _factoryRate(),
    factory_character_ids: _wiz.factoryCharIds || [],
    factory_planet_types: _wiz.factoryPlanetTypes || ['Barren', 'Temperate'],
    split_mode: _wiz.splitMode || 'off',
    distribution_mode: _wiz.distMode || 'stability',
    min_density_pct: _wiz.minDensity || 0,
  };
  try {
    const resp = await fetch('/api/profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const btn = document.getElementById('wizSaveProfileBtn');
    btn.textContent = '✓ Saved';
    setTimeout(() => btn.textContent = 'Save as profile', 2000);
    loadProfiles();
  } catch (e) { alert('Save failed: ' + e.message); }
}

// ── Share URL (server-stored) ─────────────────────────────────────────────────

// Once a share is consumed (loaded or created), skip re-restoration for this session.
let _shareConsumed = false;

// Encode the current wizard state + computed plan into the v2 share/restore payload.
// Shared by Share links AND the auto-persist that restores your plan on refresh.
function _buildPlanPayload() {
  return {
    v: 2,
    tid: _wiz.typeId,
    pn: _wiz.productName,
    op: parseInt(document.getElementById('targetOverprod').value) ?? 10,
    ps: parseInt(document.getElementById('targetSystems').value) || 1,
    mj: _maxJumps(),
    ue: document.getElementById('targetUseExisting').checked,
    fs: _wiz.factorySystem || '',
    fr: _factoryRate(),            // factory output per hour override
    fc: _wiz.factoryCharIds || [], // chars prioritised to host factories
    cs: _wiz.chosenSystems,
    cc: getSelectedConstellations(),
    ic: _wiz.importComponents || [],  // fuel-block imported component ids
    fpt: _wiz.factoryPlanetTypes || ['Barren', 'Temperate'],  // allowed factory planet types
    sx: _wiz.splitMode || 'off',  // split-extraction mode
    dm: _wiz.distMode || 'stability',  // distribution method
    mdp: _wiz.minDensity || 0,  // min planet density % cap
    xns: !!_wiz.extractorNoStorage,  // storage-less extractor templates
    mfg: _wiz.fuelblock ? _mfgRaw() : null,  // manufacturing ME selects (raw, for restore)
    bk: _wiz.basketId ? _basketSnapshot(_wiz.basketId) : null,  // basket def for shared (private) baskets
    plan: _wiz.lastPlanData || null,
  };
}

// Keep the last computed plan in localStorage so a page refresh lands you back on it
// (no re-run needed). Restored by _tryRestoreLastPlan when there's no share link.
function _persistLastPlan() {
  try {
    if (_wiz.typeId && _wiz.lastPlanData) localStorage.setItem('ppLastPlan', JSON.stringify(_buildPlanPayload()));
  } catch (e) {}
}

async function wizardShare(includeDetails = false) {
  if (!_wiz.typeId) return;
  if (includeDetails && !confirm(
      'This full link embeds your character names, systems and planets. Anyone it is '
      + 'forwarded to could use in-game locator agents to find and camp you.\n\n'
      + 'Only send it to people you trust. Continue?')) return;
  const btnId = includeDetails ? 'wizShareFullBtn' : 'wizShareBtn';
  const btn = document.getElementById(btnId);
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Sharing…';
  const payload = _buildPlanPayload();
  try {
    const resp = await fetch('/api/pp-shares', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ payload, anonymize: !includeDetails }),
    });
    const data = await resp.json();
    const url = location.origin + '/s/' + data.id;
    // Copy the link to the clipboard but deliberately DON'T put it in the owner's address
    // bar — leaving /s/<id> there would force the plan back open on the next refresh even
    // after they've moved to another tab. Recipients open the copied link directly.
    let copied = true;
    try { await navigator.clipboard.writeText(url); } catch { copied = false; }
    if (!copied) prompt('Copy your share link:', url);
    btn.textContent = copied ? '✓ Copied' : 'Link ready';
    setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 2000);
  } catch (e) {
    alert('Share failed: ' + e.message);
    btn.textContent = label;
    btn.disabled = false;
  }
}

async function _tryRestoreFromHash() {
  if (_shareConsumed) return;
  // Share id can arrive three ways: injected by the /s/<id> server route (rich
  // preview path), a /s/<id> URL path, or the legacy #s=<id> hash fragment.
  let shareId = window.__SHARE_ID__ || '';
  if (!shareId) {
    const m = location.pathname.match(/^\/s\/([^/]+)$/);
    if (m) shareId = decodeURIComponent(m[1]);
  }
  if (!shareId && location.hash.startsWith('#s=')) shareId = location.hash.slice(3);
  if (!shareId) { await _tryRestoreLastPlan(); return; }
  try {
    const resp = await fetch(`/api/pp-shares/${shareId}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.payload && data.payload.tid) {
      _shareConsumed = true;
      await _restoreFromPayload(data.payload);
      // Consume the share link: strip the id from the URL so a refresh after the user
      // navigates to another tab doesn't force the plan again. The plan stays loaded for
      // this session; the shareable URL was already copied to the clipboard on share.
      window.__SHARE_ID__ = '';
      if (location.pathname.startsWith('/s/') || location.hash.startsWith('#s=')) {
        history.replaceState(null, '', '/');
      }
    }
  } catch (e) { console.error('Failed to load share:', e); }
}

// Restore the last plan from localStorage on a fresh page load (no share link). One-shot per
// load so navigating back to the tab mid-session doesn't yank you off whatever you're editing.
let _autoRestoreDone = false;
async function _tryRestoreLastPlan() {
  if (_autoRestoreDone || _shareConsumed) return;
  _autoRestoreDone = true;
  let payload = null;
  try { payload = JSON.parse(localStorage.getItem('ppLastPlan') || 'null'); } catch (e) { return; }
  if (!payload || !payload.tid || !payload.plan) return;
  _shareConsumed = true;  // reuse the share guard so a share link (if any) doesn't double-restore
  try { await _restoreFromPayload(payload); } catch (e) { console.error('Restore last plan failed:', e); }
}

async function _restoreFromPayload(payload) {
  const tidBasketId = _basketIdFromTid(payload.tid);
  const isFuelBlock = payload.tid === FUEL_BLOCK_TYPE_ID;
  const isBasket = tidBasketId != null;
  const localBasket = isBasket ? _basketById(tidBasketId) : null;
  // If the basket isn't one we can see, fall back to the embedded snapshot (shared link).
  const snapshot = (isBasket && !localBasket && payload.bk) ? payload.bk : null;
  const isFB = isFuelBlock || isBasket;
  const basketName = localBasket ? localBasket.name : (snapshot ? snapshot.name : 'Basket');
  const prod = isFuelBlock
    ? { type_id: FUEL_BLOCK_TYPE_ID, name: FUEL_BLOCK_LABEL }
    : (isBasket ? { type_id: payload.tid, name: basketName }
                : _ppProducts.find(p => p.type_id === payload.tid));
  if (!prod) return;
  _wiz.fuelblock = isFB;
  _wiz.basketId = localBasket ? localBasket.id : null;   // don't send a stale id we can't resolve
  _wiz.inlineBasket = snapshot;                          // re-run uses the embedded basket instead
  _wiz.importComponents = isFuelBlock ? (payload.ic || []) : [];
  if (isFuelBlock && payload.mfg) {
    try { localStorage.setItem('ppMfg', JSON.stringify(payload.mfg)); _mfgWired = false; } catch (e) {}
  }
  document.getElementById('targetProduct').value = prod.name;
  document.getElementById('targetOverprod').value = payload.op ?? 10;
  document.getElementById('targetSystems').value = payload.ps || 1;
  const _mjEl2 = document.getElementById('targetMaxJumps');
  if (_mjEl2) _mjEl2.value = payload.mj ?? 1;
  ppToggleMaxJumps();
  if (payload.ue !== undefined) document.getElementById('targetUseExisting').checked = payload.ue;
  if (payload.fs) _wiz.factorySystem = payload.fs;
  const _frEl = document.getElementById('targetFactoryRate');  // field removed; guard
  if (_frEl) _frEl.value = payload.fr ?? '';
  _wiz.factoryCharIds = payload.fc || [];
  _wiz.factoryPlanetTypes = (payload.fpt && payload.fpt.length)
    ? payload.fpt : ['Barren', 'Temperate'];
  _wiz.splitMode = (payload.sx && payload.sx !== 'off') ? 'on' : 'off';
  _wiz.distMode = (payload.dm === 'need') ? 'need' : 'stability';
  _wiz.minDensity = parseInt(payload.mdp) || 0;
  _wiz.extractorNoStorage = !!payload.xns;
  { const _ns = document.getElementById('targetNoStorage'); if (_ns) _ns.checked = _wiz.extractorNoStorage; }
  if (payload.cc && payload.cc.length) {
    _applyConstellationSelection(payload.cc);
  }
  // v2 shares include the full plan result — render directly without re-running
  if (payload.plan) {
    _wiz.typeId = payload.tid;
    _wiz.productName = prod.name;
    _wiz.chosenSystems = payload.cs || [];
    _wiz.lastPlanData = payload.plan;
    const sysLabel = _wiz.chosenSystems.length ? _wiz.chosenSystems.join(' + ') : 'no system filter';
    document.getElementById('wizTitle3').textContent = prod.name + ' · ' + sysLabel;
    renderFinalPlan(payload.plan);
    if (payload.anon) {
      const pc = document.getElementById('wizPlanContent');
      const note = document.createElement('div');
      note.className = 'pp-anon-note';
      note.textContent = 'Anonymized share — character names and locations have been removed by the owner.';
      pc.prepend(note);
    }
    wizardGo(3);
    return;
  }
  // v1 fallback: re-run the plan (requires login)
  await _loadProductConfig(payload.tid, prod.name);
  if (payload.cs && payload.cs.length) {
    _wiz.chosenSystems = payload.cs;
    await wizardChooseSystems(payload.cs);
  }
}

// ── Wizard: Find Systems (Step 1 → 2) ────────────────────────────────────────

// Build the {url, body} for a plan request. Branches single-product vs fuel-block basket.
function _planRequest(systemNames) {
  const body = {
    overproduction_pct:    parseInt(document.getElementById('targetOverprod').value) || 10,
    use_existing:          document.getElementById('targetUseExisting').checked,
    constellations:        getSelectedConstellations(),
    preferred_systems:     parseInt(document.getElementById('targetSystems').value) || 1,
    max_jumps:             _maxJumps(),
    chosen_systems:        systemNames || [],
    factory_system:        _wiz.factorySystem || '',
    factory_character_ids: _wiz.factoryCharIds || [],
    split_mode:            _wiz.splitMode || 'off',
    distribution_mode:     _wiz.distMode || 'stability',
    min_density_pct:       _wiz.minDensity || 0,
    extractor_no_storage:  !!_wiz.extractorNoStorage,
  };
  if (_wiz.fuelblock) {
    body.factory_planet_types = _wiz.factoryPlanetTypes || ['Barren', 'Temperate'];
    if (_wiz.basketId) {
      // Custom production basket: no racial block type, no manufacturing ME, no import toggles.
      body.basket_id = _wiz.basketId;
    } else if (_wiz.inlineBasket) {
      // Shared link whose basket we can't see — re-run from the embedded snapshot.
      body.inline_basket = _wiz.inlineBasket;
    } else {
      body.block_type = 'Oxygen';
      body.import_components = _wiz.importComponents || [];
      Object.assign(body, _mfgValues());
    }
    return { url: '/api/plan-fuelblock', body };
  }
  body.type_id = _wiz.typeId;
  body.factory_output_per_hour = _factoryRate();
  return { url: '/api/plan', body };
}

async function wizardFindSystems() {
  if (!_wiz.typeId) { alert('Select a product first.'); return; }
  // Clear any stale plan from a previous run before fetching fresh data
  _wiz.lastRecsData = null;
  _wiz.lastPlanData = null;
  _wiz.chosenSystems = [];
  const btn = document.getElementById('ppFindSystemsBtn');
  btn.disabled = true;
  btn.textContent = 'Finding…';
  try {
    const { url, body } = _planRequest([]);
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    _wiz.lastRecsData = data;
    renderRecommendations(data);
    wizardGo(2);
  } catch (e) {
    alert('Failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Find Systems →';
  }
}

// ── Wizard: Choose Systems (Step 2 → 3) ──────────────────────────────────────

async function wizardChooseSystems(systemNames) {
  // Disable all choose buttons while running
  document.querySelectorAll('.wiz-choose-btn').forEach(b => { b.disabled = true; });

  // If empty system list, just use the already-computed plan from step 2
  if (!systemNames || !systemNames.length) {
    _wiz.chosenSystems = [];
    _wiz.lastPlanData = _wiz.lastRecsData;
    const title = (_wiz.lastRecsData && _wiz.lastRecsData.product)
      ? _wiz.lastRecsData.product.name : '';
    document.getElementById('wizTitle3').textContent = title + ' · no system filter';
    renderFinalPlan(_wiz.lastPlanData);
    wizardGo(3);
    return;
  }

  _wiz.chosenSystems = systemNames;
  try {
    const { url, body } = _planRequest(systemNames);
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    _wiz.lastPlanData = data;
    const sysLabel = systemNames.length === 1 ? systemNames[0] : systemNames.join(' + ');
    document.getElementById('wizTitle3').textContent = data.product.name + ' · ' + sysLabel;
    renderFinalPlan(data);
    wizardGo(3);
  } catch (e) {
    alert('Plan failed: ' + e.message);
    document.querySelectorAll('.wiz-choose-btn').forEach(b => { b.disabled = false; });
  }
}

// ── Rendering helpers ─────────────────────────────────────────────────────────

function _productTypeId(name) {
  for (const opt of document.getElementById('productList').options)
    if (opt.value === name) return parseInt(opt.dataset.typeId);
  return null;
}

function _ptypeSpan(t) {
  return t ? `<span class="plan-ptype ${PP_TYPE_CLASS[t] || ''}">${t}</span>` : '';
}

// Spaced ISK style ("12.3 K") — the B/M/K logic lives once in app.js's fmtIsk (global,
// loaded first); only the sub-1k branch differs (keeps separators). NB: _iskFmt below is a
// DIFFERENT compact style ("12k", no space, signed) used by the Factory Layout cards.
function _fmtIsk(v) {
  return v >= 1e3 ? fmtIsk(v) : v.toLocaleString();
}

function _fmtHours(h) {
  if (h >= 48) return (h / 24).toFixed(1) + ' d';
  return h.toFixed(h < 10 ? 1 : 0) + ' h';
}

function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Step 2: System Recommendations ───────────────────────────────────────────

function renderRecommendations(data) {
  const content = document.getElementById('wizRecsContent');
  const statsStr = data.stats
    ? ` · ${data.stats.products_per_day.toLocaleString()} ${data.fuelblock ? 'blocks/day' : 'units/day'} · ${_fmtIsk(data.stats.isk_per_day)} ISK/day`
    : '';
  document.getElementById('wizTitle2').textContent = data.product.name + statsStr;

  if (!data.system_recommendations || !data.system_recommendations.length) {
    content.innerHTML = `
      <div class="plan-no-data" style="padding:20px 0">
        No system recommendations — import planet remote-sensing data on the Planet DB tab first.
      </div>
      <div style="padding:0 0 16px">
        <button class="wiz-choose-btn" onclick="wizardChooseSystems([])">Continue without system filter →</button>
      </div>`;
    return;
  }

  const maxJumps = _maxJumps();
  const anyWithin = data.system_recommendations.some(r => r.systems_needed.length > 1 && r.within_jumps);
  const fallbackNote = (maxJumps >= 0 && !anyWithin &&
                        data.system_recommendations.some(r => r.systems_needed.length > 1))
    ? `<div class="plan-jump-note">No multi-system combos within ${maxJumps} jump${maxJumps === 1 ? '' : 's'} cover your P0s — showing the closest available. Raise “Max jumps” to allow more spread.</div>`
    : '';

  const cards = data.system_recommendations.map((rec, i) => {
    const numSys = rec.systems_needed.length;
    const sysList = rec.systems_needed.join(' + ');
    const covBadge = rec.coverage >= rec.total_p0
      ? `<span class="plan-cov-full">full coverage</span>`
      : `<span class="plan-cov-partial">${rec.coverage}/${rec.total_p0} P0</span>`;
    const jumpBadge = numSys > 1
      ? (rec.within_jumps
          ? `<span class="plan-jump-ok">${(rec.jumps || 1) <= 1 ? 'adjacent' : rec.jumps + ' jumps'}</span>`
          : `<span class="plan-jump-far">not within ${maxJumps}</span>`)
      : '';
    const missingHtml = rec.missing.length
      ? `<div class="plan-rec-missing">Missing: ${rec.missing.join(', ')}</div>` : '';
    const rows = (rec.assignments || []).map(a =>
      `<div class="plan-rec-row">
        ${_ptypeSpan(a.planet_type)}<span class="plan-rec-p0">${a.p0_name}</span>
        <span class="plan-rec-arrow">→</span><span class="plan-rec-p1">${a.p1_name}</span>
        <span class="plan-rec-sys">${a.system} P${a.planet_num}</span>
        <span class="plan-rec-val">${a.value.toLocaleString()}</span>
      </div>`
    ).join('');
    // Factory planet availability per system — prefer rec.factory_capacity (per-rec data)
    const facCap = rec.factory_capacity || {};
    const facSystems = rec.systems_needed.filter(s => facCap[s] != null || facCap[s] === 0);
    const facHtml = facSystems.length
      ? `<div class="plan-rec-factories">${facSystems.map(s => {
          const cnt = facCap[s] || 0;
          const warn = cnt < 5 ? ' plan-rec-fac-low' : '';
          return `<span class="plan-rec-fac-entry${warn}">${s}: ${cnt} Barren/Temperate</span>`;
        }).join(' · ')}</div>` : '';
    const sysJson = JSON.stringify(rec.systems_needed).replace(/'/g, '\\\'');
    return `
      <div class="plan-rec-card${i === 0 ? ' plan-rec-best' : ''}">
        <div class="plan-rec-header">
          <span class="plan-rec-name">${numSys === 1 ? sysList : numSys + ' systems: ' + sysList}</span>
          ${covBadge}${jumpBadge}
          <button class="wiz-choose-btn" onclick='wizardChooseSystems(${JSON.stringify(rec.systems_needed)})'>Choose →</button>
        </div>
        ${missingHtml}
        <div class="plan-rec-rows">${rows}</div>
        ${facHtml}
      </div>`;
  }).join('');

  content.innerHTML = `
    ${fallbackNote}
    <div class="plan-rec-list">${cards}</div>
    <div class="wiz-skip-row">
      <button class="wiz-skip-btn" onclick="wizardChooseSystems([])">Continue without system filter →</button>
    </div>`;
}

// ── Step 3: Final Plan ────────────────────────────────────────────────────────

let _overprodTimer = null;  // debounce for the live overproduction control
let _minDensTimer = null;   // debounce for the live min-density control

// One planet hosting two extractors (split P1 production). Shows both P0→P1 legs with their
// recommended head split + per-leg quality. Head counts are guidance only (see tooltip).
function _splitExtRow(e) {
  const ptype = e.planet_type || '?';
  const sysHtml = e.system
    ? `<span class="plan-ext-sys">${e.system} P${e.planet_num}</span>`
    : `<span class="plan-ext-no-planet">no planet in system</span>`;
  const legHtml = (e.legs || []).map(l => {
    const q = l.quality_pct;
    const qc = q == null ? '' : (q >= 80 ? 'plan-qual-ok' : q >= 50 ? '' : 'plan-qual-low');
    const qHtml = q == null ? '' : `<span class="plan-ext-qual ${qc}">${q}</span>`;
    const name = l.p0_name
      ? `<span class="plan-ext-p0col">${_esc(l.p0_name)}</span><span class="plan-ext-p1sub"> → ${_esc(l.p1_name || '?')}</span>`
      : _esc(l.p1_name || '?');
    return `<span class="plan-split-leg"><span class="plan-ext-p1">${name}</span><span class="plan-split-heads" title="${l.heads}/10 extractor heads (guidance — actual yield varies with heatmap placement and depletion)">${l.heads}h</span>${qHtml}</span>`;
  }).join('<span class="plan-split-plus">+</span>');
  return `<div class="plan-ext-row plan-ext-split">
    <span class="plan-ext-tag plan-ext-split-tag" title="Split planet: two extractor control units share this planet's 10 heads, feeding two P1 lines. Head counts are a recommended split — real extraction depends on heatmap placement and depletes over time.">split</span>${_ptypeSpan(ptype)}${sysHtml}<span class="plan-ext-arrow">→</span>${legHtml}</div>`;
}

function setSplitMode(mode) {
  _wiz.splitMode = (mode && mode !== 'off') ? 'on' : 'off';
  _rerunPlan();
}

function setDistMode(mode) {
  _wiz.distMode = (mode === 'need') ? 'need' : 'stability';
  _rerunPlan();
}

// ── P1 stack splitter (PI Planner tab, driven by a saved plan) ─────────────────
// You collect stacks of P1 from extractors; this tells you exactly how many units of each to
// drop at each factory planet so a stack splits across the factories that consume it. Plans
// built in Planetary Planning are snapshotted to localStorage and listed in the PI Planner tab.
let _p1Stacks = {};        // type_id -> qty on hand (parsed from the shared inventory textarea)
let _p1NameToTid = {};     // lowercase P1 name -> type_id, for parsing the inventory
let _p1TidToName = {};     // type_id -> P1 name, for the "days of production" readout
let _planConsumption = {}; // type_id -> units/day the plan's factories eat (full rate)
let _planMeta = {};        // selected plan's production rate + sell value, for the refill stats bar
let _refillModel = null;   // [{title, cols:[{id,name}], facs:[{loc, char, shareByTid, amt}]}]
let _refillView = (() => { try { return localStorage.getItem('refillView') || 'summary'; } catch (e) { return 'summary'; } })();
let _refillChar = (() => { try { return localStorage.getItem('refillChar') || ''; } catch (e) { return ''; } })();
const _PLAN_SNAP_KEY = 'ppPlanSnapshots';

function _loadPlanSnapshots() {
  try { return JSON.parse(localStorage.getItem(_PLAN_SNAP_KEY) || '[]'); } catch (e) { return []; }
}

// Merged saved plans: server-saved (named, cross-device) first, then any unsaved last-built
// ones from this browser. Shared by the refill view and the page-1 "Saved plans" list.
async function _fetchAllSnapshots() {
  let server = [];
  try { server = (await (await fetch('/api/plan-snapshots')).json()).snapshots || []; } catch (e) {}
  const serverNames = new Set(server.map(s => s.name));
  return [
    ...server.map(s => ({ id: 'srv:' + s.id, srvId: s.id, name: s.name, factories: s.factories, consumption: s.consumption || {},
                          products_per_day: s.products_per_day, isk_per_day: s.isk_per_day, unit_label: s.unit_label,
                          factory_refill_hours: s.factory_refill_hours, factories_count: s.factories_count,
                          hasPayload: !!s.has_payload, saved: true })),
    ..._loadPlanSnapshots().filter(s => !serverNames.has(s.name)),
  ];
}

// Page-1 "Saved plans" list (Planetary Planning). Lists the refill snapshots so you can jump
// straight to refilling one without going through the PI Planner tab.
async function renderSavedPlansBar() {
  const el = document.getElementById('ppSavedPlansBar');
  if (!el) return;
  const snaps = await _fetchAllSnapshots();
  _analyzeSnaps = snaps;   // warm the Setup Analysis cache so its tab paints instantly
  if (!snaps.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';
  const rows = snaps.map(s => {
    const nfac = (s.factories || []).length;
    const ppd = s.products_per_day;
    const meta = `${nfac} factor${nfac === 1 ? 'y' : 'ies'}`
      + (ppd ? ` · ${Number(ppd).toLocaleString()} ${_esc(s.unit_label || 'units')}/day` : '');
    const del = `<button class="pp-profile-action-btn pp-profile-del-btn" onclick="deleteSavedPlan('${s.id}','${s.srvId || ''}')">Delete</button>`;
    const open = s.hasPayload
      ? `<button class="pp-profile-action-btn" onclick="openSavedPlanFull('${s.srvId || ''}')" title="Reopen the full allocation plan">Open plan</button>`
      : '';
    return `<div class="pp-saved-row">
        <span class="pp-saved-name">${_esc(s.name)}${s.saved ? '' : ' · this browser'}</span>
        <span class="pp-saved-meta">${meta}</span>
        <span class="pp-saved-actions">
          ${open}
          <button class="pp-profile-action-btn" onclick="openSavedPlanRefill('${s.id}')">Refill</button>
          ${del}
        </span>
      </div>`;
  }).join('');
  el.innerHTML = `<details class="pp-saved-fold"><summary>Saved plans (${snaps.length})</summary>
      <div class="pp-saved-list">${rows}</div></details>`;
}

// Reopen a saved plan as the full allocation view (Planetary Planning → Plan step). Pulls the
// stored v2 payload and restores it the same way a share link does.
async function openSavedPlanFull(srvId) {
  if (!srvId) return;
  let payload = null;
  try { payload = (await (await fetch(`/api/plan-snapshots/${srvId}`)).json()).payload; } catch (e) {}
  if (!payload || !payload.plan) {
    alert('This saved plan has no stored plan view — re-save it (Save plan) to enable Open.');
    return;
  }
  if (typeof switchTab === 'function') switchTab('planetary');
  await _restoreFromPayload(payload);
}

// Open a saved plan straight in the refill tool (PI Planner → Refill a plan, that plan selected).
function openSavedPlanRefill(id) {
  const sect = document.getElementById('planDistSection');
  if (sect) sect.dataset.sel = id;
  if (typeof switchTab === 'function') switchTab('planner');
  if (typeof setPiMode === 'function') setPiMode('refill');
}

async function deleteSavedPlan(id, srvId) {
  if (!confirm('Delete this saved plan?')) return;
  if (srvId) {
    try { await fetch(`/api/plan-snapshots/${srvId}`, { method: 'DELETE' }); } catch (e) {}
  } else {
    try { localStorage.setItem(_PLAN_SNAP_KEY, JSON.stringify(_loadPlanSnapshots().filter(s => s.id !== id))); } catch (e) {}
  }
  renderSavedPlansBar();
  if (document.getElementById('planDistSection')) renderPlanDistribution();
}

// ── Setup Analysis tab ───────────────────────────────────────────────────────
// Maps what the player's colonies ACTUALLY produce per P1 (units/day from the ESI forward-sim,
// summed across every character & planet) against a saved plan's required P1 (units/day the
// factories eat at full rate). Answers "am I extracting/producing enough to keep this plan's
// factories refilled?" with headline stats + per-P1 bars.
let _analyzeSnaps = [];
// Show days normally, but drop to hours when it's under a day (otherwise it rounds to "0 days").
function _dur(days) {
  if (days >= 1) { const d = days >= 10 ? Math.round(days) : Math.round(days * 10) / 10; return `${d.toLocaleString()} ${d === 1 ? 'day' : 'days'}`; }
  const h = days * 24, hr = h >= 10 ? Math.round(h) : Math.round(h * 10) / 10;
  return `${hr.toLocaleString()} ${hr === 1 ? 'hour' : 'hours'}`;
}

// Per-material drill-down: every colony producing a given output, weakest (underperforming) first.
let _anExpanded = new Set();   // material type_ids whose planet breakdown is expanded
function _planetsForMaterial(t) {
  const out = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    if (String(o.type_id) === String(t))
      out.push({ char: ch.name, system: p.system, planet_num: p.planet_num, perDay: o.per_day || 0 });
  })));
  out.sort((a, b) => a.perDay - b.perDay);   // underperforming (lowest output) first
  return out;
}
function _toggleMatDetail(t) {
  t = String(t);
  if (_anExpanded.has(t)) _anExpanded.delete(t); else _anExpanded.add(t);
  renderAnalysis();
}

// type_id(str) -> {name, perDay, planets} — every colony output rolled up (P1 for refiners, P0 for
// pure extractors), with how many planets contribute (to size "add N planets" suggestions). The
// plan's needs pick which rows are relevant.
function _setupProductionByP1() {
  const out = {};
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    const k = String(o.type_id);
    if (!out[k]) out[k] = { name: o.name, perDay: 0, planets: 0 };
    out[k].perDay += o.per_day || 0;
    out[k].planets += 1;
  })));
  return out;
}

// type_id(str) -> [{cid, char, system, planet_num, perDay}] — every colony that outputs that
// material, so rebalance suggestions can name the exact planet/character to move.
function _setupPlanetsByMaterial() {
  const idx = {};
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    const k = String(o.type_id);
    (idx[k] = idx[k] || []).push({ cid: ch.character_id, char: ch.name, system: p.system, planet_num: p.planet_num, perDay: o.per_day || 0 });
  })));
  return idx;
}

// Placeability for the destination materials: which Planet-DB planets each character could actually
// colonise for a given P1's P0 (reachable system, not already used). Fetched per plan, then the
// rebalance moves only suggest a redeploy the character can physically make.
let _placements = null;       // {type_id: {p0_name, by_char: {cid: [{system,planet_num,planet_type,richness}]}}}
let _factorySites = null;     // {cid: [{system, planet_num, planet_type}]} — free B/T planets for a new factory
let _placementsKey = null;
async function _ensurePlacements(typeIds) {
  const key = typeIds.slice().sort().join(',');
  if (_placementsKey === key) return;     // already loaded for this plan
  _placementsKey = key;
  try {
    const r = await fetch('/api/analyze-placements', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                       body: JSON.stringify({ type_ids: typeIds.map(Number) }) });
    const j = await r.json();
    _placements = j.placements || {};
    _factorySites = j.factory_sites || {};
  } catch (e) { _placements = {}; _factorySites = {}; }
  renderAnalysis();   // re-render now that feasibility is known
}

function _snapNeedsByP1(snap) {
  const out = {};
  const c = snap && snap.consumption;
  if (Array.isArray(c)) c.forEach(x => { if (x.units_per_day > 0) out[String(x.p1_type_id)] = { name: x.p1_name, perDay: x.units_per_day }; });
  else if (c) Object.keys(c).forEach(t => { if (c[t] > 0) out[t] = { name: 'P1 ' + t, perDay: c[t] }; });
  return out;
}

function _renderAnalyzePlans() {
  const sel = document.getElementById('analyzePlanSelect');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = _analyzeSnaps.length
    ? _analyzeSnaps.map((s, i) => `<option value="${i}">${s.derived ? '◆ ' : ''}${_esc(s.name)}</option>`).join('')
    : '<option value="">— no plans —</option>';
  if (prev !== '' && _analyzeSnaps[prev]) sel.value = prev;
}

// Derive a demand profile per product the player's deployed factories build (server-side from
// the live colony scan), shaped like a saved snapshot so the Analyze comparison works unchanged.
async function _fetchSetupPlans() {
  try {
    const plans = ((await (await fetch('/api/my-setup-plan')).json()).plans) || [];
    return plans.map((p, i) => ({ ...p, id: 'setup:' + i, derived: true, saved: false, hasPayload: false }));
  } catch (e) { return []; }
}

// Pull fresh colony data from ESI (re-scans each real character's planets → rebuilds sim_state),
// then re-render. This is what actually updates the production rates; the plain reopen only re-reads
// the DB. Used by the tab's "Rescan colonies" button.
async function rescanAndAnalyze(btn) {
  const ids = (_ppCharsData || []).filter(c => !c.is_dummy && c.character_id > 0).map(c => c.character_id);
  if (!ids.length) { await onAnalyzeTabOpen(); return; }
  const orig = btn ? btn.textContent : '';
  if (btn) btn.disabled = true;
  let failed = 0;
  for (let i = 0; i < ids.length; i++) {
    if (btn) btn.textContent = `Rescanning ${i + 1}/${ids.length}…`;
    try { const r = await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' }); if (!r.ok) failed++; }
    catch (e) { failed++; }
  }
  if (btn) { btn.textContent = orig || 'Rescan colonies'; btn.disabled = false; }
  await onAnalyzeTabOpen();
  if (failed) alert(`${failed} of ${ids.length} character${ids.length !== 1 ? 's' : ''} could not be rescanned — usually an expired ESI token (red dot in Characters).`);
}

// Paint instantly from cache (colony data + snapshots are usually warm from the Planetary tab),
// then refresh in the background — no need to hit Reload to see anything.
async function onAnalyzeTabOpen() {
  _renderAnalyzePlans();
  if (_ppCharsData.length || _analyzeSnaps.length) renderAnalysis();
  else {
    const el = document.getElementById('analyzeContent');
    if (el) el.innerHTML = '<div class="admin-hint">Loading your colonies and saved plans…</div>';
  }
  await loadCharacters();          // refresh the live production rates
  let saved = [], derived = [];
  try { saved = await _fetchAllSnapshots(); } catch (e) {}
  try { derived = await _fetchSetupPlans(); } catch (e) {}
  _analyzeSnaps = [...derived, ...saved];   // your live setup first, then saved plans
  _renderAnalyzePlans();
  renderAnalysis();
}

function renderAnalysis() {
  const el = document.getElementById('analyzeContent');
  if (!el) return;
  if (!_analyzeSnaps.length) {
    el.innerHTML = `<div class="admin-hint">Nothing to compare against yet. Either <b>set a recipe on a factory</b> in-game (then <b>Rescan colonies</b> to get a "Current setup" demand profile), or build a plan in <b>Planetary Planning</b> and <b>Save plan</b>.</div>`;
    return;
  }
  const sel = document.getElementById('analyzePlanSelect');
  const snap = _analyzeSnaps[parseInt(sel && sel.value, 10) || 0];
  if (!snap) { el.innerHTML = ''; return; }
  const needs = _snapNeedsByP1(snap);
  const prod = _setupProductionByP1();
  const needKeys = Object.keys(needs);

  if (!Object.keys(prod).length) {
    el.innerHTML = `<div class="admin-hint">No colony production data. Add your characters and refresh in the <b>Characters</b> tab (logged in with ESI access), then reload.</div>`;
    return;
  }
  if (!needKeys.length) {
    el.innerHTML = `<div class="admin-hint">This saved plan predates the per-P1 consumption data. Re-open it in Planetary Planning and <b>Save plan</b> again to enable the analysis.</div>`;
    return;
  }

  // Per-P1 rows for exactly the P1 this plan consumes; worst-fed first.
  const rows = needKeys.map(t => {
    const need = needs[t].perDay;
    const have = (prod[t] && prod[t].perDay) || 0;
    const ratio = need > 0 ? have / need : (have > 0 ? Infinity : 1);
    return { t, name: needs[t].name, have, need, ratio };
  }).sort((a, b) => a.ratio - b.ratio);

  const binding = rows[0];
  const feedPct = Math.round(Math.min(1, binding.ratio) * 100);
  const fed = binding.ratio >= 0.995;
  const rh = snap.factory_refill_hours;
  const refillDays = rh ? rh / 24 : null;

  const stat = (val, lbl, cls) => `<div class="an-stat ${cls || ''}"><div class="an-stat-val">${val}</div><div class="an-stat-lbl">${lbl}</div></div>`;
  const head = `<div class="an-headline ${fed ? 'an-ok' : 'an-bad'}">
      <div class="an-head-word">${fed ? 'KEEPS UP' : 'FALLS BEHIND'}</div>
      <div class="an-head-sub">Your colonies produce <b>${feedPct}%</b> of what “${_esc(snap.name)}” needs to stay fed${fed ? `, and refill the factories every <b>${refillDays ? _dur(refillDays) : '—'}</b>.` : ` — short on <b>${_esc(binding.name)}</b>.`}</div>
    </div>`;

  // Achievable final output is throttled by the scarcest input: the factories can only run at the
  // binding P1's feed ratio, so actual product/day (and its ISK) = target × feed ratio.
  const feedRatio = Math.min(1, binding.ratio);
  const unit = snap.unit_label || 'units';
  let stats = `<div class="an-stats">`;
  stats += stat(feedPct + '%', 'Plan fed at', fed ? 'an-ok' : 'an-bad');
  if (snap.products_per_day) {
    const target = Number(snap.products_per_day), actual = Math.round(target * feedRatio);
    stats += stat(`${actual.toLocaleString()} <span class="an-of">/ ${target.toLocaleString()}</span>`,
                  `${unit}/day · making / target`, fed ? 'an-ok' : 'an-bad');
  }
  if (snap.isk_per_day)
    stats += stat(_fmtIsk(snap.isk_per_day * feedRatio),
                  `ISK/day${fed ? '' : ' · ' + _fmtIsk(snap.isk_per_day) + ' if fed'}`, '');
  // Surplus headroom in END-PRODUCT terms. The extra product the surplus could ACTUALLY become is
  // capped by the LOWEST-overproduced input — you need every input in the right ratio, so total
  // surplus mass badly overstates it (tons of spare Oxidizing is useless if Industrial Fibers has
  // only ~22 SHPC of headroom). So we take the min over inputs of (surplus ÷ per-product need).
  if (snap.products_per_day) {
    let bind = null;
    rows.forEach(r => {
      const perProduct = r.need / snap.products_per_day;   // input units per 1 product
      if (perProduct <= 0) return;
      const hp = (r.have - r.need) / perProduct;           // surplus expressed as products
      if (bind === null || hp < bind.hp) bind = { hp, name: r.name };
    });
    if (bind && bind.hp >= 1) {
      const n = Math.round(bind.hp);
      const iskPerProduct = (snap.isk_per_day || 0) / snap.products_per_day;
      const iskpart = iskPerProduct > 0 ? ` · ≈${_fmtIsk(n * iskPerProduct)} ISK` : '';
      stats += stat(`≈${n.toLocaleString()}`, `more ${_esc(unit)}/day your surplus could feed · capped by ${_esc(bind.name)}${iskpart}`, 'an-warn');
    }
  }
  stats += `</div>`;

  // Refill cadence = factory P1 buffer (3 launchpads = 30,000 m³) ÷ consumption (P1/day × 0.19 m³).
  let proj = '';
  {
    const nfac = snap.factories_count || (snap.factories || []).length || 0;
    const totP1 = Array.isArray(snap.consumption)
      ? snap.consumption.reduce((a, c) => a + (c.units_per_day || 0), 0)
      : Object.values(snap.consumption || {}).reduce((a, v) => a + (v || 0), 0);
    const perFacP1 = nfac ? totP1 / nfac : 0;
    const perFacM3 = perFacP1 * 0.19;
    const days = perFacM3 ? 30000 / perFacM3 : 0;
    if (perFacP1) {
      const cells = [
        stat(_dur(days), 'between refills · 3 LP', 'an-ok'),
        stat(`~${Math.round(perFacM3).toLocaleString()} <span class="an-of">m³/day</span>`, 'of P1 per factory to haul', ''),
        nfac ? stat(String(nfac), 'factories to service', '') : '',
      ].join('');
      proj = `<div class="an-proj">`
        + `<div class="an-proj-h">Refill run — empty extractors & top up factories every <b>${_dur(days)}</b></div>`
        + `<div class="an-stats">${cells}</div></div>`;
    }
  }

  const barRows = rows.map(r => {
    const surplus = r.have - r.need;
    const cls = r.ratio >= 0.995 ? 'an-bar-ok' : (r.ratio >= 0.85 ? 'an-bar-warn' : 'an-bar-bad');
    const haveW = Math.max(2, Math.min(100, (r.have / r.need) * 100));
    const delta = surplus >= 0
      ? `<span class="an-pos">+${Math.round(surplus).toLocaleString()}/day</span>`
      : `<span class="an-neg">${Math.round(surplus).toLocaleString()}/day</span>`;
    const expanded = _anExpanded.has(String(r.t));
    let detail = '';
    if (expanded) {
      const pls = _planetsForMaterial(r.t);
      const items = pls.length
        ? pls.map(p => `<div class="an-pd-row"><span class="an-pd-char">${_esc(p.char)}</span><span class="an-pd-loc">${p.system ? _esc(p.system) + (p.planet_num != null ? ' P' + p.planet_num : '') : '—'}</span><span class="an-pd-val">${Math.round(p.perDay).toLocaleString()}/day</span></div>`).join('')
        : '<div class="an-pd-empty">No colony is producing this yet.</div>';
      detail = `<div class="an-row-detail">${items}</div>`;
    }
    return `<div class="an-mat">
        <div class="an-row an-row-click" onclick="_toggleMatDetail('${r.t}')">
          <div class="an-row-name"><span class="an-row-caret">${expanded ? '▾' : '▸'}</span> ${_esc(r.name)}</div>
          <div class="an-bar-track"><div class="an-bar-fill ${cls}" style="width:${haveW}%"></div></div>
          <div class="an-row-nums"><span class="an-have">${Math.round(r.have).toLocaleString()}</span><span class="an-sep">/</span><span class="an-need">${Math.round(r.need).toLocaleString()}/day</span>${delta}</div>
        </div>${detail}
      </div>`;
  }).join('');

  // ── Rebalance moves ─────────────────────────────────────────────────────────
  // You can't change a planet's richness — you only learn it once deployed. What's actionable is
  // moving a surplus colony onto a short material (1 colony slot = 1 move). Size everything in
  // PLANETS off each material's own per-planet output (a P1 not extracted yet falls back to the
  // average of what you do produce).
  const rates = needKeys.map(t => prod[t]).filter(p => p && p.planets > 0).map(p => p.perDay / p.planets);
  const fallbackRate = rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : 7680;
  const perPlanet = t => (prod[t] && prod[t].planets > 0) ? prod[t].perDay / prod[t].planets : fallbackRate;

  const deficits = rows.filter(r => r.ratio < 0.995)
    .map(r => ({ name: r.name, t: r.t, gap: r.need - r.have, per: perPlanet(r.t),
                 planets: Math.max(1, Math.ceil((r.need - r.have) / perPlanet(r.t))) }))
    .sort((a, b) => b.planets - a.planets);
  const surplus = rows.filter(r => r.ratio >= 1.25)
    .map(r => ({ name: r.name, t: r.t, per: perPlanet(r.t), ratio: r.ratio,
                 spare: Math.floor((r.have - r.need) / perPlanet(r.t)) }))
    .filter(s => s.spare >= 1)
    .sort((a, b) => b.ratio - a.ratio);   // most over-produced first → free those, keep scarcer ones

  // Validate placeability: ask the backend which planets each character could actually colonise for
  // the short materials' P0 (reachable system, carries the P0, not already used). Re-renders when ready.
  _ensurePlacements(needKeys);

  // Free colonies from the MOST over-produced material first (surplus is sorted by ratio), and within
  // a material take the weakest colony (keep your best producers). A move is only valid if the freed
  // colony's CHARACTER can place the short material somewhere reachable — so we pair surplus colonies
  // with that char's free destination planets and consume both as we assign.
  const planetIdx = _setupPlanetsByMaterial();
  const spareLeft = {};
  const colsByMat = {};
  surplus.forEach(s => { spareLeft[s.t] = s.spare; colsByMat[s.t] = (planetIdx[s.t] || []).slice().sort((a, b) => a.perDay - b.perDay); });
  const usedKey = new Set();
  const ckey = c => `${c.cid}:${c.system}:${c.planet_num}`;
  // Per-(short material, character) free destination planets, consumed as assigned.
  const destPool = {};
  if (_placements) Object.keys(_placements).forEach(t => {
    destPool[t] = {};
    Object.entries(_placements[t].by_char).forEach(([cid, arr]) => { destPool[t][cid] = arr.slice(); });
  });
  const p0Of = t => (_placements && _placements[t] && _placements[t].p0_name) || null;

  const moves = [];        // {colony, fromName, to, dest|null}
  deficits.forEach(d => {
    let need = d.planets;
    for (const s of surplus) {                               // most over-produced material first
      if (need <= 0) break;
      for (const c of colsByMat[s.t]) {
        if (need <= 0 || spareLeft[s.t] <= 0) break;
        if (usedKey.has(ckey(c))) continue;
        let dest = null;
        if (_placements) {
          const dests = destPool[d.t] && destPool[d.t][String(c.cid)];
          if (!dests || !dests.length) continue;             // this character can't place the short P0
          dest = dests.shift();                              // claim that char's richest free planet
        }
        usedKey.add(ckey(c)); spareLeft[s.t]--; need--;
        moves.push({ colony: c, fromName: s.name, fromT: s.t, to: d.name, toT: d.t, dest });
      }
    }
    d.unmet = need;     // still short: no feasible surplus colony / no reachable free planet
  });
  const newBuilds = deficits.filter(d => d.unmet > 0);
  const leftover = surplus.filter(s => spareLeft[s.t] >= 1).map(s => ({ name: s.name, spare: spareLeft[s.t] }));
  const movedTotal = moves.length;

  // material label as "P0 → P1" (matching the plan), falling back to just P1 if the P0 isn't known.
  const _matHtml = (p1name, t) => {
    const p0 = p0Of(t);
    return p0 ? `${_esc(p0)} <span class="an-move-p0arrow">→</span> ${_esc(p1name)}` : _esc(p1name);
  };
  const _moveSide = (cls, tag, loc, matHtml, note) =>
    `<div class="an-move-side ${cls}"><span class="an-move-tag">${tag}</span>`
    + (loc ? `<span class="an-move-loc">${loc}</span>` : '')
    + `<span class="an-move-mat">${matHtml}</span><span class="an-sug-note">${note}</span></div>`;
  const _moveLi = (m) => {
    const c = m.colony;
    const fromLoc = c.system ? `${_esc(c.system)}${c.planet_num != null ? ' P' + c.planet_num : ''}` : '';
    const rm = _moveSide('an-move-rm', 'tear down', fromLoc, _matHtml(m.fromName, m.fromT), `${Math.round(c.perDay).toLocaleString()}/day`);
    const add = m.dest
      ? _moveSide('an-move-add', 'build', `${_esc(m.dest.system)} P${m.dest.planet_num} <span class="an-cc-tag">${_esc(m.dest.planet_type)}</span>`, _matHtml(m.to, m.toT), `richness ${m.dest.richness}%`)
      : _moveSide('an-move-add', 'build', '', _matHtml(m.to, m.toT), 'on a free planet');
    return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div>`
      + `<div class="an-move-pair">${rm}<div class="an-move-arrow">→</div>${add}</div></li>`;
  };

  // Over-producing every input? The surplus could feed MORE factories. To host one, a factory
  // character needs (a) a free Barren/Temperate planet — preferring the system its factories are
  // already in — and (b) a colony slot: a spare one if under max planets, else freed by tearing down
  // an over-produced extractor (which also trims waste). Maxed chars with nothing over-produced to
  // free, or no free B/T planet, can't host one.
  const curFac = snap.factories_count || (snap.factories || []).length || 0;
  // Round (not floor) the surplus headroom to whole factories so this agrees with the over-extraction
  // tile: e.g. 22 SHPC of headroom = 1.83 factories → 2, not floor→1.
  const extraFac = Math.round(curFac * (binding.ratio - 1));
  const supportable = curFac + extraFac;
  let addFactories = '';
  if (curFac && binding.ratio > 1.005 && extraFac >= 1) {
    const facChars = new Set((snap.factories || []).map(f => (f.loc || '').split(' · ')[0].trim()).filter(Boolean));
    const facSystems = new Set((snap.factories || []).map(f => ((f.loc || '').split('·')[1] || '').trim().split(' ')[0]).filter(Boolean));
    const ratioOf = {}; surplus.forEach(s => { ratioOf[String(s.t)] = s.ratio; });
    const surplusTids = new Set(surplus.map(s => String(s.t)));
    const pn = snap.unit_label && snap.unit_label !== 'units' ? _esc(snap.unit_label) : '';
    const facLabel = pn ? `${pn} factory` : 'factory';

    // Per factory character: spare slots, free B/T destinations (factory system first), and the
    // over-produced extractors we could tear down to free a slot (most over-produced first).
    const chars = (_ppCharsData || []).filter(c => facChars.has(c.name)).map(c => {
      const teardowns = [];
      (c.planets || []).forEach(p => {
        if (!p.is_extractor) return;
        (p.production || []).forEach(o => {
          if (surplusTids.has(String(o.type_id)))
            teardowns.push({ system: p.system, planet_num: p.planet_num, ptype: p.planet_type,
                             mat: o.name, t: String(o.type_id), ratio: ratioOf[String(o.type_id)] || 1 });
        });
      });
      teardowns.sort((a, b) => b.ratio - a.ratio);
      const dests = ((_factorySites && _factorySites[String(c.character_id)]) || []).slice()
        .sort((a, b) => (facSystems.has(b.system) ? 1 : 0) - (facSystems.has(a.system) ? 1 : 0));
      return { name: c.name, spare: (c.max_planets || 0) - (c.planets || []).length, dests, teardowns };
    });

    // Place extraFac factories round-robin across characters (spread, less disruption per char), on
    // distinct destination planets, freeing a slot where the char is maxed.
    const usedDest = new Set();
    const cards = [];
    let remaining = extraFac, progress = true;
    while (remaining > 0 && progress) {
      progress = false;
      for (const ch of chars) {
        if (remaining <= 0) break;
        if (ch.spare <= 0 && ch.teardowns.length === 0) continue;   // maxed, nothing to free
        // next free destination not already claimed
        let dest = null;
        while (ch.dests.length) {
          const d = ch.dests[0];
          if (usedDest.has(`${d.system} P${d.planet_num}`)) { ch.dests.shift(); continue; }
          dest = d; break;
        }
        if (!dest) continue;
        ch.dests.shift();
        const teardown = ch.spare > 0 ? (ch.spare--, null) : ch.teardowns.shift();
        usedDest.add(`${dest.system} P${dest.planet_num}`);
        cards.push({ char: ch.name, dest, teardown });
        remaining--; progress = true;
      }
    }

    const cardLi = cards.map(c => {
      const loc = `${_esc(c.dest.system)} P${c.dest.planet_num} <span class="an-cc-tag">${_esc(c.dest.planet_type)}</span>`;
      const note = facSystems.has(c.dest.system) ? '' : 'not in your factory system';
      const build = _moveSide(c.teardown ? 'an-move-add' : 'an-move-add an-move-solo',
                              c.teardown ? 'build factory' : 'build new', loc, facLabel, note);
      if (c.teardown) {
        const t = c.teardown;
        const rm = _moveSide('an-move-rm', 'free a slot', `${_esc(t.system)} P${t.planet_num}`, _matHtml(t.mat, t.t), `${_esc(t.ptype)} extractor`);
        return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div><div class="an-move-pair">${rm}<div class="an-move-arrow">→</div>${build}</div></li>`;
      }
      return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div><div class="an-move-pair">${build}</div></li>`;
    });
    if (remaining > 0) cardLi.push(`<li class="an-move-note">+ ${remaining} more couldn't be placed — your factory characters are at max planets with no over-produced colony to free (or no free Barren/Temperate planet). You'd need another character or to free a slot.</li>`);
    addFactories = `<div class="an-suggest an-suggest-add"><div class="an-suggest-h">Add factories — your inputs could feed ~${supportable} (${extraFac} more than ${curFac}), capped by ${_esc(binding.name)}</div><ul>${cardLi.join('')}</ul></div>`;
  }

  let suggest = '';
  if (moves.length) {
    suggest += `<div class="an-suggest an-suggest-move"><div class="an-suggest-h">Rebalance — redeploy ${movedTotal} colon${movedTotal === 1 ? 'y' : 'ies'}</div><ul>${moves.map(_moveLi).join('')}</ul></div>`;
  }
  if (addFactories) suggest += addFactories;
  if (newBuilds.length) {
    const li = newBuilds.map(d => {
      const p0 = p0Of(d.t);
      const why = _placements ? (p0 ? `no free ${_esc(p0)} planet reachable by a character with a spare colony` : 'no reachable planet free') : 'no surplus to move';
      return `<li><b>${_esc(d.name)}</b> — still short <b>${Math.round(d.unmet * d.per).toLocaleString()}/day</b> (${d.unmet} planet${d.unmet === 1 ? '' : 's'}) <span class="an-sug-note">(${why})</span></li>`;
    }).join('');
    suggest += `<div class="an-suggest an-suggest-fix"><div class="an-suggest-h">Can't rebalance — still short</div><ul>${li}</ul></div>`;
  }
  if (leftover.length && !addFactories) {
    const li = leftover.map(s => `<li><b>${_esc(s.name)}</b> — <b>${s.spare}</b> planet${s.spare === 1 ? '' : 's'} still spare (nothing short needs them)</li>`).join('');
    suggest += `<div class="an-suggest an-suggest-free"><div class="an-suggest-h">Leftover surplus</div><ul>${li}</ul></div>`;
  }
  if (!moves.length && !newBuilds.length && !addFactories)
    suggest = `<div class="an-suggest an-suggest-free"><div class="an-suggest-h">Balanced — every material this plan needs is covered${leftover.length ? ', with a little to spare' : ''}.</div></div>`;

  // Extraction-runtime advice: longest program that keeps extraction above factory demand
  // (+ safety buffer), anchored on the measured headroom and your detected current program.
  _extRt = { ppd: Number(snap.products_per_day) || 0, ipd: Number(snap.isk_per_day) || 0, unit };
  const rtAdvice = _extRuntimeAdviceHtml(binding.ratio, _currentProgramDays());

  el.innerHTML = head + stats + proj
    + `<div class="an-legend">Producing (left) vs the plan’s daily need (right) per P1. A full green bar = factories stay fed; a short red bar is the bottleneck.</div>`
    + `<div class="an-bars">${barRows}</div>`
    + suggest + rtAdvice;
}

// ── Extraction-runtime helper (decay-aware recommendation) ────────────────────
let _extRt = null;   // {ppd, ipd, unit} of the currently-shown plan, for live recompute
// EVE extractors front-load yield and decay over the program (per-cycle ≈ peak/(1+k·t)). The
// AVERAGE over a program of length L hours = peak · ln(1+k·L)/(k·L). k is the decay factor —
// this is an ESTIMATE (validate against your in-game ECU graph); it's one constant to retune.
const _EXT_DECAY_K = 0.012;   // per hour
const _EXT_MAX_DAYS = 14;     // EVE extraction-program cap

function _extEff(days) {       // average yield as a fraction of peak over a `days`-long program
  const h = Math.max(0, days) * 24;
  return h > 0 ? Math.log(1 + _EXT_DECAY_K * h) / (_EXT_DECAY_K * h) : 1;
}
function _extRtLine(n) {       // production figures for an n-day program (peak rate = plan rate)
  const eff = _extEff(n), ppd = _extRt ? _extRt.ppd : 0;
  const avgDay = ppd * eff;
  return { eff, pct: Math.round(eff * 100), avgDay, total: avgDay * n,
           isk: (_extRt && _extRt.ipd) ? _extRt.ipd * eff * n : 0 };
}

// Whole-day-multiple program duration minus 30 min headroom (so it ends a touch earlier each
// day for a same-time restart). N days → "1d 23h 30m" (N=2), "23h 30m" (N=1).
function _progDuration(days) {
  const n = Math.max(1, Math.min(60, Math.round(days)));
  const total = n * 24 * 60 - 30;
  const d = Math.floor(total / 1440), h = Math.floor((total % 1440) / 60), m = total % 60;
  return [d ? d + 'd' : '', h ? h + 'h' : '', m ? m + 'm' : ''].filter(Boolean).join(' ');
}
// Representative current extraction-program length (median across the deployed extractors),
// from ESI install→expiry. null if no colony data yet.
function _currentProgramDays() {
  const vals = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => {
    if (p.is_extractor && p.program_days > 0) vals.push(p.program_days);
  }));
  if (!vals.length) return null;
  vals.sort((a, b) => a - b);
  return vals[Math.floor(vals.length / 2)];
}

// Solid, low-touch advice: the longest extractor program that keeps extraction above factory
// demand (with a safety buffer). While extraction ≥ demand the factories stay fed, so final
// output holds at the planned rate — a longer program only buys fewer restarts. Past the point
// where the decaying average drops below demand, output starts falling.
//   headroom = supply ÷ demand at the CURRENT program (the analysis "fed at" ratio, uncapped)
//   curDays  = detected current program length (anchor for the decay)
// Line graph of extractor yield decline vs how long heads run before reseating. Two curves —
// raw per-cycle yield (drops fast) and the running average the factories actually see (the pads
// buffer it, so it drops slower) — against the factory-demand line. Where the average crosses
// demand is when, if you don't reseat, the factories start falling behind.
function _extDecayGraphSvg(headroom, L0, safe, edge) {
  const W = 600, H = 190, ml = 40, mr = 14, mt = 14, mb = 26, XMAX = _EXT_MAX_DAYS;
  const x0 = ml, x1 = W - mr, y0 = mt, y1 = H - mb;
  const sx = d => x0 + (d / XMAX) * (x1 - x0);
  const sy = v => y0 + (1 - Math.max(0, Math.min(1, v))) * (y1 - y0);
  const demand = Math.max(0, Math.min(1, _extEff(L0) / headroom));
  const path = fn => { let p = ''; for (let d = 0; d <= XMAX; d += 0.5) p += (d === 0 ? 'M' : 'L') + sx(d).toFixed(1) + ',' + sy(fn(d)).toFixed(1) + ' '; return p.trim(); };
  const grid = [0, 0.5, 1].map(v => `<line x1="${x0}" y1="${sy(v).toFixed(1)}" x2="${x1}" y2="${sy(v).toFixed(1)}" class="g-grid"/><text x="${x0 - 4}" y="${(sy(v) + 3).toFixed(1)}" class="g-ylab">${v * 100 | 0}%</text>`).join('');
  const xlab = [0, 7, 14].map(d => `<text x="${sx(d).toFixed(1)}" y="${H - 8}" class="g-xlab">${d}d</text>`).join('');
  const zone = edge < XMAX ? `<rect x="${sx(edge).toFixed(1)}" y="${y0}" width="${(x1 - sx(edge)).toFixed(1)}" height="${(y1 - y0).toFixed(1)}" class="g-bad"/>` : '';
  const mk = (d, cls, lbl) => (d >= 1 && d <= XMAX) ? `<line x1="${sx(d).toFixed(1)}" y1="${y0}" x2="${sx(d).toFixed(1)}" y2="${y1}" class="${cls}"/><text x="${sx(d).toFixed(1)}" y="${(y1 - 3).toFixed(1)}" class="g-mklab">${lbl}</text>` : '';
  return `<svg class="ext-graph" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      ${zone}${grid}${xlab}
      <line x1="${x0}" y1="${sy(demand).toFixed(1)}" x2="${x1}" y2="${sy(demand).toFixed(1)}" class="g-demand"/>
      <text x="${x1}" y="${(sy(demand) - 3).toFixed(1)}" class="g-dlab" text-anchor="end">factory demand</text>
      <path d="${path(d => 1 / (1 + _EXT_DECAY_K * d * 24))}" class="g-inst"/>
      <path d="${path(d => _extEff(d))}" class="g-avg"/>
      ${mk(L0, 'g-now', 'now')}${mk(safe, 'g-pick', 'pick')}
    </svg>
    <div class="ext-graph-leg"><span><i class="g-k g-k-avg"></i>average fed to factories</span><span><i class="g-k g-k-inst"></i>raw per-cycle yield</span><span><i class="g-k g-k-demand"></i>demand</span></div>`;
}

function _extRuntimeAdviceHtml(headroom, curDays) {
  if (!_extRt || !_extRt.ppd || !headroom) return '';
  const unit = _extRt.unit || 'units', ppd = Math.round(_extRt.ppd).toLocaleString();
  const L0 = (curDays && curDays > 0) ? Math.round(curDays) : 2;
  const detected = !!(curDays && curDays > 0), e0 = _extEff(L0);
  const buffer = d => headroom * _extEff(d) / e0 - 1;     // extraction over demand at a d-day program
  const maxDayFor = m => { let b = 0; for (let d = 1; d <= _EXT_MAX_DAYS; d++) if (buffer(d) >= m) b = d; return b; };
  const safe = maxDayFor(0.10), edge = maxDayFor(0.0);    // ≥10% buffer vs right at demand

  if (safe < 1) {   // can't keep any buffer even at the shortest program → under-supplied
    return `<div class="an-suggest an-suggest-fix">
        <div class="an-suggest-h">Extraction runtime</div>
        <div class="an-rt-pick">Extraction covers only <b>${Math.round(headroom * 100)}%</b> of demand on ${detected ? `your ~${L0}-day` : `a ~${L0}-day`} program — under a safe margin.</div>
        <ul><li>Shorten the program or add extraction (see above) before the factories fall behind.</li></ul>
      </div>`;
  }

  const start = Math.max(1, Math.min(L0, safe) - 1);
  const end = Math.min(_EXT_MAX_DAYS, Math.max(edge + 1, safe + 2));
  let rows = '';
  for (let d = start; d <= end; d++) {
    const b = buffer(d);
    const pct = (b >= 0 ? '+' : '−') + Math.round(Math.abs(b) * 100) + '%';
    const cls = b >= 0.10 ? 'an-rt-ok' : b >= 0 ? 'an-rt-warn' : 'an-rt-bad';
    const tags = [];
    if (d === safe) tags.push('recommended');
    if (detected && d === L0) tags.push('now');
    rows += `<tr class="${d === safe ? 'an-rt-row-rec' : ''}">`
      + `<td>${d}d</td><td class="an-rt-mono">${_progDuration(d)}</td>`
      + `<td class="${cls}">${pct}</td><td class="an-rt-tag">${tags.join(' · ')}</td></tr>`;
  }
  const pick = (detected && safe < L0) ? `Shorten to <b>${safe} day${safe === 1 ? '' : 's'}</b>` : `Run <b>${safe} day${safe === 1 ? '' : 's'}</b>`;
  return `<div class="an-suggest an-suggest-add">
      <div class="an-suggest-h">Extraction runtime</div>
      <div class="an-rt-pick">${pick} — set programs to <b>${_progDuration(safe)}</b>; output holds at <b>${ppd}</b> ${_esc(unit)}/day.</div>
      <table class="an-rt-tbl">
        <thead><tr><th>Run</th><th>Program</th><th class="an-rt-num">Buffer</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${_extDecayGraphSvg(headroom, L0, safe, edge)}
      <div class="an-sug-note">Buffer = how far the program's average extraction sits above factory demand (below 0 = factories starve).${detected ? '' : ' Current length assumed 2d — rescan to detect yours.'} Decay is an estimate — verify against your in-game ECU.</div>
    </div>`;
}

function _buildPlanSnapshot(data) {
  const factories = [];
  for (const a of (data.assignments || []))
    for (const f of (a.factory_assignments || []))
      if (f.p1_inputs && f.p1_inputs.length && f.system)  // placed factories only
        factories.push({
          loc: `${a.character_name} · ${f.system}${f.planet_num != null ? ' P' + f.planet_num : ''}`,
          product: f.product ? f.product.name : 'Factory',
          p1_inputs: f.p1_inputs.map(p => ({ p1_type_id: p.p1_type_id, p1_name: p.p1_name, share: p.share })),
        });
  if (!factories.length) return null;
  // Per-P1 daily consumption (units/day at full factory rate) so the refill tool can show how
  // long a pasted P1 stash lasts before a refill. Stored with names so the readout is
  // self-contained (no cross-lookup needed).
  const consumption = (data.p1_requirements || [])
    .filter(r => r.units_per_day != null)
    .map(r => ({ p1_type_id: r.p1_type_id, p1_name: r.p1_name, units_per_day: r.units_per_day }));
  const st = data.stats || {};
  return {
    name: (data.product && data.product.name) || 'Plan',
    factories, consumption,
    // Production rate + daily sell value so the refill view can show units made and ISK over a run.
    products_per_day: st.products_per_day,
    isk_per_day: st.isk_per_day,
    unit_label: data.fuelblock ? (st.unit_label || 'fuel blocks') : 'units',
    // Refill cadence + factory count so the Setup Analysis tab can frame "refill every X days".
    factory_refill_hours: st.factory_refill_hours,
    factories_count: factories.length,
  };
}

// localStorage keeps the last-built plan per product (a quick, unsaved fallback). Explicit
// saves go server-side via savePlanForRefills (cross-device, named).
function _storePlanSnapshot(data) {
  const snap = _buildPlanSnapshot(data);
  if (!snap) return;
  const entry = { ...snap, id: 'local:' + snap.name, savedAt: Date.now(), local: true };
  let snaps = _loadPlanSnapshots().filter(s => s.name !== snap.name);
  snaps.unshift(entry);
  try { localStorage.setItem(_PLAN_SNAP_KEY, JSON.stringify(snaps.slice(0, 8))); } catch (e) {}
}

async function savePlanForRefills() {
  const data = _wiz.lastPlanData;
  const snap = data && _buildPlanSnapshot(data);
  if (!snap) { alert('No placed factories in this plan to save.'); return; }
  if (!_loggedIn) {
    alert('Log in to save plans across devices.\n\nYour last plan is already kept in this browser, so it\'s in the PI Planner tab without re-running the wizard.');
    return;
  }
  const name = prompt('Save this plan as:', snap.name);
  if (!name || !name.trim()) return;
  snap.name = name.trim();
  snap.payload = _buildPlanPayload();  // full plan so it can be reopened (not just refilled)
  const btn = document.getElementById('savePlanBtn');
  try {
    const resp = await fetch('/api/plan-snapshots', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: snap.name, snapshot: snap }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    if (btn) { const t = btn.textContent; btn.textContent = '✓ Saved'; setTimeout(() => { btn.textContent = t; }, 2000); }
    renderSavedPlansBar();  // keep the page-1 list current
  } catch (e) { alert('Save failed: ' + e.message); }
}

async function deletePlanSnapshot(srvId) {
  if (!confirm('Delete this saved plan?')) return;
  try { await fetch(`/api/plan-snapshots/${srvId}`, { method: 'DELETE' }); } catch (e) {}
  const el = document.getElementById('planDistSection');
  if (el) el.dataset.sel = '';
  renderPlanDistribution();
}

// PI Planner has two tools fed by the one inventory paste: "Find buildable stuff"
// (analyze/optimize) and "Refill a plan" (split P1 stacks into a saved plan's factories).
// A mode switch keeps them apart so pasting doesn't dump every table at once.
let _piMode = (() => { try { return localStorage.getItem('piMode') || 'build'; } catch (e) { return 'build'; } })();

function setPiMode(mode) {
  _piMode = (mode === 'refill') ? 'refill' : 'build';
  const build = document.getElementById('piModeBuild');
  const refill = document.getElementById('piModeRefill');
  if (build) build.style.display = _piMode === 'build' ? '' : 'none';
  if (refill) refill.style.display = _piMode === 'refill' ? '' : 'none';
  // Highlight the active sidebar sub-item (both share data-tab="planner").
  document.querySelectorAll('.tab[data-pimode]').forEach(b =>
    b.classList.toggle('active', b.dataset.pimode === _piMode));
  try { localStorage.setItem('piMode', _piMode); } catch (e) {}
  if (_piMode === 'refill') renderPlanDistribution();  // (re)build tables + sync from inventory
}

// Called when the PI Planner tab opens (wired in app.js switchTab).
function onPlannerTabOpen() { setPiMode(_piMode); }

function onPlanDistSelect(id) {
  const el = document.getElementById('planDistSection');
  if (el) el.dataset.sel = id;
  renderPlanDistribution();
}

async function renderPlanDistribution() {
  const el = document.getElementById('planDistSection');
  if (!el) return;
  // Saved plans + the live "Current setup" profiles derived from your deployed factories
  // (same ones the Setup Analysis tab offers) — so you can refill what you actually run
  // without first saving a plan. Derived plans are marked with a ◆ and can't be deleted.
  const saved = await _fetchAllSnapshots();
  let derived = [];
  try { derived = await _fetchSetupPlans(); } catch (e) {}
  // Current setup first → it's the sensible default selection (your live factories) when present.
  const snaps = [...derived, ...saved];
  if (!snaps.length) {
    el.innerHTML = `<div class="plan-section-title">Split P1 stacks into plan factories</div>
      <div class="pp-card"><div class="admin-hint">Build a plan in <b>Planetary Planning</b> and hit <b>Save plan</b> — or set up factories on your characters — and it'll appear here so you can paste your P1 stacks and see exactly how many units to drop at each factory.</div></div>`;
    return;
  }
  const snap = snaps.find(s => String(s.id) === el.dataset.sel) || snaps[0];
  const opts = snaps.map(s =>
    `<option value="${s.id}"${String(s.id) === String(snap.id) ? ' selected' : ''}>${s.derived ? '◆ ' : ''}${_esc(s.name)}${(!s.saved && !s.derived) ? ' · this browser (unsaved)' : ''}</option>`).join('');
  const delBtn = snap.saved
    ? `<button class="pp-profile-action-btn pp-profile-del-btn" onclick="deletePlanSnapshot(${snap.srvId})" title="Delete this saved plan">Delete</button>` : '';
  // P1 name → type_id (for parsing the pasted inventory — the textarea is the single input).
  const p1s = {};
  snap.factories.forEach(f => f.p1_inputs.forEach(p => { p1s[p.p1_type_id] = p.p1_name; }));
  _p1NameToTid = {};
  Object.keys(p1s).forEach(tid => { _p1NameToTid[p1s[tid].toLowerCase()] = tid; });
  _p1TidToName = p1s;
  _planConsumption = snap.consumption || {};  // only on plans saved after this feature shipped
  _planMeta = { productsPerDay: snap.products_per_day || 0, iskPerDay: snap.isk_per_day || 0,
                unitLabel: snap.unit_label || 'units',
                count: snap.factories_count || (snap.factories ? snap.factories.length : 1),
                refillHours: snap.factory_refill_hours || 0 };

  // Build a render model: one group per product, each with its P1 columns and factory rows
  // (parsed character + per-P1 share). The split math (updateP1Distribution) runs over this model
  // — independent of what's displayed — so the per-character filter / summary view can never skew
  // the global split.
  const groups = {};
  snap.factories.forEach(f => {
    const key = f.product || ('sig:' + f.p1_inputs.map(p => p.p1_type_id).slice().sort((a, b) => a - b).join(','));
    if (!groups[key]) groups[key] = { title: f.product || '', facs: [] };
    groups[key].facs.push(f);
  });
  _refillModel = Object.keys(groups).map(key => {
    const g = groups[key];
    const colMap = {};
    g.facs.forEach(f => f.p1_inputs.forEach(p => { colMap[String(p.p1_type_id)] = p.p1_name; }));
    const cols = Object.keys(colMap).sort((a, b) => colMap[a].localeCompare(colMap[b])).map(id => ({ id: id, name: colMap[id] }));
    const facs = g.facs.map(f => {
      const loc = f.loc || f.label || '?';
      const shareByTid = {};
      f.p1_inputs.forEach(p => { shareByTid[String(p.p1_type_id)] = p.share; });
      return { loc, char: _refillCharOf(loc), shareByTid, amt: {},
               inputM3: (typeof f.input_m3 === 'number') ? f.input_m3 : null };  // P1 already in the LP (live setups only)
    });
    return { title: g.title || cols.map(c => c.name).join(' + '), cols, facs };
  });

  el.innerHTML = `
    <section class="pp-card dist-block">
      <div class="pp-card-title">Refill split
        <span class="pp-card-hint">— split your pasted P1 across the plan's factories</span>
        <select class="dist-plan-select" onchange="onPlanDistSelect(this.value)">${opts}</select>
        ${delBtn}
      </div>
      <div class="pp-card-body">
        <div class="dist-controls" id="refillControls"></div>
        <div class="dist-hint">Tops up each factory's launchpads toward full (3 LP ≈ 30,000 m³) in recipe ratio, <b>counting the P1 already in them</b> — so you never overflow. <b>Summary</b> = one drop that fits every factory; <b>All planets</b> = each topped to its own free space. Limited by the P1 in your <b>inventory above</b>. Click a number to copy.</div>
        <div class="dist-days" id="refillDays"></div>
        <div class="plan-dist-tables" id="refillTables"></div>
      </div>
    </section>`;
  el.dataset.sel = snap.id;
  _renderRefillControls();
  syncRefillFromInventory();  // compute amounts from the shared inventory paste, then render
}

// Character = the part of a factory loc before " · " (e.g. "Ekaoni · 0-U2M4 P6" → "Ekaoni").
function _refillCharOf(loc) { return (loc.split(' · ')[0] || '').trim(); }
function _setRefillChar(v) { _refillChar = v; try { localStorage.setItem('refillChar', v); } catch (e) {} _renderRefillTables(); }
function _setRefillView(v) { _refillView = v; try { localStorage.setItem('refillView', v); } catch (e) {} _renderRefillControls(); _renderRefillTables(); }

function _renderRefillControls() {
  const el = document.getElementById('refillControls');
  if (!el || !_refillModel) return;
  const chars = [...new Set(_refillModel.flatMap(g => g.facs.map(f => f.char)))].filter(Boolean).sort();
  if (_refillChar && !chars.includes(_refillChar)) _refillChar = '';   // stale selection
  const total = _refillModel.reduce((a, g) => a + g.facs.length, 0);
  const charSel = chars.length > 1
    ? `<label class="dist-ctl">Character
        <select onchange="_setRefillChar(this.value)">
          <option value="">All characters (${total})</option>
          ${chars.map(c => `<option value="${_esc(c)}"${c === _refillChar ? ' selected' : ''}>${_esc(c)}</option>`).join('')}
        </select></label>` : '';
  el.innerHTML = `${charSel}
    <div class="dist-view-toggle">
      <button class="dist-view-btn${_refillView === 'summary' ? ' on' : ''}" onclick="_setRefillView('summary')" title="One uniform drop that fits every factory">Summary</button>
      <button class="dist-view-btn${_refillView === 'detailed' ? ' on' : ''}" onclick="_setRefillView('detailed')" title="Each factory topped to its own free launchpad space">All planets</button>
    </div>`;
}

// One P1 cell. undefined share → factory doesn't use this input (·); null amount → no stock yet (–).
function _amtCell(fac, colId) {
  if (!(colId in fac.shareByTid)) return '<td class="dist-num p1-cell-na">·</td>';
  const v = fac.amt[colId];
  if (v == null) return '<td class="dist-num"><b class="p1-amt">–</b></td>';
  return `<td class="dist-num"><b class="p1-amt p1-amt-set" onclick="copyP1Amount(this)" title="Click to copy">${v.toLocaleString()}</b></td>`;
}

function _renderRefillTables() {
  const host = document.getElementById('refillTables');
  if (!host || !_refillModel) return;
  const summary = _refillView !== 'detailed';
  host.innerHTML = _refillModel.map(g => {
    const facs = _refillChar ? g.facs.filter(f => f.char === _refillChar) : g.facs;
    const head = `<thead><tr><th>${summary ? 'Per factory' : 'Factory planet'}</th>${g.cols.map(c => `<th class="dist-num">${_esc(c.name)}</th>`).join('')}</tr></thead>`;
    let body;
    if (!facs.length) {
      body = `<tr><td class="p1-dist-loc" colspan="${g.cols.length + 1}"><span class="p1-cell-na">No factories for this character.</span></td></tr>`;
    } else if (summary) {
      // One row: the uniform top-up sized to the fullest factory, so the same drop fits every one.
      const cells = g.cols.map(c => {
        const v = _refillUniform[c.id];
        return (v == null) ? '<td class="dist-num"><b class="p1-amt">–</b></td>'
          : `<td class="dist-num"><b class="p1-amt p1-amt-set" onclick="copyP1Amount(this)" title="Click to copy">${v.toLocaleString()}</b></td>`;
      }).join('');
      body = `<tr><td class="p1-dist-loc">Each factory <span class="dist-line-count">× ${facs.length}</span></td>${cells}</tr>`;
    } else {
      body = facs.map(f => `<tr><td class="p1-dist-loc">${_esc(f.loc)}</td>${g.cols.map(c => _amtCell(f, c.id)).join('')}</tr>`).join('');
    }
    return `<div class="dist-line">
        <div class="dist-line-head">${_esc(g.title)}<span class="dist-line-count">${facs.length} planet${facs.length !== 1 ? 's' : ''}${_refillChar ? ' · ' + _esc(_refillChar) : ''}</span></div>
        <table class="p1-dist-table">${head}<tbody>${body}</tbody></table>
      </div>`;
  }).join('');
}

// Parse one EVE-inventory line into [name, qty] or null. Mirrors app/pi.py parse_inventory:
// handles tab- or 2+-space columns, "Name | Qty | Category" (qty 2nd) AND "Name | Category |
// Qty" (qty 3rd), space/comma thousands separators, and a single-space "Name 1234" fallback.
function _parseInventoryLine(line) {
  line = line.trim();
  if (!line) return null;
  let parts = line.includes('\t') ? line.split('\t') : line.split(/ {2,}/);
  if (parts.length < 2) {
    const m = line.match(/^(.+?)\s+(\d[\d,\s]*)\s*$/);
    if (!m) return null;
    parts = [m[1].trim(), m[2].trim()];
  }
  const name = parts[0].trim();
  const col1 = parts[1].trim();
  let qtyStr;
  if (/^[\d\s,]+$/.test(col1) && /\d/.test(col1)) qtyStr = col1;   // Format A (qty in col 2)
  else if (parts.length >= 3) qtyStr = parts[2].trim();           // Format B (category col 2)
  else return null;
  const qty = parseInt(qtyStr.replace(/[^\d]/g, ''), 10) || 0;
  return (qty > 0 && name) ? [name, qty] : null;
}

// The PI Planner's single inventory paste (#inv) drives the refill split too — re-parse it
// (matching P1 names against the selected plan) and refresh the tables. Called on every #inv
// edit (wired in app.js) and after the section renders.
function syncRefillFromInventory() {
  const inv = document.getElementById('inv');
  _p1Stacks = {};
  for (const line of (inv ? inv.value : '').split('\n')) {
    const parsed = _parseInventoryLine(line);
    if (!parsed) continue;
    const tid = _p1NameToTid[parsed[0].toLowerCase()];
    if (tid) _p1Stacks[tid] = parsed[1];
  }
  updateP1Distribution();
}

// Click a filled cell to copy its integer (no commas) — ready to paste into EVE quantity fields.
function copyP1Amount(el) {
  if (!el.classList.contains('p1-amt-set')) return;
  const n = el.textContent.replace(/[^\d]/g, '');
  if (!n) return;
  const done = () => { el.classList.add('p1-amt-copied'); setTimeout(() => el.classList.remove('p1-amt-copied'), 600); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(n).then(done).catch(done);
  else done();
}

// Distribute each entered stack across the factory planets that consume that P1, whole units
// summing exactly to the stack (largest-remainder). No stack → show the % share.
// Compute the refill top-up into the model, then render. Each factory's launchpads hold up to
// 30,000 m³ of P1 (3 LP); we fill the FREE space (30,000 − what's already in, from inputM3) in
// recipe ratio, capped by how much P1 you actually have (your pasted stock). Two readings:
//   • per-factory (All planets view): top each factory to full given its own current contents.
//   • uniform (Summary view): one amount sized to the FULLEST factory so none overflow.
// Both floor to whole units; leftover stays in your hangar.
const _P1_VOL_M3 = 0.19;                  // m³ per P1 unit (verified in-game)
const _LP_CAP_M3 = 30000;                 // 3 launchpads of P1 input space
let _refillUniform = {};                  // tid -> uniform per-factory amount (Summary view)
function updateP1Distribution() {
  if (!_refillModel) return;
  _refillUniform = {};
  const items = _planConsumptionItems();          // perDay = PLAN-total units/day per P1
  const perDay = {}; items.forEach(it => { perDay[String(it.tid)] = it.perDay; });
  const sumW = items.reduce((a, it) => a + it.perDay, 0);
  const haveWeights = items.length > 0 && sumW > 0;

  const allFacs = [];
  _refillModel.forEach(g => g.facs.forEach(f => {
    f.amt = {};
    f.availM3 = (f.inputM3 != null) ? Math.max(0, _LP_CAP_M3 - f.inputM3) : _LP_CAP_M3;  // free LP space
    allFacs.push(f);
  }));

  if (!haveWeights) {                              // no recipe data → plain split of each stack by share
    const byTid = {};
    allFacs.forEach(f => Object.keys(f.shareByTid).forEach(t => (byTid[t] = byTid[t] || []).push(f)));
    for (const tid in byTid) {
      const stack = _p1Stacks[tid] || 0, arr = byTid[tid];
      let sh = arr.map(f => f.shareByTid[tid] || 0), ss = sh.reduce((a, b) => a + b, 0);
      if (ss <= 0) { sh = arr.map(() => 1 / arr.length); ss = 1; }
      arr.forEach((f, i) => { f.amt[tid] = stack ? Math.max(0, Math.floor(stack * sh[i] / ss)) : null; });
      _refillUniform[tid] = stack ? Math.floor(stack / arr.length) : null;
    }
    _renderRefillTables(); _updateRefillDays(); return;
  }

  const tids = Object.keys(perDay);
  const fvFrac = {}; tids.forEach(t => { fvFrac[t] = perDay[t] / sumW; });   // recipe share of P1 t (units = vol, same VOL)
  const target = (availM3, tid) => (availM3 / _P1_VOL_M3) * fvFrac[tid];      // units of P1 t to fill `availM3` in ratio

  // Per-factory: fill each factory's own free space; global scale so no P1 exceeds your stock.
  let s = 1;
  for (const tid of tids) {
    const stack = _p1Stacks[tid] || 0;
    let tot = 0; allFacs.forEach(f => { if (tid in f.shareByTid) tot += target(f.availM3, tid); });
    if (tot > 0) s = Math.min(s, stack / tot);
  }
  if (!(s >= 0)) s = 0;
  allFacs.forEach(f => { for (const tid of tids) if (tid in f.shareByTid) f.amt[tid] = Math.max(0, Math.floor(s * target(f.availM3, tid))); });

  // Uniform: size to the fullest factory (smallest free space) so the same drop fits every one.
  const used = allFacs.filter(f => Object.keys(f.shareByTid).length);
  const minAvail = used.length ? Math.min(...used.map(f => f.availM3)) : 0;
  const n = used.length || 1;
  let su = 1;
  for (const tid of tids) {
    const stack = _p1Stacks[tid] || 0, tot = target(minAvail, tid) * n;
    if (tot > 0) su = Math.min(su, stack / tot);
  }
  if (!(su >= 0)) su = 0;
  for (const tid of tids) _refillUniform[tid] = Math.max(0, Math.floor(su * target(minAvail, tid)));

  _renderRefillTables();
  _updateRefillDays();
}

// Normalise the snapshot's consumption (new = list with names; legacy = {tid: units}) to
// [{tid, name, perDay}] so the readout is robust to either format.
function _planConsumptionItems() {
  const c = _planConsumption;
  if (Array.isArray(c))
    return c.filter(x => x.units_per_day > 0)
            .map(x => ({ tid: String(x.p1_type_id), name: x.p1_name, perDay: x.units_per_day }));
  return Object.keys(c || {}).filter(t => c[t] > 0)
          .map(t => ({ tid: t, name: _p1TidToName[t] || ('P1 ' + t), perDay: c[t] }));
}

// How long the P1 you pasted lasts before you must refill the factories: each P1 is eaten at
// units_per_day (full rate), so the run lasts until the pasted input you have least of (relative
// to its consumption) runs out. Computed over what you actually pasted.
function _updateRefillDays() {
  const el = document.getElementById('refillDays');
  if (!el) return;
  const items = _planConsumptionItems();
  if (!items.length) {  // older snapshot saved before this feature — prompt a re-save
    el.innerHTML = `<span class="dist-days-hint">Re-save this plan in Planetary Planning to see how long your P1 lasts before a refill.</span>`;
    return;
  }
  const pasted = items.filter(it => (_p1Stacks[it.tid] || 0) > 0);
  if (!pasted.length) {  // nothing relevant pasted yet → tell the user it's here
    el.innerHTML = `<span class="dist-days-hint">Paste your P1 in the Inventory box above to see how long it lasts before a refill.</span>`;
    return;
  }
  let minDays = Infinity, binding = null;
  for (const it of pasted) {
    const d = _p1Stacks[it.tid] / it.perDay;
    if (d < minDays) { minDays = d; binding = it; }
  }
  const m = _planMeta || {};
  const unitsMade = Math.round((m.productsPerDay || 0) * minDays);
  const sellValue = (m.iskPerDay || 0) * minDays;
  const lead = `<div class="refill-lead">With what you pasted, <b>${_esc(binding.name)}</b> runs out first.</div>`;
  const tiles = [
    `<div class="refill-stat"><span class="refill-stat-val">${_dur(minDays)}</span>
       <span class="refill-stat-lbl">before refill</span></div>`,
  ];
  if (m.productsPerDay)
    tiles.push(`<div class="refill-stat"><span class="refill-stat-val">${unitsMade.toLocaleString()}</span>
       <span class="refill-stat-lbl">${_esc(m.unitLabel || 'units')} produced</span></div>`);
  if (m.iskPerDay)
    tiles.push(`<div class="refill-stat"><span class="refill-stat-val">${_fmtIsk(sellValue)}</span>
       <span class="refill-stat-lbl">sell value of the run</span></div>`);
  // Older snapshot without the production rate → tell the user how to get the extra tiles.
  const rateNote = (!m.productsPerDay && !m.iskPerDay)
    ? `<div class="refill-stat-note">Re-save this plan in Planetary Planning to also see units produced &amp; sell value.</div>`
    : '';
  // Required inputs you didn't paste — they'd cap the run too, so flag them.
  const missing = items.filter(it => !((_p1Stacks[it.tid] || 0) > 0));
  const missNames = missing.length <= 3 ? ` (${missing.map(x => _esc(x.name)).join(', ')})` : '';
  const missNote = missing.length
    ? `<div class="refill-stat-note">${missing.length} plan input${missing.length > 1 ? 's' : ''} not pasted${missNames} — they'd cap the run too.</div>`
    : '';
  el.innerHTML = `${lead}<div class="refill-stats-bar">${tiles.join('')}</div>${rateNote}${missNote}`;
}

function renderFinalPlan(data, opts = {}) {
  const content = document.getElementById('wizPlanContent');

  // Order characters case-insensitively (so "ekaoni" doesn't sort below "Enbe"); also fixes
  // older saved/restored plans without a re-run.
  if (Array.isArray(data.assignments))
    data.assignments.sort((a, b) => (a.character_name || '').toLowerCase().localeCompare((b.character_name || '').toLowerCase()));

  // Stats bar
  let statsHtml = '';
  if (data.stats) {
    const s = data.stats;
    // P0 extraction stat: compare actual avg (quality-based) vs required (for self-sufficiency)
    // _k: format a P0-per-cycle value as "47k" etc.
    const _k = v => v >= 1000 ? Math.round(v / 1000) + 'k' : v;
    let p0StatHtml = '';
    if (s.required_avg_p0_per_cycle != null) {
      const req = s.required_avg_p0_per_cycle;
      const avg = s.avg_p0_per_cycle;
      if (avg != null) {
        const overPct = Math.round((avg / req - 1) * 100);
        const ok = avg >= req;
        const sign = overPct >= 0 ? '+' : '';
        // When undersupplied: show "needed" prominently so it's clear what's missing.
        // When oversupplied: show "avg" prominently as a positive signal.
        p0StatHtml = ok
          ? `<div class="plan-stat" title="Actual avg P0/cycle vs required for factories to run at 100%. Required assumes 24 extractor cycles/day (1h cycle).">
            <span class="plan-stat-val plan-stat-ok">${_k(avg)} avg</span>
            <span class="plan-stat-lbl">P0/cycle per ext · req ${_k(req)} · ${sign}${overPct}% buffer · ${s.total_extractors} ext</span>
          </div>`
          : `<div class="plan-stat" title="Actual avg P0/cycle vs required for factories to run at 100%. Required assumes 24 extractor cycles/day (1h cycle).">
            <span class="plan-stat-val plan-stat-warn">${_k(req)} needed</span>
            <span class="plan-stat-lbl">P0/cycle per ext · ${_k(avg)} avg · ${sign}${overPct}% · ${s.total_extractors} ext</span>
          </div>`;
      } else {
        p0StatHtml = `
          <div class="plan-stat" title="Required avg P0/cycle per extractor for self-sufficiency. 48k = 0% overproduction baseline. Assumes 1h extractor cycles.">
            <span class="plan-stat-val">${_k(req)} req</span>
            <span class="plan-stat-lbl">P0/cycle per ext · ${s.total_extractors} ext · no planet data for actual avg</span>
          </div>`;
      }
    } else if (s.avg_p0_per_cycle != null) {
      p0StatHtml = `
        <div class="plan-stat">
          <span class="plan-stat-val">${_k(s.avg_p0_per_cycle)} avg</span>
          <span class="plan-stat-lbl">P0/cycle per ext · ${s.total_extractors} ext</span>
        </div>`;
    }
    const targetOverprod = parseInt((document.getElementById('targetOverprod') || {}).value);
    // Clamp to ≥0: negative overproduction just builds factories the extractors can't feed
    // (output collapses), so it's never useful.
    const opVal = Number.isNaN(targetOverprod) ? 10 : Math.max(0, targetOverprod);
    // Split-extraction toggle: reuse a planet for two P0s (2 ECUs sharing the 10 heads), then
    // turn the freed planets into more factories. Single on/off.
    const splitOn = ((_wiz.splitMode || s.split_mode || 'off') !== 'off');
    const splitBtns = [['off', 'Off'], ['on', 'On']].map(
      ([v, l]) => `<button type="button" class="plan-split-btn${(v === 'on') === splitOn ? ' plan-split-on' : ''}" onclick="setSplitMode('${v}')">${l}</button>`
    ).join('');
    const savedLbl = (s.split_planets > 0)
      ? `${s.split_planets} split → ${s.planets_saved} planet${s.planets_saved !== 1 ? 's' : ''} reinvested`
      : (splitOn ? 'no overproduction slack to reclaim' : 'reuse planets → more factories');
    const splitStatHtml = `
        <div class="plan-stat plan-split-ctrl" title="Split P1 production: where two P0s share a planet type, host both on one planet (2 ECUs sharing the 10-head budget → two P1 lines). The planets this frees are reinvested into more factory planets — so output rises only by what those real extra factories produce (it needs overproduction slack to reclaim; with none, nothing to split). Head counts on split planets are guidance — real yield varies with hotspot placement and depletion.">
          <span class="plan-split-seg">${splitBtns}</span>
          <span class="plan-stat-lbl">split planets · ${savedLbl}</span>
        </div>`;
    // Distribution method: how extractor counts are split across resources. Each button gets
    // its own tooltip (one combined title on the wrapper was unreadable and ambiguous).
    const distMode = (_wiz.distMode || s.distribution_mode || 'stability');
    const distTips = {
      stability: 'Stability — extractors ∝ need ÷ planet density. A thin-deposit input gets MORE extractors so every resource lands in the recipe ratio: minimal leftover P1, no single thin resource bottlenecking the chain. (Default.)',
      need: 'Match need — extractors ∝ recipe need only, assuming uniform planet density. Simpler and fewer planets, but thin deposits underproduce and leave leftover P1.',
    };
    const distBtns = [['stability', 'Stability'], ['need', 'Match need']].map(
      ([v, l]) => `<button type="button" class="plan-split-btn${v === distMode ? ' plan-split-on' : ''}" onclick="setDistMode('${v}')" title="${distTips[v]}">${l}</button>`
    ).join('');
    const distStatHtml = `
        <div class="plan-stat plan-split-ctrl">
          <span class="plan-split-seg">${distBtns}</span>
          <span class="plan-stat-lbl">distribution · hover a mode</span>
        </div>`;
    // Density cap: ignore planets thinner than this %. Caps extractors on thin deposits
    // (fewer planets, a little residual) and is most useful in Stability mode.
    const minDens = _wiz.minDensity || 0;
    const minDensHtml = `
        <div class="plan-stat plan-stat-edit" title="Ignore planets thinner than this % — in the plan AND the system suggestions. Stops piling extractors onto thin deposits (e.g. a low-density gas) at the cost of a little residual on resources that then can't reach their need. 0 = use any planet that has the resource.">
          <span class="plan-stat-val"><input type="number" id="planMinDensityInput" class="plan-overprod-input" value="${minDens}" min="0" max="100" step="5">% min density</span>
          <span class="plan-stat-lbl">cap thin deposits</span>
        </div>`;
    // Supply-limited throughput: products/day assumes 100% factory uptime, but when extraction
    // can't keep a resource fed the factories run slow. The server reports the binding ratio +
    // bottleneck; show the real output with the "if fully fed" figure as context.
    const supLim = s.supply_limited && s.effective_products_per_day != null;
    const fedPct = Math.round((s.supply_ratio != null ? s.supply_ratio : 1) * 100);
    const unitLbl = data.fuelblock ? 'fuel blocks/day' : 'units/day';
    const iskTip = data.fuelblock
      ? 'Daily Jita-sell value of the PI components you actually produce. A finished fuel block is worth more but also needs ice products + racial isotopes you do NOT produce here.'
      : 'Daily Jita-sell value of the product.';
    const prodTile = supLim
      ? `<div class="plan-stat" title="Real output. The extractors can only keep ${_esc(s.bottleneck_p0 || 'a resource')} fed to ${fedPct}% of what the factories need, so the whole chain runs at ${fedPct}%. ${s.products_per_day.toLocaleString()} is the 'if fully fed' figure — raise extraction for ${_esc(s.bottleneck_p0 || 'it')} (min density, more extractor planets, or richer systems) to close the gap.">
          <span class="plan-stat-val plan-stat-warn">${s.effective_products_per_day.toLocaleString()}</span>
          <span class="plan-stat-lbl">${unitLbl} · ${fedPct}% fed, capped by ${_esc(s.bottleneck_p0 || 'extraction')}</span>
        </div>`
      : `<div class="plan-stat" title="Daily output assuming the factories stay fed (extraction meets the recipe need).">
          <span class="plan-stat-val">${s.products_per_day.toLocaleString()}</span>
          <span class="plan-stat-lbl">${unitLbl}</span>
        </div>`;
    const iskTile = supLim
      ? `<div class="plan-stat" title="Daily Jita-sell value at the supply-limited output (${fedPct}% fed). ${_fmtIsk(s.isk_per_day)} is the if-fully-fed value.">
          <span class="plan-stat-val plan-stat-warn">${_fmtIsk(s.effective_isk_per_day)}</span>
          <span class="plan-stat-lbl">ISK/day${data.fuelblock ? ' (PI)' : ''} · extractor-limited</span>
        </div>`
      : `<div class="plan-stat" title="${iskTip}">
          <span class="plan-stat-val">${_fmtIsk(s.isk_per_day)}</span>
          <span class="plan-stat-lbl">ISK/day${data.fuelblock ? ' (PI)' : ''}</span>
        </div>`;
    statsHtml = `
      <div class="plan-stats-bar">
        ${prodTile}
        ${iskTile}
        ${data.fuelblock && s.block_gross_isk_per_day ? `
        <div class="plan-stat" title="Gross Jita-sell value of ${(supLim ? s.effective_products_per_day : s.products_per_day).toLocaleString()} finished fuel blocks/day. Requires buying/producing the ice products + racial isotopes (not modelled here), so it is NOT your PI profit.">
          <span class="plan-stat-val plan-stat-dim">${_fmtIsk(supLim ? s.block_gross_isk_per_day * s.supply_ratio : s.block_gross_isk_per_day)}</span>
          <span class="plan-stat-lbl">block gross · needs ice</span>
        </div>` : `
        <div class="plan-stat">
          <span class="plan-stat-val">${_fmtIsk(s.sell_price)}</span>
          <span class="plan-stat-lbl">sell/unit</span>
        </div>`}
        ${s.factory_refill_hours ? `
        <div class="plan-stat" title="How long a factory planet's P1 input buffer lasts before you must refill it (assumes ${s.factory_launchpads_assumed} launchpads = ${(s.factory_launchpads_assumed*10).toLocaleString()}k m³ of P1; ~${s.factory_input_m3_day.toLocaleString()} m³/day consumed per factory). Add storage facilities to extend it.">
          <span class="plan-stat-val">${_fmtHours(s.factory_refill_hours)}</span>
          <span class="plan-stat-lbl">refill / factory (${s.factory_launchpads_assumed} LP)</span>
        </div>` : ''}
        ${p0StatHtml}
      </div>
      <div class="plan-stats-bar plan-settings-bar">
        <span class="plan-settings-tag">Settings</span>
        <div class="plan-stat plan-stat-edit" title="Target overproduction % — edit and the plan recalculates. 10% means extractors produce 10% more P0 than factories need. Reported baseline: ${s.overproduction_pct >= 0 ? '+' : ''}${s.overproduction_pct}%.">
          <span class="plan-stat-val ${s.overproduction_pct < 0 ? 'plan-stat-warn' : 'plan-stat-ok'}"><input type="number" id="planOverprodInput" class="plan-overprod-input" value="${opVal}" min="0" max="500" step="5">% overprod</span>
          <span class="plan-stat-lbl">${s.factories} factories${s.max_supportable_factories != null ? ' · max ' + s.max_supportable_factories : ''}</span>
        </div>
        ${distStatHtml}
        ${minDensHtml}
        ${splitStatHtml}
      </div>`;
  }

  // P1 requirement pills. relative_qty can be a long float (e.g. 60.9187392) — round to
  // at most one decimal (dropping trailing .0) so the pills stay readable. Opacity scales
  // with demand (brighter = needs more extractor planets) so the row reads at a glance.
  const _fmtQty = v => (Math.round(v * 10) / 10).toString();
  const maxQty = Math.max(...data.p1_requirements.map(r => r.relative_qty));
  const reqPills = data.p1_requirements.map(r => {
    const op = maxQty > 0 ? (0.5 + 0.5 * (r.relative_qty / maxQty)).toFixed(2) : 1;
    const chain = r.p0_name ? `${r.p0_name} → ${r.p1_name}` : r.p1_name;
    const tip = `${chain} · relative extractor demand ${_fmtQty(r.relative_qty)} (brighter = more extractor planets)`;
    return `<span class="plan-req-pill" style="opacity:${op}" title="${tip}">${r.p1_name} <em>×${_fmtQty(r.relative_qty)}</em></span>`;
  }).join('');

  const totalFreeSlots = (data.assignments || []).reduce((s, a) => s + (a.free_planets || 0), 0);
  const unmetHtml = data.unassigned && data.unassigned.length
    ? `<div class="plan-warning">${data.unassigned.length} extractor slot${data.unassigned.length > 1 ? 's' : ''} unassigned — ${totalFreeSlots >= data.unassigned.length ? 'no suitable planet type available for these P0 types' : 'not enough character planet slots'}.</div>`
    : '';
  // Allowed factory planet types → compact abbr for option labels ("B/T", "B/T/L") and a
  // readable name list for prose warnings; single-product defaults to Barren/Temperate.
  const facPtypes = (data.factory_planet_types && data.factory_planet_types.length)
    ? data.factory_planet_types : ['Barren', 'Temperate'];
  const typeAbbr = facPtypes.map(t => t[0]).join('/');
  const typeNames = facPtypes.join('/');

  const totalUnplacedFac = data.assignments.reduce((s, a) =>
    s + (a.factory_assignments || []).filter(f => f.unplaced).length, 0);
  const unplacedFacHtml = totalUnplacedFac
    ? `<div class="plan-warning">${totalUnplacedFac} factory slot${totalUnplacedFac !== 1 ? 's' : ''} unplaced — not enough ${typeNames} planets in chosen system. Check character ext config if unexpected.</div>`
    : '';

  // Factory system selector — dropdown of systems with their allowed-planet-type counts.
  let facSysHtml = '';
  {
    const opts = data.factory_system_options || [];
    const needed = data.factory_planets_needed || 0;
    // Build option list; ensure current factory_system is always present
    const optSystems = new Map(opts.map(o => [o.system, o.count]));
    if (data.factory_system && !optSystems.has(data.factory_system)) {
      optSystems.set(data.factory_system, null);
    }
    const optHtml = [...optSystems.entries()].map(([s, cnt]) => {
      const sel = s === data.factory_system ? ' selected' : '';
      const label = cnt != null
        ? `${s}  (${cnt} ${typeAbbr}${cnt < needed ? ' ⚠' : ''})`
        : s;
      return `<option value="${_esc(s)}"${sel}>${_esc(label)}</option>`;
    }).join('');
    const unplacedFac = (data.assignments || []).reduce((s, a) =>
      s + (a.factory_assignments || []).filter(f => f.unplaced).length, 0);
    const warnCls = unplacedFac ? ' plan-fac-sys-warn' : '';
    facSysHtml = `
      <div class="plan-fac-sys-bar">
        <span class="plan-fac-sys-label">Factory system</span>
        <select class="plan-fac-sys-select${warnCls}" id="factorySysSelect">
          ${optHtml}
          <option value="">— custom —</option>
        </select>
        <input class="plan-fac-sys-input" id="factorySysInput" type="text"
               placeholder="System name" style="display:none">
        <span class="plan-fac-sys-need">${needed} planet${needed !== 1 ? 's' : ''} needed</span>
        ${unplacedFac ? `<span class="plan-fac-sys-shortage">${unplacedFac} slot${unplacedFac !== 1 ? 's' : ''} unplaced — scan more ${typeNames} planets in this system</span>` : ''}
      </div>`;
  }

  // Factory planet-type chips (fuel-block only) — choose which planet types factories use.
  // Barren/Temperate are smallest (least power-grid/link footprint), so they're the default.
  let facTypeHtml = '';
  if (data.fuelblock) {
    const ALL_TYPES = ['Barren', 'Temperate', 'Lava', 'Plasma', 'Gas', 'Ice', 'Oceanic', 'Storm'];
    const available = data.available_planet_types || [];
    const chosen = new Set(data.factory_planet_types || _wiz.factoryPlanetTypes || ['Barren', 'Temperate']);
    // Show all standard types; grey out ones with no planets in the chosen systems.
    const chips = ALL_TYPES.map(t => {
      const has = available.includes(t);
      const on = chosen.has(t);
      const cls = `plan-ptype-chip${on ? ' plan-ptype-on' : ''}${has ? '' : ' plan-ptype-empty'}`;
      const title = has ? `Allow factories on ${t} planets`
                        : `No ${t} planets in the chosen system(s)`;
      return `<button type="button" class="${cls}" data-ptype="${t}" title="${title}">${t}</button>`;
    }).join('');
    const unpinned = data.factory_planets_unpinned || 0;
    const sysLabel = data.factory_system ? ` in ${_esc(data.factory_system)}` : '';
    const shortageHtml = unpinned ? `
      <div class="plan-warning plan-ptype-shortage">${unpinned} factory planet${unpinned !== 1 ? 's' : ''} couldn't be placed${sysLabel} — only ${[...chosen].join('/')} allowed and there aren't enough.
        Enable more planet types above, pick a system with more planets, or scan more.</div>` : '';
    facTypeHtml = `
      <div class="plan-fac-type-bar">
        <span class="plan-fac-sys-label">Factory planet types</span>
        <div class="plan-ptype-chips" id="factoryTypeChips">${chips}</div>
      </div>
      ${shortageHtml}`;
  }

  // Character assignments
  const _facSys = data.factory_system || '';

  // Per-planet row HTML. In the per-system columns the system is the column header, so the row
  // shows just the planet number (showSys=false); set showSys=true to include the system name.
  const _extHtml = (e, showSys) => {
    if (e.split) return _splitExtRow(e);
    const ptype = e.is_existing
      ? (e.planet_type || e.existing_ptype || '?')
      : (e.planet_type || e.best_planet_type || (e.planet_types && e.planet_types[0]) || '?');
    const over = e.is_extra ? ' plan-ext-over' : '';
    const overMark = e.is_extra ? ' +' : '';
    const tag = e.is_existing
      ? `<span class="plan-ext-tag plan-ext-existing${over}">existing${overMark}</span>`
      : e.is_replace
        ? `<span class="plan-ext-tag plan-ext-replace${over}">replace${overMark}</span>`
        : `<span class="plan-ext-tag plan-ext-new${over}">new${overMark}</span>`;
    const loc = e.system
      ? `<span class="plan-ext-sys">${showSys ? e.system + ' ' : ''}P${e.planet_num}</span>`
      : `<span class="plan-ext-no-planet">no planet in system</span>`;
    const qualHtml = e.quality_pct !== undefined
      ? `<span class="plan-ext-qual ${e.quality_pct >= 80 ? 'plan-qual-ok' : e.quality_pct >= 50 ? '' : 'plan-qual-low'}">${e.quality_pct}</span>` : '';
    // Show the P0 you extract (what you place heads on) → the P1 it makes (for templates).
    const chain = e.p0_name
      ? `<span class="plan-ext-p0col">${e.p0_name}</span><span class="plan-ext-p1sub"> → ${e.p1_name || '?'}</span>`
      : (e.p1_name || '?');
    return `<div class="plan-ext-row">${tag}${_ptypeSpan(ptype)}${loc}<span class="plan-ext-arrow">→</span><span class="plan-ext-p1" title="${e.p0_name ? e.p0_name + ' → ' : ''}${e.p1_name || '?'}">${chain}</span>${qualHtml}</div>`;
  };
  const _facHtml = (f, showSys) => {
    const tag = f.is_existing
      ? `<span class="plan-ext-tag plan-ext-existing">existing</span>`
      : f.is_replace
        ? `<span class="plan-ext-tag plan-ext-replace">replace</span>`
        : `<span class="plan-ext-tag plan-ext-new">new</span>`;
    const loc = f.system
      ? `<span class="plan-ext-sys">${showSys ? f.system : ''}${f.planet_num != null ? (showSys ? ' P' : 'P') + f.planet_num : ''}</span>`
      : f.unplaced ? `<span class="plan-ext-no-planet">no factory planet in system</span>` : '';
    const facLabel = f.product ? `${f.product.name}${f.ccu ? ' · CC' + f.ccu : ''}` : 'factory';
    return `<div class="plan-ext-row">${tag}${_ptypeSpan(f.planet_type || 'Barren')}${loc}<span class="plan-ext-arrow">→</span><span class="plan-ext-p1 plan-fac-label">${_esc(facLabel)}</span></div>`;
  };

  const _sysOrder = (x, y) => {
    const fx = x === _facSys ? 0 : 1, fy = y === _facSys ? 0 : 1;
    return fx !== fy ? fx - fy : x.localeCompare(y);          // factory system first, then by name
  };
  const _byNum = (x, y) => (x.planet_num == null ? 1e9 : x.planet_num) - (y.planet_num == null ? 1e9 : y.planet_num);
  const _sysKey = s => s || '￿';                              // unplaced/no-system rows sort last
  const _locSort = (x, y) => {
    const fx = x.system && x.system === _facSys ? 0 : 1, fy = y.system && y.system === _facSys ? 0 : 1;
    if (fx !== fy) return fx - fy;
    const c = _sysKey(x.system).localeCompare(_sysKey(y.system));
    return c || _byNum(x, y);
  };

  // Layout per character: "grouped" splits a toon's planets into a column per system (factory
  // system first) — for deploying system by system; "flat" is one location-ordered list.
  if (_wiz.planGroup == null) { try { _wiz.planGroup = localStorage.getItem('ppPlanGroup') || 'grouped'; } catch (e) { _wiz.planGroup = 'grouped'; } }
  const _grouped = _wiz.planGroup !== 'flat';

  const assignHtml = data.assignments.map(a => {
    if (!a.extractors.length && !a.factory_planets) return '';
    const items = [];
    a.extractors.forEach(e => items.push({ system: e.system || '', planet_num: e.planet_num, e }));
    (a.factory_assignments || []).forEach(f => items.push({ system: f.system || '', planet_num: f.planet_num, f }));
    const _html = (i, showSys) => i.e ? _extHtml(i.e, showSys) : _facHtml(i.f, showSys);
    const freeRows = Array.from({length: a.free_planets || 0}, () =>
      `<div class="plan-ext-row"><span class="plan-ext-tag plan-ext-free">free</span><span class="plan-ext-p1 plan-free-label">available</span></div>`).join('');

    // Command Centers this toon needs, by planet type (one CC per colony) — for buying/hauling.
    const ccCounts = {};
    items.forEach(i => {
      const pt = i.e ? (i.e.planet_type || i.e.best_planet_type || (i.e.planet_types && i.e.planet_types[0]))
                     : (i.f.planet_type || 'Barren');
      if (pt) ccCounts[pt] = (ccCounts[pt] || 0) + 1;
    });
    const ccHtml = Object.keys(ccCounts).length
      ? `<div class="plan-char-cc"><span class="plan-char-cc-lbl">Command centers</span>${
          Object.entries(ccCounts).sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
            .map(([t, n]) => `<span class="plan-cc-badge"><b>${n}×</b> ${_ptypeSpan(t)}</span>`).join('')}</div>`
      : '';

    let body;
    if (_grouped) {
      const systems = [...new Set(items.filter(i => i.system).map(i => i.system))].sort(_sysOrder);
      const cols = systems.map(sys => {
        const cr = items.filter(i => i.system === sys).sort(_byNum);
        return `<div class="plan-char-syscol"><div class="plan-char-syshdr">${_esc(sys)}<span class="plan-char-sysn">${cr.length}</span></div>${cr.map(i => _html(i, false)).join('')}</div>`;
      });
      const noSys = items.filter(i => !i.system);
      if (noSys.length)
        cols.push(`<div class="plan-char-syscol"><div class="plan-char-syshdr plan-sys-title-warn">no system<span class="plan-char-sysn">${noSys.length}</span></div>${noSys.map(i => _html(i, false)).join('')}</div>`);
      if (freeRows)
        cols.push(`<div class="plan-char-syscol"><div class="plan-char-syshdr plan-char-syshdr-free">free<span class="plan-char-sysn">${a.free_planets}</span></div>${freeRows}</div>`);
      body = `<div class="plan-char-syscols">${cols.join('')}</div>`;
    } else {
      const rows = items.sort(_locSort).map(i => _html(i, true)).join('');
      body = `<div class="plan-char-extractors">${rows}${freeRows}</div>`;
    }

    const isFacChar = (_wiz.factoryCharIds || []).includes(a.character_id);
    const facBadge = a.factory_only ? `<span class="plan-fac-only-badge">factory only</span>` : '';
    const facToggle = `<button class="plan-fac-toggle-btn${isFacChar ? ' plan-fac-toggle-active' : ''}"
      onclick="toggleFactoryChar(${a.character_id})" title="${isFacChar ? 'Stop prioritising this character for factories' : 'Prioritise this character to host factories (still extracts on spare slots)'}">${isFacChar ? '★ factories' : 'host factories'}</button>`;
    const isSel = _wiz.selectedChar === a.character_id;
    return `
      <div class="plan-char-block${isSel ? ' plan-char-selected' : ''}" data-char-id="${a.character_id}" onclick="togglePlanChar(${a.character_id}, event)" title="Click to highlight this character while you set them up">
        <div class="plan-char-name">${a.character_name}${facBadge}${facToggle}<span class="plan-char-meta"> · ${a.effective_planets} pl · CCU ${a.ccu}</span></div>
        ${ccHtml}
        ${body}
      </div>`;
  }).join('');

  // P0 summary: group extractor slots by P0 type
  const p0Map = {};
  for (const a of data.assignments) {
    for (const e of a.extractors) {
      if (!e.p0_name || e.is_extra) continue;
      if (!p0Map[e.p0_name]) p0Map[e.p0_name] = { p1_name: e.p1_name, slots: [] };
      p0Map[e.p0_name].slots.push(e);
    }
  }
  const p0SummaryHtml = Object.keys(p0Map).length ? `
    <div class="plan-section-title">P0 Summary</div>
    <table class="p0-sum-table">
      <thead><tr><th>P0</th><th></th><th>P1</th><th>#</th><th>Planets</th></tr></thead>
      <tbody>${Object.entries(p0Map).map(([p0, info]) => {
        const ptype = e => e.planet_type || e.existing_ptype || e.best_planet_type || (e.planet_types && e.planet_types[0]) || '?';
        const planets = info.slots.map(e => {
          const loc = e.system ? `${e.system} P${e.planet_num}` : '<em>unplaced</em>';
          const q = e.quality_pct != null ? ` <span class="p0-sum-q">${e.quality_pct}</span>` : '';
          return `<span class="p0-sum-slot">${_ptypeSpan(ptype(e))} ${loc}${q}</span>`;
        }).join('');
        return `<tr><td class="p0-sum-name">${p0}</td><td class="p0-sum-arr">→</td><td class="p0-sum-p1">${info.p1_name}</td><td class="p0-sum-n">${info.slots.length}</td><td>${planets}</td></tr>`;
      }).join('')}</tbody>
    </table>` : '';

  // Fuel-block basket: headline blocks/day + per-component factory table.
  let fbHtml = '';
  let templatesHref = _wiz.typeId ? `/api/layout/bundle?type_ids=${_wiz.typeId}&expand=1` : '';
  if (data.fuelblock) {
    const lines = data.factory_lines || [];
    const blocks = data.fuel_blocks_per_day || 0;
    const runSize = data.stats?.blocks_per_run || 40;
    const runs = runSize > 1 ? Math.floor(blocks / runSize) : 0;  // 1:1 baskets → no separate "runs"
    const insufficient = blocks <= 0;
    const lineRows = lines.map(l => {
      const need = l.need_per_day != null
        ? l.need_per_day
        : Math.round((blocks / (data.stats?.blocks_per_run || 40)) * l.qty_per_run);
      const short = l.units_per_day < need - 1 ? ' plan-qual-low' : '';
      return `<tr>
        <td>${_esc(l.name)} <span class="p0-sum-q">P${l.tier}</span></td>
        <td class="p0-sum-n">${l.count}</td>
        <td>${Math.round(l.rate_per_hour)}/h</td>
        <td class="${short}">${Math.round(l.units_per_day).toLocaleString()}</td>
        <td>${need.toLocaleString()}</td>
      </tr>`;
    }).join('');
    const imported = data.imported || [];
    const importHtml = imported.length ? `
      <div class="plan-section-title">Imported (buy / haul in)</div>
      <table class="p0-sum-table">
        <thead><tr><th>Component</th><th>Units/day</th></tr></thead>
        <tbody>${imported.map(i => `<tr>
          <td>${_esc(i.name)} <span class="p0-sum-q">P${i.tier}</span></td>
          <td>${Math.round(i.units_per_day).toLocaleString()}</td>
        </tr>`).join('')}</tbody>
      </table>` : '';
    fbHtml = `
      <div class="plan-fb-headline">
        <span class="plan-fb-blocks ${insufficient ? 'plan-stat-warn' : 'plan-stat-ok'}">${blocks.toLocaleString()}</span>
        <span class="plan-fb-lbl">${_esc(data.block_type ? `${data.block_type} fuel blocks` : (data.unit_label || data.product?.name || 'sets'))} / day${runs ? ` · ${runs.toLocaleString()} runs` : ''}${data.stats?.material_efficiency_pct ? ` · at ${data.stats.material_efficiency_pct}% ME${data.mfg && data.mfg.rig_tier && data.mfg.rig_tier !== 'none' ? ` (${data.mfg.rig_tier.toUpperCase()} rig · ${_esc(data.mfg.sec_label || '')})` : ''}` : ''}</span>
      </div>
      ${insufficient ? `<div class="plan-warning">Not enough planet slots to run all ${lines.length} produced component factories at once — add characters/planets, or import more parts. Each produced factory line needs at least one packed planet.</div>` : ''}
      ${data.unplaced_factories ? `<div class="plan-warning">${data.unplaced_factories} factory planet${data.unplaced_factories !== 1 ? 's' : ''} could not be placed — not enough free planets in the chosen system(s).</div>` : ''}
      ${lineRows ? `<div class="plan-section-title">Factory Lines (produced)
        <span class="pp-card-hint">— throughput at CC${data.stats?.plan_cc ?? 5}${data.stats?.ccu_mixed ? ' (mixed; set per-character CC in Setup)' : ''}</span></div>
      <table class="p0-sum-table">
        <thead><tr><th>Component</th><th>Planets</th><th>Rate</th><th>Units/day</th><th>Need/day</th></tr></thead>
        <tbody>${lineRows}</tbody>
      </table>` : ''}
      ${importHtml}`;
    // Build templates from the ACTUAL placements so each planet's command-centre level +
    // planet type match the toon hosting it. Factories are tagged by the factory toon's CCU;
    // extractors by the EXTRACTOR toon's CCU (a mixed CC5/CC4 fleet then gets the right CC for
    // each). Token `tid:::cc:ptype`; expand=0 since we list the extractor templates explicitly.
    const combos = new Map();
    for (const a of (data.assignments || [])) {
      for (const f of (a.factory_assignments || [])) {
        const tid = f.product && f.product.type_id;
        if (!tid) continue;
        const ptype = f.planet_type && f.planet_type !== 'Any' ? f.planet_type : 'Barren';
        const cc = f.ccu || data.stats?.plan_cc || 5;
        combos.set(`f|${tid}|${ptype}|${cc}`, `${tid}:::${cc}:${ptype}`);
      }
      // One P0→P1 extractor template per (P1, this toon's CCU); planet type from the slot.
      const ecc = a.effective_ccu || data.stats?.plan_cc || 5;
      for (const e of (a.extractors || [])) {
        const p1 = e.p1_type_id;
        if (!p1) continue;
        const ept = e.best_planet_type || (e.planet_types && e.planet_types[0]) || '';
        combos.set(`e|${p1}|${ecc}|${ept}`, `${p1}:::${ecc}${ept ? ':' + ept : ''}`);
      }
    }
    const toks = [...combos.values()].join(',')
      || lines.map(l => l.type_id).join(',');  // fallback: unscaled, if no placements
    templatesHref = toks ? `/api/layout/bundle?type_ids=${encodeURIComponent(toks)}&expand=0` : '';
  }

  // Split-extraction planets → one two-ECU template each (p1a:p1b:headsA:headsB:cc:ptype),
  // appended to whichever bundle URL was built above (single-product or fuel-block).
  if (templatesHref) {
    const splitCombos = new Map();
    for (const a of (data.assignments || [])) {
      const ecc = a.effective_ccu || data.stats?.plan_cc || 5;
      for (const e of (a.extractors || [])) {
        if (!e.split || !e.legs || e.legs.length < 2) continue;
        const [la, lb] = e.legs;
        if (!la.p1_type_id || !lb.p1_type_id) continue;
        const pt = e.planet_type || 'Barren';
        splitCombos.set(`${la.p1_type_id}|${lb.p1_type_id}|${la.heads}|${lb.heads}|${ecc}|${pt}`,
          `${la.p1_type_id}:${lb.p1_type_id}:${la.heads}:${lb.heads}:${ecc}:${pt}`);
      }
    }
    const splitToks = [...splitCombos.values()].join(',');
    if (splitToks) templatesHref += `&splits=${encodeURIComponent(splitToks)}`;
  }
  // Storage-less extractors (buffer P0 in the launchpad) — applies to every extractor template.
  if (templatesHref && _wiz.extractorNoStorage) templatesHref += '&no_storage=1';

  content.innerHTML = `
    <div class="plan-header">
      <span class="plan-product-name">${data.product.name}</span>
      <span class="plan-summary">${data.total_extractors_base} base extractor${data.total_extractors_base !== 1 ? 's' : ''}</span>
    </div>
    ${statsHtml}
    ${fbHtml}
    ${facSysHtml}
    ${facTypeHtml}
    <div class="plan-req-row">
      <span class="plan-req-label" title="Each P1 input and its relative extractor-planet demand (×). The split drives how many extractor planets go to each material — brighter pills need more.">P1 inputs · × = relative extractors</span>
      ${reqPills}
    </div>
    ${unmetHtml}
    ${unplacedFacHtml}
    <div class="plan-section-title">Character assignment
      <span class="plan-view-toggle">
        <button class="plan-view-btn${!_grouped ? ' plan-view-on' : ''}" onclick="setPlanGroup('flat')" title="One location-ordered list per character">All systems</button>
        <button class="plan-view-btn${_grouped ? ' plan-view-on' : ''}" onclick="setPlanGroup('grouped')" title="Split each character's planets into a column per system (factory system first) — deploy one system at a time">Grouped by system</button>
      </span>
    </div>
    <div class="plan-assignments${_wiz.selectedChar != null ? ' plan-has-selection' : ''}">${assignHtml}</div>
    ${p0SummaryHtml}
    <div class="plan-actions-bar">
      <button class="plan-action-btn" id="savePlanBtn" onclick="savePlanForRefills()" title="Save this plan so you can split P1 stacks into its factories from the PI Planner tab — no need to re-run the wizard at refill time.">Save plan</button>
      <button class="plan-action-btn" id="ppShoppingListBtn">Command Centers</button>
      ${templatesHref ? `<a class="plan-action-btn" href="${templatesHref}" download>PI Templates (.zip)</a>` : ''}
    </div>
  `;
  _storePlanSnapshot(data);  // make this plan's factory distribution available in the PI Planner tab
  const facSelect = content.querySelector('#factorySysSelect');
  const facInput  = content.querySelector('#factorySysInput');
  if (facSelect) {
    facSelect.addEventListener('change', () => {
      if (facSelect.value === '') {
        facInput.style.display = '';
        facInput.focus();
      } else {
        facInput.style.display = 'none';
        _rerunWithFactorySystem(facSelect.value);
      }
    });
  }
  if (facInput) {
    facInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && facInput.value.trim()) {
        _rerunWithFactorySystem(facInput.value.trim());
      }
    });
  }
  const typeChips = content.querySelector('#factoryTypeChips');
  if (typeChips) {
    typeChips.addEventListener('click', e => {
      const btn = e.target.closest('.plan-ptype-chip');
      if (!btn) return;
      const t = btn.dataset.ptype;
      const cur = new Set(_wiz.factoryPlanetTypes || ['Barren', 'Temperate']);
      if (cur.has(t)) {
        if (cur.size === 1) return;  // keep at least one type selected
        cur.delete(t);
      } else {
        cur.add(t);
      }
      // Preserve a stable order matching the chip row.
      const ORDER = ['Barren', 'Temperate', 'Lava', 'Plasma', 'Gas', 'Ice', 'Oceanic', 'Storm'];
      _wiz.factoryPlanetTypes = ORDER.filter(x => cur.has(x));
      _rerunPlan();
    });
  }
  const mdInput = content.querySelector('#planMinDensityInput');
  if (mdInput) {
    const applyMinDensity = () => {
      let v = parseInt(mdInput.value);
      if (Number.isNaN(v)) return;
      v = Math.max(0, Math.min(100, v));
      if (parseInt(mdInput.value) !== v) mdInput.value = v;
      if ((_wiz.minDensity || 0) === v) return;  // unchanged → skip re-run
      _wiz.minDensity = v;
      const setup = document.getElementById('targetMinDensity');
      if (setup) setup.value = v;  // keep the setup field (used by Find Systems) in sync
      _rerunPlan();
    };
    mdInput.addEventListener('change', applyMinDensity);
    mdInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyMinDensity(); } });
    // Wheel adjusts in steps of 5, debounced (same as overprod) — native spinners are hidden.
    mdInput.addEventListener('wheel', e => {
      e.preventDefault();
      const cur = parseInt(mdInput.value) || 0;
      mdInput.value = Math.max(0, Math.min(100, cur + (e.deltaY < 0 ? 5 : -5)));
      clearTimeout(_minDensTimer);
      _minDensTimer = setTimeout(applyMinDensity, 150);
    }, { passive: false });
  }

  const opInput = content.querySelector('#planOverprodInput');
  if (opInput) {
    const applyOverprod = () => {
      let v = parseInt(opInput.value);
      if (Number.isNaN(v)) return;
      v = Math.max(0, Math.min(500, v));
      if (parseInt(opInput.value) !== v) opInput.value = v;  // reflect the clamp in the field
      const store = document.getElementById('targetOverprod');
      if (store && parseInt(store.value) === v) return;  // unchanged → skip re-run
      if (store) store.value = v;
      _rerunPlan();
    };
    opInput.addEventListener('change', applyOverprod);
    opInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyOverprod(); } });
    // Wheel adjusts in steps of 5 but only re-plans once the user pauses (debounced) —
    // re-running on every tick refetched and re-rendered, which made the page jump.
    opInput.addEventListener('wheel', e => {
      e.preventDefault();
      const cur = parseInt(opInput.value) || 0;
      opInput.value = Math.max(0, Math.min(500, cur + (e.deltaY < 0 ? 5 : -5)));
      clearTimeout(_overprodTimer);
      _overprodTimer = setTimeout(applyOverprod, 150);
    }, { passive: false });
  }
  const shopBtn = content.querySelector('#ppShoppingListBtn');
  if (shopBtn) shopBtn.addEventListener('click', () => renderShoppingList(data));

  // Only scroll into view on the first render of a plan, not on in-place re-runs
  // (re-running from the overprod / factory controls otherwise jumps the page).
  if (opts.scroll !== false) content.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  _persistLastPlan();  // remember this plan so a refresh lands back on it
}

function _buildShoppingList(data) {
  // Returns { perChar: { name: [{cc, reason, system, planet_num}] }, totals: {ccName: count} }
  const perChar = {};
  const totals = {};

  for (const asgn of (data.assignments || [])) {
    const items = [];

    for (const ext of (asgn.extractors || [])) {
      if (ext.is_existing && !ext.is_replace) continue;  // already set up correctly
      const ptype = ext.planet_type || ext.best_planet_type || '?';
      const cc = ptype + ' Command Center';
      const loc = ext.system ? `${ext.system} P${ext.planet_num}` : '';
      const reason = ext.is_replace
        ? `replace ${ext.replace_ptype || ext.existing_ptype || '?'} (wrong system)`
        : 'new extractor planet';
      items.push({ cc, reason, loc });
      totals[cc] = (totals[cc] || 0) + 1;
    }

    for (const fac of (asgn.factory_assignments || [])) {
      if (fac.unplaced) continue;
      if (fac.is_existing && !fac.is_replace) continue;
      const ptype = fac.planet_type || '?';
      const cc = ptype + ' Command Center';
      const loc = fac.system ? `${fac.system} P${fac.planet_num}` : '';
      const reason = fac.is_replace ? 'replace factory (wrong system)' : 'new factory planet';
      items.push({ cc, reason, loc });
      totals[cc] = (totals[cc] || 0) + 1;
    }

    if (items.length) perChar[asgn.character_name] = items;
  }

  return { perChar, totals };
}

function renderShoppingList(data) {
  const existing = document.getElementById('ppShoppingList');
  if (existing) { existing.remove(); return; }  // toggle off

  const { totals } = _buildShoppingList(data);
  const totalCount = Object.values(totals).reduce((s, n) => s + n, 0);

  if (!totalCount) {
    alert('Nothing to buy — all planets are already set up correctly.');
    return;
  }

  const rows = Object.entries(totals)
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([cc, n]) => `<tr><td class="shop-cc">${n}×</td><td class="shop-cc-name">${cc}</td></tr>`)
    .join('');

  const div = document.createElement('div');
  div.id = 'ppShoppingList';
  div.className = 'pp-shopping-list';
  div.innerHTML = `
    <div class="plan-section-title">Command Centers to Buy
      <span class="shop-total-count">${totalCount} total</span>
    </div>
    <table class="shop-table"><tbody>${rows}</tbody></table>`;

  document.getElementById('wizPlanContent').appendChild(div);
  div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function _rerunPlan(overrides = {}) {
  try {
    const { url, body } = _planRequest(_wiz.chosenSystems);
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, ...overrides }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    _wiz.lastPlanData = data;
    renderFinalPlan(data, { scroll: false });
  } catch (e) { alert('Re-run failed: ' + e.message); }
}

async function _rerunWithFactorySystem(factorySystem) {
  _wiz.factorySystem = factorySystem;
  await _rerunPlan();
}

// Click a character block to highlight it (and dim the rest) while you set that toon up —
// click again to clear. Toggles classes in place; no re-render. Ignores clicks on buttons.
function togglePlanChar(id, ev) {
  if (ev && ev.target.closest('button, a, input, select')) return;
  const sel = _wiz.selectedChar === id ? null : id;
  _wiz.selectedChar = sel;
  document.querySelectorAll('.plan-char-block').forEach(b =>
    b.classList.toggle('plan-char-selected', sel != null && Number(b.dataset.charId) === sel));
  const wrap = document.querySelector('.plan-assignments');
  if (wrap) wrap.classList.toggle('plan-has-selection', sel != null);
}

// Per-character planet layout: "flat" (one location-ordered list) or "grouped" (a column per
// system, factory system first — for deploying one system at a time).
function setPlanGroup(v) {
  _wiz.planGroup = (v === 'flat') ? 'flat' : 'grouped';
  try { localStorage.setItem('ppPlanGroup', _wiz.planGroup); } catch (e) {}
  if (_wiz.lastPlanData) renderFinalPlan(_wiz.lastPlanData, { scroll: false });
}

async function toggleFactoryChar(charId) {
  const ids = _wiz.factoryCharIds || [];
  const idx = ids.indexOf(charId);
  if (idx >= 0) {
    _wiz.factoryCharIds = ids.filter(id => id !== charId);
  } else {
    _wiz.factoryCharIds = [...ids, charId];
  }
  await _rerunPlan();
}

// ══════════════════════════════════════════════════════════════════════════════
// Factory Layout — generate importable EVE PI templates from a chosen product
// ══════════════════════════════════════════════════════════════════════════════

let _layoutTierMap = {};   // product type_id -> tier (for SVG colouring)
let _layoutSel = [];       // [{key, type_id, name, tier, planet, launchpads, data, error}]
// Factory (P2/P3) Advanced Industry Facilities run on any planet; P4 is Barren/Temperate
// only; extractors are limited to the P0's valid_planets (from the summary).
const _LAYOUT_PLANET_TYPES = ['Barren', 'Temperate', 'Lava', 'Plasma', 'Gas', 'Ice', 'Oceanic', 'Storm'];

const _LAYOUT_LS_KEY = 'layoutSelections';
let _layoutNoStorage = (() => { try { return localStorage.getItem('layoutNoStorage') === '1'; } catch (e) { return false; } })();

async function onLayoutTabOpen() {
  await loadPiProducts();
  _ppProducts.forEach(p => { _layoutTierMap[p.type_id] = p.tier; });
  const ns = document.getElementById('layoutNoStorage');
  if (ns) ns.checked = _layoutNoStorage;
  if (!_layoutSel.length) await _restoreLayoutState();
  renderLayoutSelections();
}

// Storage-less extractors toggle (Factory Layout): buffer P0 in the launchpad, no storage hub.
async function toggleLayoutNoStorage(checked) {
  _layoutNoStorage = !!checked;
  try { localStorage.setItem('layoutNoStorage', _layoutNoStorage ? '1' : '0'); } catch (e) {}
  const btn = document.getElementById('layoutBundleBtn');
  if (btn) btn.href = _layoutBundleUrl();
  renderLayoutSelections();                     // show "Generating…" while extractors rebuild
  await Promise.all(_layoutSel.filter(e => e.tier === 1).map(_fetchLayout));
  renderLayoutSelections();
}

function _saveLayoutState() {
  try {
    const slim = _layoutSel.map(e => ({ type_id: e.type_id, name: e.name, tier: e.tier, planet: e.planet, launchpads: e.launchpads, count: e.count || 1, cc: e.cc || 5 }));
    localStorage.setItem(_LAYOUT_LS_KEY, JSON.stringify(slim));
  } catch (e) { /* ignore */ }
}

async function _restoreLayoutState() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(_LAYOUT_LS_KEY) || '[]'); } catch (e) { saved = []; }
  if (!Array.isArray(saved) || !saved.length) return;
  _layoutSel = saved.map(s => ({
    key: 'k' + Math.random().toString(36).slice(2, 8),
    type_id: s.type_id, name: s.name, tier: s.tier,
    planet: s.planet, launchpads: s.launchpads, count: s.count ?? null, cc: s.cc || 5, data: null, error: null,
  }));
  renderLayoutSelections();
  await Promise.all(_layoutSel.map(_fetchLayout));
  renderLayoutSelections();
}

// ── searchable, click-to-add results that stay open ──
function onLayoutSearch() {
  const q = document.getElementById('layoutSearch').value.trim().toLowerCase();
  const res = document.getElementById('layoutResults');
  if (!q) { res.style.display = 'none'; res.innerHTML = ''; return; }
  const added = new Set(_layoutSel.map(e => e.type_id));
  const matches = _ppProducts.filter(p => p.name.toLowerCase().includes(q)).slice(0, 16);
  if (!matches.length) {
    res.innerHTML = '<div class="layout-result-empty">No matches</div>';
  } else {
    res.innerHTML = matches.map(p => {
      const isAdded = added.has(p.type_id);
      return `<div class="layout-result${isAdded ? ' is-added' : ''}" onmousedown="event.preventDefault();addLayoutById(${p.type_id})">
        <span class="layout-tag tier-${p.tier}">P${p.tier}</span> ${_esc(p.name)}
        ${isAdded ? '<span class="layout-result-added">added ✓</span>' : ''}</div>`;
    }).join('');
  }
  res.style.display = '';
}

function hideLayoutResults() {
  const r = document.getElementById('layoutResults');
  if (r) r.style.display = 'none';
}

// Preset: add the fuel-block factory components (Coolant, Mechanical Parts,
// Enriched Uranium, Robotics) to the layout in one click.
async function addFuelBlockLayout() {
  await loadPiProducts();  // ensure _ppProducts is populated for addLayoutById
  if (!_fbBom) {
    try { _fbBom = (await (await fetch('/api/fuelblock-bom')).json()).components || []; }
    catch (e) { _fbBom = []; }
  }
  for (const c of _fbBom) {
    if (c.is_factory) await addLayoutById(c.type_id);
  }
}

function addLayoutTop() {
  const q = document.getElementById('layoutSearch').value.trim().toLowerCase();
  if (!q) return;
  const m = _ppProducts.find(p => p.name.toLowerCase() === q) || _ppProducts.find(p => p.name.toLowerCase().includes(q));
  if (m) addLayoutById(m.type_id);
}

async function addLayoutById(typeId) {
  const prod = _ppProducts.find(p => p.type_id === typeId);
  if (!prod) return;
  if (_layoutSel.some(e => e.type_id === typeId)) return;  // already added
  const entry = {
    key: 'k' + Date.now() + Math.random().toString(36).slice(2, 6),
    type_id: prod.type_id, name: prod.name, tier: prod.tier,
    planet: document.getElementById('layoutPlanetType').value,
    cc: parseInt(document.getElementById('layoutCcu')?.value, 10) || 5,
    launchpads: prod.tier === 1 ? 1 : 3, count: null, data: null, error: null,
  };
  _layoutSel.push(entry);
  _saveLayoutState();
  renderLayoutSelections();
  onLayoutSearch();   // refresh results to mark this one "added"
  await _fetchLayout(entry);
  renderLayoutSelections();
}

async function _fetchLayout(entry) {
  try {
    const resp = await fetch('/api/layout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: entry.type_id, planet_type: entry.planet, launchpads: entry.launchpads, count: entry.count == null ? null : entry.count, cc_level: entry.cc || 5, no_storage: _layoutNoStorage }),
    });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.status); }
    entry.data = await resp.json();
    if (entry.data.summary && entry.data.summary.count != null) entry.count = entry.data.summary.count;  // resolve auto-max
    // Sync to the planet the backend actually used (it coerces to a type that yields the P0,
    // e.g. Reactive Gas → Gas), so the per-card selector, bundle and download all agree.
    if (entry.data.summary && entry.data.summary.planet_type) entry.planet = entry.data.summary.planet_type;
    entry.error = null;
  } catch (e) { entry.error = String(e.message || e); }
}

function removeLayout(key) {
  _layoutSel = _layoutSel.filter(e => e.key !== key);
  _saveLayoutState();
  renderLayoutSelections();
}

async function changeLayoutLaunchpads(key, val) {
  const entry = _layoutSel.find(e => e.key === key);
  if (!entry) return;
  entry.launchpads = Math.max(1, Math.min(8, parseInt(val, 10) || 1));
  _saveLayoutState();
  await _fetchLayout(entry);
  renderLayoutSelections();
}

// Scroll wheel over the launchpad input changes the value (debounced regenerate).
const _lpDebounce = {};
function _layoutWheelLp(e, input, key) {
  e.preventDefault();
  const entry = _layoutSel.find(x => x.key === key);
  if (!entry) return;
  const v = Math.max(1, Math.min(8, (parseInt(input.value, 10) || 1) + (e.deltaY < 0 ? 1 : -1)));
  input.value = v;
  entry.launchpads = v;
  _saveLayoutState();
  const btn = document.getElementById('layoutBundleBtn');
  if (btn) btn.href = _layoutBundleUrl();
  clearTimeout(_lpDebounce[key]);
  _lpDebounce[key] = setTimeout(async () => { await _fetchLayout(entry); renderLayoutSelections(); }, 350);
}

function _iskFmt(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (a >= 1e3) return (v / 1e3).toFixed(0) + 'k';
  return Math.round(v);
}

function _economicsLine(e, isExtractor) {
  if (!e || !e.output_price) return '';
  if (isExtractor)
    return `<div class="layout-card-isk">≈ <b>${_iskFmt(e.revenue_per_day)}</b> ISK/day <span class="layout-sub">Jita sell</span></div>`;
  const net = e.profit_per_day;
  return `<div class="layout-card-isk${net < 0 ? ' isk-neg' : ''}">≈ <b>${_iskFmt(net)}</b> ISK/day net
    <span class="layout-sub">${_iskFmt(e.revenue_per_day)} gross − ${_iskFmt(e.input_cost_per_day)} P1</span></div>`;
}

function _resourcesLine(res) {
  if (!res) return '';
  const cls = res.over ? 'res-over' : (Math.max(res.cpu_pct, res.pg_pct) >= 90 ? 'res-warn' : '');
  const k = v => (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k';
  return `<div class="layout-card-res ${cls}">CC${res.cc_level}: CPU <b>${res.cpu_pct}%</b> (${k(res.cpu)}/${k(res.cpu_max)}) · PG <b>${res.pg_pct}%</b> (${k(res.pg)}/${k(res.pg_max)})${res.over ? ' — OVER BUDGET' : ''}</div>`;
}

function _layoutBundleUrl() {
  // token: tid:lp:count:cc:ptype — include planet type + CC so the zip matches each card
  // (previously omitted, so "Download all" silently generated everything at Barren/CC5).
  const toks = _layoutSel.map(e =>
    `${e.type_id}:${e.launchpads}:${e.count || 1}:${e.cc || 5}:${e.planet}`).join(',');
  return `/api/layout/bundle?type_ids=${encodeURIComponent(toks)}&expand=0${_layoutNoStorage ? '&no_storage=1' : ''}`;
}

function renderLayoutSelections() {
  const el = document.getElementById('layoutPlanets');
  const bundleBtn = document.getElementById('layoutBundleBtn');
  if (bundleBtn) {
    bundleBtn.style.display = _layoutSel.length > 1 ? '' : 'none';
    bundleBtn.href = _layoutBundleUrl();
  }
  el.innerHTML = _layoutSel.map(renderLayoutCard).join('');
}

function renderLayoutCard(entry) {
  const head = `
    <div class="pp-card-title">
      <span class="layout-tag tier-${entry.tier}">P${entry.tier}</span>
      <span class="layout-card-name">${_esc(entry.name)}</span>
      <button class="layout-card-x" title="Remove" onclick="removeLayout('${entry.key}')">✕</button>
    </div>`;
  if (entry.error) return `<div class="pp-card layout-card">${head}<div class="layout-card-body"><div class="layout-card-meta" style="color:#e07a7a">Error: ${_esc(entry.error)}</div></div></div>`;
  if (!entry.data) return `<div class="pp-card layout-card">${head}<div class="layout-card-body"><div class="layout-card-meta">Generating…</div></div></div>`;

  const s = entry.data.summary, p = entry.data.planets[0];
  const isExtractor = s.kind === 'extractor';
  const byTier = Object.entries(s.facilities_by_tier).map(([k, v]) => `${k}: ${v}`).join(' · ');
  const structs = Object.entries(p.structures)
    .filter(([k, v]) => v && k !== 'facility')
    .map(([k, v]) => `<b>${v}</b> ${_esc(k)}`).join(' · ');
  const outStr = `<b>${s.product_per_hour}</b>/hr`;
  let info;
  if (isExtractor) {
    const others = (s.valid_planets || []).filter(x => x !== s.planet_type);
    info = `
      <div class="layout-card-line">${outStr} · extracts <b>${_esc(s.extracts)}</b> · ${s.heads} heads</div>
      <div class="layout-card-meta">planet: ${_esc(s.planet_type)} · <b>CC${entry.cc || 5}</b>${others.length ? ' · also: ' + others.map(_esc).join(', ') : ''}</div>`;
  } else {
    const imports = s.imports.map(i => `${_esc(i.name)} <b>${i.per_hour}/hr</b>`).join(', ');
    info = `
      <div class="layout-card-line">${outStr} · ${(s.buffer_m3 / 1000).toLocaleString()} km³ buffer · planet: ${_esc(s.planet_type)} · <b>CC${entry.cc || 5}</b></div>
      <div class="layout-card-meta">${_esc(s.imports_label)}: ${imports}</div>`;
  }
  const url = `/api/layout/download?type_id=${entry.type_id}&planet_type=${encodeURIComponent(s.planet_type)}&launchpads=${entry.launchpads}&count=${entry.count || 1}&cc_level=${entry.cc || 5}${(_layoutNoStorage && entry.tier === 1) ? '&no_storage=1' : ''}`;
  const countLabel = isExtractor ? 'Factories' : (entry.tier === 2 ? 'Factories' : 'Chains');
  const ccSel = `<label title="Command Center level — fewer facilities fit at lower levels">CC
    <select onchange="changeLayoutCcu('${entry.key}', this.value)">${[5,4,3,2,1].map(n => `<option value="${n}"${n === (entry.cc || 5) ? ' selected' : ''}>${n}</option>`).join('')}</select></label>`;
  // Per-card planet picker: extractors are limited to the P0's valid planets; P4 to
  // Barren/Temperate; P2/P3 run anywhere. Hidden when there's only one valid choice
  // (e.g. Oxidizing Compound → Gas only).
  const planetOpts = isExtractor ? (s.valid_planets || [s.planet_type])
                   : entry.tier === 4 ? ['Barren', 'Temperate']
                   : _LAYOUT_PLANET_TYPES;
  const planetSel = planetOpts.length > 1
    ? `<label title="${isExtractor ? 'Planet types that yield this P0' : 'Planet type for the factory'}">Planet
        <select onchange="changeLayoutPlanet('${entry.key}', this.value)">${planetOpts.map(pt => `<option value="${pt}"${pt === s.planet_type ? ' selected' : ''}>${_esc(pt)}</option>`).join('')}</select></label>`
    : '';
  return `
    <div class="pp-card layout-card">
      ${head}
      <div class="layout-card-body">
        ${_layoutPreviewSvg(p.template)}
        <div class="layout-card-info">
          <div class="layout-card-structs">${structs}</div>
          ${info}
          ${_economicsLine(s.economics, isExtractor)}
          <div class="layout-card-meta">${p.pins} pins · ${p.links} links · ${p.routes} routes · facilities (${byTier})</div>
          ${_resourcesLine(p.resources)}
        </div>
      </div>
      <div class="layout-card-controls">
        ${isExtractor ? '' : `<label title="Max that fits the command center: ${s.max_count}">${countLabel} <input type="number" min="1" max="99" value="${entry.count || 1}"
          onchange="changeLayoutCount('${entry.key}', this.value)"
          onwheel="_layoutWheelCount(event, this, '${entry.key}')"><span class="layout-max">/${s.max_count}</span></label>`}
        <label>Launchpads <input type="number" min="1" max="8" value="${entry.launchpads}"
          onchange="changeLayoutLaunchpads('${entry.key}', this.value)"
          onwheel="_layoutWheelLp(event, this, '${entry.key}')"></label>
        ${planetSel}
        ${ccSel}
        <a class="layout-btn" href="${url}" download>Download .json</a>
        <button class="layout-btn" onclick="copyLayoutEntry('${entry.key}', this)">Copy JSON</button>
      </div>
    </div>`;
}

// Global Command Center selector: re-apply that level to every card (a "set all"),
// re-resolving the max facilities that fit at the new level.
async function applyGlobalCcu() {
  const cc = Math.max(1, Math.min(5, parseInt(document.getElementById('layoutCcu').value, 10) || 5));
  if (!_layoutSel.length) return;
  for (const e of _layoutSel) { e.cc = cc; e.count = null; }
  _saveLayoutState();
  renderLayoutSelections();              // show "Generating…" immediately
  await Promise.all(_layoutSel.map(_fetchLayout));
  renderLayoutSelections();
}

async function changeLayoutCcu(key, val) {
  const entry = _layoutSel.find(e => e.key === key);
  if (!entry) return;
  entry.cc = Math.max(1, Math.min(5, parseInt(val, 10) || 5));
  entry.count = null;   // re-resolve max that fits at the new CC level
  _saveLayoutState();
  await _fetchLayout(entry);
  renderLayoutSelections();
}

async function changeLayoutPlanet(key, val) {
  const entry = _layoutSel.find(e => e.key === key);
  if (!entry) return;
  entry.planet = val;
  entry.count = null;   // planet diameter changes how many facilities fit → re-resolve max
  _saveLayoutState();
  await _fetchLayout(entry);
  renderLayoutSelections();
}

async function changeLayoutCount(key, val) {
  const entry = _layoutSel.find(e => e.key === key);
  if (!entry) return;
  entry.count = Math.max(1, Math.min(99, parseInt(val, 10) || 1));
  _saveLayoutState();
  await _fetchLayout(entry);   // count packs real units onto the planet
  renderLayoutSelections();
}

const _countDebounce = {};
function _layoutWheelCount(e, input, key) {
  e.preventDefault();
  const entry = _layoutSel.find(x => x.key === key);
  if (!entry) return;
  const v = Math.max(1, Math.min(99, (parseInt(input.value, 10) || 1) + (e.deltaY < 0 ? 1 : -1)));
  input.value = v;
  entry.count = v;
  _saveLayoutState();
  clearTimeout(_countDebounce[key]);
  _countDebounce[key] = setTimeout(async () => { await _fetchLayout(entry); renderLayoutSelections(); }, 350);
}

async function copyLayoutEntry(key, btn) {
  const entry = _layoutSel.find(e => e.key === key);
  if (!entry || !entry.data) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(entry.data.planets[0].template));
    const old = btn.textContent; btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = old; }, 1200);
  } catch (e) { btn.textContent = 'Copy failed'; }
}

// Plot pins in raw lat/lon (EVE's flat plane) with equal aspect so the preview matches
// the in-game layout. Pins coloured by role: launchpad/storage hub, extractor, or tier.
let _svgUid = 0;
function _layoutPreviewSvg(t) {
  const W = 300, H = 220, pad = 22;
  const xs = t.P.map(p => p.Lo), ys = t.P.map(p => p.La);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const span = Math.max(xMax - xMin, yMax - yMin) || 1;
  const scale = (Math.min(W, H) - 2 * pad) / span;
  const cx = (xMin + xMax) / 2, cy = (yMin + yMax) / 2;
  const px = p => W / 2 - (p.Lo - cx) * scale;  // flip lon to match in-game orientation
  const py = p => H / 2 - (p.La - cy) * scale;  // north up
  const role = p => {
    if (p.S === null) return { cls: 'lp-hub', r: 6.5 };
    const tier = _layoutTierMap[p.S];
    if (!tier) return { cls: 'lp-ecu', r: 7 };                 // ECU (S = a P0)
    return { cls: `lp-t${tier}`, r: tier >= 4 ? 7 : tier === 3 ? 6 : 5 };
  };
  const links = t.L.map(l => {
    const a = t.P[l.S - 1], b = t.P[l.D - 1];
    return `<line x1="${px(a).toFixed(1)}" y1="${py(a).toFixed(1)}" x2="${px(b).toFixed(1)}" y2="${py(b).toFixed(1)}"/>`;
  }).join('');
  const dots = t.P.map(p => {
    const { cls, r } = role(p);
    return `<circle cx="${px(p).toFixed(1)}" cy="${py(p).toFixed(1)}" r="${r}" class="${cls}"/>`;
  }).join('');
  // Unique def ids per render — duplicate ids across cards break filter/gradient refs.
  const uid = 'lp' + (_svgUid++);
  return `<svg class="layout-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    <defs>
      <radialGradient id="${uid}bg" cx="50%" cy="45%" r="70%">
        <stop offset="0%" stop-color="#16202e"/><stop offset="100%" stop-color="#0b0e15"/>
      </radialGradient>
      <filter id="${uid}glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <rect x="0" y="0" width="${W}" height="${H}" rx="8" fill="url(#${uid}bg)"/>
    <g class="lp-links" filter="url(#${uid}glow)">${links}</g>
    <g class="lp-pins" filter="url(#${uid}glow)">${dots}</g>
  </svg>`;
}

