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
  const hasStructure = Object.keys(_indFacilityMap).some(k => k.startsWith('s:'));
  indApplyGate(hasStructure);
  if (!hasStructure) return;

  indLoadSetupSummary();
  indLoadLifetime();
  indLoadQueue();
  indLoadInstall();
  indLoadRunning();
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
  if (sum) {
    const s = slots ? `<b>${slots.manufacturing_free}/${slots.manufacturing_slots}</b> mfg · <b>${slots.reaction_free}/${slots.reaction_slots}</b> rx slots free` : '';
    const b = bp ? (bp.connected ? ` · <span class="ind-bp-ok">${bp.owned_count} blueprints</span>` : '') : '';
    sum.innerHTML = s + b;
  }
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
  const w = window.open('/auth/login?industry=1', 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      indRefreshBlueprints();
    }
  });
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
      + `${c.manufacturing_free}/${c.manufacturing_slots}<span class="ind-slot-sub">mfg</span> · `
      + `${c.reaction_free}/${c.reaction_slots}<span class="ind-slot-sub">rx</span></span>`
    ).join('');
    el.innerHTML = `<div class="ind-slot-tot"><b>${d.manufacturing_free}/${d.manufacturing_slots}</b> manufacturing · `
      + `<b>${d.reaction_free}/${d.reaction_slots}</b> reaction slots free `
      + `<button class="ind-bp-btn" onclick="indRefreshJobs()" title="Re-read running jobs from ESI">Refresh jobs</button></div>`
      + `<div class="ind-slot-chips">${chips || '<span class="pp-sub">No characters — add one to get real slot counts.</span>'}</div>`;
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
  _indPicked = { type_id: typeId, name };
  document.getElementById('indSearch').value = name;
  _indHideResults();
  document.getElementById('indPlanBtn').disabled = false;
  document.getElementById('indQueueBtn').disabled = false;
  document.getElementById('indPickHint').textContent = '';
}

