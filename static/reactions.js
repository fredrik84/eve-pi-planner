// Reactions tab (moon-goo profitability ranking — priced from your alliance group's own price
// sheet if it has one, live market prices otherwise). Fetches the ranked opportunity list and
// renders a client-sortable table — this is advice, not an optimizer, so every dimension
// (profit, steps, liquidity) is shown un-collapsed for the viewer to weigh.

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
  { key: 'job_cost',             label: 'Job cost',        fmt: v => _fmtIsk(v) },
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
        if (!r.ok) throw new Error(r.status === 401 ? 'Log in to use Reactions' : 'Load failed');
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
  // Lazy: the shopping list is folded by default (see #rxShoppingDetails) and only worth
  // computing when actually visible — market-price lookups behind it can take a moment, and
  // most tab-opens/refreshes don't need this data recomputed at all. _onRxShoppingToggle
  // fetches it the first time it's unfolded; here we only re-fetch if it's ALREADY open (e.g.
  // this is a post-assign/cancel refresh and the user had it expanded), not on every tab open.
  const shopDetails = document.getElementById('rxShoppingDetails');
  if (shopDetails && shopDetails.open) _loadRxShoppingList();
}

function _onRxShoppingToggle(el) {
  if (el.open) _loadRxShoppingList();
}

let _rxLastShoppingList = [];

