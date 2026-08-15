/**
 * The router is RUN here, not read — the first executable test of client code in this repo.
 *
 * `test_routing.py` pins the three lists and the server's routes. It cannot pin behaviour: every
 * assertion it makes about `app.js` is a string match, and a string match cannot tell you that
 * `switchTab` pushed the wrong URL, or that two browser tabs contaminate each other. That last one
 * is the whole point of Phase 2 and is invisible to any scan.
 *
 * There is no browser and no DOM here. The routing region of `app.js` is extracted and run in a
 * `vm` context with stubbed `location` / `history` / `localStorage` / `document`, which is enough to
 * exercise everything routing actually does. A context is one BROWSER TAB, so two of them sharing a
 * `localStorage` object reproduces the multi-tab bug exactly.
 *
 * Node lives on the host, not in the web container, so this one runs OUTSIDE:
 *
 *     node test_routing_client.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const fails = [];

function check(cond, msg) {
  console.log((cond ? '  ok   ' : '  FAIL ') + msg);
  if (!cond) fails.push(msg);
}

// ── Extracting the routing region ─────────────────────────────────────────────────────────────
// By marker, not by line number, so ordinary edits above it do not silently shift what is tested.
// If the markers ever stop matching this throws rather than testing nothing, which is the failure
// mode that matters: a green run over zero code.
function routingSource() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
  const start = src.indexOf('// ── Routing:');
  if (start < 0) throw new Error('the routing region marker is gone from static/app.js');
  const fnAt = src.indexOf('\nfunction switchTab(', start);
  if (fnAt < 0) throw new Error('switchTab is no longer in the routing region');
  const end = src.indexOf('\n}\n', fnAt);         // first column-0 close brace = end of switchTab
  if (end < 0) throw new Error('could not find the end of switchTab');
  return src.slice(start, end + 3);
}

function adminNavToSource() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'admin.js'), 'utf8');
  const at = src.indexOf('function adminNavTo(');
  if (at < 0) throw new Error('adminNavTo is gone from static/admin.js');
  const end = src.indexOf('\n}\n', at);
  if (end < 0) throw new Error('could not find the end of adminNavTo');
  return src.slice(at, end + 3);
}

const ROUTING = routingSource();
const ADMIN_NAV_TO = adminNavToSource();

/** One browser tab. `store` is shared between tabs when you want them to see the same storage. */
function makeTab(pathname, store) {
  const calls = [];
  const loc = { pathname, hash: '', search: '' };
  const storage = store || {};
  const sandbox = {
    location: loc,
    history: {
      pushState: (s, t, u) => { calls.push(['push', u]); loc.pathname = String(u).split('#')[0]; },
      replaceState: (s, t, u) => { calls.push(['replace', u]); loc.pathname = String(u).split('#')[0]; },
    },
    localStorage: {
      getItem: k => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
      removeItem: k => { delete storage[k]; },
    },
    // Enough DOM for switchTab's class/display bookkeeping to run without branching on it.
    document: { querySelectorAll: () => [] },
    loadHelpPanel: () => {},
    console,
    _opened: [],
  };
  sandbox.onReactionsTabOpen = () => sandbox._opened.push('reactions');
  sandbox.onDashboardTabOpen = () => sandbox._opened.push('dashboard');
  // Stand-ins for the two section modules — the real ones are thick with DOM and data loading.
  // They behave like the real ones in the way that matters to routing: opening the page RESTORES a
  // remembered section, which is precisely what could write the URL a second time and clobber the
  // entry for the page you came from.
  //
  // Because these stubs call `noteSubPage` themselves, nothing here can notice the real modules
  // dropping that call — verified by deleting it from both and watching this stay green. The
  // production call sites are pinned separately, at the bottom of this file.
  sandbox._sub = null;
  sandbox.adminSubPage = k => { sandbox._sub = k; sandbox.noteSubPage('admin', k); };
  sandbox.setPiMode = k => { sandbox._sub = k; sandbox.noteSubPage('planner', k); };
  sandbox.onAdminTabOpen = () => sandbox.adminSubPage(storage.adminPage || 'stats');
  sandbox.onPlannerTabOpen = () => sandbox.setPiMode(storage.piMode || 'build');
  vm.createContext(sandbox);
  vm.runInContext(ROUTING, sandbox, { filename: 'app.js#routing' });
  // The REAL adminNavTo, lifted from admin.js — it is the sidebar's entry point and the thing that
  // decides how many history entries a click produces, so stubbing it would test the stub.
  vm.runInContext(ADMIN_NAV_TO, sandbox, { filename: 'admin.js#adminNavTo' });
  return {
    sandbox, calls, loc, storage,
    run: expr => vm.runInContext(expr, sandbox),
    get current() { return vm.runInContext('currentTab()', sandbox); },
  };
}

