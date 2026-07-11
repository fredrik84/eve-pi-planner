// Reactions tab (B0SS moon-goo profitability ranking, alliance-gated). Fetches the ranked
// opportunity list and renders a client-sortable table — this is advice, not an optimizer, so
// every dimension (profit, steps, liquidity) is shown un-collapsed for the viewer to weigh.

let _rxOpps = [];
let _rxSortKey = 'net_profit_instant';
let _rxSortDir = -1; // -1 = descending

const _RX_COLUMNS = [
  { key: 'name',                 label: 'Product',        fmt: v => _esc(v) },
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
}

// Best-effort name lookup via whatever the opportunity list has already loaded — falls back to
// the raw type_id (no dedicated type-name endpoint is worth adding just for this display).
function _rxProductName(type_id) {
  const hit = _rxOpps.find(o => o.type_id === type_id);
  return hit ? hit.name : `#${type_id}`;
}

function _loadReactionsDashboard() {
  const el = document.getElementById('rxDashboardContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-empty">Loading…</div>';
  fetch('/api/reactions/jobs')
    .then(r => {
      if (!r.ok) throw new Error(r.status === 403 ? 'B0SS alliance membership required' : 'Load failed');
      return r.json();
    })
    .then(_renderReactionsDashboard)
    .catch(err => {
      el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
}

function _renderReactionsDashboard(data) {
  const el = document.getElementById('rxDashboardContent');
  if (!el) return;

  const untracked = (data.characters || []).filter(c => !c.tracked);
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
  const untrackedNote = untracked.length
    ? `<div class="pp-card-hint" style="margin-bottom:8px">Not yet tracked: ${untracked.map(c => _esc(c.character_name)).join(', ')}</div>`
    : '';

  if (!data.tracked) {
    el.innerHTML = '<div class="pp-empty">No characters are tracking reaction jobs yet — use "Connect a character" above.</div>';
    return;
  }

  // "Loadout screen" per character: one square per reaction slot, occupied ones showing the
  // product's icon + a countdown, empty ones dashed — so an idle tracked character (no jobs
  // running) still shows up as a row of empty slots, not just an aggregate free-slot count.
  const tracked = (data.characters || []).filter(c => c.tracked);
  const jobsByChar = new Map();
  for (const j of data.running) {
    if (!jobsByChar.has(j.character_name)) jobsByChar.set(j.character_name, []);
    jobsByChar.get(j.character_name).push(j);
  }

  const blocks = tracked.map(c => {
    const jobs = jobsByChar.get(c.character_name) || [];
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
    for (let i = jobs.length; i < c.slots; i++) {
      squares.push('<div class="rx-slot rx-slot-empty" title="Free reaction slot"><span class="rx-slot-empty-mark">+</span></div>');
    }
    return `
      <div class="rx-char-block">
        <div class="rx-char-hdr">${_esc(c.character_name)} <span class="pp-card-hint">${c.free_slots} / ${c.slots} free</span></div>
        <div class="rx-slot-row">${squares.join('')}</div>
      </div>`;
  }).join('');

  el.innerHTML = blocks + untrackedNote;
}

// ── Wizard: "Add Reaction Product" ──────────────────────────────────────────────────────────
function wizRStart() {
  document.getElementById('rxDashboard').style.display = 'none';
  document.getElementById('rxWizard').style.display = '';
  wizRGo(1);
  _wizRIskLive();
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
  const steps = parseInt(document.getElementById('wizRSteps').value, 10) || 0;
  const el = document.getElementById('wizRSuggestionsContent');
  wizRGo(2);
  el.innerHTML = '<div class="pp-empty">Crunching the numbers…</div>';
  fetch('/api/reactions/suggest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ isk_budget: isk, steps_budget: steps }),
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

function _renderReactionsSuggestions(data) {
  const el = document.getElementById('wizRSuggestionsContent');
  if (!el) return;

  if (!data.suggestions.length) {
    el.innerHTML = '<div class="pp-empty">No suggestions fit that budget — try raising the ISK or steps budget, or connect more characters for reaction tracking.</div>';
    return;
  }

  // Grouped by assigned character (each is "this character's job list"), not one flat table —
  // the engine already picks a character per suggestion based on free reaction slots; this
  // just presents that as concrete per-character assignments instead of a ranked table with a
  // character column buried in it. Preserves the backend's profit-descending order within
  // each character's group.
  const byChar = new Map();
  for (const s of data.suggestions) {
    if (!byChar.has(s.assigned_character)) byChar.set(s.assigned_character, []);
    byChar.get(s.assigned_character).push(s);
  }

  const cards = [...byChar.entries()].map(([charName, jobs]) => {
    const rows = jobs.map(s => `
      <tr>
        <td>${_esc(s.name)}</td>
        <td>${s.runs}</td>
        <td>${_fmtIsk(s.input_cost)}</td>
        <td>${_fmtIsk(s.reward)}</td>
      </tr>`).join('');
    const cost = jobs.reduce((sum, s) => sum + s.input_cost, 0);
    const reward = jobs.reduce((sum, s) => sum + s.reward, 0);
    return `
      <div class="pp-card" style="margin-bottom:10px">
        <div class="pp-card-title">${_esc(charName)}
          <span class="pp-card-hint">${jobs.length} reaction${jobs.length === 1 ? '' : 's'} · ${_fmtIsk(cost)} in · ${_fmtIsk(reward)} profit</span>
        </div>
        <div style="overflow-x:auto">
          <table class="pp-card-table" style="width:100%">
            <thead><tr><th>Product</th><th>Runs</th><th>Input cost</th><th>Reward</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }).join('');

  const t = data.totals;
  el.innerHTML = cards + `
    <div class="pp-card-hint" style="margin-top:8px">
      ${_fmtIsk(t.isk_committed)} committed · ${_fmtIsk(t.net_profit)} net profit ·
      ${t.characters_used} character${t.characters_used === 1 ? '' : 's'} used ·
      ${t.completion_hours != null ? _fmtHours(t.completion_hours) + ' to complete' : ''}
    </div>`;
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
