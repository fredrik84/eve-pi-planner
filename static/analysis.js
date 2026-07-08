// Setup Analysis tab (supply-vs-plan, skill-ROI, reseat/redeploy advice)
// — split out of planetary.js (2026-06-23). Loaded as a separate <script>; all functions are
// global and resolve at call time. Shared state/util (_ppCharsData, _esc, _fmtIsk, _fmtHours,
// _featureActive, etc.) lives in planetary.js, which loads first.
// Move-a-character (user-initiated swap tool, not analysis advice) moved OUT to planetary.js's
// Settings → Characters section 2026-07-02 — see _renderMoveCharacterSection there.

// ── Setup Analysis tab ───────────────────────────────────────────────────────
// Maps what the player's colonies ACTUALLY produce per P1 (units/day from the ESI forward-sim,
// summed across every character & planet) against a saved plan's required P1 (units/day the
// factories eat at full rate). Answers "am I extracting/producing enough to keep this plan's
// factories refilled?" with headline stats + per-P1 bars.
let _analyzeSnaps = [];

// Per-material drill-down: every colony producing a given output, weakest (underperforming) first.
let _anExpanded = new Set();   // material type_ids whose planet breakdown is expanded
function _planetsForMaterial(t) {
  const out = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    if (String(o.type_id) === String(t))
      out.push({ char: ch.name, system: p.system, planet_num: p.planet_num, perDay: o.per_day || 0,
                 p0Hr: p.ext_p0_day ? Math.round(p.ext_p0_day / 24) : null });
  })));
  out.sort((a, b) => a.perDay - b.perDay);   // underperforming (lowest output) first
  return out;
}
function _toggleMatDetail(t) {
  t = String(t);
  if (_anExpanded.has(t)) _anExpanded.delete(t); else _anExpanded.add(t);
  renderAnalysis();
}

// type_id(str) -> {name, perDay, planets} — every colony output rolled up (P1 for refiners, P0 for
// pure extractors), with how many planets contribute (to size "add N planets" suggestions). The
// plan's needs pick which rows are relevant.
function _setupProductionByP1() {
  const out = {};
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    // exportable=false means this output is fully self-consumed by a colocated on-planet factory
    // line (a hybrid colony) — not actually available to the rest of the account, so it must not
    // inflate the pooled supply picture here.
    if (o.exportable === false) return;
    const k = String(o.type_id);
    if (!out[k]) out[k] = { name: o.name, perDay: 0, planets: 0 };
    out[k].perDay += o.per_day || 0;
    out[k].planets += 1;
  })));
  return out;
}

// type_id(str) -> [{cid, char, system, planet_num, perDay}] — every colony that outputs that
// material, so rebalance suggestions can name the exact planet/character to move.
function _setupPlanetsByMaterial() {
  const idx = {};
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => (p.production || []).forEach(o => {
    const k = String(o.type_id);
    (idx[k] = idx[k] || []).push({ cid: ch.character_id, char: ch.name, system: p.system, planet_num: p.planet_num,
                                   perDay: o.per_day || 0, extPerDay: o.ext_per_day || o.full_per_day || o.per_day || 0 });
  })));
  return idx;
}

// Placeability for the destination materials: which Planet-DB planets each character could actually
// colonise for a given P1's P0 (reachable system, not already used). Fetched per plan, then the
// rebalance moves only suggest a redeploy the character can physically make.
let _placements = null;       // {type_id: {p0_name, by_char: {cid: [{system,planet_num,planet_type,richness}]}}}
let _factorySites = null;     // {cid: [{system, planet_num, planet_type}]} — free B/T planets for a new factory
let _placementsKey = null;
async function _ensurePlacements(typeIds) {
  const key = typeIds.slice().sort().join(',');
  if (_placementsKey === key) return;     // already loaded for this plan
  _placementsKey = key;
  try {
    const r = await fetch('/api/analyze-placements', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                       body: JSON.stringify({ type_ids: typeIds.map(Number) }) });
    const j = await r.json();
    _placements = j.placements || {};
    _factorySites = j.factory_sites || {};
  } catch (e) { _placements = {}; _factorySites = {}; }
  renderAnalysis();   // re-render now that feasibility is known
}

function _snapNeedsByP1(snap) {
  const out = {};
  const c = snap && snap.consumption;
  if (Array.isArray(c)) c.forEach(x => { if (x.units_per_day > 0) out[String(x.p1_type_id)] = { name: x.p1_name, perDay: x.units_per_day }; });
  else if (c) Object.keys(c).forEach(t => { if (c[t] > 0) out[t] = { name: 'P1 ' + t, perDay: c[t] }; });
  return out;
}

function _renderAnalyzePlans() {
  const sel = document.getElementById('analyzePlanSelect');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = _analyzeSnaps.length
    ? _analyzeSnaps.map((s, i) => `<option value="${i}">${s.derived ? '◆ ' : ''}${_esc(s.name)}</option>`).join('')
    : '<option value="">— no plans —</option>';
  if (prev !== '' && _analyzeSnaps[prev]) sel.value = prev;
}

// Derive a demand profile per product the player's deployed factories build (server-side from
// the live colony scan), shaped like a saved snapshot so the Analyze comparison works unchanged.
async function _fetchSetupPlans() {
  try {
    const plans = ((await (await fetch('/api/my-setup-plan')).json()).plans) || [];
    return plans.map((p, i) => ({ ...p, id: 'setup:' + i, derived: true, saved: false, hasPayload: false }));
  } catch (e) { return []; }
}

// Paint instantly from cache (colony data + snapshots are usually warm from the Planetary tab),
// then refresh in the background — no need to hit Reload to see anything.
async function onAnalyzeTabOpen() {
  const statusEl = document.getElementById('analyzeStatusContent');
  const el = document.getElementById('analyzeContent');
  // Only this tab's OWN cached data (_analyzeSnaps) means there's something to paint instantly.
  // _ppCharsData is populated globally on page boot regardless of which tab is open, so checking
  // it here suppressed the spinner on a genuine first visit — the header/characters list being
  // warm doesn't mean this tab's own analysis data (snapshots/skill-roi/expansion) has loaded yet.
  // Spinner goes in the status card's own body (matching where it rendered before the multi-card
  // split) rather than the bare #analyzeContent below it — otherwise it floats card-less under the
  // still-visible "Setup vs Plan" title bar, inconsistent with Dashboard's clean loading state.
  if (statusEl && !_analyzeSnaps.length) {
    statusEl.innerHTML = '<div class="pp-loading"><span class="pp-spinner"></span> Loading…</div>';
    if (el) el.innerHTML = '';
  }
  // Fetch characters, snapshots and derived plans in parallel — all independent. loadCharacters()
  // already calls _loadFeatures() internally, so no separate call is needed here.
  const [saved, derived] = await Promise.all([
    _fetchAllSnapshots().catch(() => []),
    _fetchSetupPlans().catch(() => []),
    loadCharacters(),
  ]);
  _analyzeSnaps = [...(derived || []), ...(saved || [])];
  // Skill ROI and expansion depend on _loadFeatures (via _featureActive) — run after phase 1.
  await Promise.all([_fetchSkillRoi(), _fetchExpansion()]);
  _renderAnalyzePlans();
  renderAnalysis();
}

let _skillRoi = null;
async function _fetchSkillRoi() {
  if (!_featureActive('skill_roi')) { _skillRoi = null; return; }
  try { _skillRoi = await (await fetch('/api/skill-roi')).json(); }
  catch (e) { _skillRoi = null; }
}

// "Train these skills for more output" — forward-looking ROI section for the Setup Analysis tab.
function _renderSkillRoiSection() {
  if (!_featureActive('skill_roi') || !_skillRoi) return '';
  const sugs = _skillRoi.suggestions || [];
  if (!sugs.length) return '';
  const li = sugs.map(s => {
    const gain = [];
    if (s.add_isk_day) gain.push(`+${_fmtIsk(s.add_isk_day)}/day`);
    if (s.add_units_day) gain.push(`+${s.add_units_day.toLocaleString()} ${_esc(s.unit_label || 'units')}/day`);
    return `<li class="an-skill-row">
        <span class="an-skill-main"><b>${_esc(s.char)}</b> · ${_esc(s.skill)} <span class="an-skill-lvl">${s.from_lvl} → ${s.to_lvl}</span></span>
        <span class="an-skill-gain">${gain.join(' · ') || '—'}</span>
        <span class="an-sug-note">${_esc(s.detail)}</span>
      </li>`;
  }).join('');
  const note = _skillRoi.note ? `<div class="an-sug-note">${_esc(_skillRoi.note)}</div>` : '';
  return `<section class="pp-card">
      <div class="pp-card-title">Train skills for more output <span class="tl-preview-tag">estimate</span></div>
      <div class="pp-card-body an-suggest an-suggest-skill">
        <div class="an-sug-note">What an extra skill level on each character would add at your current setup — your call whether the train (or an injector) is worth it.</div>
        <ul class="an-skill-list">${li}</ul>${note}
      </div>
    </section>`;
}

