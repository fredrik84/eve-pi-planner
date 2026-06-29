// Admin tab — split out of planetary.js (2026-06-23) to keep feature files small.
// Planet submissions, feature flags, baskets, admin users, bug triage.
// Loaded as a separate <script> after planetary.js; all functions are global and resolve
// at call time. Shared state/util (_esc, _fmtIsk, _features, _featureActive, _ppCharsData,
// _isAdmin, switchTab) lives in planetary.js, which loads first.

// ── Admin tab ───────────────────────────────────────────────────────────────────
let _adminPage = (() => { try { return localStorage.getItem('adminPage') || 'submissions'; } catch (e) { return 'submissions'; } })();
function onAdminTabOpen() {
  // Only bounce a CONFIRMED non-admin (session loaded), and to a mobile-visible tab — bouncing to
  // the hidden planner before _isAdmin had loaded was shuffling phone users off a restored admin tab.
  // When state is still unknown, proceed: the admin endpoints are authenticated server-side, so they
  // populate for a real admin and 403 gracefully otherwise (loadCharacters then bounces a non-admin).
  if (_sessionLoaded && !_isAdmin) { switchTab('dashboard'); return; }
  // Load every section's data on open so the nav badges are populated; the sub-nav just toggles
  // which section is visible.
  loadPlanetSubmissions();
  loadBugs();
  loadAdmins();
  loadTesters();
  loadAdminFeatures();
  adminSubPage(_adminPage);
}

// Admin sub-navigation: driven from the sidebar nav-group.
// adminNavTo() is called by sidebar buttons; adminSubPage() handles the panel logic.
function adminNavTo(key) {
  switchTab('admin');
  adminSubPage(key);
}

function adminSubPage(key) {
  _adminPage = key;
  try { localStorage.setItem('adminPage', key); } catch (e) {}
  // Mark active item in the sidebar nav-group.
  document.querySelectorAll('#adminNavGroup .admin-nav-item').forEach(b =>
    b.classList.toggle('active', b.dataset.page === key));
  // Show the matching sub-page panel.
  document.querySelectorAll('#tab-admin .admin-subpage').forEach(p => {
    p.style.display = (p.dataset.page === key) ? '' : 'none';
  });
  // Lazy-load expensive sub-pages only when opened.
  if (key === 'wallet' && typeof loadCorpWallet === 'function') loadCorpWallet();
  if (key === 'users') _loadCharNameSuggestions();
  if (key === 'stats') loadAdminStats();
}

async function _loadCharNameSuggestions() {
  const dl = document.getElementById('charNameSuggestions');
  if (!dl) return;
  try {
    const { names } = await (await fetch('/api/character-names')).json();
    dl.innerHTML = (names || []).map(n => `<option value="${_esc(n)}">`).join('');
  } catch (e) { /* suggestions are best-effort */ }
}

// Feature flags: 4-state (hidden / admin / testers / public) per feature.
const _FEATURE_STATES = [
  { key: 'hidden',  label: 'Hidden',  title: 'Nobody sees this feature, including admins' },
  { key: 'admin',   label: 'Admin',   title: 'Visible to admins only' },
  { key: 'testers', label: 'Testers', title: 'Visible to admins and testers' },
  { key: 'public',  label: 'Public',  title: 'Visible to everyone' },
];
async function loadAdminFeatures() {
  const el = document.getElementById('adminFeatureList');
  if (!el) return;
  await _loadFeatures();
  const feats = Object.values(_features);
  if (!feats.length) { el.innerHTML = '<div class="pp-empty">No features registered.</div>'; return; }
  el.innerHTML = feats.map(f => {
    const cur = f.state || (f.enabled ? 'public' : 'admin');
    const btns = _FEATURE_STATES.map(s =>
      `<button class="afs-btn${cur === s.key ? ' afs-active afs-' + s.key : ''}" title="${s.title}"
         onclick="setFeatureState('${f.key}','${s.key}')">${s.label}</button>`
    ).join('');
    return `<div class="admin-feature-row">
      <div class="admin-feature-head">
        <span class="admin-feature-name">${_esc(f.label)}</span>
        <div class="afs-group">${btns}</div>
      </div>
      <div class="admin-feature-desc">${_esc(f.description)}</div>
    </div>`;
  }).join('');
}

