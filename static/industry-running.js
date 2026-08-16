// ── Industry — steps 5 to 7: a build that has started. ──────────────────────────────────────
// The install checklist and who is eligible for it, per-order material sourcing, and the
// jobs actually running.

// ── "To install now" checklist + in-progress jobs ───────────────────────────────────────────
async function indRefreshJobs() {
  try { await apiSend('POST', '/api/industry/jobs/refresh'); } catch (e) {}
  indLoadSlots();
  indLoadSetupSummary();
  await indRefreshStatus();   // redraws install / pipeline / running from the fresh job data
}

// "Do this now", written as instructions rather than a status. We know which characters have free
// slots and the plan knows which jobs are ready, so the checklist names WHO installs WHAT instead
// of reporting that "a slot" is free somewhere and leaving you to work it out.
// Collapse a character's assigned jobs to one line per PRODUCT. A big batch is split into one job
// per free slot, so a character can be handed a dozen identical installs — listing each separately
// turns one action ("start 12 of these") into twelve lines. Grouped, the checklist stays readable
// whether the plan has 18 jobs or 300. Longest job first, since that's what gates the stage.
// Does this plan install jobs in more than one structure? Only then is naming the building on
// every job worth the space — one structure means the answer is the same every line.
function _indIsMultiSite() {
  return ((_indLastPlan && _indLastPlan.build_sites) || []).length > 1;
}

// The routing's station changes ("Parts to move") are NOT rendered, and the plan no longer
// computes them. A builder whose jobs are routed to two structures already knows the parts have
// to travel; a list restating it was a panel that changed nothing anyone does.

function _indGroupJobs(jobs) {
  const by = {};
  (jobs || []).forEach(j => {
    const g = by[j.type_id] || (by[j.type_id] = {
      type_id: j.type_id, name: j.name || ('#' + j.type_id), activity: j.activity,
      count: 0, minRuns: Infinity, maxRuns: 0, totalRuns: 0, dur: 0, runsList: [], why: j.why,
      // Which structure to install it in. Only present when the plan is routed across several.
      site: j.site,
      // …and whether it is there because the user PINNED that family, not because it scored best.
      sitePinned: j.site_pinned,
    });
    g.count += 1;
    g.minRuns = Math.min(g.minRuns, j.runs);
    g.maxRuns = Math.max(g.maxRuns, j.runs);
    g.totalRuns += j.runs;
    g.runsList.push(j.runs);
    g.dur = Math.max(g.dur, j.duration_hours || 0);
  });
  return Object.values(by).map(g => ({
    ...g,
    // Bucket the jobs by run count, biggest batch first. An uneven split used to render as a
    // range ("165–166") or a total, and both leave the reader doing arithmetic to work out what
    // to actually type into the industry window. The buckets ARE the instruction: start 8 jobs
    // of 165 runs and 1 of 166. Usually one or two buckets, since the splitter divides a batch
    // across free slots, but nothing here assumes that.
    buckets: Object.entries(g.runsList.reduce((m, r) => (m[r] = (m[r] || 0) + 1, m), {}))
      .map(([runs, n]) => ({ runs: Number(runs), n }))
      .sort((a, b) => b.n - a.n || b.runs - a.runs),
  })).sort((a, b) => b.dur - a.dur);
}

// One labelled line per slot POOL. The two pools used to be bare pip strips sitting side by side
// with nothing naming them, so they read as one row of dots and the reaction pips looked like
// manufacturing jobs spilling into reaction slots. The name and the count carry the meaning; the
// pips are decoration on top of it.
function _indSlotRow(label, cls, used, free, assigned, slots) {
  if (!slots) return '';
  // The pips already say how many are busy, filling and free — spelling all three out again in
  // words was longer than the card is wide. The label disambiguates the pool, one number gives the
  // count at a glance, and the full breakdown stays in the tooltip.
  return `<div class="ind-slotrow ind-slotrow-${cls}" title="${assigned} to start · ${used} busy`
    + ` · ${slots} ${label.toLowerCase()} slot${slots > 1 ? 's' : ''}">`
    + `<span class="ind-slotlbl">${label}</span>`
    + `<span class="ind-slotset">${_indSlotPips(used, free, assigned, cls)}</span>`
    + `<span class="ind-slotnum"><b>${assigned}</b>/${slots}</span></div>`;
}

function _indSlotPips(used, free, assigned, cls) {
  const total = used + free;
  let out = '';
  for (let i = 0; i < total; i++) {
    const k = i < used ? 'busy' : (i < used + assigned ? 'fill' : 'open');
    out += `<span class="ind-pip ind-pip-${k} ind-pip-${cls}"></span>`;
  }
  return out || '<span class="ind-pip-none">no slots</span>';
}

