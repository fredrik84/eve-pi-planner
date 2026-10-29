// ── Industry — blueprints: read from ESI, or declared by hand. ──────────────────────────────
// ME/TE and ownership auto-read off the character's blueprints, plus the hand-declared list
// behind industry_manual_blueprints (paste preview/import, batches, per-blueprint rows).

// ── Blueprint auto-read (ME/TE + ownership from ESI) ────────────────────────────────────────
async function indLoadBlueprints() {
  const el = document.getElementById('indBlueprints');
  if (!el) return;
  try {
    const d = await api('/api/industry/blueprints');
    if (d.connected) {
      el.innerHTML = `<span class="ind-bp-ok">✓ ${d.owned_count} blueprint${d.owned_count === 1 ? '' : 's'} and reaction formula${d.owned_count === 1 ? '' : 's'} detected — using your real ME/TE</span>`
        + `<button class="ind-bp-btn" onclick="indRefreshBlueprints()">Refresh</button>`;
    } else {
      el.innerHTML = `<span class="ind-bp-hint">Connect a character to auto-read the blueprints and reaction formulas it owns — their ME/TE and which BPOs you hold, with no manual entry.</span>`
        + `<button class="ind-bp-btn ind-bp-connect" onclick="indConnectBlueprints()">Connect blueprints</button>`;
    }
  } catch (e) { el.innerHTML = ''; }
}

function indConnectBlueprints() {
  indEsiConnect(indRefreshBlueprints);
}

async function indRefreshBlueprints() {
  const el = document.getElementById('indBlueprints');
  if (el) el.innerHTML = '<span class="ind-bp-hint">Reading blueprints…</span>';
  try { await apiSend('POST', '/api/industry/blueprints/refresh'); } catch (e) {}
  indLoadBlueprints();
  indLoadSetupSummary();   // this panel now lives in Settings, so the tab's own summary needs telling
}

// ── Blueprints declared by hand — Settings → Blueprints & formulas ──────────────────────────
// The ESI panel above reads PERSONAL blueprints only, so a print in a corp or shared hangar can be
// stated here and nowhere else. Same section on purpose: "what prints do I own" is one question.
// A declaration REPLACES what ESI read for its product (see owned_blueprints' merge rule) — the
// hint in index.html says so, because a user who declares one of three copies has just told the
// plan they hold one.
let _indManualPick = null;      // {type_id, name} chosen in the add row

async function indLoadManualBps() {
  const sec = document.getElementById('indManualBpSubsec');
  const el = document.getElementById('indManualBps');
  if (!el || !sec) return;
  if (!_featureActive('industry_manual_blueprints')) { sec.style.display = 'none'; return; }
  sec.style.display = '';
  let d = null;
  try { d = await api('/api/industry/manual-blueprints'); } catch (e) { el.innerHTML = ''; return; }
  const rows = (d.entries || []).filter(e => !e.batch).map(e => {
    const runs = e.kind === 'bpo' ? 'original (BPO)' : `${e.runs} run${e.runs === 1 ? '' : 's'}`;
    const pref = e.prefer === 'bpo' ? ' · use the original'
      : e.prefer === 'bpc' ? ' · use the copies' : '';
    // A quantity-0 row declares no print at all — it only carries the BPO-vs-BPC choice, which is
    // how someone whose prints ESI CAN see states a preference without retyping the holding.
    const qty = e.quantity > 0 ? `${e.quantity}× ` : 'preference only · ';
    return `<div class="ind-src-row"><span class="ind-src-name">${_esc(e.name)}</span>`
      + `<span class="ind-src-meta">${qty}ME ${e.me} · TE ${e.te} · ${runs}${_esc(pref)}</span>`
      + `<button class="ind-src-del" title="Remove this declaration"`
      + ` onclick="indDeleteManualBp(${e.id})">✕</button></div>`;
  }).join('');
  // Pasted batches have one canonical home in the print-library paste panel. This advanced area
  // is intentionally only for precise, one-product overrides; showing the batches and a second
  // paste box here made one source look like two unrelated stores.
  el.innerHTML = `<div class="ind-src-list">${rows || '<span class="ind-bp-hint">Nothing declared yet.</span>'}</div>`
    + `<div class="ind-manual-bp-add">`
    + `<input id="indManualBpSearch" class="bug-input" placeholder="Product (e.g. Nitrogen Fuel Block)"`
    + ` autocomplete="off" oninput="indManualBpSearch(this.value)">`
    + `<div id="indManualBpResults" class="ind-search-results" style="display:none"></div>`
    + `<label>ME<input id="indManualBpMe" type="number" min="0" max="10" value="0" class="bug-input dummy-num"></label>`
    + `<label>TE<input id="indManualBpTe" type="number" min="0" max="20" value="0" class="bug-input dummy-num"></label>`
    + `<label title="Blank = an original (BPO), which covers any batch and is never consumed">Runs`
    + `<input id="indManualBpRuns" type="number" min="0" placeholder="BPO" class="bug-input dummy-num"></label>`
    + `<label title="How many separate prints you hold. 0 declares no print and only sets the BPO/BPC choice below.">Qty`
    + `<input id="indManualBpQty" type="number" min="0" value="1" class="bug-input dummy-num"></label>`
    + `<label title="When you hold both an original and copies of this product, which should the plan spend? The original costs no copies but runs one job at a time; copies run side by side but are consumed.">Prefer`
    + `<select id="indManualBpPrefer" class="bug-input">`
    + `<option value="">no preference</option><option value="bpo">the original</option>`
    + `<option value="bpc">the copies</option></select></label>`
    + `<button onclick="indSaveManualBp(this)">Add</button>`
    + `<span id="indManualBpMsg" class="bug-status-msg"></span></div>`;
  _indManualPick = null;
  _indBpBatchEntries = {};
  for (const e of d.entries || []) {
    if (e.batch) (_indBpBatchEntries[e.batch] = _indBpBatchEntries[e.batch] || []).push(e);
  }
}

