# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-05.

---

## 19. Pages should live at URLs, so a link can be shared (2026-08-15, scoped)

Today every page is `/`. Which page you are on is a `localStorage` key, so a link to "the Reactions
tab" cannot be sent to anyone, a refresh silently restores somebody's *last* page rather than the
one they meant, and back/forward do nothing. Scoped 2026-08-15 — findings below are from the code,
not an estimate off the top of the head.

### What makes this cheaper than it looks

* **`switchTab(name)` is the single choke point.** Every nav button, every programmatic jump and the
  boot restore all go through it (`static/app.js:453`). A `history.pushState` there is a few lines,
  not a refactor.
* **Path-based SPA routes already work.** `/s/{share_id}` and `/b/{share_id}` serve the app from a
  real path today, with Open Graph tags injected so links unfurl in Discord. The mechanism, the
  helper (`_page`) and the unfurl story all exist.
* **Tabs are already declarative and 1:1** — 12 `data-tab` values against 12 `tab-<name>` panels, so
  a path segment maps straight onto one with no new concept.
* **There is precedent for a catch-all before the static mount** (`/api/{rest:path}`,
  `app/main.py:352`), which is exactly the shape the SPA route needs.

### What the work actually is

1. **A server catch-all that returns the SPA for a known page path** — and this is the riskiest
   change in the whole item. `StaticFiles` is mounted at `/` (`app/main.py:364`), so anything
   unmatched lands there. The route must be registered BEFORE the mount and must not shadow
   `/api/*`, real asset paths, `/s/`, `/b/` or `/healthz`. Get the predicate wrong and a missing
   `.js` returns HTML, which browsers report as a confusing parse error rather than a 404.
2. **Eleven `localStorage.getItem('activeTab')` reads across four files, and most are not restores —
   they are GUARDS**: *"am I on the dashboard right now?"* (`dashboard.js:62`, `planetary.js:186,
   233, 275, 280, 330`). Each has to ask the router instead. This is the bulk of the mechanical work
   and the main regression risk.
3. **Page gating has to correct the URL, not just the view.** `_isPageRestricted` / `_firstAllowedPage`
   currently bounce inside `switchTab`; with real URLs a blocked deep link must also rewrite the
   address bar or it lies about where you are. Gating is resolved asynchronously (after `/api/me`),
   so a deep link can arrive before it is known — the route has to resolve gating first, or it will
   flash the wrong page or bounce a page the user may actually see.
4. **Back/forward has never existed.** `popstate` means every `on*TabOpen` hook must be safe to
   re-enter. They already re-run on every click, so this is probably free — but it is per-tab
   checking, not an assumption.
5. **The share flow overlaps.** Consuming a `/s/{id}` link ends with `history.replaceState(null, '', '/')`
   (`planetary.js:1711`), which would wipe a real route. That interaction has to be reworked, not
   left.
6. **Mobile hides the heavy tabs** (`MOBILE_TABS`, `app.js:479`). A deep link to a desktop-only page
   on a phone needs a decided answer rather than a blank screen.

### How deep to go — decide before starting

`/reactions` is worth much more than nothing; `/industry/order/123` is worth more again, and is where
the cost balloons, because per-tab state lives in each tab's own module. **Recommendation: stop at
the top level first** and treat deep state as a separate item once the URL exists at all.

**Privacy check before any deep link ships (rule 8):** an id in a path is visible to whoever the
link is sent to. `/industry/order/123` in a pasted link tells the recipient an order id exists;
anything beyond a page name needs the same scrutiny the share links already got.

### Effort

* **Phase 1 — the top-level page in the URL, back/forward, deep-link entry, gating-aware.** One
  focused session. This is the phase that delivers the actual ask: shareable links to 12 pages.
* **Phase 2 — replace the eleven guards, rework the share-link interaction.** One session, and it is
  the one that needs care rather than cleverness.
* **Phase 3 — per-tab deep state.** Optional, open-ended, one tab at a time.

**The risk that dominates all three:** there are no browser tests (§2e-residual), and this is
*navigation* — the one thing source-scanning cannot verify. That argues for Phase 1 being kept small
and walked through by hand in a browser before Phase 2 starts.

## 18b. Config export / import (2026-08-14, from §18's answer)

**All that survives of §18** — the rest was answered and closed; see `TODO-archive.md` and
`docs/config-shape-2026-08.md` for the measurements.

A tester supplied a real ravworks config export: one flat keyed JSON object carrying structures,
rigs, declared slots and skills, per-category allocation, job-length settings, blacklists and tax,
with a version field, shared alliance-wide. Export/import is wanted (T13), and it was the strongest
of the three arguments for reshaping storage into a blob.

**It does not need the reshape.** A serialiser over the readers and writers that already exist
produces the same portable object without touching a single table — `get_settings`,
`_policy_payload`, `_pins_payload`, `effective_reaction_settings` and the source sets are already
the whole surface. Storage shape and portability turned out to be independent questions, which is
why §18's storage half is closed and this is not.

**First step:** write down what an export must contain and what it must NOT (anything identifying —
character names, structure IDs a stranger could locate), since it is meant to be shared. Then one
`GET /api/config/export` and one `POST /api/config/import` over the existing readers, versioned,
with import validating before it writes anything.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
