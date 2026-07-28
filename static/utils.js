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
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = '';
    try { detail = ((await resp.json()) || {}).detail || ''; } catch (e) { /* non-JSON body */ }
    throw new ApiError(detail || `HTTP ${resp.status}`, resp.status);
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

function fmtIsk(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + ' B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + ' M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' K';
  return n.toFixed(0);
}

// Spaced ISK style ("12.3 K") — the B/M/K logic lives once in fmtIsk above; only the
// sub-1k branch differs (keeps separators). NB: _iskFmt (planetary.js) is a DIFFERENT
// compact style ("12k", no space, signed) used by the Factory Layout cards.
function _fmtIsk(v) {
  return v >= 1e3 ? fmtIsk(v) : v.toLocaleString();
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
