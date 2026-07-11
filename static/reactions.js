// Reactions tab (B0SS moon-goo profitability ranking, alliance-gated). Fetches the ranked
// opportunity list and renders a client-sortable table — this is advice, not an optimizer, so
// every dimension (profit, steps, liquidity) is shown un-collapsed for the viewer to weigh.

let _rxOpps = [];
let _rxSortKey = 'net_profit_instant';
let _rxSortDir = -1; // -1 = descending

const _RX_COLUMNS = [
  { key: 'name',                 label: 'Product',        fmt: (v, o) =>
      `<img src="https://images.evetech.net/types/${o.type_id}/icon?size=32" alt="" style="width:18px;height:18px;border-radius:3px;vertical-align:middle;margin-right:5px" onerror="this.style.display='none'">${_esc(v)}` },
  { key: 'steps',                label: 'Steps',          fmt: v => String(v) },
  { key: 'output_qty',           label: 'Max output',     fmt: v => Math.round(v).toLocaleString() },
  { key: 'input_cost',           label: 'Input cost',     fmt: v => _fmtIsk(v) },
  { key: 'shipping_cost',        label: 'Ship+collateral', fmt: (v, o) => _fmtIsk(v + o.collateral_cost) },
  { key: 'instant_sell_value',   label: 'Instant sell',   fmt: v => _fmtIsk(v) },
  { key: 'sell_order_value',     label: 'Sell order',     fmt: v => _fmtIsk(v) },
  { key: 'net_profit_instant',   label: 'Profit (instant)', fmt: v => _fmtIsk(v) },
  { key: 'net_profit_order',     label: 'Profit (order)', fmt: v => _fmtIsk(v) },
  { key: 'profit_per_m3_instant', label: 'ISK/m³',        fmt: v => v == null ? '—' : Math.round(v).toLocaleString() },
  { key: 'buy_volume',           label: 'Buy depth',      fmt: v => Math.round(v).toLocaleString() },
  { key: 'sell_volume',          label: 'Sell depth',     fmt: v => Math.round(v).toLocaleString() },
];

function onReactionsTabOpen() {
  const el = document.getElementById('reactionsContent');
  if (el) {
    el.innerHTML = '<div class="pp-empty">Loading…</div>';
    fetch('/api/reactions/opportunities')
      .then(r => {
        if (!r.ok) throw new Error(r.status === 403 ? 'B0SS alliance membership required' : 'Load failed');
        return r.json();
      })
      .then(data => {
        _rxOpps = data.opportunities || [];
        _renderReactions();
      })
      .catch(err => {
        el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
      });
  }
  _loadReactionsDashboard();
  _loadRxShoppingList();
}

let _rxLastShoppingList = [];

function _loadRxShoppingList() {
  const card = document.getElementById('rxShoppingCard');
  const el = document.getElementById('rxShoppingListContent');
  if (!card || !el) return;
  fetch('/api/reactions/shopping-list')
    .then(r => r.ok ? r.json() : { materials: [] })
    .then(d => {
      _rxLastShoppingList = d.materials || [];
      if (!_rxLastShoppingList.length) { card.style.display = 'none'; return; }
      card.style.display = '';
      const goo = _rxLastShoppingList.filter(m => m.is_moon_goo);
      const other = _rxLastShoppingList.filter(m => !m.is_moon_goo);
      const section = (title, items) => !items.length ? '' : `
        <div class="rx-shop-sec-title">${title}</div>
        <div style="overflow-x:auto">
          <table class="pp-card-table" style="width:100%">
            <thead><tr><th>Material</th><th>Quantity</th></tr></thead>
            <tbody>${items.map(m => `<tr><td>${_esc(m.name)}</td><td>${m.quantity.toLocaleString()}</td></tr>`).join('')}</tbody>
          </table>
        </div>`;
      el.innerHTML = section('Moon materials (buy from the alliance)', goo)
        + section('Other purchased materials (fuel blocks etc.)', other);
    })
    .catch(() => { card.style.display = 'none'; });
}

