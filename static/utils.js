// Shared formatting helpers used across app.js, planetary.js, dashboard.js, analysis.js
// and refill.js. Loaded first so every feature file can rely on these being global.

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
