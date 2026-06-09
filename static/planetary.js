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
  lastRecsData: null,   // /api/plan result from step 2 (no chosen_systems)
  lastPlanData: null,   // /api/plan result from step 3 (with chosen_systems)
  chosenSystems: [],
  factorySystem: '',    // override for factory system ('' = auto)
  factoryCharIds: [],   // if non-empty, only these character IDs do factories
  importComponents: [], // fuel-block component type_ids to buy/import, not produce
  factoryPlanetTypes: ['Barren', 'Temperate'], // allowed factory planet types (fuel block)
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
  await _tryRestoreFromHash();
}

function onPlanetDbTabOpen() {
  if (!Object.keys(_ppConstRegions).length) loadConstellations();  // region map for search
  loadPlanets();
}

// ── ESI / Characters ──────────────────────────────────────────────────────────

let _esiConfigured = false;
let _loggedIn = false;
let _isAdmin = false;

async function loadCharacters() {
  try {
    const resp = await fetch('/api/characters');
    const data = await resp.json();
    _esiConfigured = data.configured;
    _loggedIn = data.logged_in || false;
    _isAdmin = data.is_admin || false;
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
    return;
  }
  const char = chars.find(c => c.character_id === sessionCharId);
  const name = char ? char.name : 'Unknown';
  // Admin access is the sidebar "Admin" item now (no duplicate header button).
  el.innerHTML =
    `<button class="header-bug-btn" onclick="openBugModal()">Report bug</button>`
    + `<span class="header-session">${name} · <a href="/auth/logout" class="header-logout">Log out</a></span>`;
  const navTab = document.getElementById('adminNavTab');
  if (navTab) navTab.style.display = _isAdmin ? '' : 'none';
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
  loadBasketsAdmin();
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

// ── Custom baskets (admin) ────────────────────────────────────────────────────
let _basketEditId = null;            // null = creating a new basket
let _basketEditItems = [];           // [{type_id, name, tier, qty}]

async function loadBasketsAdmin() {
  await _refreshBaskets();           // keeps the wizard picker in sync too
  const el = document.getElementById('basketList');
  if (!el) return;
  if (!_baskets.length) { el.innerHTML = '<div class="pp-empty">No baskets yet.</div>'; }
  else el.innerHTML = _baskets.map(b => {
    const items = b.items.map(i => `${_esc(i.name)}×${i.qty}`).join(', ');
    return `<div class="basket-row">
      <div class="basket-row-head">
        <span class="admin-name">${_esc(b.name)}</span>
        <span class="admin-meta">run ${b.run_size} · ${_esc(b.unit_label)}</span>
        <button class="bug-act" onclick="editBasket(${b.id})">Edit</button>
        <button class="bug-act bug-act-ignore" onclick="deleteBasket(${b.id}, '${_esc(b.name).replace(/'/g, "\\'")}')">Delete</button>
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
  status.textContent = 'Saving…';
  try {
    const url = _basketEditId ? `/api/baskets/${_basketEditId}` : '/api/baskets';
    const resp = await fetch(url, {
      method: _basketEditId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, run_size, unit_label, items }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    resetBasketEditor();
    loadBasketsAdmin();
  } catch (e) { status.textContent = e.message; }
}

async function deleteBasket(id, name) {
  if (!confirm(`Delete basket "${name}"? Users will no longer be able to plan it.`)) return;
  try {
    const resp = await fetch(`/api/baskets/${id}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    if (_basketEditId === id) resetBasketEditor();
    loadBasketsAdmin();
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
    row.className = 'pp-char-row';
    const tokenDot = c.token_ok
      ? '<span title="Token valid" style="color:#5ecf80;font-size:10px">●</span>'
      : '<span title="Token expired — re-add character" style="color:#e06060;font-size:10px">●</span>';
    const extractors = (c.planets || []).filter(p => p.is_extractor);
    const factories  = (c.planets || []).filter(p => !p.is_extractor);
    let planetTag = '';
    if (c.planets && c.planets.length) {
      const tooltip = extractors.map(p => `${p.planet_type}${p.p0_name ? ' → ' + p.p0_name : ''}`).join(', ')
        + (factories.length ? ` · ${factories.length} fac` : '');
      const parts = [];
      if (extractors.length) parts.push(`${extractors.length} ext`);
      if (factories.length)  parts.push(`${factories.length} fac`);
      planetTag = `<span class="pp-char-planets" title="${tooltip}">${parts.join(' · ')}</span>`;
    }
    const delHtml = loggedIn
      ? `<button class="pp-char-del" title="Remove character" data-id="${c.character_id}">✕</button>`
      : '';
    row.innerHTML = `
      <div class="pp-char-header">
        <span class="pp-char-name">${tokenDot} ${c.name}</span>
        ${delHtml}
      </div>
      <div class="pp-char-meta">
        <span class="pp-char-skill" title="Max planets / CCU level">${c.max_planets} pl · CCU ${c.ccu}</span>
        ${planetTag}
      </div>`;
    if (loggedIn) {
      row.querySelector('.pp-char-del').addEventListener('click', async () => {
        await fetch(`/api/characters/${c.character_id}`, { method: 'DELETE' });
        loadCharacters();
      });
    }
    list.appendChild(row);
  });
}

async function refreshAllPlanets(btn) {
  const chars = document.querySelectorAll('.pp-char-del');
  const ids = Array.from(chars).map(b => parseInt(b.dataset.id));
  btn.textContent = `Refreshing 0/${ids.length}…`;
  btn.disabled = true;
  for (let i = 0; i < ids.length; i++) {
    btn.textContent = `Refreshing ${i + 1}/${ids.length}…`;
    await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' });
  }
  btn.disabled = false;
  btn.textContent = 'Refresh';
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
  const builtinFb = _wiz.fuelblock && !_wiz.basketId;
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
    _wiz.typeId = null; _wiz.fuelblock = false; _wiz.basketId = null;
    return;
  }
  _wiz.basketId = _basketIdFor(name);
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
  _wiz.lastRecsData = null;
  _wiz.lastPlanData = null;
  if (isFuelBlock) await _loadProductConfig(FUEL_BLOCK_TYPE_ID, FUEL_BLOCK_LABEL);
  else if (basket) await _loadProductConfig(profile.type_id, basket.name);
  else if (prod) await _loadProductConfig(profile.type_id, prod.name);
  wizardGo(1);
}

