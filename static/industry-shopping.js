// ── Industry — the tier/stage model, and step 2's buy side. ─────────────────────────────────
// _indComputeTiers/_indStageModel are shared with the pipeline and the build page, and live
// here because the shopping list is their heaviest reader. Then: the always-buy blacklist,
// the reaction policy bar, the borderline-components strip, and multibuy copy.

// Walk the build tree once into { byType (with tier + type_id), tiers (tier -> [entries]), maxT }.
// Shared by the pipeline visualization and the per-stage shopping list so their stage numbering
// always matches — a "Buy" card in the pipeline links to the exact same stage in the list below.
// `bought` = the type_ids that actually appear on the plan's shopping list, and it OVERRIDES the
// tree's own decision. The tree comes from build_plan (pure cost-optimal) while the shopping list
// comes from plan_queue, which additionally flips components to "buy" for speed or negligible
// saving — so the tree alone would show a build step for something you're really purchasing. When
// a type is bought we also stop descending: its sub-materials aren't yours to make any more.
function _indComputeTiers(tree, bought) {
  const buys = bought instanceof Set ? bought : new Set(bought || []);
  // A queue can hold several products, so accept either one root or a list of them. Aggregated
  // demand is shared across them, which is exactly what the tier walk already merges by type_id.
  const roots = Array.isArray(tree) ? tree.filter(Boolean) : (tree ? [tree] : []);
  const byType = {};
  const inputsOf = {};      // type_id -> Set of type_ids it consumes
  const consumersOf = {};   // type_id -> Set of type_ids that consume it
  const walk = ((n, depth) => {
    if (!n) return;
    const isBought = buys.has(n.type_id) && depth > 0;   // the root is always built
    const e = byType[n.type_id] || (byType[n.type_id] = { type_id: n.type_id, name: n.name, decision: n.decision, activity: n.activity, owned: n.owned, qty: 0, runs: 0, tier: depth });
    e.qty += n.qty || 0;
    e.tier = Math.max(e.tier, depth);
    if (isBought) {
      e.decision = 'buy';
      e.runs = 0;
      return;                                            // bought ⇒ a leaf; don't expand its inputs
    }
    e.runs += n.runs || 0;
    if (n.decision === 'build' || n.decision === 'buy') e.decision = n.decision;
    (n.inputs || []).forEach(c => {
      (inputsOf[n.type_id] || (inputsOf[n.type_id] = new Set())).add(c.type_id);
      (consumersOf[c.type_id] || (consumersOf[c.type_id] = new Set())).add(n.type_id);
      walk(c, depth + 1);
    });
  });
  roots.forEach(r => walk(r, 0));
  const tiers = {};
  Object.values(byType).forEach(e => (tiers[e.tier] = tiers[e.tier] || []).push(e));
  const maxT = Object.keys(tiers).length ? Math.max(...Object.keys(tiers).map(Number)) : 0;
  return { byType, tiers, maxT, inputsOf, consumersOf };
}

// The stage model both the pipeline and the shopping list render from, so their stage numbering
// can never drift apart.
//
// Only BUILDS define a stage — buying isn't a step in the production chain, it's a prerequisite of
// one. Bought materials therefore don't get columns of their own (which spread the pipeline out
// with a leading "Stage 0" that was nothing but purchases); each one is filed under the stage that
// actually consumes it. A material needed by several stages is filed under the EARLIEST one
// (deepest tier), so the list reads "buy this before you start stage N".
function _indStageModel(tiersData) {
  const { byType, tiers, consumersOf } = tiersData;
  const buildTiers = Object.keys(tiers)
    .map(Number)
    .filter(t => (tiers[t] || []).some(e => e.decision === 'build'))
    .sort((a, b) => b - a);                       // deepest first = left to right
  if (!buildTiers.length) return { cols: [], stageOf: {} };

  const deepest = buildTiers[0];
  const stageOf = {};                             // bought type_id -> stage tier it belongs to
  Object.values(byType).forEach(e => {
    if (e.decision === 'build') return;
    let best = null;
    (consumersOf[e.type_id] || []).forEach(cid => {
      const c = byType[cid];
      if (!c || c.decision !== 'build') return;
      if (best === null || c.tier > best) best = c.tier;   // earliest consuming stage
    });
    // No build consumer (shouldn't happen — bought nodes are leaves): park it at the first stage.
    stageOf[e.type_id] = best === null ? deepest : best;
  });

  const cols = buildTiers.map((t, i) => ({
    t,
    index: i,
    label: t === 0 ? 'Finished' : `Stage ${i + 1}`,
    // "For Finished" reads wrong in the shopping list — name the act, not the column.
    shopLabel: t === 0 ? 'the final build' : `Stage ${i + 1}`,
    builds: (tiers[t] || []).filter(e => e.decision === 'build'),
    buys: Object.values(byType).filter(e => e.decision !== 'build' && stageOf[e.type_id] === t),
  }));
  return { cols, stageOf };
}

