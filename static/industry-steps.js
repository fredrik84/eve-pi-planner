// ── Industry — step 2's build side and step 4's checklist: the same steps, twice. ───────────
// The build tree, ME/TE and owned-copy chips, job chips, the done state a step cycles
// through and what a mark does locally, the step list, and the pipeline diagram.

// The blueprint chip for a step, and the one place that decides what a blueprint noun may sit next
// to. Three different numbers meet here and none of them is interchangeable:
//   * RUNS NEEDED — how many units this build wants. It belongs to the build, never to the print.
//   * RUNS PER COPY — what the copy you hold actually carries. A capital BPC is 1 run, always.
//   * COPIES TO BUY — how many contracts cover the shortfall.
// Rendering the first of those beside the word "BPC" is how a Phoenix builder was told the plan had
// found him a 2-run capital copy. It had not: he had ordered two hulls, off a 1-run copy, and the
// scheduler had correctly split it into two 1-run jobs. So the chip states the copy's OWN runs,
// and when the batch needs more than the copy carries it says so on the chip rather than leaving
// the run count next door to be misread as the copy's.
function _indOwnedBpChip(owned, runsNeeded) {
  if (!owned) return '';
  const kind = String(owned.kind || '').toUpperCase();
  const me = owned.me != null ? ` ME${owned.me}` : '';
  if (kind !== 'BPC') {
    return ` <span class="ind-owned" title="You own this ${kind} — an original, so it never runs out">${kind}${me}</span>`;
  }
  // `runs` is the coverage of EVERY copy you hold for this product, summed — so with more than one
  // the chip has to say so, or "BPC ME10 · 60 runs" reads as one enormous ME10 copy when it is
  // really three copies of mixed research, best first.
  const have = Math.max(0, Math.round(owned.runs || 0));
  const n = Math.max(1, Math.round(owned.copy_count || 1));
  const need = Math.max(0, Math.round(runsNeeded || 0));
  const short = need > have ? need - have : 0;
  const runTxt = `${have} run${have === 1 ? '' : 's'}`;
  const copyTxt = n > 1 ? ` across ${n} copies` : '';
  const tip = `You own ${n > 1 ? `${n} copies of this BPC carrying ${runTxt} in total`
    : `this BPC and it carries ${runTxt}`}`
    + (n > 1 ? `; the best-researched are used first (ME${owned.me} is the best of them)` : '')
    + (need ? `. This build needs ${need} run${need === 1 ? '' : 's'}` : '')
    + (short ? `, so ${short} more must come from further copies` : '')
    + '.';
  return ` <span class="ind-owned${short ? ' ind-owned-short' : ''}" title="${tip}">`
    + `BPC${me} · ${runTxt}${copyTxt}${short ? ` · ${short} short` : ''}</span>`;
}

// Compact tree row label (shared by leaves and collapsible nodes).
function _indTreeLabel(n) {
  const badge = n.decision === 'build'
    ? `<span class="ind-badge ind-build">build${n.activity === 'reaction' ? ' rx' : ''}${n.runs ? ' ×' + n.runs : ''}</span>`
    : n.decision === 'buy' ? '<span class="ind-badge ind-buy">buy</span>'
    : '<span class="ind-badge ind-unres">no price</span>';
  const cost = n.unit_cost != null ? `<span class="ind-tree-cost">${fmtIsk((n.unit_cost || 0) * (n.qty || 0))}</span>` : '';
  const owned = _indOwnedBpChip(n.owned, n.runs);
  return `<span class="ind-tree-name">${_esc(n.name)}</span> <span class="ind-tree-qty">×${Math.round(n.qty).toLocaleString()}</span> ${badge}${owned}${cost}`;
}

// Collapsible tree via native nested <details>: a node WITH built children folds (open only near
// the top so a deep build isn't a wall of text); leaves render as plain rows. Indent comes from
// the nesting, not per-row padding.
function _indTreeNode(n, depth) {
  const kids = (n.inputs || []).filter(c => c.decision === 'build' || (c.inputs && c.inputs.length));
  const leaves = (n.inputs || []).filter(c => !(c.decision === 'build' || (c.inputs && c.inputs.length)));
  if (!kids.length && !leaves.length) return `<div class="ind-tree-leaf">${_indTreeLabel(n)}</div>`;
  const open = depth < 1 ? ' open' : '';
  const childHtml = kids.map(c => _indTreeNode(c, depth + 1)).join('')
    + leaves.map(c => `<div class="ind-tree-leaf">${_indTreeLabel(c)}</div>`).join('');
  return `<details class="ind-tree-node"${open}><summary class="ind-tree-sum">${_indTreeLabel(n)}</summary>`
    + `<div class="ind-tree-kids">${childHtml}</div></details>`;
}

// ME/TE actually used per build step, keyed by type_id — filled from the plan's requirements so a
// job chip can show what it was costed at. An assumed efficiency that isn't visible is an invisible
// input to every number on the page.
let _indReqMeTe = {};
// The user's own ME/TE per product. NOT cleared when the product changes: it's a fact about a
// blueprint ("the copy I use is ME 10"), not about one build.
let _indMeTe = {};

const _IND_ME_SRC = {
  owned: 'from your own blueprint',
  // Deliberately worded as YOUR statement, not as a reading — a typed number and an ESI-read one
  // are different kinds of evidence and the chip is where that difference is visible.
  declared: 'the blueprint you declared by hand',
  contract: 'assumed from the contract copy this plan buys',
  override: 'you set this',
  default: 'un-researched — no blueprint of yours and none listed',
};

