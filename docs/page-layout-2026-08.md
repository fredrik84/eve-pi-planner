# One page-split pattern for Manufacturing and Reactions — proposal, 2026-08-18

TODO §41 asked for both pages to stop being one long scroll, and explicitly asked that the split be
designed ONCE and applied to both, so they don't end up with two different navigation idioms for the
same underlying problem. This is that proposal. **Not implemented** — written up for review before
either page's markup moves, per §41's own first step.

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

## Manufacturing's split

- **Landing dashboard**: headline/metrics tiles + Do This Now. Nothing else — the marginal-savings
  strip and the reaction-policy bar are controls, not information, and belong with the section they
  control (see below).
- **Fold: "Blueprints & materials"** — merges the missing-blueprint / blueprint-copy-runs-short
  notices with the Shopping list, exactly as the user described for Reactions' equivalent. The
  `<summary>` carries a red badge with the missing count whenever `_indMissingBpWarn`/blueprint-short
  would have rendered anything — collapsed-but-flagged is the whole point, so a blocked build is
  never silently hidden behind a fold. The marginal-savings strip moves inside this fold too: it's a
  make-or-buy control over the same list.
- **Fold: "Currently running"** — `indLoadRunning`, unchanged, just folded instead of always-open.
- **Its own view: "Build Pipeline"** — `_indPipelineHtml` + `_indStepsHtml` + the reaction-policy bar
  (it's a control over what the pipeline schedules, not over what to buy). Reached via a sub-nav
  entry under Manufacturing, same idea as `industry_manual_done`'s step-through already gives each
  stage its own interaction; a pipeline card's "jump to this stage" continues to work as a same-page
  anchor when you're already on that view, and as a real navigation (`?stage=N`) when you're not.
- Skill-gap blockers stay in Notices, rendered wherever the plan is shown (both the pipeline view
  and the blueprints fold read them off the same `d.skill_gaps`) — a blocker that stops a stage
  needs to be visible from whichever section the user actually opened.

## Reactions' split

Reactions already has the shape half-built; this is the delta to bring it in line with the same
three-kind pattern rather than leaving it as three independent folds:

- **Landing dashboard**: `rxMetricsContent` + `rxDashboardContent` (the pending-jobs / stage list) —
  unchanged, already the always-open pair.
- **Fold: "Formulas & shopping"** — merges the missing-formulas report (`reactions_missing_formulas`
  — the direct equivalent of Manufacturing's missing-blueprint warning) into the Shopping list fold,
  same badge-on-`<summary>` rule as Manufacturing's blueprints fold. Today the missing-formulas
  report lives inside the shopping list's own "formulas to acquire" section already (docs/
  reactions.md) — this is mostly a labelling/badge change, not a new data path.
- **Fold: "Orders"** and **Fold: "Currently running / adopt orphans"** — unchanged, already folds.
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
- **No new badge/alert component.** "Alert if missing" is a count badge on a `<details><summary>` —
  reuse whatever the admin-preview tag or the skill-gap warning already use for a small colored
  count, not a new pattern.
- **Routing cost is real and bounded.** Only Manufacturing gains one route
  (`/manufacturing/pipeline` or similar sub-page, alongside the existing `TAB_SUBPAGES` model PI
  Planner already uses for Find Buildables / Refill). Reactions gains no new route at all under this
  proposal — everything it needed was already a fold.

## Implementation notes

- **Routing**: CLAUDE.md's checklist applies — `index.html`'s panel, `TAB_SLUGS`/`SPA_PAGES` in
  `app/main.py`, the nav button, and `test_routing.py`'s four-list check, for the one new
  Manufacturing sub-page. Reactions needs none of this.
- **One shared helper, not two copies**: the "fold with a count badge on the summary" idiom should
  be a single small function (`_pp_foldSummary(title, count, itemNoun)` or similar) used by both
  pages' JS, so a future third page inherits the same look instead of a third copy drifting from the
  first two. This is the one piece of shared code this proposal actually calls for — everything else
  is a per-page rearrangement of sections that already exist.
- **Order of work, if this is approved**: Reactions first (smaller, mostly labelling), to prove the
  shared fold-badge helper before Manufacturing's larger move (the new pipeline route). Doing the
  bigger one first would mean debugging the shared helper and the new route at the same time.