function _indShopRowHtml(s) {
  // "Low saving" is a verdict; on its own it asks the user to trust it. Say what building this one
  // would actually have saved (or cost) and let them overrule it per material.
  // Explains why this row is on the list. The ACTION lives in the decision strip above the plan
  // (_indMarginalBar) and only there — this list is collapsed by default and stage-grouped, which
  // is a fine place to look something up and a terrible place to hide a decision.
  let marginal = '';
  if (s.bought_marginal) {
    const sv = s.marginal_saving;
    const why = sv == null ? 'Building this would save too little to be worth a job'
      : sv > 0 ? `Building this batch yourself would save ${fmtIsk(sv)} — under the threshold, so it's bought`
      : `Building this batch yourself would cost ${fmtIsk(-sv)} MORE than buying it`;
    marginal = ` <span class="ind-marginal-badge" title="${_esc(why)}">low saving</span>`
      + (sv != null ? ` <span class="ind-shop-note">${sv > 0 ? `saves ${fmtIsk(sv)} if built`
                                                             : `${fmtIsk(-sv)} dearer to build`}</span>` : '');
  }
  // A blacklisted material is on the list because of a standing rule, not because building lost on
  // cost — without saying so the plan just looks like it got the make-or-buy call wrong.
  const never = s.blacklisted
    ? ` <button class="ind-never-badge" onclick="indBlacklist(${s.type_id}, false)"`
      + ` title="On your always-buy list. Click to let the planner decide again.">always buy</button>` : '';
  // …and the same for the reaction policy, one rung coarser: bought because this account doesn't
  // run that kind of reaction, not because the make-or-buy math came out that way.
  const rxp = s.reaction_policy
    ? ` <span class="ind-never-badge ind-rxp-badge" title="Bought because your builds don't run this`
      + ` kind of reaction. Change that in the strip above the plan.">not reacted</span>` : '';
  return `<tr><td>${_esc(s.name)}`
    + `${s.bought_for_speed ? ' <span class="ind-speed-badge" title="Bought instead of built to save time">for speed</span>' : ''}`
    + `${never}${rxp}${marginal}</td>`
    + `<td class="ind-num">${Math.round(s.qty).toLocaleString()}</td>`
    + `<td class="ind-src">${s.source ? _esc(s.source) : '<span class="pp-warn">no price</span>'}</td>`
    + `<td class="ind-num">${s.line_cost != null ? fmtIsk(s.line_cost) : '—'}</td></tr>`;
}

// ── Always-buy blacklist ────────────────────────────────────────────────────────────────────
// The account's standing "never build this" list. It changes what every plan does, so like the
// forced-build overrides it is shown next to the shopping list it produces rather than buried in a
// settings panel — a rule you can't see is one you can't remember setting.
let _indBlacklist = [];

async function indLoadBlacklist() {
  if (!_featureActive('industry_blacklist')) return;
  try { _indBlacklist = ((await api('/api/industry/blacklist')) || {}).items || []; }
  catch (e) { _indBlacklist = []; }
}