// ── The URL names the page ────────────────────────────────────────────────────────────────────
console.log('\nthe page on screen is seeded from the URL, before anything can ask:');
check(makeTab('/reactions').current === 'reactions', "/reactions -> 'reactions' at script-load time");
check(makeTab('/industry/how-it-works').current === 'indhowitworks',
      'a multi-segment slug resolves to its tab');
check(makeTab('/').current === 'dashboard', '/ is the dashboard');
check(makeTab('/reactions/').current === 'reactions', 'a trailing slash is the same page');
check(makeTab('/s/abc123').current === null,
      'a share link names no page — null, so callers can tell "nothing chosen" from "dashboard"');
check(makeTab('/nope').current === null, 'an unknown path names no page');

console.log('\nswitching pages puts the page in the address bar:');
{
  const t = makeTab('/');
  t.run("switchTab('reactions')");
  check(t.current === 'reactions', 'currentTab() follows the switch');
  check(JSON.stringify(t.calls) === JSON.stringify([['push', '/reactions']]),
        'one history entry, pointing at /reactions (got ' + JSON.stringify(t.calls) + ')');
  check(t.storage.activeTab === 'reactions', 'the remembered tab is still written for a bare visit to /');
  check(t.sandbox._opened.includes('reactions'), "...and the page's open hook actually ran");
}
{
  const t = makeTab('/reactions');
  t.run("switchTab('reactions')");
  check(t.calls.length === 0, 'switching to the page you are already on adds no history entry');
}

console.log('\nback/forward does not push the entry the browser just moved to:');
{
  // Started AT the page, which is what `fromHistory` means — the browser has already moved the
  // address bar. (Handing it a page the URL does not name is not a real state; a mismatch there is
  // now repaired rather than ignored, which is the bare-`/admin` case further down.)
  const t = makeTab('/reactions');
  t.run("switchTab('reactions', { fromHistory: true })");
  check(t.calls.length === 0, 'fromHistory touches history not at all');
  check(t.current === 'reactions', '...and the page is open');
}

console.log('\na bounce REPLACES the URL — the bar must never name a page you cannot open:');
{
  const t = makeTab('/');
  t.run("switchTab('howitworks', { corrected: true })");
  check(JSON.stringify(t.calls) === JSON.stringify([['replace', '/how-it-works']]),
        'corrected:true replaces rather than pushes (got ' + JSON.stringify(t.calls) + ')');
}
{
  // The gating path inside switchTab itself: a restricted page is swapped for an allowed one, and
  // that swap must be a replace even though the caller asked for a plain navigation.
  const t = makeTab('/admin');
  t.run("var _isPageRestricted = n => n === 'admin'; var _firstAllowedPage = () => 'dashboard';");
  t.run("switchTab('admin')");
  check(t.current === 'dashboard', 'a restricted deep link lands on the first allowed page');
  check(t.calls.length === 1 && t.calls[0][0] === 'replace' && t.calls[0][1] === '/',
        'and the URL is corrected to it, by replacement (got ' + JSON.stringify(t.calls) + ')');
}

