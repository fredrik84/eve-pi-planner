// Dashboard tab — split out of planetary.js (2026-06-23) to keep feature files small.
// Overview + maintenance routine + spare-capacity + global rescan.
// Loaded as a separate <script> after planetary.js; all functions are global and resolve
// at call time. Shared state/util (_esc, _fmtIsk, _features, _featureActive, _ppCharsData,
// _isAdmin, switchTab) lives in planetary.js, which loads first.

async function onDashboardTabOpen() {
  await _loadFeatures();
  const el = document.getElementById('dashboardContent');
  if (el && !el.dataset.loaded) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading overview…</div>';
  try {
    const data = await (await fetch('/api/dashboard')).json();
    renderDashboard(data);
    if (el) el.dataset.loaded = '1';
  } catch (e) {
    if (el) el.innerHTML = '<section class="pp-card"><div class="pp-card-body"><div class="pp-empty">Failed to load dashboard.</div></div></section>';
  }
}

// Global rescan (header button): rescan each character one at a time via the PER-CHARACTER endpoint
// (small, fast requests), NOT one giant /refresh-all that re-fetches every planet in a single HTTP
// call. The bulk call made 90+ sequential ESI calls in one request for a real fleet — it timed out
// (so the whole rescan failed) and a mid-loop ESI hiccup silently skipped a toon. Per-char requests
// isolate failures (the rest still update) and show real progress. `_rescanning` is a module flag so
// the button shows "Rescanning…" (disabled) on any page and a second press is ignored.
let _rescanning = false;
let _dashCharIds = null;   // character_ids surfaced on the dashboard — set by renderDashboard; lets the
                           // rescan button hit only the toons in view (usually just the factory owners)
function _setRescanUI() {
  const b = document.getElementById('rescanBtn');
  if (b) { b.disabled = _rescanning; b.textContent = _rescanning ? 'Rescanning…' : 'Rescan'; }
}
async function rescanAll() {
  if (_rescanning) return;                 // already scanning → ignore repeat presses
  // On the dashboard, rescan only the characters it actually shows (a few factory owners). Other tabs
  // cover every real toon (skip dummies, the wallet-only viewer, and toons with a dead token).
  const onDash = localStorage.getItem('activeTab') === 'dashboard';
  const ids = (onDash && _dashCharIds && _dashCharIds.length)
    ? _dashCharIds.slice()
    : (_ppCharsData || []).filter(c => !c.is_dummy && !c.wallet_only && c.token_ok && c.character_id > 0)
                          .map(c => c.character_id);
  if (!ids.length) return;
  _rescanning = true; _setRescanUI();
  const b = document.getElementById('rescanBtn');
  let failed = 0;
  for (let i = 0; i < ids.length; i++) {
    if (b) b.textContent = `Rescanning ${i + 1}/${ids.length}…`;
    try {
      const r = await fetch(`/api/characters/${ids[i]}/refresh-planets`, { method: 'POST' });
      if (!r.ok) failed++;
    } catch (e) { failed++; }
  }
  _rescanning = false;
  if (typeof loadCharacters === 'function') await loadCharacters();   // refresh _ppCharsData + header
  if (typeof onDashboardTabOpen === 'function' && onDash) await onDashboardTabOpen();
  if (typeof renderAnalysis === 'function' && _analyzeSnaps.length) renderAnalysis();
  _setRescanUI();
  if (failed) alert(`${failed} of ${ids.length} character${ids.length !== 1 ? 's' : ''} could not be rescanned — usually an expired ESI token (red dot in Characters).`);
}

// Fold/unfold a dashboard card by clicking its title (state persisted in localStorage).
function _toggleDashFold(titleEl, key) {
  const body = titleEl.nextElementSibling;
  const caret = titleEl.querySelector('.dash-fold-caret');
  const collapsing = body.style.display !== 'none';
  body.style.display = collapsing ? 'none' : '';
  if (caret) caret.textContent = collapsing ? '▸' : '▾';
  localStorage.setItem(key, collapsing ? '1' : '0');
}

function _dashTile(val, lbl, cls) {
  return `<div class="an-stat"><div class="an-stat-val${cls ? ' ' + cls : ''}">${val}</div><div class="an-stat-lbl">${lbl}</div></div>`;
}


