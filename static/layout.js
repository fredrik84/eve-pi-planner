// Factory Layout tab — split out of planetary.js (2026-06-23). Loaded as a separate
// <script> after planetary.js; all functions are global and resolve at call time.
// State/util it relies on (_esc, _fmtIsk, _ppProducts, switchTab hooks) lives in planetary.js.

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