function _indMeTeChip(typeId) {
  const r = _indReqMeTe[typeId];
  if (!r || r.me_source === 'reaction') return '';     // reactions have no blueprint ME/TE
  const src = _IND_ME_SRC[r.me_source] || '';
  return `<button class="ind-mete ind-mete-${r.me_source}" id="mete-${typeId}"`
    + ` title="ME ${r.me}% materials / TE ${r.te}% time — ${_esc(src)}. Click to set what you'll really use."`
    + ` onclick="indEditMeTe(${typeId})">ME ${r.me} · TE ${r.te}</button>`;
}

// Two per-job controls that both amount to "the plan is wrong about this one, and I'd know":
// I'm further along than you think, and I never build this — see indCycleDone / indBlacklist below.
// Two corrections you can make to a job from the step list. Both are real buttons with words on
// them: the first cut used bare dimmed glyphs, which on a chip that already carries a name, a run
// count, a duration and an ME/TE tag were effectively invisible.
function _indJobActions(x) {
  let html = '';
  if (_featureActive('industry_manual_done')) {
    html += _indDoneBtn(_indProgTypeMap()[x.type_id], x.type_id, 'ind-job-act');
  }
  if (_featureActive('industry_blacklist')) {
    html += `<button class="ind-job-act ind-job-never" onclick="indBlacklist(${x.type_id}, true)"`
      + ` title="Always buy ${_esc(x.name)} instead of building it — on every build, until you undo it">always buy</button>`;
  }
  return html;
}

function _indJobChips(g) {
  return g.map(x => {
    const blocked = x.blocked || [];
    // Name the assignee, and say plainly when that assignee can't actually install it — an
    // instruction you can't follow is worse than no instruction, so it's marked inline rather
    // than left to the skills panel further up the page.
    const who = (x.who && x.who.length)
      ? `<span class="ind-wave-who${blocked.length ? ' ind-wave-who-blocked' : ''}" title="${
          blocked.length
            ? 'Missing skills: ' + _esc(blocked.join(', ')) + ' cannot install this job — see the missing-skills panel above'
            : 'Install this on ' + _esc(x.who.join(', '))
        }">on ${_esc(x.who.join(', '))}${blocked.length ? ' ⚠' : ''}</span>`
      : '';
    const t = _indProgTypeMap()[x.type_id];
    const isDone = t && t.required_runs > 0 && t.done_runs >= t.required_runs;
    return `<span class="ind-wave-job${isDone ? ' ind-wave-job-done' : ''}">`
      + `${_esc(x.name)} ×${x.runs}${x.activity === 'reaction' ? ' rx' : ''} · ${_fmtHours(x.dur)}`
      + who + _indMeTeChip(x.type_id) + _indJobActions(x) + `</span>`;
  }).join('');
}

// Progress is three-valued, so the control is too. Where a step stands right now, as far as the
// user is concerned: what ESI measured and what they said by hand, already combined by the server.
// A part-done step counts as running — some of it has happened, none of it is finished.
function _indDoneState(t) {
  if (!t || !t.required_runs) return 'none';
  if (t.done_runs >= t.required_runs) return 'done';
  if (t.running_runs > 0 || t.done_runs > 0 || t.manual_state === 'running') return 'running';
  return 'none';
}

// One click advances the step: not started → running → done → not started. The wrap-around is the
// point — a misclick has to be undoable with more clicks, never a dead end, and "done" was already
// the state you could take back.
function indCycleDone(typeId) {
  const st = _indDoneState(_indProgTypeMap()[typeId]);
  if (st === 'none') return _indPostDone(typeId, null, 'running');
  if (st === 'running') return _indPostDone(typeId, null, 'done');
  return _indPostDone(typeId, 0);                 // done → back to not started
}

// The step's button, wherever it appears — the pipeline card has its own click target, everything
// else uses this. **The label is the NEXT state, not the current one**: it reads "run" on a step
// that hasn't started and "done" once it is running, so the button says what pressing it does. Only
// the finished state names itself, because there the press is an undo.
function _indDoneBtn(t, typeId, cls) {
  const st = _indDoneState(t);
  const byHand = t && t.manual_state;
  if (st === 'done') {
    return `<button class="${cls} ind-job-done-on" onclick="indCycleDone(${typeId})" title="${
      byHand === 'done' ? 'You marked this done — click to start over'
                        : 'Already done. Click if that is wrong.'}">✓ done</button>`;
  }
  if (st === 'running') {
    return `<button class="${cls} ind-job-run-on" onclick="indCycleDone(${typeId})"`
      + ` title="This one is running${byHand === 'running' ? ' (you said so)' : ''} — click when it has finished">done</button>`;
  }
  return `<button class="${cls}" onclick="indCycleDone(${typeId})"`
    + ` title="Say this step is running — for work we can't see, like a job installed on a character that isn't connected">run</button>`;
}

