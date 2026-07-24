// Admin tab — split out of planetary.js (2026-06-23) to keep feature files small.
// Planet submissions, feature flags, baskets, admin users, bug triage.
// Loaded as a separate <script> after planetary.js; all functions are global and resolve
// at call time. Shared state/util (_esc, _fmtIsk, _features, _featureActive, _ppCharsData,
// _isAdmin, switchTab) lives in planetary.js, which loads first.

// ── Admin tab ───────────────────────────────────────────────────────────────────
// Site-admin-only sub-pages — hidden from a pure group manager (manages a group's own data
// without being a full site admin). Anything NOT in this set (moongoo, groups-mine — the
// group-scoped ones) is visible to both.
const _SITE_ADMIN_ONLY_PAGES = new Set(['stats', 'features', 'users', 'submissions', 'bugs', 'baskets', 'wallet', 'cleanup', 'groups']);

let _adminPage = (() => { try { return localStorage.getItem('adminPage') || 'submissions'; } catch (e) { return 'submissions'; } })();
function onAdminTabOpen() {
  // Only bounce a CONFIRMED non-admin/non-manager (session loaded), and to a mobile-visible tab —
  // bouncing to the hidden planner before _isAdmin had loaded was shuffling phone users off a
  // restored admin tab. When state is still unknown, proceed: the admin endpoints are
  // authenticated server-side, so they populate for an authorized caller and 403 gracefully
  // otherwise (loadCharacters then bounces someone with neither role).
  if (_sessionLoaded && !_isAdmin && !_isGroupManager) { switchTab('dashboard'); return; }
  _applyAdminNavVisibility();
  // A pure group manager landing on a site-admin-only page (e.g. the hardcoded 'submissions'
  // default, or a leftover localStorage value from once viewing as an admin) has nothing to see
  // there — redirect to their own group-scoped moon-goo editor instead.
  if (!_isAdmin && _isGroupManager && _SITE_ADMIN_ONLY_PAGES.has(_adminPage)) _adminPage = 'moongoo';
  // Load every section's data on open so the nav badges are populated; the sub-nav just toggles
  // which section is visible. Site-admin-only sections are skipped for a pure group manager —
  // their own endpoints would just 403.
  if (_isAdmin) {
    loadPlanetSubmissions();
    loadBugs();
    loadAdmins();
    loadTesters();
    loadAdminFeatures();
    if (typeof loadGroups === 'function') loadGroups();
  }
  adminSubPage(_adminPage);
}

