// ── Industry — step 3 and step 8: the queue, and what a customer is shown of it. ────────────
// Adding a plan to the queue, the what-if simulator, the order list and its editing, and the
// per-order share links.

// ── Build queue ─────────────────────────────────────────────────────────────────────────────
async function indAddToQueue() {
  if (!_indPicked) return;
  const qty = Math.max(1, parseInt(document.getElementById('indQty').value) || 1);
  const srcSel = (document.getElementById('indPlanSrc') || {}).value || '';
  const pasted = srcSel === '__paste'
    ? ((document.getElementById('indPlanPasteText') || {}).value || '') : '';
  const keys = _indPlanSourceKeys();
  try {
    const order = await apiSend('POST', '/api/industry/orders',
      { product_type_id: _indPicked.type_id, quantity: qty,
        label: (document.getElementById('indLabel') || {}).value || '',
        // The overrides were decided against THIS product — they ride along, or
        // queueing would silently undo every one of them.
        force_build_ids: _indForceIds(), me_te_overrides: _indMeTeMap(),
        margin_pct: _indMarginPct(),
        // `source_keys` is only sent when per-plan sources are on, and sending it is what tells the
        // server this plan owns its stock. Without the flag the old single field goes up alone and
        // the account-wide behaviour is untouched.
        ...(_featureActive('industry_plan_sources')
              ? { source_keys: keys, output_source_key: _indPlanOutputKey() }
              : { source_key: srcSel === '__paste' ? '' : srcSel }) });
    // A paste is per-order sourcing, not planner stock: it says what's already gathered for THIS
    // build, so it lands on the order's checklist and nowhere else. Best-effort — the order itself
    // is already queued, and failing the whole action over the checklist would be the wrong trade.
    if (pasted.trim() && order && order.id) {
      try { await apiSend('POST', `/api/industry/orders/${order.id}/sourcing/paste`, { text: pasted }); }
      catch (e) {}
    }
    document.getElementById('indResult').innerHTML = '';
    _indLastOutputKey = _indPlanOutputKey();   // a builder running a can per build answers this the same way every time
    _indForcedTypes.clear();       // they live on the order now
    const lb = document.getElementById('indLabel');
    if (lb) lb.value = '';          // don't inherit the last customer on the next order
    const pt = document.getElementById('indPlanPasteText');
    if (pt) pt.value = '';          // and don't carry one build's materials onto the next
    indClosePlanner();
    await indLoadQueue();
    await indRefreshStatus();     // adding re-plans the whole queue together — the reason to queue
  } catch (e) { toastError(e, 'Could not queue'); }
}

// Live queue progress, keyed by type_id — populated by indLoadProgress() and read by the pipeline
// so its cards/stages can show what's actually done rather than just what's planned.
let _indProgress = null;

// Preview mode: null = off, otherwise a 0-100 completion to fabricate. Kept in the Setup modal
// rather than the main flow — it's a way to SEE the live views before you have live data, not a
// setting anyone should leave on. The server writes nothing for it.
let _indSim = null;

function indSimToggle() {
  const on = document.getElementById('indSimOn');
  const sl = document.getElementById('indSim');
  if (sl) sl.disabled = !(on && on.checked);
  _indSim = (on && on.checked) ? parseFloat(sl.value) : null;
  indSimInput();
  indLoadQueue();
  if (_indStatusVisible()) indRefreshStatus();
}
function indSimInput() {
  const sl = document.getElementById('indSim');
  const lbl = document.getElementById('indSimPct');
  if (lbl && sl) lbl.textContent = `${sl.value}% complete`;
}
function indSimChange() {
  const on = document.getElementById('indSimOn');
  const sl = document.getElementById('indSim');
  if (!(on && on.checked)) return;
  _indSim = parseFloat(sl.value);
  indLoadQueue();
  if (_indStatusVisible()) indRefreshStatus();
}

async function indLoadProgress() {
  try {
    _indProgress = await api('/api/industry/progress' + (_indSim === null ? '' : '?simulate=' + _indSim));
    if (_indProgress && _indProgress.empty) _indProgress = null;
  } catch (e) { _indProgress = null; }
  return _indProgress;
}

function _indProgTypeMap() {
  const m = {};
  ((_indProgress && _indProgress.types) || []).forEach(t => { m[t.type_id] = t; });
  return m;
}