// `d` is the install block from the plan response. It's passed in rather than fetched: asking the
// server for it meant re-planning the whole queue, which was the slowest thing on the page.
function indRenderInstall(d) {
  const el = document.getElementById('indInstall');
  if (!el) return;
  try {
    if (!d) { el.innerHTML = ''; return; }
    if (d.empty || !d.ready || !d.ready.length) { el.innerHTML = ''; return; }

    const doers = (d.characters || []).filter(c => c.assigned > 0);
    const cards = doers.map(c => {
      const groups = _indGroupJobs(c.jobs);
      const jobs = groups.map(g => {
        // Say exactly what to install. Neither "9× 165–166 runs each" nor a 1,486-run total
        // tells you what to type — both hand the reader a division problem. One entry per
        // distinct run count does: "8× 165 runs · 1× 166 runs" is nine installs, spelled out.
        const each = `<span class="ind-do-runs" title="${g.count} job${g.count > 1 ? 's' : ''} · `
          + `${g.totalRuns.toLocaleString()} runs in total">`
          + g.buckets.map(b => (g.count > 1 ? `<b>${b.n}×</b> ${b.runs}` : `<b>${b.runs}</b>`)
              + ` run${b.runs > 1 ? 's' : ''}`).join(' · ')
          + `</span>`;
        // Why this job is that long. "Everything else is 5h, why is this one 2h32m" has one
        // answer — something needs it sooner — and it is unanswerable from the screen otherwise.
        const w = g.why || {};
        const why = w.bound_by === 'consumer' && w.needed_by_name
          ? ` — held to this because ${_esc(w.needed_by_name)} needs it then`
          : w.bound_by === 'pace' ? ` — matched to the plan's pace (${_fmtHours(w.pace_h)})`
          : '';
        const dur = `<span class="ind-do-dur" title="${w.runs_per_job || 1} run(s) per job${_esc(why)}">`
          + `${_fmtHours(g.dur)}${why ? ' <span class="ind-do-why">?</span>' : ''}</span>`;
        // Where to install it. With group-specific rigs the plan may spread a build over several
        // structures, and "install 40 runs" without naming the building is half an instruction.
        // A PINNED step says so: "I chose this building" and "the tool worked it out" are different
        // facts about the same line, and only one of them is worth arguing with.
        const where = g.site && _indIsMultiSite()
          ? `<span class="ind-do-site${g.sitePinned ? ' ind-do-site-pin' : ''}" title="Install in `
            + `${_esc(g.site)}${g.sitePinned ? ' — you pinned this family here' : ''}">`
            + `@ ${_esc(g.site)}${g.sitePinned ? ' (pinned)' : ''}</span>` : '';
        return `<li class="ind-do-job"><span class="ind-do-name">${_esc(g.name)}</span>${each}`
          + where
          + `<span class="ind-do-act ind-do-${g.activity}">${g.activity === 'reaction' ? 'reaction' : 'industry'}</span>`
          + dur + `</li>`;
      }).join('');
      const mUsed = c.manufacturing_slots - c.manufacturing_free;
      const rUsed = c.reaction_slots - c.reaction_free;
      const mAss = c.jobs.filter(j => j.activity !== 'reaction').length;
      const rAss = c.jobs.filter(j => j.activity === 'reaction').length;
      return `<div class="ind-do-char">
        <div class="ind-do-hd"><span class="ind-do-who">${c.is_placeholder ? '<span class="pp-char-dummy-badge" title="Placeholder character — not connected to ESI; its slots are the ones you declared">placeholder</span> ' : ''}${_esc(c.character_name)}</span>
          <span class="ind-do-count">start ${c.assigned} job${c.assigned > 1 ? 's' : ''}`
          + (groups.length < c.assigned ? ` · ${groups.length} product${groups.length > 1 ? 's' : ''}` : '')
          + `</span></div>
        <div class="ind-do-slots">
          ${_indSlotRow('Industry', 'mfg', mUsed, c.manufacturing_free, mAss, c.manufacturing_slots)}
          ${_indSlotRow('Reactions', 'rx', rUsed, c.reaction_free, rAss, c.reaction_slots)}
        </div>
        <ul class="ind-do-jobs">${jobs}</ul></div>`;
    }).join('');

    const blocked = (d.unassigned || []).length;
    const wait = blocked
      ? `<div class="ind-do-blocked">${blocked} more job${blocked > 1 ? 's are' : ' is'} ready but every slot is busy — `
        + `they start as jobs finish.</div>` : '';
    const later = d.later_waves
      ? `<div class="pp-sub ind-later">Then ${d.later_waves} more round${d.later_waves > 1 ? 's' : ''} unlock as these finish · about ${_fmtHours(d.makespan_hours)} to the end.</div>` : '';

    el.innerHTML = doers.length
      ? `<h3 class="ind-do-title">Do this now</h3><div class="ind-do-grid">${cards}</div>${wait}${later}`
      : `<h3 class="ind-do-title">Nothing to start yet</h3>${wait}${later}`;
  } catch (e) { el.innerHTML = ''; }
}