// ── Grow your setup (spare capacity) ──────────────────────────────────────────
// Moved here from the Dashboard (which now shows only the status counts + a link): the concrete
// "deploy this here" cards for free planet slots. Fed by GET /api/expansion.
let _expansion = null;
let _expandDeploysByProduct = {};
let _expandProduct = '';
async function _fetchExpansion() {
  try { _expansion = await (await fetch('/api/expansion')).json(); }
  catch (e) { _expansion = null; }
}
function _renderExpandCards(deploys) {
  if (!deploys || !deploys.length)
    return `<div class="an-sug-note">This product is already balanced for your spare slots — nothing to add right now.</div>`;
  const cards = deploys.map((d, i) => {
    const loc = `${_esc(d.system)}${d.planet_num != null ? ' P' + d.planet_num : ''}`;
    const isFac = d.kind === 'factory';
    const cc = d.planet_type ? `<span class="an-cc-tag">${_esc(d.planet_type)}${isFac ? ' CC' : ''}</span>` : '';
    const mat = isFac
      ? `<b>${_esc(d.p1)}</b> factory`
      : `${_esc(d.p0)} <span class="an-move-p0arrow">→</span> ${_esc(d.p1)}`;
    const dens = (!isFac && d.richness != null) ? ` ${d.richness}% density` : '';
    const meta = isFac
      ? `Supply has room — turns your surplus into <b>~${(d.add_per_day || 0).toLocaleString()} more ${_esc(d.p1)}/day</b>`
      : `${_esc(d.p1)} is only <b>${d.fed_pct}% fed</b> — this colony lifts your bottleneck`;
    let ccuWarn = '';
    if (isFac && d.ccu_low) {
      const lp = d.fit_lp || 0;
      const fits = lp >= 1 ? `fits with only <b>${lp} launchpad${lp !== 1 ? 's' : ''}</b> here (full layout needs 3)` : `won't fit here`;
      const train = d.train_to ? `Train Command Center Upgrades to <b>CCU ${d.train_to}</b> first` : `Train Command Center Upgrades higher first`;
      ccuWarn = `<div class="an-bu-warn">⚠ At ${_esc(d.char)}'s <b>CCU ${d.host_ccu}</b> this ${_esc(d.p1)} factory ${fits} — ${train}, or you'll redeploy it after training.</div>`;
    }
    return `<div class="an-bu-card">
        <div class="an-bu-rank">${i + 1}</div>
        <div class="an-bu-body">
          <div class="an-bu-mat">${mat}</div>
          <div class="an-bu-where">anchor on <b>${_esc(d.char)} · ${loc}</b> ${cc}${dens}</div>
          <div class="an-bu-meta">${meta}</div>
          ${ccuWarn}
        </div>
      </div>`;
  }).join('');
  return `<div class="an-bu-list">${cards}</div>`;
}
function _setExpandProduct(tid) {
  _expandProduct = String(tid);
  const el = document.getElementById('expandDeployCards');
  if (el) el.innerHTML = _renderExpandCards(_expandDeploysByProduct[_expandProduct] || []);
}
function _renderGrowSection() {
  const ex = _expansion;
  if (!ex || !(ex.free_slots > 0)) return '';
  const bits = [];
  const fc = ex.free_slot_chars || [];
  if (fc.length)
    bits.push(`<b>${ex.free_slots} free planet slot${ex.free_slots !== 1 ? 's' : ''}</b> (${fc.slice(0, 4).map(c => `${_esc(c.name)} ${c.used}/${c.max}`).join(', ')}${fc.length > 4 ? ', …' : ''})`);
  if (ex.idle_chars && ex.idle_chars.length)
    bits.push(`<b>${ex.idle_chars.length} idle character${ex.idle_chars.length !== 1 ? 's' : ''}</b>`);
  const status = bits.length ? `<div class="an-sug-note">${bits.join(' · ')}</div>` : '';
  const deploys = ex.deploys || [];
  if (!deploys.length)
    return `<section class="pp-card">
        <div class="pp-card-title">Spare capacity</div>
        <div class="pp-card-body an-suggest an-suggest-free">${status}`
      + `<div class="an-sug-note">Your setup is already balanced for the spare slots — nothing short, and no richer planet free in your systems to deploy on right now.</div></div>
      </section>`;
  _expandDeploysByProduct = ex.deploys_by_product || {};
  const prods = ex.products || [];
  _expandProduct = prods.length ? String(prods[0].type_id) : '';
  const dropdown = prods.length > 1
    ? `<span class="dash-expand-prod-pick">Plan for <select class="dash-expand-prod" onchange="_setExpandProduct(this.value)">`
      + prods.map(p => `<option value="${p.type_id}">${_esc(p.name)} (×${p.count})</option>`).join('') + `</select></span>`
    : '';
  return `<section class="pp-card">
      <div class="pp-card-title">Grow your setup <span class="pp-card-hint">— deploy these on your spare slots, most impactful first</span>${dropdown}</div>
      <div class="pp-card-body an-suggest an-suggest-add">
        ${status}
        <div id="expandDeployCards">${_renderExpandCards(deploys)}</div>
        <div class="an-sug-note">Targets the inputs your factories are short on, so the new colonies actually lift output — no re-plan, no teardown.</div>
      </div>
    </section>`;
}