// The queue no longer has a card of its own — orders render as chips in the status header, so
// "reload the queue" and "refresh the status" are the same operation.
async function indLoadQueue() { return indRefreshStatus(); }


// ── Per-order overrides + customer share links ───────────────────────────────────────────────
// Take back every "build it anyway" on one order. All of them at once: they were made together in
// one preview, and a per-component undo on a chip in a header row is more UI than the case wants.
async function indClearOrderForced(orderId) {
  try {
    await apiSend('PATCH', `/api/industry/orders/${orderId}`, { force_build_ids: [] });
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();     // the overrides change what gets built, so the whole plan moves
}

// A link the customer can open with no account: what's being built, which stage, how far, when.
// Minting is idempotent server-side, so this is also "show me the link I already made".
async function indShareOrder(orderId) {
  const box = document.getElementById('indShareBox');
  try {
    const d = await apiSend('POST', `/api/industry/orders/${orderId}/share`);
    const url = `${location.origin}/b/${d.share_id}`;
    if (!box) { prompt('Share this with your customer:', url); return; }
    box.style.display = '';
    box.innerHTML = `<span class="ind-share-lbl">Customer link</span>`
      + `<input class="ind-share-url" id="indShareUrl" readonly value="${_esc(url)}" onclick="this.select()">`
      + `<button class="ind-copy-btn" onclick="indCopyShare()">Copy</button>`
      + `<a class="ind-share-open" href="${_esc(url)}" target="_blank" rel="noopener">Open</a>`
      + `<button class="ind-share-revoke" onclick="indRevokeShare(${orderId})" title="Kill this link — the customer's page stops working">Revoke</button>`
      + `<button class="ind-share-x" onclick="document.getElementById('indShareBox').style.display='none'">✕</button>`;
  } catch (e) { toastError(e, 'Could not create the link'); }
}

function indCopyShare() {
  const el = document.getElementById('indShareUrl');
  if (!el) return;
  navigator.clipboard.writeText(el.value).then(() => {
    const b = el.parentElement.querySelector('.ind-copy-btn');
    if (b) { b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1500); }
  }).catch(() => el.select());
}

async function indRevokeShare(orderId) {
  if (!await ppConfirm('Revoke this link? The customer\'s page will stop working immediately.')) return;
  try {
    await apiSend('DELETE', `/api/industry/orders/${orderId}/share`);
  } catch (e) { /* revoking is best-effort from the UI's point of view */ }
  const box = document.getElementById('indShareBox');
  if (box) { box.innerHTML = '<span class="ind-share-lbl">Link revoked.</span>'; setTimeout(() => { box.style.display = 'none'; }, 2000); }
}

// ── Queue order ─────────────────────────────────────────────────────────────────────────────
// Position is not cosmetic: the scheduler ranks by it, so the first order wins a contested slot and
// its finish time is the "first delivery" number.
let _indOrderDraft = [];

function indOpenOrder() {
  const m = document.getElementById('indOrderModal');
  if (!m) return;
  // Start from the order the status view shows — the same sort, so the line the user drags is the
  // line they were just looking at.
  _indOrderDraft = _indOrdersByRank(_indLastPlan && _indLastPlan.targets);
  m.style.display = '';
  _indRenderOrderList();
}

function indCloseOrder() {
  const m = document.getElementById('indOrderModal');
  if (m) m.style.display = 'none';
}

function _indRenderOrderList() {
  const el = document.getElementById('indOrderList');
  if (!el) return;
  const byOrder = {};
  ((_indProgress && _indProgress.orders) || []).forEach(o => { byOrder[o.id] = o; });
  el.innerHTML = _indOrderDraft.map((o, i) => {
    const p = byOrder[o.id] || {};
    return `<div class="ind-ord-row">`
      + `<span class="ind-ord-pos">${i + 1}</span>`
      + `<span class="ind-ord-nm"><b>${o.quantity}×</b> ${_esc(o.name)}`
      + (p.label ? `<span class="ind-oc-for">${_esc(p.label)}</span>` : '') + `</span>`
      + `<button class="ind-ord-btn" title="Move up" ${i === 0 ? 'disabled' : ''} onclick="indMoveOrder(${i}, -1)">▲</button>`
      + `<button class="ind-ord-btn" title="Move down" ${i === _indOrderDraft.length - 1 ? 'disabled' : ''} onclick="indMoveOrder(${i}, 1)">▼</button>`
      + `</div>`;
  }).join('');
}

