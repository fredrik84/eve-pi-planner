// ── Industry — Build rules: one renderer, two modes. ────────────────────────────────────────
// The single surface over the account's build configuration (Settings) and one order's
// overrides (the dialog), plus the gate that decides whether it is offered at all.

// ── Build rules: one renderer, two modes ──────────────────────────────────────────────────────
// Configuration used to live in eight strips down the plan card plus six write paths, and none of
// the strips read as a setting — the app's own author could not find the reaction control. This is
// the single surface over them (GET/POST /api/industry/build-setup), rendered in two modes:
//
//   account — Settings → Build rules. The standing rules.
//   order   — the per-order dialog. The SAME sections, each stating what it inherits, storing only
//             what this build actually changed.
//
// Two modes rather than two components on purpose: a global and its override that are rendered by
// different code are a pair that will drift, and the drift shows up as a plan that disagrees with
// the screen that configured it.
let _indRules = null;          // {account, available, order}
let _indRulesMode = 'account';
let _indRulesOrderId = null;

function _indRulesActive() { return _featureActive('industry_build_setup'); }

async function _indLoadRules(orderId) {
  const q = orderId != null ? `?order_id=${encodeURIComponent(orderId)}` : '';
  _indRules = await api('/api/industry/build-setup' + q);
  return _indRules;
}

// ── Section renderers. Each returns '' when its feature is off, so a closed section is absent
// rather than shown and refused by the POST.
function _indRuleRow(label, body, note) {
  return `<div class="ind-rule-row"><div class="ind-rule-lbl">${_esc(label)}</div>`
    + `<div class="ind-rule-ctl">${body}`
    + (note ? `<div class="ind-src-meta">${note}</div>` : '') + `</div></div>`;
}

function _indRuleGroup(kicker, title, hint, rows) {
  const content = rows.filter(Boolean).join('');
  if (!content) return '';
  return `<section class="ind-rule-group"><div class="settings-panel-kicker">${_esc(kicker)}</div>`
    + `<h4>${_esc(title)}</h4>`
    + (hint ? `<div class="settings-source-copy">${hint}</div>` : '')
    + content + `</section>`;
}

// In order mode a field either INHERITS or is CHANGED, and it has to say which — an inherited value
// shown as a plain choice is how an override gets made by accident.
function _indInheritTag(field) {
  if (_indRulesMode !== 'order') return '';
  const on = ((_indRules.order || {}).overridden || []).includes(field);
  return on ? `<span class="ind-rule-tag ind-rule-changed">changed for this build</span>`
            : `<span class="ind-rule-tag">inherited</span>`;
}

function _indRulesFacility(a) {
  const f = a.facility || {};
  return _indRuleRow('Where you build',
    `<div class="ind-src-meta">Facilities and rigs are configured separately.</div>`
    + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indCloseRules();openSettingsModal('markets')">Open Facilities &amp; pricing</button></div>`,
    f.facility_id ? `Currently: <b>${_esc(f.facility_id)}</b>`
      + (f.struct_material_pct != null ? ` · ME ${f.struct_material_pct}%` : '')
      + (f.struct_time_pct != null ? ` · TE ${f.struct_time_pct}%` : '')
      : 'No structure chosen — jobs are costed without a rig bonus.');
}

