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
    // ...and OPEN the page that analysis renders on. This filled the box and ran the analysis on
    // whichever page the recipient's browser happened to restore, so the one thing the link was
    // sent for was off screen. `fromHistory` because the URL is corrected below, in one step, and
    // pushing here would drop the `#s=` fragment the link is made of.
    if (typeof switchTab === 'function') switchTab('analyze', { fromHistory: true });
    const slug = (typeof TAB_SLUGS === 'object' && TAB_SLUGS.analyze) || '';
    // Only claim the page we actually landed on. Gating can refuse the switch — and rewriting the
    // bar regardless would have it name Setup Analysis while How it works is on screen, AND drop
    // the `#s=` fragment, making the share unrecoverable by reload. Checked rather than assumed.
    if (slug && location.pathname !== slug && currentTab() === 'analyze') {
      // Keep the fragment: it is still the share, so a refresh reloads the same analysis. Replace
      // rather than push — arriving on a link is not a navigation the user can go "back" from.
      try { history.replaceState(null, '', slug + location.hash); } catch (e) {}
    }
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


// ── Routing: a page is a URL, not a localStorage key ───────────────────────────────────────────
//
// Every page used to live at `/`, so a link to one could not be shared, a refresh restored whoever
// had used the browser last rather than the page you meant, and back/forward did nothing.
//
// The slug is deliberately NOT the internal tab id: `indhowitworks` is fine in the DOM and awful in
// something you paste to somebody. The map is the only place the two vocabularies meet, and
// `test_routing.py` asserts it stays in step with both the panels in index.html and the server's
// route list — three lists that must agree, checked rather than remembered.
//
// The DASHBOARD keeps `/`. It is the landing page, it is what an unrouted visit already showed, and
// giving it a second address would mean two URLs for one page from day one.
const TAB_SLUGS = {
  dashboard: '/',
  analyze: '/setup-analysis',
  planetary: '/planetary-planning',
  planner: '/planner',
  layout: '/factory-layout',
  planetdb: '/planet-db',
  contribute: '/contribute',
  howitworks: '/how-it-works',
  reactions: '/reactions',
  industry: '/manufacturing',
  indhowitworks: '/industry/how-it-works',
  admin: '/admin',
};
const SLUG_TABS = Object.fromEntries(Object.entries(TAB_SLUGS).map(([t, u]) => [u, t]));

// A second segment, for the two pages that are really several pages behind one nav entry: Admin
// (eleven sections) and PI Planner (two modes). Both were `localStorage` in exactly the way the
// top-level tab was, so "look at the bug list" or "use Refill" could not be sent to anybody.
//
// The records a page can NAME are a third segment — see TAB_RECORDS below, and the privacy answer
// that had to come before it.
//
// `apply` is how the URL reaches the module that owns the state; `slugs` maps the module's internal
// key to what a person should see in the address bar. Same split, and same reason, as TAB_SLUGS.
const TAB_SUBPAGES = {
  admin: {
    apply: k => { if (typeof adminSubPage === 'function') adminSubPage(k); },
    slugs: {
      stats: 'stats', jobs: 'jobs', features: 'features', users: 'users',
      submissions: 'submissions', bugs: 'bugs', baskets: 'baskets', groups: 'groups',
      moongoo: 'moon-goo', wallet: 'corp-wallet', cleanup: 'cleanup', audit: 'audit',
    },
  },
  planner: {
    apply: k => { if (typeof setPiMode === 'function') setPiMode(k); },
    slugs: { build: 'find-buildables', refill: 'refill' },
  },
};

