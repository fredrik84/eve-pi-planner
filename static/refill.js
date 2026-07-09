// Refill tool (PI Planner tab) — split out of planetary.js (2026-06-23) to keep feature files small.
// Saved-plans bar, build/refill mode, P1-stack distribution into a saved plan.
// Loaded as a separate <script> after planetary.js; functions are global, resolve at call
// time. Shared state/util lives in planetary.js (loads first).

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
// Ignore what the last scan saw in the launchpads and fill to a clean 30,000 m³. ON by default:
// factory pad contents aren't simulated forward (only extractors are), so the scanned figure goes
// stale fast, and the usual workflow is to empty the pads when dropping off the next batch.
let _refillIgnorePads = (() => { try { return localStorage.getItem('refillIgnorePads') !== '0'; } catch (e) { return true; } })();
let _refillUserPicked = false;   // true once the user deliberately chose a plan — else default to Current Setup
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
                          hasPayload: !!s.has_payload, saved: true, stale: !!s.stale, stale_reason: s.stale_reason || '' })),
    ..._loadPlanSnapshots().filter(s => !serverNames.has(s.name)),
  ];
}

// Settings → Plans "Saved plans" list. Lists the refill snapshots so you can jump straight to
// reopening/refilling one. Moved out of the wizard's step 1 (2026-07-09) — deleting a saved plan
// from inside the planning flow was an odd place for it; management now lives in Settings, always
// rendered into an always-present element (no "only on reveal" guard needed).
async function renderSavedPlansBar() {
  const el = document.getElementById('settingsPlansList');
  if (!el) return;
  const snaps = await _fetchAllSnapshots();
  _analyzeSnaps = snaps;   // warm the Setup Analysis cache so its tab paints instantly
  if (!snaps.length) { el.innerHTML = _loggedIn ? '<div class="pp-empty">No saved plans yet.</div>' : ''; return; }
  const rows = snaps.map(s => {
    const nfac = (s.factories || []).length;
    const ppd = s.products_per_day;
    const meta = `${nfac} factor${nfac === 1 ? 'y' : 'ies'}`
      + (ppd ? ` · ${Number(ppd).toLocaleString()} ${_esc(s.unit_label || 'units')}/day` : '');
    const del = `<button class="pp-profile-action-btn pp-profile-del-btn" onclick="deleteSavedPlan('${s.id}','${s.srvId || ''}')">Delete</button>`;
    const open = s.hasPayload
      ? `<button class="pp-profile-action-btn" onclick="openSavedPlanFull('${s.srvId || ''}')" title="Reopen the full allocation plan">Open plan</button>`
      : '';
    const stale = s.stale ? `<span class="pp-saved-stale" title="${_esc(s.stale_reason || '')} — re-run to refresh">⚠ stale</span>` : '';
    return `<div class="pp-saved-row${s.stale ? ' pp-saved-row-stale' : ''}">
        <span class="pp-saved-name">${_esc(s.name)}${s.saved ? '' : ' · this browser'}${stale}</span>
        <span class="pp-saved-meta">${meta}</span>
        <span class="pp-saved-actions">
          ${open}
          <button class="pp-profile-action-btn" onclick="openSavedPlanRefill('${s.id}')">Refill</button>
          ${del}
        </span>
      </div>`;
  }).join('');
  el.innerHTML = rows;
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
  if (typeof closeSettingsModal === 'function') closeSettingsModal();
  if (typeof switchTab === 'function') switchTab('planetary');
  await _restoreFromPayload(payload, false, true);   // explicit open → navigate to the plan
}