function _loadRxShoppingList() {
  const el = document.getElementById('rxShoppingListContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading shopping list…</div>';
  fetch('/api/reactions/shopping-list')
    .then(r => r.ok ? r.json() : { materials: [] })
    .then(d => {
      _rxLastShoppingList = d.materials || [];
      if (!_rxLastShoppingList.length) {
        el.innerHTML = '<div class="pp-empty">Nothing needed right now — nothing currently assigned.</div>';
        _renderRxReceivedDiff();
        return;
      }
      // "source" is which price actually won for THAT specific material right now (see
      // app.reactions._resolve_reachable's cheaper-wins logic) — not a fixed "is this moon
      // goo" category. Your group's own sheet can lose to a better market rate on any given
      // material, so this list always reflects the cheaper source per item, not a static split.
      const group = _rxLastShoppingList.filter(m => m.source === 'group');
      const market = _rxLastShoppingList.filter(m => m.source !== 'group');
      // Unit price is whichever source (alliance sheet or market) actually won for this
      // material right now — a concrete number to check the real quote against, not just a
      // bare quantity to go find a price for yourself.
      // alt_unit_cost is the LOSING price (whichever source didn't win, group vs market) when
      // both were actually available for this material — shows the real ISK/unit gap instead
      // of just silently picking the cheaper one, so you can judge whether it's worth chasing.
      const priceDiff = m => {
        if (m.alt_unit_cost == null || !m.unit_cost) return '';
        const diff = m.alt_unit_cost - m.unit_cost;
        const pct = Math.abs(diff) / m.unit_cost * 100;
        const altLabel = m.alt_source === 'group' ? 'alliance' : 'market';
        return `<div class="pp-card-hint" style="font-size:10px;white-space:nowrap">${diff >= 0 ? '−' : '+'}${pct.toFixed(1)}% vs ${altLabel}</div>`;
      };
      const section = (title, items) => !items.length ? '' : `
        <div class="rx-shop-sec-title">${title} <span class="pp-card-hint">— ${Math.round(items.reduce((s, m) => s + (m.volume_m3 || 0), 0)).toLocaleString()} m³ total</span></div>
        <div style="overflow-x:auto">
          <table class="pp-card-table" style="width:100%">
            <thead><tr><th>Material</th><th>Quantity</th><th>Unit price</th><th>Est. cost</th><th>Volume</th></tr></thead>
            <tbody>${items.map(m => `<tr><td>${_esc(m.name)}</td><td>${_rxCopyQtyCell(m.quantity)}</td><td>${_fmtIsk(m.unit_cost)}${priceDiff(m)}</td><td>${_fmtIsk(m.unit_cost * m.quantity)}</td><td>${Math.round(m.volume_m3 || 0).toLocaleString()} m³</td></tr>`).join('')}</tbody>
          </table>
        </div>`;
      el.innerHTML = section('Fetch from your alliance', group)
        + section('Buy on the market (fuel blocks, or cheaper right now than your sheet)', market);
      _renderRxReceivedDiff();
    })
    .catch(() => { el.innerHTML = '<div class="pp-empty">Failed to load shopping list.</div>'; });
}

// A quantity cell that copies its raw integer to the clipboard on click — ready to paste
// straight into EVE's multibuy quantity field. Reuses the same visual treatment as the PI
// Refill tool's click-to-copy P1 amounts (style-wizard.css .p1-amt*).
function _rxCopyQtyCell(qty) {
  return `<b class="p1-amt p1-amt-set" onclick="_rxCopyQty(this)" title="Click to copy">${Math.round(qty).toLocaleString()}</b>`;
}
function _rxCopyQty(el) {
  const n = el.textContent.replace(/[^\d]/g, '');
  if (!n) return;
  const done = () => { el.classList.add('p1-amt-copied'); setTimeout(() => el.classList.remove('p1-amt-copied'), 600); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(n).then(done).catch(done);
  else done();
}

let _rxDiffStillShort = [];  // last-rendered "still short" rows — feeds _rxCopyReceivedDiff

// Diffs a pasted partial delivery (alliance contract, market buy, whatever actually arrived)
// against the shopping list totals — a contract rarely covers everything you asked for in one
// go, so this shows what's still short per material instead of making you eyeball two lists.
// Reuses the same parser as the PI Refill inventory paste (utils.js). Always leaves a status
// line behind (parsed count, matched count) so pasting never looks like a silent no-op —
// whether the list matched perfectly, partially, or not at all.
function _renderRxReceivedDiff() {
  const el = document.getElementById('rxReceivedDiff');
  const ta = document.getElementById('rxReceivedPaste');
  if (!el || !ta) return;
  const text = (ta.value || '').replace(/ /g, ' ');  // normalize non-breaking spaces some clipboards insert
  if (!text.trim()) { el.innerHTML = ''; _rxDiffStillShort = []; return; }
  const lines = text.split('\n').filter(l => l.trim());
  const received = {};
  let parsedCount = 0;
  for (const line of lines) {
    const parsed = _parseInventoryLine(line);
    if (!parsed) continue;
    parsedCount++;
    const key = parsed[0].toLowerCase();
    received[key] = (received[key] || 0) + parsed[1];
  }
  if (!parsedCount) {
    el.innerHTML = `<div class="pp-empty">Couldn't recognize any "Name  Quantity" lines in ${lines.length} pasted line${lines.length === 1 ? '' : 's'} — expecting EVE's inventory/contract copy format (name, then quantity, tab- or space-separated).</div>`;
    _rxDiffStillShort = [];
    return;
  }
  const matched = new Set();
  const rows = _rxLastShoppingList.map(m => {
    const key = m.name.toLowerCase();
    const got = received[key] || 0;
    if (got) matched.add(key);
    const remaining = Math.max(0, m.quantity - got);
    return { name: m.name, needed: m.quantity, got, remaining, unit_cost: m.unit_cost };
  });
  const leftover = Object.keys(received).filter(k => !matched.has(k));
  const stillShort = rows.filter(r => r.remaining > 0);
  const covered = rows.filter(r => r.remaining <= 0 && r.needed > 0);
  _rxDiffStillShort = stillShort;
  const rowHtml = r => `<tr class="${r.remaining <= 0 ? 'rx-diff-covered' : ''}">
      <td>${_esc(r.name)}</td><td>${r.needed.toLocaleString()}</td><td>${r.got.toLocaleString()}</td>
      <td>${r.remaining > 0 ? _rxCopyQtyCell(r.remaining) : 'Covered ✓'}</td>
      <td>${r.remaining > 0 ? _fmtIsk(r.remaining * r.unit_cost) : '—'}</td></tr>`;
  el.innerHTML = `
    <div class="pp-card-hint" style="margin-bottom:6px">Parsed ${parsedCount} of ${lines.length} pasted line${lines.length === 1 ? '' : 's'} · matched ${matched.size} of ${rows.length} material${rows.length === 1 ? '' : 's'} on this list</div>
    <div style="overflow-x:auto">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Material</th><th>Needed</th><th>Received</th><th>Still short</th><th>Est. cost to finish</th></tr></thead>
        <tbody>${rows.map(rowHtml).join('')}</tbody>
      </table>
    </div>
    <div class="pp-card-hint" style="margin-top:8px">
      ${stillShort.length} material${stillShort.length === 1 ? '' : 's'} still short · ${covered.length} fully covered
      ${leftover.length ? ` · unrecognized/unneeded in your paste: ${leftover.map(_esc).join(', ')}` : ''}
    </div>
    ${stillShort.length ? `<div style="margin-top:8px"><button class="pp-add-btn" onclick="_rxCopyReceivedDiff(this)">Copy still-short for multibuy</button></div>` : ''}`;
}

function _rxCopyReceivedDiff(btn) {
  const text = _rxDiffStillShort.map(r => `${r.name}\t${r.remaining}`).join('\n');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
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
      if (!r.ok) throw new Error(r.status === 401 ? 'Log in to use Reactions' : 'Load failed');
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
      squares.push(`<div class="rx-slot rx-slot-empty" title="Free reaction slot — click to assign your own product" onclick="_rxOpenManualAssign('${c.character_id}')"><span class="rx-slot-empty-mark">+</span></div>`);
    }
    return `
      <div class="rx-char-row">
        <div class="rx-char-label">${_esc(c.character_name)}<br><span class="pp-card-hint">${c.free_slots} / ${c.slots} free</span></div>
        <div class="rx-slot-row">${squares.join('')}</div>
      </div>`;
  }).join('');

  // Grouped per character, styled the same way the PI Dashboard groups its own "Needs
  // attention" cards (dash-issue/dash-issue-char/dash-issue-items) — a flat list of "Install X
  // on Y" lines got hard to scan once several characters each had a few pending installs.
  const todoByChar = new Map();
  for (const g of todoGroups.values()) {
    if (!todoByChar.has(g.character_name)) todoByChar.set(g.character_name, []);
    todoByChar.get(g.character_name).push(g);
  }
  // Reaction Formulas aren't consumed (unlike a BPC) but one physical copy has to be loaded
  // per concurrent job slot to start it — so the formula count needed to install everything
  // shown is exactly g.count (the same number of job slots), not a separate figure to compute.
  const todoNote = todoByChar.size
    ? `<div class="rx-todo-groups">${[...todoByChar.entries()].map(([charName, items]) => `
        <div class="dash-issue dash-issue-warn">
          <div class="dash-issue-char">${_esc(charName)}</div>
          <ul class="dash-issue-items">${items.map(g =>
            `<li class="dash-il-warn">Install <b>${_esc(g.name)}</b> ×${g.runs} — <b>${g.count}</b> formula${g.count > 1 ? 's' : ''} needed</li>`
          ).join('')}</ul>
        </div>`).join('')}</div>`
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
      ${_dashTile('+' + _fmtIsk(data.pending_net_profit_per_day), 'Expected profit / day', 'an-ok')}
      ${_dashTile(`${usedSlots}<span class="an-of"> / ${data.total_slots}</span>`, 'Slots used')}
      ${_dashTile(String(pendingCount), 'Jobs to install', pendingCount > 0 ? 'an-warn' : '')}
      ${_dashTile(timeLeftVal, timeLeftLbl)}
    </div>`;

  if (metricsEl) metricsEl.innerHTML = overviewTiles;

  // Alerts get their own card at the very top of the page (same placement/shape as the
  // Dashboard tab's own alert cards) rather than being buried inside the "Reactions" card body —
  // "what needs installing right now" is the actionable part, not just a status footnote.
  const alertsCard = document.getElementById('rxAlertsCard');
  const alertsContent = document.getElementById('rxAlertsContent');
  const alertsHint = document.getElementById('rxAlertsHint');
  if (alertsCard && alertsContent) {
    if (todoByChar.size) {
      alertsCard.style.display = '';
      alertsContent.innerHTML = todoNote;
      if (alertsHint) alertsHint.textContent = `— ${pendingCount} job${pendingCount === 1 ? '' : 's'} to install`;
    } else {
      alertsCard.style.display = 'none';
    }
  }

  el.innerHTML = rows + untrackedNote;
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

// ── Manual "assign a product to this empty slot" modal ─────────────────────────────────────
// Picks from the same reachable/priced product list the "Advanced" opportunity table already
// has (_rxOpps) — so a manual pick gets the same real cost/profit numbers as an algorithm
// suggestion, not a guess. If the concurrent-jobs count is more than the clicked character has
// free, the extra jobs spill onto the account's other free slots (most-free-first), same idea
// as the factory-planet overflow elsewhere in this app — never silently overloads one character.
let _rxManualAssignCharId = null;

function _rxOpenManualAssign(characterId) {
  _rxManualAssignCharId = characterId;
  const chars = (_rxLastDashboardData && _rxLastDashboardData.characters) || [];
  const char = chars.find(c => String(c.character_id) === String(characterId));
  const hint = document.getElementById('rxManualAssignHint');
  if (hint) {
    hint.textContent = char
      ? `Assigning to ${char.character_name} (${char.free_slots} free slot${char.free_slots === 1 ? '' : 's'} here). If this needs more concurrent jobs than that, the rest spill onto your other characters' free slots.`
      : 'Pick a product, how many runs each job does, and how many jobs to start at once.';
  }
  document.getElementById('rxManProduct').value = '';
  document.getElementById('rxManRuns').value = 1;
  document.getElementById('rxManJobs').value = 1;
  document.getElementById('rxManualAssignStatus').textContent =
    _rxOpps.length ? '' : 'Still loading the product list — wait a moment, then search again.';
  document.getElementById('rxManualAssignPreview').innerHTML = '';
  _rxHideProductDropdown();
  document.getElementById('rxManualAssignModal').style.display = '';
}