function _rxCopyShoppingList(btn) {
  const text = _rxLastShoppingList.map(m => `${m.name}\t${m.quantity}`).join('\n');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}

// Best-effort name lookup via whatever the opportunity list has already loaded — falls back to
// the raw type_id (no dedicated type-name endpoint is worth adding just for this display).
function _rxProductName(type_id) {
  const hit = _rxOpps.find(o => o.type_id === type_id);
  return hit ? hit.name : `#${type_id}`;
}

let _rxLastDashboardData = null;

function _loadReactionsDashboard() {
  const el = document.getElementById('rxDashboardContent');
  if (!el) return;
  // Only show the loading flash on a genuinely first/cold load — a refresh after cancelling one
  // assignment updates the cached data in place instead (see _rxCancelAssignment), so this
  // full-reload path only runs on tab-open or after "Clear all", not on every small action.
  if (!_rxLastDashboardData) el.innerHTML = '<div class="pp-empty">Loading…</div>';
  fetch('/api/reactions/jobs')
    .then(r => {
      if (!r.ok) throw new Error(r.status === 403 ? 'B0SS alliance membership required' : 'Load failed');
      return r.json();
    })
    .then(data => { _rxLastDashboardData = data; _renderReactionsDashboard(data); })
    .catch(err => {
      el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
}

function _renderReactionsDashboard(data) {
  const el = document.getElementById('rxDashboardContent');
  const metricsEl = document.getElementById('rxMetricsContent');
  if (!el) return;

  // Connecting a character (via /auth/login?reactions=1) is a per-character ESI authorisation,
  // so this must stay reachable even after some characters are already tracked — the account
  // may have several characters (own alts, or characters logged in from other EVE accounts
  // sharing this context) that each need to opt in separately. Lives as a persistent header
  // button (#rxConnectBtn, next to "Add Reaction Product") rather than buried in the dashboard
  // body, since it was easy to miss there.
  const connectBtn = document.getElementById('rxConnectBtn');
  if (connectBtn) {
    connectBtn.style.display = '';
    connectBtn.textContent = `Connect ${data.tracked ? 'another' : 'a'} character`;
  }

  if (!data.tracked) {
    el.innerHTML = '<div class="pp-empty">No characters are tracking reaction jobs yet — use "Connect a character" above.</div>';
    if (metricsEl) metricsEl.innerHTML = '<div class="pp-empty">Nothing to show yet.</div>';
    return;
  }

  // Hide characters with no real Reactions skill investment — slots==1 is just the base slot
  // everyone has (1 + mass_reactions + advanced_mass_reactions), so a character sitting at 1
  // hasn't trained anything and can't meaningfully react. Applies to both the "not yet tracked"
  // nudge list and the loadout itself — no point highlighting a character that can't do this.
  const HAS_SKILL = c => c.slots > 1;
  const untracked = (data.characters || []).filter(c => !c.tracked && HAS_SKILL(c))
    .sort((a, b) => b.slots - a.slots);
  const untrackedNote = untracked.length
    ? `<div class="pp-card-hint" style="margin-bottom:8px">Not yet tracked: ${untracked.map(c => _esc(c.character_name)).join(', ')}</div>`
    : '';

  // "Loadout screen" per character: one square per reaction slot, occupied ones showing the
  // product's icon + a countdown, empty ones dashed — so an idle tracked character (no jobs
  // running) still shows up as a row of empty slots, not just an aggregate free-slot count.
  // Sorted by total slots descending — your most-trained (most useful) characters lead.
  const tracked = (data.characters || []).filter(c => c.tracked && HAS_SKILL(c))
    .sort((a, b) => b.slots - a.slots);
  if (!tracked.length) {
    el.innerHTML = '<div class="pp-empty">No tracked characters have trained Mass Reactions skills yet — a bare base slot isn\'t enough to be worth showing.</div>' + untrackedNote;
    if (metricsEl) metricsEl.innerHTML = '<div class="pp-empty">Nothing to show yet.</div>';
    return;
  }
  const jobsByChar = new Map();
  for (const j of data.running) {
    if (!jobsByChar.has(j.character_name)) jobsByChar.set(j.character_name, []);
    jobsByChar.get(j.character_name).push(j);
  }

  // Pending assignments are one row per actual in-game job slot (see assign_reaction), so a
  // single big suggestion can produce a dozen identical-looking rows — group them back into ONE
  // todo line per (character, product, runs) with a "×N jobs" count instead of repeating the
  // same instruction over and over.
  const todoGroups = new Map();
  const rows = tracked.map(c => {
    const jobs = jobsByChar.get(c.character_name) || [];
    const pending = c.pending || [];
    const squares = jobs.map(j => {
      const icon = `https://images.evetech.net/types/${j.product_type_id}/icon?size=32`;
      const timer = j.hours_left != null ? _fmtHours(j.hours_left) : '—';
      const tip = `${_rxProductName(j.product_type_id)} — finished in ${timer}${j.facility_name ? ' — ' + j.facility_name : ''}`;
      return `
        <div class="rx-slot rx-slot-filled" title="${_esc(tip)}">
          <img class="rx-slot-icon" src="${icon}" alt="" onerror="this.style.visibility='hidden'">
          <div class="rx-slot-timer">${_esc(timer)}</div>
        </div>`;
    });
    // Assigned (via the wizard's "Assign") but ESI hasn't confirmed it's actually running yet —
    // a red slashed-circle "you need to go do this" slot, distinct from a genuinely free one.
    // Click to cancel the assignment (e.g. changed your mind, or already did it under a
    // different product than planned).
    for (const a of pending) {
      const key = `${c.character_name} ${a.name} ${a.runs}`;
      if (!todoGroups.has(key)) {
        todoGroups.set(key, { character_name: c.character_name, name: a.name, runs: a.runs, count: 0, ids: [] });
      }
      const g = todoGroups.get(key);
      g.count++;
      g.ids.push(a.assignment_id);
      const pendingIcon = `https://images.evetech.net/types/${a.type_id}/icon?size=32`;
      squares.push(`
        <div class="rx-slot rx-slot-pending" title="Not running yet — install ${_esc(a.name)} ×${a.runs} in-game. Click to cancel this assignment." onclick="_rxCancelAssignment(${a.assignment_id})">
          <img class="rx-slot-icon" src="${pendingIcon}" alt="" onerror="this.style.visibility='hidden'">
          <span class="rx-slot-pending-badge">⊘</span>
          <div class="rx-slot-pending-label">${_esc(a.name)}</div>
        </div>`);
    }
    for (let i = jobs.length + pending.length; i < c.slots; i++) {
      squares.push('<div class="rx-slot rx-slot-empty" title="Free reaction slot"><span class="rx-slot-empty-mark">+</span></div>');
    }
    return `
      <div class="rx-char-row">
        <div class="rx-char-label">${_esc(c.character_name)}<br><span class="pp-card-hint">${c.free_slots} / ${c.slots} free</span></div>
        <div class="rx-slot-row">${squares.join('')}</div>
      </div>`;
  }).join('');

  const todoNote = todoGroups.size
    ? `<div class="rx-todo-list">${[...todoGroups.values()].map(g =>
        `<div class="rx-todo-item"><span class="rx-todo-x">⊘</span> Install <b>${_esc(g.name)}</b> ×${g.runs}${g.count > 1 ? ` (×${g.count} jobs)` : ''} on <b>${_esc(g.character_name)}</b></div>`
      ).join('')}</div>`
    : '';

  // Easy at-a-glance numbers, first thing on the page — same big-number-tile pattern as the PI
  // Dashboard's own "Overview" row (_dashTile, dashboard.js), not a small text line buried below
  // the character loadout where it's easy to miss.
  const pendingCount = [...todoGroups.values()].reduce((sum, g) => sum + g.count, 0);
  // Soonest-finishing running job — data.running is already sorted ascending by hours_left
  // (see get_industry_jobs), so the first entry is "the next thing that'll need attention."
  const soonest = (data.running || []).find(r => r.hours_left != null);
  const timeLeftVal = soonest ? _fmtHours(soonest.hours_left) : '—';
  const timeLeftLbl = soonest ? `Time left · ${_rxProductName(soonest.product_type_id)}` : 'Time left';

  const usedSlots = data.total_slots - data.free_slots;
  const overviewTiles = `<div class="an-stats">
      ${_dashTile(_fmtIsk(data.pending_isk_committed), 'ISK committed')}
      ${_dashTile('+' + _fmtIsk(data.pending_net_profit), 'Expected profit', 'an-ok')}
      ${_dashTile(`${usedSlots}<span class="an-of"> / ${data.total_slots}</span>`, 'Slots used')}
      ${_dashTile(String(pendingCount), 'Jobs to install', pendingCount > 0 ? 'an-warn' : '')}
      ${_dashTile(timeLeftVal, timeLeftLbl)}
    </div>`;

  if (metricsEl) metricsEl.innerHTML = overviewTiles;
  el.innerHTML = rows + todoNote + untrackedNote;
}

function _rxCancelAssignment(assignmentId) {
  fetch(`/api/reactions/assign/${assignmentId}`, { method: 'DELETE' })
    .then(r => {
      if (!r.ok || !_rxLastDashboardData) return;
      // Optimistic in-place update instead of a full refetch+re-render — a full reload here
      // was visibly flickery/slow for something as small as clearing one pending slot.
      for (const c of _rxLastDashboardData.characters || []) {
        const before = (c.pending || []).length;
        c.pending = (c.pending || []).filter(p => p.assignment_id !== assignmentId);
        if (c.pending.length !== before) { c.free_slots++; _rxLastDashboardData.free_slots++; break; }
      }
      _renderReactionsDashboard(_rxLastDashboardData);
    });
}

function _rxClearAllAssignments() {
  fetch('/api/reactions/assign', { method: 'DELETE' })
    .then(r => { if (r.ok) { _rxLastDashboardData = null; _loadReactionsDashboard(); } });
}

// ── Wizard: "Add Reaction Product" ──────────────────────────────────────────────────────────
let _rxMaterials = []; // [{type_id, name}] — loaded once, reused across wizard opens

function wizRStart() {
  document.getElementById('rxDashboard').style.display = 'none';
  document.getElementById('rxWizard').style.display = '';
  wizRGo(1);
  _wizRIskLive();
  _loadRxMaterialFilter();
}

function _loadRxMaterialFilter() {
  const el = document.getElementById('rxMaterialFilterList');
  if (!el) return;
  if (_rxMaterials.length) { _renderRxMaterialFilter(); return; }
  fetch('/api/moon-goo')
    .then(r => r.ok ? r.json() : { prices: [] })
    .then(d => {
      _rxMaterials = (d.prices || []).map(p => ({ type_id: p.type_id, name: p.name }));
      _renderRxMaterialFilter();
    })
    .catch(() => { el.innerHTML = '<div class="pp-empty">Could not load the material list.</div>'; });
}

function _renderRxMaterialFilter() {
  const el = document.getElementById('rxMaterialFilterList');
  if (!el) return;
  if (!_rxMaterials.length) { el.innerHTML = '<div class="pp-empty">No moon materials priced yet.</div>'; return; }
  el.innerHTML = _rxMaterials.map(m => `
    <label class="pp-label-check">
      <input type="checkbox" class="rx-material-cb" value="${m.type_id}" checked> ${_esc(m.name)}
    </label>`).join('');
}

// Unchecked = excluded. Returns null (no restriction) when everything is checked, since that's
// the same as not filtering at all — avoids sending a full list on the common "use everything"
// path.
function _rxSelectedMaterialIds() {
  const boxes = document.querySelectorAll('.rx-material-cb');
  if (!boxes.length) return null;
  const checked = [...boxes].filter(b => b.checked).map(b => parseInt(b.value, 10));
  return checked.length === boxes.length ? null : checked;
}

// Live "= 1.00 B" hint next to the raw ISK budget input — counting zeros in a 10-digit number
// is error-prone, so echo the same B/M/K formatting used everywhere else (fmtIsk, utils.js).
function _wizRIskLive() {
  const el = document.getElementById('wizRIsk');
  const out = document.getElementById('wizRIskFmt');
  if (!el || !out) return;
  const v = parseFloat(el.value);
  out.textContent = v > 0 ? `= ${fmtIsk(v)} ISK` : '';
}

function wizRCancel() {
  document.getElementById('rxWizard').style.display = 'none';
  document.getElementById('rxDashboard').style.display = '';
  _loadReactionsDashboard();
}

function wizRGo(n) {
  for (let i = 1; i <= 2; i++) {
    const pg = document.getElementById(`wizRPage${i}`);
    if (pg) pg.style.display = (i === n) ? '' : 'none';
    const dot = document.getElementById(`wizRDot${i}`);
    if (!dot) continue;
    dot.classList.toggle('active', i === n);
    dot.classList.toggle('done', i < n);
    if (i < n) {
      dot.style.cursor = 'pointer';
      dot.onclick = () => wizRGo(i);
    } else {
      dot.style.cursor = 'default';
      dot.onclick = null;
    }
  }
}

function wizRSuggest() {
  const isk = parseFloat(document.getElementById('wizRIsk').value) || 0;
  const depth = parseInt(document.getElementById('wizRDepth').value, 10) || 2;
  const cadence = parseFloat(document.getElementById('wizRCadence').value) || 168;
  const materialIds = _rxSelectedMaterialIds();
  const el = document.getElementById('wizRSuggestionsContent');
  wizRGo(2);
  el.innerHTML = '<div class="pp-empty">Crunching the numbers…</div>';
  fetch('/api/reactions/suggest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ isk_budget: isk, max_chain_depth: depth, cadence_hours: cadence, material_ids: materialIds }),
  })
    .then(r => {
      if (!r.ok) throw new Error(r.status === 403 ? 'B0SS alliance membership required' : 'Suggest failed');
      return r.json();
    })
    .then(_renderReactionsSuggestions)
    .catch(err => {
      el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
}

let _rxLastSuggestions = [];

let _rxLastSuggestData = null;

function _renderReactionsSuggestions(data) {
  const el = document.getElementById('wizRSuggestionsContent');
  if (!el) return;
  _rxLastSuggestData = data;
  _rxLastSuggestions = data.suggestions;
  const assignAllBtn = document.getElementById('rxAssignAllBtn');
  if (assignAllBtn) {
    assignAllBtn.disabled = !data.suggestions.length;
    assignAllBtn.textContent = 'Assign all';
  }

  if (!data.suggestions.length) {
    el.innerHTML = '<div class="pp-empty">No suggestions fit that budget — try raising the ISK budget, loosening the max chain depth, or connect more characters for reaction tracking.</div>';
    return;
  }

  const t = data.totals;
  // No progress bar here — with ISK as the only real constraint, it reads at (or near) 100%
  // in the common case, so a bar added no information a line of text doesn't already say.
  const bindingNote = t.binding === 'isk'
    ? 'Used your full ISK budget.'
    : 'There weren\'t enough profitable, liquid reactions within the chain-depth limit to use the whole budget — try loosening "Max chain depth" or the material filter.';
  const budgetSummary = `<div class="pp-card-hint" style="margin-bottom:10px">${bindingNote}</div>`;

  // Grouped by assigned character (each is "this character's job list"), not one flat table —
  // the engine already picks a character per suggestion based on free reaction slots; this
  // just presents that as concrete per-character assignments instead of a ranked table with a
  // character column buried in it. Preserves the backend's profit-descending order within
  // each character's group.
  const byChar = new Map();
  data.suggestions.forEach((s, i) => {
    if (!byChar.has(s.assigned_character)) byChar.set(s.assigned_character, []);
    byChar.get(s.assigned_character).push(i);
  });

  const cards = [...byChar.entries()].map(([charName, idxs]) => {
    const jobs = idxs.map(i => data.suggestions[i]);
    const rows = idxs.map(i => {
      const s = data.suggestions[i];
      const icon = `https://images.evetech.net/types/${s.type_id}/icon?size=32`;
      const jobLine = s.job_count > 1
        ? `${s.job_count} jobs × ${s.runs_per_job} runs`
        : `${s.runs} runs`;
      // A real multi-tier chain (goo -> intermediate -> this product) needs the intermediate(s)
      // reacted and finished FIRST — the player can't even install the top-level job until then.
      // Shown here (before assigning), not just buried in the dashboard's flat pending list.
      const chainNote = (s.chain_tiers && s.chain_tiers.length)
        ? `<div class="rx-sugg-chain">React first: ${s.chain_tiers.map(t =>
            `${_esc(t.name)} (${t.job_count > 1 ? `${t.job_count} jobs × ${Math.ceil(t.runs / t.job_count)} runs` : `${t.runs} runs`})`
          ).join(', then ')} — <b>then</b> ${_esc(s.name)}</div>`
        : '';
      return `
        <div class="rx-sugg-row">
          <img class="rx-sugg-icon" src="${icon}" alt="" onerror="this.style.visibility='hidden'">
          <div class="rx-sugg-info">
            <div class="rx-sugg-name" title="${_esc(s.name)}">${_esc(s.name)}</div>
            <div class="rx-sugg-meta">${jobLine} · ${_fmtIsk(s.input_cost)} in · ${_fmtHours(s.runtime_hours)} runtime</div>
            <div class="rx-sugg-meta">${Math.round(s.output_qty).toLocaleString()} units · ${_fmtIsk(s.output_value)} value · ${Math.round(s.output_m3).toLocaleString()} m³</div>
            ${chainNote}
          </div>
          <div class="rx-sugg-reward">+${_fmtIsk(s.reward)}</div>
          <button class="rx-sugg-assign-btn" id="rxAssignBtn${i}" onclick="_rxAssignSuggestion(${i}, this)">Assign</button>
        </div>`;
    }).join('');
    const cost = jobs.reduce((sum, s) => sum + s.input_cost, 0);
    const reward = jobs.reduce((sum, s) => sum + s.reward, 0);
    return `
      <div class="rx-sugg-card">
        <div class="rx-sugg-hdr">
          <span class="rx-char-label">${_esc(charName)}</span>
          <span class="pp-card-hint">${jobs.length} reaction${jobs.length === 1 ? '' : 's'} · ${_fmtIsk(cost)} in · ${_fmtIsk(reward)} profit</span>
        </div>
        ${rows}
      </div>`;
  }).join('');

  el.innerHTML = budgetSummary + cards + `
    <div class="rx-totals-summary">
      <span>${_fmtIsk(t.isk_committed)} committed</span>
      <span class="rx-totals-profit">+${_fmtIsk(t.net_profit)} net profit</span>
      <span>${_fmtIsk(t.output_value)} output value</span>
      <span>${Math.round(t.output_m3).toLocaleString()} m³ output</span>
      <span class="pp-card-hint">
        ${t.characters_used} character${t.characters_used === 1 ? '' : 's'} used ·
        ${t.completion_hours != null ? _fmtHours(t.completion_hours) + ' to complete' : ''}
      </span>
    </div>` + _renderRxAdvisor(data.advisor);
}

function _renderRxAdvisor(advisor) {
  if (!advisor) return '';
  const items = [];
  for (const hint of advisor.skill_hints || []) {
    items.push(`<li>${_esc(hint)}</li>`);
  }
  if (advisor.budget_hint) {
    items.push(`<li>Raising your ISK budget by ${_fmtIsk(advisor.budget_hint.extra_isk)} would add about ${_fmtIsk(advisor.budget_hint.extra_profit)} more profit</li>`);
  }
  (advisor.align_hints || []).forEach((a, i) => {
    items.push(`<li>Increase by ${_fmtIsk(a.extra_isk)} ISK to align <b>${_esc(a.name)}</b> to your cadence (keeps its slots busy the whole period instead of finishing early) — about +${_fmtIsk(a.extra_reward)} more profit
      <button class="rx-sugg-assign-btn" id="rxAlignBtn${i}" onclick="_rxApplyAlignHint(${i}, this)">Apply</button></li>`);
  });
  if (!items.length) return '';
  return `
    <div class="rx-advisor">
      <div class="rx-advisor-title">Suggestions to improve</div>
      <ul>${items.join('')}</ul>
    </div>`;
}

// Applies one "align to cadence" hint in place — bumps the matching suggestion's runs/cost/
// reward/output up to its already-computed aligned values (backend-provided, capped by the
// real cadence/stock limit) and updates the running totals, without re-running the whole
// optimizer (which could reshuffle every other suggestion too, not just this one product).
function _rxApplyAlignHint(hintIndex, btn) {
  if (!_rxLastSuggestData) return;
  const hint = (_rxLastSuggestData.advisor.align_hints || [])[hintIndex];
  if (!hint) return;
  const s = _rxLastSuggestData.suggestions.find(x => x.name === hint.name && x.align_extra_isk > 0);
  if (!s) return;

  _rxLastSuggestData.totals.isk_committed += s.align_extra_isk;
  _rxLastSuggestData.totals.net_profit += s.align_extra_reward;
  _rxLastSuggestData.totals.output_value += (s.aligned_output_value - s.output_value);
  _rxLastSuggestData.totals.output_m3 += (s.aligned_output_m3 - s.output_m3);
  s.runs = s.aligned_runs;
  s.runs_per_job = s.aligned_runs_per_job;
  s.input_cost = s.aligned_input_cost;
  s.reward = s.aligned_reward;
  s.output_qty = s.aligned_output_qty;
  s.output_value = s.aligned_output_value;
  s.output_m3 = s.aligned_output_m3;
  const cadenceEl = document.getElementById('wizRCadence');
  s.runtime_hours = cadenceEl ? parseFloat(cadenceEl.value) : s.runtime_hours;
  s.align_extra_isk = 0;
  s.align_extra_reward = 0;

  _rxLastSuggestData.advisor.align_hints.splice(hintIndex, 1);
  _renderReactionsSuggestions(_rxLastSuggestData);
}

function _rxAssignSuggestion(i, btn) {
  const s = _rxLastSuggestions[i];
  if (!s) return Promise.resolve();
  btn.disabled = true;
  btn.textContent = '…';
  return fetch('/api/reactions/assign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character_id: s.assigned_character_id, type_id: s.type_id, name: s.name,
      runs: s.runs, job_count: s.job_count || 1, input_cost: s.input_cost, reward: s.reward,
      chain_tiers: s.chain_tiers || [],
    }),
  })
    .then(r => {
      if (!r.ok) throw new Error();
      btn.textContent = 'Assigned ✓';
    })
    .catch(() => {
      btn.disabled = false;
      btn.textContent = 'Retry';
    });
}