function _indRulesThreshold(a) {
  const t = a.threshold || {};
  const pct = t.marginal_pct == null ? 3 : t.marginal_pct;
  return _indRuleRow('Worth building?',
    `<label class="ind-opt-check"><input type="checkbox" id="ir-force" ${t.force_build ? 'checked' : ''}`
    + ` onchange="indRulesForceToggle(this)"> Build everything — ignore small savings and slow batches, and react your own materials</label>`
    + `<div class="ind-field ind-field-marg" id="ir-marg-wrap"${t.force_build ? ' style="opacity:.45"' : ''}>`
    + `<label for="ir-marg">Build only if it saves <b id="ir-marg-pct">${pct}%</b> of the build</label>`
    + `<input type="range" id="ir-marg" min="0" max="10" step="0.5" value="${pct}"`
    + ` ${t.force_build ? 'disabled' : ''} oninput="document.getElementById('ir-marg-pct').textContent=this.value+'%'"></div>`
    + `<label class="ind-opt-check"><input type="checkbox" id="ir-speed" ${t.prioritize_speed ? 'checked' : ''}>`
    + ` Prioritize speed — buy slow bulk materials to finish sooner</label>`,
    'At 0% nothing is bought for being a small saving — there is no hidden floor under it.');
}

function _indRulesReactions(a) {
  if (!_indRules.available.reactions) return '';
  const p = (a.reactions || {}).policy || {};
  const cats = (a.reactions || {}).categories || [];
  const bought = new Set(p.buy_categories || []);
  if (_indRulesMode === 'order') {
    const on = ((_indRules.order || {}).values || {}).build_reactions;
    return _indRuleRow('Reactions',
      `<label class="ind-opt-check"><input type="checkbox" id="ir-rx-own" ${on ? 'checked' : ''}>`
      + ` React this build's own materials, whatever the account rule says</label> ${_indInheritTag('build_reactions')}`,
      `Account rule: ${bought.size ? _esc([...bought].join(', ')) + ' bought in' : 'all reactions made here'}.`);
  }
  return _indRuleRow('Reactions',
    `<label class="ind-opt-check"><input type="checkbox" id="ir-rx-run" ${p.build_reactions !== false ? 'checked' : ''}`
    + ` onchange="indRulesRxToggle(this)"> This account runs its own reactions</label>`
    + `<div id="ir-rx-cats" class="ind-rule-checks"${p.build_reactions === false ? ' style="display:none"' : ''}>`
    + cats.map(c => `<label class="ind-opt-check" title="${_esc(c.description || '')}">`
        + `<input type="checkbox" class="ir-rx-cat" data-key="${_esc(c.key)}" ${bought.has(c.key) ? 'checked' : ''}>`
        + ` buy ${_esc(c.label)} instead</label>`).join('') + `</div>`,
    (a.reactions || {}).defaulted
      ? '<b>Still on the default</b> — composites &amp; intermediates are bought in. That is a default, not a choice you made.'
      : '');
}

// One list, one rule per component. Two lists ("never build" / "build anyway") were one list
// wearing two hats: a component is always-buy, always-build, or left to the cost engine, and it
// cannot be two of those. A row per type with a two-way toggle makes the exclusion visible and
// makes changing your mind one click instead of a delete here and an add there.
function _indRulesComponents(a) {
  if (!_indRules.available.components || _indRulesMode === 'order') return '';
  const items = (a.components || {}).items || [];
  const rows = items.length
    ? `<div class="ind-rule-list">` + items.map(it =>
        `<div class="ind-rule-item"><span class="ind-rule-item-name">${_esc(it.name)}</span>`
        + `<span class="ind-rule-seg">`
        + `<button class="ind-rule-segbtn${it.rule === 'build' ? ' on' : ''}"`
        + ` onclick="indRulesSetComponent(${it.type_id}, 'build')"`
        + ` title="Build this whatever the shortcuts say">always build</button>`
        + `<button class="ind-rule-segbtn${it.rule === 'buy' ? ' on' : ''}"`
        + ` onclick="indRulesSetComponent(${it.type_id}, 'buy')"`
        + ` title="Always buy this, whatever the cost engine works out">always buy</button>`
        + `</span>`
        + `<button class="ind-link-btn" onclick="indRulesSetComponent(${it.type_id}, null)"`
        + ` title="Remove the rule — let the cost engine decide again">×</button></div>`).join('')
      + `</div>`
    : `<div class="ind-src-meta">No overrules — the cost engine decides every component.</div>`;
  return _indRuleRow('Components',
    rows
    + `<div class="ind-rule-add">`
    + `<input type="text" id="ir-comp-q" placeholder="Add a component…" autocomplete="off"`
    + ` oninput="indRulesCompSearch()" onkeydown="if(event.key==='Escape')indRulesCompClear()">`
    + `<div class="ind-rule-sugg" id="ir-comp-sugg"></div></div>`,
    'A rule here applies to every build. A single order can still overrule it for itself.');
}

