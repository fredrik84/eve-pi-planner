# One page-split pattern for Manufacturing and Reactions — proposal, 2026-08-18

TODO §41 asked for both pages to stop being one long scroll, and explicitly asked that the split be
designed ONCE and applied to both, so they don't end up with two different navigation idioms for the
same underlying problem. This is that proposal, written up for review before either page's markup
moved, per §41's own first step. **Both halves built 2026-08-18**, on `dev` pending review — the
Manufacturing/Reactions sections below carry the corrections found while building each one.

## Contents

- **What's actually on each page today** — the real section inventory, read from the code
- **The pattern** — three kinds of section, and the rule for sorting a thing into one of them
- **Manufacturing's split**
- **Reactions' split**
- **What does NOT change**
- **Implementation notes** — routing, the one shared helper, order of work

## What's actually on each page today

**Manufacturing's queue status view** (`_indPaintStatus`, `static/industry.js:429` +
`_indRenderPlanBody`, `static/industry-render.js:347`) renders nine things in one vertical stack,
none of them foldable except the last:

1. Headline/metrics tiles (`_indStatusHeadline`)
2. **Do This Now** (`indRenderInstall`)
3. Notices — blueprint-copy-runs-short, **missing-blueprint warning**, a pinned-structure note,
   skill-gap blockers (`_indNotices`, `static/industry-render.js:99`)
4. The marginal-savings strip ("worth building instead?")
5. The reaction-policy bar
6. **Build Pipeline** (the stage grid — `_indPipelineHtml`, just corrected by §39)
7. Steps (`_indStepsHtml`)
8. **Shopping list** — the one section already behind a `<details>` fold
9. Currently running jobs (`indLoadRunning`)

**Reactions' dashboard** (`static/index.html:592-680`) is already closer to split: a metrics card
and a dashboard-content card sit open on the page, and three heavier sections are already behind
`<details>` folds — Shopping list, Orders (feature-gated), and the Advanced opportunity table. So
Reactions' problem is smaller (three things already fold), but it's still one page, and the folds
aren't the tabs/sub-nav split Manufacturing needs — see "What does NOT change" below for why folds
alone aren't proposed as the whole answer for either page.

## The pattern

Three kinds of section, sorted by one question: **does the user need this on every visit, or only
when something is actually happening in it?**

- **Landing dashboard** — always the thing you see first. Metrics/headline + the action list ("Do
  This Now" / the pending-jobs dashboard). Nothing else earns a permanent place here: it's the one
  section both pages agree is read on every single visit, so it's the one section that gets to cost
  nothing to reach.
- **A fold, not a tab** — detail that's read often but not on every visit, and is cheap to render
  once open. Shopping list, Orders, currently-running jobs, the Advanced/opportunities table. Stays
  a `<details>` on the SAME page as the dashboard — a tab click for something you check most visits
  anyway is a cost with no payoff.
- **Its own view** — content that is genuinely a different task, not detail on the same one, and is
  either expensive to compute or long enough to want its own scroll position and its own back-button
  entry. Build Pipeline is the one clear case on either page today: it's a different way of looking
  at the SAME plan (a visualization, not a checklist), it's long (one column per stage, one row per
  building), and jumping to it from a pipeline card already exists (`_indJumpToStage`) — a real
  cross-reference, which is exactly what a same-page anchor should be, and exactly what a materially
  different tab still supports via `?stage=`.