// ── Single-product plan ─────────────────────────────────────────────────────────────────────
async function indRunPlan() {
  if (!_indPicked) return;
  const qty = Math.max(1, parseInt(document.getElementById('indQty').value) || 1);
  const out = document.getElementById('indResult');
  out.innerHTML = '<div class="pp-card"><p class="pp-sub">Planning…</p></div>';
  try {
    const r = await fetch('/api/industry/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type_id: _indPicked.type_id, quantity: qty, prioritize_speed: _indPrioSpeed(), ..._indFacilityBonus() }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(e.detail || 'Plan failed')}</p></div>`; return; }
    const d = await r.json();
    _indLastPlan = d;
    out.innerHTML = _indRenderPlan(d, `Build ${qty}× ${_esc(d.target.name)}`);
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
  tiles.push(['Materials', fmtIsk(m.materials_cost), ''], ['Job fees', fmtIsk(m.job_cost), ''],
             ['Build steps', steps, 'Distinct things to build — each may split into parallel jobs across your slots']);
  if (m.makespan_hours != null) tiles.push(['Makespan', _fmtHours(m.makespan_hours), 'Wall-clock time with jobs running in parallel across your slots']);
  else if (m.total_job_hours != null) tiles.push(['Total job time', _fmtHours(m.total_job_hours), '']);
  return `<div class="an-stats">` + tiles.map(([l, v, t]) =>
    `<div class="an-stat"${t ? ` title="${_esc(t)}"` : ''}><div class="an-stat-lbl">${l}</div><div class="an-stat-val">${v}</div></div>`).join('') + `</div>`;
}

function _indPrioSpeed() {
  const el = document.getElementById('indPrioSpeed');
  return el ? el.checked : true;
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
}

let _indLastPlan = null;   // last rendered plan, for the shopping-list copy features
let _indShopStageData = {};   // stage key (tier) -> [{name, qty}], for per-stage multibuy copy

// Walk the build tree once into { byType (with tier + type_id), tiers (tier -> [entries]), maxT }.
// Shared by the pipeline visualization and the per-stage shopping list so their stage numbering
// always matches — a "Buy" card in the pipeline links to the exact same stage in the list below.
function _indComputeTiers(tree) {
  const byType = {};
  const inputsOf = {};      // type_id -> Set of type_ids it consumes
  const consumersOf = {};   // type_id -> Set of type_ids that consume it
  (function walk(n, depth) {
    if (!n) return;
    const e = byType[n.type_id] || (byType[n.type_id] = { type_id: n.type_id, name: n.name, decision: n.decision, activity: n.activity, owned: n.owned, qty: 0, runs: 0, tier: depth });
    e.qty += n.qty || 0;
    e.runs += n.runs || 0;
    e.tier = Math.max(e.tier, depth);
    if (n.decision === 'build' || n.decision === 'buy') e.decision = n.decision;
    (n.inputs || []).forEach(c => {
      (inputsOf[n.type_id] || (inputsOf[n.type_id] = new Set())).add(c.type_id);
      (consumersOf[c.type_id] || (consumersOf[c.type_id] = new Set())).add(n.type_id);
      walk(c, depth + 1);
    });
  })(tree, 0);
  const tiers = {};
  Object.values(byType).forEach(e => (tiers[e.tier] = tiers[e.tier] || []).push(e));
  const maxT = Object.keys(tiers).length ? Math.max(...Object.keys(tiers).map(Number)) : 0;
  return { byType, tiers, maxT, inputsOf, consumersOf };
}

function _indStageLabel(t, maxT, hasBuilds) {
  if (t === 0) return 'Finished';
  return hasBuilds ? `Stage ${maxT - t}` : `Buy — stage ${maxT - t}`;
}

function _indShopRowHtml(s) {
  return `<tr><td>${_esc(s.name)}`
    + `${s.bought_for_speed ? ' <span class="ind-speed-badge" title="Bought instead of built to save time">for speed</span>' : ''}`
    + `${s.bought_marginal ? ' <span class="ind-marginal-badge" title="Building this would save too little to be worth a job">low saving</span>' : ''}</td>`
    + `<td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`;
}

// The shopping list split by build stage — buy just what the next step needs, or everything at
// once. Stage numbering/labels match the pipeline's "Buy N materials" cards exactly, since both
// are derived from the same _indComputeTiers() walk.
function _indShoppingSections(d, tiersData) {
  const list = d.shopping_list || [];
  if (!list.length) return '<p class="pp-sub">Nothing to buy — built entirely from stock/recipes.</p>';
  const { tiers, maxT } = tiersData;
  const byId = {};
  list.forEach(s => { byId[s.type_id] = s; });
  _indShopStageData = {};
  let sections = '';
  for (let t = maxT; t >= 0; t--) {
    const entries = (tiers[t] || []).filter(e => e.decision !== 'build');
    const rows = entries.map(e => byId[e.type_id]).filter(Boolean);
    if (!rows.length) continue;
    const hasBuilds = (tiers[t] || []).some(e => e.decision === 'build');
    const label = _indStageLabel(t, maxT, hasBuilds);
    _indShopStageData[t] = rows.map(r => ({ name: r.name, qty: r.qty }));
    const stageCost = rows.reduce((a, s) => a + (s.line_cost || 0), 0);
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-${t}">`
      + `<div class="ind-shop-stage-hd"><span>${_esc(label)} — ${rows.length} item${rows.length > 1 ? 's' : ''} · ${fmtIsk(stageCost)}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy(${t})">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows.map(_indShopRowHtml).join('')}</tbody></table></div>`;
  }
  const totalCost = list.reduce((a, s) => a + (s.line_cost || 0), 0);
  return `<div class="ind-shop-bar"><button class="ind-copy-btn" onclick="indCopyMultibuy()">Copy everything</button>`
    + `<span class="ind-shop-tot">${list.length} items · ${fmtIsk(totalCost)}</span></div>${sections}`;
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

// Group a schedule wave's parallel split-jobs back into one entry per product.
function _indWaveGroup(w) {
  const b = {};
  (w.tasks || []).forEach(t => {
    const g = b[t.type_id] || (b[t.type_id] = { name: t.name || _indName(t.type_id), runs: 0, activity: t.activity, dur: 0 });
    g.runs += t.runs; g.dur = Math.max(g.dur, t.duration_hours);
  });
  return Object.values(b).sort((a, b) => b.dur - a.dur);
}
function _indJobChips(g) {
  return g.map(x => `<span class="ind-wave-job">${_esc(x.name)} ×${x.runs}${x.activity === 'reaction' ? ' rx' : ''} · ${_fmtHours(x.dur)}</span>`).join('');
}

// Step-by-step "what to do right now": buy your materials, start the first wave of jobs now, then
// each later wave as the previous finishes. The prominent, plain-language answer to "what am I
// supposed to be doing" — the tree/schedule below are just the detail behind it.
// "N jobs · M runs" — the concise per-wave summary the user asked for (not a chip per job).
function _indStepSummary(g) {
  const runs = g.reduce((s, x) => s + x.runs, 0);
  return `${g.length} job${g.length > 1 ? 's' : ''} · ${runs.toLocaleString()} runs`;
}
function _indStepItems(g, open) {
  return `<details class="ind-step-items"${open ? ' open' : ''}><summary>show items</summary>`
    + `<div class="ind-wave-jobs">${_indJobChips(g)}</div></details>`;
}

function _indStepsHtml(d) {
  const waves = (d.schedule && d.schedule.waves) || [];
  if (!waves.length) return '';
  const shop = d.shopping_list || [];
  let n = 0;
  let html = '<div class="ind-steps"><div class="ind-steps-title">Step by step</div>';
  if (shop.length) {
    n++;
    html += `<div class="ind-step"><div class="ind-step-hd"><span class="ind-step-num">${n}</span>Buy your materials</div>`
      + `<div class="ind-step-body">${shop.length} item${shop.length > 1 ? 's' : ''} · ${fmtIsk(d.metrics.materials_cost)} — full list below.</div></div>`;
  }
  n++;
  const now = _indWaveGroup(waves[0]);
  html += `<div class="ind-step ind-step-now"><div class="ind-step-hd"><span class="ind-step-num">${n}</span>Start ${_indStepSummary(now)} now <span class="ind-step-tag">do this now</span></div>`
    + _indStepItems(now, true) + `</div>`;
  if (waves.length > 1) {
    const later = waves.slice(1).map((w, i) => {
      const g = _indWaveGroup(w);
      return `<div class="ind-step ind-step-later"><div class="ind-step-hd"><span class="ind-step-num">${n + 1 + i}</span>Then start ${_indStepSummary(g)} <span class="ind-step-when">≈ +${_fmtHours(w.start_hours)}</span></div>`
        + _indStepItems(g, false) + `</div>`;
    }).join('');
    html += `<details class="ind-details ind-steps-more"><summary>Then, as each batch finishes — ${waves.length - 1} more step${waves.length > 2 ? 's' : ''}</summary>${later}</details>`;
  }
  html += `<div class="ind-step ind-step-done"><div class="ind-step-hd"><span class="ind-step-num">✓</span>Done — ${_esc(d.target ? d.target.name : 'product')} built in ≈ ${_fmtHours(d.metrics.makespan_hours)}</div></div>`;
  return html + '</div>';
}

// The build as a PRODUCTION PIPELINE: stages flow left→right (raw/reacted on the left, finished
// product on the right), split into horizontal LANES by where the work physically happens —
// reactions and manufacturing are different structures with separate job slots, and bought
// materials aren't built anywhere. Hovering a card highlights what feeds it and what it feeds.
let _indPipeGraph = { inputsOf: {}, consumersOf: {} };

function _indPipelineHtml(d, tiersData) {
  const tree = d.tree;
  if (!tree || !(tree.inputs || []).length) return '';
  const { tiers, maxT, inputsOf, consumersOf } = tiersData;
  _indPipeGraph = { inputsOf: inputsOf || {}, consumersOf: consumersOf || {} };

  // Columns, deepest stage first so the finished product lands on the right.
  const cols = [];
  for (let t = maxT; t >= 0; t--) {
    const items = tiers[t] || [];
    if (items.length) cols.push({ t, items });
  }
  if (!cols.length) return '';

  const isRx = e => e.decision === 'build' && e.activity === 'reaction';
  const isMfg = e => e.decision === 'build' && e.activity !== 'reaction';
  const isBuy = e => e.decision !== 'build';
  const lanes = [
    { key: 'mfg', title: 'Manufacturing', sub: _indBuildingLabel('manufacturing') || 'your structure', match: isMfg },
    { key: 'rx', title: 'Reactions', sub: _indBuildingLabel('reaction') || 'reaction structure', match: isRx },
    { key: 'buy', title: 'Buy', sub: 'from market', match: isBuy },
  ].filter(l => cols.some(c => c.items.some(l.match)));

  const buildCard = e => {
    const owned = e.owned ? `<span class="ind-owned" title="You own this ${e.owned.kind.toUpperCase()}">${e.owned.kind.toUpperCase()}</span>` : '';
    const runs = e.runs ? `<span class="ind-pipe-runs">${e.runs.toLocaleString()} run${e.runs > 1 ? 's' : ''}</span>` : '';
    return `<div class="ind-pipe-card ind-pipe-build" data-tid="${e.type_id}" title="Hover to trace what this feeds"><span class="ind-pipe-name">${_esc(e.name)}</span>`
      + `<span class="ind-pipe-meta">×${Math.round(e.qty).toLocaleString()} <span class="ind-pipe-tag ind-t-build">build${e.activity === 'reaction' ? ' rx' : ''}</span>${runs}${owned}</span></div>`;
  };
  const buyCard = (buys, t) => {
    const names = buys.slice(0, 25).map(b => b.name).join(', ') + (buys.length > 25 ? '…' : '');
    const members = buys.map(b => b.type_id).join(',');
    return `<div class="ind-pipe-card ind-pipe-buys" data-members="${members}" title="${_esc(names)} — click to jump to this stage's shopping list" onclick="_indJumpToStage(${t})"><span class="ind-pipe-name">Buy ${buys.length} material${buys.length > 1 ? 's' : ''}</span>`
      + `<span class="ind-pipe-meta">in shopping list ↓</span></div>`;
  };

  // Header row: one stage label per column (the lane-label column gets an empty corner cell).
  let html = `<div class="ind-pipe-corner"></div>`;
  cols.forEach(({ t, items }, i) => {
    const builds = items.filter(e => e.decision === 'build');
    const label = _indStageLabel(t, maxT, !!builds.length);
    const count = builds.length ? `<span>${builds.length}</span>` : '';
    html += `<div class="ind-pipe-hd${t === 0 ? ' ind-pipe-hd-final' : ''}${i < cols.length - 1 ? ' ind-pipe-hd-flow' : ''}">${label}${count}</div>`;
  });

  // One row per lane; empty cells keep every stage aligned across lanes.
  lanes.forEach(l => {
    html += `<div class="ind-pipe-lanelbl ind-lane-${l.key}"><span class="ind-pipe-lanename">${l.title}</span>`
      + `<span class="ind-pipe-lanesub" title="${_esc(l.sub)}">${_esc(l.sub)}</span></div>`;
    cols.forEach(({ t, items }) => {
      const mine = items.filter(l.match);
      let cards = '';
      if (l.key === 'buy') {
        if (mine.length) cards = buyCard(mine, t);
      } else {
        const sorted = mine.slice().sort((a, b) => (b.qty || 0) - (a.qty || 0));
        cards = sorted.slice(0, 12).map(buildCard).join('');
        if (sorted.length > 12) cards += `<div class="ind-pipe-more">+${sorted.length - 12} more</div>`;
      }
      html += `<div class="ind-pipe-cell${t === 0 ? ' ind-pipe-final' : ''}">${cards}</div>`;
    });
  });

  return `<details class="ind-details" open><summary>Build pipeline</summary>`
    + `<p class="ind-pipe-hint">Hover a step to trace what feeds it and what it feeds. Lanes are the structures the work happens in.</p>`
    + `<div class="ind-pipe-scroll"><div class="ind-pipe-grid" style="--ind-cols:${cols.length}">${html}</div></div></details>`;
}

// The type_ids a card stands for: a build card is one type, a condensed buy card is many.
function _indCardTids(card) {
  if (!card) return [];
  if (card.dataset.tid) return [Number(card.dataset.tid)];
  return (card.dataset.members || '').split(',').filter(Boolean).map(Number);
}

// Hover trace: dim the pipeline, then light up the hovered step, everything it FEEDS (downstream,
// e.g. what a stage-1 component is used for in stage 2) and everything that feeds IT (upstream).
function _indPipeHover(card) {
  const grid = card.closest('.ind-pipe-grid');
  if (!grid) return;
  const { inputsOf, consumersOf } = _indPipeGraph;
  const self = new Set(_indCardTids(card));
  const feeds = new Set();    // downstream — what this is used for
  const fedBy = new Set();    // upstream — what goes into this
  self.forEach(tid => {
    (consumersOf[tid] || []).forEach(x => feeds.add(x));
    (inputsOf[tid] || []).forEach(x => fedBy.add(x));
  });
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
  const grid = e.target.closest('.ind-pipe-grid');
  if (grid) _indPipeClearHover(grid);
});
document.addEventListener('mouseout', e => {
  if (!e.target.closest) return;
  const grid = e.target.closest('.ind-pipe-grid');
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
  const tiersData = d.tree ? _indComputeTiers(d.tree) : { byType: {}, tiers: {}, maxT: 0 };
  const treeKids = d.tree && (d.tree.inputs || []).length
    ? (d.tree.inputs || []).map(c => _indTreeNode(c, 0)).join('') : '';
  const tree = treeKids
    ? `<details class="ind-details"><summary>Debug: full build tree</summary><div class="ind-tree">${treeKids}</div></details>` : '';
  return `<div class="pp-card">
    <h2 class="pp-card-title">${title}</h2>
    <div class="ind-body">
      ${_indMetricTiles(d.metrics)}
      ${unres}
      ${_indStepsHtml(d)}
      ${_indPipelineHtml(d, tiersData)}
      <details class="ind-details" open><summary>Shopping list (${(d.shopping_list || []).length})</summary>${_indShoppingSections(d, tiersData)}</details>
      ${tree}
      ${leftovers}
    </div>
  </div>`;
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
      body: JSON.stringify({ product_type_id: _indPicked.type_id, quantity: qty }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.detail || 'Could not queue'); return; }
    indLoadQueue();
    indLoadInstall();
  } catch (e) { alert(String(e)); }
}

async function indLoadQueue() {
  const el = document.getElementById('indQueueList');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/orders');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    if (!d.orders || !d.orders.length) { el.innerHTML = '<p class="pp-sub">Queue is empty. Search a product above and hit “+ Queue”.</p>'; document.getElementById('indQueuePlanBtn').disabled = true; return; }
    document.getElementById('indQueuePlanBtn').disabled = false;
    el.innerHTML = d.orders.map(o =>
      `<div class="ind-queue-row"><span class="ind-queue-name">${_esc(o.name)}</span>`
      + `<span class="ind-queue-qty">×${o.quantity}</span>`
      + `<button class="ind-queue-del" title="Remove" onclick="indRemoveOrder(${o.id})">✕</button></div>`
    ).join('');
  } catch (e) { el.innerHTML = ''; }
}