let _indCompSearchT = null;
function indRulesCompClear() {
  const el = document.getElementById('ir-comp-sugg');
  if (el) el.innerHTML = '';
}

function indRulesCompSearch() {
  clearTimeout(_indCompSearchT);
  _indCompSearchT = setTimeout(async () => {
    const q = (document.getElementById('ir-comp-q') || {}).value || '';
    const box = document.getElementById('ir-comp-sugg');
    if (!box) return;
    if (q.trim().length < 2) { box.innerHTML = ''; return; }
    let hits = [];
    try { hits = (await api('/api/industry/search?q=' + encodeURIComponent(q.trim()))) || []; }
    catch (e) { box.innerHTML = ''; return; }
    const have = new Set(((_indRules.account.components || {}).items || []).map(i => i.type_id));
    box.innerHTML = (hits.items || hits).slice(0, 8).filter(h => !have.has(h.type_id)).map(h =>
      `<button class="ind-rule-sugg-hit" onclick="indRulesAddComponent(${h.type_id})">`
      + `${_esc(h.name)}</button>`).join('') || `<div class="ind-src-meta">No match.</div>`;
  }, 220);
}

function indRulesAddComponent(typeId) {
  const q = document.getElementById('ir-comp-q');
  if (q) q.value = '';
  indRulesCompClear();
  // New rules default to "always buy": that is what the old blacklist meant, so an existing habit
  // keeps working, and it is the safer of the two — buying something you could have built costs
  // ISK, while building something you cannot costs a plan that will not run.
  return indRulesSetComponent(typeId, 'buy');
}

// `rule` null removes the overrule entirely. Written straight through rather than collected on
// Save: this list is edited by clicking, and a click that looks like it took effect has to have.
async function indRulesSetComponent(typeId, rule) {
  const items = ((_indRules.account.components || {}).items || [])
    .filter(i => i.type_id !== typeId);
  if (rule) items.push({ type_id: typeId, rule });
  try {
    const res = await apiSend('POST', '/api/industry/build-setup', { components: { items } });
    _indRules.account = res.account;
    _indRulesRepaintOpen();
  } catch (e) { toastError(e, 'Could not save'); }
}

// Repaint whichever surface is showing — the Settings pane or the order dialog.
function _indRulesRepaintOpen() {
  const pane = document.getElementById('settingsSecBuildrulesBody');
  const modal = document.getElementById('indRulesModal');
  if (modal && modal.style.display !== 'none') { _indRulesPaint(); return; }
  if (pane) {
    pane.innerHTML = _indRulesSectionsHtml()
      + `<div class="ind-src-actions"><button class="ind-bp-btn ind-bp-btn-primary" onclick="indSaveRules()">Save build rules</button>`
      + `<span class="ind-src-meta" id="ir-saved"></span></div>`;
  }
}

function _indRulesJobLength(a) {
  if (!_indRules.available.job_length || _indRulesMode === 'order') return '';
  const d = (a.job_length || {}).max_reaction_job_days;
  return _indRuleRow('Longest reaction job',
    `<input type="number" id="ir-joblen" min="0" step="0.5" style="width:80px" value="${d == null ? '' : d}"> days`,
    'Blank or 0 = no ceiling. A reaction has no per-job run cap, so a big batch will otherwise sit in one slot for weeks.');
}