function _rxCloseManualAssign() {
  document.getElementById('rxManualAssignModal').style.display = 'none';
  _rxHideProductDropdown();
}

function _rxManualAssignMatch() {
  const name = document.getElementById('rxManProduct').value.trim();
  return _rxOpps.find(o => o.name === name) || null;
}

// ── Product search dropdown (click to browse the full reachable list, or type to filter) ──
// A plain <input list=datalist> doesn't let you browse everything with an empty query on every
// browser, and gives no icons — this is a small custom combobox instead: click/focus with
// nothing typed shows every reachable product (alphabetical), typing filters by substring
// match anywhere in the name, arrow keys + Enter select, click selects, Escape/blur closes.
let _rxProductDropdownList = [];
let _rxProductDropdownIdx = -1;

function _rxProductDropdownFilter() {
  const input = document.getElementById('rxManProduct');
  const dd = document.getElementById('rxManProductDropdown');
  if (!input || !dd) return;
  const q = input.value.trim().toLowerCase();
  const all = [..._rxOpps].sort((a, b) => a.name.localeCompare(b.name));
  _rxProductDropdownList = (q ? all.filter(o => o.name.toLowerCase().includes(q)) : all).slice(0, 200);
  _rxProductDropdownIdx = -1;
  if (!_rxProductDropdownList.length) {
    dd.innerHTML = `<div class="rx-man-product-empty">${_rxOpps.length ? 'No matching product.' : 'Loading products…'}</div>`;
  } else {
    dd.innerHTML = _rxProductDropdownList.map((o, i) => `
      <div class="rx-man-product-row" data-idx="${i}" onmousedown="event.preventDefault();_rxSelectProduct(${i})">
        <img src="https://images.evetech.net/types/${o.type_id}/icon?size=32" alt="" onerror="this.style.visibility='hidden'">
        ${_esc(o.name)}
      </div>`).join('');
  }
  dd.style.display = '';
}