// "Up next" PI agenda (admin-gated PoC). The extractor and factory cadences are usually badly
// desynced (you might restart extractors several times before a factory refill), so a single-cycle
// line is misleading. Instead: a sorted list of the next maintenance tasks with countdown + the
// absolute clock time. All timing comes from the dashboard totals (no extra request).
function _renderTimelineCard(t) {
  const jobs = [
    { lbl: 'Restart extractors', due: t.restart_due_hours, loc: t.restart_due_loc },
    { lbl: 'Haul extractor P1',  due: t.empty_due_hours,   loc: t.empty_due_loc || t.empty_pads_loc },
    { lbl: 'Refill factories',   due: t.refill_due_hours,  loc: t.refill_due_loc || t.refill_factories_loc },
  ].filter(j => j.due != null && j.due >= 0).sort((a, b) => a.due - b.due);
  if (!jobs.length) return '';   // no live colony timing yet

  // Absolute clock time for a task `due` hours out (browser-local; Date is fine client-side).
  const clock = h => new Date(Date.now() + h * 3600 * 1000)
    .toLocaleString([], { weekday: 'long', hour: '2-digit', minute: '2-digit' });
  // Use _fmtDHM (rounds + keeps minutes) to match the Maintenance-routine card exactly — _fmtHours
  // floors and drops minutes past 12h/24h, which made the same job read 1 minute off between cards.
  const rel = h => (h < 0.05 ? 'now' : 'in ' + _fmtDHM(h));

  // "admin preview" only while it's still admin-gated (not yet rolled out to the public).
  const isPublic = !!(_features['timeline'] && _features['timeline'].enabled);
  const previewTag = isPublic ? '' : '<span class="tl-preview-tag">admin preview</span>';

  const next = jobs[0], rest = jobs.slice(1);
  const restHtml = rest.map(j => `
    <li class="tl-up-item">
      <span class="tl-up-lbl">${_esc(j.lbl)}${j.loc ? ` <span class="tl-up-loc">${_esc(j.loc)}</span>` : ''}</span>
      <span class="tl-up-when">${rel(j.due)} <span class="tl-up-clock">· ${clock(j.due)}</span></span>
    </li>`).join('');

  return `
    <section class="pp-card tl-card">
      <div class="pp-card-title">Up next
        <span class="pp-card-hint">— your next PI tasks</span>
        ${previewTag}
      </div>
      <div class="pp-card-body">
        <div class="tl-next">
          <div class="tl-next-task">${_esc(next.lbl)}${next.loc ? ` <span class="tl-next-loc">${_esc(next.loc)}</span>` : ''}</div>
          <div class="tl-next-when">${rel(next.due)} <span class="tl-next-clock">· ${clock(next.due)}</span></div>
        </div>
        ${rest.length ? `<ul class="tl-up-list">${restHtml}</ul>` : ''}
      </div>
    </section>`;
}

// Per-character mutes for the extractor-sync warning, kept client-side (low-stakes, and some
// accounts deliberately run a character on a different schedule).
let _syncMutes = (() => { try { return new Set(JSON.parse(localStorage.getItem('ppSyncMutes') || '[]')); } catch (e) { return new Set(); } })();
let _dashData = null;   // last dashboard payload, so mute toggles can re-render without a refetch
function _saveSyncMutes() { try { localStorage.setItem('ppSyncMutes', JSON.stringify([..._syncMutes])); } catch (e) {} }
function _toggleSyncMute(cid) { cid = String(cid); _syncMutes.has(cid) ? _syncMutes.delete(cid) : _syncMutes.add(cid); _saveSyncMutes(); if (_dashData) renderDashboard(_dashData); }
function _clearSyncMutes() { _syncMutes.clear(); _saveSyncMutes(); if (_dashData) renderDashboard(_dashData); }

