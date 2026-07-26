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

// ── Feature flags ─────────────────────────────────────────────────────────────
// Admin-gated rollout (no staging env): a feature is "active" for the user when its flag is
// enabled for the public OR the user is an admin (admins preview everything). New features
// default off (admin-only); retrofitted existing ones default on — pass dflt=true so they stay
// visible if /api/features hasn't loaded yet.
let _features = {};            // key -> {key,label,description,enabled,group}
let _featuresGroupOrder = [];  // display order for the Admin > Features grouped list
let _featuresIsAdmin = false;
let _featuresIsTester = false;
let _featuresGitCommit = 'unknown';
let _loadFeaturesInFlight = null;
async function _loadFeatures() {
  // Several tab-open/boot paths call this independently and can overlap (e.g. page boot's
  // loadCharacters() and switchTab('dashboard')'s onDashboardTabOpen() both call this on the
  // same load) — share one in-flight request instead of firing a duplicate /api/features call.
  // Each call still gets a genuinely fresh fetch once no request is already in progress, so
  // callers that want an up-to-date reload (e.g. after an admin toggles a flag) still get one.
  if (_loadFeaturesInFlight) return _loadFeaturesInFlight;
  _loadFeaturesInFlight = (async () => {
    try {
      const d = await (await fetch('/api/features')).json();
      _features = {}; (d.features || []).forEach(f => { _features[f.key] = f; });
      _featuresGroupOrder = d.group_order || [];
      _featuresIsAdmin = !!d.is_admin;
      _featuresIsTester = !!d.is_tester;
      _featuresGitCommit = d.git_commit || 'unknown';
    } catch (e) { /* leave whatever we had; _featureActive falls back to dflt */ }
  })();
  try {
    await _loadFeaturesInFlight;
  } finally {
    _loadFeaturesInFlight = null;
  }
}
function _featureActive(key, dflt = false) {
  const f = _features[key];
  const state = f ? (f.state || (f.enabled ? 'public' : 'admin')) : (dflt ? 'public' : 'admin');
  if (state === 'public') return true;
  if (state === 'testers') return _featuresIsAdmin || _featuresIsTester;
  if (state === 'admin') return _featuresIsAdmin;
  return false; // hidden
}
function _applyTabGates() {
  const gates = [
    { key: 'factory_layout', storageKey: 'ppNavFeatLayout', cls: 'nav-feat-layout', tab: 'layout' },
    { key: 'planet_db',      storageKey: 'ppNavFeatPdb',    cls: 'nav-feat-pdb',    tab: 'planetdb' },
    { key: 'reactions',      storageKey: 'ppNavFeatRx',     cls: 'nav-feat-reactions', tab: 'reactions' },
    { key: 'industry',       storageKey: 'ppNavFeatIndustry', cls: 'nav-feat-industry', tab: 'industry' },
  ];
  const cur = localStorage.getItem('activeTab');
  let needRedirect = false;
  gates.forEach(({ key, storageKey, cls, tab }) => {
    const show = _featureActive(key);
    try { localStorage.setItem(storageKey, show ? '1' : '0'); } catch(e) {}
    document.documentElement.classList.toggle(cls, show);
    if (!show && cur === tab) needRedirect = true;
  });
  if (needRedirect && typeof switchTab === 'function') switchTab('dashboard');
}

function _applyLoginGates() {
  // Display is handled by CSS classes (nav-li set in renderHeaderSession).
  // This function only handles redirects for users who land on a gated tab while logged out.
  if (_loggedIn) return;
  const AUTH_TABS = ['analyze', 'planetary', 'characters', 'planetdb'];
  const cur = localStorage.getItem('activeTab');
  if (AUTH_TABS.includes(cur) || cur === 'dashboard' || !cur) {
    if (typeof switchTab === 'function') switchTab('howitworks');
  }
}


// ── ESI / Characters ──────────────────────────────────────────────────────────

let _esiConfigured = false;
let _loggedIn = false;
let _isAdmin = false;
let _isGroupManager = false;  // manages at least one alliance group's own data (app.groups) without being a full site admin
let _restrictedPages = null;  // null = unrestricted; array = caller's group allows ONLY these pages (app.groups.PAGE_REGISTRY keys)
let _sessionLoaded = false;   // true once /api/characters has resolved → _isAdmin/_loggedIn are real
let _ppCharsData = [];   // last /api/characters payload, for the Setup Analysis tab
let _ppSessionCharId = null;   // last session_character_id, so the cache-hint tick below can re-render without a fetch

let _loadCharactersInFlight = null;
async function loadCharacters() {
  // Boot calls this unconditionally (app.js) AND the restored tab's own onXTabOpen() hook
  // (e.g. Setup Analysis, Dashboard) can call it again concurrently on the very same load —
  // share one in-flight request instead of firing a duplicate /api/characters call. A later,
  // non-overlapping call (e.g. after adding a character) still gets a genuinely fresh fetch.
  if (_loadCharactersInFlight) return _loadCharactersInFlight;
  _loadCharactersInFlight = (async () => {
    try {
      const resp = await fetch('/api/characters');
      const data = await resp.json();
      _esiConfigured = data.configured;
      _loggedIn = data.logged_in || false;
      _isAdmin = data.is_admin || false;
      _isGroupManager = data.is_group_manager || false;
      _restrictedPages = data.restricted_pages || null;
      _ppCharsData = data.characters || [];
      _ppSessionCharId = data.session_character_id;
      renderCharacters(data.characters || [], _loggedIn);
      renderHeaderSession(_loggedIn, data.characters || [], data.session_character_id);
      _sessionLoaded = true;
      // Tab-restore on boot runs before this resolves, so a saved "admin" tab opened with _isAdmin
      // still false. Now that the real state is known, bounce a confirmed non-admin/non-manager off
      // the admin tab to a mobile-visible tab (the old onAdminTabOpen bounce to the hidden planner
      // shuffled phones).
      if (!_isAdmin && !_isGroupManager && localStorage.getItem('activeTab') === 'admin' && typeof switchTab === 'function') switchTab('dashboard');
      // ...and conversely, an admin/manager who refreshed straight ONTO the admin tab had
      // onAdminTabOpen() run at boot with _isAdmin still false, so its sections (Features, etc.)
      // never loaded. Now that roles are known, re-invoke it so the page isn't blank until a
      // manual nav click.
      else if ((_isAdmin || _isGroupManager) && localStorage.getItem('activeTab') === 'admin' && typeof onAdminTabOpen === 'function') onAdminTabOpen();
      _applyPageRestriction();
      await _loadFeatures();
      // Re-render: renderCharacters()/renderHeaderSession() above ran BEFORE _loadFeatures()
      // resolved, so any _featureActive()-gated content (e.g. the esi_cache_skip "no new data
      // until" hint) was invisible on this very first paint — it only ever showed up once
      // something else (like a rescan) called loadCharacters() again after flags were warm.
      renderCharacters(data.characters || [], _loggedIn);
      renderHeaderSession(_loggedIn, data.characters || [], data.session_character_id);
      _applyTabGates();
      _applyLoginGates();
      await loadProfiles();
    } catch (e) {
      console.error('Failed to load characters:', e);
    }
  })();
  try {
    await _loadCharactersInFlight;
  } finally {
    _loadCharactersInFlight = null;
  }
}

// Pages a group's page-restriction (app.groups.PAGE_REGISTRY) can actually apply to — Admin,
// Characters, How it works, and Contribute are account-management/informational, never
// restrictable. Keys match both PAGE_REGISTRY and switchTab()'s tab names directly.
const _RESTRICTABLE_PAGES = ['dashboard', 'analyze', 'planetary', 'planner', 'layout', 'planetdb', 'reactions'];

function _isPageRestricted(name) {
  if (_restrictedPages === null || !_RESTRICTABLE_PAGES.includes(name)) return false;
  return !_restrictedPages.includes(name);
}

function _firstAllowedPage() {
  if (_restrictedPages === null) return 'dashboard';
  const found = _RESTRICTABLE_PAGES.find(p => _restrictedPages.includes(p));
  return found || 'dashboard'; // shouldn't happen in practice, but never leave the app with nowhere to land
}

// Hides nav buttons for any restricted page (UI staging, not a hard backend boundary — see
// app.groups' module docstring). Setting style.display only when actually restricted (never
// forcing it back to visible) means this layers cleanly on top of the existing nav-li/nav-adm
// CSS-class visibility rules instead of fighting them.
function _applyPageRestriction() {
  _RESTRICTABLE_PAGES.forEach(key => {
    const hidden = _isPageRestricted(key);
    document.querySelectorAll(`.tab[data-tab="${key}"]`).forEach(el => { el.style.display = hidden ? 'none' : ''; });
  });
  // If the currently active tab just became restricted, bounce off it immediately rather than
  // leaving a blocked page on screen.
  const active = localStorage.getItem('activeTab');
  if (active && _isPageRestricted(active) && typeof switchTab === 'function') switchTab(_firstAllowedPage());
}