function _rxSelectProduct(idx) {
  const o = _rxProductDropdownList[idx];
  if (!o) return;
  document.getElementById('rxManProduct').value = o.name;
  _rxHideProductDropdown();
  _rxManualAssignPreview();
}

function _rxHideProductDropdown() {
  const dd = document.getElementById('rxManProductDropdown');
  if (dd) dd.style.display = 'none';
  _rxProductDropdownIdx = -1;
}

function _rxProductDropdownKey(event) {
  const dd = document.getElementById('rxManProductDropdown');
  if (!dd || dd.style.display === 'none') return;
  if (event.key === 'Escape') { _rxHideProductDropdown(); return; }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!_rxProductDropdownList.length) return;
    const dir = event.key === 'ArrowDown' ? 1 : -1;
    _rxProductDropdownIdx = (_rxProductDropdownIdx + dir + _rxProductDropdownList.length) % _rxProductDropdownList.length;
    [...dd.children].forEach((el, i) => el.classList.toggle('rx-man-product-active', i === _rxProductDropdownIdx));
    dd.children[_rxProductDropdownIdx].scrollIntoView({ block: 'nearest' });
  } else if (event.key === 'Enter') {
    event.preventDefault();
    _rxSelectProduct(_rxProductDropdownIdx >= 0 ? _rxProductDropdownIdx : 0);
  }
}

// Scroll-wheel adjusts a number input by ±1 per tick (clamped to its min) — quicker than typing
// for the small counts ("Runs per job", "Concurrent jobs") this modal asks for.
function _rxNumWheel(event, el) {
  event.preventDefault();
  const min = parseInt(el.min, 10) || 1;
  const cur = parseInt(el.value, 10) || min;
  el.value = Math.max(min, cur + (event.deltaY < 0 ? 1 : -1));
  _rxManualAssignPreview();
}

