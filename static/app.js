function fmt(n) { return n.toLocaleString('en-US'); }
function fmtDuration(hours) {
  if (hours < 24) return hours + 'h';
  const d = Math.floor(hours / 24);
  const h = hours % 24;
  return h > 0 ? `${d}d ${h}h` : `${d}d`;
}
const TIER_OUTPUT = { P2: 5, P3: 3, P4: 1 };
function execCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (_) {}
  document.body.removeChild(ta);
  if (cb) cb();
}
function pct(used, available) {
  if (!available) return '';
  return Math.round(used / available * 100) + '%';
}

// ── Sharing ──────────────────────────────────────────────────────────────────

async function createShareLink(inventoryText) {
  try {
    const data = await apiSend('POST', '/api/share', { inventory: inventoryText });
    history.replaceState(null, '', '#s=' + data.id);
  } catch (e) { toastError(e, 'Could not create the share link'); }
}

async function copyLink() {
  const btn = document.getElementById('copyBtn');
  try {
    await navigator.clipboard.writeText(location.href);
    btn.textContent = 'Copied!';
  } catch { btn.textContent = 'Failed'; }
  setTimeout(() => btn.textContent = 'Copy link', 1800);
}

async function loadFromHash() {
  const hash = location.hash;
  if (!hash.startsWith('#s=')) return;
  try {
    const data = await api('/api/share/' + hash.slice(3));
    document.getElementById('inv').value = data.inventory;
    analyze();
  } catch (e) { console.error('Failed to load share:', e); }
}

// ── Analyze ───────────────────────────────────────────────────────────────────

async function analyze() {
  const inv = document.getElementById('inv').value.trim();
  if (!inv) return;
  await runAction('analyze', { inventory: inv }, '/api/analyze');
}

// ── Optimize ──────────────────────────────────────────────────────────────────

async function optimize() {
  const inv = document.getElementById('inv').value.trim();
  const order = document.getElementById('orderText').value.trim();
  if (!inv) return;
  await runAction('optimize', { inventory: inv, order }, '/api/optimize');
}

// ── Shared action runner ──────────────────────────────────────────────────────

async function runAction(mode, payload, url) {
  const status = document.getElementById('status');
  const copyBtn = document.getElementById('copyBtn');
  document.getElementById('analyzeBtn').disabled = true;
  document.getElementById('optimizeBtn').disabled = true;
  status.textContent = mode === 'optimize' ? 'Optimizing...' : 'Analyzing...';
  status.className = '';

  try {
    const data = await apiSend('POST', url, payload);

    renderInventoryPills(data);

    if (mode === 'optimize') {
      document.getElementById('resultsSection').style.display = 'none';
      renderOptimize(data);
    } else {
      document.getElementById('optSection').style.display = 'none';
      renderAnalyze(data);
    }

    await createShareLink(payload.inventory);
    copyBtn.style.display = 'inline-block';
    status.textContent = '';
  } catch (e) {
    status.textContent = e.message;
    status.className = 'error';
  } finally {
    document.getElementById('analyzeBtn').disabled = false;
    document.getElementById('optimizeBtn').disabled = false;
  }
}

// ── Inventory pills ───────────────────────────────────────────────────────────

function renderInventoryPills(data) {
  const summary = document.getElementById('invSummary');
  const pills = document.getElementById('invPills');
  pills.innerHTML = '';
  const entries = Object.entries(data.inventory || {});
  if (entries.length) {
    entries.forEach(([name, qty]) => {
      const d = document.createElement('div');
      d.className = 'pill';
      d.innerHTML = `${name} <span>${fmt(qty)}</span>`;
      pills.appendChild(d);
    });
    summary.style.display = 'block';
  } else {
    summary.style.display = 'none';
  }
}

// ── Optimize results ──────────────────────────────────────────────────────────

