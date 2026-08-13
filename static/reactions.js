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
  // Same flag race as the orders card above — the job-length row is gated on an admin-preview
  // flag, so it has to wait for the flags to land or it stays hidden until the tab is re-opened.
  Promise.resolve(typeof _loadFeatures === 'function' ? _loadFeatures() : null)
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

// Formulas the plan needs and the account doesn't hold, as a SHOPPING section rather than a
// warning: this is the page you open when you're about to go and buy things. Kept apart from the
// materials tables and out of every total on purpose — a formula is bought on CONTRACT, one item
// at a time, not multibought by quantity, so it needs different actions (copy the names, check the
// contract price) and must never be summed into a material cost.
function _rxFormulaShoppingSection(rep) {
  const rows = (rep && rep.formulas) || [];
  if (!rows.length) return _rxMissingFormulaWarn(rep);   // still shows unresolved-name warnings
  const inst = ++_rxMissSeq;
  setTimeout(() => _rxLoadFormulaPrices(inst, rows.map(m => m.type_id)), 0);
  const body = rows.map(m => `
    <tr>
      <td><b>${m.formulas_needed || 1}×</b> ${_esc(m.formula_name)}</td>
      <td>${_esc(m.name)}</td>
      <td>${m.runs_needed.toLocaleString()}</td>
      <td id="rxfpx-${inst}-${m.type_id}" class="ind-bp-px"
          data-need="${m.formulas_needed || 1}">checking the market…</td>
    </tr>`).join('');
  const unresolved = (rep.unresolved || []).length
    ? `<div class="pp-card-hint" style="color:var(--clr-amber)">⚠ ${rep.unresolved.length} name${rep.unresolved.length === 1 ? '' : 's'} in your pasted window didn't match any item, so a formula you DO own could be listed above.</div>`
    : '';
  return `
    <div class="rx-shop-sec-title">Formulas to acquire
      <span class="pp-card-hint">— ${rows.length} you don't hold; bought on the market, not in the multibuy above</span>
      <button class="pp-add-btn" onclick="_rxCopyFormulaNames(this)">Copy names</button>
    </div>
    <div style="overflow-x:auto;margin-bottom:12px">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Formula</th><th>For</th><th>Runs planned</th><th>Market</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    <div class="pp-card-hint" style="margin-bottom:10px">Not included in any cost above — what you'd pay
      for these is a purchase you may not want to make, and you may already hold one somewhere we can't see.</div>
    ${unresolved}`;
}

// One line per formula, with the COUNT — the same shape a multibuy paste takes, since these are
// bought off the market like anything else (reported 2026-08-08; they were previously treated as
// contract buys, which is right for a ship BPC and wrong for a reaction formula).
function _rxCopyFormulaNames(btn) {
  _rxCopyText((_rxLastFormulaShopping || [])
    .map(m => `${m.formula_name} ${m.formulas_needed || 1}`).join('\n'), btn);
}

let _rxLastFormulaShopping = [];