// Scales the matched opportunity's own totals (computed for its max achievable batch,
// top_level_runs) down to whatever run count the user actually entered — same per-run rates,
// just a smaller slice, rather than re-deriving cost/profit from scratch client-side.
function _rxManualAssignPreview() {
  const el = document.getElementById('rxManualAssignPreview');
  if (!el) return;
  const o = _rxManualAssignMatch();
  const runsPerJob = parseInt(document.getElementById('rxManRuns').value, 10) || 0;
  const jobs = parseInt(document.getElementById('rxManJobs').value, 10) || 0;
  if (!o || runsPerJob <= 0 || jobs <= 0 || !o.top_level_runs) { el.innerHTML = ''; return; }
  const totalRuns = runsPerJob * jobs;
  const scale = totalRuns / o.top_level_runs;
  const outputQty = o.output_qty * scale;
  const fixedCosts = (o.input_cost + (o.job_cost || 0) + o.shipping_cost + o.collateral_cost) * scale;
  const outputValue = o.instant_sell_value * scale;
  const profit = outputValue - fixedCosts;
  const breakEven = outputQty > 0 ? fixedCosts / outputQty : 0;
  const runtimeHours = o.cycle_time ? (o.cycle_time / 3600) * runsPerJob : 0;
  el.innerHTML = `
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="pp-card-hint">Input cost</span><b>${_fmtIsk(fixedCosts)}</b></div>
      <div class="rx-manual-preview-row"><span class="pp-card-hint">Runtime per job</span><b>${_fmtHours(runtimeHours)}</b></div>
      <div class="rx-manual-preview-row"><span class="pp-card-hint">Output value</span><b>${_fmtIsk(outputValue)} <span class="pp-card-hint">(${Math.round(outputQty).toLocaleString()} units)</span></b></div>
      <div class="rx-manual-preview-row"><span class="pp-card-hint">Profit</span><b class="${profit >= 0 ? 'an-ok' : 'an-warn'}">${_fmtIsk(profit)}</b></div>
      <div class="rx-manual-preview-breakeven">Sell for at least <b>${_fmtIsk(breakEven)}</b>/unit to break even at today's material, shipping${o.job_cost ? ', job' : ''} and collateral cost.</div>
    </div>`;
}

function _rxSubmitManualAssign() {
  const status = document.getElementById('rxManualAssignStatus');
  const o = _rxManualAssignMatch();
  if (!o) { status.textContent = 'Pick a product from the list.'; return; }
  const runsPerJob = parseInt(document.getElementById('rxManRuns').value, 10) || 0;
  const jobsWanted = parseInt(document.getElementById('rxManJobs').value, 10) || 0;
  if (runsPerJob <= 0 || jobsWanted <= 0 || !o.top_level_runs) { status.textContent = 'Enter runs per job and concurrent jobs.'; return; }

  // Per-run rates, matching the exact input_cost/reward split _rxAssignSuggestion already uses:
  // input_cost = raw material spend only (the "ISK committed" tile), reward = fully netted
  // profit (materials + shipping + collateral + job cost already subtracted) — see
  // app.reactions._build_opportunities.
  const costPerRun = o.input_cost / o.top_level_runs;
  const rewardPerRun = o.net_profit_instant / o.top_level_runs;

  const chars = ((_rxLastDashboardData && _rxLastDashboardData.characters) || []).filter(c => c.tracked && c.slots > 1);
  const clicked = chars.find(c => String(c.character_id) === String(_rxManualAssignCharId));
  const others = chars.filter(c => String(c.character_id) !== String(_rxManualAssignCharId))
    .sort((a, b) => b.free_slots - a.free_slots);
  const ordered = clicked ? [clicked, ...others] : others;

  const allocations = [];
  let remaining = jobsWanted;
  for (const c of ordered) {
    if (remaining <= 0) break;
    const take = Math.min(remaining, c.free_slots);
    if (take > 0) { allocations.push({ char: c, jobs: take }); remaining -= take; }
  }

  if (!allocations.length) { status.textContent = 'No free reaction slots on any tracked character.'; return; }
  if (remaining > 0 && !confirm(`Only ${jobsWanted - remaining} of ${jobsWanted} jobs fit across your free slots right now. Assign what fits?`)) return;

  status.textContent = 'Assigning…';
  Promise.all(allocations.map(a => fetch('/api/reactions/assign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character_id: a.char.character_id, type_id: o.type_id, name: o.name,
      runs: a.jobs * runsPerJob, job_count: a.jobs,
      input_cost: costPerRun * a.jobs * runsPerJob, reward: rewardPerRun * a.jobs * runsPerJob,
      chain_tiers: [],
    }),
  }).then(r => { if (!r.ok) throw new Error('Assign failed'); })))
    .then(() => { _rxCloseManualAssign(); onReactionsTabOpen(); })
    .catch(err => { status.textContent = err.message; });
}