// "Just assign and sort out the best use of my slots" — commits every current suggestion at
// once instead of clicking Assign per row (still respects whatever's already been assigned/
// retried, via each row button's own state).
function _rxAssignAll() {
  const btn = document.getElementById('rxAssignAllBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Assigning…'; }
  const jobs = _rxLastSuggestions.map((s, i) => {
    const rowBtn = document.getElementById(`rxAssignBtn${i}`);
    if (!rowBtn || rowBtn.textContent.includes('✓')) return Promise.resolve();
    return _rxAssignSuggestion(i, rowBtn);
  });
  Promise.all(jobs).then(() => {
    if (!btn) return;
    const allDone = _rxLastSuggestions.every((s, i) => {
      const rowBtn = document.getElementById(`rxAssignBtn${i}`);
      return rowBtn && rowBtn.textContent.includes('✓');
    });
    btn.textContent = allDone ? 'All assigned ✓' : 'Some failed — retry below';
    btn.disabled = allDone;
  });
}

function _rxSortBy(key) {
  if (_rxSortKey === key) {
    _rxSortDir *= -1;
  } else {
    _rxSortKey = key;
    _rxSortDir = key === 'name' ? 1 : -1; // numeric columns default to highest-first
  }
  _renderReactions();
}

function _renderReactions() {
  const el = document.getElementById('reactionsContent');
  if (!el) return;
  if (!_rxOpps.length) {
    el.innerHTML = '<div class="pp-empty">No reaction opportunities found — check the moon-goo price list has stock, and that pricing is up to date.</div>';
    return;
  }
  const rows = [..._rxOpps].sort((a, b) => {
    const av = a[_rxSortKey], bv = b[_rxSortKey];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string') return _rxSortDir * av.localeCompare(bv);
    return _rxSortDir * (av - bv);
  });
  const head = _RX_COLUMNS.map(c => {
    const active = c.key === _rxSortKey;
    const arrow = active ? (_rxSortDir === 1 ? ' ▲' : ' ▼') : '';
    return `<th onclick="_rxSortBy('${c.key}')" style="cursor:pointer;white-space:nowrap">${_esc(c.label)}${arrow}</th>`;
  }).join('');
  const body = rows.map(o => {
    const cells = _RX_COLUMNS.map(c => `<td>${c.fmt(o[c.key], o)}</td>`).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  el.innerHTML = `
    <div style="overflow-x:auto">
      <table class="pp-card-table" style="width:100%">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    <div class="pp-card-hint" style="margin-top:8px">
      ${rows.length} opportunit${rows.length === 1 ? 'y' : 'ies'} · "Steps" = distinct reaction runs needed (the least-work proxy) ·
      Ship+collateral uses the configured import/export rates ·
      Buy/sell depth = current Jita order-book units, not daily trade volume — a rough liquidity signal only.
    </div>
    ${typeof _isAdmin !== 'undefined' && _isAdmin ? _rxSettingsFormHtml() : ''}`;
  if (typeof _isAdmin !== 'undefined' && _isAdmin) _loadRxSettings();
}

function _rxSettingsFormHtml() {
  return `
    <div class="pp-target-form" style="margin-top:14px;border-top:1px solid var(--clr-border);padding-top:12px">
      <label class="pp-label" for="rxSetImport">Import ISK/m³</label>
      <input type="number" id="rxSetImport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxSetExport">Export ISK/m³</label>
      <input type="number" id="rxSetExport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxSetCollateral">Export collateral %</label>
      <input type="number" id="rxSetCollateral" class="pp-num-input" style="width:100px" step="0.1">
    </div>
    <div style="margin-top:8px">
      <button class="pp-add-btn" onclick="_saveRxSettings()">Save</button>
      <span id="rxSettingsMsg" class="pp-card-hint"></span>
    </div>`;
}

function _loadRxSettings() {
  fetch('/api/reactions/settings').then(r => r.ok ? r.json() : null).then(s => {
    if (!s) return;
    document.getElementById('rxSetImport').value = s.import_isk_per_m3;
    document.getElementById('rxSetExport').value = s.export_isk_per_m3;
    document.getElementById('rxSetCollateral').value = (s.export_collateral_pct * 100).toFixed(2);
  });
}

function _saveRxSettings() {
  const msg = document.getElementById('rxSettingsMsg');
  fetch('/api/reactions/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      import_isk_per_m3: parseFloat(document.getElementById('rxSetImport').value) || 0,
      export_isk_per_m3: parseFloat(document.getElementById('rxSetExport').value) || 0,
      export_collateral_pct: (parseFloat(document.getElementById('rxSetCollateral').value) || 0) / 100,
    }),
  })
    .then(r => { if (!r.ok) throw new Error('Save failed'); return r.json(); })
    .then(() => { msg.textContent = 'Saved.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}
