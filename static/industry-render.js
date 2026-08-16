// ── Industry — the two plan renderers, and the notice stack they open with. ─────────────────
// _indRenderPlan (the modal) and _indRenderPlanBody (the build page) are the composition
// roots: everything above assembles blocks, this decides what a plan LOOKS like. The notices
// are here because the bar for being in one is a property of the plan as a whole.

// ── The notice stack, and the bar it has to clear ────────────────────────────────────────────
// ONE block above the plan, never a column of coloured banners. Everything in here either corrects
// a number the builder would otherwise believe (times, fees, an unpriced material) or names money
// they are spending (copies bought). A notice a reader will not act on is not worth its space,
// however true it is — which is what removed the "this schedule assumes unlimited blueprint
// copies" paragraph and the standalone "no skill data yet" box.

// Job times come from the account's REAL Industry / Advanced Industry levels (see
// account_industry_time_mults). When no character has been scanned there are no real levels to use
// and V/V stands in — say so, because an optimistic time that looks identical to a measured one is
// exactly the number people promise deliveries on.
function _indSkillBasisWarn(d) {
  if (d.skill_time_basis !== 'assumed') return '';
  return `<div class="ind-note-line">Job times assume Industry V and Advanced Industry V \u2014 no `
    + `character has been scanned for skills yet. Rescan to plan against your real training.</div>`;
}

