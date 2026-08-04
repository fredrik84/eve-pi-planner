// Planet DB tab — split out of planetary.js (2026-06-23) to keep feature files small.
// Constellation/region filter, planet list + chunked table, import modal.
// Loaded as a separate <script> after planetary.js; functions are global, resolve at call
// time. Shared state/util lives in planetary.js (loads first).

// ── Constellation filter ──────────────────────────────────────────────────────

let _ppConstRegions = {};    // constellation -> region
let _ppConstByRegion = {};   // region -> [constellations]  (only regions in the Planet DB)
let _ppRegion = '';          // currently displayed region
let _ppSelected = new Set(); // selected constellation names (the filter)

async function loadConstellations() {
  try {
    const data = await api('/api/constellations');
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
    const data = await api('/api/planets');
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
  // Computed once per render, not per-cell: whether to show the pooled real-world measured-yield
  // annotation (app/yield_stats.py) alongside each planet's static submitted richness value.
  const showMeasured = _featureActive('measured_yield');
  _ppRender = { rows: planets, cursor: 0, tbody, cols: _ppActiveCols, wrap, showMeasured };
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
    // Row-level "this planet has measured data somewhere" badge — separate from the per-cell
    // ⚡pct annotation below, so scanning the table doesn't require checking every column.
    const rowMeasured = r.showMeasured && p.measured;
    const rowBadge = rowMeasured
      ? `<span class="pp-row-measured" title="Measured yield data: ${
          Object.entries(p.measured).map(([col, m]) => `${_esc(P0_LABEL[col] || col)} (n=${m.n})`).join(', ')
        }">⚡</span>`
      : '';
    html += '<tr>' +
      `<td class="left">${_esc(p.system)}</td>` +
      `<td class="left">${_esc(String(p.planet_num))}${rowBadge}</td>` +
      `<td class="left planet-type ${PP_TYPE_CLASS[p.planet_type] || ''}">${_esc(p.planet_type)}</td>` +
      `<td class="left">${_esc(p.constellation)}</td>`;
    for (const col of r.cols) {
      const v = p[col] || 0;
      const m = r.showMeasured && p.measured && p.measured[col];
      if (v > 0) {
        html += m
          ? `<td class="p0-val" title="Measured avg from ${m.n} real colonies: ${m.pct}">${v} <span class="p0-measured">⚡${m.pct}</span></td>`
          : `<td class="p0-val">${v}</td>`;
      } else if (m) {
        html += `<td class="p0-val p0-measured-only" title="No submitted value — measured avg from ${m.n} real colonies">⚡${m.pct}</td>`;
      } else {
        html += '<td class="p0-zero">—</td>';
      }
    }
    html += '</tr>';
  }
  r.tbody.insertAdjacentHTML('beforeend', html);
  r.cursor = end;
  // Keep filling until the container is scrollable (so on-scroll loading can kick in).
  if (r.cursor < r.rows.length && r.wrap.scrollHeight <= r.wrap.clientHeight + 1) _ppRenderChunk();
}

async function clearPlanets() {
  if (!await ppConfirm('Clear all planet data?')) return;
  try {
    await apiSend('DELETE', '/api/planets');
  } catch (e) { toastError(e, 'Could not clear the planet data'); return; }
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
    const data = await apiSend('POST', '/api/planets/import', { text });
    const warn = (data.errors && data.errors.length) ? ' Warnings: ' + data.errors.join('; ') : '';
    const importBtn = document.getElementById('ppImportBtn');
    if (data.queued) {
      // Non-admin: held for admin review, nothing written to the live DB yet.
      toast(`Thanks! ${data.submitted} planet${data.submitted === 1 ? '' : 's'} submitted for review. `
            + `An admin will approve them before they appear in the Planet DB.` + warn, 'success', 9000);
      importBtn.textContent = `✓ submitted`;
      closePlanetImport();
    } else {
      if (warn) toast(`Imported ${data.imported}, skipped ${data.skipped}.${warn}`, 'info', 9000);
      closePlanetImport();
      await loadPlanets(true);
      loadConstellations();
      importBtn.textContent = `✓ ${data.imported}`;
    }
    setTimeout(() => importBtn.textContent = 'Import', 2500);
  } catch (e) {
    toastError(e, 'Import failed');
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