function renderHeaderSession(loggedIn, chars, sessionCharId) {
  try { localStorage.setItem('ppNavLoggedIn', loggedIn ? '1' : '0'); } catch(e) {}
  try { localStorage.setItem('ppNavIsAdmin', _isAdmin ? '1' : '0'); } catch(e) {}
  try { localStorage.setItem('ppNavIsGroupMgr', _isGroupManager ? '1' : '0'); } catch(e) {}
  document.documentElement.classList.toggle('nav-li', !!loggedIn);
  document.documentElement.classList.toggle('nav-adm', !!_isAdmin);
  document.documentElement.classList.toggle('nav-grpmgr', !!_isGroupManager);
  if (typeof _applyAdminNavVisibility === 'function') _applyAdminNavVisibility();
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
  // Whole-fleet cache hint: only shown when EVERY real, rescannable character is still within
  // its ESI cache window — i.e. hitting Rescan right now is guaranteed to change nothing. The
  // earliest of those cache windows is when it first becomes worth trying again.
  // A character with zero scanned planets has nothing cached to wait on (its next_data_at is
  // always null, "no opinion") — without excluding it here, one never-scanned alt permanently
  // hid this hint for the whole account, since .every() below saw its null and gave up.
  const _rescanTargets = chars.filter(c => !c.is_dummy && !c.wallet_only && c.token_ok && c.character_id > 0 && (c.planets || []).length > 0);
  // next_data_at is a fixed timestamp from the last scan — once real time passes it, the cache
  // has actually expired even though the stale value is still sitting in _ppCharsData (nothing
  // re-fetches it), so a plain truthiness check kept showing "cached until HH:MM" long after
  // HH:MM had passed. Gate on it still being in the future too.
  const _nowSec = Date.now() / 1000;
  const _allCached = _featureActive('esi_cache_skip') && _rescanTargets.length > 0
    && _rescanTargets.every(c => c.next_data_at && c.next_data_at > _nowSec);
  // The "no new data until" phrase is wrapped separately so it can be hidden on narrow mobile
  // headers (style-misc-responsive.css) without losing the time itself — the full phrase is
  // still in the title tooltip. Without this, the hint's full text was wide enough to push the
  // Settings gear + username/logout off the right edge of a phone-width header entirely.
  const rescanHint = _allCached
    ? `<span class="pp-cache-hint" title="Colony pin/pad snapshots are still within ESI's ~10min cache window, so those won't change — but rescan still refreshes skills and live production estimates."><span class="pp-cache-hint-text">colony data cached until </span>${_fmtEpochClock(Math.min(..._rescanTargets.map(c => c.next_data_at)))}</span>`
    : '';
  el.innerHTML =
    `<button id="rescanBtn" class="header-bug-btn" onclick="rescanAll()" ${_rescanning ? 'disabled' : ''} title="Re-scan every character's colonies from ESI">${_rescanning ? 'Rescanning…' : 'Rescan'}</button>`
    + rescanHint
    + `<button id="reportBugBtn" class="header-bug-btn" onclick="openBugModal()">Report bug</button>`
    + `<button class="header-settings-btn" onclick="openSettingsModal()" title="Settings">⚙&#xFE0E;</button>`
    + `<span class="header-session"><span class="header-session-name">${name} · </span><a href="/auth/logout" class="header-logout">Log out</a></span>`;
  if (!_dashLanded) {
    _dashLanded = true;
    const isShare = window.__SHARE_ID__ || /^\/s\//.test(location.pathname);
    if (!localStorage.getItem('activeTab') && !isShare && typeof switchTab === 'function') switchTab('dashboard');
  }
  const mb = document.getElementById('manageBasketsBtn');
  if (mb) mb.style.display = loggedIn ? '' : 'none';
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

// ── Account deletion modal ────────────────────────────────────────────────────
function openDeleteAccountModal() {
  document.getElementById('deleteAccountInput').value = '';
  document.getElementById('deleteAccountStatus').textContent = '';
  document.getElementById('deleteAccountBtn').disabled = true;
  document.getElementById('deleteAccountModal').style.display = 'flex';
  document.getElementById('deleteAccountInput').focus();
}
function closeDeleteAccountModal() {
  document.getElementById('deleteAccountModal').style.display = 'none';
}
function onDeleteAccountInput() {
  const val = document.getElementById('deleteAccountInput').value;
  document.getElementById('deleteAccountBtn').disabled = (val !== 'DELETE');
}
async function confirmDeleteAccount() {
  const btn = document.getElementById('deleteAccountBtn');
  const status = document.getElementById('deleteAccountStatus');
  btn.disabled = true;
  status.textContent = 'Deleting…';
  try {
    const resp = await fetch('/api/me', { method: 'DELETE' });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${resp.status}`);
    }
    status.textContent = 'Done. Redirecting…';
    setTimeout(() => { window.location.href = '/'; }, 800);
  } catch (e) {
    status.textContent = 'Failed: ' + e.message;
    btn.disabled = false;
  }
}

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


// Rescan a single character's colonies from ESI (the per-character button), then repaint.
async function rescanCharacter(cid, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Rescanning…'; }
  try {
    const resp = await fetch(`/api/characters/${cid}/refresh-planets`, { method: 'POST' });
    if (!resp.ok) { const d = await resp.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${resp.status}`); }
    const d = await resp.json().catch(() => ({}));
    // Briefly show real fetched-vs-cache-skipped counts before the repaint recreates this
    // button — the only way to see esi_cache_skip actually doing something, short of
    // reading server logs (which this deployment doesn't even emit at INFO level).
    if (btn && typeof d.planets_skipped_cached === 'number') {
      // "cached" colonies aren't frozen — skills + live production estimates still refresh
      // every rescan (see loadCharacters() below); only their raw pin/pad snapshot is unchanged
      // since ESI hasn't regenerated it yet. Framing it as "updated"/"unchanged" avoids reading
      // as a no-op when planets_fetched is 0.
      btn.textContent = `${d.planets_fetched} updated, ${d.planets_skipped_cached} unchanged`;
      await new Promise(r => setTimeout(r, 1600));
    }
    await loadCharacters();   // repaint with fresh data (recreates the button)
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Rescan this character'; }
    alert('Rescan failed: ' + e.message);
  }
}

