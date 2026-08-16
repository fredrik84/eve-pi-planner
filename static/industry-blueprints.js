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
  // Batch rows are summarised, not listed one by one: a pasted industry window is a hundred-odd
  // prints and a list that long buries the handful someone typed in themselves.
  const batches = (d.batches || []).map(b => {
    // Where the batch's prints are, when we know it: read out of the paste (the long layout names a
    // structure and a container per print) or asked for at import (the short layout names neither).
    // Display only — nothing in planning, routing or the build pins reads it, and the batch is
    // identified by its NAME, so the title is always the name. One batch usually spans several
    // containers, so a place is only named when there is exactly one of them; otherwise the count.
    const where = b.places > 1 ? ` · in ${b.places} places`
      : (b.structure && b.container) ? ` · in ${b.container}, ${b.structure}`
        : (b.container || b.structure) ? ` · in ${b.container || b.structure}` : '';
    // ...and WHAT is in it. The row said how many prints and where they were, which is enough to
    // know a paste landed and not enough to know whether the formula you pasted it for is in there
    // (reported 2026-08-08: "I can see where it's located, but I cannot see what it contains").
    return `<div class="ind-src-row"><span class="ind-src-name">${_esc(b.name)}</span>`
    + `<span class="ind-src-meta">pasted · ${b.prints} print${b.prints === 1 ? '' : 's'}`
    + ` · ${b.products} product${b.products === 1 ? '' : 's'}${_esc(where)}</span>`
    + `<button class="ind-src-peek" title="Show what this batch declares"`
    + ` onclick="indToggleBatchItems('${_esc(b.batch)}')">contents</button>`
    + `<button class="ind-src-del" title="Remove this pasted batch"`
    + ` onclick="indDeleteManualBpBatch('${_esc(b.batch)}')">✕</button></div>`
    + `<div class="ind-src-items" id="indbpb-${_srcDomId(b.batch)}" style="display:none"></div>`;
  }).join('');
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
  const list = batches + rows;
  el.innerHTML = `<div class="ind-src-list">${list || '<span class="ind-bp-hint">Nothing declared yet.</span>'}</div>`
    + _indBpPasteFormHtml()
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
  _indBpFillStructures();       // async, and the form is usable without it — free text still works
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

// Pasting the industry window, which is how a real library arrives — one at a time is unusable at
// ~100 formulas per character. One paste per character, named: re-pasting a name replaces THAT
// batch and leaves the others alone (same model as a pasted stock source).
function _indBpPasteFormHtml() {
  return `<div class="ind-paste">
    <p class="ind-src-help">Or paste a whole industry window: in game open <b>Industry →
      Blueprints</b>, <b>Ctrl+A</b>, <b>Ctrl+C</b>, paste here. One paste is one batch — re-pasting
      the same name replaces it.
      <a href="#" onclick="event.preventDefault();indTogglePasteHelp(this)">Details</a></p>
    <div class="ind-paste-help" style="display:none">
      <p class="ind-src-help">Copy it with <b>nothing selected in the tree</b> and every line carries
      the structure and container it sits in — we record that and offer it as the batch name.
      Otherwise pick the structure below; it only labels the batch.</p>
      <p class="ind-src-help">A pasted product's declaration is used <b>instead of</b> what ESI read
      for it, on every character — so paste each character's window, not just one.</p>
    </div>
    <div class="ind-src-actions">
      <label>Where are these?
        <select id="indBpPasteStruct" class="bug-input" onchange="indBpPasteStructChanged()">
          <option value="">— the paste says, or just name it below —</option>
        </select></label>
      <input type="text" id="indBpPasteStructOther" class="bug-input" style="display:none"
             placeholder="Structure name" autocomplete="off">
    </div>
    <input type="text" id="indBpPasteName" placeholder="Name this batch — e.g. Main's industry window">
    <textarea id="indBpPasteText" rows="6" placeholder="Formulas:&#10;4 x Nanotransistors Reaction Formula&#9;0&#9;0&#9;-1&#9;Composite&#10;Amarr Shuttle Blueprint&#9;10&#9;20&#9;-1&#9;Shuttle"></textarea>
    <div class="ind-src-actions">
      <button class="ind-bp-btn" onclick="indPreviewManualBpPaste()">Preview</button>
      <button class="ind-primary-btn" onclick="indImportManualBpPaste()">Import</button>
      <span id="indBpPasteMsg" class="ind-src-meta"></span>
    </div>
  </div>`;
}

// The structure picker's options are the account's own build structures — the same `/api/markets`
// rows `indPopulateFacility` builds the Industry Facility dropdown from, so there is one list of
// "your structures" and not two that can disagree. Plus free text, because prints live in plenty of
// places that are not a build structure, and NOTHING, because typing a batch name still works and
// this must not become a step on the way in.
const IND_BP_OTHER = '__other';
async function _indBpFillStructures() {
  const sel = document.getElementById('indBpPasteStruct');
  if (!sel) return;
  let names = [];
  try {
    const d = await api('/api/markets');
    names = (d.markets || [])
      .filter(m => m.kind === 'structure' && (m.build_mfg || m.build_rx))
      .map(m => m.name);
  } catch (e) { names = []; }
  sel.innerHTML = `<option value="">— the paste says, or just name it below —</option>`
    + names.map(n => `<option value="${_esc(n)}">${_esc(n)}</option>`).join('')
    + `<option value="${IND_BP_OTHER}">Somewhere else…</option>`;
}