// "Extractor schedule out of sync" card — only the gated feature, only non-muted offenders.
function _renderSyncWarn(data) {
  const sw = data.sync_warn;
  if (!_featureActive('schedule_sync') || !sw || !sw.off || !sw.off.length) return '';
  const visible = sw.off.filter(o => !_syncMutes.has(String(o.cid)));
  if (!visible.length) {
    return _syncMutes.size ? `<section class="pp-card dash-issues"><div class="pp-card-body"><div class="sync-muted-note">${_syncMutes.size} character${_syncMutes.size !== 1 ? 's' : ''} muted from the extractor-sync check. <a href="#" onclick="_clearSyncMutes();return false;">Unmute all</a></div></div></section>` : '';
  }
  const byChar = {};
  visible.forEach(o => { (byChar[o.cid] = byChar[o.cid] || { char: o.char, cid: o.cid, items: [] }).items.push(o); });
  const planet = loc => loc.split(' · ').slice(1).join(' · ') || loc;
  const cards = Object.values(byChar).map(c => `
    <div class="dash-issue dash-issue-warn">
      <div class="dash-issue-char">${_esc(c.char)}<button class="sync-mute-btn" onclick="_toggleSyncMute('${c.cid}')" title="Stop warning for this character (it's on a different schedule on purpose)">Mute</button></div>
      <ul class="dash-issue-items">${c.items.map(o => `<li class="dash-il-warn">${_esc(planet(o.loc))} runs a <b>${_fmtDHM(o.hours)}</b> program — the fleet uses <b>${_fmtDHM(sw.norm_hours)}</b></li>`).join('')}</ul>
    </div>`).join('');
  return `<section class="pp-card dash-issues">
      <div class="pp-card-title">Extractor schedule out of sync <span class="pp-card-hint">— ${visible.length} extractor${visible.length !== 1 ? 's' : ''} on a different program; they drift off your batch restart</span></div>
      <div class="pp-card-body">${cards}${_syncMutes.size ? `<div class="sync-muted-note">${_syncMutes.size} muted. <a href="#" onclick="_clearSyncMutes();return false;">Unmute all</a></div>` : ''}</div>
    </section>`;
}