// ── Wizard: "Add Reaction Product" ──────────────────────────────────────────────────────────
let _rxMaterials = []; // [{type_id, name}] moon materials — loaded once, reused across wizard opens
let _rxFuelBlocks = []; // [{type_id, name}] racial fuel blocks — same idea, separate category

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
  if (_rxMaterials.length || _rxFuelBlocks.length) { _renderRxMaterialFilter(); return; }
  Promise.all([
    fetch('/api/moon-goo').then(r => r.ok ? r.json() : { prices: [] }),
    fetch('/api/reactions/fuel-blocks').then(r => r.ok ? r.json() : { fuel_blocks: [] }),
  ]).then(([goo, fb]) => {
    _rxMaterials = (goo.prices || []).map(p => ({ type_id: p.type_id, name: p.name }));
    _rxFuelBlocks = fb.fuel_blocks || [];
    _renderRxMaterialFilter();
  }).catch(() => { el.innerHTML = '<div class="pp-empty">Could not load the material list.</div>'; });
}

function _renderRxMaterialFilter() {
  const el = document.getElementById('rxMaterialFilterList');
  if (!el) return;
  if (!_rxMaterials.length && !_rxFuelBlocks.length) { el.innerHTML = '<div class="pp-empty">No materials priced yet.</div>'; return; }
  // Two separate groups — a player's real access to moon goo and to each racial fuel block are
  // independent concerns (e.g. cheap local Oxygen Fuel Block production, but unreliable
  // Hydrogen supply), so they're not lumped into one undifferentiated checklist. data-group
  // tags which group a box belongs to, so _rxEnforceMaterialMinimum can require at least one
  // checked box PER group (deselecting every fuel block makes every reaction unreachable —
  // every formula needs one — which surfaced as a confusing generic "raise your budget" error
  // instead of the real cause).
  const group = (title, items, key) => !items.length ? '' : `
    <div class="pp-card-hint" style="margin:8px 0 2px;font-weight:600">${title}</div>
    ${items.map(m => `
      <label class="pp-label-check">
        <input type="checkbox" class="rx-material-cb" data-group="${key}" value="${m.type_id}" checked onchange="_rxEnforceMaterialMinimum(this)"> ${_esc(m.name)}
      </label>`).join('')}`;
  el.innerHTML = group('Moon materials', _rxMaterials, 'moon') + group('Fuel blocks', _rxFuelBlocks, 'fuel');
}

