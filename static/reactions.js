// Reactions tab (moon-goo profitability ranking — priced from your alliance group's own price
// sheet if it has one, live market prices otherwise). Fetches the ranked opportunity list and
// renders a client-sortable table — this is advice, not an optimizer, so every dimension
// (profit, steps, liquidity) is available to weigh, but only the core ones (what it costs, what
// it makes, what it's worth, what you keep) show as actual table COLUMNS — the full 13-column
// table was overflowing badly, and a "show more columns" toggle just overflowed just as badly
// the moment you turned it on (still the same wide row, just wider). Extra dimensions instead
// expand as a wrapped block BELOW a row when you click it — never adds table width.

let _rxOpps = [];
let _rxSortKey = 'net_profit_instant';
let _rxSortDir = -1; // -1 = descending
let _rxExpandedOpps = new Set();  // type_ids with their detail row open — survives re-sorts

const _RX_CORE_COLUMNS = [
  { key: 'name',                 label: 'Product',        fmt: (v, o) =>
      `<img src="https://images.evetech.net/types/${o.type_id}/icon?size=32" alt="" style="width:18px;height:18px;border-radius:3px;vertical-align:middle;margin-right:5px" onerror="this.style.display='none'">${_esc(v)}` },
  { key: 'output_qty',           label: 'Units',          fmt: v => Math.round(v).toLocaleString() },
  { key: 'input_cost',           label: 'Input cost',     fmt: v => _fmtIsk(v) },
  { key: 'instant_sell_value',   label: 'Output value',   fmt: v => _fmtIsk(v) },
  { key: 'shipping_cost',        label: 'Ship+collateral', fmt: (v, o) => _fmtIsk(v + o.collateral_cost) },
  { key: 'net_profit_instant',   label: 'Profit',         fmt: v => _fmtIsk(v) },
];
// Rendered as "Label: value" chips in the fold-out row, not table cells — order here is display
// order, not column order, so it's fine that these vary in shape (a plain number vs. ISK).
const _RX_DETAIL_FIELDS = [
  { key: 'steps',                label: 'Steps',           fmt: v => String(v) },
  { key: 'job_cost',             label: 'Job cost',        fmt: v => _fmtIsk(v) },
  { key: 'sell_order_value',     label: 'Sell order value', fmt: v => _fmtIsk(v) },
  { key: 'net_profit_order',     label: 'Profit (order)',  fmt: v => _fmtIsk(v) },
  { key: 'profit_per_m3_instant', label: 'ISK/m³',         fmt: v => v == null ? '—' : Math.round(v).toLocaleString() },
  { key: 'buy_volume',           label: 'Buy depth',       fmt: v => Math.round(v).toLocaleString() },
  { key: 'sell_volume',          label: 'Sell depth',      fmt: v => Math.round(v).toLocaleString() },
];

function _rxToggleOppDetail(typeId) {
  if (_rxExpandedOpps.has(typeId)) _rxExpandedOpps.delete(typeId);
  else _rxExpandedOpps.add(typeId);
  _renderReactions();
}

// Shared loader for the reachable/priced opportunity list (_rxOpps) — used by the "Advanced"
// table AND the manual-assign modal's product search. Server-side this is now cached for a
// short TTL (see app.reactions._build_opportunities), but the bigger win is not re-fetching it
// at all client-side unless something actually needs it: a fresh, in-flight fetch is shared
// (concurrent callers get the same promise, not a duplicate request), and once loaded it's
// reused until a caller explicitly forces a refresh (e.g. the Advanced table re-opening).
let _rxOppsLoaded = false;
let _rxOppsLoading = null;

// A 401 on any Reactions load means "not logged in", which is far more useful to show than the
// generic auth detail. Everything else keeps the server's own message.
function _rxErr(e, fallback) {
  return new Error(e && e.status === 401 ? 'Log in to use Reactions' : (fallback || e.message));
}

let _rxCostBasis = null;

// Reaction profits are quoted with NO job installation fee when no reaction system is configured:
// the rate zeroes out entirely (system cost index + facility tax + 4% SCC), which flatters every
// opportunity in the list. Say so rather than letting the numbers pass as complete.
function _rxCostBasisWarn() {
  if (!_rxCostBasis || _rxCostBasis.system) return '';
  return `<p class="pp-warn">Profits exclude the job installation fee \u2014 no reaction system is `
    + `set, so the system cost index, facility tax and the 4% SCC are all uncounted. `
    + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Set your reaction system</button> `
    + `to price jobs properly.</p>`;
}

function _rxLoadOpportunities(force) {
  if (_rxOppsLoading) return _rxOppsLoading;
  if (_rxOppsLoaded && !force) return Promise.resolve(_rxOpps);
  _rxOppsLoading = api('/api/reactions/opportunities')
    .catch(e => { throw _rxErr(e, 'Load failed'); })
    .then(data => {
      _rxOpps = data.opportunities || [];
      // What the job installation fee was costed against. Unlike the manufacturing planner, an
      // unconfigured reaction system zeroes the WHOLE rate here (index + tax + the 4% SCC), so
      // these profits are quoted with no install fee at all — see _rxCostBasisWarn.
      _rxCostBasis = data.cost_basis || null;
      _rxOppsLoaded = true;
      _rxOppsLoading = null;
      return _rxOpps;
    })
    .catch(err => { _rxOppsLoading = null; throw err; });
  return _rxOppsLoading;
}

async function onReactionsTabOpen() {
  // First-run gate (local_market flag): block the whole tab until the user has added a character
  // and saved. Returns true when the gate is showing, in which case we skip loading the dashboard.
  if (await _rxApplyGate()) return;

  // Lazy, same idea as the shopping list fold: the Advanced table is collapsed by default and
  // its opportunity list is the single most expensive thing this tab can compute (full reaction
  // graph walk + a market fetch per candidate + job-cost ESI lookups) — only recompute it when
  // it's actually visible, not on every tab-open/post-assign refresh.
  const advDetails = document.getElementById('rxAdvancedDetails');
  if (advDetails && advDetails.open) _rxLoadAdvancedTable(true);
  _loadReactionsDashboard();
  _rxStructureRecommend();
  // Pull live job status from ESI in the background (respects ESI's ~5min cache server-side, so
  // flipping tabs won't hammer it) and reload the dashboard if anything actually refreshed. The
  // GET above only reads our cached job table — without this, a job you just installed in-game
  // never appears, since nothing else triggers the ESI fetch.
  _rxRefreshJobs(false);
  // Lazy: the shopping list is folded by default (see #rxShoppingDetails) and only worth
  // computing when actually visible — market-price lookups behind it can take a moment, and
  // most tab-opens/refreshes don't need this data recomputed at all. _onRxShoppingToggle
  // fetches it the first time it's unfolded; here we only re-fetch if it's ALREADY open (e.g.
  // this is a post-assign/cancel refresh and the user had it expanded), not on every tab open.
  const shopDetails = document.getElementById('rxShoppingDetails');
  if (shopDetails && shopDetails.open) _loadRxShoppingList();

  const ordersCard = document.getElementById('rxOrdersCard');
  if (ordersCard) {
    // Wait for the feature flags to be loaded before deciding — on a fresh page load _features
    // isn't populated yet, so _featureActive('reaction_orders') (an admin-preview flag) returns
    // false and the card stayed hidden until the tab was re-opened. Awaiting fixes that race.
    Promise.resolve(typeof _loadFeatures === 'function' ? _loadFeatures() : null).then(() => {
      const show = typeof _featureActive === 'function' && _featureActive('reaction_orders');
      ordersCard.style.display = show ? '' : 'none';
      if (show) _rxLoadOrders();
    });
  }
}

function _onRxAdvancedToggle(el) {
  if (el.open) _rxLoadAdvancedTable(true);
}

function _rxLoadAdvancedTable(force) {
  const el = document.getElementById('reactionsContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
  _rxLoadOpportunities(force)
    .then(() => _renderReactions())
    .catch(err => { el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`; });
}

function _onRxShoppingToggle(el) {
  if (el.open) _loadRxShoppingList();
}

let _rxLastShoppingList = [];
// Whether the list folds in customer-order assignments. Off by default, matching the endpoint:
// each order has its own correctly-sized materials report, and the two overlap by construction.
let _rxShoppingIncludeOrders = false;

function _toggleRxShoppingOrders() {
  _rxShoppingIncludeOrders = !_rxShoppingIncludeOrders;
  _loadRxShoppingList();
}

