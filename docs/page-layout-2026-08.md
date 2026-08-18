# One page-split pattern for Manufacturing and Reactions — built 2026-08-18

TODO §41 asked for both pages to stop being one long scroll, and explicitly asked that the split be
designed ONCE and applied to both, so they don't end up with two different navigation idioms for the
same underlying problem. **Built and on `dev`.**

The mechanism is a **horizontal tab strip within the page** — not an accordion fold per section, and
not a separate route per section. Both of those were tried first and corrected: an earlier draft of
this doc proposed `<details>` folds for secondary sections and a real sub-page/route for
Manufacturing's Build Pipeline, both were built, and the user's actual intent — a tab strip, nothing
folded, nothing on its own address — only surfaced once they saw the result. Rebuilt to match. The
folds/own-page draft is gone from history below; this describes the shipped shape only.

## Contents

- **The mechanism** — `ppSelectTab`/`ppRestoreTab` (`static/utils.js`), the one shared piece
- **Manufacturing's tabs**
- **Reactions' tabs**
- **What does NOT change**
- **Why not folds, why not a route** — the two things this correctly avoided

## The mechanism

One small shared primitive, `static/utils.js`:

- `ppSelectTab(group, key)` — shows the panel whose `data-tabpanel="<group>"` +
  `data-tabkey="<key>"` matches, hides the rest, marks the matching button (`data-tabgroup`
  matching, `data-tabkey` matching) `.pp-tab-active`, and remembers the choice
  (`localStorage['ppTab:<group>']`).
