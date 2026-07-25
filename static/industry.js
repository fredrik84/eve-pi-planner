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
  indPopulateFacility();
  indLoadSetupSummary();
  indLoadLifetime();
  indLoadQueue();
  indLoadInstall();
  indLoadRunning();
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
  { id: 'none', label: 'NPC station — no bonus', me: 0, te: 0 },
  { id: 't1_me', label: 'Structure + T1 ME rig — ME 3% / TE 15%', me: 3, te: 15 },
  { id: 't1_te', label: 'Structure + T1 TE rig — ME 1% / TE 34%', me: 1, te: 34 },
  { id: 't2_me_null', label: 'Structure + T2 ME rig, null/WH — ME 6% / TE 15%', me: 6, te: 15 },
  { id: 't2_te_null', label: 'Structure + T2 TE rig, null/WH — ME 1% / TE 44%', me: 1, te: 44 },
];
function indPopulateFacility() {
  const sel = document.getElementById('indFacility');
  if (!sel || sel.options.length) return;
  sel.innerHTML = IND_FACILITIES.map(f => `<option value="${f.id}">${_esc(f.label)}</option>`).join('');
  const saved = localStorage.getItem('indFacility');
  if (saved && IND_FACILITIES.some(f => f.id === saved)) sel.value = saved;
}
function _indFacilityBonus() {
  const sel = document.getElementById('indFacility');
  const f = IND_FACILITIES.find(x => x.id === (sel ? sel.value : 'none')) || IND_FACILITIES[0];
  return { struct_material_pct: f.me, struct_time_pct: f.te };
}
function indOnFacilityChange() {
  const sel = document.getElementById('indFacility');
  if (sel) { try { localStorage.setItem('indFacility', sel.value); } catch (e) {} }
  if (_indPicked && document.getElementById('indResult').innerHTML.trim()) indRunPlan();
}

let _indLastPlan = null;   // last rendered plan, for the shopping-list copy features

function _indShoppingTable(list) {
  if (!list || !list.length) return '<p class="pp-sub">Nothing to buy — built entirely from stock/recipes.</p>';
  const rows = list.map(s =>
    `<tr><td>${_esc(s.name)}`
    + `${s.bought_for_speed ? ' <span class="ind-speed-badge" title="Bought instead of built to save time">for speed</span>' : ''}`
    + `${s.bought_marginal ? ' <span class="ind-marginal-badge" title="Building this would save too little to be worth a job">low saving</span>' : ''}</td>`
    + `<td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`
  ).join('');
  return `<div class="ind-shop-bar"><button class="ind-copy-btn" onclick="indCopyMultibuy()">Copy for EVE Multibuy</button>`
    + `<span class="ind-shop-tot">${list.length} items · ${fmtIsk(list.reduce((a, s) => a + (s.line_cost || 0), 0))}</span></div>`
    + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// Copy the shopping list in EVE's Multibuy paste format ("Item Name<tab>qty" per line) so the whole
// buy can be pasted straight into the in-game Multibuy window.
function indCopyMultibuy() {
  const list = (_indLastPlan && _indLastPlan.shopping_list) || [];
  if (!list.length) return;
  const text = list.map(s => `${s.name}\t${Math.ceil(s.qty)}`).join('\n');
  const btn = event && event.target;
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = t; }, 1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => {});
  else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} document.body.removeChild(ta); }
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