function renderOptimize(data) {
  const section = document.getElementById('optSection');
  section.style.display = 'block';

  // Stats tiles (matches the Planetary Planning / Setup Analysis aesthetic)
  const statsBar = document.getElementById('optStatsBar');
  const producedCount = (data.plan || []).filter(p => p.quantity > 0).length;
  const tiles = [
    `<div class="plan-stat"><span class="plan-stat-val">${data.utilization}%</span><span class="plan-stat-lbl">Utilization</span></div>`,
  ];
  if (data.total_isk > 0) tiles.push(
    `<div class="plan-stat"><span class="plan-stat-val plan-stat-ok">${fmtIsk(data.total_isk)}</span><span class="plan-stat-lbl">Revenue (ISK)</span></div>`);
  tiles.push(
    `<div class="plan-stat"><span class="plan-stat-val">${producedCount}</span><span class="plan-stat-lbl">Items</span></div>`);
  statsBar.innerHTML = tiles.join('');

  // Warnings
  const warnings = document.getElementById('optWarnings');
  warnings.innerHTML = '';
  if (data.not_producible && data.not_producible.length) {
    data.not_producible.forEach(item => {
      const d = document.createElement('div');
      d.className = 'opt-warning';
      const missingHtml = item.missing && item.missing.length
        ? ` — missing: <strong>${item.missing.join(', ')}</strong>`
        : ' — inputs unresolvable';
      d.innerHTML = `<strong>${item.name}</strong> not producible${missingHtml}`;
      warnings.appendChild(d);
    });
  }
  if (data.error) {
    const d = document.createElement('div');
    d.className = 'opt-warning error';
    d.textContent = `Optimizer: ${data.error}`;
    warnings.appendChild(d);
  }

  // Plan table
  const body = document.getElementById('optBody');
  body.innerHTML = '';

  const plan = data.plan || [];
  if (!plan.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="7" class="no-results">No production possible from current inventory</td>';
    body.appendChild(tr);
  } else {
    plan.forEach(item => {
      const tr = document.createElement('tr');
      if (item.is_ordered) tr.classList.add('ordered-row');

      const fillCell = item.fill_pct !== null
        ? `<span class="fill-pct ${item.fill_pct >= 100 ? 'fill-full' : item.fill_pct >= 50 ? 'fill-partial' : 'fill-low'}">${item.fill_pct}%</span>`
        : '<span class="isk-na">—</span>';

      const orderCell = item.order_qty ? fmt(item.order_qty) : '<span class="isk-na">—</span>';

      tr.innerHTML = `
        <td class="item-name">${item.name}</td>
        <td><span class="tier-badge-sm badge-${item.tier.toLowerCase()}">${item.tier}</span></td>
        <td class="qty">${fmt(item.quantity)}</td>
        <td class="qty-order">${orderCell}</td>
        <td>${fillCell}</td>
        <td class="isk-cell"><span class="isk-unit">${item.sell_price > 0 ? fmtIsk(item.sell_price) : '—'}</span></td>
        <td class="isk-cell"><span class="isk-total">${item.total_isk > 0 ? fmtIsk(item.total_isk) : '—'}</span></td>
      `;
      body.appendChild(tr);
    });
  }

  // Leftover
  const leftoverSection = document.getElementById('leftoverSection');
  const leftoverPills = document.getElementById('leftoverPills');
  leftoverPills.innerHTML = '';
  const leftover = data.leftover || {};
  const leftoverEntries = Object.entries(leftover);
  if (leftoverEntries.length) {
    leftoverEntries.forEach(([name, qty]) => {
      const d = document.createElement('div');
      d.className = 'pill pill-warn';
      d.innerHTML = `${name} <span>${fmt(qty)}</span>`;
      leftoverPills.appendChild(d);
    });
    leftoverSection.style.display = 'block';
  } else {
    leftoverSection.style.display = 'none';
  }

  // Factory split
  renderFactorySplit(data.plan || []);
}

// ── Analyze results ───────────────────────────────────────────────────────────

function renderAnalyze(data) {
  document.getElementById('resultsSection').style.display = 'block';
  renderTier('p2', data.results.p2 || []);
  renderTier('p3', data.results.p3 || []);
  renderTier('p4', data.results.p4 || []);
}