function renderAnalysis() {
  // Status card (title + plan picker, static in index.html) holds the at-a-glance status badge;
  // everything else renders as its own separate pp-card in the bare #analyzeContent below it —
  // matching the Dashboard's multi-card layout instead of one large card with internal sub-boxes.
  const statusEl = document.getElementById('analyzeStatusContent');
  const el = document.getElementById('analyzeContent');
  if (!statusEl || !el) return;
  if (!_analyzeSnaps.length) {
    statusEl.innerHTML = `<div class="admin-hint">Nothing to compare against yet. Either <b>set a recipe on a factory</b> in-game (then <b>Rescan colonies</b> to get a "Current setup" demand profile), or build a plan in <b>Planetary Planning</b> and <b>Save plan</b>.</div>`;
    el.innerHTML = '';
    return;
  }
  const sel = document.getElementById('analyzePlanSelect');
  const snap = _analyzeSnaps[parseInt(sel && sel.value, 10) || 0];
  if (!snap) { statusEl.innerHTML = ''; el.innerHTML = ''; return; }
  const needs = _snapNeedsByP1(snap);
  const prod = _setupProductionByP1();
  const needKeys = Object.keys(needs);

  if (!Object.keys(prod).length) {
    statusEl.innerHTML = `<div class="admin-hint">No colony production data. Add your characters and refresh in the <b>Characters</b> tab (logged in with ESI access), then reload.</div>`;
    el.innerHTML = '';
    return;
  }
  if (!needKeys.length) {
    statusEl.innerHTML = `<div class="admin-hint">This saved plan predates the per-P1 consumption data. Re-open it in Planetary Planning and <b>Save plan</b> again to enable the analysis.</div>`;
    el.innerHTML = '';
    return;
  }

  // Per-P1 rows for exactly the P1 this plan consumes; worst-fed first.
  const rows = needKeys.map(t => {
    const need = needs[t].perDay;
    const have = (prod[t] && prod[t].perDay) || 0;
    const ratio = need > 0 ? have / need : (have > 0 ? Infinity : 1);
    return { t, name: needs[t].name, have, need, ratio };
  }).sort((a, b) => a.ratio - b.ratio);

  const binding = rows[0];
  const feedPct = Math.round(Math.min(1, binding.ratio) * 100);
  const fed = binding.ratio >= 0.995;
  const rh = snap.factory_refill_hours;
  const refillDays = rh ? rh / 24 : null;

  const stat = (val, lbl, cls) => `<div class="an-stat ${cls || ''}"><div class="an-stat-val">${val}</div><div class="an-stat-lbl">${lbl}</div></div>`;

  // Status badge (check / warning / needs-fixing) instead of a verbose card that duplicates the
  // stats + bars below. Banded like the bars: short (binding under-fed) → fix; fed but the binding's
  // extraction buffer is thin (<10%) → tight warning (it'll dip as heads decay); else healthy.
  const bindSupply = _extSupplyOf(binding.t) || binding.have;
  const bindPct = binding.need > 0 ? Math.round((bindSupply / binding.need - 1) * 100) : 0;
  const status = !fed ? 'bad' : (bindPct < 10 ? 'warn' : 'ok');
  const statusMeta = {
    ok:   { ico: '✓', word: 'Keeping up', sub: `+${bindPct}% extraction buffer on ${_esc(binding.name)}${refillDays ? ` · refill every ${_fmtHours(refillDays * 24)}` : ''}` },
    warn: { ico: '⚠', word: 'Keeping up, but tight', sub: `only +${bindPct}% buffer on ${_esc(binding.name)} — it'll dip short as your heads decay` },
    bad:  { ico: '✕', word: 'Needs fixing', sub: `short on ${_esc(binding.name)} — fed at ${feedPct}%` },
  }[status];
  const head = `<div class="an-status an-st-${status}">
      <span class="an-status-ico">${statusMeta.ico}</span>
      <span class="an-status-txt"><b class="an-status-word">${statusMeta.word}</b><span class="an-status-sub">${statusMeta.sub}</span></span>
    </div>`;

  // Achievable final output is throttled by the scarcest input: the factories can only run at the
  // binding P1's feed ratio, so actual product/day (and its ISK) = target × feed ratio.
  const feedRatio = Math.min(1, binding.ratio);
  const unit = snap.unit_label || 'units';
  let stats = `<div class="an-stats">`;
  stats += stat(feedPct + '%', 'Plan fed at', fed ? 'an-ok' : 'an-bad');
  if (snap.products_per_day) {
    const target = Number(snap.products_per_day), actual = Math.round(target * feedRatio);
    stats += stat(`${actual.toLocaleString()} <span class="an-of">/ ${target.toLocaleString()}</span>`,
                  `${unit}/day · making / target`, fed ? 'an-ok' : 'an-bad');
  }
  if (snap.isk_per_day)
    stats += stat(_fmtIsk(snap.isk_per_day * feedRatio),
                  `ISK/day${fed ? '' : ' · ' + _fmtIsk(snap.isk_per_day) + ' if fed'}`, '');
  // Surplus headroom in END-PRODUCT terms. The extra product the surplus could ACTUALLY become is
  // capped by the LOWEST-overproduced input — you need every input in the right ratio, so total
  // surplus mass badly overstates it (tons of spare Oxidizing is useless if Industrial Fibers has
  // only ~22 SHPC of headroom). So we take the min over inputs of (surplus ÷ per-product need).
  if (snap.products_per_day) {
    let bind = null;
    rows.forEach(r => {
      const perProduct = r.need / snap.products_per_day;   // input units per 1 product
      if (perProduct <= 0) return;
      const hp = (r.have - r.need) / perProduct;           // surplus expressed as products
      if (bind === null || hp < bind.hp) bind = { hp, name: r.name };
    });
    if (bind && bind.hp >= 1) {
      const n = Math.round(bind.hp);
      const iskPerProduct = (snap.isk_per_day || 0) / snap.products_per_day;
      const iskpart = iskPerProduct > 0 ? ` · ≈${_fmtIsk(n * iskPerProduct)} ISK` : '';
      stats += stat(`≈${n.toLocaleString()}`, `more ${_esc(unit)}/day your surplus could feed · capped by ${_esc(bind.name)}${iskpart}`, 'an-warn');
    }
  }
  stats += `</div>`;

  // Refill cadence = factory P1 buffer (3 launchpads = 30,000 m³) ÷ consumption (P1/day × 0.19 m³).
  let proj = '';
  {
    const nfac = snap.factories_count || (snap.factories || []).length || 0;
    const totP1 = Array.isArray(snap.consumption)
      ? snap.consumption.reduce((a, c) => a + (c.units_per_day || 0), 0)
      : Object.values(snap.consumption || {}).reduce((a, v) => a + (v || 0), 0);
    const perFacP1 = nfac ? totP1 / nfac : 0;
    const perFacM3 = perFacP1 * 0.19;
    const days = perFacM3 ? 30000 / perFacM3 : 0;
    if (perFacP1) {
      const cells = [
        stat(_fmtHours(days * 24), 'between refills · 3 LP', 'an-ok'),
        stat(`~${Math.round(perFacM3).toLocaleString()} <span class="an-of">m³/day</span>`, 'of P1 per factory to haul', ''),
        nfac ? stat(String(nfac), 'factories to service', '') : '',
      ].join('');
      proj = `<div class="an-proj">`
        + `<div class="an-proj-h">Refill run</div>`
        + `<div class="an-stats">${cells}</div></div>`;
    }
  }

  const _p0h = p1day => p1day * 150 / 24;     // P1/day → P0/hour (1h cycle, 150 P0 per P1)
  const barRows = rows.map(r => {
    const cls = r.ratio >= 0.995 ? 'an-bar-ok' : (r.ratio >= 0.85 ? 'an-bar-warn' : 'an-bar-bad');
    const haveW = Math.max(2, Math.min(100, (r.have / r.need) * 100));
    // Over/under-production %, measured on the UNCLIPPED extraction (so a factory-limited colony still
    // shows its true head margin, not a flat +0). The % alone is meaningless without "good vs bad", so we
    // band it: 10%+ = comfortable decay cushion (good), 0–10% = tight (hit it, will fade short), <0 = short.
    const extSupply = _extSupplyOf(r.t) || r.have;
    const f = r.need > 0 ? extSupply / r.need - 1 : 0;
    const pct = Math.round(f * 100);
    let band = 'an-ovr-ok', lbl = 'healthy';        // band on the DISPLAYED % so label/colour match it
    if (pct < 0) { band = 'an-ovr-short'; lbl = 'short'; }
    else if (pct < 10) { band = 'an-ovr-tight'; lbl = 'tight'; }
    const ovrStr = `<b>${pct >= 0 ? '+' : ''}${pct}%</b> <span class="an-ovr-lbl">${lbl}</span>`;
    const numTitle = `Your heads extract ${pct >= 0 ? '+' + pct : pct}% vs what the factories consume (≈${Math.round(_p0h(extSupply)).toLocaleString()} of ${Math.round(_p0h(r.need)).toLocaleString()} P0/hr). 10%+ is a comfortable cushion as extraction decays; 0–10% is tight (you hit it, but it'll dip short as the heads fade); below 0 isn't keeping up.`;
    const expanded = _anExpanded.has(String(r.t));
    let detail = '';
    if (expanded) {
      const pls = _planetsForMaterial(r.t);
      const items = pls.length
        ? pls.map(p => `<div class="an-pd-row"><span class="an-pd-char">${_esc(p.char)}</span><span class="an-pd-loc">${p.system ? _esc(p.system) + (p.planet_num != null ? ' P' + p.planet_num : '') : '—'}</span><span class="an-pd-p0">${p.p0Hr != null ? p.p0Hr.toLocaleString() + ' P0/hr' : ''}</span><span class="an-pd-val">${Math.round(p.perDay).toLocaleString()}/day</span></div>`).join('')
        : '<div class="an-pd-empty">No colony is producing this yet.</div>';
      detail = `<div class="an-row-detail">${items}${_fixNudge(r)}</div>`;
    }
    return `<div class="an-mat">
        <div class="an-row an-row-click" onclick="_toggleMatDetail('${r.t}')">
          <div class="an-row-name"><span class="an-row-caret">${expanded ? '▾' : '▸'}</span> ${_esc(r.name)}</div>
          <div class="an-bar-track"><div class="an-bar-fill ${cls}" style="width:${haveW}%"></div></div>
          <div class="an-row-nums ${band}" title="${numTitle}">${ovrStr}</div>
        </div>${detail}
      </div>`;
  }).join('');

  // ── Rebalance moves ─────────────────────────────────────────────────────────
  // You can't change a planet's richness — you only learn it once deployed. What's actionable is
  // moving a surplus colony onto a short material (1 colony slot = 1 move). Size everything in
  // PLANETS off each material's own per-planet output (a P1 not extracted yet falls back to the
  // average of what you do produce).
  const rates = needKeys.map(t => prod[t]).filter(p => p && p.planets > 0).map(p => p.perDay / p.planets);
  const fallbackRate = rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : 7680;
  const perPlanet = t => (prod[t] && prod[t].planets > 0) ? prod[t].perDay / prod[t].planets : fallbackRate;

  const deficits = rows.filter(r => r.ratio < 0.995)
    .map(r => ({ name: r.name, t: r.t, gap: r.need - r.have, per: perPlanet(r.t),
                 planets: Math.max(1, Math.ceil((r.need - r.have) / perPlanet(r.t))) }))
    .sort((a, b) => b.planets - a.planets);
  const surplus = rows.filter(r => r.ratio >= 1.25)
    .map(r => ({ name: r.name, t: r.t, per: perPlanet(r.t), ratio: r.ratio,
                 spare: Math.floor((r.have - r.need) / perPlanet(r.t)) }))
    .filter(s => s.spare >= 1)
    .sort((a, b) => b.ratio - a.ratio);   // most over-produced first → free those, keep scarcer ones

  // Validate placeability: ask the backend which planets each character could actually colonise for
  // the short materials' P0 (reachable system, carries the P0, not already used). Re-renders when ready.
  _ensurePlacements(needKeys);

  // Free colonies from the MOST over-produced material first (surplus is sorted by ratio), and within
  // a material take the weakest colony (keep your best producers). A move is only valid if the freed
  // colony's CHARACTER can place the short material somewhere reachable — so we pair surplus colonies
  // with that char's free destination planets and consume both as we assign.
  const planetIdx = _setupPlanetsByMaterial();
  const spareLeft = {};
  const colsByMat = {};
  surplus.forEach(s => { spareLeft[s.t] = s.spare; colsByMat[s.t] = (planetIdx[s.t] || []).slice().sort((a, b) => a.perDay - b.perDay); });
  const usedKey = new Set();
  const ckey = c => `${c.cid}:${c.system}:${c.planet_num}`;
  // Per-(short material, character) free destination planets, consumed as assigned.
  const destPool = {};
  if (_placements) Object.keys(_placements).forEach(t => {
    destPool[t] = {};
    Object.entries(_placements[t].by_char).forEach(([cid, arr]) => { destPool[t][cid] = arr.slice(); });
  });
  const p0Of = t => (_placements && _placements[t] && _placements[t].p0_name) || null;

  const moves = [];        // {colony, fromName, to, dest|null}
  deficits.forEach(d => {
    let need = d.planets;
    for (const s of surplus) {                               // most over-produced material first
      if (need <= 0) break;
      for (const c of colsByMat[s.t]) {
        if (need <= 0 || spareLeft[s.t] <= 0) break;
        if (usedKey.has(ckey(c))) continue;
        let dest = null;
        if (_placements) {
          const dests = destPool[d.t] && destPool[d.t][String(c.cid)];
          if (!dests || !dests.length) continue;             // this character can't place the short P0
          dest = dests.shift();                              // claim that char's richest free planet
        }
        usedKey.add(ckey(c)); spareLeft[s.t]--; need--;
        moves.push({ colony: c, fromName: s.name, fromT: s.t, to: d.name, toT: d.t, dest });
      }
    }
    d.unmet = need;     // still short: no feasible surplus colony / no reachable free planet
  });
  const newBuilds = deficits.filter(d => d.unmet > 0);
  const leftover = surplus.filter(s => spareLeft[s.t] >= 1).map(s => ({ name: s.name, spare: spareLeft[s.t] }));
  const movedTotal = moves.length;

  // material label as "P0 → P1" (matching the plan), falling back to just P1 if the P0 isn't known.
  const _matHtml = (p1name, t) => {
    const p0 = p0Of(t);
    return p0 ? `${_esc(p0)} <span class="an-move-p0arrow">→</span> ${_esc(p1name)}` : _esc(p1name);
  };
  const _moveSide = (cls, tag, loc, matHtml, note) =>
    `<div class="an-move-side ${cls}"><span class="an-move-tag">${tag}</span>`
    + (loc ? `<span class="an-move-loc">${loc}</span>` : '')
    + `<span class="an-move-mat">${matHtml}</span><span class="an-sug-note">${note}</span></div>`;
  // The move trades capacity between two P1s, so show BOTH sides' standing after it: the surplus
  // material drops (does it stay healthy?) and the short one rises (does it reach the green?). Same
  // banded over/under-% as the bars, before → after, so a redeploy that would break the donor is visible.
  const _ovrBand = pct => pct < 0 ? { c: 'an-ovr-short', l: 'short' } : (pct < 10 ? { c: 'an-ovr-tight', l: 'tight' } : { c: 'an-ovr-ok', l: 'healthy' });
  const _movePct = (supply, need) => need > 0 ? Math.round((supply / need - 1) * 100) : 0;
  const _moveTrans = (before, after) => {
    const b = _ovrBand(after);
    return `<span class="an-move-trans">${before >= 0 ? '+' : ''}${before}% <span class="an-move-tarrow">→</span> <b class="${b.c}">${after >= 0 ? '+' : ''}${after}%</b> <span class="an-ovr-lbl ${b.c}">${b.l}</span></span>`;
  };
  const _moveLi = (m) => {
    const c = m.colony;
    const fromLoc = c.system ? `${_esc(c.system)}${c.planet_num != null ? ' P' + c.planet_num : ''}` : '';
    // Donor P1 loses this colony's extraction; recipient P1 gains a colony's worth on the dest planet.
    const needFrom = (needs[m.fromT] && needs[m.fromT].perDay) || 0;
    const needTo = (needs[m.toT] && needs[m.toT].perDay) || 0;
    const fromExt = _extSupplyOf(m.fromT), toExt = _extSupplyOf(m.toT);
    const colExt = c.extPerDay || c.perDay || 0;
    const gainExt = _estColonyP1(m.toT, m.dest ? m.dest.richness : 0);
    const rmNote = _moveTrans(_movePct(fromExt, needFrom), _movePct(fromExt - colExt, needFrom));
    const addNote = _moveTrans(_movePct(toExt, needTo), _movePct(toExt + gainExt, needTo));
    const rm = _moveSide('an-move-rm', 'tear down', fromLoc, _matHtml(m.fromName, m.fromT), rmNote);
    const add = m.dest
      ? _moveSide('an-move-add', 'build', `${_esc(m.dest.system)} P${m.dest.planet_num} <span class="an-cc-tag">${_esc(m.dest.planet_type)}</span> ${m.dest.richness}%`, _matHtml(m.to, m.toT), addNote)
      : _moveSide('an-move-add', 'build', '', _matHtml(m.to, m.toT), addNote);
    return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div>`
      + `<div class="an-move-pair">${rm}<div class="an-move-arrow">→</div>${add}</div></li>`;
  };

  // Over-producing every input? The surplus could feed MORE factories. To host one, a factory
  // character needs (a) a free Barren/Temperate planet — preferring the system its factories are
  // already in — and (b) a colony slot: a spare one if under max planets, else freed by tearing down
  // an over-produced extractor (which also trims waste). Maxed chars with nothing over-produced to
  // free, or no free B/T planet, can't host one.
  const curFac = snap.factories_count || (snap.factories || []).length || 0;
  // Only recommend factories from surplus BEYOND a comfort buffer (~10%), so we don't push the user
  // to spend the headroom that keeps their current runtime relaxed — and don't start a runaway where
  // each added factory forces a shorter program, which frees more surplus → more factories → …
  const RT_BUFFER = 0.10;
  const extraFac = Math.max(0, Math.round(curFac * (binding.ratio - 1 - RT_BUFFER)));
  const supportable = curFac + extraFac;
  let addFactories = '';
  if (curFac && extraFac >= 1) {
    const facChars = new Set((snap.factories || []).map(f => (f.loc || '').split(' · ')[0].trim()).filter(Boolean));
    const facSystems = new Set((snap.factories || []).map(f => ((f.loc || '').split('·')[1] || '').trim().split(' ')[0]).filter(Boolean));
    const ratioOf = {}; surplus.forEach(s => { ratioOf[String(s.t)] = s.ratio; });
    const surplusTids = new Set(surplus.map(s => String(s.t)));
    const pn = snap.unit_label && snap.unit_label !== 'units' ? _esc(snap.unit_label) : '';
    const facLabel = pn ? `${pn} factory` : 'factory';

    // Per factory character: spare slots, free B/T destinations (factory system first), and the
    // over-produced extractors we could tear down to free a slot (most over-produced first).
    const chars = (_ppCharsData || []).filter(c => facChars.has(c.name) && !c.wallet_only).map(c => {
      const teardowns = [];
      (c.planets || []).forEach(p => {
        if (!p.is_extractor) return;
        (p.production || []).forEach(o => {
          if (surplusTids.has(String(o.type_id)))
            teardowns.push({ system: p.system, planet_num: p.planet_num, ptype: p.planet_type,
                             mat: o.name, t: String(o.type_id), ratio: ratioOf[String(o.type_id)] || 1 });
        });
      });
      teardowns.sort((a, b) => b.ratio - a.ratio);
      const dests = ((_factorySites && _factorySites[String(c.character_id)]) || []).slice()
        .sort((a, b) => (facSystems.has(b.system) ? 1 : 0) - (facSystems.has(a.system) ? 1 : 0));
      return { name: c.name, spare: (c.max_planets || 0) - (c.planets || []).length, dests, teardowns };
    });

    // Place extraFac factories round-robin across characters (spread, less disruption per char), on
    // distinct destination planets, freeing a slot where the char is maxed.
    const usedDest = new Set();
    const cards = [];
    let remaining = extraFac, progress = true;
    while (remaining > 0 && progress) {
      progress = false;
      for (const ch of chars) {
        if (remaining <= 0) break;
        if (ch.spare <= 0 && ch.teardowns.length === 0) continue;   // maxed, nothing to free
        // next free destination not already claimed
        let dest = null;
        while (ch.dests.length) {
          const d = ch.dests[0];
          if (usedDest.has(`${d.system} P${d.planet_num}`)) { ch.dests.shift(); continue; }
          dest = d; break;
        }
        if (!dest) continue;
        ch.dests.shift();
        const teardown = ch.spare > 0 ? (ch.spare--, null) : ch.teardowns.shift();
        usedDest.add(`${dest.system} P${dest.planet_num}`);
        cards.push({ char: ch.name, dest, teardown });
        remaining--; progress = true;
      }
    }

    const cardLi = cards.map(c => {
      const loc = `${_esc(c.dest.system)} P${c.dest.planet_num} <span class="an-cc-tag">${_esc(c.dest.planet_type)}</span>`;
      const note = facSystems.has(c.dest.system) ? '' : 'not in your factory system';
      const build = _moveSide(c.teardown ? 'an-move-add' : 'an-move-add an-move-solo',
                              c.teardown ? 'build factory' : 'build new', loc, facLabel, note);
      if (c.teardown) {
        const t = c.teardown;
        const rm = _moveSide('an-move-rm', 'free a slot', `${_esc(t.system)} P${t.planet_num}`, _matHtml(t.mat, t.t), `${_esc(t.ptype)} extractor`);
        return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div><div class="an-move-pair">${rm}<div class="an-move-arrow">→</div>${build}</div></li>`;
      }
      return `<li class="an-move"><div class="an-move-char">${_esc(c.char)}</div><div class="an-move-pair">${build}</div></li>`;
    });
    if (remaining > 0) cardLi.push(`<li class="an-move-note">+ ${remaining} more couldn't be placed — your factory characters are at max planets with no over-produced colony to free (or no free Barren/Temperate planet). You'd need another character or to free a slot.</li>`);
    addFactories = `<div class="an-suggest an-suggest-add"><div class="an-suggest-h">Add factories — spare extraction could feed ${extraFac} more (${curFac} → ${supportable}), capped by ${_esc(binding.name)}</div><div class="an-sug-note">Keeps a ~10% extraction buffer, so you can stay on your current program length — not chase a shorter one.</div><ul>${cardLi.join('')}</ul></div>`;
  }

  // The rebalance section (specific colony moves) — hidden until the "Redeploy a CC" lever is opened.
  let rebal = '';
  if (moves.length) {
    rebal += `<div class="an-suggest an-suggest-move"><div class="an-suggest-h">Rebalance — redeploy ${movedTotal} colon${movedTotal === 1 ? 'y' : 'ies'}</div><ul>${moves.map(_moveLi).join('')}</ul></div>`;
  }
  if (newBuilds.length) {
    const li = newBuilds.map(d => {
      const p0 = p0Of(d.t);
      const why = _placements ? (p0 ? `no free ${_esc(p0)} planet reachable by a character with a spare colony` : 'no reachable planet free') : 'no surplus to move';
      return `<li><b>${_esc(d.name)}</b> — still short <b>${Math.round(d.unmet * d.per).toLocaleString()}/day</b> (${d.unmet} planet${d.unmet === 1 ? '' : 's'}) <span class="an-sug-note">(${why})</span></li>`;
    }).join('');
    rebal += `<div class="an-suggest an-suggest-fix"><div class="an-suggest-h">Can't rebalance — still short</div><ul>${li}</ul></div>`;
  }
  if (leftover.length && !addFactories) {
    const li = leftover.map(s => `<li><b>${_esc(s.name)}</b> — <b>${s.spare}</b> planet${s.spare === 1 ? '' : 's'} still spare (nothing short needs them)</li>`).join('');
    rebal += `<div class="an-suggest an-suggest-free"><div class="an-suggest-h">Leftover surplus</div><ul>${li}</ul></div>`;
  }
  if (!moves.length && !newBuilds.length && !addFactories)
    rebal = `<div class="an-suggest an-suggest-free"><div class="an-suggest-h">Balanced — every material this plan needs is covered${leftover.length ? ', with a little to spare' : ''}.</div></div>`;

  // Levers head the advice; each reveals its own section. Reseat → yield burn-down, Redeploy → the
  // rebalance moves above. "Add factories" (a distinct surplus opportunity) always shows.
  // If the plan is SHORT and the user hasn't touched the levers yet, open the fix section for them —
  // otherwise the "deploy on a free planet" advice stays hidden behind a collapsed accordion.
  if (!_anLeverTouched && !_anLeverOpen.size && rows.some(r => r.ratio < 0.995)) _anLeverOpen.add('reseat');
  let suggest = _leverCards(binding.ratio, binding.name);
  if (_anLeverOpen.has('reseat')) suggest += _burndownSection(rows);
  if (_anLeverOpen.has('redeploy')) suggest += rebal;
  if (addFactories) suggest += addFactories;

  // Extraction-runtime advice: longest program that keeps extraction above factory demand
  // (+ safety buffer), anchored on the measured headroom and your detected current program.
  _extRt = { ppd: Number(snap.products_per_day) || 0, ipd: Number(snap.isk_per_day) || 0, unit };
  const rtAdvice = _extRuntimeAdviceHtml(binding.ratio, _currentProgramDays());

  statusEl.innerHTML = head + _staleSupplyNote(rows) + stats + proj;

  const supplyCard = `<section class="pp-card">
      <div class="pp-card-title">Material supply</div>
      <div class="pp-card-body">
        <div class="an-legend">Bar = how fed each input is. % = extraction headroom: <span class="an-ovr-ok">+10% or more healthy</span>, <span class="an-ovr-tight">0–10% tight</span> (dips as heads decay), <span class="an-ovr-short">below 0 short</span>. Click a row for the fix.</div>
        <div class="an-bars">${barRows}</div>
      </div>
    </section>`;
  const suggestCard = (suggest + rtAdvice)
    ? `<section class="pp-card">
        <div class="pp-card-title">Suggestions</div>
        <div class="pp-card-body">${suggest}${rtAdvice}</div>
      </section>`
    : '';

  el.innerHTML = supplyCard + suggestCard + _hybridReseatSection() + _renderGrowSection() + _renderSkillRoiSection();
}