// Repaint FIRST, save second. Ticking a step changes nothing the plan computes, so the browser can
// work out the new numbers itself — `max(observed, manual)`, the same rule the server applies — and
// redraw from the plan already on screen. The write still goes out, and its authoritative answer
// replaces the local guess when it lands, but nobody waits for a capital build to be re-planned
// twice over just to watch a card turn green.
async function _indPostDone(typeId, runs, state) {
  state = state || 'done';
  // Does this mark FINISH the step? "Do this now" and the pipeline's stage gating are computed
  // server-side and arrive on the plan (`d.install`) — the local fast path can recompute progress
  // because that is just max(observed, manual), but it cannot work out what became installable as a
  // result, and recomputing the checklist here is exactly what item 16 removed so the plan and the
  // checklist could never disagree. So a completing mark gives up the fast path and re-plans: that
  // is the one case where the whole point of the click is that the NEXT stage should appear.
  const completes = _indMarkCompletesStep(typeId, runs, state);
  const painted = !completes && _indApplyDoneLocally(typeId, runs, state);
  if (painted) _indPaintStatus(_indLastPlan, { local: true });
  try {
    const fresh = await apiSend('POST', '/api/industry/progress/done', { type_id: typeId, runs, state });
    _indProgress = (fresh && !fresh.empty) ? fresh : _indProgress;
    // Nothing was painted locally (no plan in hand, preview mode, or a mark that completes a step
    // and therefore needs the server's view of what is ready next) — fall back to the full path.
    if (painted) _indPaintStatus(_indLastPlan, { local: true }); else indRefreshStatus();
  } catch (e) {
    toastError(e, 'Could not save');
    indRefreshStatus();      // the local guess is now a lie — go and get the truth
  }
}

// Apply a mark to the progress we already hold, exactly as the server would. Returns false when
// there's nothing to apply to, in which case the caller falls back to a full refresh.
function _indApplyDoneLocally(typeId, runs, state) {
  const p = _indProgress;
  if (!p || !p.types || !_indLastPlan) return false;
  // Preview mode's numbers are fabricated, so editing them would be editing fiction — leave that
  // path exactly as it was and let the full refresh re-fetch the simulation.
  if (_indSim !== null) return false;
  const t = p.types.find(x => x.type_id === typeId);
  if (!t || !t.required_runs) return false;
  const need = t.required_runs;
  // `runs === null` is "all of it"; a number is that many; 0 clears the mark. The `observed_*`
  // counts are what the server measured with no mark at all, which is what makes this computable
  // without asking it — and what enforces the same precedence here: a mark is folded in with
  // max(), so it can move the step forward and never back over a measured signal.
  const cleared = runs === 0;
  const manDone = (!cleared && state !== 'running')
    ? (runs === null ? need : Math.max(0, Math.min(need, runs))) : 0;
  const manRun = (!cleared && state === 'running') ? need : 0;
  const observed = t.observed_runs != null ? t.observed_runs : 0;
  const obsRun = t.observed_running_runs != null ? t.observed_running_runs : (t.running_runs || 0);
  t.manual_runs = manDone;
  t.manual_state = cleared ? '' : state;
  t.done_runs = Math.min(need, Math.max(observed, manDone));
  t.running_runs = Math.min(Math.max(obsRun, manRun), Math.max(0, need - t.done_runs));
  t.waiting_runs = Math.max(0, need - t.done_runs - t.running_runs);
  t.pct = need ? Math.round(1000 * t.done_runs / need) / 10 : 0;
  // Headline counters are sums over the types, so they follow from the same edit.
  const sum = k => p.types.reduce((a, x) => a + (x[k] || 0), 0);
  p.totals = { required: sum('required_runs'), done: sum('done_runs'),
               running: sum('running_runs'), waiting: sum('waiting_runs') };
  // Same weighting the server uses (_weighted_pct): by job time, falling back to runs when no
  // schedule times are known. Recomputing this the old run-counted way would flash a different
  // number for the second it takes the real response to land.
  const hTot = p.types.reduce((a, x) => a + (x.job_hours || 0), 0);
  const hDone = p.types.reduce((a, x) => a + (x.required_runs ? (x.done_runs / x.required_runs) * (x.job_hours || 0) : 0), 0);
  p.hours = { total: Math.round(hTot * 100) / 100, done: Math.round(hDone * 100) / 100 };
  p.runs_pct = p.totals.required ? Math.round(1000 * p.totals.done / p.totals.required) / 10 : 0;
  p.pct = hTot > 0 ? Math.round(1000 * hDone / hTot) / 10 : p.runs_pct;
  // Order chips are units, not runs, and only the order FOR this product can move. Anything subtler
  // (a product already covered by stock, say) is corrected a moment later by the real response.
  (p.orders || []).forEach(o => {
    if (o.product_type_id !== typeId) return;
    o.done_units = Math.min(o.quantity, t.done_runs * (t.output_qty || 1));
    o.running_units = Math.min(t.running_runs * (t.output_qty || 1),
                               Math.max(0, o.quantity - o.done_units));
    o.pct = o.quantity ? Math.round(1000 * o.done_units / o.quantity) / 10 : 0;
    o.status = o.done_units >= o.quantity ? 'complete'
      : (o.running_units > 0 || o.done_units > 0) ? 'building' : 'waiting';
  });
  return true;
}