console.log('\nthe share link is left for planetary.js to consume, not fought over:');
{
  const t = makeTab('/s/abc123');
  t.run("switchTab('planetary')");
  check(t.calls.length === 0, 'landing on the planetary page from /s/<id> leaves the URL alone');
  check(t.loc.pathname === '/s/abc123', '...so the share id is still there to be read');
  check(t.current === 'planetary', '...and the page is open regardless');
}

// ── Sections: a second segment, for the pages that are really several pages ───────────────────
console.log('\na section is part of the address, so "look at the bug list" can be sent:');
{
  const t = makeTab('/admin/bugs');
  const route = p => JSON.parse(JSON.stringify(t.run(`routeForPath(${JSON.stringify(p)})`)));
  check(t.current === 'admin' && t.run('currentSubPage()') === 'bugs',
        'both halves of /admin/bugs are known at script-load time');
  check(route('/planner/find-buildables').sub === 'build',
        'the slug is not the internal key — find-buildables is the `build` mode');
  check(t.run("routeForPath('/admin/not-a-section')") === null,
        'an unknown section is not a route, so it falls through rather than opening a blank page');
  check(t.run("routeForPath('/reactions/anything')") === null,
        'a page with no sections does not acquire them');
  const hiw = route('/industry/how-it-works');
  check(hiw.tab === 'indhowitworks' && hiw.sub === null,
        'a two-segment TAB slug still beats the page/section reading of the same path');
}

console.log('\nopening a section deep link applies it over whatever was remembered:');
{
  const t = makeTab('/admin/users', { adminPage: 'stats' });
  t.run("switchTab('admin', { fromHistory: true, sub: 'users' })");
  check(t.sandbox._sub === 'users', 'the URL wins over the stored section (got ' + t.sandbox._sub + ')');
  check(t.calls.length === 0, 'and arriving on it writes no history entry');
}

console.log('\nONE click leaves ONE history entry, even though a section is restored on the way:');
{
  // The trap this guards: `onAdminTabOpen` restores a section mid-switch. If that restore wrote the
  // URL itself it would fire while the bar still read `/` — replacing the entry for the page you
  // came from, and then the real push would be a no-op. Back would leave the site. Confirmed by
  // removing the `_switching` guard: this drops to a single `replace` and the entry is gone.
  const t = makeTab('/', { adminPage: 'features' });
  t.run("adminNavTo('bugs')");
  check(JSON.stringify(t.calls) === JSON.stringify([['push', '/admin/bugs']]),
        'one push, straight to the section asked for (got ' + JSON.stringify(t.calls) + ')');
  check(t.sandbox._sub === 'bugs', 'and that is the section actually shown');
}
{
  const t = makeTab('/planner', { piMode: 'build' });
  t.run("switchTab('planner', { sub: 'refill' })");
  check(JSON.stringify(t.calls) === JSON.stringify([['push', '/planner/refill']]),
        'the PI Planner mode behaves the same (got ' + JSON.stringify(t.calls) + ')');
}
{
  // Moving between sections of the page you are already on is a navigation: Back should return to
  // the section you were reading, not to the previous top-level page.
  const t = makeTab('/admin/stats', { adminPage: 'stats' });
  t.run("adminNavTo('groups')");
  t.run("adminNavTo('cleanup')");
  check(JSON.stringify(t.calls) === JSON.stringify([['push', '/admin/groups'], ['push', '/admin/cleanup']]),
        'each section change is its own entry (got ' + JSON.stringify(t.calls) + ')');
}
{
  // A section changed by the module itself rather than by a click — refill.js flips to Refill once
  // a plan exists. The address must follow, but REPLACE: the user did not ask to navigate.
  const t = makeTab('/planner/find-buildables', { piMode: 'build' });
  t.run("setPiMode('refill')");
  check(JSON.stringify(t.calls) === JSON.stringify([['replace', '/planner/refill']]),
        'a module-driven section change corrects the address without a history entry (got '
        + JSON.stringify(t.calls) + ')');
}
{
  // Restoring a section for a page that is NOT on screen must not touch the URL at all.
  const t = makeTab('/reactions');
  t.run("switchTab('reactions')");
  const before = t.calls.length;
  t.run("adminSubPage('bugs')");
  check(t.calls.length === before && t.loc.pathname === '/reactions',
        'a background module reporting its state cannot move the address bar');
}