// ── Extraction-runtime helper (decay-aware recommendation) ────────────────────
let _extRt = null;   // {ppd, ipd, unit} of the currently-shown plan, for live recompute
// EVE extractors front-load yield and decay over the program (per-cycle ≈ peak/(1+k·t)). The
// AVERAGE over a program of length L hours = peak · ln(1+k·L)/(k·L). k is the decay factor —
// this is an ESTIMATE (validate against your in-game ECU graph); it's one constant to retune.
const _EXT_DECAY_K = 0.012;   // per hour
const _EXT_MAX_DAYS = 14;     // EVE extraction-program cap

function _extEff(days) {       // average yield as a fraction of peak over a `days`-long program
  const h = Math.max(0, days) * 24;
  return h > 0 ? Math.log(1 + _EXT_DECAY_K * h) / (_EXT_DECAY_K * h) : 1;
}

// Whole-day-multiple program duration minus 30 min headroom (so it ends a touch earlier each
// day for a same-time restart). N days → "1d 23h 30m" (N=2), "23h 30m" (N=1).
function _progDuration(days) {
  const n = Math.max(1, Math.min(60, Math.round(days)));
  const total = n * 24 * 60 - 30;
  const d = Math.floor(total / 1440), h = Math.floor((total % 1440) / 60), m = total % 60;
  return [d ? d + 'd' : '', h ? h + 'h' : '', m ? m + 'm' : ''].filter(Boolean).join(' ');
}
// Representative current extraction-program length (median across the deployed extractors),
// from ESI install→expiry. null if no colony data yet.
function _currentProgramDays() {
  const vals = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => {
    if (p.is_extractor && p.program_days > 0) vals.push(p.program_days);
  }));
  if (!vals.length) return null;
  vals.sort((a, b) => a - b);
  return vals[Math.floor(vals.length / 2)];
}