function _loadRxShoppingList() {
  const el = document.getElementById('rxShoppingListContent');
  if (!el) return;
  el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading shopping list…</div>';
  api('/api/reactions/shopping-list?include_orders=' + (_rxShoppingIncludeOrders ? 'true' : 'false'))
    .catch(() => ({ materials: [] }))
    .then(d => {
      _rxLastShoppingList = d.materials || [];
      _rxLastFormulaShopping = (d.formulas && d.formulas.formulas) || [];
      const formulaSection = _rxFormulaShoppingSection(d.formulas);
      if (!_rxLastShoppingList.length) {
        // "Nothing assigned" and "everything you have is on a customer order" are different
        // situations and used to render identically — the second one told a player with four
        // live assignments that they had none.
        const orders = d.order_count || 0;
        el.innerHTML = formulaSection + (orders > 0
          ? `<div class="pp-empty">No speculative assignments — but ${orders} assignment${orders === 1 ? ' is' : 's are'}
             committed to customer orders, each with its own materials report on the order itself.
             <button class="pp-btn-link" onclick="_toggleRxShoppingOrders()">Include customer orders</button></div>`
          : '<div class="pp-empty">Nothing needed right now — nothing currently assigned.</div>');
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
        + formulaSection
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

// A plan row's `tier_order` IS its stage: 0 is the deepest intermediate (react first), and the
// top-level product sits at len(chain_tiers). Displayed 1-based, absolute — NOT re-ranked against
// whatever is still pending, so a stage whose predecessor is already running keeps its real
// number ("Stage 2") instead of being relabelled "start now" while its input is still cooking.
function _rxStageLabel(tier, ready) {
  if (tier <= 0) return 'Stage 1 — start now';
  // `ready` comes from ESI: every job in the stage below is finished (see chain_stage_state), so
  // this one is startable now rather than something to keep checking back on.
  return ready ? `Stage ${tier + 1} — ready to start now` : `Stage ${tier + 1} — after stage ${tier} finishes`;
}

// (chain, stage) -> ready, across every tracked character. Keyed on the chain because two separate
// plans on one character must not gate each other.
function _rxStageReady(data) {
  const map = new Map();
  ((data && data.characters) || []).forEach(c => (c.stages || []).forEach(s => {
    map.set(`${c.character_id}:${s.chain}:${s.stage}`, !!s.ready);
  }));
  return map;
}

// ── "You don't hold a formula for these" ──────────────────────────────────────────────────────
// The Reactions counterpart of Industry's `metrics.missing_blueprints` panel (_indMissingBpWarn),
// deliberately the same shape and the same restraint: what you'd have to acquire, how many runs
// the plan asks of it, what a contract costs — and NOTHING added to any shopping list or cost
// total. The server only fills this in once a pasted industry window makes the library complete
// (see app/reactions/library.py); until then absence stays "unknown" and this renders nothing.
//
// Reuses the .ind-notes / .ind-bp-* styles rather than cloning them under rx- names: it is the
// same kind of notice saying the same kind of thing, and two copies of that CSS is how the two
// pages start looking like different products.
let _rxMissSeq = 0;

// ── Login cadence, on the surface it shapes ───────────────────────────────────────────────────
// *"I'd prefer to be able to schedule my jobs on a Saturday and handle the next stage a week later
// on a Saturday when I have time to play."* The setting is `max_reaction_job_days` and it already
// existed — Build rules → "Longest reaction job", on the Industry tab. This is the SAME value on
// the surface it actually shapes, not a second one: both read and write `/api/industry/build-setup`,
// so whichever you touch last is what both show.
let _rxCadenceDays = null;      // null until loaded; '' or a number after
let _rxCadenceAvail = false;

async function _rxLoadCadence() {
  try {
    const r = await api('/api/industry/build-setup');
    _rxCadenceAvail = !!(r.available && r.available.job_length);
    const d = ((r.account || r).job_length || {}).max_reaction_job_days;
    _rxCadenceDays = (d == null) ? '' : d;
  } catch (e) {
    _rxCadenceAvail = false;                 // the flag is off, or the surface 403'd — show nothing
  }
}

function _rxCadenceHtml() {
  if (!_rxCadenceAvail) return '';
  const v = _rxCadenceDays == null ? '' : _rxCadenceDays;
  const set = v !== '' && Number(v) > 0;
  return `<div class="rx-cadence">
      <label for="rxCadence">Come back every</label>
      <input type="number" id="rxCadence" min="0" step="0.5" value="${_esc(String(v))}"
             onchange="_rxSaveCadence(this.value)" title="No reaction job will be planned longer than this, so a stage lands inside the window you set. Blank or 0 = no ceiling.">
      <span>days</span>
      <span class="rx-cadence-note">${set
        ? 'No job is planned longer than this, so a stage finishes inside the window — it costs reactors when the work does not fit.'
        : 'No ceiling: a batch can sit in one reactor for as long as the work takes. Set a number to plan around a fixed play day.'}</span>
    </div>`;
}

function _rxSaveCadence(value) {
  const raw = String(value).trim();
  const days = raw === '' ? null : parseFloat(raw);
  if (days !== null && (!isFinite(days) || days < 0)) { toastError(new Error('Days must be 0 or more')); return; }
  apiSend('POST', '/api/industry/build-setup', { job_length: { max_reaction_job_days: days } })
    .then(() => {
      _rxCadenceDays = days == null ? '' : days;
      // The cadence reshapes the plan on the next read, so re-fetch rather than re-render what we
      // already have — the run counts on screen are exactly what just changed.
      _rxLastDashboardData = null;
      _loadReactionsDashboard();
    })
    .catch(e => toastError(e, 'Could not save the cadence'));
}

// ── Marking a reaction running or done by hand (`reactions_manual_done`) ──────────────────────
// The same three states, the same click cycle and the same wording as a build step in the Industry
// tab (`indCycleDone`). ESI is right nearly always; this is for when it isn't — a job cache up to
// five minutes stale, a job installed under a different product than planned, or a stage reacted
// before this tool ever saw it — and the page would otherwise keep saying "after stage 1 finishes"
// about a stage that finished an hour ago.
const _RX_MARK_NEXT = { none: 'running', running: 'done', done: 'none' };

// What a (character, product, stage) group is marked as. Read off the rows themselves so it can
// never disagree with what the backend used for the slot count and the stage gate.
function _rxMarkOf(rows) {
  const marked = rows.filter(r => r.marked);
  if (!marked.length) return 'none';
  // A part-marked group reads as the state it has ALREADY reached, never as the one it hasn't:
  // "2 of 4 done" is a group with work still to install, so it must not look finished.
  return marked.length < rows.length ? 'running'
    : (marked.every(r => r.marked === 'done') ? 'done' : 'running');
}

// none → running → done → none. One write per click, wrapping round, so a misclick costs a click
// and never data — the same forgiving cycle the build pipeline uses.
function rxCycleMark(characterId, typeId, tier) {
  const next = _RX_MARK_NEXT[_rxMarkOfGroup(characterId, typeId, tier)] || 'running';
  apiSend('POST', '/api/reactions/mark', {
    character_id: Number(characterId), type_id: Number(typeId), tier_order: Number(tier),
    // 'none' clears it: the setter reads jobs=0 as "forget this group", whatever state is sent.
    state: next === 'none' ? 'done' : next, jobs: next === 'none' ? 0 : null,
  })
    .then(() => { _rxLastDashboardData = null; _loadReactionsDashboard(); })
    .catch(toastError);
}

// The current state of a group, off the last payload — `rxCycleMark` needs it before it writes.
function _rxMarkOfGroup(characterId, typeId, tier) {
  const c = ((_rxLastDashboardData && _rxLastDashboardData.characters) || [])
    .find(x => String(x.character_id) === String(characterId));
  if (!c) return 'none';
  const rows = (c.pending || []).filter(p => String(p.type_id) === String(typeId)
    && (p.tier_order || 0) === Number(tier));
  return rows.length ? _rxMarkOf(rows) : 'none';
}

// "95 runs × 3 jobs" — the number you TYPE, and how many times you type it. Never the group total:
// "×285  3 jobs" reads as three jobs of 285 runs, i.e. three times the work really being asked for.
// A group whose jobs are not all the same size says so rather than averaging them away.
function _rxPerJobLabel(rows) {
  if (!rows.length) return '';
  const bySize = new Map();
  rows.forEach(r => bySize.set(r.runs, (bySize.get(r.runs) || 0) + 1));
  const parts = [...bySize.entries()].sort((a, b) => b[0] - a[0])
    .map(([runs, n]) => `${runs.toLocaleString()}\u00a0×\u00a0${n}`);
  const jobs = rows.length;
  return `${parts.join(' + ')}\u00a0job${jobs === 1 ? '' : 's'}`;
}

// The stage list drawn the way Manufacturing draws its build pipeline: columns are stages, rows
// are the characters holding work, a cell is what that character installs at that stage. The grid,
// the cards and the classes are Manufacturing's own (`_indPipelineHtml`, `.ind-pipe*`) rather than
// a lookalike — a stage is the same idea on both tabs, and two different pictures of it was the
// complaint. Reusing the stylesheet is also what stops them drifting apart again.
//
// Rows are CHARACTERS because that is the constraint the plan is built around: an intermediate has
// to be on the character reacting the thing above it, so a chain never splits and a row is exactly
// one login's worth of work. Manufacturing's rows are buildings for the same reason — the row is
// wherever the work physically has to happen.
function _rxPipelineHtml(todoRows, readyStages) {
  if (!todoRows.length) return '';
  const tiers = [...new Set(todoRows.map(g => g.tier))].sort((a, b) => a - b);
  const chars = [];
  todoRows.forEach(g => {
    if (!chars.some(c => c.id === g.character_id)) {
      chars.push({ id: g.character_id, name: g.character_name });
    }
  });
  chars.sort((a, b) => a.name.localeCompare(b.name));
  // Ready is per (character, stage): any chain of theirs at that stage whose inputs have landed.
  // Stage 1 needs nothing to finish first, so it is always installable.
  const ready = (cid, tier) => tier <= 0
    || [...readyStages.entries()].some(([k, v]) => v && k.startsWith(`${cid}:`) && k.endsWith(`:${tier}`));
  const anyReady = tier => chars.some(c => ready(c.id, tier));
  const markable = _featureActive('reactions_manual_done');

  let html = '<div class="ind-pipe-corner"></div>';
  tiers.forEach((tier, i) => {
    const jobs = todoRows.filter(g => g.tier === tier).reduce((s, g) => s + g.count, 0);
    const last = i === tiers.length - 1;
    html += `<div class="ind-pipe-hd${last ? ' ind-pipe-hd-final' : ' ind-pipe-hd-flow'}"`
      + ` title="${_esc(_rxStageLabel(tier, anyReady(tier)))}">`
      + `Stage ${tier + 1}<span>${jobs}</span></div>`;
  });

  chars.forEach(c => {
    const mine = todoRows.filter(g => g.character_id === c.id);
    const jobs = mine.reduce((s, g) => s + g.count, 0);
    html += `<div class="ind-pipe-rowlbl ind-row-rx">`
      + `<span class="ind-pipe-rowname">${_esc(c.name)}</span>`
      + `<span class="ind-pipe-rowsub" title="${jobs} job${jobs === 1 ? '' : 's'} to install">`
      + `${jobs} job${jobs === 1 ? '' : 's'}</span></div>`;
    tiers.forEach((tier, i) => {
      const last = i === tiers.length - 1;
      const cards = mine.filter(g => g.tier === tier).map(g => {
        // The run count is the number you type into the job, and the job count is how many times
        // you type it — the two things the player is actually reading this to find out.
        const isReady = ready(c.id, tier);
        const mark = markable ? _rxMarkOf(g.rows || []) : 'none';
        const jobs = (g.count || 0) + (g.markedCount || 0);
        const runs = g.totalRuns || (g.rows || []).reduce((s, r) => s + (r.runs || 0), 0);
        // A hand mark is the card's state when there is one — it is the more specific statement,
        // and it is the player's own. Otherwise the card says what the stage gate says.
        let state = '', cls = '';
        if (mark === 'done') {
          state = '<span class="ind-pipe-state ind-st-done">✓ done</span>';
          cls = ' ind-pipe-is-done';
        } else if (mark === 'running') {
          state = `<span class="ind-pipe-state ind-st-run">${g.markedCount} installed</span>`;
          cls = ' ind-pipe-is-run';
        } else if (tier > 0) {
          state = isReady
            ? '<span class="ind-pipe-state ind-st-run">ready</span>'
            : `<span class="ind-pipe-state ind-st-wait">after stage ${tier}</span>`;
          if (!isReady) cls = ' ind-pipe-is-wait';
        }
        const nextTip = mark === 'done' ? ' Click to set it back to not started.'
          : mark === 'running' ? ' Click when it has finished.'
          : ' Click to say it is installed.';
        // PER-JOB runs × how many jobs — never the group total. "×285  3 jobs" reads as three
        // jobs of 285 runs, which is three times the work actually being asked for; the number
        // that gets typed into the industry window is the per-job one.
        const perJob = _rxPerJobLabel(g.rows || []);
        const tip = `${g.name} — ${perJob} on ${c.name} (${runs.toLocaleString()} runs in total)`
          + `${tier > 0 ? ' — ' + _rxStageLabel(tier, isReady) : ''}`
          + (markable ? nextTip : '');
        const onclick = markable
          ? ` onclick="rxCycleMark('${c.id}', ${g.type_id}, ${tier})"` : '';
        return `<div class="ind-pipe-card ind-pipe-build${cls}${markable ? ' ind-pipe-markable' : ''}"`
          + `${onclick} data-tid="${g.type_id}" title="${_esc(tip)}">`
          + `<span class="ind-pipe-name">${_esc(g.name)}</span>`
          + `<span class="ind-pipe-meta"><span class="ind-pipe-runs">${_esc(perJob)}</span>`
          + `${state}</span></div>`;
      }).join('');
      html += `<div class="ind-pipe-cell ind-row-rx${last ? ' ind-pipe-final' : ''}">${cards}</div>`;
    });
  });

  const totalRuns = todoRows.reduce((s, g) => s + g.totalRuns, 0);
  return `<details class="ind-details" open><summary>Reaction pipeline</summary>`
    + `<p class="ind-pipe-hint">Each row is a character, each column a stage — ${totalRuns.toLocaleString()} runs`
    + ` to install${tiers.length > 1 ? `, in ${tiers.length} stages you start in order` : ''}.`
    + `${markable ? ' Click a step to step it on: not started → installed → done.' : ''}</p>`
    + `<div class="ind-pipe-scroll"><div class="ind-pipe" style="--ind-cols:${tiers.length}">${html}</div></div></details>`;
}

// Stages a plan does NOT have to run because the intermediate is already in an enabled stock
// source. Said out loud wherever it applies: a stage that silently disappears from a chain reads
// as the tool losing a step, not as it saving you one.
function _rxToastStockCovered(res) {
  const rows = (res && res.stock_covered) || [];
  if (!rows.length) return;
  const parts = rows.map(c => `${c.name} (${c.runs_saved} run${c.runs_saved === 1 ? '' : 's'})`);
  toast(`Skipped what you already hold: ${parts.join(', ')}.`, 'info');
}

function _rxStockCoveredNote(covered) {
  const rows = covered || [];
  if (!rows.length) return '';
  const parts = rows.map(c => `<b>${_esc(c.name)}</b> (${Math.round(c.units).toLocaleString()} units, ${c.runs_saved.toLocaleString()} run${c.runs_saved === 1 ? '' : 's'} saved)`);
  return `<div class="rx-stock-covered">Already in your stock, so ${rows.length === 1 ? 'this stage was' : 'these stages were'} shortened or skipped: ${parts.join(', ')}.</div>`;
}

function _rxMissingFormulaWarn(rep) {
  if (!rep) return '';
  const rows = rep.formulas || [];
  const unresolved = rep.unresolved || [];
  // The unresolved-name warning stands on its own even with nothing missing: once a paste is
  // treated as the whole library, a name we failed to resolve is silently indistinguishable from
  // a formula the user doesn't own (a CCP rename did exactly this once — `Fullerides` vs
  // `Fulleride Reaction Formula`). It has to be loud where the consequence is, not in an import
  // status line that scrolled away days ago.
  if (!rows.length && !(rep.complete && unresolved.length)) return '';
  const inst = ++_rxMissSeq;
  const unresolvedHtml = !unresolved.length ? '' : `
    <div class="rx-miss-unresolved"><b>⚠ ${unresolved.length} pasted name${unresolved.length === 1 ? '' : 's'} didn't match any item</b>
      <div>${unresolved.slice(0, 8).map(u => _esc(u.name) + (u.batch_name ? ` <span class="ind-bp-sub2">(${_esc(u.batch_name)})</span>` : '')).join(', ')}${unresolved.length > 8 ? '…' : ''}</div>
      <div class="ind-bp-warn-sub">A name we can't resolve looks exactly like a formula you don't own,
      so ${rows.length ? 'a formula you do hold could still be listed above' : 'one of those can show up as missing on a plan'}.
      Re-paste that window, or check the item's current in-game name.</div>
    </div>`;
  if (!rows.length) return `<div class="ind-notes">${unresolvedHtml}</div>`;

  const rowsHtml = rows.map(m => {
    // How many COPIES to buy, not just what to buy: a formula is locked into the reactor for the
    // job's duration, so four parallel jobs of a product need four of its formula.
    const n = m.formulas_needed || 1;
    return `
    <div class="ind-bp-row2">
      <span class="ind-bp-nm"><b>${n}×</b> ${_esc(m.formula_name)}<span class="ind-bp-need">for ${_esc(m.name)} · ${n} job${n === 1 ? '' : 's'} at once · ${m.runs_needed.toLocaleString()} run${m.runs_needed === 1 ? '' : 's'} planned</span></span>
      <span class="ind-bp-px" id="rxfpx-${inst}-${m.type_id}" data-need="${n}">checking the market…</span>
    </div>`;
  }).join('');
  // Prices land after render — the market lookup must never hold up the warning itself (same rule
  // the Industry panel follows).
  setTimeout(() => _rxLoadFormulaPrices(inst, rows.map(m => m.type_id)), 0);
  return `<div class="ind-notes"><div class="ind-note-block">
      <b>You don't hold a formula for ${rows.length === 1 ? 'this' : 'these'}</b>
      <div class="ind-bp-rows">${rowsHtml}</div>
      <div class="ind-bp-warn-sub">You've pasted your industry window, so that paste is read as your
      whole library — a formula it doesn't name is one you don't own, and this plan can't be
      installed without ${rows.length === 1 ? 'it' : 'them'}. Prices are what the formula goes for on
      the market, for comparison only: nothing here is in the materials list or any cost total.</div>
      ${unresolvedHtml}
    </div></div>`;
}

async function _rxLoadFormulaPrices(inst, ids) {
  if (!ids || !ids.length) return;
  let d = null;
  try { d = await api('/api/reactions/formula-prices?type_ids=' + ids.join(',')); } catch (e) {}
  ids.forEach(id => {
    const el = document.getElementById(`rxfpx-${inst}-${id}`);
    if (!el) return;
    const info = d && d.prices && d.prices[id];
    // The MARKET, not the contract index — reported 2026-08-08: a reaction formula is bought off
    // a sell order like any other item, and quoting a contract price budgets the wrong number.
    if (!info || !info.sell_price) {
      el.innerHTML = '<span class="ind-bp-sub2">no sell orders seen for this</span>';
      return;
    }
    const need = parseInt(el.dataset.need || '1', 10) || 1;
    el.innerHTML = `<b>${_fmtIsk(info.sell_price * need)}</b>`
      + `<span class="ind-bp-sub2">${need > 1 ? `${need} × ${_fmtIsk(info.sell_price)} · ` : ''}`
      + `${_esc(info.source || 'Jita')}</span>`;
  });
}


let _rxLastDashboardData = null;

function _loadReactionsDashboard() {
  const el = document.getElementById('rxDashboardContent');
  if (!el) return;
  // Only show the loading flash on a genuinely first/cold load — a refresh after cancelling one
  // assignment updates the cached data in place instead (see _rxCancelAssignment), so this
  // full-reload path only runs on tab-open or after "Clear all", not on every small action.
  if (!_rxLastDashboardData) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
  // Returned so a caller that must not finish before the plan is re-read can wait on it — the
  // levelling pass runs on this endpoint, so "assigned" is not "settled" until it resolves.
  // The cadence is fetched alongside, not before: a failure there must not stop the plan loading,
  // and the render reads whatever it has (nothing, until the first fetch lands).
  if (_rxCadenceDays === null) _rxLoadCadence().then(() => {
    if (_rxLastDashboardData) _renderReactionsDashboard(_rxLastDashboardData);
  });
  const load = api('/api/reactions/jobs')
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
  return load;
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
  const _rxReadyStages = _rxStageReady(data);
  const rows = tracked.map(c => {
    // Sorted by product name (running AND pending) so same-product squares cluster together in
    // the row instead of appearing in arbitrary insertion order — several interleaved products
    // was the actual "too messy to read off" complaint, not just the missing summary below.
    const _jobName = j => j.name || _rxProductName(j.product_type_id);
    const jobs = (jobsByChar.get(c.character_name) || [])
      .slice().sort((a, b) => _jobName(a).localeCompare(_jobName(b)));
    // Then by STAGE first: a chain's intermediate rows carry a lower tier_order than the product
    // they feed (see assign_reaction) and tier 0 must finish before tier 1 can start — the
    // backend has always ordered by it and the payload has always carried it, but the loadout
    // rendered every stage flat, which reads as "it isn't sequencing" when in fact it is.
    const pending = (c.pending || [])
      .slice().sort((a, b) => (a.tier_order || 0) - (b.tier_order || 0)
        || a.name.localeCompare(b.name) || a.runs - b.runs);
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
      const tier = a.tier_order || 0;
      // Keyed by stage too: the same product can legitimately appear at two stages (an
      // intermediate for one chain, the end product of another), and merging those into one
      // checklist line would claim you can install both right now.
      const key = `${c.character_id}:${a.type_id}:${tier}`;
      if (!todoGroups.has(key)) {
        todoGroups.set(key, {
          character_id: c.character_id, character_name: c.character_name,
          type_id: a.type_id, name: a.name, tier,
          count: 0, totalRuns: 0, jobRuns: new Map(), ids: [],
          rows: [], markedCount: 0,
        });
      }
      const g = todoGroups.get(key);
      g.rows.push(a);
      // A row the player has ticked is no longer something to install, so it leaves the counts the
      // checklist is built from — but the GROUP stays, carrying its mark, because that is what the
      // pipeline draws and what you click to take the mark back.
      if (a.marked) {
        g.markedCount++;
        continue;
      }
      g.count++;
      g.totalRuns += a.runs;
      g.jobRuns.set(a.runs, (g.jobRuns.get(a.runs) || 0) + 1);
      g.ids.push(a.assignment_id);
    }
    // One square per job for everything you can START — the row IS the character's reactors, and
    // folding those made a full character look empty. What overflowed the row was the other kind:
    // a chain's LATER stage, which is drawn because the grid is the plan, but is not holding a
    // reactor yet (it reuses the one the stage below frees). Ten startable jobs plus a queued
    // stage 2 put a 10-slot character at 13 squares and wrapped it onto a second line.
    //
    // So only the queued ones fold: identical later-stage jobs (same product, stage, run count and
    // order) become ONE square carrying "+N" — the same instruction, and how many more times to
    // repeat it once the stage below lands. Adjacent-merge is enough because `pending` is already
    // sorted by exactly those keys.
    const pendGroups = [];
    // A job marked DONE has given its reactor back, so it stops holding a square — the same call
    // the backend makes for `free_slots`, and the two must agree or the row count and the slot
    // count contradict each other on one screen. One marked RUNNING keeps its square: it IS
    // occupying a reactor, it just isn't one ESI can see yet.
    for (const a of pending.filter(p => p.marked !== 'done')) {
      const tier = a.tier_order || 0;
      const ready = tier <= 0 || _rxReadyStages.get(`${c.character_id}:${a.chain}:${tier}`) === true;
      const queuedJob = tier > 0 && !ready;
      const last = pendGroups[pendGroups.length - 1];
      if (queuedJob && last && last.queued
          && last.a.type_id === a.type_id && last.tier === tier && last.a.runs === a.runs
          && (last.a.order_label || '') === (a.order_label || '')) {
        last.n++;
        continue;
      }
      pendGroups.push({ a, tier, ready, queued: queuedJob, n: 1 });
    }
    for (const grp of pendGroups) {
      const a = grp.a;
      const tier = grp.tier;
      const pendingIcon = `https://images.evetech.net/types/${a.type_id}/icon?size=32`;
      // A slot committed to a customer order (see the Customer orders card) carries the
      // order's client label — lets a player tell client-committed slots apart from
      // speculative-profit ones without opening the order itself.
      const orderTag = a.order_label ? `<div class="rx-order-slot-tag" title="Committed to a customer order">Order: ${_esc(a.order_label)}</div>` : '';
      const orderTip = a.order_label ? ` — for order "${a.order_label}"` : '';
      // A later-stage slot is dimmed and dashed, and says why: its inputs come out of the stage
      // below it, so it is not startable yet. Still clickable (edit/cancel) — this is a "not
      // yet", not a lock.
      const ready = grp.ready;
      const stageBadge = `<span class="rx-slot-stage${tier > 0 && !ready ? ' rx-slot-stage-later' : ''}" title="${_esc(_rxStageLabel(tier, ready))}">S${tier + 1}</span>`;
      const stageTip = tier > 0
        ? (ready ? ` — ${_rxStageLabel(tier, true)}` : ` — ${_rxStageLabel(tier, false)}, so don't install it yet`)
        : ' — nothing has to finish first';
      // "+2" = two MORE jobs exactly like this one. The count, not the total, because the square
      // shows one job's run count and the whole point is that you type the same number again.
      const moreBadge = grp.n > 1
        ? `<span class="rx-slot-more" title="${grp.n} identical jobs — install ${_esc(a.name)} ×${a.runs} ${grp.n} times">+${grp.n - 1}</span>`
        : '';
      const moreTip = grp.n > 1 ? ` — ${grp.n} identical jobs` : '';
      // The mark also has to be reachable without the pipeline view, since that is its own flag —
      // and the square is where a player looking at a stale ⊘ actually is. Same click cycle,
      // stop-propagated so it doesn't open the editor the square is otherwise wired to.
      const markBadge = _featureActive('reactions_manual_done')
        ? `<span class="rx-slot-mark-badge" title="ESI hasn't seen this yet — click to say you installed it, again when it has finished" onclick="event.stopPropagation();rxCycleMark('${c.character_id}', ${a.type_id}, ${tier})">✓</span>`
        : '';
      // Marked installed by hand: it reads as a job in progress, not as a red "go do this", which
      // is the whole point of having said so.
      const isMarked = a.marked === 'running';
      const pendTitle = isMarked
        ? `You marked this installed — ${_esc(a.name)} ×${a.runs}, waiting for ESI to confirm it${_esc(orderTip)}. Click to edit.`
        : `Not running yet — install ${_esc(a.name)} ×${a.runs} in-game${_esc(moreTip)}${_esc(orderTip)}${_esc(stageTip)}. Click to edit.`;
      squares.push(`
        <div class="rx-slot rx-slot-pending${isMarked ? ' rx-slot-marked' : ''}${tier > 0 && !ready && !isMarked ? ' rx-slot-later' : ''}" title="${pendTitle}" onclick="_rxOpenEditAssign(${a.assignment_id}, '${c.character_id}')">
          <img class="rx-slot-icon" src="${pendingIcon}" alt="" onerror="this.style.visibility='hidden'">
          ${stageBadge}
          ${moreBadge}
          ${markBadge}
          <span class="rx-slot-pending-badge" onclick="event.stopPropagation();_rxCancelAssignment(${a.assignment_id})" title="Cancel ${grp.n > 1 ? 'one of these jobs' : 'this assignment'}">⊘</span>
          <span class="rx-slot-runs">×${a.runs}</span>
          <div class="rx-slot-pending-label">${_esc(a.name)}</div>
          ${orderTag}
        </div>`);
    }
    // A later stage is drawn as its own square — the grid is the PLAN — but it isn't holding a
    // reactor while the stage below it runs, so it doesn't count against free slots either. Say
    // that where the two numbers would otherwise look like they disagree.
    // What the plan holds at once is its busiest stage, so anything beyond that peak is queued
    // rather than occupying — exactly the number the server subtracted (`_concurrent_load`).
    const byTier = new Map();
    pending.forEach(a => byTier.set(a.tier_order || 0, (byTier.get(a.tier_order || 0) || 0) + 1));
    const queued = pending.length - Math.max(0, ...byTier.values(), 0);
    // Free squares are what this character can START now, so they are counted against the PEAK
    // stage rather than every planned row: a queued later stage is reusing a reactor the stage
    // below frees, not holding one of its own. Counting rows hid free reactors the server was
    // reporting as free — the same disagreement the note below exists to explain.
    const occupying = jobs.length + (_featureActive('reactions_parallel_stages')
      ? Math.max(0, ...byTier.values(), 0) : pending.length);
    for (let i = occupying; i < c.slots; i++) {
      squares.push(`<div class="rx-slot rx-slot-empty" title="Free reaction slot — click to assign your own product" onclick="_rxOpenManualAssign('${c.character_id}')"><span class="rx-slot-empty-mark">+</span></div>`);
    }
    const reuseNote = (queued > 0 && _featureActive('reactions_parallel_stages'))
      ? `<div class="pp-card-hint" style="font-size:11px;flex-basis:100%">${queued} queued job${queued === 1 ? '' : 's'} reuse a reactor an earlier stage frees up — not counted against free slots.</div>`
      : '';
    return `
      <div class="rx-char-row">
        <div class="rx-char-label">${_esc(c.character_name)}<br><span class="pp-card-hint">${c.free_slots} / ${c.slots} free</span></div>
        <div class="rx-slot-row">${squares.join('')}</div>
        ${reuseNote}
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
  // Ordered by STAGE first, then character/product: this is the install order. A chain's
  // intermediates have to be reacted and finished before the product they feed can start, so a
  // flat alphabetical list was telling the player to install things they cannot install yet.
  // A group whose every job is ticked has nothing left to install. It stays in `todoGroups` so the
  // pipeline can draw it and offer the mark back; the checklist below is about work, so it drops.
  const todoRows = [...todoGroups.values()]
    .filter(g => g.count > 0)
    .sort((a, b) => a.tier - b.tier
      || a.character_name.localeCompare(b.character_name) || a.name.localeCompare(b.name));
  const pipeRows = [...todoGroups.values()]
    .sort((a, b) => a.tier - b.tier
      || a.character_name.localeCompare(b.character_name) || a.name.localeCompare(b.name));
  const todoStages = [...new Set(todoRows.map(g => g.tier))].sort((a, b) => a - b);
  const _todoRowHtml = g => {
    const jobBreakdown = [...g.jobRuns.entries()].sort((a, b) => b[0] - a[0])
      .map(([runs, n]) => n > 1 ? `${n}×${runs} runs` : `${runs} runs`).join(' + ');
    return `
      <tr${g.tier > 0 ? ' class="rx-todo-later"' : ''}>
        <td>${_esc(g.character_name)}</td>
        <td><img src="https://images.evetech.net/types/${g.type_id}/icon?size=32" alt="" style="width:16px;height:16px;border-radius:3px;vertical-align:middle;margin-right:5px" onerror="this.style.display='none'">${_esc(g.name)}</td>
        <td>${_esc(jobBreakdown)}</td>
        <td><b>${g.totalRuns.toLocaleString()}</b></td>
      </tr>`;
  };
  // The same rows, drawn as Manufacturing's pipeline instead of a stage-bannered table. One
  // surface replaces the other rather than sitting beside it: two readings of the same list is
  // exactly the inconsistency this is here to remove.
  const todoListHtml = _featureActive('reactions_stage_pipeline')
    ? _rxPipelineHtml(pipeRows, _rxReadyStages)
    : !todoRows.length ? '' : `
    <div class="pp-card-hint" style="font-weight:600;margin:2px 0 4px">
      To install — ${todoRows.reduce((s, g) => s + g.totalRuns, 0).toLocaleString()} runs across ${todoRows.length} product${todoRows.length === 1 ? '' : 's'}${todoStages.length > 1 ? `, in ${todoStages.length} stages` : ''}
    </div>
    <div style="overflow-x:auto;margin-bottom:12px">
      <table class="pp-card-table" style="width:100%">
        <thead><tr><th>Character</th><th>Product</th><th>Jobs</th><th>Total runs</th></tr></thead>
        <tbody>${todoStages.map(tier => {
          // The stage banner is dropped entirely when everything pending is stage 1 — the
          // common single-step case shouldn't grow a header row that says nothing.
          const anyReady = [..._rxReadyStages.entries()].some(([k, v]) => v && k.endsWith(`:${tier}`));
          const head = todoStages.length === 1 && tier === 0 ? '' : `
            <tr class="rx-todo-stage"><td colspan="4">${_esc(_rxStageLabel(tier, anyReady))}</td></tr>`;
          return head + todoRows.filter(g => g.tier === tier).map(_todoRowHtml).join('');
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
  // An order with no agreed price costs real ISK but brings in a revenue the tool was never told,
  // so both value tiles understate. Say so under them rather than let a confident-looking number
  // stand for a figure nobody has supplied.
  const unpriced = data.unpriced_orders || 0;
  const unpricedNote = unpriced
    ? `<div class="settings-note"><span>${unpriced} order${unpriced === 1 ? '' : 's'} here
       ${unpriced === 1 ? 'has' : 'have'} no agreed price, so ${unpriced === 1 ? 'it is' : 'they are'}
       valued at <b>market</b> rather than at the invoice. Set the price on the order for the real
       figure.</span></div>`
    : '';
  const overviewTiles = `<div class="an-stats">
      ${_dashTile(_fmtIsk(data.pending_isk_committed), 'ISK committed')}
      ${_dashTile(_fmtIsk(data.pending_output_value), 'Expected output value')}
      ${_dashTile(_fmtIsk(data.pending_net_profit_per_day), 'Expected profit / day',
                  (data.pending_net_profit_per_day || 0) >= 0 ? 'an-ok' : 'an-bad')}
      ${_dashTile(`${usedSlots}<span class="an-of"> / ${data.total_slots}</span>`, 'Slots used')}
      ${_dashTile(String(pendingCount), 'Jobs to install', pendingCount > 0 ? 'an-warn' : '')}
      ${_dashTile(timeLeftVal, timeLeftLbl)}
    </div>${unpricedNote}`;

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

  // ...and formulas the plan ALREADY holding these slots needs but the account doesn't have. The
  // wizard/order/manual paths catch this before a slot is created, but only from the moment they
  // were switched on: a plan assigned earlier — or one whose formula has since been sold — sits
  // here looking installable. Above the checklist, because it changes what you'd install.
  // "Stage 1 is finished — you can start stage 2 now." The thing the player was otherwise left to
  // work out by watching timers: a later stage whose own predecessors are ALL done (read off ESI
  // job states, see chain_stage_state) and which still has jobs waiting to be installed.
  const readyNow = [];
  (data.characters || []).forEach(c => (c.stages || []).forEach(s => {
    if (s.stage > 0 && s.ready && s.todo > 0) {
      readyNow.push({ character: c.character_name, stage: s.stage + 1, names: s.names || [] });
    }
  }));
  const readyBanner = !readyNow.length ? '' : `
    <div class="rx-stage-ready">✅ <b>Stage ${readyNow[0].stage} is ready to start${readyNow.length > 1 ? ' on several characters' : ` on ${_esc(readyNow[0].character)}`}</b>
      — everything it waits on has finished. Install ${readyNow.map(r => r.names.map(_esc).join(', ')).join(' · ')}.</div>`;

  el.innerHTML = reconnectNote + readyBanner + _rxMissingFormulaWarn(data.missing_formulas)
    + _rxCadenceHtml() + todoListHtml + rows + untrackedNote;
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
    .then(res => {
      // Order-linked slots are cleared too and their orders handed their runs back (see
      // unassign_all_reactions) — say so, because "my customer order went back to 0 assigned"
      // is not something to discover later.
      const n = ((res && res.orders_reset) || []).length;
      if (n) toast(`Cleared ${res.cleared} planned job${res.cleared === 1 ? '' : 's'}; ${n} customer order${n === 1 ? '' : 's'} put back to unassigned.`, 'info');
      _rxLastDashboardData = null;
      _loadReactionsDashboard();
      if (typeof _rxLoadOrders === 'function') _rxLoadOrders();
    })
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
  // Fresh per modal open: declaring a formula or pasting a window between two opens has to show
  // up. Within one open the cache is what keeps typing a run count off the network.
  _rxMissingCache.clear();
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
    // Which stage the step belongs to — steps sharing one run at the same time. Passed straight
    // through from the opportunity; the server re-derives it if an older payload lacks it.
    tier: t.tier,
  }));
}

// The manual-assign path asks the same question the wizard and an order answer inline, over the
// generic /api/reactions/missing-formulas endpoint. Cached per PRODUCT, and only the set of
// missing formulas comes from the server: which ones you hold doesn't change when you type a
// different run count, so the run figures are filled in locally and the endpoint isn't hit on
// every keystroke.
const _rxMissingCache = new Map();

function _rxMissingForProduct(typeId, wantedRuns, onLoaded) {
  if (_rxMissingCache.has(typeId)) {
    const rep = _rxMissingCache.get(typeId);
    if (!rep) return null;
    return {...rep, formulas: (rep.formulas || []).map(
      f => ({...f, runs_needed: wantedRuns.get(f.type_id) || f.runs_needed}))};
  }
  _rxMissingCache.set(typeId, null);        // in flight — don't fire a second request for it
  apiSend('POST', '/api/reactions/missing-formulas',
          {items: [...wantedRuns.entries()].map(([tid, runs]) => ({type_id: tid, runs}))})
    .then(rep => { _rxMissingCache.set(typeId, rep || {}); if (onLoaded) onLoaded(); })
    .catch(() => { _rxMissingCache.set(typeId, {}); });
  return null;
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
  // Which of these steps you hold no formula for. The chain tiers are asked about only when you're
  // actually producing the chain — un-tick "produce the chain" and the intermediates become
  // something you're sourcing yourself, not steps this plan installs.
  const wantedRuns = new Map([[o.type_id, totalRuns]]);
  if (produceChain) allChainTiers.forEach(t => wantedRuns.set(t.type_id, t.runs));
  const missingHtml = _rxMissingFormulaWarn(
    _rxMissingForProduct(o.type_id, wantedRuns, _rxManualAssignPreview));
  el.innerHTML = `
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Input cost</span><b>${_fmtIsk(fixedCosts)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Runtime per job</span><b>${_fmtHours(runtimeHours)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Output value</span><b>${_fmtIsk(outputValue)} <span class="rx-manual-preview-units">(${Math.round(outputQty).toLocaleString()} units)</span></b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Profit</span><b class="${profit >= 0 ? 'an-ok' : 'an-warn'}">${_fmtIsk(profit)}</b></div>
      <div class="rx-manual-preview-breakeven">Sell for at least <b>${_fmtIsk(breakEven)}</b>/unit to break even at today's material, shipping${o.job_cost ? ', job' : ''} and collateral cost.</div>
      ${chainNote}
      ${missingHtml}
    </div>`;
}

async function _rxSubmitManualAssign() {
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
  //
  // How MUCH room: the stages run one after another, each freeing its reactor for the next, so
  // what the chain needs at once is its busiest stage — one job — not one per stage. Demanding
  // `chainJobs + 1` refused four-stage chains on a character with three free slots that would
  // have run them fine, and is the same double-count the server's own guard never made
  // (`_concurrent_load`).
  // The chain's busiest STAGE, not its step count and not 1: steps sharing a stage (Carbon Fiber,
  // Oxy-Organic Solvents and Thermosetting Polymer all sit one step off goo) run at the same time
  // and each need their own reactor, while a later stage reuses what an earlier one frees.
  const _stageCounts = new Map();
  chainTiers.forEach(t => _stageCounts.set(t.tier || 0, (_stageCounts.get(t.tier || 0) || 0) + (t.job_count || 1)));
  const chainPeak = chainJobs > 0
    ? (_featureActive('reactions_parallel_stages') ? Math.max(1, ..._stageCounts.values()) : chainJobs)
    : 0;
  let primary = null;
  if (chainJobs > 0) {
    primary = ordered.find(c => freeLeft.get(c.character_id) >= chainPeak + 1) || null;
    if (!primary) {
      const names = chainTiers.map(t => t.name).join(', ');
      status.textContent = `This needs ${chainJobs} intermediate reaction${chainJobs === 1 ? '' : 's'} first (${names}) plus at least 1 slot for the product itself, all on one character — none of your tracked characters has ${chainPeak + 1} free slots. Free up slots or lower the run count.`;
      return;
    }
  }

  const allocations = [];  // { char, jobs, chain_tiers }
  let remaining = jobsWanted;
  if (primary) {
    const take = Math.min(remaining, freeLeft.get(primary.character_id) - chainPeak);
    freeLeft.set(primary.character_id, freeLeft.get(primary.character_id) - chainPeak - take);
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
    if (!await ppConfirm(`Only ${jobsWanted - remaining} of ${jobsWanted} jobs fit across your free slots right now${chainNote}. Assign what fits?`)) return;
  }

  status.textContent = _rxEditingAssignmentId ? 'Saving…' : 'Assigning…';
  // Editing = delete the old row, then run the exact same create flow with the new values —
  // there's no dedicated update endpoint, and this is simpler than one that would have to
  // handle the same job-count-changed/chain-changed cases this already handles for a fresh
  // assign. If the delete fails, don't touch anything else — which is why it is its own step and
  // the runner is told to stop there rather than creating the new rows beside the old ones.
  const steps = [];
  if (_rxEditingAssignmentId) {
    const oldId = _rxEditingAssignmentId;
    steps.push({
      label: 'Remove the slot being replaced', critical: true,
      run: () => apiSend('DELETE', `/api/reactions/assign/${oldId}`)
        .catch(() => { throw new Error('Could not delete the old assignment'); }),
    });
  }
  allocations.forEach(a => steps.push({
    label: `${o.name} ×${(a.jobs * runsPerJob).toLocaleString()} in ${a.jobs} job${a.jobs === 1 ? '' : 's'} → ${a.char.character_name}`,
    run: () => apiSend('POST', '/api/reactions/assign', {
      character_id: a.char.character_id, type_id: o.type_id, name: o.name,
      runs: a.jobs * runsPerJob, job_count: a.jobs,
      input_cost: costPerRun * a.jobs * runsPerJob, reward: rewardPerRun * a.jobs * runsPerJob,
      chain_tiers: a.chain_tiers,
      // Keep what the server said. A refusal now carries a reason worth reading — "that needs 12
      // reaction slots at once and this character has 10" tells you what to change; "Assign
      // failed" tells you nothing and reads as a bug in the tool.
    }).then(res => { _rxToastStockCovered(res); return res; })
      .catch(e => { throw new Error((e && e.message) || 'Assign failed'); }),
  }));
  steps.push({ label: 'Level the run counts and re-read the plan', run: _rxReloadPlan });

  _rxRunSteps(_rxEditingAssignmentId ? 'Saving this slot' : 'Assigning', steps).then(res => {
    if (res.ok) { _rxCloseManualAssign(); onReactionsTabOpen(); return; }
    status.textContent = 'Some of it did not go through — see the list.';
  });
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
  // A formula is locked into the reactor while a job runs on it, so a product runs in as many jobs
  // as you hold formulas — not as many as you have free slots. Say so, or 10 slots used as 1 reads
  // as a broken tool.
  const capped = t.formula_capped || [];
  const formulaNote = capped.length
    ? `<div class="pp-card-hint" style="margin-bottom:10px">${capped.length} step${capped.length === 1 ? '' : 's'} run in fewer jobs than your free slots allow — a formula is locked while a job runs on it, so ${_esc(capped.join(', '))} run${capped.length === 1 ? 's' : ''} on the formulas you hold.</div>`
    : '';
  // ...and the harder version of the same question: not "how many jobs can this run side by side"
  // but "can you run this step at all". Above the suggestions, because it changes what you'd
  // assign, not just how fast it finishes.
  // Extra jobs that went to reactors nobody else claimed. Said out loud, because "why is this
  // product running in 5 jobs when the cadence only needed 2" should have an answer on the page.
  const idleUsed = t.idle_slots_used || 0;
  const alignedSlots = t.stage_aligned_slots || 0;
  const idleNote = !idleUsed ? '' :
    `<div class="pp-card-hint" style="margin-bottom:10px">${idleUsed} reactor${idleUsed === 1 ? '' : 's'} re-used to finish the slowest steps sooner${alignedSlots ? `, ${alignedSlots} of them moved off steps that were already going to finish early so each stage lands in one go` : ''} — same runs, same cost, fewer trips to install.</div>`;
  const budgetSummary = `<div class="pp-card-hint" style="margin-bottom:10px">${bindingNote}</div>${absorbNote}${formulaNote}${idleNote}`
    + _rxStockCoveredNote(t.stock_covered)
    + _rxMissingFormulaWarn(data.missing_formulas);

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
      // Shown here (before assigning) as well as on the dashboard, where the same ordering is
      // rendered as labelled stages off each plan row's tier_order.
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
          <button class="rx-sugg-assign-btn" id="rxAssignBtn${i}" onclick="_rxAssignOne(${i}, this)">Assign</button>
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

// ── Committing a plan is several round trips — block the view and show them ─────────────────
// "Assign all" is one POST per suggestion and then a re-read of the plan, and that re-read is
// where the run counts get levelled and the jobs re-split (`level_product_runs`) — so the numbers
// on screen are only true once the last step lands. Leaving the page live through all of it is
// what produced the two failures this package already carries scars from: a second click
// appending another full set of rows, and a dashboard read that raced the writes.
//
// So the view is blocked, and the steps run ONE AT A TIME. Sequential is not just for the
// display: each assign has to see the slots the one before it took, and firing them together let
// two suggestions both claim the same free reactor. A failed step marks itself and the rest still
// run — suggestions are independent, and stopping at the first refusal would strand the others.
let _rxStepsBusy = false;

function _rxRunSteps(title, steps) {
  const modal = document.getElementById('rxStepsModal');
  if (!modal || !steps.length) return Promise.resolve({ ok: true, failed: [] });
  const listEl = document.getElementById('rxStepsList');
  const barEl = document.getElementById('rxStepsBar');
  const actEl = document.getElementById('rxStepsActions');
  const titleEl = document.getElementById('rxStepsTitle');
  const MARK = { todo: '·', now: '◐', done: '✓', failed: '✕' };
  const state = steps.map(() => 'todo');
  const notes = steps.map(() => '');
  const failed = [];
  let done = 0;

  const paint = () => {
    barEl.innerHTML = _rxProgressBar(done / steps.length, `${done} of ${steps.length} done`);
    listEl.innerHTML = steps.map((s, i) => `
      <div class="rx-step rx-step-${state[i]}">
        <span class="rx-step-mark">${MARK[state[i]]}</span>
        <span>${_esc(s.label)}</span>
        ${notes[i] ? `<span class="rx-step-note">${_esc(notes[i])}</span>` : ''}
      </div>`).join('');
  };

  _rxStepsBusy = true;
  titleEl.textContent = title;
  actEl.style.display = 'none';
  modal.style.display = '';
  paint();

  let stopped = false;
  let chain = Promise.resolve();
  steps.forEach((s, i) => {
    chain = chain.then(() => {
      // A `critical` step is one the rest depend on (deleting the row an edit replaces): if it
      // fails, going on would leave the new jobs sitting beside the ones they were meant to be.
      if (stopped) { notes[i] = 'skipped'; done++; paint(); return; }
      state[i] = 'now';
      paint();
      return Promise.resolve().then(() => s.run())
        .then(() => { state[i] = 'done'; })
        .catch(err => {
          state[i] = 'failed';
          notes[i] = (err && err.message) ? err.message : 'failed';
          failed.push(i);
          if (s.critical) stopped = true;
        })
        .then(() => { done++; paint(); });
    });
  });
  return chain.then(() => {
    _rxStepsBusy = false;
    if (!failed.length) { modal.style.display = 'none'; return { ok: true, failed }; }
    // Something refused. Leave it on screen with the reason the server gave — closing over a
    // failure is how "Assign all" used to report "some failed" with no way to see which.
    titleEl.textContent = `${failed.length} of ${steps.length} did not go through`;
    actEl.style.display = '';
    return { ok: false, failed };
  });
}

function _rxCloseSteps() {
  if (_rxStepsBusy) return;    // there is nothing to close while it is still running
  document.getElementById('rxStepsModal').style.display = 'none';
}

// The plan re-read every commit ends with: it is what levels the run counts and re-splits the
// jobs, so until it lands the dashboard is showing what was asked for rather than what is there.
function _rxReloadPlan() {
  _rxLastDashboardData = null;
  return Promise.resolve(_loadReactionsDashboard());
}

function _rxAssignSuggestion(i, btn, strict) {
  const s = _rxLastSuggestions[i];
  if (!s) return Promise.resolve();
  btn.disabled = true;
  btn.textContent = '…';
  return apiSend('POST', '/api/reactions/assign', {
    character_id: s.assigned_character_id, type_id: s.type_id, name: s.name,
    runs: s.runs, job_count: s.job_count || 1, input_cost: s.input_cost, reward: s.reward,
    chain_tiers: s.chain_tiers || [],
  })
    .then(res => {
      // A stage the hangar already covers is not committed (see _trim_tiers_by_stock), so say which
      // — the plan quietly holding fewer stages than the suggestion showed would read as a bug.
      _rxToastStockCovered(res);
      // There used to be an `if (!r.ok) throw new Error()` here — a leftover from when this called
      // fetch() directly. `r` does not exist in this scope, so it threw a ReferenceError on every
      // SUCCESSFUL assign, the catch below relabelled the row "Retry", and "Assign all" reported
      // "Some failed". The POST had already committed, so each retry appended another full set of
      // assignment rows: two suggestions retried a few times became 27 rows on a 10-slot character
      // (reported 2026-08-01). apiSend() already rejects on a non-2xx, so reaching here IS success.
      btn.textContent = 'Assigned ✓';
    })
    .catch(err => {
      btn.disabled = false;
      btn.textContent = 'Retry';
      // Swallowed for a direct click (the button IS the report). Re-thrown when the step runner is
      // driving, or a refused assign would tick over as a green ✓.
      if (strict) throw err;
    });
}

// One row's Assign button. Two steps, not one: the POST, and the plan re-read that levels the run
// counts — so this row's number is settled before the view comes back, same as Assign all.
function _rxAssignOne(i, btn) {
  const s = _rxLastSuggestions[i];
  if (!s) return Promise.resolve();
  return _rxRunSteps(`Assigning ${s.name}`, [
    { label: `${s.name} ×${s.runs.toLocaleString()} → ${s.assigned_character}`,
      run: () => _rxAssignSuggestion(i, btn, true) },
    { label: 'Level the run counts and re-read the plan', run: _rxReloadPlan },
  ]);
}

// "Just assign and sort out the best use of my slots" — commits every current suggestion at
// once instead of clicking Assign per row (still respects whatever's already been assigned/
// retried, via each row button's own state).
function _rxAssignAll() {
  const btn = document.getElementById('rxAssignAllBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Assigning…'; }
  const steps = [];
  _rxLastSuggestions.forEach((s, i) => {
    const rowBtn = document.getElementById(`rxAssignBtn${i}`);
    if (!rowBtn || rowBtn.textContent.includes('✓')) return;   // already committed, don't re-post
    steps.push({
      label: `${s.name} ×${s.runs.toLocaleString()} → ${s.assigned_character}`,
      run: () => _rxAssignSuggestion(i, rowBtn, true),
    });
  });
  if (!steps.length) {
    if (btn) { btn.textContent = 'All assigned ✓'; }
    return Promise.resolve();
  }
  steps.push({ label: 'Level the run counts and re-read the plan', run: _rxReloadPlan });
  return _rxRunSteps('Assigning your plan', steps).then(res => {
    if (btn) {
      btn.textContent = res.ok ? 'All assigned ✓' : 'Some failed — retry below';
      btn.disabled = res.ok;
    }
    // Everything landed: the dashboard now holds the real plan — levelled run counts, jobs
    // re-split — and the suggestion list behind this shows what was ASKED for. Show the truth.
    if (res.ok) _rxShowDashboard();
  });
}

// Back from the wizard to the plan, without the reload `wizRCancel` does — the caller has just
// re-read it as its last step and a second fetch would only repaint the same answer.
function _rxShowDashboard() {
  const wiz = document.getElementById('rxWizard');
  const dash = document.getElementById('rxDashboard');
  if (wiz) wiz.style.display = 'none';
  if (dash) dash.style.display = '';
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

// ── Solar-system typeahead (shared by the group and account system fields) ─────────────────
// The system name is resolved by exact match server-side and REJECTED if unknown, so a typo is a
// silent no-op that leaves every job-fee estimate light. This is a suggestion list only — the
// input stays free text and still validates on save, so a failed/empty search costs nothing.
const _RX_SYS_TIMERS = {};

function _rxSystemInputHtml(id) {
  return `<div class="ind-search-wrap" style="max-width:260px">
      <input type="text" id="${id}" class="pp-num-input" style="width:100%;box-sizing:border-box"
             placeholder="e.g. Jita" autocomplete="off"
             oninput="_rxSysOnInput('${id}')" onkeydown="_rxSysOnKey('${id}', event)"
             onblur="setTimeout(() => _rxSysHide('${id}'), 150)">
      <div id="${id}Results" class="ind-search-results" style="display:none"></div>
    </div>`;
}

function _rxSysOnInput(id) {
  clearTimeout(_RX_SYS_TIMERS[id]);
  const q = document.getElementById(id).value.trim();
  if (q.length < 2) { _rxSysHide(id); return; }
  _RX_SYS_TIMERS[id] = setTimeout(() => _rxSysSearch(id, q), 200);
}

// Enter takes the first suggestion, same as the Industry product picker.
function _rxSysOnKey(id, ev) {
  if (ev.key === 'Escape') { _rxSysHide(id); return; }
  if (ev.key !== 'Enter') return;
  const box = document.getElementById(`${id}Results`);
  const first = box && box.style.display !== 'none' && box.querySelector('.ind-search-row');
  if (first) { ev.preventDefault(); first.click(); }
}

async function _rxSysSearch(id, q) {
  let d;
  try {
    d = await api('/api/systems/search?q=' + encodeURIComponent(q));
  } catch (e) { _rxSysHide(id); return; }        // endpoint down → plain free-text field
  const box = document.getElementById(`${id}Results`);
  if (!box) return;
  const results = (d && d.results) || [];
  if (!results.length) { _rxSysHide(id); return; }
  box.innerHTML = results.map(x => {
    const where = [x.constellation, x.region].filter(Boolean).join(' · ');
    const sec = x.security == null ? '' : x.security.toFixed(1);
    return `<div class="ind-search-row" onclick="_rxSysPick('${id}', '${_esc(x.system).replace(/'/g, "\\'")}')">`
      + `${_esc(x.system)} <span class="pp-card-hint">${sec ? _esc(sec) + (where ? ' — ' : '') : ''}${_esc(where)}</span></div>`;
  }).join('');
  box.style.display = '';
}

function _rxSysHide(id) {
  const box = document.getElementById(`${id}Results`);
  if (box) box.style.display = 'none';
}

function _rxSysPick(id, name) {
  document.getElementById(id).value = name;
  _rxSysHide(id);
}

// The freight/system/tax fields, built once for both the GROUP default form and the personal
// override. They were two near-identical copies, each carrying its own paragraph explaining the
// same six fields — so the copy drifted and every edit had to be made twice. The two forms are
// never on screen together (the group one is manager-only, in Reactions ⚙; the personal one lives
// in Settings → Structures & Markets and the two onboarding flows), so sharing the markup costs
// the user nothing and halves the words.
function _rxRateFieldsHtml(prefix, mine) {
  const p = mine ? 'Your ' : '';
  return `
    <div class="pp-target-form" style="margin-top:8px">
      <label class="pp-label" for="${prefix}Import">${p || ''}Freight in, ISK/m³</label>
      <input type="number" id="${prefix}Import" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="${prefix}Export">${p || ''}Freight out, ISK/m³</label>
      <input type="number" id="${prefix}Export" class="pp-num-input" style="width:120px">

      <label class="pp-label" for="${prefix}Collateral">Courier collateral %</label>
      <input type="number" id="${prefix}Collateral" class="pp-num-input" style="width:100px" step="0.1">

      <label class="pp-label" for="${prefix}System">Reaction${mine ? ' / build' : ''} system</label>
      ${_rxSystemInputHtml(prefix + 'System')}

      <label class="pp-label" for="${prefix}Tax">Facility tax %</label>
      <input type="number" id="${prefix}Tax" class="pp-num-input" style="width:100px" step="0.1">

      ${_rxTimeEffFieldHtml(prefix, 'Time efficiency %')}
    </div>`;
}

// The two things here that silently change your numbers if you get them wrong. Everything else the
// fields say for themselves.
const _RX_RATE_NOTES = `
  <div class="settings-note">No system set means <b>job install fees are left out</b> of every
    estimate — for manufacturing as well as reactions.</div>
  <div class="settings-note">Time efficiency can't be detected. Compare one real job's duration
    against the formula's raw time and enter the difference.</div>`;

function _rxSettingsFormHtml() {
  return `
    <div class="pp-card-title" style="font-size:13px;margin-top:4px">Group defaults
      <span class="pp-card-hint">— members inherit these unless they set their own</span>
    </div>
    ${_rxRateFieldsHtml('rxSet', false)}
    ${_RX_RATE_NOTES}
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
      <button onclick="_saveRxSettings()">Save</button>
      <span id="rxSettingsMsg" class="pp-card-hint"></span>
    </div>`;
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
    <div class="pp-card-title" style="font-size:13px;margin-top:4px">Your rates
      <span class="pp-card-hint">— yours only; overrides any group default</span>
    </div>
    <div class="pp-card-hint" id="rxAcctSettingsHint" style="margin:6px 0 0"></div>
    ${_rxRateFieldsHtml('rxAcct', true)}
    ${_RX_RATE_NOTES}
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px">
      <button onclick="_saveRxAccountSettings()">Save my rates</button>
      <button class="pp-cancel-btn" onclick="_resetRxAccountSettings()">Use group default</button>
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
  if (!el) return Promise.resolve();
  // Returns its promise so a caller that wants to say "refreshing…" can await the refresh really
  // finishing, rather than guessing.
  return api('/api/reactions/orders')
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
  document.getElementById('rxOrderPrice').value = '';
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
  const price = parseFloat(document.getElementById('rxOrderPrice').value);
  apiSend('POST', '/api/reactions/orders/preview',
          { type_id: o.type_id, target_qty: qty, client_price: price > 0 ? price : null })
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
  const price = parseFloat(document.getElementById('rxOrderPrice').value);
  status.textContent = 'Creating…';
  apiSend('POST', '/api/reactions/orders',
          { type_id: o.type_id, target_qty: qty, client_name: clientName || null, notes: notes || null,
            client_price: price > 0 ? price : null })
    .then(data => {
      _rxCloseNewOrderModal();
      _rxLoadOrders();
      document.getElementById('rxOrderDetailModal').style.display = '';
      _renderRxOrderDetail(data);
    })
    .catch(err => { status.textContent = err.message; });
}

// What the order EARNS. Only a price the user typed can answer it — an order's revenue is what was
// negotiated, not a market rate — so with no price this states the cost and says the profit is not
// set, rather than showing a zero that reads as "this earns nothing".
function _rxOrderProfitHtml(data) {
  const p = data.profit || {};
  const id = data.order && data.order.id;
  // A price is agreed, changed and re-agreed — usually AFTER the work is planned — so it has to be
  // editable wherever the order is shown, not just typed once when it is created.
  const editor = !id ? '' : `
    <div class="rx-mkt-search" style="margin-top:8px">
      <input type="number" id="rxOrderPriceEdit" min="0" step="1000000" style="flex:0 1 200px"
             placeholder="Total ISK for the order" value="${p.client_price == null ? '' : p.client_price}">
      <button class="pp-add-btn" onclick="_rxSaveOrderPrice(${id})">${p.client_price == null ? 'Set price' : 'Update price'}</button>
      <span id="rxOrderPriceMsg" class="pp-card-hint"></span>
    </div>`;
  if (p.client_price == null) {
    return `<div class="pp-card-hint" style="margin-top:10px">No price agreed yet — this is what it
      costs you to produce. Enter what the client pays to see the profit.</div>${editor}`;
  }
  const profit = p.profit || 0;
  const good = profit >= 0;
  return `
    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Profit on this order</div>
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Client pays</span><b>${_fmtIsk(p.client_price)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Cost to produce</span><b>${_fmtIsk(data.cost.total_cost)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Profit</span><b class="${good ? 'an-ok' : 'an-bad'}">${_fmtIsk(profit)}</b></div>
      ${p.margin_pct == null ? '' : `<div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Margin</span><b class="${good ? 'an-ok' : 'an-bad'}">${p.margin_pct.toFixed(1)}%</b></div>`}
      ${p.price_per_unit == null ? '' : `<div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Per unit</span><b>${_fmtIsk(p.price_per_unit)} sold, ${_fmtIsk(data.cost.cost_per_unit)} to make</b></div>`}
    </div>
    ${good ? '' : '<div class="settings-note"><span>This order costs more to produce than the client is paying.</span></div>'}
    ${editor}`;
}

// Save the agreed price on an existing order and re-render from the response, which is the full
// report recomputed — so the profit, margin and the dashboard's own figures all move together.
function _rxSaveOrderPrice(orderId) {
  const el = document.getElementById('rxOrderPriceEdit');
  const msg = document.getElementById('rxOrderPriceMsg');
  const raw = parseFloat(el.value);
  msg.textContent = 'Saving…';
  apiSend('POST', `/api/reactions/orders/${orderId}/price`, { client_price: raw > 0 ? raw : null })
    .then(data => { _renderRxOrderDetail(data); _rxLoadOrders(); _rxReloadPlan(); })
    .catch(err => { msg.textContent = err.message; });
}

// ── Order detail / report view ───────────────────────────────────────────────────────────────

function _rxOpenOrderDetail(orderId) {
  document.getElementById('rxOrderDetailModal').style.display = '';
  document.getElementById('rxOrderDetailContent').innerHTML =
    '<div class="pp-loading"><span class="pp-spinner"></span> Pricing this order against today\'s market…</div>';
  _rxFetchOrderDetail(orderId);
}

function _rxFetchOrderDetail(orderId) {
  // Which order the modal is showing RIGHT NOW. A slow response for an order the user has since
  // closed (or navigated away from) must not paint itself over whatever they are looking at —
  // "I closed the modal and the page suddenly jumped and filled in" is exactly that race.
  _rxOpenOrderId = orderId;
  return api(`/api/reactions/orders/${orderId}`)
    .catch(() => { throw new Error('Failed to load order'); })
    .then(data => {
      if (_rxOpenOrderId !== orderId) return;          // stale response — drop it
      const modal = document.getElementById('rxOrderDetailModal');
      if (modal && modal.style.display === 'none') return;
      _renderRxOrderDetail(data);
    })
    .catch(err => {
      if (_rxOpenOrderId !== orderId) return;
      const el = document.getElementById('rxOrderDetailContent');
      if (el) el.innerHTML = `<div class="pp-empty">${_esc(err.message)}</div>`;
    });
}

let _rxOpenOrderId = null;

// "Clear its jobs" — drop everything this order holds in reaction slots and give it its runs back,
// leaving the order itself alone so it can be assigned again. The counterpart to "Assign next
// batch": before this, re-planning ONE order meant Clear all (the whole account) or cancelling the
// order (which throws it away).
async function _rxClearOrderAssignments(orderId) {
  if (!await ppConfirm('Free every reaction slot this order holds and hand its runs back, so you '
      + 'can assign it again? The order itself is kept. Jobs already running in-game keep running '
      + '— they show up as orphans you can add back to the plan.')) return;
  // Say what is happening: the clear itself is quick, but the modal that follows re-prices the
  // whole chain live, and a button that just sat there looked like nothing had happened.
  const el = document.getElementById('rxOrderDetailContent');
  if (el) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Freeing this order\'s slots…</div>';
  try {
    const res = await apiSend('DELETE', `/api/reactions/orders/${orderId}/assignments`);
    if (el) el.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Slots freed — re-pricing the order…</div>';
    const running = (res && res.running_cleared) || 0;
    toast(`Freed ${res.cleared} planned job${res.cleared === 1 ? '' : 's'}; ${res.runs_returned.toLocaleString()} run${res.runs_returned === 1 ? '' : 's'} back to unassigned.`
      + (running ? ` ${running} was already running in-game and continues as an orphan.` : ''), 'info');
    _rxFetchOrderDetail(orderId);
    _rxLoadOrders();
    _rxLastDashboardData = null;
    _loadReactionsDashboard();
  } catch (e) {
    toastError(e, 'Could not clear this order\'s jobs');
    _rxFetchOrderDetail(orderId);        // put the modal back rather than leaving it spinning
  }
}

function _rxCloseOrderDetail() {
  document.getElementById('rxOrderDetailModal').style.display = 'none';
  _rxOpenOrderId = null;               // anything still in flight for it is now stale
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
  // Folded away by default: the materials table is the longest thing in the modal and pushed the
  // order's own actions off the bottom. The summary carries enough (line count + total ISK) to
  // decide whether opening it is worth it.
  const matCount = (data.materials || []).length;
  const matIsk = (data.materials || []).reduce((s, m) => s + (m.unit_cost || 0) * (m.quantity || 0), 0);

  return `
    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Cost to produce</div>
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Materials</span><b>${_fmtIsk(data.cost.material_cost)}</b></div>
      ${data.cost.job_cost ? `<div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Job install fees</span><b>${_fmtIsk(data.cost.job_cost)}</b></div>` : ''}
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Total</span><b>${_fmtIsk(data.cost.total_cost)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Per unit</span><b>${_fmtIsk(data.cost.cost_per_unit)}</b></div>
    </div>
    ${_rxOrderProfitHtml(data)}

    <div class="pp-card-title" style="margin-top:14px;font-size:14px">Time estimate</div>
    <div class="rx-manual-preview">
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Start to finish</span><b>${data.time.estimated_hours == null ? 'Can&rsquo;t say yet' : '~' + _fmtHours(data.time.estimated_hours)}</b></div>
      <div class="rx-manual-preview-row"><span class="rx-manual-preview-label">Free slots now</span><b>${data.time.free_slots_now}</b></div>
    </div>
    <div class="pp-card-hint">${_esc(data.time.caveat || '')}</div>
    ${(data.time.formula_capped || []).length ? `<div class="pp-card-hint">${_esc((data.time.formula_capped || []).join(', '))} can't use every free slot — a formula is locked while a job runs on it, so that step runs on the formulas you hold.</div>` : ''}
    ${chainNote}
    ${_rxStockCoveredNote(data.stock_covered)}
    ${_rxMissingFormulaWarn(data.missing_formulas)}

    <details class="rx-order-materials" style="margin-top:14px">
      <summary class="pp-card-title rx-fold-summary" style="font-size:14px">
        <span class="rx-fold-caret">▸</span>
        <span>Materials to import <span class="pp-card-hint">— full chain, ${Math.round(o.target_qty).toLocaleString()} units${matCount ? ` · ${matCount} line${matCount === 1 ? '' : 's'} · ${_fmtIsk(matIsk)}` : ''}</span></span>
        ${matCount ? `<button class="pp-add-btn" onclick="event.preventDefault();event.stopPropagation();_rxCopyOrderMaterials(this)">Copy for Janice</button>` : ''}
      </summary>
      ${materialsHtml}
    </details>`;
}

function _renderRxOrderDetail(data) {
  const o = data.order;
  const el = document.getElementById('rxOrderDetailContent');
  const titleEl = document.getElementById('rxOrderDetailTitle');
  if (titleEl.firstChild) titleEl.firstChild.textContent = `${o.name} — order`;
  const remaining = o.top_level_runs - o.assigned_runs;

  const actionButtons = o.status === 'open' ? `
      ${remaining > 0 ? `<button id="rxOrderAssignBtn" onclick="_rxAssignOrderBatch(${o.id})">Assign next batch (${remaining.toLocaleString()} run${remaining === 1 ? '' : 's'} left)</button>` : '<span class="pp-card-hint">Every run has been assigned.</span>'}
      ${o.assigned_runs > 0 ? `<button class="pp-add-btn" onclick="_rxClearOrderAssignments(${o.id})" title="Free every slot this order holds and hand its runs back, so you can assign it again from scratch. The order itself is kept.">Clear its jobs</button>` : ''}
      <button class="pp-add-btn" onclick="_rxCompleteOrder(${o.id})">Mark completed</button>
      <button class="pp-danger-btn" onclick="_rxCancelOrder(${o.id})">Cancel order</button>
      ${o.assigned_runs === 0 ? `<button class="pp-danger-btn" onclick="_rxDeleteOrder(${o.id})">Delete</button>` : ''}
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
  // Assigning an order is the slowest thing on this page — it plans the whole chain, checks
  // formulas and slots per character, writes the rows, and is then followed by three refreshes.
  // Say what is happening at each step: a silent minute reads as a hang, and the page suddenly
  // filling in afterwards reads as a bug.
  if (status) status.textContent = 'Planning the chain and claiming reaction slots…';
  apiSend('POST', `/api/reactions/orders/${orderId}/assign`, {})
    .then(async data => {
      const where = data.characters.map(c => c.character_name).join(', ');
      if (status) status.textContent = `Assigned ${data.runs_assigned.toLocaleString()} run${data.runs_assigned === 1 ? '' : 's'} to ${where} — refreshing…`;
      // Sequential and awaited, so "refreshing" ends when the page really is refreshed, and the
      // three requests don't race each other to re-render the same panels.
      await _rxFetchOrderDetail(orderId);
      await _rxLoadOrders();
      await onReactionsTabOpen();
      if (status) status.textContent = `Assigned ${data.runs_assigned.toLocaleString()} run${data.runs_assigned === 1 ? '' : 's'} to ${where}.`;
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

async function _rxCompleteOrder(orderId) {
  if (!await ppConfirm('Mark this order completed? Any reaction slots reserved for it will be freed.')) return;
  _rxSetOrderStatus(orderId, 'completed');
}

async function _rxCancelOrder(orderId) {
  if (!await ppConfirm('Cancel this order? Any reaction slots reserved for it will be freed.')) return;
  _rxSetOrderStatus(orderId, 'cancelled');
}

async function _rxDeleteOrder(orderId) {
  if (!await ppConfirm('Delete this order? This cannot be undone.')) return;
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
// (per-context `onboarded` flag) and all changes are made from Settings → Structures & Markets,
// which hosts the same market manager + freight forms. The market list + search is a reusable
// component mounted into either the gate (#rxOnboardMarkets) or that settings section
// (#settingsMarketsMgr); _rxMarketMount tracks which one is live.
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
    + `<div class="pp-card-hint" style="margin-top:6px">Characters added here bring reaction slots and market access. Pick a <b>market character</b> that can dock at your structure.</div>`;
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
  const settings = document.getElementById('settingsModal');
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

// ── Rig families: what each fitted rig is FOR ────────────────────────────────────────
// A Standup M-Set rig covers one family of products, so a structure rigged for capital parts does
// nothing for a battleship hull. The list comes from the backend registry (never hardcoded here —
// the labels would drift). Selecting nothing keeps the old meaning: this rig covers everything.
// Rig SIZE is a fitting constraint, not a strength ladder: a Raitaru takes M-Set rigs and no
// M-Set capital-ship rig exists, so the picker only offers what the hull can really carry. Which
// families that is comes from the backend too (`by_hull`) — the rule is game data, not JS logic.
let _rxRigFamilies = null;
let _rxRigByHull = {};
async function _rxLoadRigFamilies() {
  if (_rxRigFamilies) return _rxRigFamilies;
  try {
    const d = await api('/api/markets/rig-families');
    _rxRigFamilies = d.families || [];
    _rxRigByHull = d.by_hull || {};
  } catch (e) { _rxRigFamilies = []; _rxRigByHull = {}; }
  return _rxRigFamilies;
}
function _rxRigRoutingOn() {
  return typeof _featureActive === 'function' && _featureActive('industry_rig_routing');
}
function _rigFamSelect(id, activity, selected, hull) {
  let fams = (_rxRigFamilies || []).filter(f => f.activity === activity);
  const sel = new Set(selected || []);
  const fits = hull ? _rxRigByHull[hull] : null;
  if (fits) {
    // Anything already saved that this hull can't fit stays in the list, still selected, so a
    // save can never silently drop it behind the user's back — it's labelled instead.
    const ok = new Set(fits);
    fams = fams.filter(f => ok.has(f.key) || sel.has(f.key))
               .map(f => ok.has(f.key) ? f : { key: f.key, label: f.label + ' — not fittable here' });
  }
  if (!fams.length) return '';
  return `<select id="${id}" multiple size="4" class="rx-rig-fams">`
    + fams.map(f => `<option value="${f.key}" ${sel.has(f.key) ? 'selected' : ''}>${_esc(f.label)}</option>`).join('')
    + `</select>`;
}
function _rigFamRow(m, kind, activity) {
  if (!_rxRigRoutingOn()) return '';
  const col = { bmmeg: 'me_rig_groups', bmteg: 'te_rig_groups',
                brmeg: 'rx_me_rig_groups', brteg: 'rx_te_rig_groups' }[kind];
  const which = kind.indexOf('me') > 0 ? 'ME' : 'TE';
  return `<div class="rx-rig-row rx-rig-fam-row"><span>${which} rig covers</span>`
    + _rigFamSelect(kind + '-' + m.id, activity, m[col], m.hull)
    + `<span class="pp-card-hint">nothing selected = every group</span></div>`;
}
// ── Pins: what is ALWAYS built here, whatever the routing scores ─────────────────────────────
// A capital builder runs the parts in one structure and the hull in another and wants to SAY so,
// not hope the rig inference lands there. The unit is a rig family (the same registry above, read
// from the backend), and the map is keyed by family, so a family is pinned to exactly one building
// by construction — ticking it here takes it off wherever it was.
//
// Deliberately NOT narrowed by what the hull can fit: a Raitaru cannot fit a capital-ship RIG, but
// you may still choose to build capital ships there and simply earn no rig bonus for it. The fit
// rule decides the bonus; the pin decides the building.
let _rxBuildPins = null;
async function _rxLoadBuildPins() {
  try {
    const d = await api('/api/industry/build-pins');
    _rxBuildPins = d.pins || {};
  } catch (e) { _rxBuildPins = _rxBuildPins || {}; }
  return _rxBuildPins;
}
function _rxPinRow(m) {
  if (!_rxRigRoutingOn() || m.suggested) return '';
  const acts = [];
  if (m.build_mfg) acts.push('manufacturing');
  if (m.build_rx) acts.push('reaction');
  const fams = (_rxRigFamilies || []).filter(f => acts.indexOf(f.activity) >= 0);
  if (!fams.length) return '';
  const mine = _rxBuildPins || {};
  const opts = fams.map(f => `<option value="${f.key}" ${mine[f.key] === 's:' + m.id ? 'selected' : ''}>`
    + `${_esc(f.label)}</option>`).join('');
  return `<div class="rx-rig-row rx-rig-fam-row"><span>Always build here</span>`
    + `<select id="bpin-${m.id}" multiple size="4" class="rx-rig-fams">${opts}</select>`
    + `<span class="pp-card-hint">overrides the routing — nothing selected = let the plan decide</span></div>`;
}

// An impossible claim on an already-saved structure: say which one and what it was inflating,
// and leave the fix to the user rather than rewriting what they configured.
function _rigFitWarnings(m) {
  if (!_rxRigRoutingOn()) return '';
  const w = m.rig_warnings || [];
  if (!w.length) return '';
  return w.map(x => `<div class="rx-rig-warn">${_esc(x.text)}</div>`).join('');
}

// A build structure, configured inline (no ordering — building isn't a priority chain).
function _rxBuildRowHtml(m, d) {
  const hullNote = m.hull ? `<b>${_esc(m.hull)}</b> · ${_esc(m.security || '?')}-sec` : 'type not detected';
  const manual = m.hull ? '' :
    `<div class="rx-rig-row">Structure <select id="bhull-${m.id}"><option value="">—</option>`
    + ['raitaru', 'azbel', 'sotiyo', 'athanor', 'tatara'].map(h => `<option ${m.hull === h ? 'selected' : ''}>${h}</option>`).join('')
    + `</select> in <select id="bsec-${m.id}">`
    + ['high', 'low', 'null'].map(s => `<option value="${s}" ${m.security === s ? 'selected' : ''}>${s}</option>`).join('')
    + `</select></div>`;
  // Only a group manager may put a building in front of the whole alliance — one wrong rig answer
  // would otherwise be adopted by everybody and quoted as an efficiency nobody can see is wrong.
  const share = (d && d.can_manage_group && d.group)
    ? `<button class="pp-add-btn" onclick="_rxShareStructure(${m.id})" title="Offer this building to everyone in ${_esc(d.group.name)}">Share with alliance</button>` : '';
  const bonus = [];
  if (m.build_mfg && m.mfg_bonus) bonus.push(`mfg ME ${m.mfg_bonus.me}% / TE ${m.mfg_bonus.te}%`);
  if (m.build_rx && m.rx_bonus) bonus.push(`rx ME ${m.rx_bonus.me}% / TE ${m.rx_bonus.te}%`);
  return `<div class="rx-build-card">
    <div class="rx-build-hd"><span class="rx-mkt-name">${_esc(m.name)}</span><span class="pp-card-hint">${hullNote}</span>
      <button class="pp-add-btn rx-build-rm" onclick="_rxMarketRemove(${m.id})" title="Remove">✕</button></div>
    ${_rigFitWarnings(m)}
    ${manual}
    <label class="rx-build-chk"><input type="checkbox" id="bm-${m.id}" ${m.build_mfg ? 'checked' : ''}> Manufacture here</label>
    <div class="rx-rig-row">ME rig <select id="bmme-${m.id}">${_rigOpts(m.me_rig)}</select> · TE rig <select id="bmte-${m.id}">${_rigOpts(m.te_rig)}</select></div>
    ${_rigFamRow(m, 'bmmeg', 'manufacturing')}${_rigFamRow(m, 'bmteg', 'manufacturing')}
    <label class="rx-build-chk"><input type="checkbox" id="br-${m.id}" ${m.build_rx ? 'checked' : ''}> React here</label>
    <div class="rx-rig-row">ME rig <select id="brme-${m.id}">${_rigOpts(m.rx_me_rig)}</select> · TE rig <select id="brte-${m.id}">${_rigOpts(m.rx_te_rig)}</select></div>
    ${_rigFamRow(m, 'brmeg', 'reaction')}${_rigFamRow(m, 'brteg', 'reaction')}
    ${_rxPinRow(m)}
    ${m.location_id < 0
      ? `<div class="pp-card-hint">Added by hand — can't be priced from: reading a structure's market needs ESI and its real in-game id.</div>`
      : `<label class="rx-build-chk"><input type="checkbox" id="bp-${m.id}" ${m.price_from ? 'checked' : ''}> Also price from here</label>`}
    <div class="rx-build-foot"><button class="pp-add-btn" onclick="_rxSaveBuild(${m.id})">Save</button>${share}${bonus.length ? `<span class="rx-mkt-build-badge">${bonus.join(' · ')}</span>` : ''}</div>
  </div>`;
}

// ── Describe a structure by hand ─────────────────────────────────────────────────────
// Searching for a structure needs a connected character holding structure-search scopes. Somebody
// who already knows which buildings they run shouldn't have to grant those just to describe one —
// everything after the id (hull, rigs, families, tax) was always typed anyway. Hulls come from the
// backend registry, the system from the shared typeahead, and its SECURITY is derived from that
// system rather than asked for.
let _rxHulls = null;
async function _rxLoadHulls() {
  if (_rxHulls) return _rxHulls;
  try {
    const d = await api('/api/markets/hulls');
    _rxHulls = d.hulls || [];
  } catch (e) { _rxHulls = []; }
  return _rxHulls;
}

function _rxManualOn() {
  return typeof _featureActive === 'function' && _featureActive('industry_manual_structures');
}

function _rxManualFormHtml() {
  if (!_rxManualOn()) return '';
  // Label, not key — the keys are lowercase because they match ESI's hull names, and rendering
  // them raw put "raitaru" / "athanor (reactions)" in the picker. The rig size rides along because
  // it is what decides which rig families the next control will offer.
  const hulls = (_rxHulls || []).map(h =>
    `<option value="${_esc(h.key)}">${_esc(h.label || h.key)}`
    + `${h.rig_size_label ? ` — ${_esc(h.rig_size_label)} rigs` : ''}`
    + `${h.activity === 'reaction' ? ' (reactions)' : ''}</option>`).join('');
  if (!hulls) return '';
  return `<div class="rx-mkt-search" style="flex-wrap:wrap;align-items:flex-start">
      <input id="rxManualName" placeholder="Structure name, e.g. 1DQ1-A - Home Azbel" style="flex:1 1 220px">
      <select id="rxManualHull">${hulls}</select>
      ${_rxSystemInputHtml('rxManualSystem')}
      <button class="pp-add-btn" onclick="_rxManualAdd()">Add by hand</button>
      <div class="settings-note" style="flex:1 1 100%"><span>You can <b>build</b> in a hand-added
        structure but not <b>price</b> from it — reading a market needs its real in-game id.</span></div>
      <span id="rxManualMsg" class="pp-card-hint"></span>
    </div>`;
}

async function _rxManualAdd() {
  const msg = document.getElementById('rxManualMsg');
  const name = (document.getElementById('rxManualName') || {}).value || '';
  const hull = (document.getElementById('rxManualHull') || {}).value || '';
  const system = (document.getElementById('rxManualSystem') || {}).value || '';
  if (!name.trim() || !system.trim()) {
    if (msg) msg.textContent = 'Name and system are both needed.';
    return;
  }
  try {
    _rxMarketData = await apiSend('POST', '/api/markets/manual',
                                  { name: name.trim(), hull: hull, system: system.trim() });
  } catch (e) { toastError(e, 'Could not add that structure'); return; }
  toast('Added — now set its rigs');
  _rxRenderMarketManager();
  if (typeof indPopulateFacility === 'function') indPopulateFacility();
}

// ── Alliance-suggested buildings ─────────────────────────────────────────────────────
// An alliance builds in the same few structures, and describing one — hull, rig tiers, families,
// system, tax — is real work. A manager shares theirs; every other member is OFFERED it and adds
// it with one click. Deliberately a suggestion and not a switch: adopting changes where jobs route
// and what they cost, so it stays the member's decision, and their own row always wins.
function _rxSuggestedHtml(d) {
  const sugg = (d.suggested_structures || []).filter(m => m.kind === 'structure');
  if (!sugg.length) return '';
  const rows = sugg.map(m => {
    const what = [m.build_mfg ? 'manufacturing' : '', m.build_rx ? 'reactions' : '']
      .filter(Boolean).join(' + ') || 'not set up for building';
    const rigs = [];
    if (m.build_mfg && m.mfg_bonus) rigs.push(`mfg ME ${m.mfg_bonus.me}% / TE ${m.mfg_bonus.te}%`);
    if (m.build_rx && m.rx_bonus) rigs.push(`rx ME ${m.rx_bonus.me}% / TE ${m.rx_bonus.te}%`);
    return `<div class="rx-mkt-row"><span class="rx-mkt-kind">${_esc(what)}</span>`
      + `<span class="rx-mkt-name">${_esc(m.name)}</span>`
      + (rigs.length ? `<span class="rx-mkt-build-badge">${_esc(rigs.join(' · '))}</span>` : '')
      + `<span class="rx-mkt-ctrl"><button class="pp-add-btn" onclick="_rxAdoptStructure(${m.id})">Add</button></span></div>`;
  }).join('');
  const who = (sugg[0] && sugg[0].group_name) ? _esc(sugg[0].group_name) : 'your alliance';
  return `<div class="rx-mkt-sec"><div class="rx-mkt-sec-h">Buildings ${who} uses `
    + `<span class="pp-card-hint">added to your own list when you take one — nothing changes until you do</span></div>`
    + `<div class="rx-mkt-list">${rows}</div></div>`;
}

async function _rxAdoptStructure(id) {
  try {
    _rxMarketData = await apiSend('POST', '/api/markets/adopt', { market_id: id });
  } catch (e) { toastError(e, 'Could not add that structure'); return; }
  toast('Added — check its rigs are right for you');
  _rxRenderMarketManager();
  if (typeof indPopulateFacility === 'function') indPopulateFacility();
}

async function _rxShareStructure(id) {
  try {
    _rxMarketData = await apiSend('POST', '/api/markets/share', { market_id: id });
  } catch (e) { toastError(e, 'Could not share that structure'); return; }
  toast('Shared with your alliance');
  _rxRenderMarketManager();
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
      + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Structures &amp; Markets</button> → 🔨 → React here. `
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
    scope: 'account',
  };
  // What each rig is for. Only sent when the picker is on screen — an omitted field leaves the
  // stored value alone, so a save from a build without this UI can't silently blank it.
  const fams = sel => sel ? Array.from(sel.selectedOptions).map(o => o.value) : null;
  [['bmmeg', 'me_rig_groups'], ['bmteg', 'te_rig_groups'],
   ['brmeg', 'rx_me_rig_groups'], ['brteg', 'rx_te_rig_groups']].forEach(([id, key]) => {
    const got = fams(v(id));
    if (got) body[key] = got;
  });
  if (v('bhull')) body.hull = v('bhull').value || null;
  if (v('bsec')) body.security = v('bsec').value || null;
  if (v('bp')) body.price_from = v('bp').checked;
  try {
    _rxMarketData = await apiSend('POST', `/api/markets/${id}/build`, body);
  } catch (e) {}
  // The pins live with the account's other build options, not on the structure row, so they are a
  // second call. Sent as the WHOLE map with this structure's selection rewritten: keyed by family,
  // so ticking one here is what unpins it from wherever it was.
  const pinSel = document.getElementById('bpin-' + id);
  if (pinSel) {
    const key = 's:' + id;
    const next = {};
    Object.keys(_rxBuildPins || {}).forEach(f => { if (_rxBuildPins[f] !== key) next[f] = _rxBuildPins[f]; });
    Array.from(pinSel.selectedOptions).forEach(o => { next[o.value] = key; });
    try {
      const d = await apiSend('POST', '/api/industry/build-pins', { pins: next });
      _rxBuildPins = d.pins || {};
    } catch (e) { toastError(e, 'Could not save what is always built here'); }
  }
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
    ? `<div class="settings-note"><span>Your structure market${structs.length > 1 ? 's' : ''} can't be read, so pricing <b>falls back to Jita</b>. Connect a character that can dock there.</span></div>`
    : (d.connected ? '' : `<div class="pp-card-hint" style="margin:0 0 8px">Searching structures needs a `
        + `connected character — public regions work without one.`
        + ` <button class="ind-link-btn" onclick="connectReactionsMarket()">Connect one</button></div>`);
  return inheritNote + warn
    + `<div class="rx-mkt-sec"><div class="rx-mkt-sec-h">Price against — in priority order</div>`
    + `<div class="rx-mkt-list">${_rxPricingRowsHtml(pricing, editable)}</div>`
    + `<div class="rx-mkt-search">`
    + `<input id="rxMarketSearchInput" placeholder="Search a structure or region…" onkeydown="if(event.key==='Enter')_rxMarketSearch()">`
    + `<button class="pp-add-btn" onclick="_rxMarketSearch()">Search</button>`
    + `<div id="rxMarketSearchResults"></div></div></div>`
    + `<div class="rx-mkt-sec"><div class="rx-mkt-sec-h">Structures you build in <span class="pp-card-hint">their rigs set your ME &amp; TE</span></div>`
    + (build.length ? build.map(m => _rxBuildRowHtml(m, d)).join('') : `<div class="pp-card-hint">None yet — search above and choose <b>+ Build</b> on a structure.</div>`)
    + _rxManualFormHtml()
    + `</div>`
    + _rxSuggestedHtml(d);
}

async function _rxMountMarkets(containerId) {
  _rxMarketMount = containerId;
  // Fetch on demand if nothing has loaded the market list yet. Previously this just called
  // _rxRenderMarketManager(), which returns silently when _rxMarketData is null — so opening
  // Settings -> Structures & Markets WITHOUT first visiting the Reactions tab (the only thing
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
    // Both together: the rig-family registry has to be in hand before the build cards render, or
    // the "what is this rig for" pickers come up empty on the first paint.
    // Hulls ride along for the same reason: the hand-added-structure form can't paint its hull
    // picker before the registry is in hand, and the registry is the only place hulls are named.
    // The pins ride along too: they are drawn on the same build cards, and a card painted before
    // them would show every family unpinned and invite the user to re-tick what is already set.
    const [d] = await Promise.all([api('/api/markets'), _rxLoadRigFamilies(), _rxLoadHulls(),
                                   _rxLoadBuildPins()]);
    _rxMarketData = d;
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
      ? `<div class="pp-card-hint">No matches. Searching for a <b>structure</b> needs a connected
           character — public regions work without one.</div>`
        + `<div class="settings-connect-row"><button class="pp-connect-btn" onclick="connectReactionsMarket()">`
        + `Connect a character</button></div>`
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