async function setFeatureState(key, state) {
  try {
    const resp = await fetch('/api/features/' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    if (_features[key]) { _features[key].state = state; _features[key].enabled = state === 'public'; }
    loadAdminFeatures();
    _applyTabGates();
  } catch (e) { alert('Failed to update feature: ' + e.message); }
}

// ── Tester management ─────────────────────────────────────────────────────────
async function loadTesters() {
  const el = document.getElementById('testerList');
  if (!el) return;
  try {
    const data = await (await fetch('/api/testers')).json();
    const rows = (data.testers || []).map(t =>
      `<div class="admin-row"><span class="admin-name">${_esc(t.character_name)}</span>`
      + `<span class="admin-meta">${t.added_by ? 'by ' + _esc(t.added_by) : ''}</span>`
      + `<button class="bug-act bug-act-ignore" onclick="removeTester('${_esc(t.character_name).replace(/'/g, "\\'")}')">Remove</button></div>`).join('');
    el.innerHTML = rows || '<div class="pp-empty">No testers yet.</div>';
  } catch (e) { el.innerHTML = `<div class="pp-empty">Failed: ${_esc(e.message)}</div>`; }
}

async function addTester() {
  const inp = document.getElementById('testerNameInput');
  const status = document.getElementById('testerAddStatus');
  const name = inp.value.trim();
  if (!name) return;
  status.textContent = 'Adding…';
  try {
    const resp = await fetch('/api/testers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character_name: name }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    inp.value = ''; status.textContent = '';
    loadTesters();
  } catch (e) { status.textContent = e.message; }
}

async function removeTester(name) {
  if (!confirm(`Remove tester "${name}"?`)) return;
  try {
    const resp = await fetch(`/api/testers/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    loadTesters();
  } catch (e) { alert('Failed: ' + e.message); }
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
  const navBadge = document.getElementById('adminNavPsub');
  if (navBadge) navBadge.textContent = subs.length ? String(subs.length) : '';
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
  const navBadge = document.getElementById('adminNavBugs');
  if (navBadge) navBadge.textContent = counts.open ? String(counts.open) : '';
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

// ── System stats ─────────────────────────────────────────────────────────────
async function loadAdminStats() {
  const el = document.getElementById('adminStatsContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  try {
    const resp = await fetch('/api/admin/stats');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    renderAdminStats(await resp.json());
  } catch (e) {
    el.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
  _checkPrometheusStatus();
}

async function _checkPrometheusStatus() {
  const el = document.getElementById('adminPrometheusStatus');
  if (!el) return;
  try {
    const resp = await fetch('/metrics');
    if (resp.status === 404) {
      el.innerHTML = '<span class="admin-prom-status admin-prom-off">Disabled — set PROMETHEUS_ENABLED=1 to enable</span>';
    } else if (resp.status === 401) {
      el.innerHTML = '<span class="admin-prom-status admin-prom-on">Enabled — secured with PROMETHEUS_TOKEN</span>';
    } else if (resp.ok) {
      el.innerHTML = '<span class="admin-prom-status admin-prom-warn">Enabled — no token set, endpoint is open</span>';
    } else {
      el.innerHTML = `<span class="admin-prom-status">${resp.status}</span>`;
    }
  } catch (e) {
    el.innerHTML = '';
  }
}

function renderAdminStats(s) {
  const el = document.getElementById('adminStatsContent');
  if (!el) return;
  const tile = (val, lbl) =>
    `<div class="an-stat"><div class="an-stat-val">${val}</div><div class="an-stat-lbl">${lbl}</div></div>`;

  const groups = [
    {
      heading: 'Users',
      tiles: [
        tile(s.contexts, 'accounts'),
        tile(s.active_7d, 'active 7d'),
        tile(s.active_30d, 'active 30d'),
        tile(s.characters, 'characters'),
        tile(s.dummy_characters, 'dummy chars'),
      ],
    },
    {
      heading: 'Planet DB',
      tiles: [
        tile(s.planet_rows.toLocaleString(), 'planet rows'),
        tile(s.systems_covered.toLocaleString(), 'systems'),
        tile(s.constellations_covered, 'constellations'),
        tile(s.pending_submissions, 'pending submissions'),
      ],
    },
    {
      heading: 'Usage',
      tiles: [
        tile(s.profiles, 'saved profiles'),
        tile(s.shares, 'shares total'),
        tile(s.shares_7d, 'shares 7d'),
        tile(s.shares_30d, 'shares 30d'),
        tile(s.shares_never_accessed, 'shares never opened'),
        tile(s.bugs_open + ' / ' + s.bugs_total, 'bugs open / total'),
      ],
    },
    {
      heading: 'Raw data',
      tiles: [
        tile(s.char_planet_scans, 'colony scans'),
        tile(s.colony_yield_rows, 'yield records'),
        tile(s.sessions, 'sessions'),
        tile(s.sessions_stale_90d, 'sessions >90d old'),
      ],
    },
  ];

  el.innerHTML = groups.map(g => `
    <div class="admin-stats-group">
      <div class="admin-stats-heading">${_esc(g.heading)}</div>
      <div class="an-stats">${g.tiles.join('')}</div>
    </div>`).join('');
}