// Half a step is a real state — you install five of the twelve runs, they finish, the rest are
// waiting on a slot. But it is the RARE state, so it costs an extra click and the common
// "this one's finished" stays a single click on the card. The way in is the card's own run count,
// because "12 runs" is already the number you'd be correcting.
function indEditDoneRuns(ev, typeId, required, current) {
  if (ev) { ev.stopPropagation(); ev.preventDefault(); }   // don't also toggle the whole card
  const el = document.getElementById('pruns-' + typeId);
  if (!el) return;
  const wrap = document.createElement('span');
  wrap.className = 'ind-pipe-partedit';
  wrap.onclick = e => e.stopPropagation();
  wrap.innerHTML = `<input type="number" min="0" max="${required}" value="${current || 0}" id="pdone-${typeId}">`
    + `<span class="ind-pipe-partof">of ${required}</span>`
    + `<button class="ind-srcq-btn" onclick="indApplyDoneRuns(${typeId}, ${required})">set</button>`;
  el.replaceWith(wrap);
  const inp = document.getElementById('pdone-' + typeId);
  if (inp) { inp.focus(); inp.select();
    inp.onkeydown = e => { if (e.key === 'Enter') indApplyDoneRuns(typeId, required); }; }
}

function indApplyDoneRuns(typeId, required) {
  const inp = document.getElementById('pdone-' + typeId);
  const n = Math.max(0, Math.min(required, parseInt((inp || {}).value, 10) || 0));
  // All of them means ALL of them — store the sentinel, not the number, so the mark survives the
  // plan's run count changing later.
  _indPostDone(typeId, n >= required ? null : n);
}

// Edit in place on the chip: two numbers, and the plan re-runs against them. This is the "editing
// the plan" half — the same override can be set before a plan exists via the same map.
function indEditMeTe(typeId) {
  const el = document.getElementById('mete-' + typeId);
  if (!el) return;
  const cur = _indReqMeTe[typeId] || { me: 0, te: 0 };
  const wrap = document.createElement('span');
  wrap.className = 'ind-mete-edit';
  wrap.innerHTML = `ME <input type="number" min="0" max="10" value="${cur.me}" id="mete-me-${typeId}">`
    + ` TE <input type="number" min="0" max="20" value="${cur.te}" id="mete-te-${typeId}">`
    + ` <button class="ind-mete-ok" onclick="indApplyMeTe(${typeId})">Apply</button>`
    + (_indMeTe[typeId] ? ` <button class="ind-mete-clr" onclick="indClearMeTe(${typeId})" title="Back to what the plan works out for itself">reset</button>` : '');
  el.replaceWith(wrap);
}

function indApplyMeTe(typeId) {
  const me = parseFloat((document.getElementById('mete-me-' + typeId) || {}).value);
  const te = parseFloat((document.getElementById('mete-te-' + typeId) || {}).value);
  if (isNaN(me) || isNaN(te)) return;
  _indMeTe[typeId] = [Math.max(0, Math.min(10, me)), Math.max(0, Math.min(20, te))];
  _indSweep = null; _indSweepFailed = null;      // efficiency moves both cost and time
  _indReplanCurrent();
}

function indClearMeTe(typeId) {
  delete _indMeTe[typeId];
  _indSweep = null; _indSweepFailed = null;
  _indReplanCurrent();
}

// Whichever view is on screen. The overrides feed both the preview and the queue plan, so the one
// showing has to be the one that re-runs.
function _indReplanCurrent() {
  const out = document.getElementById('indResult');
  const jobs = [];
  if (_indPicked && out && out.innerHTML.trim()) jobs.push(indRunPlan());
  if (_indStatusVisible()) jobs.push(indRefreshStatus());
  return Promise.all(jobs);      // awaitable, so a caller can hold the scroll until it's repainted
}

function _indStepItems(g, open) {
  return `<details class="ind-step-items"${open ? ' open' : ''}><summary>show items</summary>`
    + `<div class="ind-wave-jobs">${_indJobChips(g)}</div></details>`;
}