// Job installation fee = EIV x (system cost index + facility tax + 4% SCC). With no build system
// configured the INDEX term is missing, so the fee is light by exactly that share \u2014 the SCC and any
// tax are still charged, so this is an understatement, not a zero. Worth saying plainly: the index
// runs from 0.14% to 17.25% across New Eden, so in a busy system it is most of the fee.
function _indCostBasisWarn(d) {
  const cb = d.cost_basis;
  // A DEFAULTED system is not the same as a configured one, and saying nothing about it would make
  // an assumption look like a fact. A structure you build in is a good answer; Jita is a reference
  // and will be wrong for a null-sec builder, so it says so and offers the fix either way.
  if (cb && cb.system_id && (cb.basis === 'structure' || cb.basis === 'reference')) {
    const what = cb.basis === 'structure'
      ? 'the system of a structure you build in'
      : 'Jita as a reference \u2014 your real index is probably lower';
    return `<div class="ind-note-line">Job fees are costed against ${what}, because no build system `
      + `is set. `
      + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Set your build system</button>`
      + ` to quote the real one.</div>`;
  }
  if (!cb || cb.system_id) return '';
  // Points at Structures & Markets, which is where the system actually lives (the planner reads the
  // account's reaction system + facility tax \u2014 account_build_defaults). It used to open Setup &
  // slots, which holds blueprints, stock and job slots and no way whatsoever to set a system: an
  // instruction that leads somewhere it can't be carried out is worse than no instruction.
  return `<div class="ind-note-line">Job fees exclude the system cost index \u2014 no build system is `
    + `set, so only the 4% SCC${cb.facility_tax_pct ? ' and facility tax' : ''} are counted. `
    + `<button class="ind-link-btn" onclick="openSettingsModal('markets')">Set your build system</button>`
    + ` (Structures &amp; Markets \u2192 <i>your reaction/build system</i>) for a true install cost.</div>`;
}

// The default reaction policy changed on 2026-08-05 (build hybrid polymers and biochemicals, buy
// composites & intermediates). It clears the notice bar because it moved what a build COSTS for an
// account that changed nothing — the net cost, and so the floor under every quote off it, is not the
// number they were looking at last week — and because the fix is one click away. It is deliberately
// not a second policy control: it says what moved and points at the row right below it.
//
// Shown ONLY to accounts still on the default (`defaulted` from the server — a stored policy was
// never touched by this change), and only until dismissed. Dismissal is localStorage, like
// `indFacilityNudge`: an acknowledgement of a one-off announcement is not worth a settings column,
// and the worst case of a new browser is one more line to close.
function _indRxDefaultNote() {
  if (!_indRxPolicy || !_indRxPolicy.defaulted) return '';
  try { if (localStorage.getItem('indRxDefaultNote') === 'off') return ''; } catch (e) {}
  return `<div class="ind-note-line">Reaction default changed: hybrid polymers and biochemicals are `
    + `now built (they feed later steps directly), composites &amp; intermediates bought. Set your `
    + `own under <i>Reactions for this build</i> below. `
    + `<button class="ind-link-btn" onclick="indDismissRxDefaultNote(this)">Dismiss</button></div>`;
}

function indDismissRxDefaultNote(btn) {
  try { localStorage.setItem('indRxDefaultNote', 'off'); } catch (e) {}
  const line = btn && btn.closest('.ind-note-line');
  const box = line && line.closest('.ind-notes');
  if (line) line.remove();
  // The block is one container; an empty one would leave its padding behind as a stray bar.
  if (box && !box.querySelector('.ind-note-line')) box.remove();
}

// A pin the plan could NOT honour. Silence here would be the worst outcome: the user stated where a
// family is built, the plan built it somewhere else, and nothing on the screen says which. One line,
// naming the family and what happened, because the fix (re-pin it, or turn the structure back on)
// is theirs to make.
function _indPinNote(d) {
  const rows = d.build_pins_unapplied || [];
  if (!rows.length) return '';
  const off = rows.some(r => r.reason === 'routing_off');
  const names = rows.map(r => r.label).join(', ');
  return `<div class="ind-note-line">Not built where you pinned it: <b>${_esc(names)}</b> — `
    + (off ? `per-structure routing is off for this account, so every job used your selected facility.`
           : `that structure isn't available for those jobs (removed, or it doesn't run that `
             + `activity), so the plan routed them automatically.`)
    + `</div>`;
}

// The one block. `withSkills` is the only thing that differs between the two renderers: the modal
// checks whether your characters can install the jobs this plan schedules, the live build page
// does not (and never has — don't "fix" that by quietly adding a panel to the busiest screen).
function _indNotices(d, withSkills) {
  const unres = (d.unresolved && d.unresolved.length)
    ? `<div class="ind-note-line">${d.unresolved.length} material(s) had no market price — cost is `
      + `a floor.</div>` : '';
  const body = unres + _indRxDefaultNote() + _indSkillBasisWarn(d) + _indCostBasisWarn(d)
    + _indCopyShortWarn(d) + _indParallelCopyNote(d) + _indPrintLimitNote(d)
    + _indMissingBpWarn(d) + _indPinNote(d) + (withSkills ? _indSkillWarn(d) : '');
  return body ? `<div class="ind-notes">${body}</div>` : '';
}

function _indRenderPlan(d, title) {
  _indReqMeTe = {};
  (d.requirements || []).forEach(r => { _indReqMeTe[r.type_id] = { me: r.me, te: r.te, me_source: r.me_source }; });
  const leftovers = (d.leftovers && d.leftovers.length)
    ? `<details class="ind-details"><summary>Reusable leftovers (${d.leftovers.length}) — ${fmtIsk(d.metrics.leftover_value || 0)} credited</summary>`
      + d.leftovers.map(l => `<div class="ind-tree-row"><span class="ind-tree-name">${_esc(l.name)}</span> `
        + `<span class="ind-tree-qty">×${Math.round(l.qty).toLocaleString()}</span>`
        + (l.value ? `<span class="ind-tree-cost">${fmtIsk(l.value)}</span>` : '') + `</div>`).join('') + `</details>` : '';
  const boughtIds = new Set((d.shopping_list || []).map(s => s.type_id));
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, boughtIds)
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  const stageModel = _indStageModel(tiersData);
  const allRoots = d.trees || (d.tree ? [d.tree] : []);
  const treeKids = allRoots.flatMap(t => (t.inputs || []).map(c => _indTreeNode(c, 0))).join('');
  const tree = treeKids
    ? `<details class="ind-details"><summary>Debug: full build tree</summary><div class="ind-tree">${treeKids}</div></details>` : '';
  return `<div class="pp-card">
    <h2 class="pp-card-title">${title}</h2>
    <div class="ind-body">
      ${_indMetricTiles(d.metrics)}
      ${_indNotices(d, true)}
      ${_indMarginalBar(d)}
      ${_indReactionPolicyBar(d)}
      ${_indStepsHtml(d, stageModel)}
      ${_indPipelineHtml(d, tiersData, stageModel)}
      <details class="ind-details" open><summary>Shopping list (${(d.shopping_list || []).length})</summary>${_indShoppingSections(d, stageModel, true)}</details>
      ${tree}
      ${leftovers}
    </div>
  </div>`;
}

// The plan's contents without the card chrome — the status view supplies its own heading and tiles,
// so it renders the pipeline/steps/shopping list directly rather than a card inside a card.
// You can't install a manufacturing job without the blueprint. Say so plainly, and be explicit
// that the quoted cost excludes it — a capital BPC is a large, invisible addition otherwise.
// Each rendered warning gets its own instance id. The plan is rendered in TWO places — the status
// view and the preview modal — and both can show the same missing blueprint at once. With a plain
// `bpcpx-<type_id>` the two blocks collide, getElementById returns whichever is first in the
// document (the status view, sitting behind the modal), and the modal's row sits on "checking
// contracts…" forever. That was a real bug, not a hypothetical.
let _indBpcSeq = 0;

// The prints the plan is SHORT of and will not buy for you. A reaction formula is durable — it is
// reused by every build after this one — so the plan says what another one is worth in TIME and
// leaves the spend to the builder. Nothing here is in any cost on the page.
//
// ONE line. It used to be a headed block with a row per step plus a paragraph of explanation, and
// above it a second block ("this schedule assumes unlimited blueprint copies") for the state where
// the account's blueprint picture is incomplete. That second one is GONE: it was prose saying a
// number might be optimistic, with nothing to do about it on a page already too dense. The
// coverage gate itself (`print_coverage` / `prints_known()`) is untouched — a half-connected
// account is still never capped — and the fact now rides in the build-time tile's tooltip
// (`_indStatusHeadline`) rather than a banner.
function _indPrintLimitNote(d) {
  const rows = (d.print_limits || []).filter(r => (r.extra || 0) > 0);
  if (!rows.length) return '';
  // The best trade in the list leads, because it is the one worth acting on; the rest are in the
  // tooltip, where looking something up costs nothing and reading it costs no space.
  const best = rows.slice().sort((a, b) => (b.hours - b.hours_if_held) - (a.hours - a.hours_if_held))[0];
  const detail = rows.map(r => `${r.name}: ${r.held} ${r.noun}${r.held === 1 ? '' : 's'} held, `
    + `${r.jobs} job${r.jobs === 1 ? '' : 's'} at ${_fmtHours(r.hours)} — +${r.extra} → `
    + `${_fmtHours(r.hours_if_held)}`).join('\n');
  return `<div class="ind-note-line" title="${_esc(detail)}">`
    + `${rows.length} step${rows.length === 1 ? '' : 's'} run in fewer jobs than your slots allow — `
    + `a print is locked while a job runs on it. Best: <b>+${best.extra} ${_esc(best.noun)}`
    + `${best.extra === 1 ? '' : 's'}</b> of ${_esc(best.name)} would take it from `
    + `${_fmtHours(best.hours)} to ${_fmtHours(best.hours_if_held)}. Nothing was bought for these.`
    + `</div>`;
}

// A print is LOCKED while a job runs on it, so two jobs of one type at the same moment need two
// prints. Where the plan buys them to keep your slots busy, that is a purchase nobody asked for —
// so the SPEND stays on the page, on its own line, and never disappears into the blueprint cost
// beside it. Buying copies to cover RUNS you're short of and buying them to fill SLOTS are two
// different decisions, and only one of them is about being able to build the thing at all.
// One line, not a headed block with a row per type: the number that matters is the total ISK, and
// which types it was spent on is a lookup (the tooltip), not a decision.
function _indParallelCopyNote(d) {
  const rows = (d.blueprint_parallel || []).filter(r => (r.copies || 0) > 0);
  if (!rows.length) return '';
  const total = rows.reduce((s, r) => s + (r.cost || 0), 0);
  const copies = rows.reduce((s, r) => s + r.copies, 0);
  const detail = rows.map(r => `${r.name}: ${r.jobs} job${r.jobs === 1 ? '' : 's'} at once, `
    + `${r.copies} extra cop${r.copies === 1 ? 'y' : 'ies'}${r.covered ? '' : ' (estimated)'} `
    + `— ${fmtIsk(r.cost)}`).join('\n');
  return `<div class="ind-note-line ind-note-spend" title="${_esc(detail)}">`
    + `<b>${fmtIsk(total)}</b> of the total is ${copies} blueprint cop${copies === 1 ? 'y' : 'ies'} `
    + `bought so ${rows.length === 1 ? 'this step runs' : 'these steps run'} in parallel — they buy `
    + `speed, not the ability to build.</div>`;
}

// Owning a COPY is not owning the blueprint for any batch size: it carries a fixed number of runs.
// This says so, because "you have the blueprint" while sixteen of twenty runs have nowhere to come
// from is the kind of quiet wrong that gets found at the industry terminal.
function _indCopyShortWarn(d) {
  const short = (d.requirements || []).filter(r => (r.runs_short || 0) > 0);
  if (!short.length) return '';
  const rows = short.map(r => {
    const have = (r.blueprint && r.blueprint.runs) || 0;
    // All three numbers, each named, on one line: what the BUILD needs, what the COPY carries, and
    // how many COPIES that leaves to buy. Two of the three used to be shown as bare counts either
    // side of the word "copy", which is exactly ambiguous enough to read as a run count on the
    // print itself.
    const buy = r.copies_to_buy
      ? `<span class="ind-bp-px">buy ${r.copies_to_buy} more cop${r.copies_to_buy === 1 ? 'y' : 'ies'}</span>`
      : `<span class="ind-bp-px">${r.runs_short} run${r.runs_short > 1 ? 's' : ''} short</span>`;
    return `<div class="ind-bp-row2"><span class="ind-bp-nm">${_esc(r.name)}`
      + `<span class="ind-bp-need">build needs ${r.runs} run${r.runs > 1 ? 's' : ''} · `
      + `your copy carries ${have} · ${r.runs_short} run${r.runs_short > 1 ? 's' : ''} short</span></span>`
      + buy + `</div>`;
  }).join('');
  return `<div class="ind-note-block"><b>Your blueprint ${short.length === 1 ? 'copy runs' : 'copies run'} out</b>`
    + `<div class="ind-bp-rows">${rows}</div>`
    + `<div class="ind-bp-warn-sub">A copy carries a fixed number of runs, so the rest of the batch `
    + `needs more copies — those are priced into the total above, at contract prices.</div></div>`;
}

function _indMissingBpWarn(d) {
  const miss = (d.metrics && d.metrics.missing_blueprints) || [];
  if (!miss.length) return '';
  const inst = ++_indBpcSeq;
  const rows = miss.map(m => {
    // How many runs this build needs of it — the thing that decides how many copies you must buy,
    // since a copy carries a fixed number of runs and one contract is one item.
    const need = m.runs_needed ? `<span class="ind-bp-need">${m.runs_needed} run${m.runs_needed > 1 ? 's' : ''} needed</span>` : '';
    return `<div class="ind-bp-row2"><span class="ind-bp-nm">${_esc(m.name)}${need}</span>`
    + `<span class="ind-bp-px" id="bpcpx-${inst}-${m.type_id}">checking contracts…</span></div>`;
  }).join('');
  // Fill the prices in after render — a cold contract index is a background scan, so the warning
  // must never wait on it.
  setTimeout(() => indLoadBpcPrices(inst, miss.map(m => m.type_id), miss), 0);
  // No nagging about roles or connecting more characters — the user can't act on that and doesn't
  // need to be told. Just say the list may be incomplete and give them the price.
  return `<div class="ind-note-block"><b>No blueprint found for ${miss.length === 1 ? 'this' : 'these'}</b>`
    + `<div class="ind-bp-rows">${rows}</div>`
    + `<div class="ind-bp-warn-sub">Blueprints in corp hangars aren't visible here, so prints you `
    + `already have can still show up in this list — it's a price so you can compare against a `
    + `local seller. Copy prices, where copies are listed, are included in the total above.</div></div>`;
}