async function indBlacklist(typeId, add) {
  try {
    _indBlacklist = ((await apiSend('POST', '/api/industry/blacklist',
                                    { type_id: typeId, add: !!add })) || {}).items || [];
  } catch (e) { toastError(e, 'Could not save'); return; }
  _indSweep = null; _indSweepFailed = null;      // the make-or-buy mix moved, so cost and time did
  return _indKeepScroll(() => _indReplanCurrent());
}

function _indBlacklistChipsHtml() {
  if (!_featureActive('industry_blacklist') || !_indBlacklist.length) return '';
  return `<div class="ind-forced-bar ind-never-bar"><span class="ind-forced-lbl">Always bought:</span>`
    + _indBlacklist.map(b =>
        `<button class="ind-forced-chip" onclick="indBlacklist(${b.type_id}, false)" title="Let the planner decide again">`
        + `${_esc(b.name)} <span class="ind-forced-x">✕</span></button>`).join('')
    + `<span class="ind-src-meta">An order set to build one of these anyway still builds it.</span></div>`;
}

// ── Which reactions this account's builds run ───────────────────────────────────────────────
// A standing way of operating, like the always-buy list, one rung coarser: a builder who doesn't
// run reactions shouldn't have to blacklist every output by hand.
//
// It belongs with the make-or-buy CONTROLS (beside the "worth building instead?" strip), never in
// the notice stack — that block was trimmed to what a builder acts on, and a decision surface is
// not a notice. Hence one row: the switch, and the per-family detail folded BEHIND it, because
// "we don't react" is the common case and "…except biochemicals" is the rare one.
//
// The labels come from the server registry (`categories`), never from here.
let _indRxPolicy = null;
let _indRxCatsOpen = false;

async function indLoadReactionPolicy() {
  if (!_featureActive('industry_reaction_policy')) { _indRxPolicy = null; return; }
  try { _indRxPolicy = await api('/api/industry/reaction-policy'); }
  catch (e) { _indRxPolicy = null; }
}

async function indSetReactionPolicy(body) {
  try { _indRxPolicy = await apiSend('POST', '/api/industry/reaction-policy', body); }
  catch (e) { toastError(e, 'Could not save'); return; }
  _indSweep = null; _indSweepFailed = null;      // the make-or-buy mix moved, so cost and time did
  return _indKeepScroll(() => _indReplanCurrent());
}

function indToggleReactionCats() {
  _indRxCatsOpen = !_indRxCatsOpen;
  return _indKeepScroll(() => _indReplanCurrent());
}

function indSetReactionCat(key, buy) {
  const cur = new Set(((_indRxPolicy || {}).policy || {}).buy_categories || []);
  if (buy) cur.add(key); else cur.delete(key);
  return indSetReactionPolicy({ buy_categories: [...cur] });
}

