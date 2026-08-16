// ── Backup & transfer: the whole configuration as one file (TODO §18b) ────────────────────────
//
// Two buttons over `GET /api/config/export` and `POST /api/config/import`. Almost all of the code
// here is the two things that separate this from a download link:
//
//   * **the download says what it discloses.** The file carries structure names and locations and
//     placeholder character names, because full fidelity is what makes it a backup — so the warning
//     is rendered from `discloses`, which the exporter itself fills. A file that identifies you
//     should say so from the code that made it identifying, not from a comment someone forgets.
//   * **an import previews before it writes.** Every import runs `dry_run` first and shows what it
//     would change; nothing is written until that preview is confirmed. Loading somebody else's
//     config over weeks of your own settings is not an action to discover the shape of afterwards.
//
// The file is read in the browser and posted as JSON — there is no upload endpoint and nothing is
// stored server-side, so a file that is never confirmed leaves no trace anywhere.

let _cfgIoFile = null;         // {name, config} — the parsed file waiting for a preview/confirm
let _cfgIoPreview = null;      // the last dry-run report, or null

function _cfgIoActive() { return _featureActive('config_export_import'); }

async function _loadBackupSettings() {
  const body = document.getElementById('settingsSecBackupBody');
  if (!body) return;
  if (!_cfgIoActive()) {
    body.innerHTML = `<div class="pp-empty">Not enabled for this account.</div>`;
    return;
  }
  body.innerHTML = _cfgIoHtml();
  _cfgIoLoadDiscloses();
}

function _cfgIoHtml() {
  return `
    <div class="settings-subsec">
      <div class="settings-subsec-title">Download your configuration</div>
      <div class="settings-subsec-hint">One JSON file with everything below in it. Keep it as a
        backup, or send it to somebody setting up the same operation.</div>
      <div class="opt-warning" id="cfgIoDiscloses" style="margin:8px 0">
        Loading what this file would contain…</div>
      <div class="ind-src-actions">
        <button class="pp-add-btn" onclick="cfgIoExport()">Download config file</button>
      </div>
    </div>
    <div style="border-top:1px solid var(--clr-border);margin:16px 0"></div>
    <div class="settings-subsec">
      <div class="settings-subsec-title">Load a configuration file</div>
      <div class="settings-subsec-hint">Pick a file to see exactly what it would change. Nothing is
        written until you confirm.</div>
      <div class="ind-src-actions">
        <input type="file" id="cfgIoFileInput" accept="application/json,.json"
               onchange="cfgIoPickFile(this)" style="display:none">
        <button class="pp-add-btn" onclick="document.getElementById('cfgIoFileInput').click()">
          Choose a file…</button>
      </div>
      <div id="cfgIoPreview"></div>
    </div>`;
}

// The warning is filled from the export endpoint rather than written here, so a new identifying
// field cannot be added to the file without this text growing to match it.
async function _cfgIoLoadDiscloses() {
  const el = document.getElementById('cfgIoDiscloses');
  if (!el) return;
  try {
    const res = await api('/api/config/export');
    const items = (res.discloses || []).map(d => `<li>${_esc(d)}</li>`).join('');
    el.innerHTML = `<b>This file identifies you.</b> Anyone you send it to can read:<ul>${items}</ul>`;
  } catch (e) {
    el.textContent = 'This file identifies you — it carries your structures and characters by name.';
  }
}

