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
  if (!el) return;
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
      Ship+collateral assumes 1200 ISK/m³ + 0.5% collateral on Jita sell value ·
      Buy/sell depth = current Jita order-book units, not daily trade volume — a rough liquidity signal only.
    </div>`;
}