function _loadRxShoppingList() {
  const el = document.getElementById('rxShoppingListContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading shopping list…</div>';
  api('/api/reactions/shopping-list?include_orders=' + (_rxShoppingIncludeOrders ? 'true' : 'false'))
    .catch(() => ({ materials: [] }))
    .then(d => {
      _rxLastShoppingList = d.materials || [];
      if (!_rxLastShoppingList.length) {
        // "Nothing assigned" and "everything you have is on a customer order" are different
        // situations and used to render identically — the second one told a player with four
        // live assignments that they had none.
        const orders = d.order_count || 0;
        el.innerHTML = orders > 0
          ? `<div class="pp-empty">No speculative assignments — but ${orders} assignment${orders === 1 ? ' is' : 's are'}
             committed to customer orders, each with its own materials report on the order itself.
             <button class="pp-btn-link" onclick="_toggleRxShoppingOrders()">Include customer orders</button></div>`
          : '<div class="pp-empty">Nothing needed right now — nothing currently assigned.</div>';
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
      // Which market actually priced this line (structure / region / Jita) — surfaced as a badge
      // so it's obvious when the local/alliance market is being used vs falling back to Jita.
      const srcBadge = m => (m.market_name && m.market_name !== 'Group sheet')
        ? ` <span class="rx-price-source${m.market_name === 'Jita' ? ' rx-src-jita' : ''}">${_esc(m.market_name)}</span>` : '';
      const section = (title, items) => !items.length ? '' : `
        <div class="rx-shop-sec-title">${title} <span class="pp-card-hint">— ${Math.round(items.reduce((s, m) => s + (m.volume_m3 || 0), 0)).toLocaleString()} m³ total</span></div>
        <div style="overflow-x:auto">
          <table class="pp-card-table" style="width:100%">
            <thead><tr><th>Material</th><th>Quantity</th><th>Unit price</th><th>Est. cost</th><th>Volume</th></tr></thead>
            <tbody>${items.map(m => `<tr><td>${_esc(m.name)}${srcBadge(m)}</td><td>${_rxCopyQtyCell(m.quantity)}</td><td>${_fmtIsk(m.unit_cost)}${priceDiff(m)}</td><td>${_fmtIsk(m.unit_cost * m.quantity)}</td><td>${Math.round(m.volume_m3 || 0).toLocaleString()} m³</td></tr>`).join('')}</tbody>
          </table>
        </div>`;
      // Say plainly when order-linked work is folded in — this list and each order's own report
      // cover the same materials, so buying from both would double up.
      const scope = (d.order_count || 0) === 0 ? '' : (_rxShoppingIncludeOrders
        ? `<div class="pp-card-hint" style="margin-bottom:8px">Including ${d.order_count} customer-order assignment${d.order_count === 1 ? '' : 's'} — don't also buy from those orders' own reports.
           <button class="pp-btn-link" onclick="_toggleRxShoppingOrders()">Speculative only</button></div>`
        : `<div class="pp-card-hint" style="margin-bottom:8px">Speculative assignments only — ${d.order_count} more ${d.order_count === 1 ? 'is' : 'are'} committed to customer orders.
           <button class="pp-btn-link" onclick="_toggleRxShoppingOrders()">Include customer orders</button></div>`);
      el.innerHTML = scope
        + section('Fetch from your alliance', group)
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

// Shared "copy this TSV to the clipboard, then flash the button" behaviour for the three copy
// buttons below — they differ only in which rows/field they serialize, so only that one-line map
// stays per-caller.
function _rxCopyText(text, btn) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}

function _rxCopyReceivedDiff(btn) {
  _rxCopyText(_rxDiffStillShort.map(r => `${r.name}\t${r.remaining}`).join('\n'), btn);
}

function _rxCopyShoppingList(btn) {
  _rxCopyText(_rxLastShoppingList.map(m => `${m.name}\t${m.quantity}`).join('\n'), btn);
}

// Same "name <tab> quantity" copy pattern as the general shopping list, scoped to one order's
// own materials report (see _renderRxOrderDetail) — a customer order's needs are deliberately
// kept separate from the general list (see reactions_shopping_list's own docstring), so it gets
// its own copy button rather than reusing _rxLastShoppingList/_rxCopyShoppingList.
let _rxLastOrderMaterials = [];

function _rxCopyOrderMaterials(btn) {
  _rxCopyText(_rxLastOrderMaterials.map(m => `${m.name}\t${m.quantity}`).join('\n'), btn);
}

// "Copy all produced units" — the end result of every currently-running reaction, totalled per
// product across all characters (Σ runs × output_qty per run), as a "name <tab> quantity" TSV
// ready to paste into a multibuy/contract. Reads the last-rendered dashboard's running jobs.
// EXCLUDES intermediate reactions consumed by another running job (`j.consumed`, set server-side):
// their output feeds the next tier on-site, so pricing them would double-count value already in the
// final product — this is the "end result", not every work-in-progress unit.
function _rxCopyProducedUnits(btn) {
  const running = (_rxLastDashboardData && _rxLastDashboardData.running) || [];
  const byProduct = new Map();
  for (const j of running) {
    if (j.consumed) continue;
    const qty = (j.runs || 0) * (j.output_qty || 0);
    if (!qty) continue;
    const name = j.name || _rxProductName(j.product_type_id);
    byProduct.set(name, (byProduct.get(name) || 0) + qty);
  }
  const rows = [...byProduct.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  _rxCopyText(rows.map(([name, qty]) => `${name}\t${Math.round(qty)}`).join('\n'), btn);
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
  if (!_rxLastDashboardData) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
  api('/api/reactions/jobs')
    .catch(e => { throw _rxErr(e, 'Load failed'); })
    .then(data => { _rxLastDashboardData = data; _renderReactionsDashboard(data); })
    .catch(err => {
      el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
  // Lifetime ledger (forward-only turnover + net profit) — separate, cheap DB-only call; re-renders
  // the metrics once it lands so it never blocks the main dashboard.
  api('/api/reactions/lifetime').catch(() => null).then(lt => {
    if (lt) { _rxLifetime = lt; if (_rxLastDashboardData) _renderReactionsDashboard(_rxLastDashboardData); }
  }).catch(() => {});
}
let _rxLifetime = null;

// Trigger the ESI job-status fetch (POST /api/reactions/jobs/refresh) and reload the dashboard if
// it actually pulled anything. force=true (manual "Refresh jobs" button) bypasses the server's
// ~5min ESI-cache staleness guard; force=false (tab-open) lets the server skip characters still
// within ESI's cache window. Best-effort — a failed refresh just leaves the cached view in place.
function _rxRefreshJobs(force, btn) {
  const orig = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
  return apiSend('POST', '/api/reactions/jobs/refresh' + (force ? '?force=1' : ''))
    .catch(() => null)
    .then(res => { if (res && (res.characters_refreshed > 0 || force)) _loadReactionsDashboard(); })
    .catch(() => {})
    .finally(() => { if (btn) { btn.disabled = false; btn.textContent = orig; } });
}

// Adopt an orphan running job (installed in-game with no plan slot) into the recurring plan — the
// server costs it from the SDE recipe and creates the plan rows, after which it counts as planned
// (covers the running job now, reappears as "to install" and joins the shopping list next cycle).
function _rxAdoptOrphan(characterId, typeId, runs, btn) {
  if (btn) { btn.textContent = '…'; btn.style.pointerEvents = 'none'; }
  apiSend('POST', '/api/reactions/adopt-orphan',
          { character_id: Number(characterId), type_id: Number(typeId), runs: Number(runs) })
    .then(() => { _rxLastDashboardData = null; _loadReactionsDashboard(); })
    .catch(err => { toastError(err); if (btn) { btn.textContent = '⊕ plan'; btn.style.pointerEvents = ''; } });
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
  // todo line per (character, product) — even across different run-sizes, e.g. a batch split
  // unevenly across jobs — with a job-count/total-runs breakdown, instead of repeating the same
  // instruction over and over. Grouped by type_id (not name) so it can't accidentally merge two
  // different products that happen to share a display name.
  const todoGroups = new Map();
  const rows = tracked.map(c => {
    // Sorted by product name (running AND pending) so same-product squares cluster together in
    // the row instead of appearing in arbitrary insertion order — several interleaved products
    // was the actual "too messy to read off" complaint, not just the missing summary below.
    const _jobName = j => j.name || _rxProductName(j.product_type_id);
    const jobs = (jobsByChar.get(c.character_name) || [])
      .slice().sort((a, b) => _jobName(a).localeCompare(_jobName(b)));
    const pending = (c.pending || [])
      .slice().sort((a, b) => a.name.localeCompare(b.name) || a.runs - b.runs);
    const squares = jobs.map(j => {
      const icon = `https://images.evetech.net/types/${j.product_type_id}/icon?size=32`;
      const timer = j.hours_left != null ? _fmtHours(j.hours_left) : '—';
      const runsLabel = j.runs != null ? `×${j.runs}` : '';
      // An "orphan" is a job running in-game with no plan slot (installed outside this tool). It's
      // valued in the totals but is NOT part of the recurring loadout until adopted — the ⊕ badge
      // adds it to the plan so it re-appears as "to install" (and joins the shopping list) next cycle.
      const nm = _jobName(j);
      const tip = `${nm} — ${runsLabel ? runsLabel + ' runs — ' : ''}finished in ${timer}${j.facility_name ? ' — ' + j.facility_name : ''}${j.orphan ? ' — NOT in your plan (orphan)' : ''} — click for details`;
      const orphanBadge = j.orphan
        ? `<span class="rx-slot-orphan-badge" title="Not in your plan — click to add it so it recurs next cycle" onclick="event.stopPropagation();_rxAdoptOrphan(${j.character_id}, ${j.product_type_id}, ${j.runs || 0}, this)">⊕ plan</span>`
        : '';
      return `
        <div class="rx-slot rx-slot-filled${j.orphan ? ' rx-slot-orphan' : ''}" title="${_esc(tip)}" onclick="_rxOpenJobDetail(${j.product_type_id}, ${j.runs || 1}, ${j.progress_pct != null ? j.progress_pct : 'null'})">
          <div class="rx-slot-timer-corner" title="Finishes in ${_esc(timer)}">${_esc(timer)}</div>
          <img class="rx-slot-icon" src="${icon}" alt="" onerror="this.style.visibility='hidden'">
          ${runsLabel ? `<span class="rx-slot-runs">${_esc(runsLabel)}</span>` : ''}
          <div class="rx-slot-filled-label">${_esc(nm)}</div>
          ${orphanBadge}
        </div>`;
    });
    // Assigned (via the wizard's "Assign") but ESI hasn't confirmed it's actually running yet —
    // a red slashed-circle "you need to go do this" slot, distinct from a genuinely free one.
    // Click to cancel the assignment (e.g. changed your mind, or already did it under a
    // different product than planned).
    for (const a of pending) {
      const key = `${c.character_id}:${a.type_id}`;
      if (!todoGroups.has(key)) {
        todoGroups.set(key, {
          character_name: c.character_name, type_id: a.type_id, name: a.name,
          count: 0, totalRuns: 0, jobRuns: new Map(), ids: [],
        });
      }
      const g = todoGroups.get(key);
      g.count++;
      g.totalRuns += a.runs;
      g.jobRuns.set(a.runs, (g.jobRuns.get(a.runs) || 0) + 1);
      g.ids.push(a.assignment_id);
      const pendingIcon = `https://images.evetech.net/types/${a.type_id}/icon?size=32`;
      // A slot committed to a customer order (see the Customer orders card) carries the
      // order's client label — lets a player tell client-committed slots apart from
      // speculative-profit ones without opening the order itself.
      const orderTag = a.order_label ? `<div class="rx-order-slot-tag" title="Committed to a customer order">Order: ${_esc(a.order_label)}</div>` : '';
      const orderTip = a.order_label ? ` — for order "${a.order_label}"` : '';
      squares.push(`
        <div class="rx-slot rx-slot-pending" title="Not running yet — install ${_esc(a.name)} ×${a.runs} in-game${_esc(orderTip)}. Click to edit." onclick="_rxOpenEditAssign(${a.assignment_id}, '${c.character_id}')">
          <img class="rx-slot-icon" src="${pendingIcon}" alt="" onerror="this.style.visibility='hidden'">
          <span class="rx-slot-pending-badge" onclick="event.stopPropagation();_rxCancelAssignment(${a.assignment_id})" title="Cancel this assignment">⊘</span>
          <span class="rx-slot-runs">×${a.runs}</span>
          <div class="rx-slot-pending-label">${_esc(a.name)}</div>
          ${orderTag}
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

  // A sorted, one-line-per-(character, product) checklist — the per-slot squares above are
  // authoritative but easy to miscount at a glance once several products are interleaved across
  // many small squares; this is the "exactly what do I install, and how many of each" reference
  // to read off right before actually starting jobs in-game (real feedback: the square grid
  // alone was too messy for that). Purely informational, not a nagging banner — the
  // "Needs attention"-style banner this replaces a version of was removed for nagging about
  // things you hadn't gotten to yet; this is a checklist you consult once while installing, not
  // a persistent warning.
  const todoRows = [...todoGroups.values()]
    .sort((a, b) => a.character_name.localeCompare(b.character_name) || a.name.localeCompare(b.name));
  const todoListHtml = !todoRows.length ? '' : `
    <div class="pp-card-hint" style="font-weight:600;margin:2px 0 4px">
      To install — ${todoRows.reduce((s, g) => s + g.totalRuns, 0).toLocaleString()} runs across ${todoRows.length} product${todoRows.length === 1 ? '' : 's'}
    </div>
    <div style="overflow-x:auto;margin-bottom:12px">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Character</th><th>Product</th><th>Jobs</th><th>Total runs</th></tr></thead>
        <tbody>${todoRows.map(g => {
          const jobBreakdown = [...g.jobRuns.entries()].sort((a, b) => b[0] - a[0])
            .map(([runs, n]) => n > 1 ? `${n}×${runs} runs` : `${runs} runs`).join(' + ');
          return `
          <tr>
            <td>${_esc(g.character_name)}</td>
            <td><img src="https://images.evetech.net/types/${g.type_id}/icon?size=32" alt="" style="width:16px;height:16px;border-radius:3px;vertical-align:middle;margin-right:5px" onerror="this.style.display='none'">${_esc(g.name)}</td>
            <td>${_esc(jobBreakdown)}</td>
            <td><b>${g.totalRuns.toLocaleString()}</b></td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>`;

  // Easy at-a-glance numbers, first thing on the page — same big-number-tile pattern as the PI
  // Dashboard's own "Overview" row (_dashTile, dashboard.js), not a small text line buried below
  // the character loadout where it's easy to miss. "Jobs to install" is a plain count, not a
  // nagging alert — the standalone "Needs attention"-style banner this used to feed
  // (perpetually "Install X" for anything you just hadn't gotten to yet, with no natural
  // resolution) was removed 2026-07-13; each pending slot's own red ⊘ square in the loadout
  // below is reference enough.
  const pendingCount = [...todoGroups.values()].reduce((sum, g) => sum + g.count, 0);
  // "Middle of the road" completion, not the soonest: the earliest job finishing badly
  // under-represents when the whole batch is actually done. Use the MEDIAN running job — a real
  // job (real product + time), so it's honest rather than a synthetic average, and its name comes
  // straight from the backend now (was showing a raw "#16665" via the opportunity-list fallback).
  const _runTimed = (data.running || []).filter(r => r.hours_left != null).sort((a, b) => a.hours_left - b.hours_left);
  const _medJob = _runTimed.length ? _runTimed[Math.floor(_runTimed.length / 2)] : null;
  const timeLeftVal = _medJob ? _fmtHours(_medJob.hours_left) : '—';
  const _medName = _medJob ? (_medJob.name || _rxProductName(_medJob.product_type_id)) : '';
  const timeLeftLbl = _medJob
    ? `Time left · ${_esc(_medName)}${_runTimed.length > 1 ? ` (median of ${_runTimed.length})` : ''}`
    : 'Time left';

  const usedSlots = data.total_slots - data.free_slots;
  const overviewTiles = `<div class="an-stats">
      ${_dashTile(_fmtIsk(data.pending_isk_committed), 'ISK committed')}
      ${_dashTile(_fmtIsk(data.pending_output_value), 'Expected output value')}
      ${_dashTile(_fmtIsk(data.pending_net_profit_per_day), 'Expected profit / day', 'an-ok')}
      ${_dashTile(`${usedSlots}<span class="an-of"> / ${data.total_slots}</span>`, 'Slots used')}
      ${_dashTile(String(pendingCount), 'Jobs to install', pendingCount > 0 ? 'an-warn' : '')}
      ${_dashTile(timeLeftVal, timeLeftLbl)}
    </div>`;

  // Overall completion of everything currently running — a "total complete" bar under the tiles.
  const progressBar = data.running_progress_pct != null
    ? `<div class="rx-prog-wrap" style="margin-top:10px">${_rxProgressBar(data.running_progress_pct, 'Reactions complete')}</div>`
    : '';
  // "Copy all produced units" — the total end product of everything currently running, ready to
  // paste into a multibuy/contract. Only shown when something with a valued output is running.
  const hasProduced = (data.running || []).some(j => !j.consumed && (j.runs || 0) * (j.output_qty || 0) > 0);
  const producedBtn = hasProduced
    ? `<div style="margin-top:10px"><button class="pp-add-btn" onclick="_rxCopyProducedUnits(this)" title="Final products only — intermediate reactions consumed by another running job are excluded">Copy all produced units</button></div>`
    : '';
  // Lifetime ledger row — actual value PRODUCED by finished reactions (turnover) and net profit,
  // accumulated from real completions (forward-only). Distinct from the "Expected" tiles above,
  // which are the committed/in-flight pipeline. Shown once the ledger has anything in it.
  let lifetimeTiles = '';
  if (_rxLifetime && _rxLifetime.jobs > 0) {
    const since = _rxLifetime.since ? new Date(_rxLifetime.since * 1000).toLocaleDateString() : null;
    lifetimeTiles = `<div class="an-stats" style="margin-top:10px">
        ${_dashTile(_fmtIsk(_rxLifetime.turnover), 'Lifetime turnover' + (since ? ` · since ${since}` : ''))}
        ${_dashTile(_fmtIsk(_rxLifetime.net_profit), 'Lifetime net profit', 'an-ok')}
        ${_dashTile(_rxLifetime.jobs.toLocaleString(), 'Reactions completed')}
      </div>`;
  }
  if (metricsEl) metricsEl.innerHTML = overviewTiles + progressBar + producedBtn + lifetimeTiles;

  // Characters whose token lacks the structure-read scope AND have a job running (so an unresolved
  // "Structure #<id>" is actually visible) — one re-authorise adds the scope and resolves the names.
  const needReconnect = tracked.filter(c => c.needs_structures
    && (data.running || []).some(j => j.character_id === c.character_id));
  const reconnectNote = needReconnect.length
    ? `<div class="rx-reconnect-note">⚠ Facility names show as raw IDs for <b>${needReconnect.map(c => _esc(c.character_name)).join(', ')}</b> — <button type="button" class="rx-reconnect-btn" onclick="connectReactionsTracking()">reconnect</button> to resolve them.</div>`
    : '';

  el.innerHTML = reconnectNote + todoListHtml + rows + untrackedNote;
}

function _rxCancelAssignment(assignmentId) {
  apiSend('DELETE', `/api/reactions/assign/${assignmentId}`)
    .then(() => {
      if (!_rxLastDashboardData) return;
      // Optimistic in-place update instead of a full refetch+re-render — a full reload here
      // was visibly flickery/slow for something as small as clearing one pending slot.
      for (const c of _rxLastDashboardData.characters || []) {
        const before = (c.pending || []).length;
        c.pending = (c.pending || []).filter(p => p.assignment_id !== assignmentId);
        if (c.pending.length !== before) { c.free_slots++; _rxLastDashboardData.free_slots++; break; }
      }
      _renderReactionsDashboard(_rxLastDashboardData);
    })
    .catch(e => toastError(e, 'Could not clear that slot'));
}

function _rxClearAllAssignments() {
  apiSend('DELETE', '/api/reactions/assign')
    .then(() => { _rxLastDashboardData = null; _loadReactionsDashboard(); })
    .catch(e => toastError(e, 'Clear failed'));
}

// ── Manual "assign a product to this empty slot" modal ─────────────────────────────────────
// Picks from the same reachable/priced product list the "Advanced" opportunity table already
// has (_rxOpps) — so a manual pick gets the same real cost/profit numbers as an algorithm
// suggestion, not a guess. If the concurrent-jobs count is more than the clicked character has
// free, the extra jobs spill onto the account's other free slots (most-free-first), same idea
// as the factory-planet overflow elsewhere in this app — never silently overloads one character.
let _rxManualAssignCharId = null;
let _rxEditingAssignmentId = null;  // set only when opened via _rxOpenEditAssign

function _rxOpenManualAssign(characterId) {
  _rxManualAssignCharId = characterId;
  _rxEditingAssignmentId = null;
  document.getElementById('rxManualAssignTitle').firstChild.textContent = 'Assign a reaction';
  document.getElementById('rxManualAssignBtn').textContent = 'Assign';
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
  document.getElementById('rxManProduceChain').checked = true;
  document.getElementById('rxManChainRow').style.display = 'none';
  document.getElementById('rxManualAssignPreview').innerHTML = '';
  _rxHideProductDropdown();
  document.getElementById('rxManualAssignModal').style.display = '';
  _rxEnsureOppsLoadedForModal();
}

// A pending slot (assigned but not yet installed in-game) now opens this SAME modal pre-filled
// with its current product/runs, instead of clicking the slot itself deleting it outright — only
// the small ⊘ badge in the slot's corner still cancels directly (see the slot markup). Editing
// deletes the old assignment row and re-runs the normal allocation flow with the new values
// (there's no dedicated update endpoint — POST/DELETE already cover this exactly).
function _rxOpenEditAssign(assignmentId, characterId) {
  const chars = (_rxLastDashboardData && _rxLastDashboardData.characters) || [];
  const char = chars.find(c => String(c.character_id) === String(characterId));
  const a = char && (char.pending || []).find(p => p.assignment_id === assignmentId);
  if (!a) return;
  _rxManualAssignCharId = characterId;
  _rxEditingAssignmentId = assignmentId;
  document.getElementById('rxManualAssignTitle').firstChild.textContent = 'Edit reaction';
  document.getElementById('rxManualAssignBtn').textContent = 'Save changes';
  const hint = document.getElementById('rxManualAssignHint');
  if (hint) {
    hint.textContent = char
      ? `Editing this job on ${char.character_name}. Saving replaces just this one slot — raising "Concurrent jobs" adds more elsewhere if this character doesn't have room.`
      : 'Change the product or runs for this job.';
  }
  document.getElementById('rxManProduct').value = a.name;
  document.getElementById('rxManRuns').value = a.runs;
  document.getElementById('rxManJobs').value = 1;
  document.getElementById('rxManProduceChain').checked = true;
  document.getElementById('rxManChainRow').style.display = 'none';
  document.getElementById('rxManualAssignPreview').innerHTML = '';
  _rxHideProductDropdown();
  document.getElementById('rxManualAssignModal').style.display = '';
  _rxEnsureOppsLoadedForModal(() => _rxManualAssignPreview());
}

function _rxEnsureOppsLoadedForModal(onLoaded) {
  const status = document.getElementById('rxManualAssignStatus');
  if (_rxOppsLoaded) { status.textContent = ''; if (onLoaded) onLoaded(); return; }
  // Not loaded yet (e.g. the Advanced table was never opened this visit) — fetch it now and,
  // if the search dropdown is still open by the time it resolves, refresh it in place instead
  // of leaving a stale "still loading" state that never updates on its own.
  status.textContent = 'Loading the product list…';
  _rxLoadOpportunities()
    .then(() => {
      status.textContent = '';
      const dd = document.getElementById('rxManProductDropdown');
      if (dd && dd.style.display !== 'none') _rxProductDropdownFilter();
      if (onLoaded) onLoaded();
    })
    .catch(err => { status.textContent = err.message; });
}

function _rxCloseManualAssign() {
  document.getElementById('rxManualAssignModal').style.display = 'none';
  _rxHideProductDropdown();
  _rxEditingAssignmentId = null;
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
  // Escape closes just this dropdown — stop it bubbling to the global handler that would
  // otherwise also close the whole modal.
  if (event.key === 'Escape') { event.stopPropagation(); _rxHideProductDropdown(); return; }
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

// The chain tiers (intermediate reactions this product's own formula needs, e.g. goo ->
// Ferrofluid -> this product) come off the opportunity at ITS max batch (top_level_runs) — scale
// each tier's run count by the same ratio as everything else, ceil'd since a reaction run count
// can't be fractional, floored at 1 (any nonzero need still means "install this once").
function _rxScaledChainTiers(o, scale) {
  return (o.chain_tiers || []).map(t => ({
    type_id: t.type_id, name: t.name, runs: Math.max(1, Math.ceil(t.runs * scale)), job_count: 1,
  }));
}

function _rxChainCheckboxChecked() {
  const cb = document.getElementById('rxManProduceChain');
  return !cb || cb.checked;
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
  const chainRow = document.getElementById('rxManChainRow');
  if (chainRow) chainRow.style.display = (o && o.chain_tiers && o.chain_tiers.length) ? '' : 'none';
  if (!o || runsPerJob <= 0 || jobs <= 0 || !o.top_level_runs) { el.innerHTML = ''; return; }
  const totalRuns = runsPerJob * jobs;
  const scale = totalRuns / o.top_level_runs;
  const outputQty = o.output_qty * scale;
  // Input/job cost already reflect the FULL rolled-up cost of producing the chain internally
  // (see app.reactions._resolve_reachable) regardless of whether assignment rows get created
  // for it — the "produce chain" checkbox only controls the reminder/slot-reservation, not the
  // cost math, since either way you're consuming the same materials to get there.
  const fixedCosts = (o.input_cost + (o.job_cost || 0) + o.shipping_cost + o.collateral_cost) * scale;
  const outputValue = o.instant_sell_value * scale;
  const profit = outputValue - fixedCosts;
  const breakEven = outputQty > 0 ? fixedCosts / outputQty : 0;
  const runtimeHours = o.cycle_time ? (o.cycle_time / 3600) * runsPerJob : 0;
  const allChainTiers = _rxScaledChainTiers(o, scale);
  const produceChain = _rxChainCheckboxChecked();
  const chainNote = !allChainTiers.length ? '' : produceChain
    ? `<div class="rx-manual-preview-chain">Also needs ${allChainTiers.length} intermediate reaction${allChainTiers.length === 1 ? '' : 's'} first — each takes its own job slot on the same character: ${allChainTiers.map(t => `<b>${_esc(t.name)}</b> ×${t.runs}`).join(', ')}.</div>`
    : `<div class="rx-manual-preview-chain">Not producing the chain — you'll need ${allChainTiers.map(t => _esc(t.name)).join(', ')} already in stock, or this job can't actually be installed.</div>`;
  el.innerHTML = `
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Input cost</span><b>${_fmtIsk(fixedCosts)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Runtime per job</span><b>${_fmtHours(runtimeHours)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Output value</span><b>${_fmtIsk(outputValue)} <span class="rx-manual-preview-units">(${Math.round(outputQty).toLocaleString()} units)</span></b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Profit</span><b class="${profit >= 0 ? 'an-ok' : 'an-warn'}">${_fmtIsk(profit)}</b></div>
      <div class="rx-manual-preview-breakeven">Sell for at least <b>${_fmtIsk(breakEven)}</b>/unit to break even at today's material, shipping${o.job_cost ? ', job' : ''} and collateral cost.</div>
      ${chainNote}
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
  const scale = (runsPerJob * jobsWanted) / o.top_level_runs;
  const chainTiers = _rxChainCheckboxChecked() ? _rxScaledChainTiers(o, scale) : [];
  const chainJobs = chainTiers.length;  // 1 job slot each — see _rxScaledChainTiers

  const chars = ((_rxLastDashboardData && _rxLastDashboardData.characters) || []).filter(c => c.tracked && c.slots > 1);
  const clicked = chars.find(c => String(c.character_id) === String(_rxManualAssignCharId));
  const others = chars.filter(c => String(c.character_id) !== String(_rxManualAssignCharId))
    .sort((a, b) => b.free_slots - a.free_slots);
  const ordered = clicked ? [clicked, ...others] : others;
  const freeLeft = new Map(ordered.map(c => [c.character_id, c.free_slots]));
  // Editing replaces one existing slot — the old assignment still occupies it in the cached
  // free_slots count until the delete below actually happens, so credit that slot back up front
  // rather than have the allocation think it's short by one when nothing's actually changed.
  if (_rxEditingAssignmentId && clicked) {
    freeLeft.set(clicked.character_id, freeLeft.get(clicked.character_id) + 1);
  }

  // The chain (if any) has to sit on ONE character alongside at least 1 of the top-level jobs —
  // its intermediate output has to be right there to feed the final reaction, this tool doesn't
  // model shipping half-finished materials between characters. Prefer the clicked character;
  // fall back to whichever tracked character actually has the room.
  let primary = null;
  if (chainJobs > 0) {
    primary = ordered.find(c => freeLeft.get(c.character_id) >= chainJobs + 1) || null;
    if (!primary) {
      const names = chainTiers.map(t => t.name).join(', ');
      status.textContent = `This needs ${chainJobs} intermediate reaction${chainJobs === 1 ? '' : 's'} first (${names}) plus at least 1 slot for the product itself, all on one character — none of your tracked characters has ${chainJobs + 1} free slots. Free up slots or lower the run count.`;
      return;
    }
  }

  const allocations = [];  // { char, jobs, chain_tiers }
  let remaining = jobsWanted;
  if (primary) {
    const take = Math.min(remaining, freeLeft.get(primary.character_id) - chainJobs);
    freeLeft.set(primary.character_id, freeLeft.get(primary.character_id) - chainJobs - take);
    allocations.push({ char: primary, jobs: take, chain_tiers: chainTiers });
    remaining -= take;
  }
  for (const c of ordered) {
    if (remaining <= 0) break;
    if (c === primary) continue;
    const avail = freeLeft.get(c.character_id);
    const take = Math.min(remaining, avail);
    if (take > 0) { freeLeft.set(c.character_id, avail - take); allocations.push({ char: c, jobs: take, chain_tiers: [] }); remaining -= take; }
  }

  if (!allocations.length) { status.textContent = 'No free reaction slots on any tracked character.'; return; }
  if (remaining > 0) {
    const chainNote = chainJobs > 0 ? ` (after reserving ${chainJobs} for the intermediate chain)` : '';
    if (!confirm(`Only ${jobsWanted - remaining} of ${jobsWanted} jobs fit across your free slots right now${chainNote}. Assign what fits?`)) return;
  }

  status.textContent = _rxEditingAssignmentId ? 'Saving…' : 'Assigning…';
  // Editing = delete the old row, then run the exact same create flow with the new values —
  // there's no dedicated update endpoint, and this is simpler than one that would have to
  // handle the same job-count-changed/chain-changed cases this already handles for a fresh
  // assign. If the delete fails, don't touch anything else.
  const deleteOld = _rxEditingAssignmentId
    ? apiSend('DELETE', `/api/reactions/assign/${_rxEditingAssignmentId}`)
        .catch(() => { throw new Error('Could not delete the old assignment'); })
    : Promise.resolve();

  deleteOld
    .then(() => Promise.all(allocations.map(a => apiSend('POST', '/api/reactions/assign', {
      character_id: a.char.character_id, type_id: o.type_id, name: o.name,
      runs: a.jobs * runsPerJob, job_count: a.jobs,
      input_cost: costPerRun * a.jobs * runsPerJob, reward: rewardPerRun * a.jobs * runsPerJob,
      chain_tiers: a.chain_tiers,
    }).catch(() => { throw new Error('Assign failed'); }))))
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
    api('/api/moon-goo').catch(() => ({ prices: [] })),
    api('/api/reactions/fuel-blocks').catch(() => ({ fuel_blocks: [] })),
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
    toast(`At least one ${label} must stay checked — every reaction needs one, so leaving none checked would make nothing suggestible at all.`, 'error', 7000);
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

// Live label for the "Market fill" slider — what share of a product's market trade volume a
// suggested batch may be. The slider IS the source of truth (read fresh in wizRSuggest); the
// advisor's per-product "Fill N%" Apply just moves it.
function _wizRFillLive() {
  const s = document.getElementById('wizRFill');
  const out = document.getElementById('wizRFillFmt');
  if (s && out) out.textContent = `${s.value}%`;
}

function wizRSuggest() {
  const isk = parseFloat(document.getElementById('wizRIsk').value) || 0;
  const depth = parseInt(document.getElementById('wizRDepth').value, 10) || 2;
  const cadence = parseFloat(document.getElementById('wizRCadence').value) || 168;
  const fillEl = document.getElementById('wizRFill');
  const absorbFraction = fillEl ? (parseFloat(fillEl.value) || 50) / 100 : null;
  const materialIds = _rxSelectedMaterialIds();
  const el = document.getElementById('wizRSuggestionsContent');
  wizRGo(2);
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Crunching the numbers…</div>';
  apiSend('POST', '/api/reactions/suggest', { isk_budget: isk, max_chain_depth: depth,
      cadence_hours: cadence, material_ids: materialIds, absorb_fraction: absorbFraction })
    .catch(e => { throw _rxErr(e, 'Suggest failed'); })
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
  // Echo the current Market-fill slider setting on the results page (the slider itself lives on the
  // input page), with a one-click way back to default when it's been moved off 50%.
  const absorbNote = (t.absorb_fill_pct != null && t.absorb_fill_pct !== 50)
    ? `<div style="margin-bottom:10px;color:var(--clr-text-soft);font-size:0.9em">Market fill set to <b style="color:var(--clr-text-bright)">${t.absorb_fill_pct}%</b> of each product's weekly trade volume — <button type="button" onclick="_rxResetAbsorbFraction()" style="background:none;border:none;padding:0;font:inherit;color:var(--clr-accent);text-decoration:underline;cursor:pointer">back to default</button></div>`
    : '';
  const budgetSummary = `<div class="pp-card-hint" style="margin-bottom:10px">${bindingNote}</div>${absorbNote}`;

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

// Reset the slider back to the 50% default and re-run.
function _rxResetAbsorbFraction() {
  const s = document.getElementById('wizRFill');
  if (s) { s.value = 50; _wizRFillLive(); }
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
  return apiSend('POST', '/api/reactions/assign', {
    character_id: s.assigned_character_id, type_id: s.type_id, name: s.name,
    runs: s.runs, job_count: s.job_count || 1, input_cost: s.input_cost, reward: s.reward,
    chain_tiers: s.chain_tiers || [],
  })
    .then(() => {
      // There used to be an `if (!r.ok) throw new Error()` here — a leftover from when this called
      // fetch() directly. `r` does not exist in this scope, so it threw a ReferenceError on every
      // SUCCESSFUL assign, the catch below relabelled the row "Retry", and "Assign all" reported
      // "Some failed". The POST had already committed, so each retry appended another full set of
      // assignment rows: two suggestions retried a few times became 27 rows on a 10-slot character
      // (reported 2026-08-01). apiSend() already rejects on a non-2xx, so reaching here IS success.
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
  const head = _RX_CORE_COLUMNS.map(c => {
    const active = c.key === _rxSortKey;
    const arrow = active ? (_rxSortDir === 1 ? ' ▲' : ' ▼') : '';
    return `<th onclick="_rxSortBy('${c.key}')" style="cursor:pointer;white-space:nowrap">${_esc(c.label)}${arrow}</th>`;
  }).join('');
  const body = rows.map((o, i) => {
    const expanded = _rxExpandedOpps.has(o.type_id);
    const cells = _RX_CORE_COLUMNS.map((c, ci) => {
      const v = c.fmt(o[c.key], o);
      return `<td>${ci === 0 ? `<span class="rx-fold-caret${expanded ? ' rx-fold-caret-open' : ''}">▸</span>${v}` : v}</td>`;
    }).join('');
    const mainRow = `<tr class="rx-opp-row" onclick="_rxToggleOppDetail(${o.type_id})">${cells}</tr>`;
    if (!expanded) return mainRow;
    const chips = _RX_DETAIL_FIELDS.map(f => `
      <span class="rx-opp-detail-chip"><span class="rx-manual-preview-label">${_esc(f.label)}</span> <b>${f.fmt(o[f.key], o)}</b></span>`).join('');
    const detailRow = `<tr class="rx-opp-detail-row"><td colspan="${_RX_CORE_COLUMNS.length}"><div class="rx-opp-detail">${chips}</div></td></tr>`;
    return mainRow + detailRow;
  }).join('');
  el.innerHTML = _rxCostBasisWarn() + `
    <div style="overflow-x:auto">
      <table class="pp-card-table" style="width:100%">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    <div class="pp-card-hint" style="margin-top:8px">
      ${rows.length} opportunit${rows.length === 1 ? 'y' : 'ies'} · click a row for steps, job cost, sell-order
      price/profit, ISK/m³ and market depth · Output value/Profit use the instant-sell price ·
      Ship+collateral uses the configured import/export rates.
    </div>`;
}

// ── Settings modal (shipping/collateral/job-cost/time-efficiency) — a real "⚙ Settings" button
// on the Metrics card, not buried at the bottom of the (collapsed-by-default) Advanced table,
// which real feedback confirmed was undiscoverable ("Why is it there? That doesn't help me at
// all.") once time-efficiency became something people actually need to configure up front.
function _rxOpenSettingsModal() {
  const el = document.getElementById('rxSettingsModalContent');
  // Markets + your personal freight/system rates moved to the global Settings → Markets & Logistics
  // section (shared with Manufacturing). Leave a redirect here; keep the group-manager DEFAULTS
  // (an alliance-wide setting) in Reactions where group management lives.
  const redirect = `<div class="pp-card-hint" style="margin-bottom:12px">Your markets &amp; freight/system rates moved to `
    + `<button class="ind-link-btn" onclick="_rxCloseSettingsModal();openSettingsModal('markets')">Settings → Markets &amp; Logistics</button>`
    + ` — now shared with the Manufacturing planner.</div>`;
  el.innerHTML = redirect + (_rxCanEditSettings() ? _rxSettingsFormHtml() : '');
  document.getElementById('rxSettingsModal').style.display = '';
  if (_rxCanEditSettings()) _loadRxSettings();
}

function _rxCloseSettingsModal() {
  document.getElementById('rxSettingsModal').style.display = 'none';
  document.getElementById('rxSettingsModalContent').innerHTML = '';  // drop stale market-manager IDs
  if (_rxMarketMount === 'rxSettingsMarkets') _rxMarketMount = null;
  _rxApplyGate();  // refresh the top summary bar / gate state
}

// Site admins can always preview/edit; a non-admin sees the form only if they manage at least
// one group — GET/PUT /api/reactions/settings always resolves to THEIR OWN group (via
// membership), so this is just a visibility check, not a scoping one.
function _rxCanEditSettings() {
  return (typeof _isAdmin !== 'undefined' && _isAdmin) || (typeof _isGroupManager !== 'undefined' && _isGroupManager);
}

// Round-number quick picks, not a claim about any specific EVE rig/skill combo — the real
// stacking math for reactor time-efficiency (rigs + skills + structure/security bonuses) isn't
// something we can verify precisely enough to label "T1"/"T2" etc. with confidence, so this is a
// convenience picker over the underlying %, not an authoritative lookup table. "Custom" always
// reveals the exact-number field for whatever a player has actually measured against their real
// in-game job duration (see the caveat text below the field).
const _RX_TIME_EFF_PRESETS = [0, 10, 20, 30, 40, 50, 60, 70];

function _rxTimeEffFieldHtml(prefix, label) {
  const opts = _RX_TIME_EFF_PRESETS.map(p => `<option value="${p}">${p}%</option>`).join('');
  return `
      <label class="pp-label" for="${prefix}TimeEffPreset" title="How much shorter real reaction job duration is than the raw formula time, from reactor efficiency rigs/skills/structure bonuses">${label}</label>
      <div style="display:flex;align-items:center;gap:8px">
        <select id="${prefix}TimeEffPreset" class="pp-select" style="width:140px" onchange="_rxTimeEffPresetChanged('${prefix}')">
          ${opts}
          <option value="custom">Custom…</option>
        </select>
        <input type="number" id="${prefix}TimeEff" class="pp-num-input" style="width:90px;display:none" step="0.1" min="0" max="99" oninput="document.getElementById('${prefix}TimeEffPreset').value='custom'">
      </div>`;
}

// Selecting a preset hides the free-entry field and syncs its value (still the thing actually
// sent on Save); picking "Custom" reveals it for an exact measured figure.
function _rxTimeEffPresetChanged(prefix) {
  const preset = document.getElementById(`${prefix}TimeEffPreset`).value;
  const input = document.getElementById(`${prefix}TimeEff`);
  if (preset === 'custom') {
    input.style.display = '';
    input.focus();
  } else {
    input.style.display = 'none';
    input.value = preset;
  }
}

// Sets both the preset dropdown and the underlying field from a saved fraction (0-1) — selects
// the matching preset if the value lands on one exactly, else falls back to "Custom" with the
// field shown so the precise figure is still visible, not silently rounded away.
function _rxSetTimeEffValue(prefix, pct) {
  const rounded = Math.round(pct * 10) / 10;
  document.getElementById(`${prefix}TimeEff`).value = rounded;
  const presetSelect = document.getElementById(`${prefix}TimeEffPreset`);
  const match = _RX_TIME_EFF_PRESETS.includes(rounded);
  presetSelect.value = match ? String(rounded) : 'custom';
  document.getElementById(`${prefix}TimeEff`).style.display = match ? 'none' : '';
}

function _rxSettingsFormHtml() {
  return `
    <div class="pp-card-title" style="font-size:13px;margin-top:4px">Group defaults
      <span class="pp-card-hint">— your group's shared shipping/collateral/job-cost rate; members inherit this unless they set their own below</span>
    </div>
    <div class="pp-target-form" style="margin-top:8px">
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

      ${_rxTimeEffFieldHtml('rxSet', 'Time efficiency %')}
    </div>
    <div class="pp-card-hint" style="margin-top:2px">Reaction system + facility tax estimate real job-installation fees (EVE's system cost index × EIV, plus your structure's tax). Leave the system blank to skip this — nothing changes until it's set. Time efficiency % shortens every duration/runtime estimate the tool shows — there's no way to auto-detect this (ESI only reports a job's facility once it's already installed), so check your real in-game job duration against the formula's raw time and enter the % difference here.</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
      <button onclick="_saveRxSettings()">Save</button>
      <span id="rxSettingsMsg" class="pp-card-hint"></span>
    </div>
    <div style="border-top:1px solid var(--clr-border);margin-top:16px"></div>`;
}

function _loadRxSettings() {
  api('/api/reactions/settings').catch(() => null).then(s => {
    if (!s) return;
    document.getElementById('rxSetImport').value = s.import_isk_per_m3;
    document.getElementById('rxSetExport').value = s.export_isk_per_m3;
    document.getElementById('rxSetCollateral').value = (s.export_collateral_pct * 100).toFixed(2);
    document.getElementById('rxSetSystem').value = s.reaction_system || '';
    document.getElementById('rxSetTax').value = ((s.facility_tax_pct || 0) * 100).toFixed(2);
    _rxSetTimeEffValue('rxSet', (s.time_efficiency_pct || 0) * 100);
  });
}

function _saveRxSettings() {
  const msg = document.getElementById('rxSettingsMsg');
  apiSend('PUT', '/api/reactions/settings', {
      import_isk_per_m3: parseFloat(document.getElementById('rxSetImport').value) || 0,
      export_isk_per_m3: parseFloat(document.getElementById('rxSetExport').value) || 0,
      export_collateral_pct: (parseFloat(document.getElementById('rxSetCollateral').value) || 0) / 100,
      reaction_system: document.getElementById('rxSetSystem').value.trim() || null,
      facility_tax_pct: (parseFloat(document.getElementById('rxSetTax').value) || 0) / 100,
      time_efficiency_pct: (parseFloat(document.getElementById('rxSetTimeEff').value) || 0) / 100,
  })
    .then(() => { msg.textContent = 'Saved.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}

// Every logged-in user (not just group managers) can override their OWN shipping/collateral
// rate — JF cost genuinely varies account-to-account (home system, courier arrangement) even
// within one alliance. No override saved = use the group's rate (or the global default).
function _rxAccountSettingsFormHtml() {
  return `
    <div class="pp-card-title" style="font-size:13px;margin-top:4px">Your settings
      <span class="pp-card-hint">— overrides the group default above for you personally only (real JF cost/reaction system/rig fit genuinely varies account to account)</span>
    </div>
    <div class="pp-target-form" style="margin-top:8px">
      <div class="pp-card-hint" id="rxAcctSettingsHint" style="margin-bottom:8px;grid-column:1/-1"></div>
      <label class="pp-label" for="rxAcctImport">Your import ISK/m³</label>
      <input type="number" id="rxAcctImport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxAcctExport">Your export ISK/m³</label>
      <input type="number" id="rxAcctExport" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="rxAcctCollateral">Your export collateral %</label>
      <input type="number" id="rxAcctCollateral" class="pp-num-input" style="width:100px" step="0.1">

      <label class="pp-label" for="rxAcctSystem" title="Drives job-installation fees for reactions AND manufacturing — the Industry planner reads this same value">Your reaction / build system</label>
      <input type="text" id="rxAcctSystem" class="pp-num-input" style="width:120px" placeholder="e.g. Jita">

      <label class="pp-label" for="rxAcctTax">Your facility tax %</label>
      <input type="number" id="rxAcctTax" class="pp-num-input" style="width:100px" step="0.1">

      ${_rxTimeEffFieldHtml('rxAcct', 'Your time efficiency %')}
    </div>
    <div class="pp-card-hint" style="margin-top:2px">System + facility tax estimate real job-installation fees, for <b>manufacturing as well as reactions</b> — the Industry planner reads this same setting, and quotes fees light without it. Leave the system blank to skip this — nothing changes until it's set. Time efficiency % shortens every duration/runtime estimate — check your real in-game job duration against the formula's raw time and enter the % difference (we can't auto-detect this, ESI only reports a job's facility once it's already installed).</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
      <button onclick="_saveRxAccountSettings()">Save my rate</button>
      <button class="pp-cancel-btn" onclick="_resetRxAccountSettings()">Use default instead</button>
      <span id="rxAcctSettingsMsg" class="pp-card-hint"></span>
    </div>`;
}

function _loadRxAccountSettings() {
  api('/api/reactions/account-settings').catch(() => null).then(s => {
    if (!s) return;
    const eff = s.override || s.default;
    document.getElementById('rxAcctImport').value = eff.import_isk_per_m3;
    document.getElementById('rxAcctExport').value = eff.export_isk_per_m3;
    document.getElementById('rxAcctCollateral').value = (eff.export_collateral_pct * 100).toFixed(2);
    document.getElementById('rxAcctSystem').value = eff.reaction_system || '';
    document.getElementById('rxAcctTax').value = ((eff.facility_tax_pct || 0) * 100).toFixed(2);
    _rxSetTimeEffValue('rxAcct', (eff.time_efficiency_pct || 0) * 100);
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
  apiSend('PUT', '/api/reactions/account-settings', {
      import_isk_per_m3: parseFloat(document.getElementById('rxAcctImport').value) || 0,
      export_isk_per_m3: parseFloat(document.getElementById('rxAcctExport').value) || 0,
      export_collateral_pct: (parseFloat(document.getElementById('rxAcctCollateral').value) || 0) / 100,
      reaction_system: document.getElementById('rxAcctSystem').value.trim() || null,
      facility_tax_pct: (parseFloat(document.getElementById('rxAcctTax').value) || 0) / 100,
      time_efficiency_pct: (parseFloat(document.getElementById('rxAcctTimeEff').value) || 0) / 100,
  })
    .then(() => { msg.textContent = 'Saved.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}

function _resetRxAccountSettings() {
  const msg = document.getElementById('rxAcctSettingsMsg');
  apiSend('DELETE', '/api/reactions/account-settings')
    .catch(() => { throw new Error('Reset failed'); })
    .then(() => { msg.textContent = 'Reverted to default.'; onReactionsTabOpen(); })
    .catch(err => { msg.textContent = err.message; });
}

// ── Customer orders: a fixed number of finished units for another player ───────────────────
// A different framing from the day-cadence wizard above — "make me N units" instead of "keep me
// busy at a good ISK/day." Persistent (list + detail fetched fresh each time, not a one-shot
// calculator) and committing to it occupies real reaction slots the same way the suggestion/
// manual-assign flow does (see app.reactions._allocate_and_insert).

let _rxOrders = [];

function _rxLoadOrders() {
  const el = document.getElementById('rxOrdersContent');
  if (!el) return;
  api('/api/reactions/orders')
    .catch(e => { throw _rxErr(e, 'Load failed'); })
    .then(data => { _rxOrders = data.orders || []; _renderRxOrdersList(_rxOrders); })
    .catch(err => { el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`; });
}

function _rxOrderBarHtml(o) {
  const pct = o.top_level_runs > 0 ? Math.min(100, Math.round(100 * o.assigned_runs / o.top_level_runs)) : 0;
  return `<div class="rx-order-bar" title="${pct}% of runs assigned"><div class="rx-order-bar-fill" style="width:${pct}%"></div></div>`;
}

function _renderRxOrdersList(orders) {
  const el = document.getElementById('rxOrdersContent');
  if (!el) return;
  const open = orders.filter(o => o.status === 'open');
  const history = orders.filter(o => o.status !== 'open');
  // Default fold state: collapsed when there's nothing open to act on, expanded when there is.
  // Only sets the default (on tab-open / after an order mutation) — the user can still fold/unfold.
  const det = document.getElementById('rxOrdersDetails');
  if (det) det.open = open.length > 0;
  if (!orders.length) { el.innerHTML = '<div class="pp-empty">No customer orders yet — "+ New order" to track one.</div>'; return; }
  const row = o => `
    <div class="rx-order-row" onclick="_rxOpenOrderDetail(${o.id})">
      <div class="rx-order-info">
        <div class="rx-order-name">${_esc(o.name)} <span class="pp-card-hint">× ${Math.round(o.target_qty).toLocaleString()} units</span></div>
        <div class="pp-card-hint">${o.client_name ? _esc(o.client_name) + ' · ' : ''}${o.assigned_runs.toLocaleString()} / ${o.top_level_runs.toLocaleString()} runs assigned${o.status !== 'open' ? ' · ' + _esc(o.status) : ''}</div>
      </div>
      ${_rxOrderBarHtml(o)}
    </div>`;
  const openHtml = open.length ? open.map(row).join('') : '<div class="pp-empty">No open orders.</div>';
  const historyHtml = history.length
    ? `<details style="margin-top:10px"><summary class="pp-card-hint" style="cursor:pointer">History (${history.length})</summary>${history.map(row).join('')}</details>`
    : '';
  el.innerHTML = openHtml + historyHtml;
}

// ── New order modal — a separate small product-search combobox from the manual-assign one
// above (new element ids), same datalist-search pattern, reusing the already-loaded _rxOpps list.

function _rxOpenNewOrderModal() {
  document.getElementById('rxOrderProduct').value = '';
  document.getElementById('rxOrderQty').value = 100;
  document.getElementById('rxOrderClient').value = '';
  document.getElementById('rxOrderNotes').value = '';
  document.getElementById('rxOrderCreateStatus').textContent = '';
  _rxOrderResetReview();
  _rxOrderHideProductDropdown();
  document.getElementById('rxNewOrderModal').style.display = '';
  const status = document.getElementById('rxOrderCreateStatus');
  if (!_rxOppsLoaded) {
    status.textContent = 'Loading the product list…';
    _rxLoadOpportunities()
      .then(() => { status.textContent = ''; })
      .catch(err => { status.textContent = err.message; });
  }
}

function _rxCloseNewOrderModal() {
  document.getElementById('rxNewOrderModal').style.display = 'none';
  _rxOrderHideProductDropdown();
}

function _rxOrderProductMatch() {
  const name = document.getElementById('rxOrderProduct').value.trim();
  return _rxOpps.find(o => o.name === name) || null;
}

let _rxOrderProductDropdownList = [];
let _rxOrderProductDropdownIdx = -1;

function _rxOrderProductDropdownFilter() {
  const input = document.getElementById('rxOrderProduct');
  const dd = document.getElementById('rxOrderProductDropdown');
  if (!input || !dd) return;
  _rxOrderResetReview();   // product changed → the shown review no longer matches; force a re-review
  const q = input.value.trim().toLowerCase();
  const all = [..._rxOpps].sort((a, b) => a.name.localeCompare(b.name));
  _rxOrderProductDropdownList = (q ? all.filter(o => o.name.toLowerCase().includes(q)) : all).slice(0, 200);
  _rxOrderProductDropdownIdx = -1;
  if (!_rxOrderProductDropdownList.length) {
    dd.innerHTML = `<div class="rx-man-product-empty">${_rxOpps.length ? 'No matching product.' : 'Loading products…'}</div>`;
  } else {
    dd.innerHTML = _rxOrderProductDropdownList.map((o, i) => `
      <div class="rx-man-product-row" data-idx="${i}" onmousedown="event.preventDefault();_rxOrderSelectProduct(${i})">
        <img src="https://images.evetech.net/types/${o.type_id}/icon?size=32" alt="" onerror="this.style.visibility='hidden'">
        ${_esc(o.name)}
      </div>`).join('');
  }
  dd.style.display = '';
}

function _rxOrderSelectProduct(idx) {
  const o = _rxOrderProductDropdownList[idx];
  if (!o) return;
  document.getElementById('rxOrderProduct').value = o.name;
  _rxOrderHideProductDropdown();
}

function _rxOrderHideProductDropdown() {
  const dd = document.getElementById('rxOrderProductDropdown');
  if (dd) dd.style.display = 'none';
  _rxOrderProductDropdownIdx = -1;
}

function _rxOrderProductDropdownKey(event) {
  const dd = document.getElementById('rxOrderProductDropdown');
  if (!dd || dd.style.display === 'none') return;
  // Escape closes just this dropdown — don't let it bubble to the global modal-close handler.
  if (event.key === 'Escape') { event.stopPropagation(); _rxOrderHideProductDropdown(); return; }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!_rxOrderProductDropdownList.length) return;
    const dir = event.key === 'ArrowDown' ? 1 : -1;
    _rxOrderProductDropdownIdx = (_rxOrderProductDropdownIdx + dir + _rxOrderProductDropdownList.length) % _rxOrderProductDropdownList.length;
    [...dd.children].forEach((el, i) => el.classList.toggle('rx-man-product-active', i === _rxOrderProductDropdownIdx));
    dd.children[_rxOrderProductDropdownIdx].scrollIntoView({ block: 'nearest' });
  } else if (event.key === 'Enter') {
    event.preventDefault();
    _rxOrderSelectProduct(_rxOrderProductDropdownIdx >= 0 ? _rxOrderProductDropdownIdx : 0);
  }
}

// Review resets whenever the product or quantity changes, so a shown "Create order" always matches
// the report the user actually reviewed (client/notes don't affect the report, so they don't reset).
function _rxOrderResetReview() {
  const rv = document.getElementById('rxOrderReview');
  if (rv) rv.innerHTML = '';
  const btn = document.getElementById('rxOrderCreateBtn');
  if (btn) btn.style.display = 'none';
}

function _rxReviewOrder() {
  const status = document.getElementById('rxOrderCreateStatus');
  const o = _rxOrderProductMatch();
  if (!o) { status.textContent = 'Pick a product from the list.'; return; }
  const qty = parseFloat(document.getElementById('rxOrderQty').value);
  if (!qty || qty <= 0) { status.textContent = 'Enter how many units the client wants.'; return; }
  status.textContent = '';
  const rv = document.getElementById('rxOrderReview');
  rv.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Working out the order…</div>';
  document.getElementById('rxOrderCreateBtn').style.display = 'none';
  apiSend('POST', '/api/reactions/orders/preview', { type_id: o.type_id, target_qty: qty })
    .then(data => {
      rv.innerHTML = `<div class="pp-card-hint" style="margin-top:12px">Review — <b>${_esc(data.order.name)}</b>: ${Math.round(data.order.target_qty).toLocaleString()} units → ${data.order.top_level_runs.toLocaleString()} run${data.order.top_level_runs === 1 ? '' : 's'}</div>${_rxOrderReportBody(data)}`;
      document.getElementById('rxOrderCreateBtn').style.display = '';
    })
    .catch(err => { rv.innerHTML = ''; status.textContent = err.message; });
}

function _rxCreateOrder() {
  const status = document.getElementById('rxOrderCreateStatus');
  const o = _rxOrderProductMatch();
  if (!o) { status.textContent = 'Pick a product from the list.'; return; }
  const qty = parseFloat(document.getElementById('rxOrderQty').value);
  if (!qty || qty <= 0) { status.textContent = 'Enter how many units the client wants.'; return; }
  const clientName = document.getElementById('rxOrderClient').value.trim();
  const notes = document.getElementById('rxOrderNotes').value.trim();
  status.textContent = 'Creating…';
  apiSend('POST', '/api/reactions/orders',
          { type_id: o.type_id, target_qty: qty, client_name: clientName || null, notes: notes || null })
    .then(data => {
      _rxCloseNewOrderModal();
      _rxLoadOrders();
      document.getElementById('rxOrderDetailModal').style.display = '';
      _renderRxOrderDetail(data);
    })
    .catch(err => { status.textContent = err.message; });
}

// ── Order detail / report view ───────────────────────────────────────────────────────────────

function _rxOpenOrderDetail(orderId) {
  document.getElementById('rxOrderDetailModal').style.display = '';
  document.getElementById('rxOrderDetailContent').innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
  _rxFetchOrderDetail(orderId);
}

function _rxFetchOrderDetail(orderId) {
  api(`/api/reactions/orders/${orderId}`)
    .catch(() => { throw new Error('Failed to load order'); })
    .then(data => _renderRxOrderDetail(data))
    .catch(err => {
      document.getElementById('rxOrderDetailContent').innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
}

function _rxCloseOrderDetail() {
  document.getElementById('rxOrderDetailModal').style.display = 'none';
}

// The cost/time/materials report body, shared by the order-detail modal AND the pre-commit review
// (so the two can't drift and the time-estimate contrast fix lives in one place). Sets
// _rxLastOrderMaterials for the "Copy for Janice" button. Returns just the stale note when the
// product isn't priced/reachable.
function _rxOrderReportBody(data) {
  const o = data.order;
  _rxLastOrderMaterials = data.materials || [];
  if (data.stale)
    return '<div class="pp-card-hint" style="color:var(--clr-amber)">This product isn\'t priced/reachable right now — no live cost/materials breakdown.</div>';

  const materialsHtml = !(data.materials || []).length ? '<div class="pp-empty">Nothing needed right now.</div>' : `
    <div style="overflow-x:auto">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Material</th><th>Quantity</th><th>Unit price</th><th>Est. cost</th><th>Volume</th></tr></thead>
        <tbody>${data.materials.map(m => `<tr><td>${_esc(m.name)}</td><td>${_rxCopyQtyCell(m.quantity)}</td><td>${_fmtIsk(m.unit_cost)}</td><td>${_fmtIsk(m.unit_cost * m.quantity)}</td><td>${Math.round(m.volume_m3 || 0).toLocaleString()} m³</td></tr>`).join('')}</tbody>
      </table>
    </div>`;
  const chainNote = !(data.chain_tiers || []).length ? '' : `<div class="rx-manual-preview-chain" style="margin-top:6px">Also needs ${data.chain_tiers.length} intermediate reaction${data.chain_tiers.length === 1 ? '' : 's'} first: ${data.chain_tiers.map(t => `<b>${_esc(t.name)}</b> ×${t.runs.toLocaleString()}`).join(', ')}.</div>`;

  return `
    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Cost to produce</div>
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Materials</span><b>${_fmtIsk(data.cost.material_cost)}</b></div>
      ${data.cost.job_cost ? `<div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Job install fees</span><b>${_fmtIsk(data.cost.job_cost)}</b></div>` : ''}
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Total</span><b>${_fmtIsk(data.cost.total_cost)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Per unit</span><b>${_fmtIsk(data.cost.cost_per_unit)}</b></div>
    </div>
    <div class="pp-card-hint">No markup applied — decide what to charge the client yourself.</div>

    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Time estimate</div>
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Start to finish</span><b>~${_fmtHours(data.time.estimated_hours)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Free slots now</span><b>${data.time.free_slots_now}</b></div>
    </div>
    <div class="pp-card-hint">${_esc(data.time.caveat || '')}</div>
    ${chainNote}

    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Materials to import (full chain, ${Math.round(o.target_qty).toLocaleString()} units)
      ${(data.materials || []).length ? `<button class="pp-add-btn" onclick="_rxCopyOrderMaterials(this)">Copy for Janice</button>` : ''}
    </div>
    ${materialsHtml}`;
}

function _renderRxOrderDetail(data) {
  const o = data.order;
  const el = document.getElementById('rxOrderDetailContent');
  const titleEl = document.getElementById('rxOrderDetailTitle');
  if (titleEl.firstChild) titleEl.firstChild.textContent = `${o.name} — order`;
  const remaining = o.top_level_runs - o.assigned_runs;

  const actionButtons = o.status === 'open' ? `
      ${remaining > 0 ? `<button id="rxOrderAssignBtn" onclick="_rxAssignOrderBatch(${o.id})">Assign next batch (${remaining.toLocaleString()} run${remaining === 1 ? '' : 's'} left)</button>` : '<span class="pp-card-hint">Every run has been assigned.</span>'}
      <button class="pp-add-btn" onclick="_rxCompleteOrder(${o.id})">Mark completed</button>
      <button class="pp-cancel-btn" onclick="_rxCancelOrder(${o.id})">Cancel order</button>
      ${o.assigned_runs === 0 ? `<button class="pp-cancel-btn" onclick="_rxDeleteOrder(${o.id})">Delete</button>` : ''}
    ` : `<span class="pp-card-hint">Order ${_esc(o.status)}.</span>`;

  el.innerHTML = `
    <div class="pp-card-hint">${o.client_name ? `For <b>${_esc(o.client_name)}</b> — ` : ''}${Math.round(o.target_qty).toLocaleString()} units needed → ${o.top_level_runs.toLocaleString()} reaction run${o.top_level_runs === 1 ? '' : 's'}</div>
    ${o.notes ? `<div class="pp-card-hint" style="margin-top:2px">${_esc(o.notes)}</div>` : ''}

    <div style="margin:10px 0 4px">${_rxOrderBarHtml(o)}</div>
    <div class="pp-card-hint">${o.assigned_runs.toLocaleString()} / ${o.top_level_runs.toLocaleString()} runs assigned to characters</div>

    ${_rxOrderReportBody(data)}

    <div class="pp-modal-actions" style="margin-top:14px;flex-wrap:wrap">
      ${actionButtons}
      <button class="pp-cancel-btn" onclick="_rxCloseOrderDetail()">Close</button>
      <span id="rxOrderDetailStatus" class="bug-status-msg"></span>
    </div>`;
}

// ── Running-job detail modal — a running slot never showed WHAT it's making beyond a countdown;
// clicking it now opens the full breakdown (materials used, cost, output value, profit, units,
// runtime), priced live off /api/reactions/job-detail (same graph/economics as everything else).

let _rxLastJobMaterials = [];
let _rxJobDetailProgress = null;  // % complete of the specific clicked job (0-1), null if unknown

// A labeled progress bar (fraction 0-1) reused by the job-detail modal and the metrics section.
function _rxProgressBar(frac, label) {
  const p = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
  return `<div class="rx-prog-row">
    <div class="rx-prog-head"><span>${_esc(label)}</span><span>${p}%</span></div>
    <div class="rx-prog-track"><div class="rx-prog-fill${p >= 100 ? ' rx-prog-done' : ''}" style="width:${p}%"></div></div>
  </div>`;
}

function _rxOpenJobDetail(typeId, runs, progressPct) {
  _rxJobDetailProgress = (progressPct != null && !isNaN(progressPct)) ? progressPct : null;
  document.getElementById('rxJobDetailModal').style.display = '';
  document.getElementById('rxJobDetailContent').innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
  const titleEl = document.getElementById('rxJobDetailTitle');
  if (titleEl.firstChild) titleEl.firstChild.textContent = 'Running job';
  api(`/api/reactions/job-detail?type_id=${typeId}&runs=${runs || 1}`)
    .catch(e => { throw new Error(e.status === 404 ? "This product isn't priced/reachable right now" : 'Failed to load'); })
    .then(d => _renderRxJobDetail(d))
    .catch(err => { document.getElementById('rxJobDetailContent').innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`; });
}

function _rxCloseJobDetail() {
  document.getElementById('rxJobDetailModal').style.display = 'none';
}

function _rxCopyJobMaterials(btn) {
  const text = _rxLastJobMaterials.map(m => `${m.name}\t${m.quantity}`).join('\n');
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}

function _renderRxJobDetail(d) {
  const el = document.getElementById('rxJobDetailContent');
  const titleEl = document.getElementById('rxJobDetailTitle');
  if (titleEl.firstChild) titleEl.firstChild.textContent = `${d.name} — running job`;
  _rxLastJobMaterials = d.materials || [];
  const profitCls = d.net_profit >= 0 ? 'an-ok' : 'an-warn';
  // ROI = profit per ISK invested. net_profit = output_value - total cost (see _value_reaction_batch),
  // so the total cost invested is output_value - net_profit — robust regardless of which cost
  // components (materials, job, shipping) are present.
  const totalCost = d.output_value - d.net_profit;
  const roiPct = totalCost > 0 ? (d.net_profit / totalCost) * 100 : null;
  const roiStr = roiPct != null ? `${roiPct >= 0 ? '+' : ''}${roiPct.toFixed(1)}%` : '';
  const materialsHtml = !d.materials.length ? '<div class="pp-empty">No materials data.</div>' : `
    <div style="overflow-x:auto">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Material</th><th>Quantity</th><th>Unit price</th><th>Est. cost</th><th>Volume</th></tr></thead>
        <tbody>${d.materials.map(m => `<tr><td>${_esc(m.name)}</td><td>${_rxCopyQtyCell(m.quantity)}</td><td>${_fmtIsk(m.unit_cost)}</td><td>${_fmtIsk(m.unit_cost * m.quantity)}</td><td>${Math.round(m.volume_m3 || 0).toLocaleString()} m³</td></tr>`).join('')}</tbody>
      </table>
    </div>`;
  // Completion bars: this specific job's own progress (from the clicked slot) plus the overall
  // "all running reactions" progress off the last dashboard load — same total the metrics bar shows.
  const totalFrac = (_rxLastDashboardData && _rxLastDashboardData.running_progress_pct != null)
    ? _rxLastDashboardData.running_progress_pct : null;
  const barsHtml = (_rxJobDetailProgress != null || totalFrac != null) ? `<div class="rx-prog-wrap">`
    + (_rxJobDetailProgress != null ? _rxProgressBar(_rxJobDetailProgress, 'This job complete') : '')
    + (totalFrac != null ? _rxProgressBar(totalFrac, 'All reactions complete') : '')
    + `</div>` : '';
  el.innerHTML = `
    <div class="pp-card-hint">${d.runs.toLocaleString()} run${d.runs === 1 ? '' : 's'} → ${Math.round(d.units).toLocaleString()} units · ${_fmtHours(d.runtime_hours)} total runtime</div>
    ${!d.priced ? '<div class="pp-card-hint" style="color:var(--clr-amber)">No live market price for this product — values may be incomplete.</div>' : ''}
    ${barsHtml}
    <div class="rx-manual-preview" style="margin-top:10px">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Input cost</span><b>${_fmtIsk(d.input_cost)}</b></div>
      ${d.job_cost ? `<div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Job install fees</span><b>${_fmtIsk(d.job_cost)}</b></div>` : ''}
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Output value</span><b>${_fmtIsk(d.output_value)} <span class="rx-manual-preview-units">(${Math.round(d.units).toLocaleString()} units)</span></b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Profit</span><b class="${profitCls}">${_fmtIsk(d.net_profit)}${roiStr ? ` <span class="rx-manual-preview-units">(${roiStr})</span>` : ''}</b></div>
    </div>

    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Materials used
      ${d.materials.length ? `<button class="pp-add-btn" onclick="_rxCopyJobMaterials(this)">Copy for Janice</button>` : ''}
    </div>
    ${materialsHtml}

    <div class="pp-modal-actions" style="margin-top:14px">
      <button class="pp-cancel-btn" onclick="_rxCloseJobDetail()">Close</button>
    </div>`;
}

function _rxAssignOrderBatch(orderId) {
  const status = document.getElementById('rxOrderDetailStatus');
  const btn = document.getElementById('rxOrderAssignBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Assigning…'; }
  apiSend('POST', `/api/reactions/orders/${orderId}/assign`, {})
    .then(data => {
      if (status) status.textContent = `Assigned ${data.runs_assigned.toLocaleString()} run${data.runs_assigned === 1 ? '' : 's'} to ${data.characters.map(c => c.character_name).join(', ')}.`;
      _rxFetchOrderDetail(orderId);
      _rxLoadOrders();
      onReactionsTabOpen();
    })
    .catch(err => {
      if (status) status.textContent = err.message;
      if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
    });
}

function _rxSetOrderStatus(orderId, newStatus) {
  apiSend('POST', `/api/reactions/orders/${orderId}/status`, { status: newStatus })
    .then(() => {
      // Completing/cancelling frees the order's reserved slots server-side — refresh the dashboard
      // so those slots show as free again.
      _rxLastDashboardData = null;
      _rxFetchOrderDetail(orderId);
      _rxLoadOrders();
      if (typeof _loadReactionsDashboard === 'function') _loadReactionsDashboard();
    })
    .catch(err => {
      const s = document.getElementById('rxOrderDetailStatus');
      if (s) s.textContent = err.message;
    });
}

function _rxCompleteOrder(orderId) {
  if (!confirm('Mark this order completed? Any reaction slots reserved for it will be freed.')) return;
  _rxSetOrderStatus(orderId, 'completed');
}

function _rxCancelOrder(orderId) {
  if (!confirm('Cancel this order? Any reaction slots reserved for it will be freed.')) return;
  _rxSetOrderStatus(orderId, 'cancelled');
}

function _rxDeleteOrder(orderId) {
  if (!confirm('Delete this order? This cannot be undone.')) return;
  apiSend('DELETE', `/api/reactions/orders/${orderId}`)
    .then(() => { _rxCloseOrderDetail(); _rxLoadOrders(); })
    .catch(err => {
      const s = document.getElementById('rxOrderDetailStatus');
      if (s) s.textContent = err.message;
    });
}

// ── Local / alliance market pricing (local_market flag) ───────────────────────────────
// First-run flow: the Reactions tab is BLOCKED behind an inline onboarding gate (#rxGate) until
// the user connects at least one character and clicks Save. After that the gate never shows again
// (per-context `onboarded` flag) and all changes are made from the Reactions ⚙ Settings modal,
// which hosts the same market manager + freight forms. The market list + search is a reusable
// component mounted into either the gate (#rxOnboardMarkets) or the Settings modal
// (#rxSettingsMarkets); _rxMarketMount tracks which one is live.
let _rxMarketData = null;
let _rxMarketMount = null;

// Decide gate vs normal tab from /api/markets. Returns true when the gate is showing (caller
// should skip the normal dashboard load). Fails OPEN (no gate) if the feature is off or the
// request fails, so a hiccup never locks the user out of Reactions.
async function _rxApplyGate() {
  const gate = document.getElementById('rxGate');
  const dash = document.getElementById('rxDashboard');
  const show = (blocked) => {
    if (gate) gate.style.display = blocked ? '' : 'none';
    if (dash) dash.style.display = blocked ? 'none' : '';
  };
  await Promise.resolve(typeof _loadFeatures === 'function' ? _loadFeatures() : null);
  if (!(typeof _featureActive === 'function' && _featureActive('local_market'))) { show(false); return false; }
  try {
    _rxMarketData = await api('/api/markets');
  } catch (e) { show(false); return false; }
  if (!_rxMarketData.onboarded) {
    show(true);
    _rxRenderGate(_rxMarketData);
    return true;
  }
  show(false);
  return false;
}

// ── Onboarding gate (inline, blocks the tab) ──────────────────────────────────────────

function _rxRenderGate(d) {
  const gate = document.getElementById('rxGate');
  if (!gate) return;
  gate.innerHTML =
    `<section class="pp-card rx-gate-card">`
    + `<div class="pp-card-title">Set up Reactions`
    + `<span class="pp-card-hint">— connect at least one character to run reactions on, then save. You can change everything later in ⚙ Settings.</span></div>`
    // Step 1 — characters (required)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">1</span>Add your characters</div>`
    + `<div class="rx-onboard-step-b" id="rxGateStep1"></div></div>`
    // Step 2 — markets (optional)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">2</span>Add local markets<span class="rx-onboard-opt">optional</span></div>`
    + `<div class="rx-onboard-step-b"><div id="rxOnboardMarkets"></div></div></div>`
    // Step 3 — freight (optional, foldable)
    + `<div class="rx-onboard-step"><details><summary class="rx-onboard-step-h" style="cursor:pointer">`
    + `<span class="rx-onboard-num">3</span>Configure freighting costs<span class="rx-onboard-opt">optional</span></summary>`
    + `<div class="rx-onboard-step-b">${_rxAccountSettingsFormHtml()}</div></details></div>`
    // Save
    + `<div class="rx-onboard-foot"><button id="rxGateSave" class="rx-onboard-connect" onclick="_rxCompleteOnboarding()">Save &amp; continue</button></div>`
    + `</section>`;
  _rxRenderStep1();
  _rxMountMarkets('rxOnboardMarkets');
  _loadRxAccountSettings();
  _rxUpdateSaveBtn();
}

function _rxRenderStep1() {
  const el = document.getElementById('rxGateStep1');
  if (el && _rxMarketData) el.innerHTML = _rxCharListHtml(_rxMarketData);
}

// Character list with the market-character picker. Reaction slots come from every character;
// exactly one (of those holding the market scope) reads the structure market — the rest are
// "slots only". Defaults to the first market-capable character (see backend _market_character).
function _rxCharListHtml(d) {
  const chars = d.characters || [];
  const rows = chars.length ? chars.map(c => {
    const isReader = c.character_id === d.market_character_id;
    const ctrl = c.is_market
      ? `<label class="rx-gate-reader" title="This character's access reads the structure market">`
        + `<input type="radio" name="rxReader" ${isReader ? 'checked' : ''} onchange="_rxSetMarketReader(${c.character_id})"> market character</label>`
      : `<span class="rx-gate-tag">slots only</span>`;
    return `<div class="rx-gate-charrow"><span class="rx-gate-charname">${_esc(c.character_name)}</span>${ctrl}</div>`;
  }).join('') : `<div class="pp-card-hint">No characters yet — connect the ones you want to run reactions on.</div>`;
  return `<div class="rx-gate-charlist">${rows}</div>`
    + `<button class="rx-onboard-connect" style="margin-top:4px" onclick="connectReactionsMarket()">`
    + `${chars.length ? 'Connect another character' : 'Connect a character'}</button>`
    + `<div class="pp-card-hint" style="margin-top:6px">Characters connected here get reaction slots AND market access. The <b>market character</b> reads your structure market — pick one that can dock at it. You can also add characters from the Dashboard (those contribute slots but not market access).</div>`;
}

function _rxUpdateSaveBtn() {
  const btn = document.getElementById('rxGateSave');
  if (!btn) return;
  const ok = _rxMarketData && (_rxMarketData.characters || []).length > 0;
  btn.disabled = !ok;
  btn.title = ok ? '' : 'Add at least one character first';
}

async function _rxSetMarketReader(id) {
  try {
    await apiSend('POST', '/api/markets/reader', { character_id: id });
  } catch (e) {}
  await _rxReloadGateData();
}

async function _rxReloadGateData() {
  try {
    _rxMarketData = await api('/api/markets');
  } catch (e) {}
  _rxRenderStep1();
  _rxRenderMarketManager();
  _rxUpdateSaveBtn();
}

async function _rxCompleteOnboarding() {
  try {
    _rxMarketData = await apiSend('POST', '/api/markets/complete');
  } catch (e) { toastError(e, 'Could not save'); return; }
  onReactionsTabOpen();   // re-run: now onboarded, so the gate lifts and the tab loads
}

// Called by connectReactionsMarket (planetary.js) after a character is added — refresh whichever
// surface is showing without a full tab reload.
function _rxAfterConnect() {
  const gate = document.getElementById('rxGate');
  if (gate && gate.style.display !== 'none') { _rxReloadGateData(); return; }
  const settings = document.getElementById('rxSettingsModal');
  if (settings && settings.style.display !== 'none') { _rxRefreshMarkets(); return; }
  if (typeof onReactionsTabOpen === 'function') onReactionsTabOpen();
}

// ── Reusable market-manager component (list + search) ─────────────────────────────────

function _rigOpts(sel) {
  return [0, 1, 2].map(t => `<option value="${t}" ${sel == t ? 'selected' : ''}>${t === 0 ? 'None' : 'T' + t}</option>`).join('');
}

function _rxPricingRowsHtml(pricing, editable) {
  let rows = '';
  pricing.forEach((m, i) => {
    const kindLbl = m.kind === 'structure' ? 'Structure' : 'Region';
    const controls = editable
      ? `<span class="rx-mkt-ctrl">`
        + `<button class="pp-add-btn" ${i === 0 ? 'disabled' : ''} onclick="_rxMarketMove(${m.id},-1)" title="Higher priority">▲</button>`
        + `<button class="pp-add-btn" ${i === pricing.length - 1 ? 'disabled' : ''} onclick="_rxMarketMove(${m.id},1)" title="Lower priority">▼</button>`
        + `<button class="pp-add-btn" onclick="_rxMarketRemove(${m.id})" title="Remove">✕</button></span>`
      : '';
    rows += `<div class="rx-mkt-row"><span class="rx-mkt-pri">${i + 1}</span>`
      + `<span class="rx-mkt-kind">${kindLbl}</span>`
      + `<span class="rx-mkt-name">${_esc(m.name)}</span>${controls}</div>`;
  });
  rows += `<div class="rx-mkt-row rx-mkt-jita"><span class="rx-mkt-pri">${pricing.length + 1}</span>`
    + `<span class="rx-mkt-kind">Fallback</span><span class="rx-mkt-name">Jita (always last)</span></div>`;
  return rows;
}

// A build structure, configured inline (no ordering — building isn't a priority chain).
function _rxBuildRowHtml(m) {
  const hullNote = m.hull ? `<b>${_esc(m.hull)}</b> · ${_esc(m.security || '?')}-sec` : 'type not detected';
  const manual = m.hull ? '' :
    `<div class="rx-rig-row">Structure <select id="bhull-${m.id}"><option value="">—</option>`
    + ['raitaru', 'azbel', 'sotiyo', 'athanor', 'tatara'].map(h => `<option ${m.hull === h ? 'selected' : ''}>${h}</option>`).join('')
    + `</select> in <select id="bsec-${m.id}">`
    + ['high', 'low', 'null'].map(s => `<option value="${s}" ${m.security === s ? 'selected' : ''}>${s}</option>`).join('')
    + `</select></div>`;
  const bonus = [];
  if (m.build_mfg && m.mfg_bonus) bonus.push(`mfg ME ${m.mfg_bonus.me}% / TE ${m.mfg_bonus.te}%`);
  if (m.build_rx && m.rx_bonus) bonus.push(`rx ME ${m.rx_bonus.me}% / TE ${m.rx_bonus.te}%`);
  return `<div class="rx-build-card">
    <div class="rx-build-hd"><span class="rx-mkt-name">${_esc(m.name)}</span><span class="pp-card-hint">${hullNote}</span>
      <button class="pp-add-btn rx-build-rm" onclick="_rxMarketRemove(${m.id})" title="Remove">✕</button></div>
    ${manual}
    <label class="rx-build-chk"><input type="checkbox" id="bm-${m.id}" ${m.build_mfg ? 'checked' : ''}> Manufacture here</label>
    <div class="rx-rig-row">ME rig <select id="bmme-${m.id}">${_rigOpts(m.me_rig)}</select> · TE rig <select id="bmte-${m.id}">${_rigOpts(m.te_rig)}</select></div>
    <label class="rx-build-chk"><input type="checkbox" id="br-${m.id}" ${m.build_rx ? 'checked' : ''}> React here</label>
    <div class="rx-rig-row">ME rig <select id="brme-${m.id}">${_rigOpts(m.rx_me_rig)}</select> · TE rig <select id="brte-${m.id}">${_rigOpts(m.rx_te_rig)}</select></div>
    <label class="rx-build-chk"><input type="checkbox" id="bp-${m.id}" ${m.price_from ? 'checked' : ''}> Also price from here</label>
    <div class="rx-build-foot"><button class="pp-add-btn" onclick="_rxSaveBuild(${m.id})">Save</button>${bonus.length ? `<span class="rx-mkt-build-badge">${bonus.join(' · ')}</span>` : ''}</div>
  </div>`;
}

// Non-blocking nudge: recommend (never require) setting up a reaction structure so reaction ME/TE
// is accurate. Dismissible; stays dismissed. Unlike Manufacturing, Reactions works fine without it.
async function _rxStructureRecommend() {
  const el = document.getElementById('rxStructReco');
  if (!el) return;
  if (localStorage.getItem('rxStructRecoDismissed') === '1') { el.style.display = 'none'; return; }
  try {
    const d = await api('/api/markets');
    const hasRx = (d.markets || []).some(m => m.kind === 'structure' && m.build_rx);
    if (hasRx) { el.style.display = 'none'; return; }
    el.style.display = '';
    el.innerHTML = `For accurate reaction ME/TE, set up your reaction structure — `
      + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Markets &amp; Logistics</button> → 🔨 → React here. `
      + `<button class="ind-link-btn" onclick="_rxDismissStructReco()">Dismiss</button>`;
  } catch (e) { el.style.display = 'none'; }
}
function _rxDismissStructReco() {
  try { localStorage.setItem('rxStructRecoDismissed', '1'); } catch (e) {}
  const el = document.getElementById('rxStructReco');
  if (el) el.style.display = 'none';
}

async function _rxSaveBuild(id) {
  const v = s => document.getElementById(s + '-' + id);
  const body = {
    build_mfg: v('bm').checked, build_rx: v('br').checked,
    me_rig: +v('bmme').value, te_rig: +v('bmte').value,
    rx_me_rig: +v('brme').value, rx_te_rig: +v('brte').value,
    scope: (typeof _rxMarketMount !== 'undefined' && _rxMarketMount === 'rxSettingsMarketsGroup') ? 'group' : 'account',
  };
  if (v('bhull')) body.hull = v('bhull').value || null;
  if (v('bsec')) body.security = v('bsec').value || null;
  if (v('bp')) body.price_from = v('bp').checked;
  try {
    _rxMarketData = await apiSend('POST', `/api/markets/${id}/build`, body);
  } catch (e) {}
  _rxRenderMarketManager();
}

// Add a structure straight into the BUILD list (not the pricing chain) — one step, then set rigs.
async function _rxBuildAdd(payload) {
  const body = JSON.parse(decodeURIComponent(payload));
  body.scope = 'account'; body.price_from = false; body.build_mfg = true;
  try {
    await apiSend('POST', '/api/markets', body);
  } catch (e) { toastError(e, 'Could not add that structure'); return; }
  _rxRefreshMarkets();
}

function _rxMarketManagerHtml(d) {
  const own = d.markets || [];
  const list = own.length ? own : (d.effective || []);
  const editable = own.length > 0;
  const pricing = list.filter(m => m.price_from);
  const build = list.filter(m => m.kind === 'structure' && (m.build_mfg || m.build_rx));
  const inherited = !own.length && d.effective && d.effective.length;
  const inheritNote = inherited && d.group
    ? `<div class="pp-card-hint" style="margin:0 0 8px">Using your group <b>${_esc(d.group.name)}</b>'s markets. Add one below to override for your account only.</div>` : '';
  // A structure priced from can only be read by a character with the market scope — warn if not.
  const structs = pricing.filter(m => m.kind === 'structure');
  const unreadable = structs.length && !d.connected;
  const warn = unreadable
    ? `<div class="pp-warn" style="margin:0 0 8px">⚠ Your structure market${structs.length > 1 ? 's' : ''} can't be read — no connected character has market access, so pricing falls back to Jita (in Reactions <b>and</b> Manufacturing). Connect a market character to fix it.</div>`
    : (d.connected ? '' : `<div class="pp-card-hint" style="margin:0 0 8px">Structure search needs a `
        + `connected market character (public regions work without one).`
        + ` <button class="ind-link-btn" onclick="connectReactionsMarket()">Connect one</button></div>`);
  return inheritNote + warn
    + `<div class="rx-mkt-sec"><div class="rx-mkt-sec-h">Price against — in priority order</div>`
    + `<div class="rx-mkt-list">${_rxPricingRowsHtml(pricing, editable)}</div>`
    + `<div class="rx-mkt-search">`
    + `<input id="rxMarketSearchInput" placeholder="Search a structure or region…" onkeydown="if(event.key==='Enter')_rxMarketSearch()">`
    + `<button class="pp-add-btn" onclick="_rxMarketSearch()">Search</button>`
    + `<div id="rxMarketSearchResults"></div></div></div>`
    + `<div class="rx-mkt-sec"><div class="rx-mkt-sec-h">Structures you build in <span class="pp-card-hint">their rigs set your ME &amp; TE — no ordering needed</span></div>`
    + (build.length ? build.map(_rxBuildRowHtml).join('') : `<div class="pp-card-hint">None yet — search above and choose <b>+ Build</b> on a structure.</div>`)
    + `</div>`;
}

async function _rxMountMarkets(containerId) {
  _rxMarketMount = containerId;
  // Fetch on demand if nothing has loaded the market list yet. Previously this just called
  // _rxRenderMarketManager(), which returns silently when _rxMarketData is null — so opening
  // Settings -> Markets & Logistics WITHOUT first visiting the Reactions tab (the only thing
  // that populated it) rendered an empty panel, and structure search appeared broken until you
  // reloaded the page.
  if (!_rxMarketData) {
    const el = document.getElementById(containerId);
    if (el) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading markets…</div>';
    await _rxRefreshMarkets();
    return;
  }
  _rxRenderMarketManager();
}

function _rxRenderMarketManager() {
  if (!_rxMarketMount || !_rxMarketData) return;
  const el = document.getElementById(_rxMarketMount);
  if (!el) return;
  // Adding a market re-renders this whole panel, which used to wipe the search box and its
  // results — so adding two structures meant typing the search twice. Carry both across.
  const prevQ = (document.getElementById('rxMarketSearchInput') || {}).value || '';
  const prevResults = (document.getElementById('rxMarketSearchResults') || {}).innerHTML || '';
  el.innerHTML = _rxMarketManagerHtml(_rxMarketData);
  if (prevQ) {
    const inp = document.getElementById('rxMarketSearchInput');
    if (inp) inp.value = prevQ;
    const box = document.getElementById('rxMarketSearchResults');
    if (box && prevResults) box.innerHTML = prevResults;
  }
}

async function _rxRefreshMarkets() {
  try {
    _rxMarketData = await api('/api/markets');
  } catch (e) {}
  _rxRenderMarketManager();
}

// ── Market search / mutations ─────────────────────────────────────────────────────────

async function _rxMarketSearch() {
  const inp = document.getElementById('rxMarketSearchInput');
  const box = document.getElementById('rxMarketSearchResults');
  if (!inp || !box) return;
  const q = inp.value.trim();
  if (q.length < 3) { box.innerHTML = '<div class="pp-card-hint">Type at least 3 characters.</div>'; return; }
  box.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Searching…</div>';
  let d;
  try { d = await api('/api/markets/search?q=' + encodeURIComponent(q)); }
  catch (e) { box.innerHTML = '<div class="pp-card-hint">Search failed.</div>'; return; }
  const results = [...(d.structures || []), ...(d.regions || [])];
  if (!results.length) {
    // "connect one in step 1" was written for the Reactions onboarding gate and is nonsense
    // anywhere else — this same panel is mounted in Settings, and the Manufacturing gate sends
    // people straight here to add a build structure. Someone who has only ever used PI holds no
    // market-scope character, so this empty result IS their dead end unless the way out is on it.
    box.innerHTML = !d.connected
      ? `<div class="pp-card-hint">No matches. Searching for a <b>structure</b> needs a character with
           market access — public regions work without one.</div>`
        + `<div class="settings-connect-row"><button class="pp-connect-btn" onclick="connectReactionsMarket()">`
        + `Connect a market character</button></div>`
      : '<div class="pp-card-hint">No matches.</div>';
    return;
  }
  box.innerHTML = results.map(m => {
    const kindLbl = m.kind === 'structure' ? 'Structure' : 'Region';
    const payload = encodeURIComponent(JSON.stringify({ kind: m.kind, location_id: m.location_id, name: m.name }));
    const buildBtn = m.kind === 'structure' ? `<button class="pp-add-btn" onclick="_rxBuildAdd('${payload}')">+ Build</button>` : '';
    return `<div class="rx-mkt-result"><span class="rx-mkt-kind">${kindLbl}</span>`
      + `<span class="rx-mkt-name">${_esc(m.name)}</span>`
      + `<button class="pp-add-btn" onclick="_rxMarketAdd('${payload}')">+ Price</button>${buildBtn}</div>`;
  }).join('');
}

async function _rxMarketAdd(payload) {
  const body = JSON.parse(decodeURIComponent(payload));
  body.scope = 'account';
  try {
    await apiSend('POST', '/api/markets', body);
  } catch (e) { toastError(e, 'Could not add that market'); return; }
  _rxRefreshMarkets();
}

async function _rxMarketRemove(id) {
  try { await apiSend('DELETE', '/api/markets/' + id + '?scope=account'); } catch (e) {}
  _rxRefreshMarkets();
}

async function _rxMarketMove(id, dir) {
  const own = (_rxMarketData && _rxMarketData.markets) || [];
  const ids = own.map(m => m.id);
  const idx = ids.indexOf(id);
  const j = idx + dir;
  if (idx < 0 || j < 0 || j >= ids.length) return;
  ids[idx] = ids[j]; ids[j] = id;
  try {
    await apiSend('POST', '/api/markets/reorder', { order: ids, scope: 'account' });
  } catch (e) {}
  _rxRefreshMarkets();
}
