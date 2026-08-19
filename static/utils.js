// Shared HTTP + formatting helpers, loaded first so every feature file can rely on these
// being global.

// ── API ───────────────────────────────────────────────────────────────────────────────
// One place every API call goes through. Before this, ~170 call sites hand-rolled fetch and
// only about half checked `resp.ok` at all — the rest handed a JSON error body to code
// expecting data, so a 500 surfaced as an unrelated TypeError several frames later, or as a
// silently empty panel. `api()` throws instead, carrying the server's own `detail` string
// (which is what the FastAPI handlers actually put there, including main.py's "a deploy may
// still be rolling out" hint on an unmatched /api/ path).
//
// Callers that genuinely want to ignore a failure still catch it — the difference is that
// ignoring is now a deliberate `catch`, not the default.
class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    // The server's `detail` as it actually came back. `message` cannot carry it: FastAPI's detail is
    // sometimes an OBJECT (a validator answering with every problem at once), and `Error` stringifies
    // whatever it is given — so a caller reading `e.message` for a structured body got the literal
    // text "[object Object]". Kept beside it rather than instead of it, so `toastError` and the
    // dozens of callers that want one line still get one.
    this.detail = detail;
  }
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = '';
    try { detail = ((await resp.json()) || {}).detail || ''; } catch (e) { /* non-JSON body */ }
    const text = (detail && typeof detail === 'object') ? `HTTP ${resp.status}` : detail;
    throw new ApiError(text || `HTTP ${resp.status}`, resp.status, detail);
  }
  if (resp.status === 204) return null;
  return resp.json().catch(() => null);   // some endpoints legitimately return an empty body
}

// The write half — POST/PUT/PATCH/DELETE with an optional JSON body. Omitting `body` sends no
// content-type, which matters: several endpoints take no body and FastAPI rejects an empty one
// declared as JSON.
function apiSend(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  return api(path, opts);
}

// ── Toasts ────────────────────────────────────────────────────────────────────────────
// Replaces the 55 blocking `alert()` calls this app used as its only error channel. Non-modal,
// auto-dismissing, and stackable, so a failure during a bulk action (rescan-all, queue reorder)
// reports without halting the loop the way alert() did.
function toast(msg, kind = 'info', ms = 4500) {
  let host = document.getElementById('toastHost');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toastHost';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.textContent = String(msg);
  el.addEventListener('click', () => el.remove());
  host.appendChild(el);
  setTimeout(() => { el.classList.add('toast-out'); setTimeout(() => el.remove(), 250); }, ms);
}

// The common "an action failed" path: log for the console, tell the user what the server said.
function toastError(e, prefix) {
  const m = (e && e.message) ? e.message : String(e);
  toast(prefix ? `${prefix}: ${m}` : m, 'error');
}

// ── Confirm ───────────────────────────────────────────────────────────────────────────
// Native confirm() has one failure mode that matters here: once a page has thrown a few dialogs,
// browsers offer "prevent this page from creating additional dialogs", and every later confirm()
// then returns FALSE with no prompt and no error. Every destructive action in this app was gated
// on one, so the symptom is a button that silently does nothing — reported against "Cancel order",
// whose backend was provably fine and whose order was still `open` in the database because the
// request was never sent.
//
// Same reasoning that replaced 55 alert() calls with toasts, finished off: the app owns its own
// dialogs, so nothing outside it can switch them off. Returns a Promise<boolean>, so callers read
// as `ppConfirm(...).then(ok => { if (!ok) return; ... })`.
function ppConfirm(message, { okLabel = 'Confirm', danger = true } = {}) {
  return new Promise(resolve => {
    const back = document.createElement('div');
    back.className = 'pp-confirm-back';
    back.innerHTML = `
      <div class="pp-confirm" role="alertdialog" aria-modal="true">
        <div class="pp-confirm-msg"></div>
        <div class="pp-confirm-actions">
          <button class="pp-cancel-btn" data-no>Keep it</button>
          <button class="${danger ? 'pp-danger-btn' : 'pp-add-btn'}" data-yes></button>
        </div>
      </div>`;
    back.querySelector('.pp-confirm-msg').textContent = message;
    back.querySelector('[data-yes]').textContent = okLabel;

    let done = false;
    const close = ok => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey);
      back.remove();
      resolve(ok);
    };
    const onKey = e => {
      if (e.key === 'Escape') close(false);
      if (e.key === 'Enter') close(true);
    };
    back.querySelector('[data-no]').addEventListener('click', () => close(false));
    back.querySelector('[data-yes]').addEventListener('click', () => close(true));
    // Clicking the backdrop cancels — but only the backdrop, never a stray click inside the card.
    back.addEventListener('click', e => { if (e.target === back) close(false); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(back);
    back.querySelector('[data-yes]').focus();
  });
}