function renderTier(tier, items) {
  const body = document.getElementById(tier + 'Body');
  const count = document.getElementById(tier + 'Count');
  body.innerHTML = '';
  count.textContent = items.length ? `${items.length} item${items.length !== 1 ? 's' : ''}` : '';

  if (!items.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="7" class="no-results">No ${tier.toUpperCase()} items producible from current inventory</td>`;
    body.appendChild(tr);
    return;
  }

  items.forEach(item => {
    const tr = document.createElement('tr');
    const breakdownHtml = item.p1_costs.map(c => {
      const p = pct(c.total_used, c.available);
      return `<div class="p1-row">
        <span class="p1-name">${c.name}</span>
        <span class="p1-used">${fmt(c.total_used)}</span>
        ${p ? `<span class="p1-pct">(${p} of ${fmt(c.available)})</span>` : ''}
      </div>`;
    }).join('');

    tr.innerHTML = `
      <td class="item-name">${item.name}</td>
      <td class="qty">${fmt(item.max_output)}</td>
      <td class="isk-cell"><span class="isk-unit">${item.sell_price > 0 ? fmtIsk(item.sell_price) : '—'}</span></td>
      <td class="isk-cell"><span class="isk-total">${item.total_isk > 0 ? fmtIsk(item.total_isk) : '—'}</span></td>
      <td class="limiting">${item.limiting_input}</td>
      <td class="p1-breakdown">${breakdownHtml}</td>
      <td></td>
    `;
    const splitBtn = document.createElement('button');
    splitBtn.className = 'split-btn';
    splitBtn.textContent = 'Split';
    splitBtn.addEventListener('click', () => showAnalysisSplit(item, tier.toUpperCase()));
    tr.lastElementChild.appendChild(splitBtn);
    body.appendChild(tr);
  });
}

function showAnalysisSplit(item, tierLabel) {
  const planItem = {
    name: item.name,
    tier: tierLabel,
    quantity: item.max_output,
    is_ordered: false,
    inputs: (item.p1_costs || []).map(c => ({ name: c.name, per_unit: c.per_unit })),
  };
  renderFactorySplit([planItem]);
  document.getElementById('factorySection').scrollIntoView({ behavior: 'smooth' });
}

// ── Factory pipeline summary ──────────────────────────────────────────────────

function renderPipelineSummary(items, n) {
  const existing = document.getElementById('pipelineSummary');
  if (existing) existing.remove();

  // Collect tiers present in the split
  const tiers = [...new Set(items.map(i => i.tier))];
  if (!tiers.length) return;

  // Build one rate line per tier present
  const tierInfo = {
    P2: { out: 5, label: 'P2 (Advanced Industry)' },
    P3: { out: 3, label: 'P3 (High-Tech Industry)' },
    P4: { out: 1, label: 'P4 (High-Tech Industry)' },
  };

  const lines = tiers.map(tier => {
    const info = tierInfo[tier];
    if (!info) return null;
    const rateHour = info.out;
    const rateDay = info.out * 24;
    return `<span class="pipe-label">${info.label}:</span> <span class="pipe-val">${rateHour}/hour per factory · ${rateDay}/day per factory · 1h cycle</span>`;
  }).filter(Boolean);

  if (!lines.length) return;

  const div = document.createElement('div');
  div.id = 'pipelineSummary';
  div.className = 'pipeline-summary';
  const note = `<div class="pipe-note">Final-step facility rates — assumes the inputs above are already on hand. ` +
    `(Building the whole P0→Px chain on one planet is slower, e.g. a P4 factory ≈ 0.5/hour — see the Planetary Planning / Factory Layout tabs.)</div>`;
  div.innerHTML = lines.map(l => `<div class="pipe-row">${l}</div>`).join('') + note;
  document.getElementById('factorySection').appendChild(div);
}

// ── Factory split ─────────────────────────────────────────────────────────────

let _lastOptPlan = null;

function updateFactorySplit() {
  if (_lastOptPlan) renderFactorySplit(_lastOptPlan);
}

function renderFactorySplit(plan) {
  _lastOptPlan = plan;
  const n = Math.max(1, parseInt(document.getElementById('factories').value) || 15);
  document.getElementById('factoryCountLabel').textContent = n;

  const items = plan.filter(item => item.quantity > 0 && item.inputs && item.inputs.length > 0);
  if (!items.length) {
    document.getElementById('factorySection').style.display = 'none';
    return;
  }

  // Collect all unique input names across all items, sorted alphabetically
  const seenNames = new Set();
  items.forEach(item => item.inputs.forEach(inp => seenNames.add(inp.name)));
  const allInputNames = [...seenNames].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  // Build header
  const head = document.getElementById('factoryHead');
  head.innerHTML = '';
  const hr = document.createElement('tr');
  hr.innerHTML = `<th>Item</th><th>Tier</th><th>Qty / factory</th><th>Duration</th>` +
    allInputNames.map(n => `<th>${n}</th>`).join('');
  head.appendChild(hr);

  // Build body — numbers are plain integers (no separators) for easy copy-paste
  const body = document.getElementById('factoryBody');
  body.innerHTML = '';
  items.forEach(item => {
    const perFactory = Math.floor(item.quantity / n);
    if (perFactory === 0) return;

    const outPerCycle = TIER_OUTPUT[item.tier] || 1;
    const cycles = Math.ceil(perFactory / outPerCycle);
    const durationCell = `<td class="fac-duration">${fmtDuration(cycles)}</td>`;

    const inputMap = {};
    item.inputs.forEach(inp => { inputMap[inp.name] = inp.per_unit; });

    const inputCells = allInputNames.map(name => {
      const perUnit = inputMap[name] || 0;
      const qty = Math.ceil(perUnit * perFactory);
      return qty > 0
        ? `<td class="qty fac-num">${qty}</td>`
        : `<td class="isk-na">—</td>`;
    }).join('');

    const tr = document.createElement('tr');
    if (item.is_ordered) tr.classList.add('ordered-row');
    tr.innerHTML = `
      <td class="item-name">${item.name}</td>
      <td><span class="tier-badge-sm badge-${item.tier.toLowerCase()}">${item.tier}</span></td>
      <td class="qty fac-num">${perFactory}</td>
      ${durationCell}
      ${inputCells}
    `;
    body.appendChild(tr);
  });

  // Remainder row (units that don't divide evenly)
  const hasRemainder = items.some(item => item.quantity % n !== 0);
  if (hasRemainder) {
    const tr = document.createElement('tr');
    const remCells = allInputNames.map(() => '<td></td>').join('');
    tr.innerHTML = `<td colspan="4" style="color:#4a5470;font-size:11px;font-style:italic;">
      Remainder (not assigned to factories):</td>${remCells}`;
    body.appendChild(tr);
    items.forEach(item => {
      const rem = item.quantity % n;
      if (rem === 0) return;
      const inputMap = {};
      item.inputs.forEach(inp => { inputMap[inp.name] = inp.per_unit; });
      const remCells2 = allInputNames.map(name => {
        const qty = Math.ceil((inputMap[name] || 0) * rem);
        return qty > 0 ? `<td class="qty fac-num" style="color:#5a6080">${qty}</td>` : '<td class="isk-na">—</td>';
      }).join('');
      const tr2 = document.createElement('tr');
      tr2.innerHTML = `<td class="item-name" style="color:#6a7a90">+${rem} ${item.name}</td><td></td><td class="qty fac-num" style="color:#5a6080">+${rem}</td><td></td>${remCells2}`;
      body.appendChild(tr2);
    });
  }

  // Pipeline throughput summary
  renderPipelineSummary(items, n);

  document.getElementById('factorySection').style.display = 'block';

  // Click-to-copy on numeric cells
  document.querySelectorAll('.fac-num').forEach(td => {
    td.title = 'Click to copy';
    td.style.cursor = 'pointer';
    td.addEventListener('click', () => {
      const raw = td.textContent.replace(/\D/g, '');
      const prev = td.textContent;
      const confirm = () => { td.textContent = '✓'; setTimeout(() => td.textContent = prev, 700); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(raw).then(confirm).catch(() => execCopy(raw, confirm));
      } else {
        execCopy(raw, confirm);
      }
    });
  });
}

// ── Tab navigation ────────────────────────────────────────────────────────────


// Help copy is fetched, not shipped in index.html. It is documentation — long, static, and read by
// a fraction of visitors — so inlining it made every page load carry it. Fetched once per session
// and cached in the DOM; a failure leaves a link rather than an empty card, because the content is
// a plain file the user can open directly.
const _HELP_SRC = {indhowitworks: '/help/industry.html'};
const _helpLoaded = {};

async function loadHelpPanel(name) {
  const el = document.getElementById('tab-' + name + '-body');
  const src = _HELP_SRC[name];
  if (!el || !src || _helpLoaded[name]) return;
  try {
    const r = await fetch(src, {credentials: 'same-origin'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    el.innerHTML = await r.text();
    _helpLoaded[name] = true;
  } catch (e) {
    el.innerHTML = '<div class="pp-empty">Could not load this page. '
      + '<a href="' + src + '" target="_blank" rel="noopener">Open it directly</a>.</div>';
  }
}

function switchTab(name) {
  // Defense-in-depth: nav buttons for a restricted page are hidden (_applyPageRestriction), but
  // a stale localStorage 'activeTab' or a direct call could still reach here — bounce to the
  // first page the caller's group actually allows instead of rendering a blocked page.
  if (typeof _isPageRestricted === 'function' && _isPageRestricted(name)) name = _firstAllowedPage();
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = p.id === 'tab-' + name ? '' : 'none');
  // The Admin sub-nav (#adminNavGroup / mobile equivalent) tracks its own "which admin page" state
  // independently of these top-level tabs (see adminSubPage in admin.js) — nothing else clears its
  // highlight, so it stayed lit on whichever admin page was last visited even after leaving Admin
  // entirely. Clear it here; re-entering Admin re-applies the right one via onAdminTabOpen.
  if (name !== 'admin') {
    document.querySelectorAll('#adminNavGroup .admin-nav-item, #adminMobilePageNav .admin-mobile-page-btn')
      .forEach(b => b.classList.remove('active'));
  }
  if (name === 'dashboard' && typeof onDashboardTabOpen === 'function') onDashboardTabOpen();
  if (name === 'planner' && typeof onPlannerTabOpen === 'function') onPlannerTabOpen();
  if (name === 'planetary' && typeof onPlanetaryTabOpen === 'function') onPlanetaryTabOpen();
  if (name === 'planetdb' && typeof onPlanetDbTabOpen === 'function') onPlanetDbTabOpen();
  if (name === 'reactions' && typeof onReactionsTabOpen === 'function') onReactionsTabOpen();
  if (name === 'industry' && typeof onIndustryTabOpen === 'function') onIndustryTabOpen();
  if (name === 'layout' && typeof onLayoutTabOpen === 'function') onLayoutTabOpen();
  if (name === 'characters' && typeof loadCharacters === 'function') loadCharacters();
  if (name === 'analyze' && typeof onAnalyzeTabOpen === 'function') onAnalyzeTabOpen();
  if (name === 'admin' && typeof onAdminTabOpen === 'function') onAdminTabOpen();
  if (name === 'indhowitworks') loadHelpPanel(name);
  localStorage.setItem('activeTab', name);
}

function toggleSidebar() {
  const collapsed = document.body.classList.toggle('nav-collapsed');
  localStorage.setItem('navCollapsed', collapsed ? '1' : '0');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
    switchTab(t.dataset.tab);
    // PI Planner sub-items (Find Buildables / Refill a plan) carry a data-pimode.
    if (t.dataset.pimode && typeof setPiMode === 'function') setPiMode(t.dataset.pimode);
  }));
  if (localStorage.getItem('navCollapsed') === '1') document.body.classList.add('nav-collapsed');
  // A planetary share link (/s/<id> path or the id injected by the server route) is a
  // one-shot "open this plan" intent → land on the plan once. _tryRestoreFromHash then
  // strips the id from the URL, so a later refresh honours the user's last tab instead of
  // forcing the plan again. Without a share link, just restore the last active tab.
  const hasPlanetaryShare = window.__SHARE_ID__ || /^\/s\//.test(location.pathname);
  const saved = localStorage.getItem('activeTab');
  // On phones the heavy tools (planner/planetary/layout/planetdb/characters) are hidden
  // from the bottom tab bar, so never land on one — fall back to the Dashboard. A shared
  // plan link is still an explicit deep-link and opens the plan view regardless.
  const MOBILE_TABS = ['dashboard', 'analyze', 'howitworks', 'characters', 'admin'];
  const isMobile = window.matchMedia('(max-width: 760px)').matches;
  if (hasPlanetaryShare) switchTab('planetary');
  else if (isMobile) switchTab(saved && MOBILE_TABS.includes(saved) ? saved : 'dashboard');
  else if (saved) switchTab(saved);
  // Load session/character state on every page load so the header + Dashboard nav populate
  // (and a logged-in player with no remembered tab lands on the Dashboard).
  if (typeof loadCharacters === 'function') loadCharacters();
  // Cache-hint ticker: next_data_at is a fixed timestamp from the last /api/characters load,
  // and nothing else re-renders the header between rescans/reloads, so the "cached until
  // HH:MM" hint would otherwise keep showing long after HH:MM actually passed. Recompute from
  // already-loaded data (no network call) every 30s so it clears itself on time.
  setInterval(() => {
    if (_sessionLoaded && typeof renderHeaderSession === 'function') {
      renderHeaderSession(_loggedIn, _ppCharsData, _ppSessionCharId);
    }
  }, 30000);
});

document.getElementById('inv').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) analyze();
});
// The single inventory paste also drives the refill split below (shared input).
document.getElementById('inv').addEventListener('input', () => {
  if (typeof syncRefillFromInventory === 'function') syncRefillFromInventory();
});

window.addEventListener('DOMContentLoaded', loadFromHash);

// Prevent number inputs from consuming scroll events — blur on wheel so page scrolls normally
document.addEventListener('wheel', () => {
  if (document.activeElement && document.activeElement.type === 'number') {
    document.activeElement.blur();
  }
}, { passive: true });

// ── Global modal dismissal: click the backdrop (outside the box) or press Escape ───────────────
// Applies to every .pp-modal on the page. Each modal carries its own ✕ button (.pp-modal-close)
// with the correct close+cleanup handler, so we invoke THAT rather than just hiding the element —
// keeps per-modal state (e.g. _settingsOpen, dropdown resets) in sync. Falls back to display:none
// if a modal ever lacks a close button.
function _dismissModal(modal) {
  const btn = modal.querySelector('.pp-modal-close');
  if (btn) btn.click(); else modal.style.display = 'none';
}
document.addEventListener('click', e => {
  // Only a click directly on the backdrop (the .pp-modal itself, not its inner box) closes it.
  const t = e.target;
  if (t.classList && t.classList.contains('pp-modal') && t.style.display !== 'none') _dismissModal(t);
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  // A nested control (e.g. an open product-search dropdown) handles Escape first and stops
  // propagation, so we only get here when nothing inner claimed it — then close the topmost
  // (last in DOM) visible modal.
  const open = Array.from(document.querySelectorAll('.pp-modal')).filter(m => m.style.display !== 'none');
  if (open.length) _dismissModal(open[open.length - 1]);
});

// ── Silent auto-refresh (Dashboard + Reactions) ────────────────────────────────
// Values on these two views are time-derived (% complete meters, time-left, runtimes), so they go
// stale as the clock advances even without new ESI data. On a user-configurable interval (Settings
// → General, default 300s, 0 = off) we silently re-fetch and re-render the ACTIVE one of the two —
// no spinner (the load functions only show one on first load, which has long since happened) and no
// ESI rescan (just the cheap computed endpoint). Skipped when the page is hidden, when logged out,
// or while a manual Rescan is running. Also fires once when the page becomes visible again, so
// returning to a long-open tab snaps the numbers fresh.
const _AUTO_REFRESH_DEFAULT = 300;   // seconds
let _lastAutoRefresh = 0;
let _autoRefreshTimer = null;
function _autoRefreshSeconds() {
  const v = parseInt(localStorage.getItem('autoRefreshSeconds'), 10);
  if (isNaN(v) || v < 0) return _AUTO_REFRESH_DEFAULT;
  if (v > 0 && v < 30) return 30;    // floor so a typo can't hammer the endpoints
  return v;                          // 0 = off
}
function _applyAutoRefresh() {
  if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
  const secs = _autoRefreshSeconds();
  if (secs > 0) _autoRefreshTimer = setInterval(_autoRefreshTick, secs * 1000);
}
function _autoRefreshTick() {
  if (document.hidden || !_loggedIn) return;
  if (typeof _rescanning !== 'undefined' && _rescanning) return;  // don't fight a manual rescan
  const tab = localStorage.getItem('activeTab');
  if (tab !== 'dashboard' && tab !== 'reactions') return;
  _lastAutoRefresh = Date.now();
  // Silent poll: re-render ONLY on a successful, still-logged-in response. A background poll can
  // occasionally come back not-ok or logged_in:false (a transient blip — the real session is fine,
  // proven by a manual tab switch recovering instantly); it must NOT clobber the view with a login
  // screen. So on anything unexpected we skip this tick and try again next interval, rather than
  // routing through the normal load functions (which render the logged-out state).
  if (tab === 'dashboard') {
    api('/api/dashboard')
      .then(d => { if (d && d.logged_in && typeof renderDashboard === 'function') renderDashboard(d); })
      .catch(() => {});
  } else {
    api('/api/reactions/jobs')
      .then(d => { if (d && typeof _renderReactionsDashboard === 'function') { _rxLastDashboardData = d; _renderReactionsDashboard(d); } })
      .catch(() => {});
  }
}
_applyAutoRefresh();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && _autoRefreshSeconds() > 0 && Date.now() - _lastAutoRefresh > 2 * 60 * 1000) _autoRefreshTick();
});

// ── Pull-to-refresh (mobile) ───────────────────────────────────────────────────
// Two-stage gesture from the top of the page. Standalone home-screen apps have no browser refresh
// button, so a SHORT pull just reloads the page (the common "get me a fresh page" need). Keep
// pulling PAST a second, longer threshold to instead run a full ESI colony Rescan (heavier, and
// only offered when logged in). Passive listeners (we never block scrolling); a small banner
// slides in to signal which action a release will take.
(function setupPullToRefresh() {
  const RELOAD_THRESHOLD = 70;    // short pull → reload the page
  const RESCAN_THRESHOLD = 150;   // keep pulling → full ESI rescan (logged in only)
  const MAX_PULL = 180;
  let startY = 0, pulling = false, mode = 0, ind = null;  // mode: 0 none · 1 reload · 2 rescan

  const isPhone = () => window.matchMedia('(max-width: 760px)').matches;
  const atTop = () => (window.scrollY || document.documentElement.scrollTop || 0) <= 0;
  const canRescan = () => !!document.getElementById('rescanBtn'); // present only when logged in

  function indicator() {
    if (!ind) {
      ind = document.createElement('div');
      ind.id = 'ptr-indicator';
      document.body.appendChild(ind);
    }
    return ind;
  }
  function show(px, text) {
    const el = indicator();
    el.textContent = text;
    el.style.transition = 'none';
    el.style.transform = `translateY(${px}px)`;
  }
  function hide() {
    if (!ind) return;
    ind.style.transition = 'transform 0.2s ease';
    ind.style.transform = 'translateY(0)';
  }

  window.addEventListener('touchstart', e => {
    if (!isPhone() || !atTop() || e.touches.length !== 1) { pulling = false; return; }
    startY = e.touches[0].clientY; pulling = true; mode = 0;
  }, { passive: true });

  window.addEventListener('touchmove', e => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 0 || !atTop()) { pulling = false; hide(); return; }
    const pull = Math.min(dy * 0.5, MAX_PULL);     // damped
    if (pull >= RESCAN_THRESHOLD && canRescan()) mode = 2;
    else if (pull >= RELOAD_THRESHOLD) mode = 1;
    else mode = 0;
    const text = mode === 2 ? '⟳  Release to rescan (ESI)'
               : mode === 1 ? '↻  Release to reload'
               : (canRescan() ? '↓  Pull to reload · keep pulling to rescan' : '↓  Pull to reload');
    show(pull, text);
  }, { passive: true });

  window.addEventListener('touchend', () => {
    if (!pulling) return;
    pulling = false;
    if (mode === 2 && canRescan() && typeof rescanAll === 'function') {
      show(MAX_PULL, '⟳  Rescanning…');
      rescanAll();
      setTimeout(hide, 900);
    } else if (mode === 1) {
      show(MAX_PULL, '↻  Reloading…');
      setTimeout(() => location.reload(), 150);
    } else {
      hide();
    }
    mode = 0;
  });
})();