- `ppRestoreTab(group, fallback)` — call once when a page/tab opens; selects whichever tab was last
  read (or `fallback` on a first visit) and **returns the resolved key**, so a caller whose tabs
  lazy-load (Reactions' Shopping list / Advanced) can trigger that load for whichever tab is now
  showing without reading `localStorage` a second time itself.

Deliberately NOT wired into the router (`TAB_SUBPAGES`/`SPA_PAGES`): these are sections of ONE
page, not addresses. `TAB_SUBPAGES` stays reserved for pages that are genuinely several pages behind
one nav entry (Admin's eleven sections, PI Planner's two modes) — a tab a user checks by clicking,
not one anybody would paste as a link.

Styling: `.pp-tabstrip` / `.pp-tab-btn` / `.pp-tab-btn.pp-tab-active`
(`static/style-layout-admin.css`), new and shared — the underline-on-active convention doesn't
otherwise exist in this app (the sidebar's own tabs are a vertical list, not this shape). The
`.pp-fold-badge` count-badge class from the earlier fold draft survived unchanged: it now sits
inside a tab BUTTON instead of a fold's `<summary>`, same look, same job — "something here needs
your attention," visible whichever tab is actually showing.

## Manufacturing's tabs

`#indStatusCard`'s body (`static/index.html`), one tab strip (`data-tabgroup="ind"`), four tabs, all
painted from the same fetched plan on every update (`_indPaintStatus` calls
`_indPaintBlueprints`/`_indPaintPipeline` unconditionally, not lazily on switch — both are a
pure-JS pass over the plan already in hand, not a fetch, so nothing is saved by skipping the ones
not currently showing, and painting all four up front means switching tabs never shows stale
content):

- **Status** (default) — headline/metrics tiles + Do This Now (`_indStatusHeadline` +
  `indRenderInstall`), unchanged from before any of this started.
- **Blueprints & materials** (`_indRenderPlanBody` → `#indBlueprintsBody`) — the notices (missing
  prints, copies short of runs, the pin note, skill blockers — `_indNotices`), the marginal-savings
  strip, and the Shopping list. Badged (`#indBlueprintsTabBadge`) with the count of things that
  actually BLOCK a build (`_indBlueprintBadgeCount` — missing prints + copies short; NOT the
  marginal-savings count, a choice rather than a blocker).
- **Build Pipeline** (`_indRenderPipelineBody` → `#indPipelineBody`) — the reaction-policy bar (a
  control over what THIS view schedules, not over what to buy) + `_indPipelineHtml` (the stage
  grid, corrected by §39) + `_indStepsHtml` (the step-through checklist).
- **Currently running** (`#indRunning`, `indLoadRunning`) — badged (`#indRunningTabBadge`) with the
  live job count, moved off an `<h3>` that used to be visible only once this section was open.

Skill-gap blockers live in Blueprints & materials only, not duplicated into Build Pipeline:
`_indMissingBpWarn` fires a background contract-price fetch keyed by a render-instance id, and
rendering the whole notice stack in two tabs per plan update would fire it twice for nothing.

## Reactions' tabs

`static/index.html`'s Reactions panel, one tab strip (`data-tabgroup="rx"`), four tabs — Orders and
Advanced hide their TAB BUTTON entirely when gated off (feature flag / TODO 35), same as their whole
card used to hide:

- **Overview** (default) — the Metrics card + the Reactions status card (pending jobs, stage
  banners, running-job list), unchanged content, just now one tab instead of two always-open cards.
- **Shopping list** (`_loadRxShoppingList` → `#rxShoppingListContent`) — lazy: fetched on first
  select and on every re-select (matching its prior "fetch again each time the fold opens"
  behavior), not on every dashboard refresh. Carries the missing-formulas report
  (`_rxMissingFormulaWarn`) and a badge (`#rxShopMissingBadge`,
  `_rxUpdateShopMissingBadge`) — the badge updates from the Overview tab's own fetched data on
  EVERY dashboard render regardless of whether Shopping list has ever been opened, so the count is
  never stale even unselected. The nested "Paste what you've received" diff tool stays a `<details>`
  fold — it's fine-grained detail INSIDE one tab's content, not a top-level page section, and that
  distinction is exactly what folds are still right for.
- **Customer orders** (`_rxLoadOrders` → `#rxOrdersContent`) — NOT lazy like Shopping/Advanced; it
  loads unconditionally once the `reaction_orders` feature flag confirms it's on, matching its prior
  behavior (it was never behind a fold, just a feature gate).
- **Advanced** (`_rxLoadAdvancedTable` → `#reactionsContent`) — lazy, force-reloaded on every
  select (mirrors the prior "reload every time the fold opens" behavior; the 90s server-side cache,
  `_OPPS_CACHE_TTL`, absorbs a re-select within that window).

## What does NOT change

- **Nothing gains a URL.** No new `SPA_PAGES` entries, no new sidebar nav items — `data-pimode`
  (the PI Planner's Find Buildables / Refill mechanism) was briefly generalized to `data-submode`
  for a routed Manufacturing mode and then reverted byte-for-byte once the design changed; the
  final diff carries no trace of that detour.
- **The fold convention isn't gone, just demoted to what it's actually for.** `.rx-fold-summary` /
  `.rx-fold-caret` still exist and are still used — for detail nested INSIDE a tab (Reactions'
  "Paste what you've received"), never again for a top-level page section.

## Why not folds, why not a route

Both were tried and are worth naming so nobody re-derives them from scratch:

- **A fold hides a top-level section behind an extra click AND changes the page's height under
  the reader's cursor** — every fold that opens pushes everything below it down, which a tab strip
  never does (the strip's own height is fixed; only the panel content swaps). For sections read
  almost every visit (Blueprints & materials, Shopping list), that's a worse trade than a tab.
- **A route (`/manufacturing/pipeline`) is for something somebody would deep-link or expect Back
  to return to as its own step.** None of these sections are that — they're views of the SAME
  build/plan a user is already looking at, selected via a click, not typed as an address. The
  routing scaffolding (`TAB_SUBPAGES`, `SPA_PAGES`, a sidebar sub-item, `test_routing.py`'s
  four-list check) is real, tested, working machinery for the few pages that ARE several pages —
  Admin, PI Planner — and reaching for it here was solving a problem this split didn't have.