// ── Per-order material sourcing ─────────────────────────────────────────────────────────────
// "What have I already got for this build, and what's still to buy." The answer comes mostly from
// the container the build is being gathered into — bind one and the list keeps itself up to date —
// with a hand-entered quantity for everything ESI can't see.
let _indSourcingOpen = null;      // order id, so the panel can be re-rendered after an edit

async function indOpenSourcing(orderId) {
  if (_indSourcingOpen === orderId) { indCloseSourcing(); return; }   // the chip toggles it
  _indSourcingOpen = orderId;
  _indSrcPasteMsg = '';          // last build's paste result is not this one's
  const el = document.getElementById('indSourcing');
  if (el) el.innerHTML = _indLoadingHtml('Working out what this build needs…');
  await indRenderSourcing();
}

function indCloseSourcing() {
  _indSourcingOpen = null;
  const el = document.getElementById('indSourcing');
  if (el) el.innerHTML = '';
}

let _indSourcingData = null;

async function indRenderSourcing() {
  const el = document.getElementById('indSourcing');
  if (!el || _indSourcingOpen === null) return;
  let d;
  try { d = await api(`/api/industry/orders/${_indSourcingOpen}/sourcing`); }
  catch (e) { el.innerHTML = `<p class="pp-warn">${_esc(e.message || 'Could not read this build.')}</p>`; return; }
  _indSourcingData = d;

  _indSourceSets = d.sets || [];
  const multi = _featureActive('industry_plan_sources');
  // One row per bound box, plus a blank row to add the next — grouped by station/structure so two
  // cans with the same name in different stations are distinguishable. Without the flag this is the
  // single dropdown it has always been.
  const bound = multi ? (d.source_keys || []) : [d.source_key || ''];
  const picker = (bound.length ? bound : ['']).map((k, i) => _indSourceRowHtml(
      d.sources || [], k, 'indBindSources()',
      {sets: multi, blank: i === 0 ? '— not tracked in a container —' : '— pick a box —',
       removable: multi && i > 0, onremove: 'indRemoveBoundSource(this)'})).join('')
    + (multi ? `<button type="button" class="ind-src-add" onclick="indAddBoundSource()" `
        + `title="This build is gathered from more than one box — a reaction can and a manufacturing can, say">+ another box</button>`
        + (bound.length > 1 ? ` <button type="button" class="ind-bp-btn ind-copy-sm" onclick="indSaveSourceSet()" `
            + `title="Save these boxes as a named set you can pick in one go next time">Save as set…</button>` : '')
      : '');

  // NO material table here. The shopping list below is already that table, and two lists of the same
  // materials — inevitably showing different quantities, since the queue's list nets off stock and
  // batches shared components across orders — is worse than one. What this panel knows that the
  // shopping list cannot is per-BUILD state: which box this one pulls from and how far along the
  // gathering is. So it shows that, and the shortfall stays one collapsed click away for the moment
  // you want to read it without scrolling.
  const short = (d.items || []).filter(i => !i.done);
  const missing = short.length
    ? `<details class="ind-src-missing"><summary>${short.length} still short</summary>`
      + `<table class="ind-table"><thead><tr><th>Material</th><th class="ind-num">Short</th><th></th></tr></thead><tbody>`
      + short.map(i => `<tr><td>${_esc(i.name)}`
          + (i.sourced > 0 ? ` <span class="ind-src-meta">${Math.round(i.sourced).toLocaleString()} of `
              + `${Math.round(i.required).toLocaleString()}${i.noted > 0 ? ', noted by you' : ' in the box'}</span>` : '')
          + `</td><td class="ind-num">${Math.round(i.remaining).toLocaleString()}</td>`
          // Kept for correcting a single line after a paste; the paste is how the list gets filled.
          + `<td>${i.noted > 0 ? `<button class="ind-srcq-btn" onclick="indSetSourced(${i.type_id}, 0)"`
              + ` title="Forget what was noted for this one">clear</button>` : ''}</td></tr>`).join('')
      + `</tbody></table></details>`
    : `<p class="ind-src-help ind-src-allin">Everything this build needs is accounted for.</p>`;

  // Where each bound box actually is, spelled out under the picker — a closed dropdown shows only
  // the container's name, and the whole point is that its name is not enough to place it.
  const where = (multi && (d.bound || []).length)
    ? `<div class="ind-src-where ind-src-meta">Gathered into ` + d.bound.map(b =>
        `<span class="ind-src-box">${_esc(b.name)}${b.place ? ` <span class="ind-src-place-in">${_esc(b.place)}</span>` : ''}`
        + `${b.missing ? ' <span class="pp-warn">(no longer in your assets)</span>' : ''}</span>`).join(', ')
      + `</div>`
    : '';

  const t = d.totals || {};
  el.innerHTML = `<div class="ind-srcpanel">
      <div class="ind-srcpanel-hd">
        <span class="ind-srcpanel-title">Materials for ${d.quantity}× ${_esc(d.name)}`
        + (d.label ? ` <span class="ind-oc-for">${_esc(d.label)}</span>` : '') + `</span>
        <button class="ind-oc-del" onclick="indCloseSourcing()" title="Close">✕</button>
      </div>
      <div class="ind-srcpanel-bar">
        <label class="ind-src-meta">Pulling from
          <span id="indBoundSrcRows" class="ind-srcrows">${picker}</span></label>
        <span class="ind-shop-tot">${t.sourced} of ${t.materials} sourced`
        + (t.remaining_cost ? ` · ${fmtIsk(t.remaining_cost)} still to buy` : '') + `</span>
        <button class="ind-copy-btn ind-copy-sm" onclick="indCopyMissing()">Copy what's missing</button>
        <button class="ind-bp-btn" onclick="indOpenSourcePaste()">Paste what you've got</button>
      </div>
      ${where}
      <p class="ind-src-help">How far along the gathering is for this one build. Anything in the
        containers you pick counts automatically — rescan your assets after hauling and this moves on
        its own; for stock we can't see, paste it from the EVE client. ${multi
          ? `Those boxes are <b>this build's</b> stock: the plan below counts them and no others, so
             another build can only spend them if you pick them for it too.`
          : `Picking a container also lets the planner spend it, so the shopping list below stops
             asking you to buy what's already in there (untick it under Setup → stock if you'd
             rather it didn't).`}</p>
      <div id="indSrcPaste" class="ind-paste" style="display:none">
        <p class="ind-src-help">Select the materials in your hangar or container (Ctrl+A), copy
          (Ctrl+C) and paste below. This <b>replaces</b> what you've noted so far — it's a snapshot of
          what you have now — and anything this build doesn't need is ignored.</p>
        <textarea id="indSrcPasteText" rows="6" placeholder="Tritanium&#9;1 000 000&#10;Morphite&#9;2 400"></textarea>
        <div class="ind-src-actions">
          <button class="ind-primary-btn" onclick="indApplySourcePaste()">Apply</button>
          <button class="ind-bp-btn" onclick="indCloseSourcePaste()">Cancel</button>
          <span id="indSrcPasteMsg" class="ind-src-meta">${_esc(_indSrcPasteMsg)}</span>
        </div>
      </div>
      ${missing}
    </div>`;
}

// Kept outside the panel's HTML because applying a paste re-renders the whole thing — the result of
// what you just did must not be wiped by the redraw that shows it.
let _indSrcPasteMsg = '';

function indOpenSourcePaste() {
  const f = document.getElementById('indSrcPaste');
  if (!f) return;
  f.style.display = '';
  const t = document.getElementById('indSrcPasteText');
  if (t) t.focus();
}

function indCloseSourcePaste() {
  _indSrcPasteMsg = '';
  const f = document.getElementById('indSrcPaste');
  if (f) f.style.display = 'none';
}

async function indApplySourcePaste() {
  const text = (document.getElementById('indSrcPasteText') || {}).value || '';
  const msg = document.getElementById('indSrcPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  let d;
  try {
    d = await apiSend('POST', `/api/industry/orders/${_indSourcingOpen}/sourcing/paste`, { text });
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  const p = d.paste || {};
  // Say what was ignored as well as what matched: a paste that matched nothing is almost always the
  // wrong hangar, and silence would leave the user staring at an unchanged list.
  _indSrcPasteMsg = p.error === 'empty'
    ? "Couldn't read that paste."
    : `Matched ${p.matched} of this build's materials`
      + (p.ignored ? ` · ignored ${p.ignored} item${p.ignored === 1 ? '' : 's'} it doesn't need` : '')
      + ((p.unknown || []).length ? ` · ${p.unknown.length} name(s) not recognised` : '') + '.';
  await indRenderSourcing();
  if (p.matched) indOpenSourcePaste();     // leave it open so the outcome is visible next to the list
}