function _indReactionPolicyBar(d) {
  if (!_featureActive('industry_reaction_policy') || !_indRxPolicy) return '';
  // With the consolidated surface on, this strip STOPS being a control and becomes a statement of
  // what is in force with a way through to the thing that set it. Two places to change one rule is
  // how the sprawl started; the summary keeps the manifesto's "report what the shortcut cost"
  // without the disguised-control problem that hid this setting in the first place.
  if (_indRulesActive()) return _indRxSummaryBar(d);
  const pol = _indRxPolicy.policy || {};
  const cats = _indRxPolicy.categories || [];
  const runs = pol.build_reactions !== false;
  const bought = new Set(pol.buy_categories || []);

  // What the convenience cost — the same rule the low-saving strip follows: report it, don't take
  // it quietly. Signed as "what BUILDING these saves", so one figure reads correctly both ways.
  const rp = (d && d.reaction_policy) || null;
  let delta = '';
  if (rp && rp.isk) {
    const n = (rp.items || []).length;
    const what = `${n} reaction output${n === 1 ? '' : 's'}`;
    delta = rp.overridden
      ? `<span class="ind-rxp-delta ind-rxp-good" title="This build makes its own reactions, against your standing rule.">`
        + `reacting ${what} here saves ${fmtIsk(rp.isk)} on this build</span>`
      : rp.isk > 0
        ? `<span class="ind-rxp-delta" title="What buying these instead of reacting them adds to THIS build's cost — the floor under any price you quote off it.">`
          + `buying ${what} in adds ${fmtIsk(rp.isk)} to this build</span>`
        : `<span class="ind-rxp-delta ind-rxp-good" title="Buying these is cheaper than reacting them for this build.">`
          + `buying ${what} in saves ${fmtIsk(-rp.isk)} on this build</span>`;
  }

  // The state in one phrase. Only ever about THIS build — the Reactions tab is a separate feature
  // with its own slot planning, and turning reactions off here says nothing about it.
  const some = cats.filter(c => bought.has(c.key));
  const state = !runs ? 'bought in, not made here'
    : some.length ? `${some.map(c => c.label.toLowerCase()).join(', ')} bought in`
    : 'made here';

  const detail = (runs && _indRxCatsOpen)
    ? `<div class="ind-rxp-cats">` + cats.map(c =>
        `<label class="ind-rxp-cat" title="${_esc(c.description || '')}">`
        + `<input type="checkbox" ${bought.has(c.key) ? 'checked' : ''}`
        + ` onchange="indSetReactionCat('${_esc(c.key)}', this.checked)">`
        + ` buy ${_esc(c.label)}</label>`).join('')
      + `<span class="ind-src-meta">An order can still be set to make its own.</span></div>` : '';

  return `<div class="ind-forced-bar ind-rxp-bar">`
    + `<span class="ind-forced-lbl">Reactions for this build:</span>`
    + `<button class="ind-forced-chip${runs ? '' : ' ind-rxp-off'}"`
    + ` onclick="indSetReactionPolicy({ build_reactions: ${!runs} })"`
    + ` title="${runs ? 'This account runs its own reactions. Click if you buy the outputs instead.'
                     : 'Reaction outputs are bought and their sub-steps drop off the plan. Click to react them yourself again.'}">`
    + `${_esc(state)}</button>`
    + (runs ? `<button class="ind-link-btn" onclick="indToggleReactionCats()">`
              + `${_indRxCatsOpen ? 'hide families' : 'by family…'}</button>` : '')
    + delta + `</div>` + detail;
}