// Skills you don't have to install the jobs this plan schedules. The server sends `skill_gaps` only
// while the `required_skills` feature is on, so the absent key — not a flag check here — is what
// keeps this silent when the feature is off.
//
// Stays quiet when there's nothing to say. A plan every character can already install produces no
// panel at all; this is a blocker list, not a skill sheet.
const _INDSK_ROMAN = ['0', 'I', 'II', 'III', 'IV', 'V'];
const _indSkLvl = n => _INDSK_ROMAN[n] || String(n);

function _indSkillWarn(d) {
  const g = d.skill_gaps;
  if (!g) return '';                       // feature off — server omitted the key
  // NOTHING is said unless a step is actually blocked. The two info states this used to render —
  // "the SDE hasn't backfilled blueprint_skills yet" and a bare "no skill data yet for X" box on a
  // plan with no gaps at all — were both banners about our own state of knowledge, on a page where
  // the space belongs to what the builder has to do. The unknown-characters line survives INSIDE a
  // real gap report, where it qualifies a finding they are already reading.
  if (!g.blocked_steps) return '';
  const unknown = g.characters_without_data || [];
  // A character we've never read skills for is a DIFFERENT answer from one who lacks the skills,
  // and it's the one the user can fix.
  const unknownNote = unknown.length
    ? `<div class="ind-sk-sub">No skill data yet for ${unknown.map(_esc).join(', ')} — `
      + `rescan ${unknown.length === 1 ? 'that character' : 'those characters'} to include `
      + `them in this check.</div>`
    : '';
  const summary = (g.missing || []).map(m =>
    `<span class="ind-sk-chip" title="Needed for ${m.steps} build step${m.steps === 1 ? '' : 's'}">`
    + `${_esc(m.name)} ${_indSkLvl(m.level)}</span>`).join('');
  const rows = (g.steps || []).map(s => {
    const who = s.character_name
      ? `<span class="ind-sk-who">closest: ${_esc(s.character_name)}</span>`
      : `<span class="ind-sk-who">no character with skill data</span>`;
    const miss = s.missing.map(m =>
      `<span class="ind-sk-miss">${_esc(m.name)} <b>${_indSkLvl(m.need)}</b>`
      + `<span class="ind-sk-have">have ${_indSkLvl(m.have)}</span></span>`).join('');
    return `<div class="ind-sk-row"><span class="ind-sk-nm">${_esc(s.name)}</span>${who}`
      + `<span class="ind-sk-misses">${miss}</span></div>`;
  }).join('');
  const n = g.blocked_steps;
  return `<div class="ind-note-block ind-note-block-skill"><b>Missing skills for ${n} build step${n === 1 ? '' : 's'}</b>`
    + `<div class="ind-sk-chips">${summary}</div>`
    + `<details class="ind-details"><summary>Which steps, and who comes closest</summary>`
    + `<div class="ind-sk-rows">${rows}</div></details>`
    + `<div class="ind-sk-sub">Skills don't pool across characters — one character installs one `
    + `job, so each step is checked against whichever of your characters comes closest.</div>`
    + unknownNote + `</div>`;
}