async function indRemoveOrder(id) {
  try { await fetch('/api/industry/orders/' + id, { method: 'DELETE' }); } catch (e) {}
  indLoadQueue();
  indLoadInstall();
}

// ── "To install now" checklist + in-progress jobs ───────────────────────────────────────────
async function indRefreshJobs() {
  try { await fetch('/api/industry/jobs/refresh', { method: 'POST' }); } catch (e) {}
  indLoadSlots();
  indLoadInstall();
  indLoadRunning();
}

async function indLoadInstall() {
  const el = document.getElementById('indInstall');
  if (!el) return;
  try {
    const r = await fetch('/api/industry/to-install');
    if (!r.ok) { el.innerHTML = ''; return; }
    const d = await r.json();
    if (d.empty || !d.ready || !d.ready.length) { el.innerHTML = ''; return; }
    const rows = d.ready.map(t => {
      const fit = t.fits_now ? '<span class="ind-fit-yes">slot free</span>' : '<span class="ind-fit-no">wait for a slot</span>';
      return `<div class="ind-install-row"><span class="ind-tree-name">${_esc(t.name)}</span> `
        + `<span class="ind-tree-qty">×${t.runs}${t.activity === 'reaction' ? ' rx' : ''}</span> `
        + `<span class="ind-tree-cost">${_fmtHours(t.duration_hours)}</span> ${fit}</div>`;
    }).join('');
    el.innerHTML = `<h3 class="ind-install-title">Install now — ${d.fit_count} of ${d.ready.length} ready jobs fit your free slots`
      + `<span class="ind-install-free">${d.free.manufacturing} mfg · ${d.free.reaction} rx free</span></h3>`
      + rows
      + (d.later_waves ? `<div class="pp-sub ind-later">+${d.later_waves} more wave(s) unlock as these finish · full makespan ${_fmtHours(d.makespan_hours)}</div>` : '');
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

async function indPlanQueue() {
  const out = document.getElementById('indQueueResult');
  out.innerHTML = '<p class="pp-sub">Planning queue…</p>';
  try {
    const r = await fetch('/api/industry/queue-plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prioritize_speed: _indPrioSpeed(), ..._indFacilityBonus() }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); out.innerHTML = `<p class="pp-warn">${_esc(e.detail || 'Queue plan failed')}</p>`; return; }
    const d = await r.json();
    if (d.empty) { out.innerHTML = '<p class="pp-sub">Queue is empty.</p>'; return; }
    _indLastPlan = d;
    _indCacheNames(d);
    const heads = (d.targets || []).map(t => `${t.quantity}× ${_esc(t.name)}`).join(', ');
    out.innerHTML = _indRenderPlan(d, 'Whole queue: ' + heads).replace('<div class="pp-card">', '<div class="ind-inner-card">').replace(/<\/div>\s*$/, '</div>');
  } catch (e) { out.innerHTML = `<p class="pp-warn">${_esc(String(e))}</p>`; }
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