// Open a saved plan straight in the refill tool (PI Planner → Refill a plan, that plan selected).
function openSavedPlanRefill(id) {
  const sect = document.getElementById('planDistSection');
  if (sect) sect.dataset.sel = id;
  _refillUserPicked = true;       // they explicitly opened THIS plan to refill — keep it selected
  if (typeof closeSettingsModal === 'function') closeSettingsModal();
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
  _refillUserPicked = true;       // deliberate choice — honour it over the Current Setup default
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
  // Default to the live Current Setup (◆ derived) — that's what the player actually runs. Only honour a
  // remembered selection when they deliberately chose one this session (dropdown / "refill this plan"),
  // so a stale saved plan can't silently stick and get refilled against the wrong factory layout.
  let snap = (_refillUserPicked && el.dataset.sel) ? snaps.find(s => String(s.id) === el.dataset.sel) : null;
  if (!snap) snap = snaps.find(s => s.derived) || snaps[0];
  // Flag a saved plan whose factory planets aren't what's currently deployed (the source of the
  // "it splits across more factories than I built" confusion).
  const deployedLocs = new Set(derived.flatMap(d => (d.factories || []).map(f => f.loc)));
  const _mismatch = s => !s.derived && deployedLocs.size > 0 && Array.isArray(s.factories)
    && s.factories.some(f => !deployedLocs.has(f.loc));
  const opts = snaps.map(s =>
    `<option value="${s.id}"${String(s.id) === String(snap.id) ? ' selected' : ''}>${s.derived ? '◆ ' : ''}${_esc(s.name)}${_mismatch(s) ? ' ⚠ differs from deployed' : ''}${(!s.saved && !s.derived) ? ' · this browser (unsaved)' : ''}</option>`).join('');
  const mismatchNote = _mismatch(snap)
    ? `<div class="dist-mismatch">⚠ This plan's factories don't match your deployed setup, so the split may target planets you didn't build${derived.length ? ` — switch to <b>◆ ${_esc(derived[0].name)}</b> to refill what you actually run` : ''}.</div>`
    : '';
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
        ${mismatchNote}
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
function _setRefillIgnorePads(on) { _refillIgnorePads = !!on; try { localStorage.setItem('refillIgnorePads', on ? '1' : '0'); } catch (e) {} updateP1Distribution(); }

function _renderRefillControls() {
  const el = document.getElementById('refillControls');
  if (!el || !_refillModel) return;
  const chars = [...new Set(_refillModel.flatMap(g => g.facs.map(f => f.char)))].filter(Boolean).sort(_natCompare);
  if (_refillChar && !chars.includes(_refillChar)) _refillChar = '';   // stale selection
  const total = _refillModel.reduce((a, g) => a + g.facs.length, 0);
  const charSel = chars.length > 1
    ? `<label class="dist-ctl">Character
        <select onchange="_setRefillChar(this.value)">
          <option value="">All characters (${total})</option>
          ${chars.map(c => `<option value="${_esc(c)}"${c === _refillChar ? ' selected' : ''}>${_esc(c)}</option>`).join('')}
        </select></label>` : '';
  // Show the "empty pads" toggle only when some factory actually has scanned pad contents to ignore.
  const hasPadData = _refillModel.some(g => g.facs.some(f => typeof f.inputM3 === 'number' && f.inputM3 > 0));
  const ignoreCtl = hasPadData
    ? `<label class="dist-ctl dist-ctl-check" title="Factory launchpad contents come from the last scan and aren't simulated forward, so they go stale. ON = ignore them and fill to a clean 30,000 m³ (assumes you empty the pads when you drop off the batch). OFF = subtract what the last scan saw.">
        <input type="checkbox" ${_refillIgnorePads ? 'checked' : ''} onchange="_setRefillIgnorePads(this.checked)"> Pads emptied at drop-off</label>` : '';
  el.innerHTML = `${charSel}
    <div class="dist-view-toggle">
      <button class="dist-view-btn${_refillView === 'summary' ? ' on' : ''}" onclick="_setRefillView('summary')" title="One uniform drop that fits every factory">Summary</button>
      <button class="dist-view-btn${_refillView === 'detailed' ? ' on' : ''}" onclick="_setRefillView('detailed')" title="Each factory topped to its own free launchpad space">All planets</button>
    </div>${ignoreCtl}`;
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
    if (tid) _p1Stacks[tid] = (_p1Stacks[tid] || 0) + parsed[1];   // sum duplicate lines (e.g. pads paste + inventory paste)
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
    // Free LP space = full buffer minus what the last scan saw — unless we're ignoring stale pad
    // contents (default), in which case fill to the full 30,000 m³.
    f.availM3 = (!_refillIgnorePads && f.inputM3 != null) ? Math.max(0, _LP_CAP_M3 - f.inputM3) : _LP_CAP_M3;
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
    `<div class="refill-stat"><span class="refill-stat-val">${_fmtHours(minDays * 24)}</span>
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