// Sign handled by taking the magnitude first — every threshold below used to compare the SIGNED
// value (`n >= 1e6`), which is always false for a negative n regardless of size, so a losing
// figure (a negative "expected profit/day" etc.) fell through to the raw, unabbreviated branch
// while a positive figure of the same magnitude got "M"/"B" — reported live (2026-08-19) as
// Reactions' profit tile looking like it used a different number format from its neighbours.
function fmtIsk(n) {
  const sign = n < 0 ? '-' : '';
  const v = Math.abs(n);
  if (v >= 1e9) return sign + (v / 1e9).toFixed(2) + ' B';
  if (v >= 1e6) return sign + (v / 1e6).toFixed(2) + ' M';
  if (v >= 1e3) return sign + (v / 1e3).toFixed(1) + ' K';
  return sign + v.toFixed(0);
}

// Spaced ISK style ("12.3 K") — the B/M/K logic lives once in fmtIsk above; only the
// sub-1k branch differs (keeps separators). NB: _iskFmt (planetary.js) is a DIFFERENT
// compact style ("12k", no space, signed) used by the Factory Layout cards.
function _fmtIsk(v) {
  return Math.abs(v) >= 1e3 ? fmtIsk(v) : v.toLocaleString();
}

// Readable duration, FLOORED so it never overstates a "time left" (1d → days + hours, 12–24h →
// hours, under 12h → hours + minutes): 61.5h → "2d 13h", 18.x → "18h", 1.7h → "1h 42m".
function _fmtHours(h) {
  if (!(h > 0)) return '0m';
  if (h >= 24) { const d = Math.floor(h / 24), hr = Math.floor(h % 24); return hr ? `${d}d ${hr}h` : `${d}d`; }
  if (h >= 12) return Math.floor(h) + 'h';
  const hr = Math.floor(h), m = Math.floor((h % 1) * 60);
  return hr === 0 ? `${m}m` : (m ? `${hr}h ${m}m` : `${hr}h`);
}