// ── The borderline components, and the decision about them ──────────────────────────────────
// The engine buys anything whose saving is too small to be worth a job. That's a judgement about
// the user's time, so it's theirs to overrule — but the evidence for overruling it (what building
// each one would actually save) sat inside the shopping list, which is collapsed by default and
// grouped by stage, and in the queued view carried no button at all. A decision nobody can see is
// not a decision they get to make.
//
// So it lives here instead: one strip, above the plan, listing ONLY the borderline items. Not a
// second copy of the shopping list — that list keeps the "low saving" badge as an explanation of
// why a row is there, and nothing else. One place to decide, one place to look things up.
function _indMarginalBar(d) {
  const rows = (d.shopping_list || [])
    .filter(s => s.bought_marginal && (s.marginal_saving || 0) > 0)
    .sort((a, b) => b.marginal_saving - a.marginal_saving);
  if (!rows.length) return '';
  const total = rows.reduce((a, s) => a + s.marginal_saving, 0);
  // Every borderline component is listed, not a top-six: the slider selects across the whole list,
  // and a control that says "builds 9 of 14" above six visible chips is asking to be misread.
  const show = rows.slice(0, 24);
  const chips = show.map(s =>
    `<button class="ind-marg-chip" id="margchip-${s.type_id}" data-sav="${s.marginal_saving}"`
    + ` onclick="indBuildAnyway(${s.type_id}, '${_esc(s.name).replace(/'/g, "\\'")}')"`
    // Say what it costs and what it buys, and stop there. An earlier version ended "which is your
    // call, not ours", which reads as the tool bracing for blame rather than helping you decide.
    + ` title="Build ${_esc(s.name)} yourself instead of buying it: saves ${fmtIsk(s.marginal_saving)}, costs you one more job">`
    + `${_esc(s.name)} <span class="ind-marg-save">+${fmtIsk(s.marginal_saving)}</span></button>`).join('');
  const more = rows.length > show.length
    ? `<button class="ind-link-btn" onclick="indOpenShoppingList()">+${rows.length - show.length} more in the shopping list</button>` : '';

  // Take them in bulk instead of one at a time. This is NOT a second make-or-buy threshold — the
  // saving-% slider decides what gets suggested here; this decides how many of those suggestions
  // you accept in one go, over the list that slider already produced. A builder who would take any
  // job worth 10m shouldn't have to click seven chips to say so.
  _indMargRows = rows.map(s => ({ type_id: s.type_id, name: s.name, saving: s.marginal_saving }));
  const max = Math.ceil(rows[0].marginal_saving);
  // Where the slider was left last time, so a refresh doesn't silently move it back and change what
  // the strip appears to be offering. Clamped, because the next build's savings are different
  // numbers entirely. Restoring the POSITION applies nothing on its own — you still press the button.
  let start = parseFloat(localStorage.getItem('indMargCut'));
  if (!(start >= 0) || start > max) start = max;
  const bulk = (rows.length > 1 && max > 0)
    ? `<div class="ind-marg-bulk">`
      + `<label class="ind-src-meta">Build everything worth more than`
      + ` <input type="range" id="indMargCut" min="0" max="${max}" step="${Math.max(1, Math.round(max / 200))}"`
      + ` value="${start}" oninput="indMargCutLabel()"></label>`
      + `<span id="indMargCutInfo" class="ind-marg-cutinfo"></span>`
      + `<button class="ind-marg-apply" onclick="indBuildAllAbove()">Build these</button></div>` : '';

  return `<div class="ind-marg-bar"><span class="ind-marg-lbl">Worth building instead?</span>`
    + `<span class="ind-src-meta">${rows.length} component${rows.length > 1 ? 's are' : ' is'} bought`
    + ` because each saves little on its own — ${fmtIsk(total)} in total. Click one to build it,`
    + ` or take several at once below. Building some changes the shared batch, so a different set`
    + ` can be borderline afterwards — that is the plan re-costing itself, not new work appearing.</span>`
    + `<div class="ind-marg-chips">${chips}${more}</div>${bulk}</div>`;
}

// The borderline components currently on screen, so the bulk control can act on them without
// re-deriving the list from a plan that may already have been replaced.
let _indMargRows = [];

function _indMargCut() {
  const el = document.getElementById('indMargCut');
  return el ? parseFloat(el.value) : Infinity;
}

function _indMargAbove() {
  const cut = _indMargCut();
  return _indMargRows.filter(r => r.saving >= cut);
}

// Live feedback while dragging — and the LIST is the feedback, not just a counter. Dragging marks
// exactly which chips the button would take, so what you're about to accept is the thing you're
// looking at. A number saying "builds 3 of 7" over an unchanged row of chips reads as a control
// that isn't connected to anything.
function indMargCutLabel() {
  const info = document.getElementById('indMargCutInfo');
  if (!info) return;
  const cut = _indMargCut();
  const picked = _indMargAbove();
  const gain = picked.reduce((a, r) => a + r.saving, 0);
  info.textContent = picked.length
    ? `${fmtIsk(cut)} — builds ${picked.length} of ${_indMargRows.length}, saving ${fmtIsk(gain)}`
    : `${fmtIsk(cut)} — nothing that high`;
  const btn = document.querySelector('.ind-marg-apply');
  if (btn) btn.disabled = !picked.length;
  document.querySelectorAll('.ind-marg-chip').forEach(el => {
    const sav = parseFloat(el.getAttribute('data-sav'));
    el.classList.toggle('ind-marg-in', isFinite(sav) && sav >= cut);
    el.classList.toggle('ind-marg-out', isFinite(sav) && sav < cut);
  });
  try { localStorage.setItem('indMargCut', String(cut)); } catch (e) {}
}