// Which sidebar/mobile sub-nav buttons show at all — site-admin-only pages disappear entirely
// for a pure group manager, rather than being visible-but-403ing on click.
function _applyAdminNavVisibility() {
  document.querySelectorAll('#adminNavGroup .admin-nav-item, #adminMobilePageNav .admin-mobile-page-btn').forEach(b => {
    if (_SITE_ADMIN_ONLY_PAGES.has(b.dataset.page)) b.style.display = _isAdmin ? '' : 'none';
  });
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
  // Mark active item in the desktop sidebar nav-group.
  document.querySelectorAll('#adminNavGroup .admin-nav-item').forEach(b =>
    b.classList.toggle('active', b.dataset.page === key));
  // Mark active item in the mobile inline nav.
  document.querySelectorAll('#adminMobilePageNav .admin-mobile-page-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.page === key));
  // Show the matching sub-page panel.
  document.querySelectorAll('#tab-admin .admin-subpage').forEach(p => {
    p.style.display = (p.dataset.page === key) ? '' : 'none';
  });
  // Lazy-load expensive sub-pages only when opened.
  if (key === 'wallet' && typeof loadCorpWallet === 'function') loadCorpWallet();
  if (key === 'users') _loadCharNameSuggestions();
  if (key === 'stats') loadAdminStats();
  if (key === 'cleanup') loadCleanupPreview();
  if (key === 'groups' && typeof loadGroups === 'function') loadGroups();
  if (key === 'moongoo') loadMoonGoo();
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
// Which group <details> are collapsed, persisted so an admin's fold state survives reloads.
// Absence = expanded (matches the old always-expanded behaviour, so nothing regresses on first load).
function _collapsedFeatureGroups() {
  try { return new Set(JSON.parse(localStorage.getItem('adminFeatureGroupsCollapsed') || '[]')); }
  catch (e) { return new Set(); }
}
function _toggleFeatureGroup(name, open) {
  const s = _collapsedFeatureGroups();
  if (open) s.delete(name); else s.add(name);
  try { localStorage.setItem('adminFeatureGroupsCollapsed', JSON.stringify([...s])); } catch (e) {}
}
async function loadAdminFeatures() {
  const el = document.getElementById('adminFeatureList');
  if (!el) return;
  await _loadFeatures();
  const feats = Object.values(_features);
  if (!feats.length) { el.innerHTML = '<div class="pp-empty">No features registered.</div>'; return; }
  const byGroup = {};
  feats.forEach(f => { (byGroup[f.group || 'Other'] = byGroup[f.group || 'Other'] || []).push(f); });
  const order = (_featuresGroupOrder || []).filter(g => byGroup[g]);
  Object.keys(byGroup).forEach(g => { if (!order.includes(g)) order.push(g); });   // unlisted groups sort last
  const collapsed = _collapsedFeatureGroups();
  el.innerHTML = order.map(group => {
    const rows = byGroup[group].map(f => {
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
    const open = !collapsed.has(group);
    return `<details class="admin-feature-group"${open ? ' open' : ''} ontoggle="_toggleFeatureGroup('${_esc(group)}', this.open)">
        <summary class="admin-feature-group-h">${_esc(group)} <span class="admin-feature-group-count">${byGroup[group].length}</span></summary>
        <div class="admin-feature-list">${rows}</div>
      </details>`;
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
        tile(_fmtIsk(s.pi_produced_value_total ?? 0), 'PI produced (est., all)'),
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
      heading: 'Reactions',
      tiles: [
        tile(s.reaction_characters ?? 0, 'characters using'),
        tile(s.reaction_accounts ?? 0, 'accounts using'),
        tile((s.reaction_jobs_planned ?? 0).toLocaleString(), 'jobs planned'),
        tile((s.reaction_jobs_running ?? 0).toLocaleString(), 'jobs running'),
        tile((s.reaction_orders_open ?? 0) + ' / ' + (s.reaction_orders_total ?? 0), 'orders open / total'),
        tile(_fmtIsk(s.reaction_turnover_total ?? 0), 'turnover (all accounts)'),
        tile(_fmtIsk(s.reaction_net_profit_total ?? 0), 'net profit (all accounts)'),
        tile((s.reaction_completions_total ?? 0).toLocaleString(), 'reactions completed'),
      ],
    },
    {
      heading: 'Manufacturing',
      tiles: [
        tile((s.manufacturing_users_total ?? 0).toLocaleString(), 'accounts used'),
        tile(_fmtIsk(s.manufacturing_turnover_total ?? 0), 'turnover (all accounts)'),
        tile(_fmtIsk(s.manufacturing_net_profit_total ?? 0), 'net profit (all accounts)'),
        tile((s.manufacturing_completions_total ?? 0).toLocaleString(), 'jobs completed'),
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

  const commit = (typeof _featuresGitCommit !== 'undefined') ? _featuresGitCommit : 'unknown';
  const shortCommit = commit.length > 7 ? commit.slice(0, 7) : commit;
  const commitLink = commit !== 'unknown'
    ? `<a href="https://github.com/fredrik84/eve-pi-planner/commit/${commit}" target="_blank" rel="noopener">${shortCommit}</a>`
    : shortCommit;

  el.innerHTML = groups.map(g => `
    <div class="admin-stats-group">
      <div class="admin-stats-heading">${_esc(g.heading)}</div>
      <div class="an-stats">${g.tiles.join('')}</div>
    </div>`).join('')
    + `<div class="admin-stats-group"><div class="admin-stats-heading">Deployment</div>`
    + `<div class="an-stats">${tile(commitLink, 'git commit')}</div></div>`;
}

// ── DB Cleanup ────────────────────────────────────────────────────────────────
const _CLEANUP_LABELS = {
  old_sessions:        { label: 'Old sessions',         unit: 'session' },
  old_shares:          { label: 'Stale share links',    unit: 'share' },
  empty_contexts:      { label: 'Empty accounts',       unit: 'account' },
  orphaned_plan_config:  { label: 'Orphaned plan config', unit: 'row' },
  orphaned_colony_yield: { label: 'Orphaned yield data',  unit: 'row' },
};

let _cleanupPreview = null;

async function loadCleanupPreview() {
  const el = document.getElementById('cleanupContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading preview…</div>';
  try {
    const resp = await fetch('/api/admin/cleanup/preview');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _cleanupPreview = await resp.json();
    renderCleanupPreview(_cleanupPreview);
  } catch (e) {
    el.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function _cleanupDetail(key, data) {
  // Returns a plain-English detail string for the preview table.
  if (key === 'old_sessions') {
    if (!data.count) return 'None — nothing to clean';
    return `${data.count} session${data.count !== 1 ? 's' : ''}, oldest ${(data.oldest || '').slice(0, 10)}, newest ${(data.newest || '').slice(0, 10)}`;
  }
  if (key === 'old_shares') {
    if (!data.count) {
      return `None older than 90 days${data.never_accessed ? ` (${data.never_accessed} have never been opened — check back after 90 days)` : ''}`;
    }
    return `${data.count} share${data.count !== 1 ? 's' : ''} not opened since ${(data.oldest || '').slice(0, 10)}`;
  }
  if (key === 'empty_contexts') {
    if (!data.count) return 'None — nothing to clean';
    const extra = data.also_sessions ? ` + ${data.also_sessions} dangling session${data.also_sessions !== 1 ? 's' : ''}` : '';
    const ids = (data.items || []).map(i => `account ${i.id} (created ${i.created})`).join(', ');
    return `${data.count} account${data.count !== 1 ? 's' : ''} with no characters${extra}: ${ids}`;
  }
  if (key === 'orphaned_plan_config') {
    return data.count ? `${data.count} config row${data.count !== 1 ? 's' : ''} for deleted characters` : 'None — nothing to clean';
  }
  if (key === 'orphaned_colony_yield') {
    return data.count ? `${data.count} yield row${data.count !== 1 ? 's' : ''} for deleted characters` : 'None — nothing to clean';
  }
  return '';
}

function renderCleanupPreview(preview) {
  const el = document.getElementById('cleanupContent');
  if (!el) return;

  const rows = Object.entries(_CLEANUP_LABELS).map(([key, meta]) => {
    const data = preview[key] || { count: 0 };
    const hasWork = data.count > 0;
    const detail = _cleanupDetail(key, data);
    const checked = hasWork ? 'checked' : '';
    const dimmed = hasWork ? '' : ' cleanup-row-empty';
    return `<tr class="cleanup-row${dimmed}">
      <td class="cleanup-check"><input type="checkbox" id="chk_${key}" ${checked} ${hasWork ? '' : 'disabled'}></td>
      <td class="cleanup-cat">${_esc(meta.label)}</td>
      <td class="cleanup-count">${hasWork ? `<strong>${data.count}</strong> ${meta.unit}${data.count !== 1 ? 's' : ''}` : '—'}</td>
      <td class="cleanup-detail">${_esc(detail)}</td>
    </tr>`;
  }).join('');

  const anyWork = Object.values(preview).some(d => d.count > 0);

  el.innerHTML = `
    <table class="cleanup-table">
      <thead><tr>
        <th></th>
        <th>Category</th>
        <th>Will delete</th>
        <th>Detail</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="cleanup-actions">
      <button class="cleanup-run-btn" onclick="runCleanup()" ${anyWork ? '' : 'disabled'}>Run cleanup</button>
      <button onclick="loadCleanupPreview()" class="cleanup-refresh-btn">Refresh preview</button>
      <span id="cleanupStatus" class="cleanup-status"></span>
    </div>`;
}

async function runCleanup() {
  const preview = _cleanupPreview;
  if (!preview) return;

  // Collect checked categories
  const cats = Object.keys(_CLEANUP_LABELS).filter(key => {
    const cb = document.getElementById('chk_' + key);
    return cb && cb.checked;
  });
  if (!cats.length) { alert('Nothing selected.'); return; }

  // Build confirm message
  const lines = cats.map(key => {
    const data = preview[key] || { count: 0 };
    const meta = _CLEANUP_LABELS[key];
    return `• ${meta.label}: ${data.count} ${meta.unit}${data.count !== 1 ? 's' : ''}`;
  });
  if (!confirm(`Permanently delete:\n\n${lines.join('\n')}\n\nThis cannot be undone.`)) return;

  const statusEl = document.getElementById('cleanupStatus');
  if (statusEl) statusEl.textContent = 'Running…';
  document.querySelectorAll('.cleanup-run-btn').forEach(b => b.disabled = true);

  try {
    const resp = await fetch('/api/admin/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ categories: cats }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const result = await resp.json();
    const deleted = result.deleted || {};
    const summary = Object.entries(deleted)
      .map(([k, n]) => `${_CLEANUP_LABELS[k]?.label || k}: ${n} deleted`)
      .join(' · ');
    if (statusEl) statusEl.textContent = summary || 'Done.';
    // Reload preview so counts drop to 0
    await loadCleanupPreview();
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Failed: ' + e.message;
    document.querySelectorAll('.cleanup-run-btn').forEach(b => b.disabled = false);
  }
}

// ── Moon-goo prices (group-scoped — a group's own alliance price sheet, feeds Reactions) ───
let _moonGooGroups = [];   // groups the caller can manage (site admin: all; manager: their own)
let _moonGooGroupId = null;
let _moonGooMaterials = [];        // canonical [{type_id, name}] catalog of raw moon materials
let _moonGooCurrentTypeIds = new Set();  // type_ids already on the selected group's sheet (excluded from the picker)

async function loadMoonGoo() {
  const el = document.getElementById('moonGooList');
  const pickerRow = document.getElementById('moonGooGroupPickerRow');
  const picker = document.getElementById('moonGooGroupPicker');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  try {
    const resp = await fetch('/api/groups/mine');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _moonGooGroups = data.groups || [];
    if (!_moonGooGroups.length) {
      if (pickerRow) pickerRow.style.display = 'none';
      el.innerHTML = '<div class="pp-empty">No groups to manage yet — an admin needs to create one first (Admin → Groups).</div>';
      return;
    }
    if (!_moonGooGroupId || !_moonGooGroups.some(g => g.id === _moonGooGroupId)) {
      _moonGooGroupId = _moonGooGroups[0].id;
    }
    if (picker) {
      picker.innerHTML = _moonGooGroups.map(g => `<option value="${g.id}" ${g.id === _moonGooGroupId ? 'selected' : ''}>${_esc(g.name)}</option>`).join('');
    }
    if (pickerRow) pickerRow.style.display = _moonGooGroups.length > 1 ? '' : 'none';
    if (!_moonGooMaterials.length) {
      try {
        const mResp = await fetch('/api/moon-goo-materials');
        if (mResp.ok) _moonGooMaterials = (await mResp.json()).materials || [];
      } catch (e) { /* non-fatal — picker just falls back to free text */ }
    }
    await _fetchAndRenderMoonGoo();
  } catch (e) {
    el.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function onMoonGooGroupChange() {
  const picker = document.getElementById('moonGooGroupPicker');
  if (!picker) return;
  _moonGooGroupId = parseInt(picker.value, 10);
  _fetchAndRenderMoonGoo();
}

async function _fetchAndRenderMoonGoo() {
  const el = document.getElementById('moonGooList');
  if (!el || !_moonGooGroupId) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  try {
    const resp = await fetch(`/api/moon-goo/${_moonGooGroupId}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderMoonGoo(data.prices || []);
  } catch (e) {
    el.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function renderMoonGoo(rows) {
  const el = document.getElementById('moonGooList');
  if (!el) return;
  _moonGooCurrentTypeIds = new Set(rows.map(r => r.type_id));
  _rebuildGooDatalist();
  if (!rows.length) { el.innerHTML = '<div class="pp-empty">No materials priced yet — add one above or paste an import.</div>'; return; }
  el.innerHTML = rows.map(r => `
    <div class="admin-row" data-type-id="${r.type_id}">
      <span class="admin-name">${_esc(r.name)}</span>
      <span class="admin-meta">#${r.type_id}</span>
      <label class="goo-inline"><span class="goo-inline-lbl">ISK / unit</span>
        <input type="number" class="bug-input goo-row-price" value="${r.sell_price}" style="max-width:120px"></label>
      <label class="goo-inline"><span class="goo-inline-lbl">Quantity</span>
        <input type="number" class="bug-input goo-row-stock" value="${r.stock}" style="max-width:100px" title="Stock on hand. Set to 0 to mark the material 'out' — the shopping list then routes it to the open market instead of the alliance. Does not affect which reactions are suggested (those always use the sheet price)."></label>
      <button onclick="saveMoonGooRow(${r.type_id}, this)">Save</button>
      <button onclick="deleteMoonGooRow(${r.type_id}, this)">Delete</button>
    </div>`).join('');
}

// Rebuild the "add material" datalist from the catalog, excluding materials already on this
// group's sheet (you can still edit those inline below — the picker is for adding NEW ones).
function _rebuildGooDatalist() {
  const dl = document.getElementById('gooMaterialsDatalist');
  if (!dl) return;
  const available = _moonGooMaterials.filter(m => !_moonGooCurrentTypeIds.has(m.type_id));
  dl.innerHTML = available.map(m => `<option value="${_esc(m.name)}"></option>`).join('');
}

// Resolve the searchable text field to a type_id (exact name match, case-insensitive).
function _gooSearchTypeId() {
  const v = (document.getElementById('gooAddSearch').value || '').trim().toLowerCase();
  if (!v) return null;
  const hit = _moonGooMaterials.find(m => m.name.toLowerCase() === v);
  return hit ? hit.type_id : null;
}

function _onGooSearchInput() {
  const statusEl = document.getElementById('gooAddStatus');
  if (statusEl && statusEl.textContent) statusEl.textContent = '';
}

async function saveMoonGooRow(typeId, btn) {
  const row = btn.closest('.admin-row');
  const sell_price = parseFloat(row.querySelector('.goo-row-price').value) || 0;
  const stock = parseInt(row.querySelector('.goo-row-stock').value, 10) || 0;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/moon-goo/${_moonGooGroupId}/row`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: typeId, sell_price, stock }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    btn.textContent = 'Saved ✓';
    setTimeout(() => { btn.textContent = 'Save'; btn.disabled = false; }, 1200);
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
  }
}

async function deleteMoonGooRow(typeId, btn) {
  if (!confirm('Remove this material from the price list?')) return;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/moon-goo/${_moonGooGroupId}/${typeId}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _fetchAndRenderMoonGoo();
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
  }
}

async function addMoonGooRow() {
  const statusEl = document.getElementById('gooAddStatus');
  const typeId = _gooSearchTypeId();
  const sell_price = parseFloat(document.getElementById('gooAddPrice').value) || 0;
  const stock = parseInt(document.getElementById('gooAddStock').value, 10) || 0;
  if (!typeId) { statusEl.textContent = 'Pick a material from the list.'; return; }
  if (!_moonGooGroupId) { statusEl.textContent = 'No group selected.'; return; }
  try {
    const resp = await fetch(`/api/moon-goo/${_moonGooGroupId}/row`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: typeId, sell_price, stock }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    const result = await resp.json();
    statusEl.textContent = `Added/updated: ${result.name}`;
    document.getElementById('gooAddSearch').value = '';
    document.getElementById('gooAddPrice').value = '';
    document.getElementById('gooAddStock').value = '';
    _fetchAndRenderMoonGoo();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
  }
}

async function importMoonGoo() {
  const text = document.getElementById('gooImportText').value;
  const statusEl = document.getElementById('gooImportStatus');
  if (!text.trim()) { statusEl.textContent = 'Paste something first.'; return; }
  if (!_moonGooGroupId) { statusEl.textContent = 'No group selected.'; return; }
  try {
    const resp = await fetch(`/api/moon-goo/${_moonGooGroupId}/import`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    const result = await resp.json();
    statusEl.textContent = `Imported ${result.imported} row${result.imported === 1 ? '' : 's'}.`
      + (result.errors.length ? ` ${result.errors.length} warning(s): ${result.errors.join('; ')}` : '');
    document.getElementById('gooImportText').value = '';
    _fetchAndRenderMoonGoo();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
  }
}

// ── Groups (site-admin only — creates groups, assigns delegated managers, page restriction) ──
let _groupPageRegistry = [];   // [{key, label}, ...] from the backend — see app.groups.PAGE_REGISTRY

async function loadGroups() {
  const el = document.getElementById('groupsList');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  try {
    const resp = await fetch('/api/admin/groups');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _groupPageRegistry = data.page_registry || [];
    renderGroups(data.groups || []);
  } catch (e) {
    el.innerHTML = `<div class="pp-empty">Failed to load: ${_esc(e.message)}</div>`;
  }
}

function renderGroups(groups) {
  const el = document.getElementById('groupsList');
  if (!el) return;
  if (!groups.length) { el.innerHTML = '<div class="pp-empty">No groups yet — add one above.</div>'; return; }
  el.innerHTML = groups.map(g => {
    const allowed = new Set(g.allowed_pages);
    const restricted = allowed.size > 0;
    return `
    <div class="admin-row" data-group-id="${g.id}" style="flex-direction:column;align-items:stretch;gap:6px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="admin-name">${_esc(g.name)}</span>
        <span class="admin-meta">alliance #${g.alliance_id}</span>
        <button onclick="deleteGroup(${g.id}, '${_esc(g.name)}', this)">Delete group</button>
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <span class="pp-card-hint" style="margin:0">Managers:</span>
        ${g.managers.map(m => `<span class="grp-mgr-chip">${_esc(m)} <button class="grp-mgr-remove" onclick="removeGroupManager(${g.id},'${_esc(m)}',this)" title="Remove manager">×</button></span>`).join('') || '<span class="pp-card-hint">none</span>'}
        <input type="text" class="bug-input grp-mgr-input" placeholder="Character name" style="max-width:160px">
        <button onclick="addGroupManager(${g.id}, this)">Add manager</button>
      </div>
      <div>
        <span class="pp-card-hint" style="margin:0 0 4px;display:block">Page access — ${restricted
          ? `restricted to only the checked pages below`
          : `unrestricted (full access — check any page to switch this group to an allow-list)`}:</span>
        ${_groupPageRegistry.length ? `<div class="settings-toggle-grid">${_groupPageRegistry.map(p =>
          `<label class="settings-toggle-row"><input type="checkbox" class="grp-page-cb" data-key="${_esc(p.key)}" ${allowed.has(p.key) ? 'checked' : ''} onchange="toggleGroupPage(${g.id}, '${_esc(p.key)}', this.checked, this)"> ${_esc(p.label)}</label>`
        ).join('')}</div>` : '<div class="pp-card-hint">No pages registered.</div>'}
      </div>
    </div>`;
  }).join('');
}

async function createGroup() {
  const name = document.getElementById('groupAddName').value.trim();
  const allianceId = parseInt(document.getElementById('groupAddAllianceId').value, 10);
  const statusEl = document.getElementById('groupAddStatus');
  if (!name || !allianceId) { statusEl.textContent = 'Name and alliance ID are required.'; return; }
  try {
    const resp = await fetch('/api/admin/groups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, alliance_id: allianceId }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    statusEl.textContent = `Added: ${name}`;
    document.getElementById('groupAddName').value = '';
    document.getElementById('groupAddAllianceId').value = '';
    loadGroups();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e.message;
  }
}

async function deleteGroup(groupId, name, btn) {
  if (!confirm(`Delete group "${name}"? Its price sheet and reaction settings are left in place but become unreachable.`)) return;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/admin/groups/${groupId}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    loadGroups();
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
  }
}

async function addGroupManager(groupId, btn) {
  const row = btn.closest('.admin-row');
  const input = row.querySelector('.grp-mgr-input');
  const name = input.value.trim();
  if (!name) return;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/admin/groups/${groupId}/managers`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character_name: name }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    loadGroups();
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
  }
}

async function removeGroupManager(groupId, name) {
  try {
    const resp = await fetch(`/api/admin/groups/${groupId}/managers/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    loadGroups();
  } catch (e) {
    alert('Failed: ' + e.message);
  }
}

async function toggleGroupPage(groupId, key, allowed, cb) {
  cb.disabled = true;
  try {
    const resp = allowed
      ? await fetch(`/api/admin/groups/${groupId}/pages`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ page_key: key }),
        })
      : await fetch(`/api/admin/groups/${groupId}/pages/${encodeURIComponent(key)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `HTTP ${resp.status}`);
    // Reload rather than patch in place — checking/unchecking the FIRST/LAST box flips the
    // whole group between unrestricted and restricted, which changes the hint text above the
    // grid too, not just this one checkbox.
    loadGroups();
  } catch (e) {
    alert('Failed: ' + e.message);
    cb.checked = !allowed; // revert the checkbox on failure
    cb.disabled = false;
  }
}