async function cfgIoExport() {
  try {
    const res = await api('/api/config/export');
    const blob = new Blob([JSON.stringify(res.config, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const stamp = new Date().toISOString().slice(0, 10);
    a.download = `eve-pi-planner-config-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoked on a tick rather than immediately: Safari cancels an in-flight download when the
    // object URL disappears in the same frame as the click.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('Configuration downloaded.', 'success');
  } catch (e) { toastError(e, 'Export failed'); }
}

function cfgIoPickFile(input) {
  const file = (input.files || [])[0];
  input.value = '';                       // so picking the same file twice re-triggers change
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    let parsed;
    try {
      parsed = JSON.parse(String(reader.result));
    } catch (e) {
      _cfgIoFile = null;
      _cfgIoRender(`<div class="opt-warning">That file is not valid JSON.</div>`);
      return;
    }
    _cfgIoFile = { name: file.name, config: parsed };
    cfgIoPreview();
  };
  reader.onerror = () => toastError(reader.error || 'unreadable', 'Could not read that file');
  reader.readAsText(file);
}

function _cfgIoRender(html) {
  const el = document.getElementById('cfgIoPreview');
  if (el) el.innerHTML = html;
}

function _cfgIoReplaceChecked() {
  const box = document.getElementById('cfgIoReplace');
  return !!(box && box.checked);
}

async function cfgIoPreview() {
  if (!_cfgIoFile) return;
  _cfgIoRender(`<div class="pp-empty">Checking…</div>`);
  try {
    const out = await apiSend('POST', '/api/config/import',
      { config: _cfgIoFile.config, dry_run: true, replace_structures: _cfgIoReplaceChecked() });
    _cfgIoPreview = out;
    _cfgIoRender(_cfgIoPreviewHtml(out));
  } catch (e) {
    _cfgIoPreview = null;
    // The validator answers with every problem at once — listing them is the whole point of it
    // doing so, and a toast would show one line of what may be six. The list arrives on
    // `ApiError.detail`, which is the raw body: `message` is a string, and an object put through it
    // comes out as "[object Object]".
    const detail = (e && e.detail) || null;
    const problems = (detail && detail.problems) || null;
    if (problems && problems.length) {
      _cfgIoRender(`<div class="opt-warning"><b>This file cannot be loaded:</b><ul>`
        + problems.map(p => `<li>${_esc(p)}</li>`).join('') + `</ul></div>`);
    } else {
      _cfgIoRender(`<div class="opt-warning">${_esc((e && e.message) || String(e))}</div>`);
    }
  }
}

function _cfgIoPreviewHtml(out) {
  const s = out.structures || {};
  const p = out.placeholders || {};
  const src = out.sources || {};
  const lines = [];
  if (out.sections && out.sections.length) lines.push(`Build rules: ${out.sections.join(', ')}`);
  if (out.per_order_plans != null) lines.push(`Per-order plans: ${out.per_order_plans ? 'on' : 'off'}`);
  if (s.added) lines.push(`${s.added} structure(s) added`);
  if (s.updated) lines.push(`${s.updated} structure(s) updated in place`);
  if (s.removed) lines.push(`<b>${s.removed} structure(s) removed</b>`);
  if (out.reaction_settings) lines.push('Freight rates, tax and reaction system replaced');
  if (src.enabled) lines.push(`${src.enabled} stock source(s) ticked`);
  if (src.sets) lines.push(`${src.sets} saved source set(s)`);
  if (p.added) lines.push(`${p.added} placeholder character(s) created`);
  if (p.updated) lines.push(`${p.updated} placeholder character(s) updated`);
  const notes = [];
  if (src.unknown) notes.push(`${src.unknown} stock source(s) in the file are not in this account — skipped`);
  (out.skipped || []).forEach(x => notes.push(x));

  return `<div class="pp-target-form" style="margin-top:10px">`
    + `<div class="settings-subsec-title">${_esc(_cfgIoFile.name)} would change:</div>`
    + (lines.length
        ? `<ul>${lines.map(l => `<li>${l}</li>`).join('')}</ul>`
        : `<div class="pp-empty">Nothing — this file matches what you already have.</div>`)
    + (notes.length
        ? `<div class="opt-warning"><b>Not applied:</b><ul>`
          + notes.map(n => `<li>${_esc(n)}</li>`).join('') + `</ul></div>`
        : '')
    // Opt-in, and re-previewed on change rather than only taking effect at confirm time: it is the
    // one choice here that deletes something, so its consequence has to be on screen BEFORE the
    // button that carries it out.
    + `<label class="ind-opt-check"><input type="checkbox" id="cfgIoReplace"`
    + `${_cfgIoReplaceChecked() ? ' checked' : ''} onchange="cfgIoPreview()">`
    + ` Remove structures this file does not mention</label>`
    + `<div class="ind-src-actions">`
    +   `<button class="pp-add-btn" onclick="cfgIoApply()">Apply this configuration</button>`
    +   `<button class="ind-bp-btn" onclick="cfgIoCancel()">Cancel</button>`
    + `</div></div>`;
}

function cfgIoCancel() {
  _cfgIoFile = null;
  _cfgIoPreview = null;
  _cfgIoRender('');
}

async function cfgIoApply() {
  if (!_cfgIoFile) return;
  const replace = _cfgIoReplaceChecked();
  const removing = ((_cfgIoPreview || {}).structures || {}).removed || 0;
  const ok = await ppConfirm(
    removing
      ? `Apply this configuration and remove ${removing} structure(s) it does not mention?`
      : 'Apply this configuration to your account?',
    { okLabel: 'Apply', danger: !!removing });
  if (!ok) return;
  try {
    const out = await apiSend('POST', '/api/config/import',
      { config: _cfgIoFile.config, dry_run: false, replace_structures: replace });
    cfgIoCancel();
    toast('Configuration loaded.', 'success');
    // Every panel that renders any of what just changed is stale now — reload the ones on screen
    // rather than leaving a settings page showing the values from before the import.
    if (typeof _loadMarketsSettings === 'function') _loadMarketsSettings();
    if (typeof _loadBuildRulesSettings === 'function') _loadBuildRulesSettings();
    if (out.skipped && out.skipped.length) {
      toast(`Not applied: ${out.skipped.join('; ')}`, 'info', 9000);
    }
  } catch (e) { toastError(e, 'Import failed'); }
}