// Solid, low-touch advice: the longest extractor program that keeps extraction above factory
// demand (with a safety buffer). While extraction ≥ demand the factories stay fed, so final
// output holds at the planned rate — a longer program only buys fewer restarts. Past the point
// where the decaying average drops below demand, output starts falling.
//   headroom = supply ÷ demand at the CURRENT program (the analysis "fed at" ratio, uncapped)
//   curDays  = detected current program length (anchor for the decay)
// Line graph of extractor yield decline vs how long heads run before reseating. Two curves —
// raw per-cycle yield (drops fast) and the running average the factories actually see (the pads
// buffer it, so it drops slower) — against the factory-demand line. Where the average crosses
// demand is when, if you don't reseat, the factories start falling behind.
function _extDecayGraphSvg(headroom, L0, safe, edge) {
  const W = 600, H = 190, ml = 40, mr = 14, mt = 14, mb = 26, XMAX = _EXT_MAX_DAYS;
  const x0 = ml, x1 = W - mr, y0 = mt, y1 = H - mb;
  const sx = d => x0 + (d / XMAX) * (x1 - x0);
  const sy = v => y0 + (1 - Math.max(0, Math.min(1, v))) * (y1 - y0);
  const demand = Math.max(0, Math.min(1, _extEff(L0) / headroom));
  const path = fn => { let p = ''; for (let d = 0; d <= XMAX; d += 0.5) p += (d === 0 ? 'M' : 'L') + sx(d).toFixed(1) + ',' + sy(fn(d)).toFixed(1) + ' '; return p.trim(); };
  const grid = [0, 0.5, 1].map(v => `<line x1="${x0}" y1="${sy(v).toFixed(1)}" x2="${x1}" y2="${sy(v).toFixed(1)}" class="g-grid"/><text x="${x0 - 4}" y="${(sy(v) + 3).toFixed(1)}" class="g-ylab">${v * 100 | 0}%</text>`).join('');
  const xlab = [0, 7, 14].map(d => `<text x="${sx(d).toFixed(1)}" y="${H - 8}" class="g-xlab">${d}d</text>`).join('');
  const zone = edge < XMAX ? `<rect x="${sx(edge).toFixed(1)}" y="${y0}" width="${(x1 - sx(edge)).toFixed(1)}" height="${(y1 - y0).toFixed(1)}" class="g-bad"/>` : '';
  const mk = (d, cls, lbl) => (d >= 1 && d <= XMAX) ? `<line x1="${sx(d).toFixed(1)}" y1="${y0}" x2="${sx(d).toFixed(1)}" y2="${y1}" class="${cls}"/><text x="${sx(d).toFixed(1)}" y="${(y1 - 3).toFixed(1)}" class="g-mklab">${lbl}</text>` : '';
  return `<svg class="ext-graph" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      ${zone}${grid}${xlab}
      <line x1="${x0}" y1="${sy(demand).toFixed(1)}" x2="${x1}" y2="${sy(demand).toFixed(1)}" class="g-demand"/>
      <text x="${x1}" y="${(sy(demand) - 3).toFixed(1)}" class="g-dlab" text-anchor="end">factory demand</text>
      <path d="${path(d => 1 / (1 + _EXT_DECAY_K * d * 24))}" class="g-inst"/>
      <path d="${path(d => _extEff(d))}" class="g-avg"/>
      ${mk(L0, 'g-now', 'now')}${mk(safe, 'g-pick', 'pick')}
    </svg>
    <div class="ext-graph-leg"><span><i class="g-k g-k-avg"></i>average fed to factories</span><span><i class="g-k g-k-inst"></i>raw per-cycle yield</span><span><i class="g-k g-k-demand"></i>demand</span></div>`;
}