// Public-contract blueprint prices. Shows what's listed right now, and falls back to what they have
// historically gone for — blueprints sell out constantly, so "nothing listed today" is the normal
// case and still deserves an answer.
async function indLoadBpcPrices(inst, ids, miss) {
  if (!ids || !ids.length) return;
  const byId = {};
  (miss || []).forEach(m => { byId[m.type_id] = m; });
  let d = null;
  try {
    d = await api('/api/industry/bpc?type_ids=' + ids.join(','));
  } catch (e) {}
  const scanning = d && d.scan && d.scan.busy;
  ids.forEach(id => {
    const el = document.getElementById('bpcpx-' + inst + '-' + id);
    if (!el) return;
    const info = d && d.prices && d.prices[id];
    const bpc = info && info.bpc;
    if (bpc && bpc.live && bpc.live.count) {
      // Prefer what the plan actually worked out: the cheapest COMBINATION covering the runs this
      // build needs, and how many contracts that is. A single cheapest price is misleading when one
      // copy doesn't carry enough runs.
      const need = byId[id] || {};
      if (need.cost != null && need.copies) {
        el.innerHTML = `<b>${fmtIsk(need.cost)}</b> for ${need.copies} cop${need.copies === 1 ? 'y' : 'ies'}`
          + `<span class="ind-bp-sub2">${need.covered === false
              ? `only ${bpc.live.count} listed — not enough runs, rest estimated`
              : `covers ${need.runs_needed} run${need.runs_needed > 1 ? 's' : ''} · ${bpc.live.count} listed`}</span>`;
      } else {
        const runs = bpc.live.median_per_run ? ` · ${fmtIsk(bpc.live.median_per_run)}/run` : '';
        el.innerHTML = `<b>${fmtIsk(bpc.live.cheapest)}</b> cheapest now`
          + `<span class="ind-bp-sub2">${bpc.live.count} on contract · median ${fmtIsk(bpc.live.median)}${runs}</span>`;
      }
    } else if (bpc && bpc.history && bpc.history.count) {
      const days = Math.max(0, Math.round((Date.now() / 1000 - bpc.history.last_seen) / 86400));
      el.innerHTML = `<b>≈ ${fmtIsk(bpc.history.median)}</b> estimated`
        + `<span class="ind-bp-sub2">none listed now · ${bpc.history.count} seen historically, `
        + `last ${days === 0 ? 'today' : days + 'd ago'}</span>`;
    } else if (info && info.bpo && (info.bpo.live || info.bpo.history)) {
      const b = info.bpo.live || info.bpo.history;
      el.innerHTML = `<span class="ind-bp-sub2">no copies seen — originals from ${fmtIsk(b.cheapest)}</span>`;
    } else {
      el.innerHTML = `<span class="ind-bp-sub2">${scanning
        ? 'indexing Jita contracts — check back in a few minutes' : 'no contracts seen for this yet'}</span>`;
    }
  });
}