**Alerts are a property of the fold, not a section of their own.** A "missing blueprints" callout
belongs on whichever fold it blocks, as a badge on the collapsed `<summary>` — not a fourth kind of
section. That's what makes "Missing Blueprints + Shopping List, with an alert if we're missing BPs"
(the user's own phrasing) fall out of the pattern rather than needing a special case.

## Manufacturing's split — built 2026-08-18

- **Landing dashboard**: headline/metrics tiles + Do This Now. Nothing else — the marginal-savings
  strip and the reaction-policy bar are controls, not information, and belong with the section they
  control (see below).
- **Fold: "Blueprints & materials"** (`_indRenderPlanBody`) — merges the missing-blueprint /
  blueprint-copy-runs-short notices, the marginal-savings strip, and the Shopping list.
  `#indBlueprintsDetails`'s `<summary>` carries `.pp-fold-badge` with the count of things that
  actually BLOCK a build (missing prints + copies short of runs — `_indBlueprintBadgeCount`), not
  the marginal-savings count (a choice, not a blocker): collapsed-but-flagged is the whole point,
  so a blocked build is never silently hidden.
- **Fold: "Currently running"** (`#indRunningDetails`) — `indLoadRunning`'s own count moved from an
  `<h3>` (only visible expanded) to a `.pp-fold-badge` on the fold's `<summary>`.
- **Its own view: "Build Pipeline"** (`_indRenderPipelineBody`, `#indModePipeline`,
  `/manufacturing/pipeline`) — the reaction-policy bar + `_indPipelineHtml` + `_indStepsHtml`.
  Reached via a new sidebar sub-item under Manufacturing (`setIndustryMode`, mirroring the PI
  Planner's `setPiMode`); `data-pimode` was renamed to the page-agnostic `data-submode` since it now
  drives two tabs, not one. Both views paint from the SAME fetched plan on every update
  (`_indPaintStatus` calls both renderers unconditionally) rather than lazily on switch — the
  pipeline's tree walk is pure JS, not a fetch, so there's nothing to save by skipping it, and
  painting both up front means a mode switch can never show stale content.
- **Correction against the original plan:** skill-gap blockers stay in the Blueprints fold ONLY,
  not duplicated into the pipeline view. `_indMissingBpWarn` fires a background contract-price
  fetch keyed by a render-instance id (`_indBpcSeq`) — rendering the full notice stack in both views
  per plan update would have fired that fetch twice for nothing. A build blocked on a skill gap is
  still one fold away, just not repeated in a second place.

## Reactions' split

Reactions already has the shape half-built; this is the delta to bring it in line with the same
three-kind pattern rather than leaving it as three independent folds:

- **Landing dashboard**: `rxMetricsContent` + `rxDashboardContent` (the pending-jobs / stage list) —
  unchanged, already the always-open pair.
- **Fold: "Shopping list"** now also carries the missing-formulas report
  (`reactions_missing_formulas` — the direct equivalent of Manufacturing's missing-blueprint
  warning). **Correction, built 2026-08-18:** this doc originally assumed the report already lived
  inside the shopping list's own "formulas to acquire" section — it didn't. `_rxMissingFormulaWarn`
  rendered unconditionally in `rxDashboardContent`, on every visit whether or not anything was
  missing, competing with the landing dashboard for attention. Moved into `_loadRxShoppingList`
  (sourced from the dashboard's own cached `missing_formulas`, since the shopping-list fetch itself
  doesn't carry it) with a count badge (`#rxShopMissingBadge`, `.pp-fold-badge`) on the fold's
  `<summary>` — badge logic lives in `_rxUpdateShopMissingBadge`, run on every dashboard render
  regardless of whether the fold has ever been opened, so the count is never stale even collapsed.
- **Fold: "Orders"** — unchanged, already a fold. **No separate "Currently running" fold exists on
  Reactions** — running jobs are rendered inline in `rxDashboardContent` as part of the landing
  dashboard itself, not a distinct section; this doc's first draft listed one in error.
- **Its own view?** — Reactions has no Build-Pipeline equivalent on the dashboard itself; the closest
  candidate is the Advanced/opportunities table (`rxAdvancedCard`), which is already the single most
  expensive computation on the page (per `_OPPS_CACHE_TTL`'s own comment) and already collapsed. It
  stays a fold, not its own view — unlike the pipeline, it's not a different way of reading the SAME
  plan, it's a distinct planning tool (browse-and-suggest vs. status-and-act), and it's not something
  read on most visits, so the extra cost of a real navigation isn't earning anything a fold doesn't
  already give it.

**Net result: Reactions changes less** — it already had the right shape for two of the three fold
candidates. The concrete work is the missing-formulas badge and folding the orders/running sections
under the same convention Manufacturing gets, not a structural rebuild.

## What does NOT change

- **Neither page becomes tabs-only.** A landing dashboard + folds is still one page for the common
  case; only the pipeline view (Manufacturing) becomes a real navigation, because it's the one
  section that's genuinely a different task rather than more detail on the same one.
- **One new, small, shared badge class.** Neither the admin-preview tag nor the skill-gap warning
  turned out to be a fold-summary count pill on closer look, so `.pp-fold-badge`
  (`static/style-layout-admin.css`) is genuinely new — but it's one class, styled once, used by both
  pages' folds (Reactions' shopping-list fold is the first caller). Not a per-page copy.
- **Routing cost is real and bounded.** Only Manufacturing gains one route
  (`/manufacturing/pipeline` or similar sub-page, alongside the existing `TAB_SUBPAGES` model PI
  Planner already uses for Find Buildables / Refill). Reactions gains no new route at all under this
  proposal — everything it needed was already a fold.

## Implementation notes

- **Routing, as built**: `index.html`'s two new panels (`#indModeStatus`/`#indModePipeline`),
  `TAB_SUBPAGES.industry` + `SPA_PAGES`'s `manufacturing/status`/`manufacturing/pipeline` in
  `app/main.py`, the new sidebar sub-item, and `test_routing.py`'s four-list check — all green,
  including `test_routing_client.js` (which runs the router for real) and `test_nav_gating.py`
  (which needed a fix: `data-pimode` renamed to `data-submode` since it now drives two tabs, and
  one CSS selector + one test regex were still written against the old name). Reactions needed none
  of this — confirmed, not just predicted.
- **One shared badge CLASS, not a shared render function.** `.pp-fold-badge` is the one thing
  actually shared — each page still builds its own `<summary>` HTML inline (Reactions in
  `_rxUpdateShopMissingBadge`/`_loadRxShoppingList`, Manufacturing in `_indRenderPlanBody`). A
  `_pp_foldSummary(...)` render helper was considered and dropped: the two pages' fold markup
  differs enough (Reactions updates a badge independently of the fold's content load; Manufacturing
  builds the whole `<details>` inline every render) that forcing one function would have been the
  "reuse-by-conditional" CLAUDE.md rule 4 warns against, for a few lines saved per call site.
- **Order of work, as built**: Reactions first (smaller, mostly labelling), then Manufacturing.
  Confirmed worth doing in that order — Reactions surfaced the "the report didn't actually live
  where this doc assumed" correction while the change was still small, before Manufacturing's larger
  move (new mode, new route) added its own moving parts on top.