// The batch's own prints, already in the payload — no second round trip, and no way for the list
// and the contents to disagree about what was imported.
let _indBpBatchEntries = {};

function indToggleBatchItems(batch) {
  const el = document.getElementById('indbpb-' + _srcDomId(batch));
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = '';
  const entries = (_indBpBatchEntries[batch] || []).slice()
    .sort((a, b) => a.name.localeCompare(b.name));
  if (!entries.length) { el.innerHTML = '<span class="ind-bp-hint">Nothing in it.</span>'; return; }
  // Formulas first and marked: a reaction library is what most of these pastes are for, and one
  // of them being absent is the thing you opened this to check.
  const isFx = e => /Reaction Formula/i.test(e.name || '');
  const fx = entries.filter(isFx);
  const rest = entries.filter(e => !isFx(e));
  const row = e => {
    const runs = e.kind === 'bpo' ? 'BPO' : `${e.runs} run${e.runs === 1 ? '' : 's'}`;
    const me = (e.me || e.te) ? ` · ME ${e.me}/TE ${e.te}` : '';
    return `<div class="ind-src-item${isFx(e) ? ' ind-src-item-fx' : ''}">`
      + `<span>${e.quantity > 1 ? `${e.quantity}× ` : ''}${_esc(e.name)}</span>`
      + `<span>${_esc(runs)}${_esc(me)}</span></div>`;
  };
  el.innerHTML = (fx.length
      ? `<div class="ind-src-item-hd">${fx.length} reaction formula${fx.length === 1 ? '' : 's'}</div>`
        + fx.map(row).join('') : '')
    + (rest.length
      ? `<div class="ind-src-item-hd">${rest.length} other blueprint${rest.length === 1 ? '' : 's'}</div>`
        + rest.map(row).join('') : '');
}

async function indManualBpSearch(q) {
  const box = document.getElementById('indManualBpResults');
  if (!box) return;
  _indManualPick = null;
  if (!q || q.trim().length < 2) { box.style.display = 'none'; return; }
  try {
    const d = await api('/api/industry/search?q=' + encodeURIComponent(q.trim()));
    box.innerHTML = _indSearchRowsHtml(d.results, 'indManualBpPick');
    box.style.display = '';
  } catch (e) { box.style.display = 'none'; }
}

function indManualBpPick(typeId, name) {
  _indManualPick = { type_id: typeId, name: name };
  const inp = document.getElementById('indManualBpSearch');
  if (inp) inp.value = name;
  const box = document.getElementById('indManualBpResults');
  if (box) box.style.display = 'none';
}

async function indSaveManualBp(btn) {
  const msg = document.getElementById('indManualBpMsg');
  if (!_indManualPick) { if (msg) msg.textContent = 'Pick a product first'; return; }
  const num = (id, dflt) => {
    const v = document.getElementById(id);
    const n = v ? parseInt(v.value, 10) : NaN;
    return isNaN(n) ? dflt : n;
  };
  const runsEl = document.getElementById('indManualBpRuns');
  // Blank runs is the whole encoding for "this is an original" — null on the wire, -1 in the row.
  const runs = (runsEl && runsEl.value.trim() !== '') ? num('indManualBpRuns', 0) : null;
  const prefEl = document.getElementById('indManualBpPrefer');
  if (btn) btn.disabled = true;
  try {
    await apiSend('POST', '/api/industry/manual-blueprints', {
      type_id: _indManualPick.type_id,
      me: num('indManualBpMe', 0), te: num('indManualBpTe', 0),
      runs: runs, quantity: num('indManualBpQty', 1),
      prefer: prefEl ? prefEl.value : '',
    });
  } catch (e) {
    if (msg) msg.textContent = String(e.message || e);
    if (btn) btn.disabled = false;
    return;
  }
  indLoadManualBps();
}

async function indDeleteManualBp(id) {
  try { await apiSend('DELETE', '/api/industry/manual-blueprints/' + id); } catch (e) {}
  indLoadManualBps();
}