function indAddBoundSource() {
  const rows = document.getElementById('indBoundSrcRows');
  const btn = rows && rows.querySelector('.ind-src-add');
  if (!btn) return;
  btn.insertAdjacentHTML('beforebegin', _indSourceRowHtml(
    ((_indSourcingData || {}).sources) || [], '', 'indBindSources()',
    {sets: true, removable: true, onremove: 'indRemoveBoundSource(this)', blank: '— pick a box —'}));
}

async function indRemoveBoundSource(btn) {
  const row = btn && btn.closest('.ind-srcrow');
  if (row) row.remove();
  await indBindSources();
}

// Save the whole picked set, not one box. Sent as `source_keys`, which is also what tells the
// server this plan owns its stock from now on — so what the checklist measures and what the plan is
// allowed to count can never drift apart.
async function indBindSources() {
  const keys = _indExpandSets(_indPickedSources('indBoundSrcRows'),
                              _indSourceValues('indBoundSrcRows'));
  const body = _featureActive('industry_plan_sources')
    ? { source_keys: keys } : { source_key: keys[0] || '' };
  try { await apiSend('PATCH', `/api/industry/orders/${_indSourcingOpen}`, body); }
  catch (e) { toastError(e, 'Could not save'); return; }
  await indRenderSourcing();
  // The bound set is this build's stock, so the queue plan and the shopping list below are now out
  // of date by exactly the contents of those boxes.
  if (keys.length) indRefreshStatus();
}