// The schedule collapsed onto STAGES, sorted by when each starts.
//
// A wave is a scheduler artifact — jobs unlocking as slots free — so a 20-wave plan rendered 20
// "steps", which is noise: you do not do 20 different things, you work through a handful of stages
// and refill slots as they open. This turns waves into one entry per stage, carrying what the
// header has to be able to say: when it starts, when it has fully LANDED, the longest single job
// inside it, how many batches it was split into, and who installs each type.
//
// Split out of `_indStepsHtml` so the aggregation and the rendering can each be read on their own —
// the function was 100 lines of both. The two halves are pinned separately by
// `test_the_step_by_step_parts_account_for_the_whole`.
function _indStepStages(d, model) {
  const waves = (d.schedule && d.schedule.waves) || [];
  // Collapse the schedule onto STAGES. A wave is a scheduler artifact — jobs unlocking as slots
  // free — so a 20-wave plan used to render 20 "steps", which is noise: you don't do 20 different
  // things, you work through a handful of stages and refill slots as they open. One step per stage,
  // with the batch count folded into it as a note.
  const stageOfType = {};
  (model.cols || []).forEach((c, i) => c.builds.forEach(b => { stageOfType[b.type_id] = i; }));
  const byStage = {};
  waves.forEach(w => {
    (w.tasks || []).forEach(t => {
      const key = stageOfType[t.type_id] === undefined ? 'x' : stageOfType[t.type_id];
      const s = byStage[key] || (byStage[key] = { key, jobs: 0, runs: 0, start: Infinity, end: 0, longest: 0, batches: new Set(), by: {} });
      s.jobs += 1;
      s.runs += t.runs;
      s.start = Math.min(s.start, w.start_hours);
      // A step is an OFFSET into one wall clock, not a length — and the header used to show the
      // offset alone. On a real 2× Phoenix queue that read "Finished — 2 jobs ≈ +14h 34m" above
      // "Done — built in ≈ 13d 12h": the 12d 21h the Phoenix job itself runs for appeared nowhere
      // except inside the collapsed "show items" fold. Carry the longest job and the moment the
      // stage has fully landed, so the steps account for the total instead of contradicting it.
      s.end = Math.max(s.end, w.start_hours + t.duration_hours);
      s.longest = Math.max(s.longest, t.duration_hours);
      s.batches.add(w.start_hours);
      const g = s.by[t.type_id] || (s.by[t.type_id] = { name: t.name || _indName(t.type_id), runs: 0, activity: t.activity, dur: 0, who: [], type_id: t.type_id });
      g.runs += t.runs;
      g.dur = Math.max(g.dur, t.duration_hours);
      // Who installs it. A type's runs can be split across several toons' slots, so collect them
      // all — "who do I log in as" is the question every stage after the first left unanswered.
      if (t.character_name && !g.who.includes(t.character_name)) g.who.push(t.character_name);
      // `skill_ok === false` means the scheduler had to fall back to someone who provably can't
      // install this job (nobody who can had a free slot). Strictly false — undefined means the
      // check didn't run, and "not checked" must never render as a problem.
      if (t.character_name && t.skill_ok === false) {
        (g.blocked || (g.blocked = [])).includes(t.character_name) || g.blocked.push(t.character_name);
      }
    });
  });
  return Object.values(byStage).sort((a, b) => a.start - b.start);
}

function _indStepsHtml(d, model) {
  const waves = (d.schedule && d.schedule.waves) || [];
  if (!waves.length) return '';
  const shop = d.shopping_list || [];

  const stages = _indStepStages(d, model);
  if (!stages.length) return '';

  let n = 0;
  let html = '<div class="ind-steps"><div class="ind-steps-title">Step by step</div>';
  if (shop.length) {
    n++;
    html += `<div class="ind-step"><div class="ind-step-hd"><span class="ind-step-num">${n}</span>Buy your materials</div>`
      + `<div class="ind-step-body">${shop.length} item${shop.length > 1 ? 's' : ''} · ${fmtIsk(d.metrics.materials_cost)} — full list below.</div></div>`;
  }
  stages.forEach((s, i) => {
    n++;
    const col = model.cols[s.key];
    const title = col ? col.label : 'Remaining jobs';
    const items = Object.values(s.by).sort((a, b) => b.dur - a.dur);
    const jobs = `${s.jobs} job${s.jobs > 1 ? 's' : ''} · ${s.runs.toLocaleString()} run${s.runs > 1 ? 's' : ''}`;
    const first = i === 0 && s.start <= 0.01;
    const when = first
      ? '<span class="ind-step-tag">do this now</span>'
      : `<span class="ind-step-when">starts +${_fmtHours(s.start)}</span>`;
    // The two numbers that make the step add up: how long its longest job runs for, and when the
    // whole step has landed. Without them a reader can only add the start offsets, which on any
    // build with one dominant job lands nowhere near the total.
    const runs = `<span class="ind-step-when" title="The longest job in this step runs ${_fmtHours(s.longest)}. Everything in this step has landed ${_fmtHours(s.end)} after you start the build.">runs ${_fmtHours(s.longest)} · all landed by +${_fmtHours(s.end)}</span>`;
    // Several batches = the same stage restarted as slots freed, not extra decisions to make.
    const batches = s.batches.size > 1
      ? `<span class="ind-step-note">in ${s.batches.size} batches as slots free</span>` : '';
    // A stage finishes as a unit — nothing in the next one can start until all of it has landed —
    // so it is worth being able to say so in one click instead of stepping through every job.
    // Offered only on a stage that has steps we can mark, and only where the mark can do anything.
    const stageMark = (_featureActive('industry_manual_done') && s.key !== 'x'
                       && _indStageTypeIds(s.key).length)
      ? `<button class="ind-link-btn ind-step-done-all" onclick="indMarkStageDone(${JSON.stringify(s.key)})"`
        + ` title="Mark every job in this step finished, and move the checklist on to the next one">mark stage done</button>`
      : '';
    html += `<div class="ind-step${first ? ' ind-step-now' : ' ind-step-later'}">`
      + `<div class="ind-step-hd"><span class="ind-step-num">${n}</span>${_esc(title)} — ${jobs} ${when}${runs}${batches}${stageMark}</div>`
      + _indStepItems(items, first) + `</div>`;
  });
  // Say what the total MEASURES, and name the step that drives it. Two numbers on one screen that
  // disagree with no explanation is the defect this line exists to close: the steps are start
  // times on one wall clock, so they were never meant to be added, and the build's length is
  // whichever step lands last — usually the final assembly job, running for days on its own.
  const driver = stages.reduce((a, b) => (b.end > a.end ? b : a), stages[0]);
  const driverCol = model.cols[driver.key];
  const driverName = driverCol ? driverCol.label : 'the last step';
  html += `<div class="ind-step ind-step-done"><div class="ind-step-hd"><span class="ind-step-num">✓</span>Done — ${_esc(d.target ? d.target.name : 'product')} built in ≈ ${_fmtHours(d.metrics.makespan_hours)}</div>`
    + `<div class="ind-step-body">Wall-clock from installing the first job, with everything running in parallel — the times above are points on that same clock, not lengths to add up.`
    + ` ${_esc(driverName)} is what sets it: it starts at +${_fmtHours(driver.start)} and its longest job runs ${_fmtHours(driver.longest)}.</div></div>`;
  return html + '</div>';
}