// ── Found by an independent review of this work, all six confirmed ────────────────────────────
// Each of these shipped green against the tests above, which is the point: they are the cases the
// author did not think to combine. Every one is reproduced here before its fix.
console.log('\na bounce still corrects the URL when the page was ARRIVED at rather than clicked:');
{
  // `fromHistory` and `corrected` can arrive together — a restricted deep link is exactly that,
  // reached by URL and then bounced. Suppressing the write on `fromHistory` left the address bar
  // naming the blocked page while another page was on screen: the one thing this prevents.
  const t = makeTab('/reactions');
  t.run("var _isPageRestricted = n => n === 'reactions'; var _firstAllowedPage = () => 'dashboard';");
  t.run("switchTab('reactions', { fromHistory: true })");
  check(t.current === 'dashboard', 'the restricted page is refused');
  check(t.loc.pathname === '/' && t.calls.length === 1 && t.calls[0][0] === 'replace',
        'and the URL is corrected rather than left naming it (got ' + JSON.stringify(t.calls) + ')');
}

console.log('\nan open hook may redirect us, and the outer switch must not overwrite it:');
{
  // The real shape: onAdminTabOpen bounces a confirmed non-admin by calling switchTab from INSIDE
  // switchTab. Everything after the hooks used to run anyway, so the screen showed the Dashboard
  // while the bar read /admin, currentTab() and storage disagreed, and one click left two entries.
  const t = makeTab('/');
  t.run("var onAdminTabOpen = () => { switchTab('dashboard'); };");
  t.run("switchTab('admin', { sub: 'bugs' })");
  check(t.current === 'dashboard', 'the redirect wins (got ' + t.current + ')');
  check(t.storage.activeTab === 'dashboard',
        'storage agrees with the screen (got ' + t.storage.activeTab + ')');
  check(t.loc.pathname === '/', 'and so does the address bar (got ' + t.loc.pathname + ')');
  check(t.calls.length <= 1, 'one click, at most one history entry (got ' + JSON.stringify(t.calls) + ')');
}
{
  // ...and the guard the counter protects is still up for the rest of the outer switch: a section
  // restored AFTER an inner switchTab returned must not write the URL on its own.
  const t = makeTab('/');
  t.run("var onAdminTabOpen = () => { switchTab('reactions'); adminSubPage('bugs'); };");
  t.run("switchTab('admin')");
  check(t.calls.filter(c => c[1] === '/admin/bugs').length === 0,
        'no stray write for a section of a page we were redirected away from');
}

console.log('\narriving at a bare page whose section gets restored says so in the address:');
{
  const t = makeTab('/admin', { adminPage: 'groups' });
  t.run("switchTab('admin', { fromHistory: true })");
  check(t.run('currentSubPage()') === 'groups', 'the remembered section is restored');
  check(t.loc.pathname === '/admin/groups' && t.calls.length === 1 && t.calls[0][0] === 'replace',
        'and the URL becomes the full path, by replacement (got ' + JSON.stringify(t.calls) + ')');
}
{
  // Same for the PI Planner, and it is why a bare `/planner` does not stay bare: the page always
  // shows one of the two modes, so an address that names neither is simply less true than the one
  // that does. A section-less page (above) is left exactly as it arrived.
  const t = makeTab('/planner', { piMode: 'refill' });
  t.run("switchTab('planner', { fromHistory: true })");
  check(t.loc.pathname === '/planner/refill',
        '/planner canonicalises to the mode on screen (got ' + t.loc.pathname + ')');
}

