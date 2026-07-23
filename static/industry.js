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
  indLoadSlots();
  indLoadBlueprints();
  indLoadQueue();
  indLoadInstall();
  indLoadRunning();
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
      body: JSON.stringify({ type_id: _indPicked.type_id, quantity: qty }),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(e.detail || 'Plan failed')}</p></div>`; return; }
    const d = await r.json();
    out.innerHTML = _indRenderPlan(d, `Build ${qty}× ${_esc(d.target.name)}`);
  } catch (e) {
    out.innerHTML = `<div class="pp-card"><p class="pp-warn">${_esc(String(e))}</p></div>`;
  }
}

function _indMetricTiles(m) {
  const tiles = [
    ['Total cost', fmtIsk(m.total_cost)],
    ['Materials', fmtIsk(m.materials_cost)],
    ['Job fees', fmtIsk(m.job_cost)],
    ['Jobs', m.job_count],
  ];
  if (m.makespan_hours != null) tiles.push(['Makespan', _fmtHours(m.makespan_hours)]);
  else if (m.total_job_hours != null) tiles.push(['Total job time', _fmtHours(m.total_job_hours)]);
  return `<div class="an-stats">` + tiles.map(([l, v]) =>
    `<div class="an-stat"><div class="an-stat-lbl">${l}</div><div class="an-stat-val">${v}</div></div>`).join('') + `</div>`;
}

function _indShoppingTable(list) {
  if (!list || !list.length) return '<p class="pp-sub">Nothing to buy — built entirely from stock/recipes.</p>';
  const rows = list.map(s =>
    `<tr><td>${_esc(s.name)}</td><td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`
  ).join('');
  return `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function _indTreeNode(n, depth) {
  const pad = depth * 14;
  const badge = n.decision === 'build'
    ? `<span class="ind-badge ind-build">build${n.activity === 'reaction' ? ' (rx)' : ''}${n.runs ? ' ×' + n.runs : ''}</span>`
    : n.decision === 'buy' ? '<span class="ind-badge ind-buy">buy</span>'
    : '<span class="ind-badge ind-unres">no price</span>';
  const cost = n.unit_cost != null ? fmtIsk((n.unit_cost || 0) * (n.qty || 0)) : '';
  const owned = n.owned
    ? `<span class="ind-owned" title="You own this ${n.owned.kind.toUpperCase()} (ME${n.owned.me}/TE${n.owned.te})">${n.owned.kind.toUpperCase()} ME${n.owned.me}</span>` : '';
  let html = `<div class="ind-tree-row" style="padding-left:${pad}px">`
    + `<span class="ind-tree-name">${_esc(n.name)}</span> `
    + `<span class="ind-tree-qty">×${Math.round(n.qty).toLocaleString()}</span> ${badge} ${owned}`
    + (cost ? `<span class="ind-tree-cost">${cost}</span>` : '')
    + `</div>`;
  (n.inputs || []).forEach(c => { html += _indTreeNode(c, depth + 1); });
  return html;
}

function _indRenderPlan(d, title) {
  const unres = (d.unresolved && d.unresolved.length)
    ? `<p class="pp-warn">${d.unresolved.length} material(s) had no market price — cost is a floor.</p>` : '';
  const sched = d.schedule ? _indScheduleHtml(d.schedule) : '';
  const leftovers = (d.leftovers && d.leftovers.length)
    ? `<details class="ind-details"><summary>Leftover output (${d.leftovers.length})</summary>`
      + d.leftovers.map(l => `<div class="ind-tree-row">${_esc(l.name)} ×${Math.round(l.qty).toLocaleString()}</div>`).join('') + `</details>` : '';
  const tree = d.tree ? `<details class="ind-details" open><summary>Build tree</summary><div class="ind-tree">${_indTreeNode(d.tree, 0)}</div></details>` : '';
  return `<div class="pp-card">
    <h3 class="ind-res-title">${title}</h3>
    ${_indMetricTiles(d.metrics)}
    ${unres}
    ${sched}
    <details class="ind-details" open><summary>Shopping list (${(d.shopping_list || []).length})</summary>${_indShoppingTable(d.shopping_list)}</details>
    ${tree}
    ${leftovers}
  </div>`;
}

function _indScheduleHtml(s) {
  if (!s || !s.waves || !s.waves.length) return '';
  const waves = s.waves.map((w, i) => {
    const jobs = w.tasks.map(t =>
      `<span class="ind-wave-job">${_esc(t.name || _indName(t.type_id))} ×${t.runs}${t.activity === 'reaction' ? ' (rx)' : ''} · ${_fmtHours(t.duration_hours)}</span>`
    ).join('');
    return `<div class="ind-wave"><div class="ind-wave-hd">+${_fmtHours(w.start_hours)}</div><div class="ind-wave-jobs">${jobs}</div></div>`;
  }).join('');
  return `<details class="ind-details" open><summary>Schedule — ${s.waves.length} wave(s), makespan ${_fmtHours(s.makespan_hours)}</summary><div class="ind-waves">${waves}</div></details>`;
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
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
    });
    if (!r.ok) { const e = await r.json().catch(() => ({})); out.innerHTML = `<p class="pp-warn">${_esc(e.detail || 'Queue plan failed')}</p>`; return; }
    const d = await r.json();
    if (d.empty) { out.innerHTML = '<p class="pp-sub">Queue is empty.</p>'; return; }
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
    document.getElementById('indQty').addEventListener('input', () => {});
  }
});