function renderCharacters(chars, loggedIn) {
  const list = document.getElementById('characterList');
  list.innerHTML = '';
  // Wallet-only viewer toons sit at the bottom (they're not PI characters). Sort is stable, so the
  // real characters keep their server order.
  chars = [...(chars || [])].sort((a, b) => (a.wallet_only ? 1 : 0) - (b.wallet_only ? 1 : 0));
  const dummyCard = document.getElementById('dummyCharCard');
  if (dummyCard) dummyCard.style.display = (loggedIn && _featureActive('dummy_characters')) ? '' : 'none';

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
    if (c.wallet_only) {
      // A toon connected only to read the corp wallet — not a PI character. Show it plainly with a
      // remove option, but keep it out of all the PI counts/pickers.
      row.className = 'pp-char-row pp-char-wallet';
      row.innerHTML = `
        <div class="pp-char-header">
          <span class="pp-char-name"><span class="pp-char-dummy-badge" title="Connected only to read the corp wallet — not used for Planetary Industry">wallet</span> ${_esc(c.name)}</span>
          <button class="pp-char-del" title="Disconnect wallet character" data-id="${c.character_id}">✕</button>
        </div>
        <div class="pp-char-meta"><span style="color:#6a7390;font-size:12px">Corp-wallet viewer · see Admin → Corp wallet</span></div>`;
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
          // Fold each product's pad amount into its own name ("Oxidizing Compound 880") instead
          // of naming it once in the build label and AGAIN in a separate pad badge — the same
          // material was showing up twice per row. Anything in `pads` that isn't one of this
          // planet's current products (rare — a stale/reconfigured schematic) still gets its own
          // badge below so we never silently drop real pad contents.
          const padTitle = 'Estimated launchpad contents — simulated forward from the last Refresh (ESI only reports a stale checkpoint).';
          const padByType = new Map((p.pads || []).map(x => [x.type_id, x.amount]));
          const padByName = new Map((p.pads || []).map(x => [x.name, x.amount]));
          const prodTypeIds = new Set((p.products || []).map(x => x.type_id));
          const builds = (p.products || []).map(x => {
            const amt = padByType.get(x.type_id);
            return amt != null ? `${_esc(x.name)} <b>${amt.toLocaleString()}</b>` : _esc(x.name);
          }).join(', ');
          const buildTitle = builds.includes('<b>') ? ` title="${padTitle}"` : '';
          // Raw (pre-basics) extraction — p.p0_name is a single string (esi.py overwrites it per
          // ECU pin, so a DOUBLE extractor — e.g. two ECUs mining different P0s — only ever keeps
          // the last one). p.production already carries one entry per DISTINCT P0 actually being
          // harvested (tier 0, from pi_sim's raw-extraction fallback when no basics consume it
          // yet), so prefer that when it has something to show; fall back to the old single-name
          // lookup for planets scanned before `production` existed or where the forward-sim
          // couldn't run — same result as before for the ordinary single-P0 case either way,
          // since both read the same underlying pad amounts.
          const rawExtracts = p.is_extractor ? (p.production || []).filter(x => (x.tier || 0) === 0) : [];
          let extractLabel, coveredPadNames;
          if (rawExtracts.length) {
            extractLabel = rawExtracts.map(x => {
              const amt = padByType.get(x.type_id);
              return amt != null
                ? `<span class="pp-pl-extract" title="${padTitle}">→ ${_esc(x.name)} <b>${amt.toLocaleString()}</b></span>`
                : `<span class="pp-pl-extract">→ ${_esc(x.name)}</span>`;
            }).join('');
            coveredPadNames = new Set(rawExtracts.map(x => x.name));
          } else {
            const p0PadAmt = (!builds && p.p0_name) ? padByName.get(p.p0_name) : null;
            extractLabel = p0PadAmt != null
              ? `<span class="pp-pl-extract" title="${padTitle}">→ ${_esc(p.p0_name)} <b>${p0PadAmt.toLocaleString()}</b></span>`
              : `<span class="pp-pl-extract">→ ${_esc(p.p0_name || '?')}</span>`;
            coveredPadNames = p.p0_name ? new Set([p.p0_name]) : new Set();
          }
          // Wrapped in one cell (pp-pl-chain) so the P0→P1 chain (however many P0s feed it) is a
          // single flexible grid column instead of extra items spilling into the fixed-width pad
          // badge column and forcing every sibling row's columns wide too.
          const what = `<span class="pp-pl-chain">` + (p.is_extractor
            ? `${extractLabel}${builds ? `<span class="pp-pl-build"${buildTitle}> → ${builds}</span>` : ''}`
            : (builds
                ? `<span class="pp-pl-build"${buildTitle}>→ ${builds}</span>`
                : `<span class="pp-pl-factory">factory${p.num_pins ? ' · ' + p.num_pins + ' pins' : ''}</span>`)) + `</span>`;
          const extraPad = (p.pads || []).filter(x => !prodTypeIds.has(x.type_id) && !coveredPadNames.has(x.name));
          const pad = extraPad.length
            ? `<span class="pp-pl-pad" title="${padTitle}">${extraPad.map(x => `<b>${x.amount.toLocaleString()}</b> ${_esc(x.name)}`).join(' · ')}</span>`
            : '';
          const cc = p.upgrade_level ? `<span class="pp-pl-cc" title="Command center level">CC${p.upgrade_level}</span>` : '';
          // Extractor program time left (from ESI's expiry). ESI caches PI, so a recent reseat won't
          // show until ESI refreshes — when the reading is expired/soon AND ESI's data is old, flag the age.
          const extLeft = (p.is_extractor && p.expiry)
            ? (() => {
                const h = (p.expiry - Date.now() / 1000) / 3600;
                const cls = h <= 0 ? 'pp-pl-exp-now' : (h < 3 ? 'pp-pl-exp-soon' : '');
                const ageM = p.esi_modified ? Math.round((Date.now() / 1000 - p.esi_modified) / 60) : null;
                const stale = h < 3 && ageM != null && ageM >= 10;
                const ageTxt = stale ? ` <span class="pp-pl-exp-age">· ESI ${_fmtDHM(ageM / 60)} old</span>` : '';
                const tip = ageM != null
                  ? `Extraction time left, from ESI data generated ~${ageM}m ago. ESI caches PI, so a recent reseat won't show until it refreshes — rescan once this age resets.`
                  : 'Extraction program time left before it stops and the heads need reseating.';
                return `<span class="pp-pl-exp ${cls}" title="${tip}">${h <= 0 ? 'expired' : _fmtDHM(h) + ' left'}${ageTxt}</span>`;
              })()
            : '';
          // Three explicit groups (loc / what-it-produces / status+CC) instead of one flat flex
          // row — on desktop these wrappers are display:contents (invisible to layout, so the
          // CSS grid below still places each field in its own column); on mobile they become
          // real line breaks, so which fields share a line is a deliberate choice, not whatever
          // flex-wrap happens to fit given that particular row's text length.
          return `<div class="pp-pl-row"><span class="pp-pl-loc">${loc}</span>` +
            `<span class="pp-pl-line-what">${_ptypeSpan(p.planet_type)}${what}</span>` +
            `<span class="pp-pl-line-status">${extLeft}${pad}${cc}</span></div>`;
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
        ${_featureActive('reactions') && (c.reactions_opted_in || c.reaction_slots > 1)
          ? `<span title="Reaction slots — 1 base + Mass Reactions + Advanced Mass Reactions">RX ${c.reaction_slots} slot${c.reaction_slots !== 1 ? 's' : ''}</span>` : ''}
      </div>`;

    // Running reaction jobs, mirroring the PI colony list above — only for characters opted into
    // job tracking (?reactions=1). Populated from pp_char_industry_jobs via list_characters; empty
    // until a jobs refresh has run (tab-open / Rescan / the Reactions "Refresh jobs" button).
    const rxJobs = Array.isArray(c.reaction_jobs) ? c.reaction_jobs : [];
    // The character opted into job tracking but its token lacks the structure-read scope, so
    // facility names come back as "Structure #<id>". One-click re-authorise fixes it (same popup
    // as the initial connect — re-auth of an already-connected character just adds the new scope).
    const rxReconnect = (c.reactions_needs_structures && rxJobs.length)
      ? `<div class="pp-char-rx-reconnect">⚠ Facility names can't load — <button type="button" class="pp-char-rx-reconnect-btn" onclick="connectReactionsTracking()">reconnect this character</button> to show them.</div>`
      : '';
    const rxBlock = (_featureActive('reactions') && c.reactions_opted_in)
      ? `<div class="pp-char-rx">
           <div class="pp-char-rx-title">Reactions · ${rxJobs.length}/${c.reaction_slots} slot${c.reaction_slots !== 1 ? 's' : ''} running</div>
           ${rxJobs.length
             ? rxJobs.map(j => `<div class="pp-char-rx-job"><span class="pp-char-rx-name">${_esc(j.name)}</span><span class="pp-char-rx-meta">${j.runs != null ? j.runs + ' run' + (j.runs !== 1 ? 's' : '') : ''}${j.hours_left != null ? ' · ' + _fmtHours(j.hours_left) + ' left' : ''}${j.facility_name ? ' · ' + _esc(j.facility_name) : ''}</span></div>`).join('')
             : '<div class="pp-char-rx-empty">No reaction jobs running — install some from the Reactions tab.</div>'}
           ${rxReconnect}
         </div>`
      : '';

    // ESI caches each colony independently — next_data_at is only set when EVERY one of this
    // character's planets is still within its cache window, i.e. a rescan right now is
    // guaranteed to come back unchanged. Admin-preview until the esi_cache_skip flag ships.
    // Also gate on it still being in the future — next_data_at is a fixed timestamp from the
    // last scan, so once real time passes it a plain truthiness check kept showing a stale
    // "No new data until HH:MM" long after HH:MM had passed.
    const cacheHint = (c.next_data_at && c.next_data_at > Date.now() / 1000 && _featureActive('esi_cache_skip'))
      ? `<span class="pp-cache-hint" title="ESI hasn't regenerated this character's colony data yet — a rescan now would return the same cached data.">No new data until ${_fmtEpochClock(c.next_data_at)}</span>`
      : '';

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
          ${rxBlock}
          <div class="pp-char-actions">
            <button class="pp-char-rescan" ${c.token_ok ? '' : 'disabled'} onclick="rescanCharacter(${c.character_id}, this)" title="${c.token_ok ? "Re-scan just this character's colonies from ESI" : 'Token expired — re-add this character first'}">Rescan this character</button>
            ${cacheHint}
          </div>
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

// ── Move a character to another account ─────────────────────────────────────────
// A USER-INITIATED standalone tool, not analysis advice — lives in Settings → Characters (moved
// out of Setup Analysis 2026-07-02: it doesn't fit "Analysis gives advice, not a calculator tool",
// see project_eve_pi_planner_principle memory, and its CCU-fit check runs the full layout-generation
// engine server-side — real compute, not free — so it must stay gated behind an explicit reveal
// rather than computed on every Setup Analysis render regardless of whether anyone opens it).
// The real goal is moving a whole character's PI to a character on ANOTHER ACCOUNT (so factories and
// extractors can run at the same time). So it's a literal 1:1 SWAP: pick A and B, and every colony A
// runs moves to B while every colony B runs moves to A — each keeps its EXACT planet and layout, only
// the owner flips. Because the planet already hosts that exact colony it ALWAYS fits — no B/T /
// diameter / capacity juggling. NOTE: ESI never exposes account membership (each char is authorised
// individually via SSO), so we CANNOT verify B is on a different account — that's on the user (noted
// in UI). No longer plan-aware (dropped the Setup-Analysis "fold in rebalance" integration when moved
// — this tool has no "current plan" here, it's a plain as-is swap).
let _sepFrom = null, _sepTo = null;  // chosen character ids (strings); null = use a sensible default
function _setSepFrom(cid) { _sepFrom = String(cid); if (_sepTo === _sepFrom) _sepTo = null; _renderMoveCharacterSection(); }
function _setSepTo(cid) { _sepTo = String(cid); _renderMoveCharacterSection(); }

function _realChars() { return (_ppCharsData || []).filter(c => !c.is_dummy && !c.wallet_only); }

// Per-character factory/extractor breakdown of the current deployment.
function _facDeployment() {
  const byChar = {};
  const factories = [], extractors = [];
  _realChars().forEach(c => {
    const cid = String(c.character_id);
    const e = byChar[cid] = { cid, name: c.name, maxPlanets: c.max_planets || 0, ccu: c.ccu || 5, factories: [], extractors: [] };
    (c.planets || []).forEach(p => {
      if (!p.is_extractor && p.products && p.products.length) {
        const f = { cid, char: c.name, system: p.system, planet_num: p.planet_num, planet_type: p.planet_type, product: p.products[0] };
        e.factories.push(f); factories.push(f);
      } else if (p.is_extractor) {
        const out = (p.production && p.production[0]) || null;
        const ex = { cid, char: c.name, system: p.system, planet_num: p.planet_num, planet_type: p.planet_type,
                     p0: p.p0_name, p1: out ? { type_id: out.type_id, name: out.name } : null };
        e.extractors.push(ex); extractors.push(ex);
      }
    });
  });
  return { byChar, factories, extractors };
}

// A character's role: factory toon (has any factories), extractor toon (only extractors), or empty (no
// colonies — e.g. a freshly added toon on a new account).
function _sepRole(e) { return e.factories.length ? 'f' : (e.extractors.length ? 'e' : 'empty'); }
// A swap A⇄B is meaningful only when their roles DIFFER, or B is empty (move A onto the fresh toon). Same
// productive role both sides — factory↔factory or extractor↔extractor — is pointless and excluded.
function _sepValidTarget(a, b) { return b.cid !== a.cid && _sepRole(b) !== _sepRole(a); }
// Offer the tool when some character with colonies has at least one valid target.
function _sepHasWork(dep) {
  const chars = Object.values(dep.byChar);
  return chars.some(a => (a.factories.length + a.extractors.length) > 0 && chars.some(b => _sepValidTarget(a, b)));
}

// One cross-character teardown→rebuild card (reuses the an-move-* styling; char lives in each side's loc).
// `warn` = {html, block?} CCU-fit line.
function _sepCard(fromChar, fromLoc, toChar, toLoc, matHtml, warn) {
  return `<li class="an-move${warn && warn.block ? ' an-sep-noFit' : ''}"><div class="an-move-pair">`
    + `<div class="an-move-side an-move-rm"><span class="an-move-tag">tear down</span><span class="an-move-loc">${_esc(fromChar)} · ${fromLoc}</span><span class="an-move-mat">${matHtml}</span></div>`
    + `<div class="an-move-arrow">→</div>`
    + `<div class="an-move-side an-move-add"><span class="an-move-tag">rebuild</span><span class="an-move-loc">${_esc(toChar)} · ${toLoc}</span><span class="an-move-mat">${matHtml}</span></div>`
    + `</div>${warn && warn.html ? `<div class="an-sep-fit${warn.block ? ' an-sep-fit-block' : ''}">${warn.html}</div>` : ''}</li>`;
}

// Factory CPU/PG fit at a given CCU — "tid|planet_type|ccu" -> launchpads that fit (3 full, 1-2 cramped,
// 0 doesn't fit). Backed by /api/factory-fit (server generates the layout — real compute, hence gated
// behind _sepOpen, never fetched just because the Characters settings section is open).
let _facFit = {};
const _facFitPending = new Set();
async function _ensureFactoryFit(keys) {
  const need = [...new Set(keys)].filter(k => !(k in _facFit) && !_facFitPending.has(k));
  if (!need.length) return;
  need.forEach(k => _facFitPending.add(k));
  const items = need.map(k => { const [tid, pt, ccu] = k.split('|'); return { type_id: Number(tid), planet_type: pt, ccu: Number(ccu) }; });
  try {
    const r = await fetch('/api/factory-fit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
    const j = await r.json();
    Object.assign(_facFit, j.fit || {});
  } catch (e) { /* unknown fit just shows nothing */ }
  need.forEach(k => _facFitPending.delete(k));
  _renderMoveCharacterSection();   // re-render now that fit is known
}

// Collapsed by default — the expensive CCU-fit body only builds (and only calls _ensureFactoryFit)
// once the user explicitly reveals it.
let _sepOpen = false;
function _toggleSepOpen() { _sepOpen = !_sepOpen; _renderMoveCharacterSection(); }

// Entry point: called only when the Settings → Characters section is actually shown (openSettingsModal /
// settingsSection('characters')) — never during normal page/tab renders, so an account nobody ever opens
// Settings for pays zero cost for this tool, not even the cheap gating check.
function _renderMoveCharacterSection() {
  const el = document.getElementById('moveCharSection');
  if (!el) return;
  if (!_featureActive('move_character')) { el.innerHTML = ''; return; }
  const dep = _facDeployment();
  if (!_sepHasWork(dep)) { el.innerHTML = ''; return; }
  const head = `<div class="an-sep-head an-lever-click" onclick="_toggleSepOpen()">`
    + `<span class="an-lever-ico">⇆</span><span class="an-lever-ttl">Move a character to another account</span>`
    + `<span class="an-lever-tag">manual</span><span class="an-sep-cta">${_sepOpen ? 'Hide ▴' : 'Open ▾'}</span></div>`;
  const body = _sepOpen ? _sepSwapBody(dep)
    : `<div class="an-sep-sub">Factories on the wrong character, or moving a character to another account (incl. a freshly added empty toon)? Pick two characters and swap all their colonies 1:1.</div>`;
  el.innerHTML = `<div class="settings-subsec an-suggest an-suggest-sep an-sep-card">${head}${body}</div>`;
}

function _sepSwapBody(dep) {
  const chars = Object.values(dep.byChar);
  // A (source) = any character with colonies, default the heaviest factory toon. B (target) = a character
  // of a DIFFERENT role or an EMPTY (newly added) toon — so you can move A onto a fresh toon, then build
  // new stuff where A was. Same-role pairs (factory↔factory / extractor↔extractor) are excluded.
  const colonies = e => e.factories.length + e.extractors.length;
  const sources = chars.filter(e => colonies(e) > 0).sort((a, b) => (b.factories.length - a.factories.length) || (b.extractors.length - a.extractors.length));
  const from = (_sepFrom && sources.some(e => e.cid === _sepFrom)) ? _sepFrom : sources[0].cid;
  const A = dep.byChar[from];
  const toOpts = chars.filter(e => _sepValidTarget(A, e)).sort((a, b) => colonies(b) - colonies(a));
  const to = (_sepTo && toOpts.some(e => e.cid === _sepTo)) ? _sepTo : (toOpts[0] || {}).cid;
  const B = to ? dep.byChar[to] : null;
  const fromName = A.name, toName = B ? B.name : '';

  const opts = (list, val) => list.map(e => {
    const tag = colonies(e) ? `${e.factories.length}f·${e.extractors.length}e` : 'empty';
    return `<option value="${e.cid}"${String(e.cid) === String(val) ? ' selected' : ''}>${_esc(e.name)} · CCU ${e.ccu} (${tag})</option>`;
  }).join('');
  const controls = `<div class="an-sep-pick">Swap `
    + `<select class="an-sep-sel" onchange="_setSepFrom(this.value)">${opts(sources, from)}</select> ⇄ `
    + (to ? `<select class="an-sep-sel" onchange="_setSepTo(this.value)">${opts(toOpts, to)}</select>` : '—') + `</div>`;
  if (!to) return `${controls}<div class="an-sep-sub">You'd need a second character to swap with.</div>`;

  // FULL 1:1 SWAP — every colony keeps its EXACT planet & layout, only the owner flips. A's colonies move
  // to B and B's move to A. Each planet already hosts that exact colony, so PLACEMENT always fits — the one
  // real check is the receiving character's CCU: a factory packed at a higher CC may not fit a lower-CC
  // host (fewer facilities / less CPU+PG). We verify each factory against the target CCU via /api/factory-fit.
  const fitKey = (p, ccu) => `${p.product.type_id}|${p.planet_type}|${ccu}`;
  const fitKeys = [];
  A.factories.forEach(f => fitKeys.push(fitKey(f, B.ccu)));
  B.factories.forEach(f => fitKeys.push(fitKey(f, A.ccu)));
  if (fitKeys.length) _ensureFactoryFit(fitKeys);
  const fitWarn = (p, ccu) => {
    if (!p.product) return null;     // extractors keep 10 heads and just scale basics — always fit
    const v = _facFit[fitKey(p, ccu)];
    if (v === undefined) return { html: `<span class="an-sep-fit-chk">checking CCU ${ccu} fit…</span>` };
    if (v === 0) return { block: true, html: `⛔ Won't fit at <b>CCU ${ccu}</b> — the factory needs a higher Command Center than ${_esc(ccu === A.ccu ? fromName : toName)} has.` };
    if (v < 3) return { html: `⚠ Fits at <b>CCU ${ccu}</b> but cramped (${v} launchpad${v === 1 ? '' : 's'}, fewer facilities → lower output).` };
    return null;
  };

  const locOf = p => `${_esc(p.system)} P${p.planet_num} <span class="an-cc-tag">${_esc(p.planet_type)}</span>`;
  const matOf = p => p.product ? `<b>${_esc(p.product.name)}</b> factory`
    : `${_esc(p.p0 || '')}${p.p1 ? ' <span class="an-move-p0arrow">→</span> ' + _esc(p.p1.name) : ''}`;

  const sideCards = (srcName, dstName, dstCcu, colonies) =>
    colonies.map(p => _sepCard(srcName, locOf(p), dstName, locOf(p), matOf(p), p.product ? fitWarn(p, dstCcu) : null)).join('');
  const aAll = [...A.factories, ...A.extractors], bAll = [...B.factories, ...B.extractors];
  const aToB = sideCards(fromName, toName, B.ccu, aAll);
  const bToA = sideCards(toName, fromName, A.ccu, bAll);

  const noFit = A.factories.filter(f => _facFit[fitKey(f, B.ccu)] === 0).length
              + B.factories.filter(f => _facFit[fitKey(f, A.ccu)] === 0).length;
  const notes = [];
  if (noFit) notes.push(`⛔ <b>${noFit} factor${noFit === 1 ? 'y' : 'ies'} won't fit the receiving character's Command Center</b> — raise that character's CCU, or leave ${noFit === 1 ? 'it' : 'those'} where they are.`);
  if (bAll.length > A.maxPlanets) notes.push(`<b>${_esc(fromName)}</b> can only run ${A.maxPlanets} colonies but would take ${bAll.length} — train its Interplanetary Consolidation or leave ${bAll.length - A.maxPlanets} of <b>${_esc(toName)}</b>'s behind.`);
  if (aAll.length > B.maxPlanets) notes.push(`<b>${_esc(toName)}</b> can only run ${B.maxPlanets} colonies but would take ${aAll.length} — train its Interplanetary Consolidation or leave ${aAll.length - B.maxPlanets} of <b>${_esc(fromName)}</b>'s behind.`);
  const bEmpty = _sepRole(B) === 'empty';
  if (bEmpty) notes.push(`Once <b>${_esc(fromName)}</b> is cleared it's a blank character again — build a fresh setup on it, or let the <b>Spare capacity</b> card (Setup Analysis) suggest one.`);
  notes.push(`⚠ Make sure <b>${_esc(toName)}</b> is on a <b>different account</b> than <b>${_esc(fromName)}</b> — otherwise you still can't run both at once. Account membership isn't in the API, so this can't be checked for you.`);
  notes.push(`After you rebuild, <b>Rescan</b> to refresh the analysis.`);

  const lead = bEmpty
    ? `Move all of <b>${fromName}</b>'s colonies onto the empty character <b>${toName}</b> — same planets, just a new owner. A clean 1:1, so it always fits:`
    : `Swap everything between <b>${fromName}</b> and <b>${toName}</b> — same planets, just a different owner. A clean 1:1, so it always fits:`;
  return `${controls}
      <div class="an-levers-lead">${lead}</div>
      ${aToB ? `<div class="an-bd-bestuse-h">→ ${_esc(fromName)}'s ${aAll.length} colon${aAll.length === 1 ? 'y' : 'ies'} → ${_esc(toName)}:</div><ul class="an-move-list">${aToB}</ul>` : ''}
      ${bToA ? `<div class="an-bd-bestuse-h an-bd-bestuse-h2">↩ ${_esc(toName)}'s ${bAll.length} colon${bAll.length === 1 ? 'y' : 'ies'} → ${_esc(fromName)}:</div><ul class="an-move-list">${bToA}</ul>` : ''}
      ${notes.length ? `<div class="an-sep-notes">${notes.map(n => `<div>${n}</div>`).join('')}</div>` : ''}`;
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
  const beforeById = new Map((_ppCharsData || []).map(c => [c.character_id, c]));
  const failed = [];   // ids — named so the alert can say WHO, not just how many
  for (let i = 0; i < ids.length; i++) {
    // Re-query by id every iteration rather than reusing the passed-in `btn`: if the Characters
    // list re-renders mid-scan (e.g. loadCharacters() triggered by something else the user
    // clicked), the original button node gets replaced and a stale reference stops updating
    // anything visible — same bug class as the header Rescan button, see rescanAll().
    const liveBtn = document.getElementById('ppRefreshBtn') || btn;
    liveBtn.textContent = `Refreshing ${i + 1}/${ids.length}…`;
    try {
      const resp = await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' });
      if (!resp.ok) failed.push(ids[i]);
    } catch (e) { failed.push(ids[i]); }
  }
  const liveBtn = document.getElementById('ppRefreshBtn') || btn;
  liveBtn.disabled = false;
  liveBtn.textContent = 'Refresh';
  await loadCharacters();
  if (failed.length) {
    // A failed refresh only clears the token server-side when ESI actually rejected it
    // (permanent) — re-checking token_ok after the reload tells real "needs re-login" cases
    // apart from a transient ESI hiccup that's worth just retrying.
    const afterById = new Map((_ppCharsData || []).map(c => [c.character_id, c]));
    const nameOf = id => (beforeById.get(id) || afterById.get(id) || {}).character_name || `#${id}`;
    const dead = failed.filter(id => { const c = afterById.get(id); return c && !c.token_ok; });
    const transient = failed.filter(id => !dead.includes(id));
    let msg = '';
    if (dead.length) msg += `Needs re-login (token revoked): ${dead.map(nameOf).join(', ')}.\n`;
    if (transient.length) msg += `Temporary failure, try again shortly: ${transient.map(nameOf).join(', ')}.`;
    alert(msg.trim());
  }
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

// Admin-only: authorise a character WITH the corp-wallet scope (?wallet=1). Same popup flow as
// esiLogin, but the extra scope is requested only here — never on the public Login button.
function connectCorpWallet() {
  const w = window.open('/auth/login?wallet=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      loadCharacters();
      loadCorpWallet();
    }
  });
}

// Opt-in: authorise a character WITH the reactions-industry-jobs scope (?reactions=1). Same
// popup flow as esiLogin/connectCorpWallet — the extra scope is requested only here, never on
// the public Login button, since only accounts using the Reactions tool need it.
function connectReactionsTracking() {
  const w = window.open('/auth/login?reactions=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      loadCharacters();
      if (typeof onReactionsTabOpen === 'function') onReactionsTabOpen();
    }
  });
}