function indMoveOrder(i, d) {
  const j = i + d;
  if (j < 0 || j >= _indOrderDraft.length) return;
  const a = _indOrderDraft;
  [a[i], a[j]] = [a[j], a[i]];
  _indRenderOrderList();
}

// Group identical products together. They're built as one shared batch regardless, so this is about
// reading the queue, not about changing what gets built.
function indGroupByProduct() {
  const seen = [];
  _indOrderDraft.forEach(o => { if (!seen.includes(o.product_type_id)) seen.push(o.product_type_id); });
  _indOrderDraft.sort((a, b) =>
    seen.indexOf(a.product_type_id) - seen.indexOf(b.product_type_id) || a.id - b.id);
  _indRenderOrderList();
}

async function indSaveOrderOrder() {
  const msg = document.getElementById('indOrderMsg');
  if (msg) msg.textContent = 'Saving…';
  try {
    await apiSend('POST', '/api/industry/orders/reorder', { order: _indOrderDraft.map(o => o.id) });
  } catch (e) { if (msg) msg.textContent = e.message; return; }
  indCloseOrder();
  await indRefreshStatus();     // order changes ranks, ETAs and first delivery
}

// Edit in place rather than in a dialog: it's two fields, and re-planning the whole queue just to
// show an edit form would be a needless several-second wait.
// What ME/TE this order's OWN blueprint is being planned at, and where that came from. An order
// keeps its overrides in `me_te_overrides`; without one the plan resolves owned print → contract
// copy → un-researched, and `_indReqMeTe` carries whatever it landed on.
function _indOrderMeTe(o) {
  const ov = (o.me_te_overrides || {})[String(o.product_type_id)];
  if (ov) return { me: ov[0], te: ov[1], source: 'override', overridden: true };
  const r = _indReqMeTe[o.product_type_id];
  if (r) return { me: r.me, te: r.te, source: r.me_source, overridden: false };
  return { me: 0, te: 0, source: 'default', overridden: false };
}

function indEditOrder(id) {
  const chip = document.getElementById('oc-' + id);
  const o = (_indOrders || []).find(x => x.id === id);
  if (!chip || !o) return;
  const p = ((_indProgress && _indProgress.orders) || []).find(x => x.id === id) || {};
  const mt = _indOrderMeTe(o);
  // A print you own or bought yourself needn't match anything the plan could see. Editing it here
  // covers the top-level blueprint; components keep their own chips on the plan below.
  const meTe = mt.source === 'reaction' ? '' :
      `<span class="ind-oc-mete" title="Blueprint efficiency for ${_esc(o.name)} — ${_esc(_IND_ME_SRC[mt.source] || '')}. `
    + `Set what your own copy actually is.">ME `
    + `<input type="number" min="0" max="10" step="1" class="ind-oc-mt" id="oce-me-${id}" value="${mt.me}" `
    + `onwheel="indWheelStep(event, this)"> TE `
    + `<input type="number" min="0" max="20" step="2" class="ind-oc-mt" id="oce-te-${id}" value="${mt.te}" `
    + `onwheel="indWheelStep(event, this)">`
    + (mt.overridden
        ? ` <button class="ind-mete-clr" title="Back to what the plan works out for itself" `
          + `onclick="indClearOrderMeTe(${id})">reset</button>` : '')
    + `</span>`;
  chip.classList.add('ind-oc-editing');
  chip.innerHTML = `<span class="ind-oc-name">${_esc(o.name)}</span>`
    + `<input type="number" min="1" class="ind-oc-qty" id="oce-qty-${id}" value="${o.quantity}" title="Quantity">`
    + `<input type="text" maxlength="60" class="ind-oc-lbl" id="oce-lbl-${id}" `
    + `value="${_esc(p.label || '')}" placeholder="For — customer, contract…">`
    // The margin this order is quoted at. It's snapshotted per order — the planner's slider sets it
    // for NEW builds only, deliberately, so a quote a customer is holding can't move under them.
    // That leaves editing it here as the only way to change one, so here it is.
    + `<span class="ind-oc-margin"><input type="number" min="0" max="100" step="0.5" class="ind-oc-mrg" `
    + `id="oce-mrg-${id}" value="${o.margin_pct != null ? o.margin_pct : _indMarginPct()}" `
    + `onwheel="indWheelStep(event, this)" `
    + `title="Margin over net cost for this customer's quote — scroll to adjust">%</span>`
    + meTe
    // The per-order exception to the account's reaction rule. It lives here rather than on a panel
    // of its own: it is the same kind of per-order override the ME/TE and margin beside it are, and
    // it is only ever worth showing while you are editing that order.
    + (_featureActive('industry_reaction_policy')
        ? `<label class="ind-oc-rx" title="Make this order's reaction steps yourself, whatever your account rule says">`
          + `<input type="checkbox" id="oce-rx-${id}" ${o.build_reactions ? 'checked' : ''}> reacts</label>` : '')
    + `<button class="ind-oc-ok" onclick="indSaveOrder(${id})">Save</button>`
    + `<button class="ind-oc-cancel" onclick="indRefreshStatus()">Cancel</button>`;
  const q = document.getElementById('oce-lbl-' + id);
  if (q) q.focus();
}

