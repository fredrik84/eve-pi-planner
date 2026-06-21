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
function fmtIsk(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + ' B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + ' M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' K';
  return n.toFixed(0);
}
function pct(used, available) {
  if (!available) return '';
  return Math.round(used / available * 100) + '%';
}

// ── Sharing ──────────────────────────────────────────────────────────────────

async function createShareLink(inventoryText) {
  const resp = await fetch('/api/share', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ inventory: inventoryText }),
  });
  const data = await resp.json();
  history.replaceState(null, '', '#s=' + data.id);
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
    const resp = await fetch('/api/share/' + hash.slice(3));
    if (!resp.ok) return;
    const data = await resp.json();
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
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const data = await resp.json();

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

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = p.id === 'tab-' + name ? '' : 'none');
  if (name === 'dashboard' && typeof onDashboardTabOpen === 'function') onDashboardTabOpen();
  if (name === 'planner' && typeof onPlannerTabOpen === 'function') onPlannerTabOpen();
  if (name === 'planetary' && typeof onPlanetaryTabOpen === 'function') onPlanetaryTabOpen();
  if (name === 'planetdb' && typeof onPlanetDbTabOpen === 'function') onPlanetDbTabOpen();
  if (name === 'layout' && typeof onLayoutTabOpen === 'function') onLayoutTabOpen();
  if (name === 'characters' && typeof loadCharacters === 'function') loadCharacters();
  if (name === 'analyze' && typeof onAnalyzeTabOpen === 'function') onAnalyzeTabOpen();
  if (name === 'admin' && typeof onAdminTabOpen === 'function') onAdminTabOpen();
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
  const MOBILE_TABS = ['dashboard', 'analyze', 'howitworks', 'contribute', 'admin'];
  const isMobile = window.matchMedia('(max-width: 760px)').matches;
  if (hasPlanetaryShare) switchTab('planetary');
  else if (isMobile) switchTab(saved && MOBILE_TABS.includes(saved) ? saved : 'dashboard');
  else if (saved) switchTab(saved);
  // Load session/character state on every page load so the header + Dashboard nav populate
  // (and a logged-in player with no remembered tab lands on the Dashboard).
  if (typeof loadCharacters === 'function') loadCharacters();
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

// ── Pull-to-refresh (mobile) ───────────────────────────────────────────────────
// Dragging down from the very top of the page triggers a colony Rescan — the same
// action as the header Rescan button (which only exists when logged in). Standalone
// home-screen apps have no native pull-to-refresh, so we provide our own. Passive
// listeners (we never block scrolling); a small banner slides in to signal state.
(function setupPullToRefresh() {
  const THRESHOLD = 56;   // px of pull needed to arm a refresh
  const MAX_PULL = 90;
  let startY = 0, pulling = false, armed = false, ind = null;

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
    if (!isPhone() || !atTop() || !canRescan() || e.touches.length !== 1) { pulling = false; return; }
    startY = e.touches[0].clientY; pulling = true; armed = false;
  }, { passive: true });

  window.addEventListener('touchmove', e => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 0 || !atTop()) { pulling = false; hide(); return; }
    const pull = Math.min(dy * 0.5, MAX_PULL);     // damped
    armed = pull >= THRESHOLD;
    show(pull, armed ? '↻  Release to refresh' : '↓  Pull to refresh');
  }, { passive: true });

  window.addEventListener('touchend', () => {
    if (!pulling) return;
    pulling = false;
    if (armed && canRescan() && typeof rescanAll === 'function') {
      show(MAX_PULL, '↻  Refreshing…');
      rescanAll();
      setTimeout(hide, 900);
    } else {
      hide();
    }
    armed = false;
  });
})();