// The build as a PRODUCTION MATRIX: stage columns flow left→right (raw/reacted on the left,
// finished product on the right) and each ROW is a building — the reaction structure, the
// manufacturing structure, and the market you buy from. A persistent labelled row per building is
// the point: you read across a row to see everything one structure does, and down a column to see
// what a stage needs from each. Reactions row sits on top because it's what happens first.
// Hovering a card traces its whole chain in both directions.
let _indPipeGraph = { inputsOf: {}, consumersOf: {} };

// One card in the pipeline grid — the most-read surface in the tab. `prog` is the progress map
// keyed by type; it is passed in rather than read here so a grid renders against one snapshot.
function _indPipeBuildCard(e, prog) {
  // The pipeline card is the most-read surface in the tab and the one where this went wrong:
  // "2 runs" (the batch) sat directly beside a bare "BPC", which reads as a 2-run copy.
  const owned = _indOwnedBpChip(e.owned, e.runs);
  const runs = e.runs ? `<span class="ind-pipe-runs" id="pruns-${e.type_id}">${e.runs.toLocaleString()}&nbsp;run${e.runs > 1 ? 's' : ''}</span>` : '';
  const qty = `×${Math.round(e.qty).toLocaleString()}`;
  // Live state from real ESI jobs, when we have it — the pipeline doubles as a progress board.
  // Three states you can read at a glance: done (green border), in the cooker (accent + glow),
  // waiting (greyed back). Anything with no progress data at all keeps the neutral card.
  const p = prog[e.type_id];
  let state = '', cls = '';
  if (p && p.required_runs) {
    if (p.done_runs >= p.required_runs) {
      state = '<span class="ind-pipe-state ind-st-done">✓ done</span>'; cls = ' ind-pipe-is-done';
    } else if (p.running_runs > 0) {
      state = `<span class="ind-pipe-state ind-st-run">${p.running_runs} cooking</span>`; cls = ' ind-pipe-is-run';
      if (p.done_runs > 0) state += `<span class="ind-pipe-state ind-st-part">${p.done_runs}/${p.required_runs}</span>`;
    } else if (p.done_runs > 0) {
      state = `<span class="ind-pipe-state ind-st-part">${p.done_runs}/${p.required_runs}</span>`; cls = ' ind-pipe-is-run';
    } else {
      state = '<span class="ind-pipe-state ind-st-wait">waiting</span>'; cls = ' ind-pipe-is-wait';
    }
  }
  // The card already IS the progress readout for this step, so it's also where you correct it:
  // each click advances it one state, wrapping back round so a misclick costs clicks and not
  // data. A tick tucked into the step-by-step chips was the first cut and too small to read,
  // never mind aim at.
  const markable = _featureActive('industry_manual_done') && p && p.required_runs;
  const st = markable ? _indDoneState(p) : 'none';
  const onclick = markable ? ` onclick="indCycleDone(${e.type_id})"` : '';
  const nextTip = st === 'done' ? ' Click to set it back to not started.'
    : st === 'running' ? ' Click when it has finished.'
    : ' Click to say it is running.';
  const tip = `${_esc(e.name)} — ${qty}${e.runs ? ', ' + e.runs + ' runs' : ''}. Hover to trace its chain.`
    + (markable ? nextTip : '');
  // The run count doubles as the way in to a partial mark when there's more than one run to
  // split. One run can't be half done, so it stays plain text there.
  const runsCell = (markable && p.required_runs > 1)
    ? `<span class="ind-pipe-runs ind-pipe-runs-edit" id="pruns-${e.type_id}"`
      + ` onclick="indEditDoneRuns(event, ${e.type_id}, ${p.required_runs}, ${p.done_runs})"`
      + ` title="${p.done_runs} of ${p.required_runs} runs done \u2014 click to set how many, rather than the whole step">`
      // Label stays the run count: how many are DONE is already on this card as its state
      // badge, and the same number twice on a card this small reads as two different ones.
      + `${p.required_runs.toLocaleString()}&nbsp;runs</span>`
    : runs;
  return `<div class="ind-pipe-card ind-pipe-build${cls}${markable ? ' ind-pipe-markable' : ''}"${onclick}`
    + ` data-tid="${e.type_id}" title="${tip}">`
    + `<span class="ind-pipe-name">${_esc(e.name)}</span>`
    + `<span class="ind-pipe-meta"><span class="ind-pipe-qty">${qty}</span>${runsCell}${owned}${state}</span></div>`;
}


// The one card that stands for a whole stage's bought materials, rather than one card each.
function _indPipeBuyCard(buys, t) {
  const names = buys.slice(0, 25).map(b => b.name).join(', ') + (buys.length > 25 ? '…' : '');
  const members = buys.map(b => b.type_id).join(',');
  return `<div class="ind-pipe-card ind-pipe-buys" data-members="${members}" title="${_esc(names)} — click to jump to this stage's shopping list" onclick="_indJumpToStage(${t})"><span class="ind-pipe-name">Buy ${buys.length} material${buys.length > 1 ? 's' : ''}</span>`
    + `<span class="ind-pipe-meta">in shopping list ↓</span></div>`;
}