// Opt-in: authorise a character WITH the structure-market pricing scopes (?market=1). Same popup
// flow as the other connect clones — requested only from the Reactions market setup card, so the
// public Login never asks for structure-market / structure-search access. A character added here
// is a full PI + market character (MARKET_SCOPES unions the base scopes).
function connectReactionsMarket() {
  const w = window.open('/auth/login?market=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      loadCharacters();
      if (typeof _rxAfterConnect === 'function') _rxAfterConnect();
    }
  });
}

async function loadCorpWallet() {
  const el = document.getElementById('corpWalletContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  let d;
  try {
    const r = await fetch('/api/corp-wallet');
    if (!r.ok) { el.innerHTML = '<div class="pp-empty">Admin access required.</div>'; return; }
    d = await r.json();
  } catch (e) { el.innerHTML = '<div class="pp-empty">Could not reach the wallet service.</div>'; return; }

  const reconnect = `<button onclick="connectCorpWallet()">Connect a character…</button>`;
  if (!d.connected) {
    el.innerHTML = `<div class="pp-empty">No wallet character connected yet.</div>${reconnect}`;
    return;
  }
  if (d.error === 'role') {
    el.innerHTML = `<div class="an-stale-note">⚠ <b>${_esc(d.character_name)}</b> is connected, but in <b>${_esc(d.corp_name || 'its corp')}</b> it can't read the wallet. The character must be <b>CEO/Director</b>, or hold the <b>Accountant</b> or <b>Junior Accountant</b> role.</div>`
      + `<button onclick="connectCorpWallet()">Connect a different character…</button>`;
    return;
  }
  if (d.error === 'token') {
    el.innerHTML = `<div class="pp-empty"><b>${_esc(d.character_name)}</b>'s ESI token expired — reconnect to refresh it.</div>${reconnect}`;
    return;
  }
  if (d.error) {
    el.innerHTML = `<div class="pp-empty">Connected as ${_esc(d.character_name)}, but the wallet couldn't be read right now. <button onclick="loadCorpWallet()">Retry</button></div>`;
    return;
  }

  let html = `<div class="wallet-actions"><button onclick="loadCorpWallet()" title="Re-read the wallet from ESI">↻ Refresh</button></div>`
    + `<div class="an-stats">`
    + `<div class="an-stat an-ok"><div class="an-stat-val">${_fmtIsk(d.total_balance)}</div><div class="an-stat-lbl">${_esc(d.corp_name || 'Corp')} · total balance</div></div>`
    + `<div class="an-stat"><div class="an-stat-val">${_fmtIsk(d.balance)}</div><div class="an-stat-lbl">master wallet (div 1)</div></div>`
    + `<div class="an-stat"><div class="an-stat-val">${_fmtIsk(d.total_donated)}</div><div class="an-stat-lbl">player donations (recent)</div></div>`
    + `</div>`;

  if (d.donations && d.donations.length) {
    html += `<div class="wallet-don-head">Player donations <span class="pp-card-hint">— most recent first</span></div>`
      + `<div class="wallet-don-list">`
      + d.donations.map(x =>
          `<div class="wallet-don-row">`
          + `<span class="wallet-don-amt">${_fmtIsk(x.amount)}</span>`
          + `<span class="wallet-don-who">${_esc(x.donor)}</span>`
          + `<span class="wallet-don-date">${_fmtWalletDate(x.date)}</span>`
          + (x.reason ? `<span class="wallet-don-reason">“${_esc(x.reason)}”</span>` : '')
          + `</div>`).join('')
      + `</div>`;
  } else {
    const cache = (d.journal_modified || d.journal_expires)
      ? ` ESI's journal snapshot is from <b>${_fmtCacheTime(d.journal_modified)}</b>; it next refreshes at <b>${_fmtCacheTime(d.journal_expires)}</b>.`
      : '';
    let why;
    if (d.journal_status && d.journal_status !== 200) {
      why = `the journal request returned HTTP ${d.journal_status} (the character may lack the Accountant / Junior Accountant role for the journal specifically).`;
    } else if (!d.journal_count) {
      why = `the corp journal read back empty. ESI caches the corp journal ~1 hour while the balance refreshes faster, so a brand-new donation shows in the balance well before the log.${cache} Hit Refresh after that time.`;
    } else {
      const rts = Object.entries(d.ref_types || {}).map(([k, v]) => `${k} (${v})`).join(', ');
      why = `read <b>${d.journal_count}</b> journal entries but none were donations. ESI caches the journal up to ~1 hour, so a fresh donation may not be in it yet. Entry types seen: ${_esc(rts) || '—'}.`;
    }
    html += `<div class="pp-empty">No player donations yet — ${why}</div>`;
  }
  html += `<div class="an-sug-note">Reading as <b>${_esc(d.character_name)}</b> · <a href="#" onclick="connectCorpWallet();return false;">reconnect</a></div>`;
  el.innerHTML = html;
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
  await _loadFeatures();
  const dl = document.getElementById('productList');
  // Gated feature (default on): when an admin has hidden baskets from the public, don't list them.
  if (!_featureActive('baskets', true)) {
    _baskets = [];
    if (dl) dl.querySelectorAll('option[data-basket-id]').forEach(o => o.remove());
    return;
  }
  try { _baskets = (await (await fetch('/api/baskets')).json()).baskets || []; }
  catch (e) { _baskets = []; }
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

// Managed from Settings → Plans (moved out of the wizard 2026-07-09 — deleting a profile from
// inside the planning flow was an odd place for it, and profiles/plans render into always-present
// elements here rather than the wizard step, so no "only on reveal" guard is needed).
function renderProfilesBar(profiles) {
  const el = document.getElementById('settingsProfilesList');
  if (!el) return;
  if (!_loggedIn || !profiles.length) {
    el.innerHTML = _loggedIn ? '<div class="pp-empty">No saved profiles yet.</div>' : '';
    return;
  }
  el.innerHTML = profiles.map(p => {
    const op = p.overproduction_pct ?? 10;
    const sys = p.preferred_systems || 1;
    const stale = p.stale
      ? `<span class="pp-saved-stale" title="${_esc(p.stale_reason || 'fleet changed since saved')} — Load re-runs against your current fleet">⚠ stale</span>` : '';
    return `<div class="pp-saved-row${p.stale ? ' pp-saved-row-stale' : ''}">
        <span class="pp-saved-name">${_esc(p.name)}${stale}</span>
        <span class="pp-saved-meta">${_esc(p.type_name || '?')} · +${op}% overprod · ${sys} sys</span>
        <span class="pp-saved-actions">
          <button class="pp-profile-action-btn" onclick="settingsLoadProfile(${p.id})">Load</button>
          <button class="pp-profile-action-btn pp-profile-del-btn" onclick="settingsDeleteProfile(${p.id})">Delete</button>
        </span>
      </div>`;
  }).join('');
}

// Load a profile from Settings: apply it, then leave the modal and land on the wizard step it
// configures (_applyProfile already calls wizardGo(1); this just gets the right tab on screen).
function settingsLoadProfile(id) {
  const profile = _ppProfiles.find(p => p.id === id);
  if (!profile) return;
  closeSettingsModal();
  if (typeof switchTab === 'function') switchTab('planetary');
  _applyProfile(profile);
}

async function settingsDeleteProfile(id) {
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
      await _restoreFromPayload(data.payload, true);   // from a share → flag it
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
// Deliberately does NOT jump to the Plan step (autoNav=false) — opening the PI Planner tab used
// to silently dump you on a stale result page just because you'd built *any* plan before, even
// an unsaved one (reported 2026-07-09). The data is still restored (renderFinalPlan runs, so
// Step 3 is ready if you navigate there), just not forced on you.
let _autoRestoreDone = false;
async function _tryRestoreLastPlan() {
  if (_autoRestoreDone || _shareConsumed) return;
  _autoRestoreDone = true;
  let payload = null;
  try { payload = JSON.parse(localStorage.getItem('ppLastPlan') || 'null'); } catch (e) { return; }
  if (!payload || !payload.tid || !payload.plan) return;
  _shareConsumed = true;  // reuse the share guard so a share link (if any) doesn't double-restore
  try { await _restoreFromPayload(payload, false, false); } catch (e) { console.error('Restore last plan failed:', e); }
}

// autoNav: jump straight to the Plan step once restored. true for a share link (that's the
// point of the link) and an explicit "Open plan" click; false for the passive last-plan restore
// above, which should prep the data without hijacking navigation on every tab open.
async function _restoreFromPayload(payload, fromShare = false, autoNav = true) {
  _wiz.fromShare = !!fromShare;   // a shared plan is someone else's fleet — editing re-plans for YOURS
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
    if (fromShare) _showSharedBanner();
    if (autoNav) wizardGo(3);
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

// Generic image lightbox — open an image in a dark in-page overlay (so SVGs don't load as a bare
// white file page). Click the backdrop or ✕ or press Esc to close; clicking the image keeps it open.
function openImageLightbox(ev, src, alt) {
  if (ev && ev.preventDefault) ev.preventDefault();
  let ov = document.getElementById('imgLightbox');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'imgLightbox';
    ov.className = 'img-lightbox';
    ov.onclick = closeImageLightbox;
    document.body.appendChild(ov);
  }
  ov.innerHTML = `<button class="img-lightbox-close" aria-label="Close" onclick="closeImageLightbox()">×</button>`
    + `<img src="${_esc(src)}" alt="${_esc(alt || '')}" onclick="event.stopPropagation()">`;
  ov.classList.add('open');
  document.addEventListener('keydown', _lightboxEsc);
}
function closeImageLightbox() {
  const ov = document.getElementById('imgLightbox');
  if (ov) ov.classList.remove('open');
  document.removeEventListener('keydown', _lightboxEsc);
}
function _lightboxEsc(e) { if (e.key === 'Escape') closeImageLightbox(); }

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

function renderFinalPlan(data, opts = {}) {
  const content = document.getElementById('wizPlanContent');

  // Order characters naturally (case-insensitive, numeric-aware: "alt 2" before "alt 10")
  // — also fixes older saved/restored plans without a re-run.
  if (Array.isArray(data.assignments))
    data.assignments.sort((a, b) => _natCompare(a.character_name, b.character_name));

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
    // Gated feature (default on): hide the split control when an admin has pulled it from the public.
    const splitStatHtml = _featureActive('split_extraction', true) ? `
        <div class="plan-stat plan-split-ctrl" title="Split P1 production: where two P0s share a planet type, host both on one planet (2 ECUs sharing the 10-head budget → two P1 lines). The planets this frees are reinvested into more factory planets — so output rises only by what those real extra factories produce (it needs overproduction slack to reclaim; with none, nothing to split). Head counts on split planets are guidance — real yield varies with hotspot placement and depletion.">
          <span class="plan-split-seg">${splitBtns}</span>
          <span class="plan-stat-lbl">split planets · ${savedLbl}</span>
        </div>` : '';
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

  // Two ways a factory can fail to exist: the placement pass couldn't find it a planet
  // (`unplaced`), or the budget wanted more than any character could physically host, so the
  // share was never handed out (`factories_unplaceable`). Both leave planet slots idle with no
  // colony on them, so report them together — the stats bar already excludes them.
  const totalUnplacedFac = data.assignments.reduce((s, a) =>
    s + (a.factory_assignments || []).filter(f => f.unplaced).length, 0)
    + (data.factories_unplaceable || 0);
  const unplacedFacHtml = totalUnplacedFac
    ? `<div class="plan-warning">${totalUnplacedFac} factory slot${totalUnplacedFac !== 1 ? 's' : ''} unplaced — a character can only host one colony per planet, and there aren't enough ${typeNames} planets in ${data.factory_system || 'the chosen system'} to go round. Output below already excludes ${totalUnplacedFac !== 1 ? 'them' : 'it'}. Add a factory character, widen the planet types, or pick a system with more ${typeNames}.</div>`
    : '';
  const oversized = data.factory_planets_oversized || 0;
  const droppedFacHtml = oversized
    ? `<div class="plan-note">Factories can go on <b>any planet type</b> now, sized by each planet's <b>real diameter</b> (from the SDE). <b>${oversized}</b> planet${oversized !== 1 ? 's were' : ' was'} skipped as too large for the factory layout to fit its power grid${data.factory_diam_cap_km ? ` (cut-off ≈ ${(data.factory_diam_cap_km).toLocaleString()} km)` : ''} — small Ice/Lava/Storm are fine, oversized planets of any type aren't.</div>`
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
      // One P0→P1 extractor template per (P1, this toon's CCU, planet type, real diameter). The real
      // per-planet diameter (from pp_planets, tagged server-side) sizes the basic-factory count to the
      // ACTUAL planet — a smaller planet fits more basics than the planet-type default assumed, so two
      // same-type planets of different size correctly get their own template. Token 6th field = diam.
      const ecc = a.effective_ccu || data.stats?.plan_cc || 5;
      for (const e of (a.extractors || [])) {
        const p1 = e.p1_type_id;
        if (!p1) continue;
        const ept = e.best_planet_type || (e.planet_types && e.planet_types[0]) || '';
        const diam = e.diameter ? Math.round(e.diameter) : '';
        let tok = `${p1}:::${ecc}`;
        if (ept || diam) tok += ':' + ept;         // keep the planet-type slot so diam lands at index 5
        if (diam) tok += ':' + diam;
        combos.set(`e|${p1}|${ecc}|${ept}|${diam}`, tok);
      }
    }
    const toks = [...combos.values()].join(',')
      || lines.map(l => l.type_id).join(',');  // fallback: unscaled, if no placements
    templatesHref = toks ? `/api/layout/bundle?type_ids=${encodeURIComponent(toks)}&expand=0` : '';
  }

  // Split-extraction planets → one two-ECU template each (p1a:p1b:headsA:headsB:cc:ptype[:diam]),
  // appended to whichever bundle URL was built above (single-product or fuel-block). The 7th field
  // is the real per-planet diameter (tagged server-side) so a split colony's basics fit its actual
  // planet, not the type default — same fix as the single-ECU extractors.
  if (templatesHref) {
    const splitCombos = new Map();
    for (const a of (data.assignments || [])) {
      const ecc = a.effective_ccu || data.stats?.plan_cc || 5;
      for (const e of (a.extractors || [])) {
        if (!e.split || !e.legs || e.legs.length < 2) continue;
        const [la, lb] = e.legs;
        if (!la.p1_type_id || !lb.p1_type_id) continue;
        const pt = e.planet_type || 'Barren';
        const diam = e.diameter ? Math.round(e.diameter) : '';
        splitCombos.set(`${la.p1_type_id}|${lb.p1_type_id}|${la.heads}|${lb.heads}|${ecc}|${pt}|${diam}`,
          `${la.p1_type_id}:${lb.p1_type_id}:${la.heads}:${lb.heads}:${ecc}:${pt}${diam ? ':' + diam : ''}`);
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
    ${unplacedFacHtml}${droppedFacHtml}
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
  renderShoppingList(data, { initial: true });  // unfold the Command Centers list by default

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

function renderShoppingList(data, opts = {}) {
  const existing = document.getElementById('ppShoppingList');
  if (existing) { existing.remove(); return; }  // toggle off

  const { totals } = _buildShoppingList(data);
  const totalCount = Object.values(totals).reduce((s, n) => s + n, 0);

  if (!totalCount) {
    if (opts.initial) return;  // nothing to buy → just leave it folded on first render
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
  if (!opts.initial) div.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// A shared plan is a READ-ONLY view of someone else's fleet — you can't recompute their ESI setup.
// Show it with a "Make this mine" button; any edit before adopting it just bounces back to the view.
function _showSharedBanner(pulse) {
  const pc = document.getElementById('wizPlanContent');
  if (!pc || document.getElementById('ppShareBanner')) { if (pulse) _pulseMineBtn(); return; }
  const b = document.createElement('div');
  b.className = 'pp-share-note';
  b.id = 'ppShareBanner';
  b.innerHTML = "📋 <b>Shared plan</b> — a read-only view of the <b>owner's</b> characters; it can't be "
    + "re-run for their fleet. To tweak the settings for your own toons, adopt it: "
    + "<button class='pp-share-mine-btn' onclick='makeShareMine()'>Make this mine</button>";
  pc.prepend(b);
  if (pulse) _pulseMineBtn();
}
function _pulseMineBtn() {
  const btn = document.querySelector('.pp-share-mine-btn');
  if (!btn) return;
  btn.classList.remove('pp-share-mine-pulse'); void btn.offsetWidth;   // restart the animation
  btn.classList.add('pp-share-mine-pulse');
}
function makeShareMine() {
  _wiz.fromShare = false;                 // adopt it — now re-runs are computed against YOUR context
  _rerunPlan().then(() => {
    const pc = document.getElementById('wizPlanContent');
    if (pc) {
      const n = document.createElement('div');
      n.className = 'pp-share-note pp-share-note-own';
      n.innerHTML = "Now <b>your</b> plan — re-planned for your characters. Edit away.";
      pc.prepend(n);
    }
  });
}

async function _rerunPlan(overrides = {}) {
  if (_wiz.fromShare) {                    // read-only until adopted: revert controls, nudge the button
    if (_wiz.lastPlanData) renderFinalPlan(_wiz.lastPlanData, { scroll: false });
    _showSharedBanner(true);
    return;
  }
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

// ── Settings modal ────────────────────────────────────────────────────────────

let _settingsOpen = false;

function openSettingsModal(section) {
  section = section || 'characters';
  const modal = document.getElementById('settingsModal');
  if (!modal) return;
  // Show/hide nav items based on current login/feature state.
  const plansNav = document.getElementById('settingsNavPlans');
  if (plansNav) plansNav.style.display = _loggedIn ? '' : 'none';
  const notifNav = document.getElementById('settingsNavNotifications');
  if (notifNav) notifNav.style.display = (_loggedIn && _featureActive('notifications')) ? '' : 'none';
  const alertsNav = document.getElementById('settingsNavAlerts');
  if (alertsNav) alertsNav.style.display = (_loggedIn && _featureActive('alert_settings')) ? '' : 'none';
  const acctNav = document.getElementById('settingsNavAccount');
  if (acctNav) acctNav.style.display = _loggedIn ? '' : 'none';
  // Markets & Logistics (shared by Reactions + Manufacturing) — freight rates + followed markets
  // are account-wide, so show it to any logged-in user.
  const mktNav = document.getElementById('settingsNavMarkets');
  if (mktNav) mktNav.style.display = _loggedIn ? '' : 'none';
  if (section === 'markets' && !_loggedIn) section = 'characters';
  // If the requested section is gated and not available, fall back to characters.
  if (section === 'plans' && !_loggedIn) section = 'characters';
  if (section === 'notifications' && !((_loggedIn && _featureActive('notifications')))) section = 'characters';
  if (section === 'alerts' && !((_loggedIn && _featureActive('alert_settings')))) section = 'characters';
  if (section === 'account' && !_loggedIn) section = 'characters';
  modal.style.display = 'flex';
  _settingsOpen = true;
  settingsSection(section, false);
}

function closeSettingsModal() {
  const modal = document.getElementById('settingsModal');
  if (modal) modal.style.display = 'none';
  _settingsOpen = false;
}

function settingsBackdropClick(e) {
  if (e.target === document.getElementById('settingsModal')) closeSettingsModal();
}

function settingsSection(name, doLoad) {
  document.querySelectorAll('.settings-nav-item').forEach(b =>
    b.classList.toggle('active', b.id === 'settingsNav' + name.charAt(0).toUpperCase() + name.slice(1)));
  document.querySelectorAll('.settings-section').forEach(s =>
    s.style.display = (s.id === 'settingsSec' + name.charAt(0).toUpperCase() + name.slice(1)) ? '' : 'none');
  if (name === 'notifications' && doLoad !== false) loadNotifications();
  // Computed only on reveal, never during background renders — see _renderMoveCharacterSection.
  if (name === 'characters') _renderMoveCharacterSection();
  if (name === 'plans' && doLoad !== false) { loadProfiles(); renderSavedPlansBar(); }
  if (name === 'alerts' && doLoad !== false) loadAlertSettings();
  // Always load — openSettingsModal(section) passes doLoad=false, and every jump-to-markets button
  // (industry gate, Reactions redirect, recommendation) goes through it; without this the market
  // manager stays stuck on "Loading…".
  if (name === 'markets') _loadMarketsSettings();
  if (name === 'general') _loadGeneralSettings();
}

// Markets & Logistics settings — the shared market-follow list + jump-freight costs, relocated
// here from the Reactions tab so both Reactions and Manufacturing read one source. Reuses the
// reactions component builders (loaded from reactions.js; global functions, resolved at call time).
function _loadMarketsSettings() {
  const body = document.getElementById('settingsSecMarketsBody');
  if (!body) return;
  // Always show the market manager — following a market is useful to anyone and drives both
  // Reactions and Manufacturing pricing. Structure markets need a connected character (button
  // below); public region markets work without one.
  const marketMgr = `<div class="settings-subsec-title">Markets to price against <span class="pp-card-hint">— priced top-first; Jita is always the last fallback</span></div>`
    + `<div class="pp-card-hint" style="margin:6px 0 8px">Follow a public region market and/or a player structure market. <button class="ind-link-btn" onclick="connectReactionsMarket()">Connect a market character</button> (needed for structure markets).</div>`
    + `<div id="settingsMarketsMgr" class="pp-target-form" style="margin:8px 0 16px;display:block"><div class="pp-empty">Loading…</div></div>`
    + `<div style="border-top:1px solid var(--clr-border);margin-bottom:12px"></div>`;
  const freight = (typeof _rxAccountSettingsFormHtml === 'function') ? _rxAccountSettingsFormHtml() : '';
  body.innerHTML = marketMgr + freight;
  if (typeof _loadRxAccountSettings === 'function') _loadRxAccountSettings();
  if (typeof _rxMountMarkets === 'function') {
    _rxMountMarkets('settingsMarketsMgr');
    if (typeof _rxRefreshMarkets === 'function') _rxRefreshMarkets();
  }
}

// General settings — local (per-browser) UI preferences, no backend. Currently just the silent
// auto-refresh interval for the Dashboard/Reactions views (see _applyAutoRefresh in app.js).
function _loadGeneralSettings() {
  const el = document.getElementById('genAutoRefresh');
  if (el) el.value = (typeof _autoRefreshSeconds === 'function') ? _autoRefreshSeconds() : 300;
}
function _saveGeneralSettings() {
  const el = document.getElementById('genAutoRefresh');
  if (el) {
    let v = parseInt(el.value, 10);
    if (isNaN(v) || v < 0) v = 300;
    if (v > 0 && v < 30) v = 30;   // floor so a typo can't hammer the endpoints
    el.value = v;
    try { localStorage.setItem('autoRefreshSeconds', String(v)); } catch (e) {}
    if (typeof _applyAutoRefresh === 'function') _applyAutoRefresh();
    const s = document.getElementById('genSettingsStatus');
    if (s) { s.textContent = v === 0 ? 'Auto-refresh turned off' : `Saved — refreshing every ${v}s`; setTimeout(() => { s.textContent = ''; }, 2000); }
  }
}

// Freezes today's /api/my-setup-plan read (one entry per deployed product) into named,
// permanent pp_plan_snapshots rows — reuses the exact same save endpoint/shape as
// savePlanForRefills (analysis.js), since a derived "Current setup" plan is already built to the
// same shape (name/factories/consumption/products_per_day/...). Unlike the live derived read,
// which recomputes from whatever colonies exist right now, these are frozen at import time (dated
// name) so re-importing later gives you something to diff against instead of silently overwriting.
async function importCurrentSetupAsPlan(btn) {
  const statusEl = document.getElementById('importSetupStatus');
  const setStatus = (msg) => { if (statusEl) statusEl.textContent = msg; };
  if (!_loggedIn) { setStatus('Log in first.'); return; }
  if (btn) btn.disabled = true;
  setStatus('Checking your deployed colonies…');
  try {
    const resp = await fetch('/api/my-setup-plan');
    const data = await resp.json();
    const plans = data.plans || [];
    if (!plans.length) {
      setStatus('No deployed factories found — nothing to import.');
      return;
    }
    const dateTag = new Date().toISOString().slice(0, 10);
    let saved = 0, failed = 0;
    for (const plan of plans) {
      const snap = { ...plan, name: `${plan.name} — imported ${dateTag}` };
      try {
        const r = await fetch('/api/plan-snapshots', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: snap.name, snapshot: snap }),
        });
        if (r.ok) saved++; else failed++;
      } catch (e) { failed++; }
    }
    setStatus(failed
      ? `Imported ${saved} plan${saved === 1 ? '' : 's'}, ${failed} failed.`
      : `Imported ${saved} plan${saved === 1 ? '' : 's'} as saved snapshot${saved === 1 ? '' : 's'}.`);
    if (typeof renderSavedPlansBar === 'function') renderSavedPlansBar();   // keep the Refill tab's list current
  } catch (e) {
    setStatus('Import failed: ' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Intercept switchTab('characters') → settings modal. Must run at script-load time
// (not DOMContentLoaded) so the patch is in place before app.js's DOMContentLoaded
// handler calls switchTab() to restore the last active tab.
// app.js loads and fully executes before planetary.js, so switchTab is defined here.
try { if (localStorage.getItem('activeTab') === 'characters') localStorage.removeItem('activeTab'); } catch (e) {}
if (typeof window.switchTab === 'function') {
  const _origSwitchTab = window.switchTab;
  window.switchTab = function(name) {
    if (name === 'characters') { openSettingsModal('characters'); return; }
    _origSwitchTab.apply(this, arguments);
  };
}

// ── Notifications ─────────────────────────────────────────────────────────────

const _NOTIF_CHANNEL_FIELDS = {
  pushover: [
    { name: 'user_key', label: 'User key', type: 'text', placeholder: 'uXXXXXXXXXXXXXXXXXXXXX', required: true },
    { name: 'app_token', label: 'App token (optional — uses server default if blank)', type: 'text', placeholder: '' },
  ],
  ntfy: [
    { name: 'topic', label: 'Topic (keep secret)', type: 'text', placeholder: 'my-secret-topic', required: true },
    { name: 'server', label: 'Server (optional, default ntfy.sh)', type: 'text', placeholder: 'https://ntfy.sh' },
  ],
  discord: [
    { name: 'webhook_url', label: 'Webhook URL', type: 'text', placeholder: 'https://discord.com/api/webhooks/...', required: true },
  ],
};

function notifTypeChanged() {
  const type = document.getElementById('notifAddType').value;
  const fields = _NOTIF_CHANNEL_FIELDS[type] || [];
  document.getElementById('notifConfigFields').innerHTML = fields.map(f =>
    `<label>${f.label}<input type="${f.type}" class="notif-config-input" data-field="${f.name}" placeholder="${f.escapeHtml ? '' : (f.placeholder||'')}" ${f.required ? 'required' : ''}></label>`
  ).join('');
}

function _notifReadConfig() {
  const cfg = {};
  document.querySelectorAll('.notif-config-input').forEach(el => {
    if (el.value.trim()) cfg[el.dataset.field] = el.value.trim();
  });
  return cfg;
}

async function loadNotifications() {
  notifTypeChanged(); // init fields for default type
  try {
    const [sData, pData, lData] = await Promise.all([
      fetch('/api/notifications/settings').then(r => r.json()),
      fetch('/api/notifications/prefs').then(r => r.json()),
      fetch('/api/notifications/log').then(r => r.json()),
    ]);
    _renderNotifChannels(sData.settings || []);
    _renderNotifPrefs(pData);
    _renderNotifLog(lData.log || []);
  } catch (e) {
    console.error('Failed to load notifications:', e);
  }
}

function _renderNotifChannels(settings) {
  const el = document.getElementById('notifChannelList');
  if (!el) return;
  if (!settings.length) {
    el.innerHTML = '<div class="notif-empty">No channels configured.</div>';
    return;
  }
  el.innerHTML = settings.map(s => `
    <div class="notif-channel-row" data-id="${s.id}">
      <span class="notif-channel-type">${s.channel_label}</span>
      <span class="notif-channel-preview">${s.config_preview}</span>
      <label class="notif-channel-toggle"><input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="notifToggle(${s.id})"> Enabled</label>
      <button class="notif-channel-delete" onclick="notifDelete(${s.id})">Remove</button>
    </div>
  `).join('');
}

function _renderNotifPrefs(prefs) {
  const listEl = document.getElementById('notifKindList');
  if (listEl && prefs.available_kinds) {
    const notify = new Set(prefs.notify_kinds || []);
    listEl.innerHTML = prefs.available_kinds.map(k => `
      <label class="settings-toggle-row"><input type="checkbox" class="notif-kind-cb" value="${_esc(k.key)}" ${notify.has(k.key) ? 'checked' : ''}> ${_esc(k.label)}</label>
    `).join('');
  }
  const sev = document.getElementById('notifMinSeverity');
  if (sev) sev.value = prefs.min_severity === 'high' ? 'high' : 'warn';
}

function _renderNotifLog(entries) {
  const el = document.getElementById('notifLog');
  if (!el) return;
  if (!entries.length) {
    el.innerHTML = '<div class="notif-empty">No notifications sent yet.</div>';
    return;
  }
  el.innerHTML = `<table class="notif-log-table"><thead><tr>
    <th>When</th><th>Channel</th><th>Event</th><th>Character</th><th>Status</th>
  </tr></thead><tbody>` +
  entries.map(e => {
    const when = e.sent_at ? new Date(e.sent_at).toLocaleString() : '';
    const ok = e.status === 'ok';
    return `<tr>
      <td>${when}</td>
      <td>${e.channel}</td>
      <td>${e.event}</td>
      <td>${e.character || ''}</td>
      <td class="${ok ? 'notif-ok' : 'notif-err'}">${e.status || ''}</td>
    </tr>`;
  }).join('') + '</tbody></table>';
}

async function notifAddChannel() {
  const type = document.getElementById('notifAddType').value;
  const cfg = _notifReadConfig();
  const status = document.getElementById('notifAddStatus');
  status.textContent = 'Saving...';
  try {
    const r = await fetch('/api/notifications/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: type, config: cfg }),
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || r.status); }
    status.textContent = 'Saved.';
    await loadNotifications();
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}

async function notifSendTest() {
  const type = document.getElementById('notifAddType').value;
  const cfg = _notifReadConfig();
  const status = document.getElementById('notifAddStatus');
  status.textContent = 'Sending test...';
  try {
    const r = await fetch('/api/notifications/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: type, config: cfg }),
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || r.status); }
    status.textContent = 'Test sent successfully.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}

async function notifToggle(id) {
  await fetch(`/api/notifications/settings/${id}`, { method: 'PATCH' });
  await loadNotifications();
}

async function notifDelete(id) {
  if (!confirm('Remove this notification channel?')) return;
  await fetch(`/api/notifications/settings/${id}`, { method: 'DELETE' });
  await loadNotifications();
}

async function notifResendLast() {
  const status = document.getElementById('notifPrefsStatus');
  status.textContent = 'Resending...';
  try {
    const r = await fetch('/api/notifications/resend-last', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    if (d.errors && d.errors.length) {
      status.textContent = 'Send error: ' + d.errors.join('; ');
    } else {
      const summary = d.sent.map(s => s.title).join(', ');
      status.textContent = 'Sent: ' + summary;
    }
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}

async function notifSavePrefs() {
  const status = document.getElementById('notifPrefsStatus');
  const notifyKinds = Array.from(document.querySelectorAll('.notif-kind-cb:checked')).map(cb => cb.value);
  const prefs = {
    notify_kinds: notifyKinds,
    min_severity: document.getElementById('notifMinSeverity').value,
  };
  status.textContent = 'Saving...';
  try {
    const r = await fetch('/api/notifications/prefs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(prefs),
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || r.status); }
    status.textContent = 'Saved.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}

// ── Alert thresholds (Settings → Alerts) ──────────────────────────────────────

function _renderAlertSettings(s) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('alertExpiringHours', s.expiring_hours);
  set('alertStorageWarnPct', s.storage_warn_pct);
  set('alertStorageHighPct', s.storage_high_pct);
  set('alertStorageHighTtfHours', s.storage_high_ttf_hours);
  set('alertStorageUrgentHours', s.storage_urgent_hours);
  set('alertReactionRefillHours', s.reaction_refill_hours);
  const reactionSubsec = document.getElementById('alertReactionSubsec');
  if (reactionSubsec) reactionSubsec.style.display = (typeof _featureActive === 'function' && _featureActive('reactions')) ? '' : 'none';
  const listEl = document.getElementById('alertMutedList');
  if (listEl && s.available_kinds) {
    const muted = new Set(s.muted_kinds || []);
    listEl.innerHTML = s.available_kinds.map(k => `
      <label class="settings-toggle-row"><input type="checkbox" class="alert-mute-cb" value="${_esc(k.key)}" ${muted.has(k.key) ? 'checked' : ''}> ${_esc(k.label)}</label>
    `).join('');
  }
}

async function loadAlertSettings() {
  try {
    const r = await fetch('/api/alert-settings');
    const s = await r.json();
    _renderAlertSettings(s);
  } catch (e) {
    console.error('Failed to load alert settings:', e);
  }
}

async function saveAlertSettings() {
  const status = document.getElementById('alertSettingsStatus');
  const muted = Array.from(document.querySelectorAll('.alert-mute-cb:checked')).map(cb => cb.value);
  const body = {
    expiring_hours: parseFloat(document.getElementById('alertExpiringHours').value) || 3,
    storage_warn_pct: parseFloat(document.getElementById('alertStorageWarnPct').value) || 80,
    storage_high_pct: parseFloat(document.getElementById('alertStorageHighPct').value) || 95,
    storage_high_ttf_hours: parseFloat(document.getElementById('alertStorageHighTtfHours').value) || 2,
    storage_urgent_hours: parseFloat(document.getElementById('alertStorageUrgentHours').value) || 3,
    reaction_refill_hours: parseFloat(document.getElementById('alertReactionRefillHours').value) || 24,
    muted_kinds: muted,
  };
  status.textContent = 'Saving...';
  try {
    const r = await fetch('/api/alert-settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || r.status); }
    status.textContent = 'Saved.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}

async function resetAlertSettings() {
  const status = document.getElementById('alertSettingsStatus');
  status.textContent = 'Resetting...';
  try {
    const r = await fetch('/api/alert-settings/reset', { method: 'POST' });
    const s = await r.json();
    if (!r.ok) throw new Error(s.detail || r.status);
    _renderAlertSettings(s);
    status.textContent = 'Reset to defaults.';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  }
}