function renderDashboard(data) {
  const el = document.getElementById('dashboardContent');
  if (!el) return;
  _dashData = data;
  if (!data || !data.logged_in) {
    el.innerHTML = `<section class="pp-card"><div class="pp-card-title">Dashboard</div>
      <div class="pp-card-body"><div class="pp-empty">Log in with ESI (the <b>Login</b> button, top right) to see your PI overview.</div></div></section>`;
    return;
  }
  _dashCharIds = Array.isArray(data.char_ids_in_view) ? data.char_ids_in_view : null;
  const t = data.totals || {}, facs = data.factories || [], top = data.top_pi;
  const runtime = (t.runtime_hours != null) ? _fmtHours(t.runtime_hours) : '—';
  // Units of the top finished product sitting in factory pads (sum of haul_units for that product —
  // pad snapshot + what's been made since the checkpoint). Shown even at 0 ("all hauled out") so the
  // count is always there; derived from factories so it survives top_pi being null on empty pads.
  const prodAgg = {};
  facs.forEach(f => {
    const g = prodAgg[f.product] || (prodAgg[f.product] = { name: f.product, tier: f.tier || 0, units: 0 });
    g.units += (f.haul_units || 0);
  });
  const topProd = Object.values(prodAgg).sort((a, b) => (b.tier - a.tier) || (b.units - a.units))[0] || null;
  const tiles = [
    _dashTile(runtime, 'Runtime left (soonest)'),
    _dashTile(_fmtIsk(t.pads_value || 0), (top && !(topProd && topProd.tier >= 2)) ? `In pads now · top ${_esc(top.name)}` : 'In pads now'),
    ...(topProd && topProd.tier >= 2 ? [_dashTile(topProd.units.toLocaleString(), `${_esc(topProd.name)} in pads`)] : []),
    _dashTile(_fmtIsk(t.current_run_value || 0), 'Run value (from current inputs)'),
    _dashTile(_fmtIsk(t.value_per_day || 0), 'Value / day'),
  ].join('');
  const rows = facs.length ? facs.map(f => {
    const cls = f.fill_pct >= 50 ? 'an-bar-ok' : f.fill_pct >= 20 ? 'an-bar-warn' : 'an-bar-bad';
    const empty = !f.hours_left;
    return `<div class="dash-fac-row">
      <div class="dash-fac-id"><div class="dash-fac-name">${_esc(f.product)}</div><div class="dash-fac-loc">${_esc(f.loc)}</div></div>
      <div class="an-bar-track"><div class="an-bar-fill ${cls}" style="width:${Math.max(2, f.fill_pct)}%"></div></div>
      <div class="dash-fac-pct">${f.fill_pct}%</div>
      <div class="dash-fac-time${empty ? ' dash-fac-empty' : ''}">${empty ? 'empty' : _fmtHours(f.hours_left)}</div>
      <div class="dash-fac-val" title="${(f.haul_units || 0).toLocaleString()} ${_esc(f.product)} ready to haul">${f.haul_value ? _fmtIsk(f.haul_value) : '–'}</div>
    </div>`;
  }).join('') : '<div class="pp-empty">No factory planets found. Deploy factories, then refresh on the Characters tab.</div>';
  // Warnings only, grouped per character — a card that appears solely when something's amiss.
  const issues = data.issues || [];
  const issuesHtml = issues.length ? `
    <section class="pp-card dash-issues">
      <div class="pp-card-title">Needs attention <span class="pp-card-hint">— ${issues.length} character${issues.length !== 1 ? 's' : ''} need a look</span></div>
      <div class="pp-card-body">${issues.map(c =>
        `<div class="dash-issue dash-issue-${c.severity === 'high' ? 'high' : 'warn'}">
          <div class="dash-issue-char">${_esc(c.char)}</div>
          <ul class="dash-issue-items">${c.items.map(i => `<li class="dash-il-${i.severity === 'high' ? 'high' : 'warn'}">${_esc(i.msg)}</li>`).join('')}</ul>
        </div>`).join('')}</div>
    </section>` : '';
  // Spare fleet capacity — a nudge to re-plan when you've added toons, have free planet slots, or
  // trained CCU/Interplanetary Consolidation since the plan was last saved.
  const ex = data.expansion || {};
  const exItems = [];
  if (ex.idle_chars && ex.idle_chars.length) {
    const names = ex.idle_chars.slice(0, 6).map(_esc).join(', ') + (ex.idle_chars.length > 6 ? ` +${ex.idle_chars.length - 6}` : '');
    exItems.push(`<li><b>${ex.idle_chars.length} idle character${ex.idle_chars.length !== 1 ? 's' : ''}</b> with no colonies — ${names}</li>`);
  }
  if (ex.free_slots >= 1) {
    const fc = ex.free_slot_chars || [];
    const detail = fc.slice(0, 5).map(c => `${_esc(c.name)} ${c.used}/${c.max}`).join(', ') + (fc.length > 5 ? ', …' : '');
    exItems.push(`<li><b>${ex.free_slots} free planet slot${ex.free_slots !== 1 ? 's' : ''}</b>${fc.length ? ` across ${fc.length} character${fc.length !== 1 ? 's' : ''} — ${detail}` : ''}</li>`);
  }
  if (ex.skills_grew && ex.skills_grew.length) {
    const names = ex.skills_grew.slice(0, 6).map(g => {
      const bits = []; if (g.ccu_up) bits.push(`CCU +${g.ccu_up}`); if (g.ic_up) bits.push(`planets +${g.ic_up}`);
      return `${_esc(g.name)} (${bits.join(', ')})`;
    }).join(', ');
    exItems.push(`<li><b>${ex.skills_grew.length} trained up</b> since you saved${ex.plan_name ? ` “${_esc(ex.plan_name)}”` : ''} — ${names}</li>`);
  }
  // Status only: the concrete "deploy this here" advice lives in Setup Analysis (which fetches
  // /api/expansion). The dashboard just surfaces that there's spare capacity and links to it.
  const expansionHtml = exItems.length ? `
    <section class="pp-card dash-expand">
      <div class="pp-card-title">Spare capacity <span class="pp-card-hint">— unused fleet you could grow into</span></div>
      <div class="pp-card-body">
        <ul class="dash-expand-list">${exItems.join('')}</ul>
        <div class="dash-expand-cta"><a href="#" onclick="switchTab('analyze');return false;">See Setup Analysis for what to deploy →</a></div>
        <div class="dash-expand-cta dash-expand-cta-sub">Prefer to start over? <a href="#" onclick="switchTab('planetary');return false;">Planetary Planning</a> rebuilds the whole layout from scratch.</div>
      </div>
    </section>` : '';
  // Maintenance routine — big number = countdown to NEXT due (from current state); small text = the
  // cadence ("every X") + the binding colony.
  const _rtTile = (dueH, cadenceH, lbl, loc) => {
    if (dueH == null && cadenceH == null)
      return `<div class="an-stat"><div class="an-stat-val">—</div><div class="an-stat-lbl">${lbl}</div></div>`;
    const big = dueH != null ? (dueH < 0.1 ? 'due now' : _fmtDHM(dueH)) : _fmtDHM(cadenceH);
    const bits = [];
    if (cadenceH != null) bits.push(`every ${_fmtDHM(cadenceH)}`);
    if (loc) bits.push(_esc(loc));
    const sub = bits.length ? `<span class="dash-rt-sub">${bits.join(' · ')}</span>` : '';
    return `<div class="an-stat"${loc ? ` title="${_esc(loc)} is next"` : ''}><div class="an-stat-val">${big}</div><div class="an-stat-lbl">${lbl}${sub}</div></div>`;
  };
  // PI process timeline (admin-gated PoC): one account-level "you are here" line from
  // "extractors started" to the next maintenance jobs, using the same due/cadence the routine card has.
  const timelineHtml = _featureActive('timeline') ? _renderTimelineCard(t) : '';

  const routineHtml = (t.empty_pads_hours != null || t.refill_factories_hours != null || t.restart_extractors_hours != null) ? `
    <section class="pp-card">
      <div class="pp-card-title">Maintenance routine <span class="pp-card-hint">— countdown to the next job · cadence below</span></div>
      <div class="pp-card-body"><div class="an-stats">
        ${_rtTile(t.restart_due_hours, t.restart_extractors_hours, 'Restart extractors', t.restart_due_loc)}
        ${_rtTile(t.empty_due_hours, t.empty_pads_hours, 'Empty extractor pads', t.empty_due_loc || t.empty_pads_loc)}
        ${_rtTile(t.refill_due_hours, t.refill_factories_hours, 'Refill factory inputs', t.refill_due_loc || t.refill_factories_loc)}
      </div></div>
    </section>` : '';

  // What's actually in the launchpads — finished product (to sell) and raw P1 (to haul to factories).
  const pb = data.pads_breakdown || { product: [], raw: [] };
  const _padRows = (items, showM3) => items.map(it =>
    `<div class="dash-pad-row"><span class="dash-pad-amt">${it.amount.toLocaleString()}</span><span class="dash-pad-name">${_esc(it.name)}</span><span class="dash-pad-meta">${showM3 ? it.m3.toLocaleString() + ' m³' : _fmtIsk(it.value)}</span></div>`).join('');
  const prodTot = pb.product.reduce((a, x) => a + x.value, 0);
  const rawM3 = pb.raw.reduce((a, x) => a + x.m3, 0);
  const rawShown = pb.raw.slice(0, 12);
  const padsCollapsed = localStorage.getItem('dashPadsCollapsed') !== '0';   // default folded
  const padsSummary = `${pb.product.length ? _fmtIsk(prodTot) + ' to sell' : ''}${pb.product.length && pb.raw.length ? ' · ' : ''}${pb.raw.length ? Math.round(rawM3).toLocaleString() + ' m³ to haul' : ''}`;
  const padsHtml = (pb.product.length || pb.raw.length) ? `
    <section class="pp-card">
      <div class="pp-card-title dash-fold" onclick="_toggleDashFold(this, 'dashPadsCollapsed')">
        <span class="dash-fold-caret">${padsCollapsed ? '▸' : '▾'}</span> In the pads
        <span class="pp-card-hint">— ${padsCollapsed ? _esc(padsSummary) : "what's in your launchpads now"}</span>
      </div>
      <div class="pp-card-body"${padsCollapsed ? ' style="display:none"' : ''}>
        ${pb.product.length ? `<div class="dash-pad-grp"><div class="dash-pad-grp-h">Finished product <span class="dash-pad-grp-sub">ready to sell · ${_fmtIsk(prodTot)}</span></div>${_padRows(pb.product, false)}</div>` : ''}
        ${pb.raw.length ? `<div class="dash-pad-grp"><div class="dash-pad-grp-h">Raw P1 in extractors <span class="dash-pad-grp-sub">haul to factories · ${Math.round(rawM3).toLocaleString()} m³</span></div>${_padRows(rawShown, true)}${pb.raw.length > rawShown.length ? `<div class="dash-pad-more">+ ${pb.raw.length - rawShown.length} more</div>` : ''}</div>` : ''}
      </div>
    </section>` : '';
  el.innerHTML = _renderSyncWarn(data) + issuesHtml + expansionHtml + `
    <section class="pp-card">
      <div class="pp-card-title">Overview <span class="pp-card-hint">— your PI at a glance · Rescan in the top bar pulls fresh data</span></div>
      <div class="pp-card-body"><div class="an-stats">${tiles}</div></div>
    </section>` + routineHtml + timelineHtml + padsHtml + `
    <section class="pp-card">
      <div class="pp-card-title">Factories <span class="pp-card-hint">— launchpad fill &amp; time to empty, projected forward from your last rescan (${facs.length})</span></div>
      <div class="pp-card-body">
        ${facs.length ? '<div class="dash-fac-head"><span>Factory</span><span>Fill</span><span>%</span><span>Runs out</span><span>To haul</span></div>' : ''}
        <div class="dash-fac-list">${rows}</div>
      </div>
    </section>`;
}
