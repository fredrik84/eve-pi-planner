// ── Industry — step 0: first use, and the stock the plans are allowed to assume. ────────────
// The onboarding wizard, the job-slots modal and its tab summary, ESI/corp assets, pasted
// stock, the named source sets, and the slot pool.

// ── First-run setup ─────────────────────────────────────────────────────────────────────────
// Mirrors the Reactions onboarding gate (_rxApplyGate): a blocking screen with numbered steps and
// a Save & continue, remembered per ACCOUNT so it shows once rather than once per browser.
//
// One rule shapes the whole thing: **every step must be completable right here.** The old gate
// demanded a build structure, which needs structure search, which needs a market character — a
// chain a PI-only player can't finish, on a screen they couldn't get past. So the required step
// (where you build) is a dropdown that already has a valid answer, the rest are optional, and
// Save & continue is never disabled. Nothing here can strand anyone.
let _indOnboarded = true;      // assume yes until the settings load says otherwise — a failed
                               // fetch must not throw a setup screen at an established user
let _indLastSourceKey = '';    // the stock source the previous build was bound to
let _indLastSourceKeys = [];   // …and the whole remembered set, for a build gathered from several
let _indSourceSets = [];       // named, reusable groups of sources ("reaction stock")

function _indRenderWizard(hasStructure) {
  const gate = document.getElementById('indGate');
  const content = document.getElementById('indContent');
  if (!gate || !content) return;
  content.style.display = 'none';
  gate.style.display = '';

  // The same facility options the plan form uses — built by indPopulateFacility, which has already
  // run. Presets are real answers, not placeholders: an NPC station or a T1-rigged structure costs
  // a build correctly, so nobody needs a structure configured to get a true plan.
  const cur = (document.getElementById('indFacility') || {}).value || '';
  const opts = Object.keys(_indFacilityMap).map(k =>
    `<option value="${_esc(k)}"${k === cur ? ' selected' : ''}>`
    + `${_esc(_indFacilityLabel[k] || k)}${k.startsWith('s:') ? ' — your structure' : ''}</option>`).join('');

  gate.innerHTML =
    `<section class="pp-card ind-wizard">`
    + `<div class="pp-card-title">Set up manufacturing`
    + `<span class="pp-card-hint">— one required step, two worth doing. All of it changeable later.</span></div>`
    + `<div class="ind-body">`

    // Step 1 — where you build (required, and already answered)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">1</span>Where you build</div>`
    + `<div class="rx-onboard-step-b">`
    + `<select id="indWizFacility" class="ind-wiz-sel">${opts}</select>`
    + `<div class="pp-card-hint" style="margin-top:6px">A structure's rigs change the materials and`
    + ` time of every job, so this drives every cost and duration. Pick the closest match — or add`
    + ` the structure you really build in for its exact ME &amp; TE.`
    + (hasStructure ? '' : ` <button class="ind-link-btn" onclick="openSettingsModal('markets')">Add my structure</button>`)
    + `</div></div></div>`

    // Step 2 — characters and the slots they contribute (informational, never blocking)
    + `<div class="rx-onboard-step"><div class="rx-onboard-step-h"><span class="rx-onboard-num">2</span>`
    + `Characters &amp; slots<span class="rx-onboard-opt">optional</span></div>`
    + `<div class="rx-onboard-step-b"><div id="indWizSlots"><div class="pp-loading"><span class="pp-spinner"></span> Reading your slots…</div></div>`
    + `<div id="indWizSlotWarn"></div>`
    + `<div class="settings-connect-row" style="margin-top:8px">`
    + `<button class="pp-connect-btn" onclick="indWizConnect()">Connect a character</button>`
    + `<span class="pp-card-hint">Brings its real slots, skills and blueprints. Without one we plan`
    + ` against un-researched blueprints and default skills. ${_connectScopeNote()}</span></div></div></div>`

    // Step 3 — the build system, folded (this is the one people skip, and skipping it is fine)
    + `<div class="rx-onboard-step"><details><summary class="rx-onboard-step-h" style="cursor:pointer">`
    + `<span class="rx-onboard-num">3</span>Build system &amp; fees<span class="rx-onboard-opt">optional</span></summary>`
    + `<div class="rx-onboard-step-b">`
    + `<div class="pp-card-hint" style="margin-bottom:8px">Job installation fees are the system's cost`
    + ` index × the job's value, plus tax. Leave this blank and we count only the 4% SCC surcharge, so`
    + ` fees come out light — everything else in the plan is unaffected.</div>`
    + ((typeof _rxAccountSettingsFormHtml === 'function') ? _rxAccountSettingsFormHtml()
        : `<button class="ind-bp-btn" onclick="openSettingsModal('markets')">Open Structures &amp; Markets</button>`)
    + `</div></details></div>`

    + `<div class="rx-onboard-foot">`
    + `<button class="rx-onboard-connect" onclick="indWizSave()">Save &amp; continue</button>`
    + `<span id="indWizMsg" class="pp-card-hint"></span></div>`
    + `</div></section>`;

  if (typeof _loadRxAccountSettings === 'function') _loadRxAccountSettings();
  _indWizLoadSlots();
}