// Requires at least one checked box per group (moon materials, fuel blocks) — every reaction
// needs SOME fuel block and most need moon goo, so emptying either group entirely makes nothing
// reachable at all, which read as a confusing "no suggestions fit that budget" rather than the
// real cause. Reverts the change and explains why instead of silently allowing an empty group.
function _rxEnforceMaterialMinimum(cb) {
  if (cb.checked) return; // only unchecking can create an empty group
  const groupKey = cb.dataset.group;
  const groupBoxes = document.querySelectorAll(`.rx-material-cb[data-group="${groupKey}"]`);
  const anyChecked = [...groupBoxes].some(b => b.checked);
  if (!anyChecked) {
    cb.checked = true;
    const label = groupKey === 'fuel' ? 'fuel block' : 'moon material';
    alert(`At least one ${label} must stay checked — every reaction needs one, so leaving none checked would make nothing suggestible at all.`);
  }
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
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Crunching the numbers…</div>';
  fetch('/api/reactions/suggest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ isk_budget: isk, max_chain_depth: depth, cadence_hours: cadence, material_ids: materialIds }),
  })
    .then(r => {
      if (!r.ok) throw new Error(r.status === 401 ? 'Log in to use Reactions' : 'Suggest failed');
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
      <span class="rx-totals-profit">+${_fmtIsk(t.net_profit)} net profit${t.net_profit_per_day != null ? ` (${_fmtIsk(t.net_profit_per_day)}/day)` : ''}</span>
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
  if (advisor.budget_hint) {
    items.push(`<li><div class="rx-hint-row">
      <span class="rx-hint-text">Raising your ISK budget by ${_fmtIsk(advisor.budget_hint.extra_isk)} would add about ${_fmtIsk(advisor.budget_hint.extra_profit)} more profit</span>
      <button class="rx-sugg-assign-btn" onclick="_rxApplyBudgetHint(${advisor.budget_hint.extra_isk}, this)">Apply</button>
    </div></li>`);
  }
  (advisor.align_hints || []).forEach((a, i) => {
    items.push(`<li><div class="rx-hint-row">
      <span class="rx-hint-text">Increase by ${_fmtIsk(a.extra_isk)} ISK to align <b>${_esc(a.name)}</b> to your cadence — about +${_fmtIsk(a.extra_reward)} more profit</span>
      <button class="rx-sugg-assign-btn" id="rxAlignBtn${i}" onclick="_rxApplyAlignHint(${i}, this)">Apply</button>
    </div></li>`);
  });
  (advisor.fuel_block_hints || []).forEach(h => {
    items.push(`<li><div class="rx-hint-row">
      <span class="rx-hint-text">Also allowing <b>${_esc(h.name)}</b> in the material filter would raise your expected profit by about ${h.extra_pct}% (+${_fmtIsk(h.extra_isk_per_day)}/day)</span>
      <button class="rx-sugg-assign-btn" onclick="_rxApplyFuelBlockHint(${h.type_id}, this)">Apply</button>
    </div></li>`);
  });
  if (!items.length) return '';
  return `
    <div class="rx-advisor">
      <div class="rx-advisor-title">Suggestions to improve</div>
      <ul>${items.join('')}</ul>
    </div>`;
}

// Checks the matching fuel-block box in the (possibly currently-hidden, since we're on the
// suggestions page not the budget page) material filter and re-runs the whole suggest call —
// unlike _rxApplyAlignHint (a pure client-side field swap), widening the material set can
// genuinely reshuffle every suggestion, not just one product, so a full recalculation is the
// only correct way to apply this.
function _rxApplyFuelBlockHint(typeId, btn) {
  const box = document.querySelector(`.rx-material-cb[value="${typeId}"]`);
  if (box) box.checked = true;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  wizRSuggest();
}

// Bumps the ISK budget input by the hinted amount and re-runs the whole suggest call — same
// "widening a constraint can reshuffle every suggestion" reasoning as _rxApplyFuelBlockHint,
// so this is a full recalculation too, not a local field patch.
function _rxApplyBudgetHint(extraIsk, btn) {
  const iskEl = document.getElementById('wizRIsk');
  if (iskEl) {
    iskEl.value = (parseFloat(iskEl.value) || 0) + extraIsk;
    _wizRIskLive();
  }
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  wizRSuggest();
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

  const cadenceEl = document.getElementById('wizRCadence');
  const cadenceHours = cadenceEl ? parseFloat(cadenceEl.value) : null;

  _rxLastSuggestData.totals.isk_committed += s.align_extra_isk;
  _rxLastSuggestData.totals.net_profit += s.align_extra_reward;
  if (cadenceHours > 0) {
    _rxLastSuggestData.totals.net_profit_per_day = (_rxLastSuggestData.totals.net_profit_per_day || 0)
      + (s.align_extra_reward / (cadenceHours / 24));
  }
  _rxLastSuggestData.totals.output_value += (s.aligned_output_value - s.output_value);
  _rxLastSuggestData.totals.output_m3 += (s.aligned_output_m3 - s.output_m3);
  s.runs = s.aligned_runs;
  s.runs_per_job = s.aligned_runs_per_job;
  s.input_cost = s.aligned_input_cost;
  s.reward = s.aligned_reward;
  s.profit_per_day = s.aligned_profit_per_day;
  s.output_qty = s.aligned_output_qty;
  s.output_value = s.aligned_output_value;
  s.output_m3 = s.aligned_output_m3;
  s.runtime_hours = cadenceHours || s.runtime_hours;
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
    el.innerHTML = '<div class="pp-empty">No reaction opportunities found — check the moon-goo price list has pricing set, and that it\'s up to date.</div>';
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
    ${_rxCanEditSettings() ? _rxSettingsFormHtml() : ''}
    ${_rxAccountSettingsFormHtml()}`;
  if (_rxCanEditSettings()) _loadRxSettings();
  _loadRxAccountSettings();
}

// Site admins can always preview/edit; a non-admin sees the form only if they manage at least
// one group — GET/PUT /api/reactions/settings always resolves to THEIR OWN group (via
// membership), so this is just a visibility check, not a scoping one.
function _rxCanEditSettings() {
  return (typeof _isAdmin !== 'undefined' && _isAdmin) || (typeof _isGroupManager !== 'undefined' && _isGroupManager);
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

      <label class="pp-label" for="rxSetSystem">Reaction system</label>
      <input type="text" id="rxSetSystem" class="pp-num-input" style="width:120px" placeholder="e.g. Jita">

      <label class="pp-label" for="rxSetTax">Facility tax %</label>
      <input type="number" id="rxSetTax" class="pp-num-input" style="width:100px" step="0.1">
    </div>
    <div class="pp-card-hint" style="margin-top:2px">Reaction system + facility tax estimate real job-installation fees (EVE's system cost index × EIV, plus your structure's tax). Leave the system blank to skip this — nothing changes until it's set.</div>
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
    document.getElementById('rxSetSystem').value = s.reaction_system || '';
    document.getElementById('rxSetTax').value = ((s.facility_tax_pct || 0) * 100).toFixed(2);
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
      reaction_system: document.getElementById('rxSetSystem').value.trim() || null,
      facility_tax_pct: (parseFloat(document.getElementById('rxSetTax').value) || 0) / 100,
    }),
  })
    .then(async r => { if (!r.ok) throw new Error((await r.json()).detail || 'Save failed'); return r.json(); })
    .then(() => { msg.textContent = 'Saved.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}

// Every logged-in user (not just group managers) can override their OWN shipping/collateral
// rate — JF cost genuinely varies account-to-account (home system, courier arrangement) even
// within one alliance. No override saved = use the group's rate (or the global default).
function _rxAccountSettingsFormHtml() {
  return `
    <div class="pp-target-form" style="margin-top:14px;border-top:1px solid var(--clr-border);padding-top:12px">
      <div class="pp-card-hint" id="rxAcctSettingsHint" style="margin-bottom:8px"></div>
      <label class="pp-label" for="rxAcctImport">Your import ISK/m³</label>
      <input type="number" id="rxAcctImport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxAcctExport">Your export ISK/m³</label>
      <input type="number" id="rxAcctExport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxAcctCollateral">Your export collateral %</label>
      <input type="number" id="rxAcctCollateral" class="pp-num-input" style="width:100px" step="0.1">

      <label class="pp-label" for="rxAcctSystem">Your reaction system</label>
      <input type="text" id="rxAcctSystem" class="pp-num-input" style="width:120px" placeholder="e.g. Jita">

      <label class="pp-label" for="rxAcctTax">Your facility tax %</label>
      <input type="number" id="rxAcctTax" class="pp-num-input" style="width:100px" step="0.1">
    </div>
    <div class="pp-card-hint" style="margin-top:2px">Reaction system + facility tax estimate real job-installation fees. Leave the system blank to skip this — nothing changes until it's set.</div>
    <div style="margin-top:8px">
      <button class="pp-add-btn" onclick="_saveRxAccountSettings()">Save my rate</button>
      <button class="pp-cancel-btn" onclick="_resetRxAccountSettings()">Use default instead</button>
      <span id="rxAcctSettingsMsg" class="pp-card-hint"></span>
    </div>`;
}

function _loadRxAccountSettings() {
  fetch('/api/reactions/account-settings').then(r => r.ok ? r.json() : null).then(s => {
    if (!s) return;
    const eff = s.override || s.default;
    document.getElementById('rxAcctImport').value = eff.import_isk_per_m3;
    document.getElementById('rxAcctExport').value = eff.export_isk_per_m3;
    document.getElementById('rxAcctCollateral').value = (eff.export_collateral_pct * 100).toFixed(2);
    document.getElementById('rxAcctSystem').value = eff.reaction_system || '';
    document.getElementById('rxAcctTax').value = ((eff.facility_tax_pct || 0) * 100).toFixed(2);
    const hint = document.getElementById('rxAcctSettingsHint');
    if (hint) {
      hint.textContent = s.override
        ? "Your shipping cost — you're using your own rate instead of the group/default."
        : "Your shipping cost — currently using the group/default rate. Set your own below if your real JF cost or reaction system differs.";
    }
  });
}

function _saveRxAccountSettings() {
  const msg = document.getElementById('rxAcctSettingsMsg');
  fetch('/api/reactions/account-settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      import_isk_per_m3: parseFloat(document.getElementById('rxAcctImport').value) || 0,
      export_isk_per_m3: parseFloat(document.getElementById('rxAcctExport').value) || 0,
      export_collateral_pct: (parseFloat(document.getElementById('rxAcctCollateral').value) || 0) / 100,
      reaction_system: document.getElementById('rxAcctSystem').value.trim() || null,
      facility_tax_pct: (parseFloat(document.getElementById('rxAcctTax').value) || 0) / 100,
    }),
  })
    .then(async r => { if (!r.ok) throw new Error((await r.json()).detail || 'Save failed'); return r.json(); })
    .then(() => { msg.textContent = 'Saved.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}

function _resetRxAccountSettings() {
  const msg = document.getElementById('rxAcctSettingsMsg');
  fetch('/api/reactions/account-settings', { method: 'DELETE' })
    .then(r => { if (!r.ok) throw new Error('Reset failed'); return r.json(); })
    .then(() => { msg.textContent = 'Reverted to default.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}