// One press, then keep going: building these makes their own inputs a bulk demand, which can make
// THOSE worth building too. The server iterates to a fixpoint so the answer is stable — after this
// there is nothing left above the cut-off — rather than leaving the user to chase a list that
// regrows each time they accept its advice.
async function indBuildAllAbove() {
  const picked = _indMargAbove();
  if (!picked.length) return;
  // No queue yet (the preview): nothing to iterate against, so take the single pass.
  if (!_indStatusVisible() || !(_indOrders || []).length) {
    return _indKeepScroll(() => _indForceBuildMany(picked));
  }
  const btn = document.querySelector('.ind-marg-apply');
  if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
  let d;
  try {
    d = await apiSend('POST', '/api/industry/orders/force-above',
                      { ..._indQueueBody(), min_saving: _indMargCut() });
  } catch (e) { toastError(e, 'Could not save'); if (btn) btn.textContent = 'Build these'; return; }
  const n = (d.added || []).length;
  if (n) {
    toast(`Building ${n} more component${n === 1 ? '' : 's'}`
          + (d.rounds > 1 ? ` — ${d.rounds} passes, since building some made others worth building` : ''));
  }
  return _indKeepScroll(() => indRefreshStatus());
}

// Overrule the buy-it shortcut for one component. Where that override is STORED depends on whether
// there's a queue yet: a queued build keeps it on the order (the queue unions force_build_ids
// across orders, so one order carries it for the whole batch — and the ⚒ tag on that order chip is
// how you take it back), while the preview keeps it in the session map until the build is queued.
async function indBuildAnyway(typeId, name) {
  return _indKeepScroll(() => _indForceBuildMany([{ type_id: typeId, name }]));
}

// One or many, one round trip and ONE re-plan either way. Overruling seven components must not mean
// seven requests each re-planning the whole queue against a batch the next one is about to change.
async function _indForceBuildMany(items) {
  if (!items.length) return;
  if (!_indStatusVisible() || !(_indOrders || []).length) {
    items.forEach(i => _indForcedTypes.set(i.type_id, i.name || String(i.type_id)));
    _indSweep = null; _indSweepFailed = null;
    return indRunPlan();
  }
  const order = _indOrders[0];
  const ids = [...new Set([...(order.force_build_ids || []), ...items.map(i => i.type_id)])];
  try { await apiSend('PATCH', `/api/industry/orders/${order.id}`, { force_build_ids: ids }); }
  catch (e) { toastError(e, 'Could not save'); return; }
  // Building them changes the batch every other decision was weighed against, so the plan really
  // does have to re-run — but see _indKeepScroll for why you don't get thrown to the top.
  return indRefreshStatus();
}

// Re-planning replaces the whole card, and while it's being fetched the page is a short spinner —
// so the browser leaves you at the top of a document that just lost most of its height. Anyone
// overruling one borderline component usually wants to overrule the next one too, and re-finding
// the strip each time is what makes that tedious. Hold the scroll position across the repaint.
async function _indKeepScroll(run) {
  const y = window.scrollY;
  try { await run(); } finally { window.scrollTo(0, y); }
}

// The components the user overruled, with a way back — once forced they vanish from the shopping
// list (they're built now), so without this the override would be invisible and unrepeatable.
function _indForcedChipsHtml() {
  if (!_indForcedTypes.size) return '';
  return `<div class="ind-forced-bar"><span class="ind-forced-lbl">Building anyway:</span>`
    + [..._indForcedTypes.entries()].map(([tid, name]) =>
        `<button class="ind-forced-chip" onclick="indUnforceBuildType(${tid})" title="Go back to buying it">`
        + `${_esc(name)} <span class="ind-forced-x">✕</span></button>`).join('')
    + `</div>`;
}