function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Natural/numeric-aware name compare — "alt 2" before "alt 10" before "alt 20" (plain
// localeCompare treats digit runs as text, sorting alt 1, alt 10, alt 2, alt 20).
function _natCompare(a, b) {
  return (a || '').localeCompare((b || ''), undefined, { numeric: true, sensitivity: 'base' });
}

// Hours → "1d 23h 30m" (keeps minutes, unlike _fmtHours which floors them past 24h).
function _fmtDHM(h) {
  const total = Math.round((h || 0) * 60);
  const d = Math.floor(total / 1440), hr = Math.floor((total % 1440) / 60), m = total % 60;
  return [d ? d + 'd' : '', hr ? hr + 'h' : '', m ? m + 'm' : ''].filter(Boolean).join(' ') || '0m';
}

function _fmtWalletDate(s) {
  if (!s) return '';
  return String(s).slice(0, 16).replace('T', ' ');   // 2026-06-21 14:32
}

// HTTP-date (e.g. "Sun, 21 Jun 2026 20:38:14 GMT") → the viewer's local time, short form.
function _fmtCacheTime(s) {
  if (!s) return '—';
  const d = new Date(s);
  return isNaN(d) ? s : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Unix epoch (seconds) → the viewer's local clock time, e.g. "14:32" — used for ESI Expires
// hints (when a colony/skills fetch will next return anything new).
function _fmtEpochClock(sec) {
  if (!sec) return '—';
  return new Date(sec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Unix epoch (seconds) → short local date, e.g. "Jul 15" (adds the year if it's not this one) —
// used as the exact-date hover tooltip behind the relative "N days ago" hints below.
function _fmtEpochDate(sec) {
  if (!sec) return '';
  const d = new Date(sec * 1000);
  if (isNaN(d)) return '';
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString([], opts);
}

// Unix epoch (seconds) → coarse relative age, e.g. "3 weeks ago" — used for "last reseated / last
// redeployed" hints on Setup Analysis colonies, where "how long ago" reads faster than a date.
function _fmtEpochAgo(sec) {
  if (!sec) return '';
  const diff = Date.now() - sec * 1000;
  if (isNaN(diff)) return '';
  if (diff < 0) return 'just now';
  const days = Math.floor(diff / 86400000);
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return weeks === 1 ? 'a week ago' : `${weeks} weeks ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return months === 1 ? 'a month ago' : `${months} months ago`;
  const years = Math.floor(days / 365);
  return years === 1 ? 'a year ago' : `${years} years ago`;
}

// Parses one line of a pasted in-game inventory/asset list — name + quantity, tolerant of both
// tab-separated (EVE's clipboard export: Name\tQty\tGroup\tCategory\t...) and multi-space-padded
// paste variants. Returns [name, qty] or null for a blank/unparseable line. Shared by the PI
// Refill tool's inventory paste and the Reactions shopping-list "what you've received" diff.
function _parseInventoryLine(line) {
  line = line.trim();
  if (!line) return null;
  let parts = line.includes('\t') ? line.split('\t') : line.split(/ {2,}/);
  if (parts.length < 2) {
    const m = line.match(/^(.+?)\s+(\d[\d,\s]*)\s*$/);
    if (!m) return null;
    parts = [m[1].trim(), m[2].trim()];
  }
  const name = parts[0].trim();
  const col1 = parts[1].trim();
  let qtyStr;
  if (/^[\d\s,]+$/.test(col1) && /\d/.test(col1)) qtyStr = col1;   // Format A (qty in col 2)
  else if (parts.length >= 3) qtyStr = parts[2].trim();           // Format B (category col 2)
  else return null;
  const qty = parseInt(qtyStr.replace(/[^\d]/g, ''), 10) || 0;
  return (qty > 0 && name) ? [name, qty] : null;
}

// ── ESI connect consent copy ──────────────────────────────────────────────────────────
// Every "connect a character" affordance (PI market card, Reactions gate, Manufacturing wizard)
// triggers the SAME unified scope superset — see the long comment in app/esi.py. Testers were
// reasonably surprised that a button labelled "market character" asked for blueprints and assets,
// so the consent text is stated once here and reused verbatim everywhere, instead of each panel
// naming only the part it happens to care about. Do NOT narrow the scopes to match a narrower
// label: EVE refresh tokens carry only the LAST auth's scopes, so a smaller login silently strips
// what the character already had, with no way back.
function _connectScopeNote() {
  return 'One login grants markets, blueprints, industry jobs, assets, skills and planets — EVE only'
    + ' remembers the last permissions you granted, so anything narrower would strip what your'
    + ' character already had.';
}

// ── Horizontal tab strip ────────────────────────────────────────────────────────────────
// A row of tabs over a set of panels within ONE page — TODO §41 (docs/page-layout-2026-08.md):
// Manufacturing and Reactions each had a long vertical scroll of sections, and the fix is a
// shared in-page tab strip, not a fold-per-section or a separate page/route per section. Deliberately
// NOT wired into the router: these are sections of one page, not addresses — `TAB_SUBPAGES` is for
// the few pages that are genuinely several pages behind one nav entry (Admin, PI Planner). Persisted
// per GROUP to localStorage so a reload keeps you on the tab you were reading, same convenience the
// URL gives a real page without needing a URL.
//
// Markup convention: `data-tabgroup="<group>"` on both the strip's buttons and (redundantly, so a
// panel can be selected with the exact same selector shape) is NOT required on panels — panels use
// `data-tabpanel="<group>"` instead, so a panel and a button are never confused by a shared
// attribute name. `data-tabkey` names which tab a button selects / a panel belongs to.
function ppSelectTab(group, key) {
  document.querySelectorAll(`[data-tabgroup="${group}"]`).forEach(btn =>
    btn.classList.toggle('pp-tab-active', btn.dataset.tabkey === key));
  document.querySelectorAll(`[data-tabpanel="${group}"]`).forEach(panel =>
    panel.style.display = panel.dataset.tabkey === key ? '' : 'none');
  try { localStorage.setItem(`ppTab:${group}`, key); } catch (e) {}
}

// Called once when a page/tab opens, to land on whichever tab was last read (or `fallback` on a
// first visit). Safe to call every time the page opens, cheap either way. Returns the resolved
// key so a caller whose tabs lazy-load (Reactions' Shopping list / Advanced) can trigger that
// load for whichever tab is now showing, without reaching into localStorage a second time itself.
function ppRestoreTab(group, fallback) {
  let key = fallback;
  try { key = localStorage.getItem(`ppTab:${group}`) || fallback; } catch (e) {}
  ppSelectTab(group, key);
  return key;
}