console.log('\na shared analysis only claims the page it actually reached:');
{
  const app = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
  const fn = app.slice(app.indexOf('async function loadFromHash'), app.indexOf('// ── Analyze'));
  check(/currentTab\(\) === 'analyze'/.test(fn),
        'the rewrite is conditional on the switch having been honoured — an unconditional one '
        + 'named Setup Analysis while How it works was on screen, AND dropped the #s= fragment');
  const pl = fs.readFileSync(path.join(ROOT, 'static', 'planetary.js'), 'utf8');
  // The CONDITION, not just the presence of the variable: deleting `!readingShare &&` from the if
  // while leaving the `const` behind reintroduces the bounce, and a bare `/readingShare/` test
  // stayed green through exactly that.
  check(/const readingShare = [^\n]*#s=/.test(pl) && /if \(!readingShare &&/.test(pl),
        'and a logged-out visitor reading a share is not bounced off it — both endpoints behind '
        + 'that link are deliberately unauthenticated');
}

console.log('\na section a group manager may not open is refused however they reach it:');
{
  const src = fs.readFileSync(path.join(ROOT, 'static', 'admin.js'), 'utf8');
  const at = src.indexOf('function adminSubPage(');
  const body = src.slice(at, src.indexOf('\n}\n', at));
  check(/_SITE_ADMIN_ONLY_PAGES\.has\(key\)/.test(body),
        'the check is in adminSubPage, the choke point — in onAdminTabOpen alone a pasted '
        + '/admin/bugs, or Back onto it, walked straight past it');
}

// ── The bug Phase 2 exists to kill ────────────────────────────────────────────────────────────
console.log('\nTWO BROWSER TABS do not answer each other\'s "which page am I on":');
{
  // Verified by reintroducing the defect — pointing `currentTab()` back at storage makes the
  // FOREGROUND tab below report 'reactions', a page it is not on, because the background tab wrote
  // last. That is what a rescan finishing in one tab did to the other's Dashboard render. Storage
  // is per ORIGIN; the page on screen is per tab. They were never the same fact.
  const shared = {};
  const fg = makeTab('/', shared);
  const bg = makeTab('/', shared);
  fg.run("switchTab('dashboard')");
  bg.run("switchTab('reactions')");
  check(fg.current === 'dashboard', 'the foreground tab still knows it is on the dashboard');
  check(bg.current === 'reactions', '...and the background tab knows it is on reactions');
  check(shared.activeTab === 'reactions',
        'storage holds only the last write across both — which is why nothing asks it any more');
}

// ── Every guard really asks the router ────────────────────────────────────────────────────────
// The behaviour above is only worth anything if the callers use it. A twelfth guard reading storage
// would reintroduce the multi-tab bug in one line, and nothing else in the repo would notice.
console.log('\nno page-guard reads storage to find out where it is:');
{
  const ALLOWED = [
    // The boot restore: genuinely "what did this browser last open", the one question storage is
    // the right answer to.
    ['static/app.js', "const saved = localStorage.getItem('activeTab');"],
    // Sanitation of a stored value for a page that no longer exists (characters became a modal).
    // Operates on the stored key itself, not on where the user is.
    ['static/planetary.js', "if (localStorage.getItem('activeTab') === 'characters') localStorage.removeItem('activeTab');"],
  ];
  const offenders = [];
  let scanned = 0;
  for (const f of fs.readdirSync(path.join(ROOT, 'static')).filter(f => f.endsWith('.js'))) {
    const rel = 'static/' + f;
    fs.readFileSync(path.join(ROOT, 'static', f), 'utf8').split('\n').forEach((line, i) => {
      if (!line.includes("getItem('activeTab')")) return;
      scanned++;
      if (!ALLOWED.some(([af, frag]) => af === rel && line.includes(frag))) {
        offenders.push(`${rel}:${i + 1}`);
      }
    });
  }
  check(scanned >= 2, `the scan found the storage reads it expects (${scanned})`);
  check(offenders.length === 0, `every remaining read is one of the two legitimate ones (offenders: ${offenders})`);
  check(fs.readFileSync(path.join(ROOT, 'static', 'dashboard.js'), 'utf8').includes("currentTab() === 'dashboard'"),
        'the post-rescan dashboard guard asks the router');
}