// A number input only responds to the wheel while focused, which nobody discovers, and the margin is
// a nudge-until-it-looks-right number. Swallowing the scroll matters as much as stepping the value:
// without preventDefault the card scrolls out from under the cursor mid-adjustment. Rounding to the
// step keeps 12.5 + 0.5 from landing on 12.999999999999998.
function indWheelStep(ev, el) {
  ev.preventDefault();
  const step = parseFloat(el.step) || 1;
  const min = el.min === '' ? -Infinity : parseFloat(el.min);
  const max = el.max === '' ? Infinity : parseFloat(el.max);
  const cur = parseFloat(el.value);
  const base = isNaN(cur) ? (isFinite(min) ? min : 0) : cur;
  const next = base + (ev.deltaY < 0 ? step : -step);
  el.value = Math.min(max, Math.max(min, Math.round(next / step) * step));
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

// Drop the order's own-blueprint override and let the plan resolve it again. Component overrides
// on the same order are untouched.
async function indClearOrderMeTe(id) {
  const o = (_indOrders || []).find(x => x.id === id);
  if (!o) return;
  const next = { ...(o.me_te_overrides || {}) };
  delete next[String(o.product_type_id)];
  try {
    await apiSend('PATCH', '/api/industry/orders/' + id, { me_te_overrides: next });
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();
}

async function indSaveOrder(id) {
  const qty = parseInt((document.getElementById('oce-qty-' + id) || {}).value, 10);
  const label = (document.getElementById('oce-lbl-' + id) || {}).value || '';
  const body = { label };
  if (!isNaN(qty) && qty >= 1) body.quantity = qty;
  const mrg = parseFloat((document.getElementById('oce-mrg-' + id) || {}).value);
  if (!isNaN(mrg)) body.margin_pct = Math.max(0, Math.min(100, mrg));
  const rx = document.getElementById('oce-rx-' + id);
  if (rx) body.build_reactions = rx.checked;
  // ME/TE goes only when it actually MOVED. The inputs are seeded with whatever the plan resolved,
  // so sending them unconditionally would turn every rename into a permanent override pinning
  // today's guess — and the plan could then never improve on it (e.g. once you own the print).
  const o = (_indOrders || []).find(x => x.id === id);
  const me = parseFloat((document.getElementById('oce-me-' + id) || {}).value);
  const te = parseFloat((document.getElementById('oce-te-' + id) || {}).value);
  if (o && !isNaN(me) && !isNaN(te)) {
    const was = _indOrderMeTe(o);
    const m = Math.max(0, Math.min(10, me)), t = Math.max(0, Math.min(20, te));
    if (m !== was.me || t !== was.te) {
      body.me_te_overrides = { ...(o.me_te_overrides || {}), [String(o.product_type_id)]: [m, t] };
    }
  }
  try {
    await apiSend('PATCH', '/api/industry/orders/' + id, body);
  } catch (e) { toastError(e, 'Could not save'); return; }
  await indRefreshStatus();     // quantity changes the whole plan, so re-plan
}

async function indRemoveOrder(id) {
  try { await apiSend('DELETE', '/api/industry/orders/' + id); } catch (e) {}
  await indLoadQueue();
  await indRefreshStatus();
}