function indBpPasteStructChanged() {
  const sel = document.getElementById('indBpPasteStruct');
  const other = document.getElementById('indBpPasteStructOther');
  if (!sel || !other) return;
  other.style.display = sel.value === IND_BP_OTHER ? '' : 'none';
  if (sel.value === IND_BP_OTHER) other.focus();
}

// The structure the user picked for a paste that doesn't carry one. Recorded on the rows and, if
// nothing was typed, used to name the batch. '' means they picked nothing, which is the plain
// typed-name path. It never decides what a re-paste replaces — the name does.
function _indBpPasteStructure() {
  const sel = document.getElementById('indBpPasteStruct');
  const v = sel ? sel.value : '';
  if (v !== IND_BP_OTHER) return v || '';
  const other = document.getElementById('indBpPasteStructOther');
  return other ? (other.value || '').trim() : '';
}

// One sentence for both the preview and the result, so what the import reports can never read as a
// different answer from what the preview promised.
function _indBpPasteSummary(d) {
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const bits = [`${plural(d.formulas || 0, 'formula')}, ${plural(d.blueprints || 0, 'blueprint')}`
    + ` — ${plural(d.prints || 0, 'print')} across ${plural(d.products || 0, 'product')}`];
  const un = (d.unknown || []).concat(d.no_product || []);
  if (un.length) {
    bits.push(`${plural(un.length, 'name')} not recognised: ${un.slice(0, 5).join(', ')}`
      + (un.length > 5 ? '…' : ''));
  }
  if ((d.ignored || []).length) bits.push(`${plural(d.ignored.length, 'line')} skipped`);
  // A window copied with nothing selected names where each print is. It is all ONE batch — the
  // places are reported because they are worth seeing, not because they split anything.
  const locs = d.locations || [];
  if (locs.length) {
    bits.push(`in ${plural(locs.length, 'container')}: `
      + locs.slice(0, 4).map(l => l.name).join(', ') + (locs.length > 4 ? '…' : ''));
  }
  return bits.join(' · ') + '.';
}

async function indPreviewManualBpPaste() {
  const text = (document.getElementById('indBpPasteText') || {}).value || '';
  const msg = document.getElementById('indBpPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Reading…';
  try {
    const d = (await apiSend('POST', '/api/industry/manual-blueprints/paste/preview',
      { name: '', text })) || {};
    // If the window said where its prints are, offer that as the batch name — the name is the
    // batch's identity, so putting it in the box (rather than applying it invisibly) is what lets
    // someone KEEP it across a re-paste after moving the prints somewhere else.
    const nameEl = document.getElementById('indBpPasteName');
    if (nameEl && !nameEl.value.trim() && d.suggested_name && (d.locations || []).length) {
      nameEl.value = d.suggested_name;
    }
    if (msg) {
      msg.textContent = (d.prints ? 'Will declare ' : 'Nothing to declare — ') + _indBpPasteSummary(d);
    }
  } catch (e) { if (msg) msg.textContent = String(e.message || e); }
}

async function indImportManualBpPaste() {
  const name = (document.getElementById('indBpPasteName') || {}).value || '';
  const text = (document.getElementById('indBpPasteText') || {}).value || '';
  const msg = document.getElementById('indBpPasteMsg');
  if (!text.trim()) { if (msg) msg.textContent = 'Paste something first.'; return; }
  if (msg) msg.textContent = 'Importing…';
  let d = null;
  try {
    d = (await apiSend('POST', '/api/industry/manual-blueprints/paste',
      { name, text, structure: _indBpPasteStructure() })) || {};
  } catch (e) { if (msg) msg.textContent = String(e.message || e); return; }
  const r = d.imported || {};
  await indLoadManualBps();       // repaints the panel, so the note has to be written after it
  const m2 = document.getElementById('indBpPasteMsg');
  if (m2) {
    m2.textContent = r.error === 'empty' ? 'Nothing readable in that paste.'
      : r.error === 'unrecognized' ? "Couldn't match any blueprint names. " + _indBpPasteSummary(r)
        : `Imported "${r.name}" — ` + _indBpPasteSummary(r);
  }
  indLoadQueue();                 // a declared print moves both the ME/TE and the print cap
}

async function indDeleteManualBpBatch(batch) {
  try {
    await apiSend('DELETE', '/api/industry/manual-blueprints/batches/' + encodeURIComponent(batch));
  } catch (e) {}
  indLoadManualBps();
  indLoadQueue();
}

async function indManualBpSearch(q) {
  const box = document.getElementById('indManualBpResults');
  if (!box) return;
  _indManualPick = null;
  if (!q || q.trim().length < 2) { box.style.display = 'none'; return; }
  try {
    const d = await api('/api/industry/search?q=' + encodeURIComponent(q.trim()));
    if (!d.results || !d.results.length) {
      box.innerHTML = '<div class="ind-search-empty">No buildable match</div>';
      box.style.display = ''; return;
    }
    box.innerHTML = d.results.map(x =>
      `<div class="ind-search-row" onclick="indManualBpPick(${x.type_id}, '${_esc(x.name).replace(/'/g, "\\'")}')">${_esc(x.name)}</div>`
    ).join('');
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