// The runtime recommendation is built from each colony's LAST-SCANNED head yield, which depends on
// where the heads sit (hotspot density) and how far the current program has decayed — so it shifts
// when you reseat heads or restart programs. This note tells the user the two levers: reseat heads
// (light touch — raises a colony's own peak, best aimed at the binding material) vs. redeploy a
// command center (rebalances a material that stays short while others overflow).
let _anLeverOpen = new Set();   // which advice levers ('reseat'/'redeploy') have their section shown
let _anLeverTouched = false;    // once the user clicks a lever, stop auto-opening one for them
function _toggleLever(k) {
  _anLeverTouched = true;
  const open = _anLeverOpen.has(k);
  _anLeverOpen.clear();          // accordion: only one section open at a time (keeps the page short)
  if (!open) _anLeverOpen.add(k);
  renderAnalysis();
}
// Two levers heading the rebalance advice. Each card is a toggle: Reseat heads → the measured yield
// burn-down (per-colony decline across programs); Redeploy a CC → the specific rebalance moves. Both
// sections are hidden until the matching card is clicked. `headroom`/`bindName` come from the binding
// (worst-fed) P1 so the reseat card can name it when something's short.
function _leverCards(headroom, bindName) {
  const short = headroom < 0.995;
  // No shortage → reseat/redeploy have nothing to act on (reseat only recovers lost yield, redeploy
  // only rebalances a short material). Hide the whole section rather than show empty levers.
  if (!short) return '';
  const reseatTxt = (short && bindName)
    ? `Move <b>${_esc(bindName)}</b>'s heads onto stronger hotspots, then rescan — a fresh program pulls a higher peak.`
    : `Move extractor heads onto stronger hotspots, then rescan — a fresh program pulls a higher peak.`;
  const card = (key, cls, ico, ttl, tag, txt, hint) => {
    const open = _anLeverOpen.has(key);
    return `<div class="an-lever ${cls} an-lever-click${open ? ' an-lever-open' : ''}" onclick="_toggleLever('${key}')">
        <div class="an-lever-top"><span class="an-lever-ico">${ico}</span><span class="an-lever-ttl">${ttl}</span><span class="an-lever-tag">${tag}</span></div>
        <div class="an-lever-txt">${txt}</div>
        <div class="an-lever-cta">${open ? `Hide ${hint} ▴` : `Show ${hint} ▾`}</div>
      </div>`;
  };
  return `<div class="an-suggest an-suggest-levers">
      <div class="an-suggest-h">Lift yields &amp; balance</div>
      <div class="an-levers-lead">Figures track your heads' placement at the last scan and decay as a program runs — so they shift when you reseat or restart. Two levers:</div>
      <div class="an-lever-row">
        ${card('reseat', 'an-lever-a', '⟳', 'Reseat heads', 'low effort', reseatTxt, 'reseat candidates')}
        ${card('redeploy', 'an-lever-b', '⇄', 'Redeploy a CC', 'rebalance', "If a material stays short while others overflow, move a surplus colony's command center onto it.", 'rebalance moves')}
      </div>
    </div>`;
}

// ── Yield burn-down (measured decline across programs) ─────────────────────────
// One sample per extraction program (logged at scan time, deduped by install). The trend is the
// only way to tell "this colony's hotspots are depleting → reseat" from "it's just a thin planet"
// (a thin planet is steady-low; a depleting one trends DOWN). Density-free, fleet-independent.
// Colonies producing P1 `t`, weakest output first, each annotated with its measured decline (from
// yield_history) so we can flag the ones a reseat would actually recover vs ones that are just thin.
function _producersOf(t) {
  const out = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => {
    if (!p.is_extractor) return;
    (p.production || []).forEach(o => {
      if (String(o.type_id) !== String(t)) return;
      // Self-consumed by a colocated on-planet factory (hybrid colony) — not real spare supply
      // for a rebalance suggestion to hand out.
      if (o.exportable === false) return;
      const h = (p.yield_history || []).filter(s => s.peak > 0).map(s => s.peak);
      const cur = h.length ? h[h.length - 1] : null, max = h.length ? Math.max(...h) : null;
      out.push({ char: ch.name, system: p.system, planet_num: p.planet_num, p0: p.p0_name,
                 perDay: o.per_day || 0, full: o.full_per_day || o.per_day || 0,
                 extPerDay: o.ext_per_day || o.full_per_day || o.per_day || 0,
                 capped: !!o.capped, stale: !!o.stale,
                 n: h.length, decline: (max && max > 0) ? (max - cur) / max : 0 });
    });
  }));
  return out.sort((a, b) => a.perDay - b.perDay);   // weakest producer first
}

// Reseat list, scoped to the materials the plan is SHORT on (reseating a surplus P0 is pointless).
// Grouped per short P1, showing the worst few producing colonies — those drag the shortage, so they're
// where a reseat helps most. A measured decline is flagged "off best" (reseat recovers it).
const _RESEAT_PER_P1 = 5;   // worst N colonies shown per short material

// Total UNCLIPPED extraction (P1/day) the heads sustain for material `t`, summed over its colonies.
// rate_sustained clamps each colony at its factory rate, so a satisfied material reads +0 surplus even
// when the heads pull far more — this recovers that hidden headroom (the decay/overshoot buffer).
function _extSupplyOf(t) {
  return _producersOf(t).reduce((s, c) => s + (c.extPerDay || 0), 0);
}

// One-line "how to fix it" nudge for a material's drilldown, shown only when it's short or tight
// (over-production buffer < 10%, the same banding as the bars). Names the single best low-work fix:
// reseat the most-recoverable colony (capped or declined) if reseating would help, else redeploy /
// add a colony. Healthy materials get nothing (no noise).
const _HEALTHY_BUFFER = 0.10;   // target over-production buffer that clears the "tight" band
function _fixNudge(r) {
  const extSupply = _extSupplyOf(r.t) || r.have;
  const pct = r.need > 0 ? Math.round((extSupply / r.need - 1) * 100) : 0;
  if (pct >= _HEALTHY_BUFFER * 100) return '';      // already healthy — nothing to fix
  const band = pct < 0 ? 'short' : 'tight';
  const gap = Math.max(0, r.need * (1 + _HEALTHY_BUFFER) - extSupply);   // P1/day extra to reach +10%
  const landPct = added => r.need > 0 ? Math.round((extSupply + added) / r.need * 100 - 100) : 0;
  const toP0h = p1day => Math.round(p1day * 150 / 24);   // P1/day → P0/hr (the tangible in-game ECU rate)
  const RECLAIM = 0.05;
  const prods = _producersOf(r.t);
  // Reseating recovers a colony's OWN lost yield — back toward the peak it has actually produced —
  // never beyond it, and never to the 48k full-bar baseline (no real planet reaches that). So a
  // reseat fix is only offered for colonies measurably DECLINED from their own best; a colony already
  // at its peak (just a thin planet) isn't a reseat candidate.
  const recoverable = prods.map(c => {
    const rec = (c.n >= 2 && c.decline >= RECLAIM) ? c.perDay * c.decline / (1 - c.decline) : 0;
    return { ...c, rec };
  }).filter(c => c.rec > 0).sort((a, b) => b.rec - a.rec);
  const _loc = c => c.system ? `${_esc(c.char)} · ${_esc(c.system)}${c.planet_num != null ? ' P' + c.planet_num : ''}` : _esc(c.char);
  // One named colony to reseat: where it is, how far off its best, current → target extraction
  // (P0/hr, the in-game ECU rate) and the P1/day it adds (to compare against the page's figures).
  const _reseatLine = c => `<div class="an-pd-fix-col"><span class="an-pd-fix-loc"><b>${_loc(c)}</b> <span class="an-pd-fix-why">${Math.round(c.decline * 100)}% off best</span></span><span class="an-pd-fix-rec">${toP0h(c.perDay).toLocaleString()} → ${toP0h(c.target).toLocaleString()} P0/hr <span class="an-pd-fix-p1">+${Math.round(c.target - c.perDay).toLocaleString()} P1/day</span></span></div>`;

  // Fewest colonies, each lifted only as far as the +10% gap needs — so we never recommend recovering
  // more than the shortfall calls for.
  let remain = gap; const use = [];
  for (const c of recoverable) {
    if (remain <= 0) break;
    const contrib = Math.min(c.rec, remain);
    use.push({ ...c, target: c.perDay + contrib });
    remain -= contrib;
  }
  const covered = gap - remain;
  const totalRec = recoverable.reduce((s, c) => s + c.rec, 0);

  let fix;
  if (remain <= 0 && use.length) {                   // recovering lost yield alone reaches +10% (trimmed)
    fix = `Reseat ${use.length === 1 ? 'this colony' : `these ${use.length} colonies`} onto denser hotspots → recovers lost yield, lifting ${_esc(r.name)} to <b>+${landPct(covered)}%</b> (healthy), no new colony:`
        + `<div class="an-pd-fix-list">${use.map(_reseatLine).join('')}</div>`;
  } else if (totalRec > 0) {                          // recovering everything still falls short of +10%
    const full = recoverable.map(c => ({ ...c, target: c.perDay + c.rec }));
    fix = `Reseat ${full.length === 1 ? 'its declined colony' : `its ${full.length} declined colonies`} (recovers to only +${landPct(totalRec)}%), then <b>redeploy a surplus colony</b> onto ${_esc(r.name)} or <b>add one</b> to clear +10%:`
        + `<div class="an-pd-fix-list">${full.map(_reseatLine).join('')}</div>`;
  } else if (!prods.length) {
    fix = `<b>Add a ${_esc(r.name)} colony</b> — nothing produces it yet.`;
  } else {                                            // colonies at their best — reseating won't lift it
    const colonyEst = _estColonyP1(r.t, 0);
    const land = colonyEst > 0 ? landPct(colonyEst) : null;
    if (band === 'tight' && land != null && land > 25)
      fix = `Only mildly tight, and your colonies are near their best — a whole new colony would overshoot to ~+${land}%. Leave it; reseat when one decays.`;
    else
      fix = `Your colonies are near their best, so reseating won't lift it — <b>redeploy a surplus colony</b> onto ${_esc(r.name)} or <b>add one</b> for a +10% buffer.`;
  }
  return `<div class="an-pd-fix an-pd-fix-${band}">🔧 ${fix}</div>`;
}