// ── Records: the URL names one ROW, not just a page ───────────────────────────────────────────
//
// `/manufacturing/order/123`. An id in a path is visible to whoever the link is sent to, so this
// needed an answer about disclosure before it needed a mapping (CLAUDE.md rule 8). The answer, per
// record, is that **the endpoint behind it cannot tell a stranger anything the id alone did not**:
//
//   * an order is read through `_order_row`, a single query keyed on (id, context_id) that raises
//     one 404 for "not yours" and "does not exist" alike;
//   * a saved plan is read through `GET /api/plan-snapshots/{id}`, which answers `{payload: null}`
//     for a stranger and for a missing row with the same body.
//
// Neither confirms that the id exists, which is the only thing a recipient could otherwise learn.
// So a link that reaches somebody not entitled to the record lands them on the plain page, in
// silence — the same page a mistyped id gives, because telling the two apart IS the disclosure.
//
// **A colony is deliberately absent, and that is settled** — asked for, then closed as won't-build
// on 2026-08-16 (TODO-archive §19c). Two reasons, and the second is the one that generalises: there
// is no single-colony view to land on, colonies being rows across Setup Analysis and the Characters
// list rather than a page you open; and its natural id is character + planet, which is precisely the
// locatable data rule 8 names — it discloses in the PATH, before any endpoint is asked, so no
// "does the endpoint already refuse them?" answer could have made it safe. **A record whose id is
// itself locatable does not belong in this map at all; it needs an unguessable token, like the plan
// shares.**
//
// `open(id)` resolves falsy (or rejects) when the record could not be opened, and the router then
// drops it from the address bar with a REPLACE, so a bounced link leaves no back-button entry
// pointing at a record you cannot open. `close()` is how Back out of a record puts the page back.
const TAB_RECORDS = {
  industry: {
    order: {
      open: id => (typeof indOpenOrderLink === 'function' ? indOpenOrderLink(id) : false),
      close: () => { if (typeof indCloseRules === 'function') indCloseRules(); },
    },
  },
  planetary: {
    plan: {
      open: id => (typeof openSavedPlanFull === 'function'
        ? openSavedPlanFull(id, { silent: true }) : false),
    },
  },
  planner: {
    plan: {
      open: id => (typeof openSavedPlanRefill === 'function'
        ? openSavedPlanRefill(id, { silent: true }) : false),
    },
  },
};

function _recordFor(tab, kind) {
  return (TAB_RECORDS[tab] || {})[kind] || null;
}

function _subSlug(tab, key) {
  const m = TAB_SUBPAGES[tab];
  return (m && key && m.slugs[key]) || null;
}
function _subKey(tab, slug) {
  const m = TAB_SUBPAGES[tab];
  if (!m) return null;
  return Object.keys(m.slugs).find(k => m.slugs[k] === slug) || null;
}
/** The full path for a page, including its section and the record it is showing, when it has them.
 *
 *  One grammar, read left to right: `<page>[/<section>][/<kind>/<id>]`. The record is last because
 *  it is the most specific thing on screen, and because that keeps a page's URL a prefix of every
 *  URL below it — `/planner`, `/planner/refill`, `/planner/refill/plan/12` are the same page
 *  narrowing, which is what makes a truncated link still land somewhere sensible. */
function _pathFor(tab, sub, rec) {
  const base = TAB_SLUGS[tab];
  if (!base) return null;
  let path = base === '/' ? '' : base;
  const s = _subSlug(tab, sub);
  if (s) path += '/' + s;
  // A slash in an id cannot be expressed here. `%2F` looks right and is not: uvicorn decodes the
  // path before Starlette routes it, so the link 404s forever — worse than no link at all, because
  // it is a link somebody can copy. No id today contains one; this is what keeps that true when one
  // does, by leaving the record off the URL rather than minting a dead address.
  if (rec && rec.kind && rec.id != null && rec.id !== '' && _recordFor(tab, rec.kind)
      && !String(rec.id).includes('/')) {
    path += '/' + rec.kind + '/' + encodeURIComponent(rec.id);
  }
  return path || '/';
}

// **Which page is on screen right now**, as opposed to which page this browser saw last.
//
// The two used to be the same question, answered by reading `localStorage.activeTab` — a dozen
// places did exactly that. They are not the same question, and `localStorage` is the wrong place to
// ask either of them:
//
//   * it is shared across BROWSER TABS. Two tabs open on the site and the guards in one read
//     whichever page the *other* one last opened, so a rescan finishing in a background tab could
//     re-render the foreground one's Dashboard, or skip it;
//   * it survives the page. Before boot has chosen anything it still answers, with last week's page;
//   * and now that a URL names the page, a stored key is simply a second source of truth for
//     something the address bar already states.
//
// So the live answer lives here, seeded from the URL at script-load time (before anything can ask)
// and updated by `switchTab`, which is the only thing that moves a page onto the screen. `null`
// means genuinely nothing has been chosen yet — the callers that care about that distinction want
// it, and it is a distinction `localStorage` could not express.
let _activeTab = tabForPath(location.pathname);
let _activeSub = (routeForPath(location.pathname) || {}).sub || null;
// The record on screen, `{kind, id}` or null. NOT seeded from the URL the way the two above are:
// they are answers the router knows on its own, and this one is only true once the module has
// actually opened the row — which can fail, and must then leave the address bar saying nothing
// rather than naming a record that is not on screen.
let _activeRecord = null;
// Bumped by every navigation and every record that reports itself. Opening a row is the one part of
// a route that takes a network round trip, so two of them can be in flight at once — land on
// `/manufacturing/order/1`, click the gear for order 2 before it answers, and the FIRST answer used
// to arrive last and win: the address bar reverted to order 1 and the dialog repainted itself as
// order 1, which is the order a Save would then have written to. Tab identity cannot see that; both
// answers are about the same tab. A generation can.
let _recordSeq = 0;
// Set while switchTab is running its open hooks. Those hooks restore a section, which would
// otherwise write the URL a second time — and worse, write it while the address bar still holds the
// PREVIOUS page, replacing that entry instead of pushing a new one. So they only record where they
// are, and switchTab performs exactly one history operation at the end.
let _switching = 0;