console.log('\nevery gating bounce corrects the URL rather than pushing onto it:');
{
  const pl = fs.readFileSync(path.join(ROOT, 'static', 'planetary.js'), 'utf8');
  // Named individually: these are the four places that move a user off a page they asked for, and
  // each needs `corrected` for a different reason (feature off, logged out, not an admin, group
  // restriction). A generic rule over all switchTab calls would also catch the legitimate default
  // landings, which must NOT replace.
  check(/switchTab\('dashboard', \{ corrected: true \}\)/.test(pl),
        'the feature-flag and admin-role bounces are corrections');
  check((pl.match(/switchTab\('dashboard', \{ corrected: true \}\)/g) || []).length === 2,
        'both of them, not just one');
  check(/switchTab\('howitworks', \{ corrected: true \}\)/.test(pl),
        'bouncing a logged-out visitor to How it works is a correction');
  check(/switchTab\(_firstAllowedPage\(\), \{ corrected: true \}\)/.test(pl),
        'bouncing off a group-restricted page is a correction');
  check(!/switchTab\(_firstAllowedPage\(\)\)/.test(pl),
        '...with no uncorrected version left behind');
}

console.log('\nan inventory share opens the page it renders on:');
{
  const app = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
  const fn = app.slice(app.indexOf('async function loadFromHash'), app.indexOf('// ── Analyze'));
  check(/switchTab\('analyze'/.test(fn),
        'consuming #s= lands on Setup Analysis instead of running it off screen');
  check(fn.includes('location.hash'),
        '...and keeps the fragment, so a refresh reloads the same shared analysis');
}

// ── The wiring the sandbox cannot see ─────────────────────────────────────────────────────────
// Everything above drives `switchTab` and `noteSubPage` directly. That leaves the production call
// sites unpinned: deleting `noteSubPage(...)` from adminSubPage or setPiMode reintroduces the whole
// defect — a section change that never reaches the address bar — and every behavioural check above
// stays green, because the stubs make the call themselves. So the call sites are asserted, in the
// only function each can live in.
console.log('\nthe real section modules report to the router:');
{
  const owner = (file, fnName) => {
    const src = fs.readFileSync(path.join(ROOT, 'static', file), 'utf8');
    const at = src.indexOf(`function ${fnName}(`);
    if (at < 0) return '';
    const end = src.indexOf('\n}\n', at);
    return end < 0 ? '' : src.slice(at, end);
  };
  const adminBody = owner('admin.js', 'adminSubPage');
  const piBody = owner('refill.js', 'setPiMode');
  check(adminBody.length > 0 && piBody.length > 0, 'both section functions were found');
  check(/noteSubPage\('admin',\s*key\)/.test(adminBody),
        "adminSubPage tells the router its section — without this `/admin/bugs` is unsendable");
  check(/noteSubPage\('planner',\s*_piMode\)/.test(piBody),
        'setPiMode tells the router its mode');
  // Inside the CHOKE POINT, not just somewhere in the file: putting it in adminNavTo instead would
  // miss the boot restore and the group-manager fallback, which never pass through a click.
  const adminSrc = fs.readFileSync(path.join(ROOT, 'static', 'admin.js'), 'utf8');
  check((adminSrc.match(/noteSubPage\(/g) || []).length === 1,
        'and only from there, so every route into the section is covered by one call');
}

console.log('\n' + (fails.length ? 'FAILED: ' + fails.join('; ') : 'all checks passed'));
process.exit(fails.length ? 1 : 0);