async function indSaveSourceSet() {
  const keys = _indExpandSets(_indPickedSources('indBoundSrcRows'),
                              _indSourceValues('indBoundSrcRows'));
  if (!keys.length) return;
  const name = window.prompt('Name this set of containers — e.g. "Reaction stock"');
  if (!name || !name.trim()) return;
  try { await apiSend('POST', '/api/industry/source-sets', { name: name.trim(), keys }); }
  catch (e) { toastError(e, 'Could not save the set'); return; }
  await indRenderSourcing();
}

async function indSetSourced(typeId, qty) {
  try {
    await apiSend('POST', `/api/industry/orders/${_indSourcingOpen}/sourcing`,
                  { type_id: typeId, qty });
  } catch (e) { toastError(e, 'Could not save'); return; }
  indRenderSourcing();
}

// The shortfall in EVE Multibuy format — the actual point of the checklist is walking to the market
// with what's left, not admiring what you already have.
function indCopyMissing() {
  const items = ((_indSourcingData || {}).items || []).filter(i => i.remaining > 0);
  if (!items.length) return;
  _indCopyText(items.map(i => `${i.name}\t${Math.ceil(i.remaining)}`).join('\n'));
}

function _indCopyText(text) {
  // `window.event` explicitly, not the bare implicit global: it is deprecated, and reading it as a
  // free variable is exactly the shape of reference `no-undef` exists to catch (see
  // scripts/lint_js.mjs). Behaviour is identical — the callers are inline onclick handlers.
  const btn = (window.event && window.event.target) || null;
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => { btn.textContent = t; }, 1500); } };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => {});
  else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} document.body.removeChild(ta); }
}

async function indLoadRunning() {
  const el = document.getElementById('indRunning');
  if (!el) return;
  try {
    const d = await api('/api/industry/jobs');
    if (!d.jobs || !d.jobs.length) { el.innerHTML = ''; return; }
    const rows = d.jobs.map(j => {
      const ends = j.end_date ? new Date(j.end_date) : null;
      const left = ends ? Math.max(0, (ends - Date.now()) / 3.6e6) : null;
      return `<div class="ind-install-row"><span class="ind-tree-name">${_esc(j.name)}</span> `
        + `<span class="ind-tree-qty">×${j.runs}</span> `
        + `<span class="ind-run-char">${_esc(j.character_name)}</span> `
        + `<span class="ind-tree-cost">${left != null ? (left > 0 ? _fmtHours(left) + ' left' : 'ready') : _esc(j.status)}</span></div>`;
    }).join('');
    el.innerHTML = `<h3 class="ind-install-title">In progress — ${d.jobs.length} job(s)</h3>${rows}`;
  } catch (e) { el.innerHTML = ''; }
}