async function wizardSaveProfile() {
  if (!_wiz.typeId) { alert('No product selected.'); return; }
  const name = prompt('Profile name:');
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
  const payload = {
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
    mfg: _wiz.fuelblock ? _mfgRaw() : null,  // manufacturing ME selects (raw, for restore)
    plan: _wiz.lastPlanData || null,
  };
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
  if (!shareId) return;
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

async function _restoreFromPayload(payload) {
  const basket = _basketById(_basketIdFromTid(payload.tid));
  const isFuelBlock = payload.tid === FUEL_BLOCK_TYPE_ID;
  const isFB = isFuelBlock || basket != null;
  const prod = isFuelBlock
    ? { type_id: FUEL_BLOCK_TYPE_ID, name: FUEL_BLOCK_LABEL }
    : (basket ? { type_id: payload.tid, name: basket.name } : _ppProducts.find(p => p.type_id === payload.tid));
  if (!prod) return;
  _wiz.fuelblock = isFB;
  _wiz.basketId = basket ? basket.id : null;
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
  };
  if (_wiz.fuelblock) {
    body.factory_planet_types = _wiz.factoryPlanetTypes || ['Barren', 'Temperate'];
    if (_wiz.basketId) {
      // Custom production basket: no racial block type, no manufacturing ME, no import toggles.
      body.basket_id = _wiz.basketId;
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
  return t ? `<span class="${PP_TYPE_CLASS[t] || ''}">${t}</span>` : '';
}

function _fmtIsk(v) {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + ' B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + ' M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + ' K';
  return v.toLocaleString();
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

function renderFinalPlan(data, opts = {}) {
  const content = document.getElementById('wizPlanContent');

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
    statsHtml = `
      <div class="plan-stats-bar">
        <div class="plan-stat plan-stat-edit" title="Target overproduction % — edit and the plan recalculates. 10% means extractors produce 10% more P0 than factories need. Reported baseline: ${s.overproduction_pct >= 0 ? '+' : ''}${s.overproduction_pct}%.">
          <span class="plan-stat-val ${s.overproduction_pct < 0 ? 'plan-stat-warn' : 'plan-stat-ok'}"><input type="number" id="planOverprodInput" class="plan-overprod-input" value="${opVal}" min="0" max="500" step="5">% overprod</span>
          <span class="plan-stat-lbl">${s.factories} factories${s.max_supportable_factories != null ? ' · max ' + s.max_supportable_factories : ''}</span>
        </div>
        <div class="plan-stat">
          <span class="plan-stat-val">${s.products_per_day.toLocaleString()}</span>
          <span class="plan-stat-lbl">${data.fuelblock ? 'fuel blocks/day' : 'units/day'}</span>
        </div>
        <div class="plan-stat">
          <span class="plan-stat-val">${_fmtIsk(s.isk_per_day)}</span>
          <span class="plan-stat-lbl">ISK/day</span>
        </div>
        <div class="plan-stat">
          <span class="plan-stat-val">${_fmtIsk(s.sell_price)}</span>
          <span class="plan-stat-lbl">sell/unit</span>
        </div>
        ${s.factory_refill_hours ? `
        <div class="plan-stat" title="How long a factory planet's P1 input buffer lasts before you must refill it (assumes ${s.factory_launchpads_assumed} launchpads = ${(s.factory_launchpads_assumed*10).toLocaleString()}k m³ of P1; ~${s.factory_input_m3_day.toLocaleString()} m³/day consumed per factory). Add storage facilities to extend it.">
          <span class="plan-stat-val">${_fmtHours(s.factory_refill_hours)}</span>
          <span class="plan-stat-lbl">refill / factory (${s.factory_launchpads_assumed} LP)</span>
        </div>` : ''}
        ${p0StatHtml}
      </div>`;
  }

  // P1 requirement pills
  const maxQty = Math.max(...data.p1_requirements.map(r => r.relative_qty));
  const reqPills = data.p1_requirements.map(r => {
    const dim = r.relative_qty < maxQty ? ' plan-req-dim' : '';
    const tip = r.p0_name ? `${r.p0_name} → ${r.p1_name}` : r.p1_name;
    return `<span class="plan-req-pill${dim}" title="${tip}">${r.p1_name} <em>×${r.relative_qty}</em></span>`;
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
  const assignHtml = data.assignments.map(a => {
    if (!a.extractors.length && !a.factory_planets) return '';
    const extractorRows = a.extractors.map(e => {
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
      const sysHtml = e.system
        ? `<span class="plan-ext-sys">${e.system} P${e.planet_num}</span>`
        : `<span class="plan-ext-no-planet">no planet in system</span>`;
      const qualHtml = e.quality_pct !== undefined
        ? `<span class="plan-ext-qual ${e.quality_pct >= 80 ? 'plan-qual-ok' : e.quality_pct >= 50 ? '' : 'plan-qual-low'}">${e.quality_pct}</span>` : '';
      return `<div class="plan-ext-row">${tag}${_ptypeSpan(ptype)}${sysHtml}<span class="plan-ext-arrow">→</span><span class="plan-ext-p1">${e.p1_name || '?'}</span>${qualHtml}</div>`;
    }).join('');

    const factoryRows = (a.factory_assignments || []).map(f => {
      const tag = f.is_existing
        ? `<span class="plan-ext-tag plan-ext-existing">existing</span>`
        : f.is_replace
          ? `<span class="plan-ext-tag plan-ext-replace">replace</span>`
          : `<span class="plan-ext-tag plan-ext-new">new</span>`;
      const sysHtml = f.system
        ? `<span class="plan-ext-sys">${f.system}${f.planet_num != null ? ' P' + f.planet_num : ''}</span>`
        : f.unplaced ? `<span class="plan-ext-no-planet">no factory planet in system</span>` : '';
      const facLabel = f.product
        ? `${f.product.name}${f.ccu ? ' · CC' + f.ccu : ''}`
        : 'factory';
      return `<div class="plan-ext-row">${tag}${_ptypeSpan(f.planet_type || 'Barren')}${sysHtml}<span class="plan-ext-arrow">→</span><span class="plan-ext-p1 plan-fac-label">${_esc(facLabel)}</span></div>`;
    }).join('');

    const freeRows = Array.from({length: a.free_planets || 0}, () =>
      `<div class="plan-ext-row"><span class="plan-ext-tag plan-ext-free">free</span><span class="plan-ext-p1 plan-free-label">available</span></div>`
    ).join('');

    const isFacChar = (_wiz.factoryCharIds || []).includes(a.character_id);
    const facBadge = a.factory_only
      ? `<span class="plan-fac-only-badge">factory only</span>` : '';
    const facToggle = `<button class="plan-fac-toggle-btn${isFacChar ? ' plan-fac-toggle-active' : ''}"
      onclick="toggleFactoryChar(${a.character_id})" title="${isFacChar ? 'Stop prioritising this character for factories' : 'Prioritise this character to host factories (still extracts on spare slots)'}">${isFacChar ? '★ factories' : 'host factories'}</button>`;
    return `
      <div class="plan-char-block">
        <div class="plan-char-name">${a.character_name}${facBadge}${facToggle}<span class="plan-char-meta"> · ${a.effective_planets} pl · CCU ${a.ccu}</span></div>
        <div class="plan-char-extractors">${extractorRows}${factoryRows}${freeRows}</div>
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
    // Build templates from the ACTUAL placements so each factory's command-centre level
    // and planet type (→ diameter → fit) match where it landed. Distinct (type, ptype, cc)
    // combos → one token `tid:::cc:ptype` each (lp/count blank = per-tier defaults).
    const combos = new Map();
    for (const a of (data.assignments || [])) {
      for (const f of (a.factory_assignments || [])) {
        const tid = f.product && f.product.type_id;
        if (!tid) continue;
        const ptype = f.planet_type && f.planet_type !== 'Any' ? f.planet_type : 'Barren';
        const cc = f.ccu || data.stats?.plan_cc || 5;
        combos.set(`${tid}|${ptype}|${cc}`, `${tid}:::${cc}:${ptype}`);
      }
    }
    const toks = [...combos.values()].join(',')
      || lines.map(l => l.type_id).join(',');  // fallback: unscaled, if no placements
    templatesHref = toks ? `/api/layout/bundle?type_ids=${encodeURIComponent(toks)}&expand=1` : '';
  }

  content.innerHTML = `
    <div class="plan-header">
      <span class="plan-product-name">${data.product.name}</span>
      <span class="plan-summary">${data.total_extractors_base} base extractor${data.total_extractors_base !== 1 ? 's' : ''}</span>
    </div>
    ${statsHtml}
    ${fbHtml}
    ${facSysHtml}
    ${facTypeHtml}
    <div class="plan-req-row">${reqPills}</div>
    ${unmetHtml}
    ${unplacedFacHtml}
    <div class="plan-section-title">Character Assignment</div>
    <div class="plan-assignments">${assignHtml}</div>
    ${p0SummaryHtml}
    <div class="plan-actions-bar">
      <button class="plan-action-btn" id="ppShoppingListBtn">Command Centers</button>
      ${templatesHref ? `<a class="plan-action-btn" href="${templatesHref}" download>PI Templates (.zip)</a>` : ''}
    </div>
  `;
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

const _LAYOUT_LS_KEY = 'layoutSelections';

async function onLayoutTabOpen() {
  await loadPiProducts();
  _ppProducts.forEach(p => { _layoutTierMap[p.type_id] = p.tier; });
  if (!_layoutSel.length) await _restoreLayoutState();
  renderLayoutSelections();
}

function _saveLayoutState() {
  try {
    const slim = _layoutSel.map(e => ({ type_id: e.type_id, name: e.name, tier: e.tier, planet: e.planet, launchpads: e.launchpads, count: e.count || 1 }));
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
    planet: s.planet, launchpads: s.launchpads, count: s.count ?? null, data: null, error: null,
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
      body: JSON.stringify({ type_id: entry.type_id, planet_type: entry.planet, launchpads: entry.launchpads, count: entry.count == null ? null : entry.count }),
    });
    if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.status); }
    entry.data = await resp.json();
    if (entry.data.summary && entry.data.summary.count != null) entry.count = entry.data.summary.count;  // resolve auto-max
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
  const toks = _layoutSel.map(e => `${e.type_id}:${e.launchpads}:${e.count || 1}`).join(',');
  return `/api/layout/bundle?type_ids=${encodeURIComponent(toks)}`;
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
    <div class="layout-card-head">
      <div>
        <span class="layout-tag tier-${entry.tier}">P${entry.tier}</span>
        <span class="layout-card-name">${_esc(entry.name)}</span>
      </div>
      <button class="layout-card-x" title="Remove" onclick="removeLayout('${entry.key}')">✕</button>
    </div>`;
  if (entry.error) return `<div class="layout-card">${head}<div class="layout-card-meta" style="color:#e07a7a">Error: ${_esc(entry.error)}</div></div>`;
  if (!entry.data) return `<div class="layout-card">${head}<div class="layout-card-meta">Generating…</div></div>`;

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
      <div class="layout-card-meta">planet: ${_esc(s.planet_type)}${others.length ? ' · also: ' + others.map(_esc).join(', ') : ''}</div>`;
  } else {
    const imports = s.imports.map(i => `${_esc(i.name)} <b>${i.per_hour}/hr</b>`).join(', ');
    info = `
      <div class="layout-card-line">${outStr} · ${(s.buffer_m3 / 1000).toLocaleString()} km³ buffer · planet: ${_esc(s.planet_type)}</div>
      <div class="layout-card-meta">${_esc(s.imports_label)}: ${imports}</div>`;
  }
  const url = `/api/layout/download?type_id=${entry.type_id}&planet_type=${encodeURIComponent(s.planet_type)}&launchpads=${entry.launchpads}&count=${entry.count || 1}`;
  const countLabel = isExtractor ? 'Factories' : (entry.tier === 2 ? 'Factories' : 'Chains');
  return `
    <div class="layout-card">
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
        ${isExtractor ? '<span></span>' : `<label title="Max that fits the command center: ${s.max_count}">${countLabel} <input type="number" min="1" max="99" value="${entry.count || 1}"
          onchange="changeLayoutCount('${entry.key}', this.value)"
          onwheel="_layoutWheelCount(event, this, '${entry.key}')"><span class="layout-max">/${s.max_count}</span></label>`}
        <label>Launchpads <input type="number" min="1" max="8" value="${entry.launchpads}"
          onchange="changeLayoutLaunchpads('${entry.key}', this.value)"
          onwheel="_layoutWheelLp(event, this, '${entry.key}')"></label>
        <a class="layout-btn" href="${url}" download>Download .json</a>
        <button class="layout-btn" onclick="copyLayoutEntry('${entry.key}', this)">Copy JSON</button>
      </div>
    </div>`;
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