function _indPipelineHtml(d, tiersData, model) {
  const roots = d.trees || (d.tree ? [d.tree] : []);
  if (!roots.length || !roots.some(t => (t.inputs || []).length)) return '';
  const { inputsOf, consumersOf } = tiersData;
  _indPipeGraph = { inputsOf: inputsOf || {}, consumersOf: consumersOf || {} };

  const cols = model.cols;
  if (!cols.length) return '';

  const isRx = e => e.activity === 'reaction';
  const isMfg = e => e.activity !== 'reaction';
  // Row per building, in the order the work actually happens: react → manufacture → (buy feeds both).
  const rows = [
    { key: 'rx', title: 'Reactions', sub: _indBuildingLabel('reaction') || 'reaction structure',
      pick: c => c.builds.filter(isRx) },
    { key: 'mfg', title: 'Manufacturing', sub: _indBuildingLabel('manufacturing') || 'your structure',
      pick: c => c.builds.filter(isMfg) },
    { key: 'buy', title: 'Buy', sub: 'from market', pick: c => c.buys },
  ].filter(r => cols.some(c => r.pick(c).length));

  // No "build" tag on the card — the row it sits in already says Reactions vs Manufacturing, so
  // repeating it just costs width. Qty and runs are what actually differ per card.
  const prog = _indProgTypeMap();
  // The two card renderers are top-level functions above this one; this function is the grid.

  // Header row: empty corner over the building labels, then one label per stage.
  let html = `<div class="ind-pipe-corner"></div>`;
  cols.forEach((col, i) => {
    // With live progress, the counter becomes "done/total jobs" for the stage instead of a bare
    // count of things to build.
    let count = col.builds.length ? `<span>${col.builds.length}</span>` : '';
    if (col.builds.length && Object.keys(prog).length) {
      let need = 0, did = 0;
      col.builds.forEach(b => { const p = prog[b.type_id]; if (p) { need += p.required_runs; did += p.done_runs; } });
      if (need) count = `<span class="${did >= need ? 'ind-hd-done' : ''}" title="${did} of ${need} jobs done in this stage">${did}/${need}</span>`;
    }
    html += `<div class="ind-pipe-hd${col.t === 0 ? ' ind-pipe-hd-final' : ''}${i < cols.length - 1 ? ' ind-pipe-hd-flow' : ''}">${col.label}${count}</div>`;
  });

  // One grid row per building; empty cells keep every stage aligned across the rows.
  rows.forEach(r => {
    html += `<div class="ind-pipe-rowlbl ind-row-${r.key}"><span class="ind-pipe-rowname">${r.title}</span>`
      + `<span class="ind-pipe-rowsub" title="${_esc(r.sub)}">${_esc(r.sub)}</span></div>`;
    cols.forEach(col => {
      const mine = r.pick(col);
      let cards = '';
      if (mine.length) {
        if (r.key === 'buy') {
          cards = _indPipeBuyCard(mine, col.t);
        } else {
          const sorted = mine.slice().sort((a, b) => (b.qty || 0) - (a.qty || 0));
          cards = sorted.slice(0, 10).map(e => _indPipeBuildCard(e, prog)).join('');
          if (sorted.length > 10) cards += `<div class="ind-pipe-more">+${sorted.length - 10} more</div>`;
        }
      }
      html += `<div class="ind-pipe-cell ind-row-${r.key}${col.t === 0 ? ' ind-pipe-final' : ''}">${cards}</div>`;
    });
  });

  return `<details class="ind-details" open><summary>Build pipeline</summary>`
    + `<p class="ind-pipe-hint">Each row is a building, each column a stage. Hover a step to trace its`
    + ` whole chain${_featureActive('industry_manual_done') ? ', or click one to step it on: not started → running → done' : ''}.</p>`
    + `<div class="ind-pipe-scroll"><div class="ind-pipe" style="--ind-cols:${cols.length}">${html}</div></div></details>`;
}

// The type_ids a card stands for: a build card is one type, a condensed buy card is many.
function _indCardTids(card) {
  if (!card) return [];
  if (card.dataset.tid) return [Number(card.dataset.tid)];
  return (card.dataset.members || '').split(',').filter(Boolean).map(Number);
}

// Walk an edge map transitively from a set of seeds, returning everything reachable (excluding the
// seeds). Cycle-guarded via the visited set.
function _indReach(seeds, edges) {
  const seen = new Set();
  const stack = [...seeds];
  while (stack.length) {
    const cur = stack.pop();
    (edges[cur] || []).forEach(n => { if (!seen.has(n)) { seen.add(n); stack.push(n); } });
  }
  seeds.forEach(s => seen.delete(s));
  return seen;
}