/** The page currently on screen, or null before one has been chosen. Ask this, never storage. */
function currentTab() { return _activeTab; }
/** The section within it, for the two pages that have sections. */
function currentSubPage() { return _activeSub; }
/** The record on screen within it, `{kind, id}` or null. */
function currentRecord() { return _activeRecord; }

/** The tab a path names, or null when the path is not one of ours (a share link, an asset). */
function tabForPath(path) {
  return (routeForPath(path) || {}).tab || null;
}

/** The {tab, sub, kind, id} a path names, or null when the path is not one of ours.
 *
 *  The LONGEST prefix that is a page wins, which is what keeps `/industry/how-it-works` a
 *  two-segment TAB slug rather than the `how-it-works` section of an `industry` page. Everything
 *  after that prefix has to parse completely — a leftover segment means this is not our path at
 *  all, and answering `null` is what sends `/nope/app.js` to the static mount instead of the SPA. */
function routeForPath(path) {
  const clean = (path || '/').replace(/\/+$/, '') || '/';
  const parts = clean.split('/').filter(Boolean);
  for (let n = parts.length; n >= 0; n--) {
    const base = n === 0 ? '/' : '/' + parts.slice(0, n).join('/');
    const tab = SLUG_TABS[base];
    if (!tab) continue;
    const route = _routeRest(tab, parts.slice(n));
    if (route) return route;
  }
  return null;
}

/** What is left of a path once its page has been taken off the front: nothing, a section, a record,
 *  or a section and a record. Anything else is not a route. */
function _routeRest(tab, rest) {
  let sub = null;
  if (rest.length && TAB_SUBPAGES[tab]) {
    const key = _subKey(tab, rest[0]);
    if (key) { sub = key; rest = rest.slice(1); }
  }
  if (!rest.length) return { tab, sub, kind: null, id: null };
  // A record is exactly two segments, `<kind>/<id>`, and the kind must be one this page declares —
  // an unknown kind is not a route we can serve, and guessing at it is how a mistyped path comes
  // back as a page instead of a 404.
  if (rest.length !== 2 || !rest[1] || !_recordFor(tab, rest[0])) return null;
  let id;
  try { id = decodeURIComponent(rest[1]); } catch (e) { return null; }
  return { tab, sub, kind: rest[0], id };
}

/** A section module reporting where it has just moved to.
 *
 *  Always a REPLACE. A deliberate click does not arrive here first — it goes through `switchTab`
 *  with the section as `opts.sub`, which pushes exactly once (see `adminNavTo`). What reaches this
 *  function is a module correcting its own state: a restore on open, or refill.js flipping to
 *  Refill once a plan exists. Neither is a navigation the user should be able to go Back from, so
 *  neither earns a history entry — it only earns an address bar that stops lying. */
function noteSubPage(tab, key) {
  if (_activeTab !== tab) return;          // it is restoring state for a page not on screen
  _activeSub = key;
  if (_switching) return;                  // switchTab will write the URL once, in a moment
  _syncUrl(tab, true);
}

/** A module reporting which RECORD it has just put on screen, or `null` for none.
 *
 *  Always a REPLACE, for the same reason `noteSubPage` is and one more. The same reason: what
 *  reaches here is a module stating where it already is, not a navigation. The extra one: a record
 *  here is a modal or a restored view, and a PUSH would make Back close it — which sounds like a
 *  feature until you notice that Back also re-enters `switchTab` and re-runs the page's open hooks,
 *  so closing a dialog would reload the page underneath it. The address bar stops lying; the back
 *  button keeps meaning "the page before this one".
 *
 *  Ignored when the page it belongs to is not the one on screen — a loader finishing late for a tab
 *  the user has already left must not write that tab's record into another page's URL. */