// Estimated P1/day a NEW colony for material `t` would add — calibrated to the player's own existing
// colonies (real sustained output), falling back to a richness-scaled baseline (100-richness ≈ 7,680
// P1/day) when they grow none yet. Used to tell whether a whole colony FITS a gap or overshoots it.
function _estColonyP1(t, richness) {
  const prods = _producersOf(t).filter(c => c.perDay > 0);
  if (prods.length) return Math.round(prods.reduce((s, c) => s + c.perDay, 0) / prods.length);
  return Math.round(Math.min(100, richness || 0) / 100 * 7680);
}

// A pre-rate_sustained scan shows a colony's OPTIMISTIC full factory rate instead of its real
// extraction-limited output — so supply can read high until the player refreshes (the Plasmoids
// 96%→83% surprise: a thin/over-large planet reads 100% on a stale scan, then drops once measured).
function _staleSupplyNote(rows) {
  const need = new Set((rows || []).map(r => String(r.t)));
  const stale = new Set();
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => {
    if (!p.is_extractor) return;
    (p.production || []).forEach(o => { if (o.stale && need.has(String(o.type_id))) stale.add(o.name); });
  }));
  if (!stale.size) return '';
  const names = [...stale];
  const list = names.slice(0, 4).map(_esc).join(', ') + (names.length > 4 ? `, +${names.length - 4} more` : '');
  return `<div class="an-stale-note">⚠ <b>Rescan for true numbers.</b> ${names.length} material${names.length > 1 ? 's' : ''} (${list}) ${names.length > 1 ? 'are' : 'is'} fed by colonies last scanned before extraction-limit tracking — their supply may read optimistically high. <b>Rescan colonies</b> in the Characters tab; a thin or over-large planet can show 100% now and drop once its real extraction is measured.</div>`;
}

function _burndownSection(rows) {
  const short = (rows || []).filter(r => r.ratio < 0.995);   // materials below the plan's need
  if (!short.length)
    return `<div class="an-suggest an-suggest-burndown"><div class="an-suggest-h">Reseat candidates</div>`
      + `<div class="an-levers-lead">All materials covered — nothing short to fix. Reseating a depleting colony still extends runtime.</div></div>`;

  const P0_PER_P1 = 150;   // basic-industry ratio: 3000 P0 → 20 P1 per cycle. Heads show P0, so target in P0.
  const toP0h = p1day => Math.round(p1day * P0_PER_P1 / 24);   // P1/day → P0/hour (the ECU's extraction rate)
  const RECLAIM = 0.05;    // ignore sub-5% "decline" as scan noise

  const groups = short.map(r => {
    const prods = _producersOf(r.t);              // weakest first
    const shortBy = Math.max(0, Math.round(r.need - r.have));
    const coveredAbove = '';
    const headH = `${_esc(r.name)} <span class="an-bd-group-sub">${Math.round(r.ratio * 100)}% fed · short ${shortBy.toLocaleString()}/day</span>`;
    if (!prods.length)
      return `<div class="an-bd-group"><div class="an-bd-group-h">${_esc(r.name)} <span class="an-bd-group-sub">short ${shortBy.toLocaleString()}/day · no colony makes it yet</span></div>`
        + `<div class="an-bd-target"><b>Add</b> a ${_esc(r.name)} colony${coveredAbove}.</div></div>`;

    // Two ways reseating recovers a short material WITHOUT a new colony:
    //  • DECLINED — a colony dropped below its own proven best; reseating recovers the lost yield.
    //  • EXTRACTION-CAPPED — a colony never reaches its factory rate (thin/big planet); reseating onto
    //    denser hotspots can lift it toward full rate (an UPPER bound — only if richer spots exist).
    // A colony's full-rate headroom already subsumes any decline, so we never count both.
    const withBest = prods.map(c => {
      let recoverable = 0, reason = null;
      if (c.capped && c.full > c.perDay) { recoverable = c.full - c.perDay; reason = 'capped'; }
      else if (c.n >= 2 && c.decline >= RECLAIM) { recoverable = c.perDay * c.decline / (1 - c.decline); reason = 'declined'; }
      return { ...c, recoverable, reason, best: c.perDay + recoverable };
    });
    const declined = withBest.filter(c => c.recoverable > 0).sort((a, b) => b.recoverable - a.recoverable);
    const reclaimable = Math.round(declined.reduce((s, c) => s + c.recoverable, 0));
    const hasUnknown = withBest.some(c => c.n < 2 && !c.capped);
    const rem = Math.max(0, shortBy - reclaimable);
    const colTail = coveredAbove ? coveredAbove.replace(' (see', ' — see').replace(')', '') : ` <b>redeploy</b> a surplus colony or <b>add</b> a ${_esc(r.name)} colony`;

    // Suggest only the FEWEST colonies whose combined recovery covers the shortfall — and show each
    // colony's target as only AS FAR AS THE GAP NEEDS, not its full ceiling (a 917/day gap must not read
    // "21,819 → 48,000 P0/hr"). declined is sorted strongest-recovery first; the last colony takes just
    // the remainder.
    const covers = reclaimable >= shortBy && declined.length;
    const need = [];
    let remainGap = shortBy;
    for (const c of declined) {
      if (remainGap <= 0) break;
      const contrib = Math.min(c.recoverable, remainGap);
      need.push({ ...c, target: c.perDay + contrib });   // only lift it enough to close the gap
      remainGap -= contrib;
    }
    const showSet = covers ? need : declined.map(c => ({ ...c, target: c.best }));   // can't cover → full best
    const hasCapped = showSet.some(c => c.reason === 'capped');
    const ifRicher = hasCapped ? ' (extraction-capped — only if those planets have richer hotspots; if they\'re just thin, add a second source)' : '';

    const reseatRows = showSet.slice(0, _RESEAT_PER_P1).map(c => {
      const loc = c.system ? `${_esc(c.system)}${c.planet_num != null ? ' P' + c.planet_num : ''}` : '';
      const tag = c.reason === 'capped'
        ? `⚠ extraction-capped — reseat denser hotspots`
        : `▼ ${Math.round(c.decline * 100)}% off best — reseat recovers it`;
      return `<div class="an-bd-prod">
          <span class="an-bd-prod-loc">${_esc(c.char)}${loc ? ' · ' + loc : ''}</span>
          <span class="an-bd-prod-val">${toP0h(c.perDay).toLocaleString()} → ${toP0h(c.target).toLocaleString()}<span class="an-bd-unit"> P0/hr</span></span>
          <span class="an-bd-prod-tag an-bd-down">${tag}</span>
        </div>`;
    }).join('');

    let action, list = '';
    if (covers) {
      const one = need.length === 1;
      action = `<b>Reseat</b> the ${one ? 'colony' : `${need.length} colonies`} below — covers the <b>${shortBy.toLocaleString()}/day</b> gap${ifRicher}.`;
      list = `<div class="an-bd-prod-list">${reseatRows}</div>`;
    } else if (reclaimable > 0) {
      action = `Reseating recovers ~${reclaimable.toLocaleString()}/day — still short <b>${rem.toLocaleString()}/day</b>.${ifRicher} ${hasUnknown ? 'Rescan may reveal more; otherwise ' : ''}${colTail.trim()}.`;
      list = `<div class="an-bd-prod-list">${reseatRows}</div>`;
    } else if (hasUnknown) {
      action = `No decline measured — <b>try reseating</b>, then <b>Rescan</b>. If it doesn't add ~${shortBy.toLocaleString()}/day,${colTail}.`;
    } else {
      action = `At peak — reseating won't close <b>${shortBy.toLocaleString()}/day</b>. ${colTail.trim()}.`;
    }
    return `<div class="an-bd-group"><div class="an-bd-group-h">${headH}</div><div class="an-bd-target">${action}</div>${list}</div>`;
  }).join('');

  return `<div class="an-suggest an-suggest-burndown">
      <div class="an-suggest-h">Fix short materials</div>
      <div class="an-levers-lead">Short on <b>${short.map(r => _esc(r.name)).join('</b>, <b>')}</b>. Reseating recovers <em>lost</em> yield only — no help if a colony is already at its peak. If it's not enough, <b>add or redeploy</b>.</div>
      <div class="an-bd-groups">${groups}</div>
    </div>`;
}