// Hover trace: dim the pipeline, then light up the hovered step plus its WHOLE chain in both
// directions — everything it ultimately feeds (so hovering stage 3 lights stage 2 *and* stage 1)
// and everything that ultimately feeds it, not just the immediate neighbours.
function _indPipeHover(card) {
  const grid = card.closest('.ind-pipe');
  if (!grid) return;
  const { inputsOf, consumersOf } = _indPipeGraph;
  const self = new Set(_indCardTids(card));
  const feeds = _indReach(self, consumersOf);   // downstream — all it ends up in
  const fedBy = _indReach(self, inputsOf);      // upstream — everything that goes into it
  grid.classList.add('ind-pipe-focus');
  grid.querySelectorAll('.ind-pipe-card').forEach(c => {
    c.classList.remove('ind-hi-self', 'ind-hi-out', 'ind-hi-in');
    if (c === card) { c.classList.add('ind-hi-self'); return; }
    const tids = _indCardTids(c);
    if (tids.some(t => feeds.has(t))) c.classList.add('ind-hi-out');
    else if (tids.some(t => fedBy.has(t))) c.classList.add('ind-hi-in');
  });
}

function _indPipeClearHover(grid) {
  if (!grid) return;
  grid.classList.remove('ind-pipe-focus');
  grid.querySelectorAll('.ind-pipe-card').forEach(c => c.classList.remove('ind-hi-self', 'ind-hi-out', 'ind-hi-in'));
}

// Delegated once at the document level — the pipeline is re-rendered via innerHTML on every plan,
// so per-element listeners would be lost each time.
document.addEventListener('mouseover', e => {
  if (!e.target.closest) return;
  const card = e.target.closest('.ind-pipe-card');
  if (card) { _indPipeHover(card); return; }
  // Moved into the pipeline but not onto a card (gap/lane label) — drop the trace.
  const grid = e.target.closest('.ind-pipe');
  if (grid) _indPipeClearHover(grid);
});
document.addEventListener('mouseout', e => {
  if (!e.target.closest) return;
  const grid = e.target.closest('.ind-pipe');
  if (grid && !grid.contains(e.relatedTarget)) _indPipeClearHover(grid);
});

// Does a mark FINISH a step? Only a completing mark needs the server's view of what became
// installable — see _indPostDone. A partial ("3 of 12 runs"), a "running" mark and an un-mark all
// leave the stage gating exactly where it was, so they keep the fast local repaint.
function _indMarkCompletesStep(typeId, runs, state) {
  if (state !== 'done') return false;                 // running / clearing never unblocks anything
  const t = (_indProgTypeMap() || {})[typeId];
  if (!t || !t.required_runs) return true;            // nothing to reason with — take the safe path
  if (runs === 0) return false;                       // an un-mark
  const marked = runs === null || runs === undefined ? t.required_runs : runs;
  const observed = t.observed_runs != null ? t.observed_runs : 0;
  return Math.max(observed, marked) >= t.required_runs;
}

// Mark every step of a stage done in one action. A stage completes as a unit — nothing in the next
// one can start until all of it has landed — so ticking it out step by step was busywork whose
// intermediate states were all wrong. One call, one re-plan, and the checklist moves on.
async function indMarkStageDone(stageIdx) {
  const ids = _indStageTypeIds(stageIdx);
  if (!ids.length) return;
  const model = _indStageModelForPlan();
  const label = ((model.cols || [])[stageIdx] || {}).label || 'this stage';
  if (!await ppConfirm(`Mark ${label} done? ${ids.length} step${ids.length === 1 ? '' : 's'} `
        + `will be marked finished — click any of them again to correct it.`,
        { okLabel: 'Mark stage done', danger: false })) return;
  try {
    const fresh = await apiSend('POST', '/api/industry/progress/done',
                                { type_ids: ids, state: 'done' });
    _indProgress = (fresh && !fresh.empty) ? fresh : _indProgress;
  } catch (e) { toastError(e, 'Could not save'); }
  // Always the full path: the whole point is that the next stage becomes startable, and only a
  // re-planned `install` block knows that.
  indRefreshStatus();
}

// The stage model for the plan currently on screen, derived the same way the pipeline derives it.
//
// Memoised on the plan OBJECT, not a copy of it: the derivation is pure in `_indLastPlan`, and
// `_indStepsHtml` asks for it once per stage while rendering — so a six-stage build walked the whole
// recipe tree six times to answer a question it had already answered. A new plan is a new object and
// misses the cache by identity, which is the only invalidation this needs.
let _indStageModelPlan = null;      // the plan the memo below was derived from
let _indStageModelMemo = null;
function _indStageModelForPlan() {
  const d = _indLastPlan;
  if (!d) return { cols: [], stageOf: {} };
  if (_indStageModelPlan === d) return _indStageModelMemo;
  const boughtIds = new Set((d.shopping_list || []).map(s => s.type_id));
  const roots = d.trees || d.tree;
  const tiersData = roots ? _indComputeTiers(roots, boughtIds)
    : { byType: {}, tiers: {}, maxT: 0, inputsOf: {}, consumersOf: {} };
  _indStageModelPlan = d;
  _indStageModelMemo = _indStageModel(tiersData);
  return _indStageModelMemo;
}

// The BUILT steps a stage owns. `cols` is indexed by stage, and each column already carries the
// builds that belong to it — the same list the pipeline column renders, so the button can never
// mark a different set of steps from the ones shown under it.
function _indStageTypeIds(stageIdx) {
  const col = (_indStageModelForPlan().cols || [])[stageIdx];
  if (!col) return [];
  const need = {};
  ((_indProgress && _indProgress.types) || []).forEach(t => { need[t.type_id] = t.required_runs; });
  return (col.builds || []).map(b => b.type_id).filter(t => need[t]);
}