// The shopping list grouped by the stage that needs each material — buy just what the next step
// needs, or everything at once. Grouping/labels match the pipeline's "Buy N materials" cards
// exactly, since both render from the same _indStageModel().
function _indShoppingSections(d, model, allowForce) {
  const list = d.shopping_list || [];
  if (!list.length) return '<p class="pp-sub">Nothing to buy — built entirely from stock/recipes.</p>';
  const byId = {};
  list.forEach(s => { byId[s.type_id] = s; });
  _indShopStageData = {};
  let sections = '';
  let listed = 0;
  model.cols.forEach(col => {
    const rows = col.buys.map(e => byId[e.type_id]).filter(Boolean);
    if (!rows.length) return;
    listed += rows.length;
    _indShopStageData[col.t] = rows.map(r => ({ name: r.name, qty: r.qty }));
    const stageCost = rows.reduce((a, s) => a + (s.line_cost || 0), 0);
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-${col.t}">`
      + `<div class="ind-shop-stage-hd"><span>For ${_esc(col.shopLabel || col.label)} — ${rows.length} item${rows.length > 1 ? 's' : ''} · ${fmtIsk(stageCost)}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy(${col.t})">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rows.map(_indShopRowHtml).join('')}</tbody></table></div>`;
  });
  // Anything the stage model didn't place (defensive — keeps the list complete no matter what).
  const placed = new Set(model.cols.flatMap(c => c.buys.map(e => e.type_id)));
  const rest = list.filter(s => !placed.has(s.type_id));
  if (rest.length) {
    _indShopStageData['other'] = rest.map(r => ({ name: r.name, qty: r.qty }));
    sections += `<div class="ind-shop-stage" id="ind-shop-stage-other">`
      + `<div class="ind-shop-stage-hd"><span title="Not linked to a build stage — please report this">Not tied to a stage — ${rest.length} item${rest.length > 1 ? 's' : ''}</span>`
      + `<button class="ind-copy-btn ind-copy-sm" onclick="indCopyMultibuy('other')">Copy this stage</button></div>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Qty</th><th>Source</th><th class="ind-num">Cost</th></tr></thead><tbody>${rest.map(_indShopRowHtml).join('')}</tbody></table></div>`;
  }
  const totalCost = list.reduce((a, s) => a + (s.line_cost || 0), 0);
  return `<div class="ind-shop-bar"><button class="ind-copy-btn" onclick="indCopyMultibuy()">Copy everything</button>`
    + `<span class="ind-shop-tot">${list.length} items · ${fmtIsk(totalCost)}</span></div>`
    + (allowForce ? _indForcedChipsHtml() : '') + _indBlacklistChipsHtml() + sections;
}

// Copy a shopping list (or one stage of it, if `stage` is given) in EVE's Multibuy paste format
// ("Item Name<tab>qty" per line) so it can be pasted straight into the in-game Multibuy window.
function indCopyMultibuy(stage) {
  const list = (stage !== undefined && _indShopStageData[stage]) ? _indShopStageData[stage] : ((_indLastPlan && _indLastPlan.shopping_list) || []);
  if (!list.length) return;
  _indCopyText(list.map(s => `${s.name}\t${Math.ceil(s.qty)}`).join('\n'));
}

// Jump from a pipeline "Buy N materials" card down to that exact stage in the shopping list below.
// Open the (collapsed) shopping list and scroll to it — the place to read the rest of the
// borderline rows, since the strip above only carries as many as you can decide from at a glance.
function indOpenShoppingList() {
  const bar = document.querySelector('.ind-shop-bar');
  const details = bar && bar.closest('details');
  if (details) details.open = true;
  (details || bar || {}).scrollIntoView && (details || bar).scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function _indJumpToStage(t) {
  const el = document.getElementById('ind-shop-stage-' + t);
  if (!el) return;
  const details = el.closest('details');
  if (details) details.open = true;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('ind-shop-flash');
  setTimeout(() => el.classList.remove('ind-shop-flash'), 1200);
}