function noteRecord(tab, kind, id) {
  if (_activeTab !== tab) return;
  _recordSeq++;                            // this is now the current answer; older ones are stale
  _activeRecord = (kind && id != null && id !== '' && _recordFor(tab, kind)) ? { kind, id: String(id) } : null;
  if (_switching) return;                  // switchTab will write the URL once, in a moment
  _syncUrl(tab, true);
}

/** Put `name` in the address bar without navigating. `replace` for corrections, so a bounced
 *  deep link does not leave a back-button entry pointing at a page you may not open.
 *
 *  `rec` overrides the record to write. Passed only by the one caller that knows something this
 *  function cannot: `switchTab` arriving on a record link, where the row is about to be opened but
 *  has not been yet, so `_activeRecord` is legitimately still null and composing from it would
 *  write the record straight back out of the URL. */
function _syncUrl(name, replace, rec) {
  const own = name === _activeTab;
  const url = _pathFor(name, own ? _activeSub : null,
                       rec !== undefined ? rec : (own ? _activeRecord : null));
  if (!url) return;                       // a tab with no slug keeps whatever the URL already is
  // A share link is consumed by planetary.js, which clears it itself — do not fight it here.
  if (/^\/s\//.test(location.pathname) && name === 'planetary') return;
  if (location.pathname === url) return;  // nothing to say
  try { history[replace ? 'replaceState' : 'pushState']({ tab: name }, '', url); } catch (e) {}
}

/** Open the record a URL named, and put it in the address bar only if it really opened.
 *
 *  A link that reaches somebody not entitled to the row lands them on the plain page in silence —
 *  the same thing a mistyped id gives them, because telling those two apart is the disclosure the
 *  whole design avoids (CLAUDE.md rule 8). No toast, no message, no console noise: the page they
 *  asked for is simply the page they get. */
function _openRecord(tab, rec) {
  const spec = _recordFor(tab, rec.kind);
  if (!spec || typeof spec.open !== 'function') return;
  // Captured BEFORE the call: anything that navigates or opens another record from here on makes
  // this answer stale, and a stale answer must not touch the URL or claim the screen — see
  // `_recordSeq`. Tab identity is not enough; the competing answer is usually the same tab.
  const seq = ++_recordSeq;
  const stale = () => _activeTab !== tab || _recordSeq !== seq;
  let result;
  try { result = spec.open(rec.id); } catch (e) { result = false; }
  Promise.resolve(result).then(ok => {
    if (stale()) return;
    if (ok === false || ok === null || ok === undefined) {
      _activeRecord = null;
      _syncUrl(tab, true);                 // REPLACE: no back-button entry for a record you can't open
    } else {
      noteRecord(tab, rec.kind, rec.id);
    }
  }, () => {
    if (stale()) return;
    _activeRecord = null;
    _syncUrl(tab, true);
  });
}

function switchTab(name, opts) {
  // Defense-in-depth: nav buttons for a restricted page are hidden (_applyPageRestriction), but a
  // pasted deep link or a direct call could still reach here — bounce to the first page the
  // caller's group actually allows instead of rendering a blocked page.
  if (typeof _isPageRestricted === 'function' && _isPageRestricted(name)) {
    name = _firstAllowedPage();
    // **The record does NOT come along.** Two pages can declare the same record kind (`planner` and
    // `planetary` both have `plan`), so carrying it would open the row on a page the user never
    // asked for, reached by being refused the one they did — and write its address as if they had.
    // A refused page refuses everything under it.
    opts = { ...(opts || {}), corrected: true, record: null, sub: null };
  }
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
  // Recorded BEFORE the open hooks run: a hook restoring a section calls back in through
  // noteSubPage, which has to be able to tell "the page I belong to is on screen" from "something
  // else is". Nothing between here and there reads the old value.
  // Leaving a page closes whatever record it was showing. Without this, navigating away from an
  // open order and back again would find `_activeRecord` still set and write the old record into
  // the new page's URL — and the dialog itself would still be on screen over a page that no longer
  // owns it.
  const leaving = _activeRecord;
  if (leaving && (name !== _activeTab || !(opts && opts.record && opts.record.kind === leaving.kind
                                           && opts.record.id === String(leaving.id)))) {
    const spec = _recordFor(_activeTab, leaving.kind);
    // Inside the guard: a close handler reports itself through `noteRecord`, which would otherwise
    // write the OLD page's address in the middle of navigating away from it. One history operation
    // per switch, at the end, is the rule the whole of this function is built on.
    _switching++;
    try { if (spec && typeof spec.close === 'function') spec.close(); } catch (e) {} finally { _switching--; }
  }
  _activeTab = name;
  _activeSub = (opts && opts.sub) || null;
  _activeRecord = null;                    // set only once the module confirms it opened the row
  // A COUNTER, not a flag: an open hook may call switchTab again (see the bounce below), and a
  // boolean would let the inner call's `finally` clear the guard while the outer call is still
  // running its hooks — turning it off for exactly the stretch it exists to cover. Defence in
  // depth rather than a fix for something observable: with today's single re-entrant call site the
  // outer call bails out anyway, so no test covers the difference. It is here so the next hook
  // that redirects does not have to reason about it.
  _switching++;
  try {
  // **A page's own loader must never be able to break NAVIGATION.** These hooks fetch and render;
  // one of them throwing used to abort the whole switch, so the section was never applied and the
  // URL was never written — the symptom being every Admin sub-nav click landing on `/admin` showing
  // whichever section was last displayed, because `onAdminTabOpen` fires five loaders before it
  // reaches `adminSubPage`. Reported live 2026-08-15.
  //
  // Isolated rather than fixed one loader at a time: which of them can throw is a moving target,
  // and there is no version of "the page failed to load its data" that should also mean "the app
  // will not let you leave this page".
  const _hook = fn => { try { if (typeof fn === 'function') fn(); } catch (e) { console.error('tab open hook failed:', name, e); } };
  if (name === 'dashboard') _hook(typeof onDashboardTabOpen === 'function' ? onDashboardTabOpen : null);
  if (name === 'planner') _hook(typeof onPlannerTabOpen === 'function' ? onPlannerTabOpen : null);
  if (name === 'planetary') _hook(typeof onPlanetaryTabOpen === 'function' ? onPlanetaryTabOpen : null);
  if (name === 'planetdb') _hook(typeof onPlanetDbTabOpen === 'function' ? onPlanetDbTabOpen : null);
  if (name === 'reactions') _hook(typeof onReactionsTabOpen === 'function' ? onReactionsTabOpen : null);
  if (name === 'industry') _hook(typeof onIndustryTabOpen === 'function' ? onIndustryTabOpen : null);
  if (name === 'layout') _hook(typeof onLayoutTabOpen === 'function' ? onLayoutTabOpen : null);
  if (name === 'characters') _hook(typeof loadCharacters === 'function' ? loadCharacters : null);
  if (name === 'analyze') _hook(typeof onAnalyzeTabOpen === 'function' ? onAnalyzeTabOpen : null);
  if (name === 'admin') _hook(typeof onAdminTabOpen === 'function' ? onAdminTabOpen : null);
  if (name === 'indhowitworks') _hook(() => loadHelpPanel(name));
  // A section named by the URL beats whatever the open hook just restored from storage — same rule
  // as the page itself, for the same reason: a link asked for THIS section. Outside the isolation
  // above on purpose: this one IS the navigation.
  if (opts && opts.sub && TAB_SUBPAGES[name]) TAB_SUBPAGES[name].apply(opts.sub);
  } finally { _switching--; }
  // **An open hook is allowed to send us somewhere else.** onAdminTabOpen bounces a confirmed
  // non-admin by calling switchTab again, from inside this one. When that happens the inner call
  // has already recorded, stored and addressed the page it chose, and everything below would
  // overwrite all three with the page we were merely *asked* for — screen showing the Dashboard,
  // address bar reading /admin, and two history entries for one click. So if the hooks moved us,
  // the inner call is authoritative and this one is done.
  if (_activeTab !== name) return;
  // Still stored, but for ONE purpose only: what a bare visit to `/` should restore. Nothing asks
  // it "where am I" any more — that is `currentTab()`, which cannot be stale or belong to another
  // browser tab.
  localStorage.setItem('activeTab', name);
  // `fromHistory` = the browser moved us, so the URL is already right and pushing would add a
  // duplicate entry. `corrected` = gating sent us somewhere else than asked, so REPLACE rather than
  // push: the address bar must not keep claiming a page the user cannot open.
  //
  // The two can arrive TOGETHER, and `fromHistory` must not win then. A restricted deep link is
  // exactly that case — arrived at by URL (so `fromHistory`) and then bounced (so `corrected`) —
  // and suppressing the write left the bar naming the blocked page while another page was on
  // screen, which is the one thing this whole mechanism exists to prevent.
  //
  // The remaining `fromHistory` write is not a correction but an omission: arriving at a bare
  // `/admin`, the open hook restores a section, so the address is now less specific than the page
  // it names. Replace it with the full path. Never a push — the browser's own entry stands.
  // The record this URL asked for has not been opened yet — that happens below, because it is the
  // one part of a route that can be refused. So the "is the address already right?" question has to
  // be asked about the address we are ON THE WAY to, not the one that is true this instant.
  // Otherwise arriving on `/manufacturing/order/123` rewrites the bar to `/manufacturing` and then
  // back again, which is two history operations for one link, a visible flicker, and — the part
  // that matters — a URL that has already forgotten the record if the page is refreshed mid-load.
  const pending = (opts && opts.record && opts.record.kind && _recordFor(name, opts.record.kind))
    ? { kind: opts.record.kind, id: opts.record.id } : null;
  if (!(opts && opts.fromHistory)) _syncUrl(name, !!(opts && opts.corrected));
  else if ((opts && opts.corrected) || _pathFor(name, _activeSub, pending) !== location.pathname) {
    // `pending` again, and this time as the value WRITTEN: composing from `_activeRecord` here
    // would drop the record the URL is carrying, then `_openRecord` would put it back a moment
    // later — two history operations for one link, and a refresh in between losing the record.
    _syncUrl(name, true, pending);
  }
  // The record the URL asked for, opened LAST — after the page is on screen, its hooks have run and
  // its address is written. It is the only part of a route that can be refused (the row may be gone,
  // or never have been this account's), so it is also the only part that has to be able to take
  // itself back out of the address bar, which it can only do once there is a correct address to
  // fall back to. Asynchronous and deliberately not awaited: navigation is finished either way.
  if (opts && opts.record && opts.record.kind) _openRecord(name, opts.record);
}


function toggleSidebar() {
  const collapsed = document.body.classList.toggle('nav-collapsed');
  localStorage.setItem('navCollapsed', collapsed ? '1' : '0');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
    // PI Planner sub-items (Find Buildables / Refill a plan) carry a data-pimode. Handed to
    // switchTab as the section rather than applied afterwards, so one click leaves ONE history
    // entry — setting it after the switch pushed `/planner` and then `/planner/refill`, and Back
    // would have gone to a mode the user never chose.
    switchTab(t.dataset.tab, t.dataset.pimode ? { sub: t.dataset.pimode } : undefined);
  }));
  // Warm the Reactions dashboard's server-side cache (_DASHBOARD_CACHE_TTL, app/reactions/jobs.py)
  // the moment the cursor lands on its nav button — that endpoint repairs and re-prices the whole
  // plan, the single slowest fetch onReactionsTabOpen makes, so most of a mouse-driven click's
  // hover-to-click gap is spent warming the cache instead of on the click itself. Fire-and-forget,
  // once per page load: a miss just means the click pays the normal (now-cached-for-everyone-else)
  // cost, never a regression.
  let _rxPrefetched = false;
  const rxNavBtn = document.querySelector('.tab[data-tab="reactions"]');
  if (rxNavBtn) rxNavBtn.addEventListener('mouseenter', () => {
    if (_rxPrefetched) return;
    _rxPrefetched = true;
    api('/api/reactions/jobs').catch(() => {});
  });
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
  // **The URL wins.** Someone opening a link asked for THAT page, and the remembered tab is only a
  // convenience for a bare visit to `/` — honouring the remembered one over an explicit link is the
  // whole reason a shared link was useless before. A routed deep link also beats the mobile
  // fallback: it is an explicit request, exactly as a share link is.
  const routed = routeForPath(location.pathname);
  if (routed) switchTab(routed.tab, { fromHistory: true, sub: routed.sub, record: routed });
  else if (hasPlanetaryShare) switchTab('planetary');
  else if (isMobile) switchTab(saved && MOBILE_TABS.includes(saved) ? saved : 'dashboard');
  else if (saved) switchTab(saved);

  // Back/forward. Every on*TabOpen hook already re-runs on an ordinary click, so replaying one here
  // is the same code path the user exercises constantly. `fromHistory` stops it pushing the entry
  // the browser just moved us to back onto the stack.
  window.addEventListener('popstate', () => {
    const r = routeForPath(location.pathname);
    // `record` rides along both ways: Back INTO a record re-opens it, and Back OUT of one
    // arrives with no record, which is what closes the one still on screen (see switchTab).
    if (r) switchTab(r.tab, { fromHistory: true, sub: r.sub, record: r });
  });
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
  const tab = currentTab();
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