// The build as a horizontal PRODUCTION PIPELINE: raw materials on the left flow rightward through
// each build stage to the finished product. Each type appears once (at its deepest stage), coloured
// by build vs buy. An assembly line rather than an indented list — reads the way EVE industry works.
function _indPipelineHtml(d) {
  const tree = d.tree;
  if (!tree || !(tree.inputs || []).length) return '';
  const byType = {};
  (function walk(n, depth) {
    if (!n) return;
    const e = byType[n.type_id] || (byType[n.type_id] = { name: n.name, decision: n.decision, activity: n.activity, owned: n.owned, qty: 0, tier: depth });
    e.qty += n.qty || 0;
    e.tier = Math.max(e.tier, depth);
    if (n.decision === 'build' || n.decision === 'buy') e.decision = n.decision;
    (n.inputs || []).forEach(c => walk(c, depth + 1));
  })(tree, 0);

  const tiers = {};
  Object.values(byType).forEach(e => (tiers[e.tier] = tiers[e.tier] || []).push(e));
  const maxT = Math.max(...Object.keys(tiers).map(Number));
  // A build step gets its own card; bought items aren't steps, so they collapse into one card.
  const buildCard = e => {
    const owned = e.owned ? `<span class="ind-owned" title="You own this ${e.owned.kind.toUpperCase()}">${e.owned.kind.toUpperCase()}</span>` : '';
    return `<div class="ind-pipe-card ind-pipe-build"><span class="ind-pipe-name">${_esc(e.name)}</span>`
      + `<span class="ind-pipe-meta">×${Math.round(e.qty).toLocaleString()} <span class="ind-pipe-tag ind-t-build">build${e.activity === 'reaction' ? ' rx' : ''}</span>${owned}</span></div>`;
  };
  const buyCard = buys => {
    const names = buys.slice(0, 25).map(b => b.name).join(', ') + (buys.length > 25 ? '…' : '');
    return `<div class="ind-pipe-card ind-pipe-buys" title="${_esc(names)}"><span class="ind-pipe-name">Buy ${buys.length} material${buys.length > 1 ? 's' : ''}</span>`
      + `<span class="ind-pipe-meta">in shopping list ↓</span></div>`;
  };
  let cols = '';
  for (let t = maxT; t >= 0; t--) {
    const items = tiers[t] || [];
    const builds = items.filter(e => e.decision === 'build').sort((a, b) => (b.qty || 0) - (a.qty || 0));
    const buys = items.filter(e => e.decision !== 'build');
    if (!builds.length && !buys.length) continue;
    const shown = builds.slice(0, 12);
    let cards = shown.map(buildCard).join('');
    if (builds.length > 12) cards += `<div class="ind-pipe-more">+${builds.length - 12} more built</div>`;
    if (buys.length) cards += buyCard(buys);
    const label = t === 0 ? 'Finished' : (!builds.length ? 'Buy' : `Stage ${maxT - t}`);
    const count = builds.length ? `<span>${builds.length}</span>` : '';
    cols += `<div class="ind-pipe-col${t === 0 ? ' ind-pipe-final' : ''}"><div class="ind-pipe-hd">${label}${count}</div>`
      + `<div class="ind-pipe-items">${cards}</div></div>`;
    if (t > 0) cols += '<div class="ind-pipe-arrow">›</div>';
  }
  return `<details class="ind-details" open><summary>Build pipeline</summary><div class="ind-pipe">${cols}</div></details>`;
}

function _indRenderPlan(d, title) {
  const unres = (d.unresolved && d.unresolved.length)
    ? `<p class="pp-warn">${d.unresolved.length} material(s) had no market price — cost is a floor.</p>` : '';
  const sched = d.schedule ? _indScheduleHtml(d.schedule) : '';
  const leftovers = (d.leftovers && d.leftovers.length)
    ? `<details class="ind-details"><summary>Reusable leftovers (${d.leftovers.length}) — ${fmtIsk(d.metrics.leftover_value || 0)} credited</summary>`
      + d.leftovers.map(l => `<div class="ind-tree-row"><span class="ind-tree-name">${_esc(l.name)}</span> `
        + `<span class="ind-tree-qty">×${Math.round(l.qty).toLocaleString()}</span>`
        + (l.value ? `<span class="ind-tree-cost">${fmtIsk(l.value)}</span>` : '') + `</div>`).join('') + `</details>` : '';
  const treeKids = d.tree && (d.tree.inputs || []).length
    ? (d.tree.inputs || []).map(c => _indTreeNode(c, 0)).join('') : '';
  const tree = treeKids
    ? `<details class="ind-details"><summary>Build tree (list)</summary><div class="ind-tree">${treeKids}</div></details>` : '';
  return `<div class="pp-card">
    <h2 class="pp-card-title">${title}</h2>
    <div class="ind-body">
      ${_indMetricTiles(d.metrics)}
      ${unres}
      ${_indStepsHtml(d)}
      ${_indPipelineHtml(d)}
      <details class="ind-details" open><summary>Shopping list (${(d.shopping_list || []).length})</summary>${_indShoppingTable(d.shopping_list)}</details>
      ${sched}
      ${tree}
      ${leftovers}
    </div>
  </div>`;
}

function _indScheduleHtml(s) {
  if (!s || !s.waves || !s.waves.length) return '';
  const waves = s.waves.map(w =>
    `<div class="ind-wave"><div class="ind-wave-hd">+${_fmtHours(w.start_hours)}</div><div class="ind-wave-jobs">${_indJobChips(_indWaveGroup(w))}</div></div>`
  ).join('');
  return `<details class="ind-details"><summary>Full schedule — ${s.waves.length} wave(s), makespan ${_fmtHours(s.makespan_hours)}</summary><div class="ind-waves">${waves}</div></details>`;
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