async function _indWizLoadSlots() {
  const d = await indLoadSlots('indWizSlots');
  const warn = document.getElementById('indWizSlotWarn');
  if (!warn) return;
  // Zero capacity isn't an error and mustn't block — but it does make the plan render a 0h build
  // with an empty "do this now", which looks broken unless it's called out here.
  warn.innerHTML = (d && (d.manufacturing_slots || d.reaction_slots)) ? ''
    : `<p class="pp-warn" style="margin:8px 0 0">No usable job slots yet, so plans will show nothing`
      + ` to start. A character needs Mass Production (or Mass Reactions) trained and its skills`
      + ` connected here. You can carry on and set this up later.</p>`;
}

function indWizConnect() {
  indEsiConnect(() => { _indWizLoadSlots(); indPopulateFacility(); });
}

async function indWizSave() {
  const msg = document.getElementById('indWizMsg');
  const wiz = document.getElementById('indWizFacility');
  const sel = document.getElementById('indFacility');
  // The wizard's dropdown is a second view of the plan form's, so hand the choice over to the
  // control that already owns saving and restoring it rather than inventing a parallel path.
  if (wiz && sel && wiz.value) {
    sel.value = wiz.value;
    try { localStorage.setItem('indFacility', wiz.value); } catch (e) {}
  }
  if (msg) msg.textContent = 'Saving…';
  try {
    await apiSend('PUT', '/api/industry/settings', _indSettingsBody());
    await apiSend('POST', '/api/industry/onboarding/complete');
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  _indOnboarded = true;
  onIndustryTabOpen();     // re-run: set up now, so the gate lifts and the tab loads for real
}

// Lifetime manufacturing ledger tiles — shown ONLY once the account has actually completed a
// manufacturing job (opt-in-by-use), so the stats never clutter the tab for someone who hasn't
// used it. Forward-only turnover + net profit from real completions.
async function indLoadLifetime() {
  const el = document.getElementById('indLifetime');
  if (!el) return;
  try {
    const d = await api('/api/industry/lifetime');
    if (!d.used) { el.innerHTML = ''; return; }
    const since = d.since ? new Date(d.since * 1000).toLocaleDateString() : '';
    el.innerHTML = `<div class="an-stats">`
      + `<div class="an-stat"><div class="an-stat-lbl">Lifetime turnover${since ? ' · since ' + _esc(since) : ''}</div><div class="an-stat-val">${fmtIsk(d.turnover)}</div></div>`
      + `<div class="an-stat an-ok"><div class="an-stat-lbl">Lifetime net profit</div><div class="an-stat-val">${fmtIsk(d.net_profit)}</div></div>`
      + `<div class="an-stat"><div class="an-stat-lbl">Jobs completed</div><div class="an-stat-val">${d.jobs}</div></div>`
      + `</div>`;
  } catch (e) { el.innerHTML = ''; }
}

// ── Job slots (modal) + compact tab summary ─────────────────────────────────────────────────
async function indLoadSetupSummary() {
  const sum = document.getElementById('indSetupSummary');
  const rem = document.getElementById('indConnectReminder');
  let slots = null, bp = null;
  try { slots = await api('/api/industry/slots'); } catch (e) {}
  try { bp = await api('/api/industry/blueprints'); } catch (e) {}
  const txt = (() => {
    const s = slots ? `<b>${slots.manufacturing_free}/${slots.manufacturing_slots}</b> mfg · <b>${slots.reaction_free}/${slots.reaction_slots}</b> rx slots free` : '';
    const b = bp ? (bp.connected ? ` · <span class="ind-bp-ok">${bp.owned_count} blueprints</span>` : '') : '';
    return s + b;
  })();
  if (sum) sum.innerHTML = txt;
  const idle = document.getElementById('indSetupSummaryIdle');
  if (idle) idle.innerHTML = txt;
  if (rem) {
    if (bp && !bp.connected) {
      rem.style.display = '';
      // Points at Settings → Blueprints & formulas, where the connect button now lives — it used to
      // open Setup & slots, which no longer holds the blueprint panel.
      rem.innerHTML = `Using default ME/TE. <button class="ind-link-btn" onclick="openSettingsModal('blueprints')">Connect a character</button> to plan with your real blueprints and reaction formulas.`;
    } else {
      rem.style.display = 'none';
    }
  }
}

// Job slots only. Blueprints, formulas and stock moved to Settings → Blueprints & formulas
// (_loadBlueprintsSettings in planetary.js calls indLoadBlueprints/indLoadAssets on reveal), so
// this no longer fires those two reads — the panels they paint aren't in this modal any more.
function indOpenSetup() {
  document.getElementById('indSetupModal').style.display = '';
  const adm = document.getElementById('indAdminSection');
  // Hidden rather than merely refused: the endpoint is admin-gated anyway, so showing the control
  // to everyone would only offer an action that answers 403.
  if (adm) adm.style.display = (typeof _featuresIsAdmin !== 'undefined' && _featuresIsAdmin) ? '' : 'none';
  indLoadSlots();
}

// Replay the first-run setup screen on your own account. Admin-only, and only useful to an admin:
// everyone who has ever used the tab is marked set-up by the backfill, so there is otherwise no way
// to look at the thing every new user sees first.
async function indResetOnboarding() {
  const msg = document.getElementById('indResetMsg');
  if (msg) msg.textContent = 'Resetting…';
  try { await apiSend('POST', '/api/industry/onboarding/reset'); }
  catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  _indOnboarded = false;
  indCloseSetup();
  onIndustryTabOpen();      // re-runs the gate, which now renders the setup screen
}

// ── Stock on hand (ESI assets) — Settings → Blueprints & formulas ───────────────────────────
// Scanning assets makes plans subtract what you already own, and lets progress report "done"
// without guessing a start date. Opt-in and manual — a full asset list is a heavy ESI call.
// EVE tokens carry only the scopes granted at their last login, so characters connected before the
// assets scope existed hold valid tokens that simply can't read assets. Name them and offer the
// re-auth — one login per character, and the callback stores the new scope set.
function _indReauthHtml(names) {
  if (!names || !names.length) return '';
  return `<div class="ind-reauth"><span class="ind-reauth-txt">`
    + `<b>${names.length} character${names.length === 1 ? '' : 's'}</b> can't be scanned yet: `
    + `${names.map(_esc).join(', ')}. They were connected before asset access was added — `
    + `log each one in once to grant it.</span>`
    + `<button class="ind-bp-btn ind-bp-connect" onclick="indReauthAssets()">Reconnect a character</button></div>`;
}

// The SSO popup, waiting for the login to report back. `then` runs when it does; the listener
// removes itself either way, so repeated connects don't stack up handlers that all re-fire on the
// next login. WHICH scopes are asked for is the caller's decision and travels in the URL — it is
// not a mode this function branches on.
function _indSsoPopup(url, then) {
  const w = window.open(url, 'EVE SSO', 'width=800,height=900');
  window.addEventListener('message', function handler(e) {
    if (e.data === 'esi-done') {
      window.removeEventListener('message', handler);
      if (w && !w.closed) w.close();
      then();
    }
  });
}

// One login for every industry scope: /auth/login?industry=1 requests the unified scope set, so a
// single connect brings the character up to date on assets, blueprints and jobs at once.
function indEsiConnect(then) {
  _indSsoPopup('/auth/login?industry=1', then);
}

function indReauthAssets() {
  indEsiConnect(() => { indLoadAssets(); indLoadSetupSummary(); });
}

// The director connect is its OWN flow, and deliberately not the one every other button uses: it
// asks for corporation-wide read access, which EVE gates behind the Director role and which nobody
// else can use. Only someone who has just clicked "connect a director" should ever be shown those
// lines on the consent screen — which is why it names its own URL rather than taking a flag.
function indConnectDirector() {
  _indSsoPopup('/auth/login?director=1', () => { indLoadAssets(); indLoadSetupSummary(); });
}

async function indLoadAssets() {
  const el = document.getElementById('indAssets');
  if (!el) return;
  try {
    const d = await api('/api/industry/assets');
    if (!d.connected) {
      el.innerHTML = `<span class="ind-bp-hint">No asset inventory has been scanned yet.</span>`
        + _indReauthHtml(d.needs_reauth)
        + `<div class="ind-src-actions">`
        + (d.scannable ? `<button class="ind-bp-btn ind-bp-connect" onclick="indRefreshAssets()">Scan assets</button>` : '')
        + `<button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button>`
        + _indCorpScanBtn(d) + `<span id="indCorpMsg" class="ind-src-meta"></span></div>`
        + _indPasteFormHtml();
      return;
    }
    const when = d.fetched_at ? new Date(d.fetched_at * 1000).toLocaleString() : '';
    // Every source is opt-in: counting a hangar you can't actually draw from would make the
    // planner build too little, which is worse than ignoring stock altogether.
    // Grouped by station/structure, so two identically-named cans in different stations are told
    // apart here as well as in the pickers.
    _indSourceSets = d.sets || [];
    const rows = _indGroupSources(d.sources || []).map(g =>
      `<div class="ind-src-group"><div class="ind-src-place">${_esc(g.label)}</div>`
      + g.items.map(sc => {
        const sub = sc.kind === 'container'
          ? `container${sc.parent ? ' in ' + _esc(sc.parent) : ''}`
          : sc.kind === 'paste' ? 'pasted' : sc.corp ? 'corp hangar' : 'hangar';
        const del = sc.kind === 'paste'
          ? `<button class="ind-src-del" title="Remove this pasted stock" onclick="event.preventDefault();indDeleteSource('${_esc(sc.key)}')">✕</button>` : '';
        // "What's in it" is a question the list could not answer — it said where a paste was and
        // how many item types it held, which is not enough to see whether the formula you pasted
        // it for is actually in there (reported 2026-08-08).
        const eid = 'indsrc-' + _srcDomId(sc.key);
        return `<label class="ind-src-row"><input type="checkbox" ${sc.enabled ? 'checked' : ''} `
          + `onchange="indToggleSource('${_esc(sc.key)}', this.checked)">`
          + `<span class="ind-src-name">${_esc(sc.name)}</span>`
          + `<span class="ind-src-meta">${sub} · ${sc.item_count} item type${sc.item_count === 1 ? '' : 's'}</span>`
          + `<button class="ind-src-peek" title="Show what this holds"`
          + ` onclick="event.preventDefault();indToggleSourceItems('${_esc(sc.key)}')">contents</button>`
          + `${del}</label><div class="ind-src-items" id="${eid}" style="display:none"></div>`;
      }).join('') + `</div>`).join('');
    el.innerHTML = `<div class="ind-src-hd"><span class="ind-bp-ok">✓ ${d.enabled_sources} of ${(d.sources || []).length} `
      + `source${(d.sources || []).length === 1 ? '' : 's'} in use · ${d.distinct_types} item type${d.distinct_types === 1 ? '' : 's'} counted`
      + `${when ? ' · scanned ' + _esc(when) : ''}</span>`
      + `<button class="ind-bp-btn" onclick="indRefreshAssets()">Rescan</button></div>`
      + `<p class="ind-src-help">Select only stock the planners may use.`
      + ` Reaction formulas in selected sources count toward concurrent jobs.`
      + (_featureActive('industry_plan_sources')
          ? ` A build with its own containers picked counts <b>those</b> and ignores this list — this is what everything else falls back on.` : '')
      + `</p>`
      + `<div class="ind-src-list">${rows || '<span class="ind-bp-hint">No usable hangars found.</span>'}</div>`
      + _indReauthHtml(d.needs_reauth)
      + `<div class="ind-src-actions"><button class="ind-bp-btn" onclick="indOpenPaste()">Paste a hangar</button>`
      + _indCorpScanBtn(d) + `<span id="indCorpMsg" class="ind-src-meta"></span></div>`
      + _indPasteFormHtml();
  } catch (e) { el.innerHTML = ''; }
}

// Reading corp hangars over ESI needs the Director role, which most players don't have — so this is
// offered as an extra button rather than folded into the normal rescan, and its failure mode ("not
// a director") is a plain answer rather than an error.
function _indCorpScanBtn(d) {
  if (!_featureActive('industry_corp_assets')) return '';
  if (!d.corp_scannable) {
    return `<button class="ind-bp-btn" onclick="indConnectDirector()" title="Corp hangars need `
      + `corporation-wide read access, which EVE only grants to a Director. Nobody else is asked `
      + `for it — connect the director character here.">Connect a director</button>`;
  }
  return `<button class="ind-bp-btn" onclick="indRefreshCorpAssets()" title="Read the corp hangars `
    + `and containers of any corp you're a director in">Scan corp hangars</button>`;
}

async function indRefreshCorpAssets() {
  const msg = document.getElementById('indCorpMsg');
  if (msg) msg.textContent = 'Reading corp assets…';
  let d = null;
  try { d = await apiSend('POST', '/api/industry/assets/refresh-corp'); }
  catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  // Say which of the two "nothing happened" cases it was: no role is permanent and actionable,
  // an empty corp hangar is not.
  const note = d.scanned
    ? `Read ${d.corporations.join(', ')}.`
    : (d.no_role || []).length
      ? `${d.no_role.join(', ')} ${d.no_role.length === 1 ? 'is' : 'are'} not a Director — EVE only lets `
        + `directors read corp assets. Paste the hangar instead.`
      : 'No corp hangars found.';
  await indLoadAssets();          // repaints the panel, so the note has to be written after it
  indLoadQueue();
  const m2 = document.getElementById('indCorpMsg');
  if (m2) m2.textContent = note;
}

// How corp/shared hangars get in: paste them. ESI can't read a corp hangar for ordinary members,
// so pasting is the path that works for everyone, not a fallback.
// It is also the ONLY way to declare a reaction formula held outside a character's personal
// hangar — /characters/{id}/blueprints/ never returns those — and the copy used to say "materials"
// throughout, so nobody found it. Formulas are named here on purpose; see test_formula_stock.py.
// Four sentences of justification became one line of instruction — reported 2026-08-08 as
// "extremely long winded". Why a paste is the only way to declare a corp-hangar formula is real,
// but it belongs where someone goes looking for it, not in front of the box every time.
function _indPasteFormHtml() {
  return `<div id="indPasteForm" class="ind-paste" style="display:none">
    <p class="ind-src-help">In game: open the hangar, <b>Ctrl+A</b>, <b>Ctrl+C</b>, paste here.
      Include reaction formulas — a corp or shared hangar can only be declared this way.</p>
    <input type="text" id="indPasteName" placeholder="Name this stock — e.g. Corp hangar: Materials &amp; formulas">
    <textarea id="indPasteText" rows="6" placeholder="Tritanium&#9;1 000 000&#10;Caesarium Cadmide Reaction Formula&#9;1"></textarea>
    <div class="ind-src-actions">
      <button class="ind-primary-btn" onclick="indSavePaste()">Add as stock</button>
      <button class="ind-bp-btn" onclick="indClosePaste()">Cancel</button>
      <span id="indPasteMsg" class="ind-src-meta"></span>
    </div>
  </div>`;
}

// One DOM id per source key: keys carry ':' and '/' (char:123, corp:456, paste:1), neither of
// which can go in an id and be selected back out.
function _srcDomId(key) {
  return String(key).replace(/[^a-zA-Z0-9_-]/g, '_');
}

const _indSrcItems = {};      // key -> rows, so re-opening a source doesn't re-fetch it

async function indToggleSourceItems(key) {
  const el = document.getElementById('indsrc-' + _srcDomId(key));
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = '';
  if (!_indSrcItems[key]) {
    el.innerHTML = '<span class="ind-bp-hint">Reading…</span>';
    try {
      const d = await api('/api/industry/assets/sources/' + encodeURIComponent(key) + '/items');
      _indSrcItems[key] = d.items || [];
    } catch (e) {
      el.innerHTML = `<span class="ind-bp-hint">${_esc(String(e.message || e))}</span>`;
      return;
    }
  }
  const items = _indSrcItems[key];
  if (!items.length) { el.innerHTML = '<span class="ind-bp-hint">Nothing in it.</span>'; return; }
  // Formulas first and marked: they are why most of these pastes exist, and one of them being
  // missing is the thing you came here to check.
  const fx = items.filter(i => i.formula);
  const rest = items.filter(i => !i.formula);
  const row = i => `<div class="ind-src-item${i.formula ? ' ind-src-item-fx' : ''}">`
    + `<span>${_esc(i.name)}</span><span>${Number(i.qty).toLocaleString()}</span></div>`;
  el.innerHTML = (fx.length
      ? `<div class="ind-src-item-hd">${fx.length} reaction formula${fx.length === 1 ? '' : 's'}</div>`
        + fx.map(row).join('') : '')
    + (rest.length
      ? `<div class="ind-src-item-hd">${rest.length} other item type${rest.length === 1 ? '' : 's'}</div>`
        + rest.map(row).join('') : '');
}

// The "why" behind the paste rules is real and belongs where someone goes looking for it, not in
// front of the box every time (reported 2026-08-08: "extremely long winded").
function indTogglePasteHelp(a) {
  const box = a && a.closest('.ind-paste') && a.closest('.ind-paste').querySelector('.ind-paste-help');
  if (!box) return;
  const open = box.style.display === 'none';
  box.style.display = open ? '' : 'none';
  a.textContent = open ? 'Hide details' : 'Details';
}

function indOpenPaste() {
  const f = document.getElementById('indPasteForm');
  if (f) { f.style.display = ''; const t = document.getElementById('indPasteText'); if (t) t.focus(); }
}
function indClosePaste() {
  const f = document.getElementById('indPasteForm');
  if (f) f.style.display = 'none';
}

async function indSavePaste() {
  const name = (document.getElementById('indPasteName') || {}).value || '';
  const text = (document.getElementById('indPasteText') || {}).value || '';
  const msg = document.getElementById('indPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  try {
    const d = (await apiSend('POST', '/api/industry/assets/paste', { name, text })) || {};
    if (d.error) {
      if (msg) msg.textContent = d.error === 'unrecognized' ? "Couldn't match any item names." : 'Nothing readable in that paste.';
      return;
    }
    const skipped = (d.unknown || []).length;
    if (msg) msg.textContent = `Added ${d.added} item type${d.added === 1 ? '' : 's'}${skipped ? ` · ${skipped} name(s) not recognised` : ''}.`;
    indLoadAssets();
    indLoadQueue();
  } catch (e) { if (msg) msg.textContent = String(e); }
}

async function indDeleteSource(key) {
  try { await apiSend('DELETE', '/api/industry/assets/sources/' + encodeURIComponent(key)); } catch (e) {}
  indLoadAssets();
  indLoadQueue();
}

async function indToggleSource(key, on) {
  try {
    await apiSend('POST', '/api/industry/assets/sources', { keys: [key], enabled: !!on });
  } catch (e) {}
  indLoadAssets();
  indLoadQueue();     // stock changes both the plan and the progress numbers
}

async function indRefreshAssets() {
  const el = document.getElementById('indAssets');
  if (el) el.innerHTML = '<span class="ind-bp-hint">Reading assets… (a full asset list can take a moment)</span>';
  let d = null;
  try { d = await apiSend('POST', '/api/industry/assets/refresh'); } catch (e) {}
  if (d && !d.connected && (d.needs_reauth || []).length && el) {
    el.innerHTML = _indReauthHtml(d.needs_reauth) + _indPasteFormHtml();
    return;
  }
  indLoadAssets();
  indLoadQueue();
}

function indCloseSetup() {
  document.getElementById('indSetupModal').style.display = 'none';
  indLoadSetupSummary();   // reflect any changes (e.g. just connected) back on the tab
}

// ── Slot pool ───────────────────────────────────────────────────────────────────────────────
// Mounted in two places — the Job slots modal, and step 2 of the first-run wizard — so the markup lives
// in one function. Returns the loaded pool so a caller can react to it (the wizard warns when the
// account has no usable slots at all).
async function indLoadSlots(target) {
  const el = document.getElementById(target || 'indSlots');
  if (!el) return null;
  try {
    const d = await api('/api/industry/slots');
    el.innerHTML = _indSlotsHtml(d);
    return d;
  } catch (e) { el.innerHTML = ''; return null; }
}

function _indSlotsHtml(d) {
    // A placeholder's slots are DECLARED, not read from ESI — say so wherever they're counted, so
    // a pool total is never mistaken for measured capacity.
    const ph = c => c.is_placeholder ? '<span class="pp-char-dummy-badge">placeholder</span> ' : '';
    const chips = (d.characters || []).map(c =>
      `<span class="ind-slot-chip" title="${_esc(c.character_name)}${c.is_placeholder ? ' — placeholder character; these slots are the ones you declared, not read from ESI' : ''}">${ph(c)}${_esc(c.character_name)}: `
      + (c.manufacturing_slots ? `${c.manufacturing_free}/${c.manufacturing_slots}<span class="ind-slot-sub">mfg</span>` : '<span class="ind-slot-sub">no mfg</span>')
      + ` · `
      + (c.reaction_slots ? `${c.reaction_free}/${c.reaction_slots}<span class="ind-slot-sub">rx</span>` : '<span class="ind-slot-sub">no rx</span>')
      + `</span>`
    ).join('');
    return `<div class="ind-slot-tot"><b>${d.manufacturing_free}/${d.manufacturing_slots}</b> manufacturing · `
      + `<b>${d.reaction_free}/${d.reaction_slots}</b> reaction slots free `
      + `<button class="ind-bp-btn" onclick="indRefreshJobs()" title="Re-read running jobs from ESI">Refresh jobs</button></div>`
      + `<div class="ind-slot-chips">${chips || '<span class="pp-sub">No characters — add one to get real slot counts.</span>'}</div>`
      // Never silently drop capacity: say who was left out and why.
      + ((d.excluded || []).length
        ? `<div class="ind-slot-excl"><b>Not used:</b> ` + d.excluded.map(c =>
            `<span title="${_esc(c.reason)}">${_esc(c.character_name)}</span>`).join(', ')
          + `<div class="ind-slot-excl-why">Characters with no slot skills trained (or no skill data), and `
          + `placeholders with no slots declared, are left out — `
          + `their single free slot would inflate every estimate and send you jobs they can't run.</div></div>`
        : '');
}
