# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-16.

---

## 34. Industry is too big to read — split it and simplify it (2026-08-16)

**Frontend: SHIPPED 2026-08-16.** `static/industry.js` (4,705 lines) is now ten files of 327-679,
split along [docs/industry-workflow.md](docs/industry-workflow.md)'s steps rather than by size:
`industry.js` (tab shell, source pickers, status card + plan cache), `-setup` (step 0, assets,
stock, slots), `-blueprints`, `-plan` (step 2 inputs), `-shopping` (tier/stage model + buy side),
`-steps` (build tree, done state, step list, pipeline), `-render` (the two plan renderers + notice
stack), `-queue` (steps 3 + 8), `-running` (steps 5-7), `-rules` (Build rules).

How it was done, because the method is the reusable part: the split was written down and reviewed
by two agents BEFORE a line moved, which is what caught the two defects worth catching — a line
range claimed by two files (the `DOMContentLoaded` block, which would have double-bound the speed
toggle and fired two plan requests per flip) and three boundaries that would have orphaned a
comment from the function it documents. The move itself was mechanical and verified as a pure move:
every line lands in exactly one file, byte-identical to its source range.

Then the simplification pass, all behaviour-preserving: deleted `indForceBuildType` (no caller in
JS, HTML or tests — superseded by `_indForceBuildMany`); one reader each for the source picker
(`_indSourceKeys`) and the queue's order (`_indOrdersByRank`), closing two "two readers can drift"
holes; `_indRestoreControls` / `_indRestoreNum`, `_indSearchRowsHtml`, `_indSsoPopup` for the
duplicated trios; the inner builders of the four biggest renderers lifted to top level
(`_indOrderChipHtml`, `_indPipeBuildCard`/`_indPipeBuyCard`, `_indInstallJobHtml`/`_indInstallCharHtml`);
and `_indStageModelForPlan` memoised on plan identity, which stops `_indStepsHtml` walking the whole
recipe tree once per stage.

Verified per commit: `test_industry.py` (1055 checks), `test_routing.py`, `test_routing_client.js`,
`node scripts/lint_js.mjs`, `node --check`, zero NUL bytes, and — for each renderer lift — a
literal-by-literal diff of the emitted HTML against the pre-refactor function. **Note for anyone
running these locally: `static/` is NOT bind-mounted into the container, so
`docker compose cp static web:/srv/app/` first or the source-level checks read the image's baked
copy and pass against the old file.**

**Still open — the backend's three.** `schedule.py` 2,056, `blueprints.py` 1,517, `graph.py` 1,383.
`schedule.py` is I/O-free and the cleanest to cut; `blueprints.py` and `graph.py` carry the evidence
layer Reactions imports (`formula_print_floor`), so any move has to keep the import graph acyclic —
that constraint is documented in [docs/reactions.md](docs/reactions.md) and is real, not a
preference. Same bar as the frontend half: nothing changes, one commit per cut, tests between.

**Adjacent, deliberately not in scope.** `app/reactions/jobs.py` is **3,920 lines** and
`static/reactions.js` is 3,936 — same shape, same argument, different service. The frontend seams
above did generalise, so a sibling item for Reactions is now a reasonable thing to open; do not
widen this one into it.

### 34a. Three things the review found that a refactor may not fix

Each changes behaviour, so each is its own item rather than a rider on the one above.

1. **`indPrioSpeed` is double-wired.** `static/index.html:1673` has `onchange="indOnPrioSpeed()"`
   AND `industry-plan.js` adds a `change` listener on the same element; both call `indRunPlan()`.
   Flipping the speed toggle with a plan on screen fires **two `POST /api/industry/plan`**, racing
   over which response paints. Fix: drop one. Behaviour-visible, hence not done under §34.
2. **The preview modal never sets `_indLastPlan`.** It is assigned in exactly two places, both on
   the queue path. So `_indStageModelForPlan()` — called from `_indStepsHtml` during a *preview*
   render — reasons about the QUEUE's plan. With an empty queue it returns empty cols and no
   stage-mark button appears, which is probably why nobody noticed; after a queue plan has
   rendered, the preview's "mark stage done" buttons are gated on the wrong plan's stages.
   **Reproduce before fixing** — this is the shape §36's shopping-list report may also be.
3. **`_indStepsHtml` is still 100 lines** and its natural cut (the stage-aggregation loop) is
   blocked by `test_industry.py`, which slices the function body up to the next `\nfunction ` and
   asserts three assignments are inside it. Splitting it needs those assertions rewritten to follow
   the code — a test change, which §34 forbade itself. Worth doing, with the mutation check
   (reintroduce the bug, watch it go red) that the assertions were written for.

## 35. Take two controls off the Reactions card (2026-08-16)

**What.** Two things the card offers that the user does not want offered there:

1. **"Come back every N days"** — `_rxCadenceHtml` in `static/reactions.js` (~L598). Remove the
   control from the card. The number itself stays: it is one stored setting
   (`/api/reactions/cadence`, `app/reactions/settings.py:426-445`) and the first-run wizard's
   `wizRCadence` dropdown is already a view of it (`static/index.html:702-707` says so in a
   comment). **User's words:** *"That configuration is set in the settings and just duplicates and
   adds a knob the user doesn't need."*
2. **"Advanced: full opportunity list"** — the fold-out at `static/index.html:659`. **Hide it**, not
   delete it: *"that is not a thing i want to offer the users right now."* The
   `/api/reactions/opportunities` endpoint and everything behind it stays.

**Why it's open.** Both are rule 3 (the best UI is read-only) applied to a surface that grew knobs.
Neither is a math change — the cadence ceiling still applies to planning, it just stops being
adjustable from the card.

**First concrete step.** Confirm removing the card's input leaves a way to reach the number for an
account past onboarding — if the wizard is the only other home, the setting needs a home in
Settings before the card's copy comes out, or the value becomes unreachable. That question decides
whether this is one commit or two. Check what `_rxSyncWizardCadence` keeps in step before cutting.

## 36. The Industry shopping list should count the customer orders, not sit beside them (2026-08-16)

**What.** With nothing queued but a customer order, the shopping list still shows a list —
apparently of something other than what the user actually has to build. **It should include the
customer orders by default, with a control to turn them OFF**, rather than the list being something
separate that can be hidden.

**Why it's open.** Reported from live use, 2026-08-16. A shopping list that does not answer "what do
I buy for the work in front of me" is worse than none: the builder has to work out which of it is
real. Default-on with an opt-out is the rule-3 shape — the common case needs no click.

**First concrete step.** Reproduce first and say what the list is currently made of: run a plan with
one customer order queued and nothing else, and read what `_indShoppingSections` /
`_indShopStageData` (`static/industry-shopping.js`) were handed. The bug may be that the list is
built from the ACCOUNT's plan rather than the order's — that shape is already known on the
`_indLastPlan` path (see the note under §34's follow-ups). Do not add the toggle until the
default-on list is correct; a control over a wrong list is two problems.

## Nothing else open

The rest of the backlog is §34, §35 and §36 as of 2026-08-16. §18b (config export/import) and §19 (URL routing,
including the deep links that carry an id) both closed that day — §19's last piece, deep-linking a
colony, was closed as **won't build** rather than shipped, and the reasoning is in the archive.

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 ("the browser test is not something I want us to do"). Routing is pinned by `test_routing_client.js` (runs the router for real) plus
source-level checks; live-browser bugs stay the user's to catch. Don't propose a headless-browser
suite again.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