// Reseat guidance for hand-built ("hybrid") colonies — one planet running extraction + a chained
// P1->P2+ factory together, a shape our own templates never produce (see project docs). Deliberately
// SEPARATE from _burndownSection/_producersOf: a hybrid colony's internal P1 is self-consumed on
// that one planet (pi_sim.colony_sim_state tags it exportable=false), so it's never pooled into the
// account-wide rebalance picture — this only ever compares a planet against ITS OWN downstream need,
// and only ever suggests reseating the planet it already has, never redeploying/relocating it.
function _hybridReseatSection() {
  if (!_featureActive('hybrid_colonies')) return '';
  const toP0h = p1day => Math.round(p1day * 150 / 24);   // P1/day -> P0/hr, the in-game ECU rate
  const rows = [];
  (_ppCharsData || []).forEach(ch => (ch.planets || []).forEach(p => {
    if (!p.is_hybrid) return;
    const prod = p.production || [];
    const downstream = prod.find(o => o.chain_need_per_day != null);
    if (!downstream) return;
    const upstream = prod.find(o => o.type_id === downstream.chain_input_type);
    if (!upstream) return;
    const need = downstream.chain_need_per_day || 0;
    const have = upstream.per_day || 0;
    if (need <= 0 || have >= need) return;   // already fully fed — nothing to suggest
    const pct = Math.round(have / need * 100);
    const loc = p.system ? `${_esc(ch.name)} · ${_esc(p.system)}${p.planet_num != null ? ' P' + p.planet_num : ''}` : _esc(ch.name);
    rows.push(`<div class="an-bd-group">
        <div class="an-bd-group-h">${loc} <span class="an-bd-group-sub">its ${_esc(downstream.name)} line wants ~${need.toLocaleString()} ${_esc(upstream.name)}/day; its own extraction only sustains ~${Math.round(have).toLocaleString()} (${pct}% fed)</span></div>
        <div class="an-bd-target">Reseat its heads toward <b>${toP0h(need).toLocaleString()} P0/hr</b> to fully feed the ${_esc(downstream.name)} line — this colony stays put, only its hotspots move.</div>
      </div>`);
  }));
  if (!rows.length) return '';
  return `<section class="pp-card">
      <div class="pp-card-title">Hybrid colonies</div>
      <div class="pp-card-body">
        <div class="an-legend">Colonies that extract and process on the same planet — tracked separately since reseating one only helps that planet's own chain, not the rest of your account.</div>
        <div class="an-suggest an-suggest-burndown"><div class="an-suggest-h">Reseat candidates</div><div class="an-bd-groups">${rows.join('')}</div></div>
      </div>
    </section>`;
}

function _extRuntimeAdviceHtml(headroom, curDays) {
  if (!_extRt || !_extRt.ppd || !headroom) return '';
  const detected = !!(curDays && curDays > 0);
  const L0 = detected ? curDays : 2;                      // ACTUAL program length (fractional, not rounded)
  const e0 = _extEff(L0);
  const buffer = d => headroom * _extEff(d) / e0 - 1;     // extraction over demand at a d-day program
  const bL0 = buffer(L0);                                 // buffer at the CURRENT runtime
  const L0lbl = detected ? `~${_fmtHours(L0 * 24)}` : '~2-day (assumed)';

  // Fed at the current runtime → leave it alone. Don't push SHORTER (a bigger buffer you don't need,
  // plus more frequent restarts) or LONGER (spare extraction is better spent on factories — above).
  // This is deliberate: chasing buffer creates a runaway (shorten → surplus → more factories → ...).
  // Longest whole-day program that still stays fed — graph markers + the "shorten" option.
  let fedDay = 0;
  for (let d = 1; d <= _EXT_MAX_DAYS; d++) if (buffer(d) >= 0) fedDay = d;
  const graph = _extDecayGraphSvg(headroom, L0, fedDay || 1, fedDay || 1);

  // Healthy: comfortable buffer (≥10%). No graph — a flat sustainable curve is just noise; the graph
  // only earns its space when the crossover matters (tight / deficit, below).
  if (bL0 >= 0.10) {
    return `<div class="an-suggest an-suggest-add an-rt-ok">
        <div class="an-suggest-h">Extraction runtime <span class="an-rt-badge an-rt-badge-ok">✓ sustainable</span></div>
        <div class="an-rt-pick"><b>+${Math.round(bL0 * 100)}%</b> above demand on ${L0lbl} programs — comfortable margin as heads decay.</div>
        <div class="an-sug-note">Spare extraction is better spent on more factories than a shorter runtime.${detected ? '' : ' Length assumed 2d — rescan to detect yours.'}</div>
      </div>`;
  }

  // Tight: fed now, but a thin buffer that decay will eat before you reseat — surface as a WARNING.
  if (bL0 >= 0) {
    return `<div class="an-suggest an-suggest-warn an-rt-warn">
        <div class="an-suggest-h">Extraction runtime <span class="an-rt-badge an-rt-badge-warn">⚠ tight</span></div>
        <div class="an-rt-pick">Only <b>+${Math.round(bL0 * 100)}%</b> above demand on ${L0lbl} programs — extraction <b>decays</b> as the program runs, so factories will dip short before you reseat.</div>
        <ul>
          <li><b>Reseat the binding colonies</b> onto stronger hotspots, or <b>redeploy</b> a surplus colony onto the binding material — builds margin without more restarts.</li>
        </ul>
        ${graph}
        <div class="an-sug-note">Decay is an estimate — verify against your in-game ECU.</div>
      </div>`;
  }

  // Deficit at the current runtime: factories fall behind. The least-work, LASTING fix is more yield
  // (reseat / redeploy / add a colony) — that keeps the long runtime. Shortening trades runtime for
  // more restarts (more work) and only masks the gap, so it's a stopgap, demoted to the note.
  const canShorten = fedDay >= 1 && fedDay < L0;
  return `<div class="an-suggest an-suggest-fix">
      <div class="an-suggest-h">Extraction runtime <span class="an-rt-badge an-rt-badge-bad">✕ falling behind</span></div>
      <div class="an-rt-pick">${L0lbl} programs <b>over-extend</b> supply — extraction covers only <b>${Math.round((bL0 + 1) * 100)}%</b> of demand.</div>
      <ul>
        <li><b>Reseat</b> short colonies, <b>redeploy</b> a surplus colony onto the short material, or <b>add a colony</b>.</li>
      </ul>
      ${graph}
      <div class="an-sug-note">${canShorten
        ? `Shortening to ${fedDay}d (${_progDuration(fedDay)}) would help on fresher extraction — but means more restarts and masks the gap. Add extraction instead.`
        : `Shorter programs won't help — even daily cycles can't keep up.`} Decay is an estimate — verify against your in-game ECU.</div>
    </div>`;
}

function _buildPlanSnapshot(data) {
  const factories = [];
  for (const a of (data.assignments || []))
    for (const f of (a.factory_assignments || []))
      if (f.p1_inputs && f.p1_inputs.length && f.system)  // placed factories only
        factories.push({
          loc: `${a.character_name} · ${f.system}${f.planet_num != null ? ' P' + f.planet_num : ''}`,
          product: f.product ? f.product.name : 'Factory',
          p1_inputs: f.p1_inputs.map(p => ({ p1_type_id: p.p1_type_id, p1_name: p.p1_name, share: p.share })),
        });
  if (!factories.length) return null;
  // Per-P1 daily consumption (units/day at full factory rate) so the refill tool can show how
  // long a pasted P1 stash lasts before a refill. Stored with names so the readout is
  // self-contained (no cross-lookup needed).
  const consumption = (data.p1_requirements || [])
    .filter(r => r.units_per_day != null)
    .map(r => ({ p1_type_id: r.p1_type_id, p1_name: r.p1_name, units_per_day: r.units_per_day }));
  const st = data.stats || {};
  return {
    name: (data.product && data.product.name) || 'Plan',
    factories, consumption,
    // Production rate + daily sell value so the refill view can show units made and ISK over a run.
    products_per_day: st.products_per_day,
    isk_per_day: st.isk_per_day,
    unit_label: data.fuelblock ? (st.unit_label || 'fuel blocks') : 'units',
    // Refill cadence + factory count so the Setup Analysis tab can frame "refill every X days".
    factory_refill_hours: st.factory_refill_hours,
    factories_count: factories.length,
  };
}

// localStorage keeps the last-built plan per product (a quick, unsaved fallback). Explicit
// saves go server-side via savePlanForRefills (cross-device, named).
function _storePlanSnapshot(data) {
  const snap = _buildPlanSnapshot(data);
  if (!snap) return;
  const entry = { ...snap, id: 'local:' + snap.name, savedAt: Date.now(), local: true };
  let snaps = _loadPlanSnapshots().filter(s => s.name !== snap.name);
  snaps.unshift(entry);
  try { localStorage.setItem(_PLAN_SNAP_KEY, JSON.stringify(snaps.slice(0, 8))); } catch (e) {}
}

async function savePlanForRefills() {
  const data = _wiz.lastPlanData;
  const snap = data && _buildPlanSnapshot(data);
  if (!snap) { alert('No placed factories in this plan to save.'); return; }
  if (!_loggedIn) {
    alert('Log in to save plans across devices.\n\nYour last plan is already kept in this browser, so it\'s in the PI Planner tab without re-running the wizard.');
    return;
  }
  const name = prompt('Save this plan as:', snap.name);
  if (!name || !name.trim()) return;
  snap.name = name.trim();
  snap.payload = _buildPlanPayload();  // full plan so it can be reopened (not just refilled)
  const btn = document.getElementById('savePlanBtn');
  try {
    const resp = await fetch('/api/plan-snapshots', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: snap.name, snapshot: snap }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || `HTTP ${resp.status}`);
    if (btn) { const t = btn.textContent; btn.textContent = '✓ Saved'; setTimeout(() => { btn.textContent = t; }, 2000); }
    renderSavedPlansBar();  // keep the page-1 list current
  } catch (e) { alert('Save failed: ' + e.message); }
}

async function deletePlanSnapshot(srvId) {
  if (!confirm('Delete this saved plan?')) return;
  try { await fetch(`/api/plan-snapshots/${srvId}`, { method: 'DELETE' }); } catch (e) {}
  const el = document.getElementById('planDistSection');
  if (el) el.dataset.sel = '';
  renderPlanDistribution();
}