function _indRulesSources(a) {
  if (!_indRules.available.sources) return '';
  const srcs = (a.sources || {}).sources || [];
  const chosen = _indRulesMode === 'order'
    ? new Set(((_indRules.order || {}).values || {}).source_keys || [])
    : new Set(srcs.filter(s => s.enabled).map(s => s.key));
  if (!srcs.length) {
    return _indRuleRow('Materials from',
      `<div class="ind-src-meta">No stock sources are configured.</div>`
      + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indCloseRules();openSettingsModal('blueprints')">Set up inventory</button></div>`);
  }
  // Account defaults are the enabled inventory sources themselves. Editing those keys here was a
  // second editor for Settings → Blueprints & formulas and made it look as if containers had to be
  // configured twice. Orders still need the picker below because choosing a subset for one build
  // is a genuine override, not inventory definition.
  if (_indRulesMode === 'account') {
    const enabled = srcs.filter(s => s.enabled);
    const names = enabled.slice(0, 3).map(s => _esc(s.name)).join(', ');
    const more = enabled.length > 3 ? ` +${enabled.length - 3} more` : '';
    return _indRuleRow('Available stock',
      `<div class="ind-rule-source-summary"><b>${enabled.length}</b> of ${srcs.length} source${srcs.length === 1 ? '' : 's'} enabled`
      + (names ? `<span>${names}${more}</span>` : '') + `</div>`
      + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indCloseRules();openSettingsModal('blueprints')">Manage inventory</button></div>`,
      'Inventory is defined once under Blueprints & formulas. Builds spend enabled stock before buying.');
  }
  return _indRuleRow('Materials from',
    `<div class="ind-rule-checks">` + srcs.map(s => `<label class="ind-opt-check"><input type="checkbox" class="ir-src" data-key="${_esc(s.key)}"`
      + ` ${chosen.has(s.key) ? 'checked' : ''}> ${_esc(s.name)}`
      + (s.place ? ` <span class="ind-src-meta">${_esc(s.place)}</span>` : '')
      + (s.corp ? ` <span class="ind-src-meta">corp</span>` : '') + `</label>`).join('')
      + `</div>`
      + (_indRulesMode === 'order' ? ' ' + _indInheritTag('source_keys') : ''),
    'Stock in a ticked box is spent before anything is bought.');
}

function _indRulesMargin(a) {
  const m = _indRulesMode === 'order'
    ? ((_indRules.order || {}).values || {}).margin_pct
    : (a.margin || {}).margin_pct;
  return _indRuleRow('Customer margin',
    `<input type="number" id="ir-margin" min="0" max="100" step="0.5" style="width:80px"`
    + ` value="${m == null ? '' : m}"> % over net cost `
    + (_indRulesMode === 'order' ? _indInheritTag('margin_pct') : ''),
    'Blank = the account default. Priced off NET cost, so over-production you keep is not billed twice.');
}

// The per-order-plans rung, which had NO UI at all until 2026-08-14 — the account setting and
// /api/industry/queue-plan/compare were endpoints only, so what shipped to testers was the half
// that COSTS money (shared components built once per order, copies bought per order) without the
// capability that justifies it. The measured cost is stated on the control rather than discovered
// after the fact: +2.45% net on a 2x Archon, +0.96% on a Phoenix queue. Account-level only — it is
// a statement about how the whole queue is planned, not about one build.
function _indRulesPerOrder(a) {
  if (_indRulesMode === 'order') return '';
  if (!_indRules.available.per_order_plans) return '';
  const po = a.per_order_plans || {};
  return _indRuleRow('Plan each build apart',
    `<label class="ind-opt-check"><input type="checkbox" id="ir-perorder" ${po.enabled ? 'checked' : ''}`
    + ` onchange="indRulesPerOrderToggle(this)"> Keep every build's materials and jobs separate</label>`
    + `<div class="ind-src-meta" id="ir-perorder-note"></div>`,
    'On, a component shared by two builds is built <b>twice</b>, once for each — which is what lets '
    + 'each build deliver into its own container. Off, the queue batches it once and is cheaper. '
    + 'Measured: <b>+2.45%</b> net on a 2× Archon, <b>+0.96%</b> on a Phoenix queue.');
}

function indRulesPerOrderToggle(cb) {
  const note = document.getElementById('ir-perorder-note');
  if (note) {
    note.innerHTML = cb.checked
      ? 'Each build is planned on its own — more ISK, and every build has a box of its own to deliver to.'
      : 'The queue is planned as one shared batch — cheaper, and a shared component belongs to no single build.';
  }
}

function _indRulesSectionsHtml() {
  const a = _indRules.account;
  return `<div class="ind-rule-grid ind-rule-grid-${_indRulesMode}">`
    + _indRuleGroup('Production', 'Build or buy', 'Where work runs and when the planner should manufacture instead of purchase.',
        [_indRulesFacility(a), _indRulesThreshold(a), _indRulesReactions(a), _indRulesJobLength(a)])
    + _indRuleGroup('Routing', 'Components and stock', 'Exceptions to automatic component sourcing.',
        [_indRulesComponents(a), _indRulesSources(a), _indRulesPerOrder(a)])
    + _indRuleGroup('Commercial', 'Customer pricing', 'How the planner calculates the suggested selling price.',
        [_indRulesMargin(a)])
    + `</div>`;
}

function indRulesForceToggle(cb) {
  const w = document.getElementById('ir-marg-wrap');
  const r = document.getElementById('ir-marg');
  if (r) r.disabled = cb.checked;
  if (w) w.style.opacity = cb.checked ? '.45' : '';
}

function indRulesRxToggle(cb) {
  const c = document.getElementById('ir-rx-cats');
  if (c) c.style.display = cb.checked ? '' : 'none';
}

// ── Account mode: the Settings section ────────────────────────────────────────────────────────
async function _loadBuildRulesSettings() {
  const body = document.getElementById('settingsSecBuildrulesBody');
  if (!body) return;
  _indRulesMode = 'account';
  _indRulesOrderId = null;
  try { await _indLoadRules(null); }
  catch (e) { body.innerHTML = `<div class="ind-src-meta">Could not load build rules.</div>`; return; }
  body.innerHTML = _indRulesSectionsHtml()
    + `<div class="ind-src-actions"><button class="ind-bp-btn ind-bp-btn-primary" onclick="indSaveRules()">Save build rules</button>`
    + `<span class="ind-src-meta" id="ir-saved"></span></div>`;
}

// ── Order mode: the dialog ────────────────────────────────────────────────────────────────────

// Which open is the current one. A link's load and a click can be in flight together, and the
// link's answer arriving second used to repaint the dialog as the OTHER order and leave
// `_indRulesOrderId` pointing at it — so the next Save wrote to an order nobody was looking at.
let _indRulesSeq = 0;

/** Put the dialog on screen for `orderId`, titled and empty. Shared by the click path and the link
 *  path, which differ only in WHEN this runs relative to the load — see indOpenOrderLink. */
function _indRulesShow(orderId, label) {
  const m = document.getElementById('indRulesModal');
  if (!m) return false;
  _indRulesSeq++;
  _indRulesMode = orderId == null ? 'account' : 'order';
  _indRulesOrderId = orderId;
  m.style.display = 'flex';
  document.getElementById('indRulesTitle').firstChild.textContent =
    orderId == null ? 'Build setup ' : `Build setup — ${label || ('order #' + orderId)} `;
  document.getElementById('indRulesBody').innerHTML = `<div class="ind-loading">Loading…</div>`;
  return true;
}

async function indOpenRules(orderId, label) {
  if (!_indRulesShow(orderId, label)) return;
  try { await _indLoadRules(orderId); }
  catch (e) { document.getElementById('indRulesBody').innerHTML =
    `<div class="ind-src-meta">Could not load build rules.</div>`; return; }
  _indRulesPaint();
  // The order is now what this page is showing, so the address bar should say so — one click, one
  // copyable link. Replace, never push: see noteRecord in app.js.
  if (orderId != null && typeof noteRecord === 'function') noteRecord('industry', 'order', orderId);
}

/** The URL asked for this order. **Loads BEFORE it shows anything**, which is the whole difference
 *  from the click path: a click has already told the user which order they meant, so a spinner that
 *  turns into an error is fine, while a link may name an order that is gone or was never this
 *  account's — and flashing an empty dialog at somebody before bouncing them tells them the id
 *  exists. Returns false so the router silently drops it from the address bar (CLAUDE.md rule 8). */
async function indOpenOrderLink(orderId) {
  const id = Number(orderId);
  if (!Number.isFinite(id)) return false;
  // Gated exactly as the ⚙ button is. A flag decides whether a feature exists for an account, and a
  // URL must not be the way round it — the button being hidden is not the gate (CLAUDE.md rule 2).
  if (!_indRulesActive()) return false;
  const seq = _indRulesSeq;
  try { await _indLoadRules(id); } catch (e) { return false; }
  // Somebody opened another order while this one was loading — theirs is the one on screen, and
  // painting over it with this would leave the dialog showing an order the URL no longer names.
  if (seq !== _indRulesSeq) return false;
  const label = ((_indRules || {}).order || {}).label || '';
  if (!_indRulesShow(id, label)) return false;
  _indRulesPaint();
  return true;
}

function _indRulesPaint() {
  const hint = document.getElementById('indRulesHint');
  if (hint) {
    hint.innerHTML = _indRulesMode === 'order'
      ? `This build inherits your account rules. Only what you change here is stored on the order — `
        + `everything else follows <b>Settings → Build rules</b> and keeps following it when you change it there.`
      : `Your standing rules. Every build follows these unless it overrides one.`;
  }
  const body = document.getElementById('indRulesBody');
  if (body) body.innerHTML = _indRulesSectionsHtml();
}

function indCloseRules() {
  const m = document.getElementById('indRulesModal');
  if (m) m.style.display = 'none';
  // The page is no longer showing an order, so the URL must stop naming one — otherwise a copied
  // link reopens a dialog the sender had already closed.
  if (_indRulesOrderId != null && typeof noteRecord === 'function') noteRecord('industry', null, null);
  _indRulesOrderId = null;
}

// ── Save. Account mode writes the consolidated patch; order mode PATCHes the order, because an
// override belongs to the order row and must not touch the account's standing rules.
function _indRulesReadForm() {
  const val = id => { const e = document.getElementById(id); return e ? e.value : null; };
  const on = id => { const e = document.getElementById(id); return e ? e.checked : null; };
  const cats = [...document.querySelectorAll('.ir-rx-cat')].filter(c => c.checked)
    .map(c => c.dataset.key);
  const srcs = [...document.querySelectorAll('.ir-src')].filter(c => c.checked)
    .map(c => c.dataset.key);
  return { val, on, cats, srcs };
}

async function indSaveRules() {
  const f = _indRulesReadForm();
  try {
    if (_indRulesMode === 'order') {
      const body = {};
      const rx = f.on('ir-rx-own');
      if (rx !== null) body.build_reactions = rx;
      const mg = f.val('ir-margin');
      body.margin_pct = (mg === '' || mg === null) ? null : parseFloat(mg);
      if (_indRules.available.sources) body.source_keys = f.srcs;
      await apiSend('PATCH', `/api/industry/orders/${_indRulesOrderId}`, body);
      toast('Saved for this build', 'success');
      indCloseRules();
      if (typeof indRefreshStatus === 'function') indRefreshStatus();
      return;
    }
    const patch = {};
    patch.threshold = { marginal_pct: parseFloat(f.val('ir-marg')),
                        force_build: f.on('ir-force'), prioritize_speed: f.on('ir-speed') };
    const mg = f.val('ir-margin');
    patch.margin = { margin_pct: (mg === '' || mg === null) ? null : parseFloat(mg) };
    if (_indRules.available.reactions) {
      patch.reactions = { build_reactions: f.on('ir-rx-run'), buy_categories: f.cats };
    }
    if (_indRules.available.job_length) {
      const d = f.val('ir-joblen');
      patch.job_length = { max_reaction_job_days: (d === '' || d === null) ? null : parseFloat(d) };
    }
    if (_indRules.available.sources && document.querySelector('.ir-src')) {
      patch.sources = { keys: f.srcs, enabled: true };
    }
    // Its own endpoint, not a build-setup section: turning it on re-prices every queued build, so
    // it has a POST that states that rather than riding along with the rest of the rules.
    if (_indRules.available.per_order_plans && document.getElementById('ir-perorder')) {
      await apiSend('POST', '/api/industry/per-order-plans', { enabled: f.on('ir-perorder') });
    }
    const res = await apiSend('POST', '/api/industry/build-setup', patch);
    _indRules.account = res.account;
    const saved = document.getElementById('ir-saved');
    if (saved) { saved.textContent = 'Saved.'; setTimeout(() => { saved.textContent = ''; }, 2500); }
    toast('Build rules saved', 'success');
    indCloseRules();
    // The rules that were just changed are the ones the plan is priced against, so re-run it.
    if (_indPicked && (document.getElementById('indResult') || {}).innerHTML) indRunPlan();
  } catch (e) { toastError(e, 'Could not save'); }
}

// ── Read-only summaries, shown in place of the old strips once Build rules is on ───────────────
// Each states what is in force and what it cost THIS build, and links into the section that set it.
// The cost half is not decoration: a rule that quietly removes a whole sub-chain from the shopping
// list — as the reaction policy did, taking a Revelation's carbon fibre from ~26k to 6.4k — has to
// be legible where the number it changed is being read.
function _indRuleSummary(label, state, cost, onclick) {
  return `<div class="ind-forced-bar ind-rule-summary">`
    + `<span class="ind-forced-lbl">${_esc(label)}:</span> <span>${state}</span>`
    + (cost ? ` <span class="ind-rxp-delta">${cost}</span>` : '')
    + ` <button class="ind-link-btn" onclick="${onclick}">change</button></div>`;
}

function _indRxSummaryBar(d) {
  const pol = _indRxPolicy.policy || {};
  const cats = _indRxPolicy.categories || [];
  const bought = new Set(pol.buy_categories || []);
  const some = cats.filter(c => bought.has(c.key));
  const state = pol.build_reactions === false ? 'bought in, not made here'
    : some.length ? `${_esc(some.map(c => c.label.toLowerCase()).join(', '))} bought in`
    : 'made here';
  const rp = (d && d.reaction_policy) || null;
  let cost = '';
  if (rp && rp.isk) {
    const n = (rp.items || []).length;
    const what = `${n} reaction output${n === 1 ? '' : 's'}`;
    cost = rp.overridden ? `reacting ${what} here saves ${fmtIsk(rp.isk)}`
      : rp.isk > 0 ? `buying ${what} in adds ${fmtIsk(rp.isk)} to this build`
      : `buying ${what} in saves ${fmtIsk(-rp.isk)}`;
  }
  return _indRuleSummary('Reactions', state, cost, "openSettingsModal('buildrules')");
}


// Show the way in to the standing rules on the plan form itself. A page whose every number is
// shaped by rules it never mentions is how those rules went unfound in the first place — so this
// is a control, not a sentence in a hint nobody reads.
function indApplyBuildRulesGate() {
  const b = document.getElementById('indBuildRulesBtn');
  if (b) b.style.display = _indRulesActive() ? '' : 'none';
}