function _indRenderPlanBody(d) {
  _indReqMeTe = {};
  (d.requirements || []).forEach(r => { _indReqMeTe[r.type_id] = { me: r.me, te: r.te, me_source: r.me_source }; });
  // The queue plan carries one tree per ordered product (`trees`); a single-product preview carries
  // one (`tree`). Either way the tier walk merges them by type, matching the aggregated demand.
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, new Set((d.shopping_list || []).map(x => x.type_id)))
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  const stageModel = _indStageModel(tiersData);
  // Skill blockers belong HERE, not only in the preview modal. `_indSkillWarn` now says nothing
  // unless a step is genuinely blocked, so this adds a line to the build page exactly when nobody
  // on the account can install one of the jobs it is telling you to start — which is the one moment
  // it is worth the space. The queue plan has always carried `skill_gaps` (_run_queue_plan); it was
  // simply never rendered, so the blocker was visible while planning and invisible while building.
  return _indNotices(d, true)
    + _indMarginalBar(d)
    + _indReactionPolicyBar(d)
    + _indPipelineHtml(d, tiersData, stageModel)
    + _indStepsHtml(d, stageModel)
    + `<details class="ind-details"><summary>Shopping list (${(d.shopping_list || []).length})</summary>`
    + _indShoppingSections(d, stageModel) + `</details>`;
}

// Task waves only carry type_id; keep a name cache from the last plan's shopping/tree so waves read nicely.
let _indNameCache = {};
function _indName(tid) { return _indNameCache[tid] || ('#' + tid); }
function _indCacheNames(d) {
  (d.shopping_list || []).forEach(s => { _indNameCache[s.type_id] = s.name; });
  (d.targets || []).forEach(t => { _indNameCache[t.type_id] = t.name; });
  const walk = n => { if (!n) return; _indNameCache[n.type_id] = n.name; (n.inputs || []).forEach(walk); };
  walk(d.tree);
}
