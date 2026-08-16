# Industry — the workflow end to end (developer map)

The whole Industry flow in one place: the path a builder walks, and the modules, endpoints and
tables each step goes through. This file is a **map**, not a rationale — *why* each piece works the
way it does lives in [industry-planning.md](industry-planning.md) and
[industry-running.md](industry-running.md), and this file links out to them rather than restating
them. The same flow written for the user: [industry-workflow-user.md](industry-workflow-user.md).

Written 2026-08-05 from a read of `app/industry/` (22 modules, ~9.2k lines) and
`static/industry.js` (~3.6k lines). It describes what is there, without judgement; the one
judgement-bearing section is fenced at the bottom under **Observations**.

**Updated 2026-08-16:** the frontend is now ten files (`static/industry.js` plus
`industry-{setup,blueprints,plan,shopping,steps,render,queue,running,rules}.js`), split along the
nine steps below — so the step you are reading names the file to open. TODO §34.

Find a section: `grep -n '^## ' docs/industry-workflow.md`.

---

## Contents

- **The shape of it** — the two routers, the one resolver, the three plan shapes
- **Step 0 — First use** — the onboarding gate and what it writes
- **Step 1 — Tab open** — the load order, and what refreshes itself
- **Step 2 — Plan a product** — search → `/plan` → the rendered blocks
- **Step 3 — Queue it** — what an order row carries
- **Step 4 — The build page** — one request, five views of it
- **Step 5 — Sourcing** — per-order material state
- **Step 6 — Install** — the checklist, and who is eligible
- **Step 7 — Progress** — three signals, tracked per type
- **Step 8 — Quote and share** — margin snapshots and the public router
- **Step 9 — Deliver and clear**
- **Endpoint index** — every route, its module, and its caller
- **Tables** — what each one holds
- **Feature flags** — which step each one gates
- **Tests**
- **Observations** — fenced; input for the item-13 audit, not part of the description

---

## The shape of it

`app/industry/__init__.py` exports one name, `router`, plus `public_router`; submodule imports
exist for the side effect of registering their `@router` decorators. Both routers live in
`_router.py` so submodules can register without a circular import through the package.

- **`router`** carries `Depends(require_page("industry"))` — a no-op unless a group manager has
  restricted their members.
- **`public_router`** carries the customer build-status link only. It is a separate router because
  FastAPI router-level dependencies cannot be waived per endpoint, and a customer has no account
  and therefore no group.

Every plan path passes through **one resolver**, `graph.prepare_plan_inputs(ctx, targets, opts)`,
which returns a `PlanInputs`: both recipe graphs (`load_manufacturing_graph`,
`load_reaction_graph`), the reachable type ids, names + `group_id` (the SDE's only taxonomy),
market prices (`app.markets.resolve_market_data`), adjusted prices, a resolved `BuildParams`, and
the slot pools. Inside it, in order: account build options are applied over the request
(`settings.apply_account_build_options`), targets are validated, prices are fetched, `BuildParams`
is resolved, the reaction policy and blacklist carve-outs are set, blueprint acquisition costs and
contract-derived ME/TE are folded in (`bpc.acquisition_costs`, `representative_me_te`), user ME/TE
overrides win last, and **then** per-job build sites are routed (`routing.resolve_job_sites`),
because routing reads the resolved params.

Three plan shapes come out of that one resolver:

| Shape | Entry | Core call | Used by |
|---|---|---|---|
| One product | `POST /api/industry/plan` | `schedule.plan_queue` on a single target + `graph.build_plan` for the tree | the preview modal |
| Whole queue, shared batch | `POST /api/industry/queue-plan` → `orders._run_queue_plan` | `schedule.plan_queue` over combined targets | the build page |
| Queue, planned apart | same endpoint, `per_order_plans` on | `schedule.plan_queue_per_order` | per-order costs, `sourcing` |

`schedule.py` is deliberately I/O-free: prebuilt graphs and params in, jobs and a schedule out. It
owns no endpoint, DB handle or market lookup.

---

## Step 0 — First use

**UI.** `onIndustryTabOpen` → `indApplyGate(hasStructure)`. With `_indOnboarded` false it renders
`_indRenderWizard`: a three-step blocking screen (facility — required and pre-answered; characters
& slots — informational; build system & fees — folded). `indWizSave` writes the settings body and
completes onboarding, then re-runs `onIndustryTabOpen`.

**Backend.** `settings.py` — `PUT /api/industry/settings`, `POST /api/industry/onboarding/complete`,
`POST /api/industry/onboarding/reset` (admin, for replaying the screen). Stored in
`pp_industry_settings` (incl. `onboarded`).

**Cross-tab dependency.** Describing a real build structure happens in **Structures & Markets**
(`pp_markets` build columns, `structures.py`), shared with Reactions. The wizard links out to it;
`indPopulateFacility` reads the resulting facility map, and `s:`-prefixed keys are the account's own
structures.

Post-onboarding, `indApplyGate` degrades to a dismissible nudge (`localStorage.indFacilityNudge`).

## Step 1 — Tab open

`onIndustryTabOpen`, in order: `indPopulateFacility` → restore saved settings
(`_indApplySavedSettings`, then `_indRestoreControls` — all three knobs, with saving suppressed) → seed the account row if nothing
has ever saved it → `indApplyGate` → `indLoadSetupSummary` + `indLoadLifetime` (fire-and-forget) →
`await indLoadBlacklist` and `await indLoadReactionPolicy` (awaited: the shopping list and decision
strip render from them) → `await indRefreshStatus` → `indRefreshStaleCaches` after the paint.

- `GET /api/industry/slots` (`slots.py`) — pools from `pp_char_skills`: 1 base + 1/level Mass
  Production + Advanced Mass Production, ≤11; the reaction pool likewise.
- `GET /api/industry/blueprints` (`blueprints.py`) — owned ME/TE from `pp_char_blueprints`.
- `GET /api/industry/lifetime` (`jobs.py`) — forward-only turnover/profit off the completion
  ledgers; `used: false` hides the tiles entirely.
- `POST /api/industry/refresh-stale` (`freshness.py`) — per-cache thresholds (jobs, blueprints,
  assets), one attempt per `_MIN_GAP` whatever the outcome, adds no polling.

## Step 2 — Plan a product

`GET /api/industry/search` (`graph.py`) backs the picker; `indRunPlan` posts
`POST /api/industry/plan`. `_indRenderPlan` composes, in this order:

1. `_indMetricTiles(d.metrics)`
2. `_indNotices` — the notice stack (skill basis, cost basis, missing blueprints, print limits,
   copy shortfalls). The bar a notice has to clear is in
   [industry-planning.md](industry-planning.md#the-build-pages-notice-stack-trimmed-2026-08-04).
3. `_indMarginalBar` — the borderline components and `indBuildAllAbove`
   (`POST /api/industry/orders/force-above`, iterated to a fixpoint over `_FORCE_ROUNDS`).
4. `_indReactionPolicyBar` — from `GET/POST /api/industry/reaction-policy` (`settings.py`,
   categories from `categories.REACTION_CATEGORIES`; the UI never hardcodes the labels).
5. `_indStepsHtml` → 6. `_indPipelineHtml` → 7. shopping list (`_indShoppingSections`,
   `indCopyMultibuy`) → leftovers → debug tree.

The slider's live readout comes from `POST /api/industry/plan_sweep` — the whole curve once, rather
than a replan per pixel (`_indLoadSweep`, `_indRenderMarginalLive`).

Options are debounced to `PUT /api/industry/settings` (`_indSaveSettings`), guarded by
`_indRestoringSettings` so seeding the form never writes browser state over the account's.

## Step 3 — Queue it

`indAddToQueue` → `POST /api/industry/orders` (`orders.py`) with `product_type_id`, `quantity`,
`label`, `force_build_ids`, `me_te_overrides`, `margin_pct`, and either `source_keys` (flag on) or
`source_key`. A paste in the plan form is posted separately to
`POST /api/industry/orders/{id}/sourcing/paste` — it is per-order material state, not planner stock.
Then `indClosePlanner` → `indRefreshStatus`, which re-plans the whole queue.

Order rows live in `pp_industry_orders` (`status='queued'`, `priority DESC, id` = FIFO rank).

## Step 4 — The build page

`indRefreshStatus`: `GET /api/industry/orders` first (so the sessionStorage plan cache can be
matched against the actual queue — `_indQueueSig` keys on order ids, quantities, overrides **and**
the `?v=` build stamp), paint the cached plan immediately if it matches and is under 15 minutes old,
then `POST /api/industry/queue-plan`.

That one response carries five views:

- the plan itself (metrics, requirements, schedule, `targets` with per-order rank and finish hours),
- `trees` — one recipe tree per ordered product, because `plan_queue` returns aggregated demand and
  the UI derives its stages from structure,
- `install` — the start-now checklist, inline because it is a view *of this plan*,
- `progress` — likewise, computed off `_full` (the same queue planned with no stock netted off),
- `skill_gaps` / `skill_time_basis` / `cost_basis`.

`_indPaintStatus` splits painting from fetching so a hand done-mark repaints without re-planning.

`_run_queue_plan` details worth knowing: overrides ride on the order that carried them but the
aggregated path **unions** them (one shared batch per component can only be built one way);
`per_order` mode does not union, because each order carries its own; `_blend_margin` re-prices the
shared batch by apportioning each order's standalone cost, and is skipped in `per_order` mode where
a real per-order cost exists.

Stock resolution is `_stock_for` / `_order_stock` / `plan_source_keys` — a curated order spends its
own boxes, an uncurated one draws the account tick list, and the union is what the aggregated plan
is entitled to.

Queue position: `POST /api/industry/orders/reorder` (`indOpenOrder` / `_indRenderOrderList`).
Editing: `PATCH /api/industry/orders/{id}`. Removal: `DELETE`.

## Step 5 — Sourcing

`indOpenSourcing(orderId)` → `GET /api/industry/orders/{id}/sourcing` (`sourcing.py`). Two signals
combined, higher wins per material: the **bound source** (containers named on the order, contents
read from the asset cache) and a **pasted inventory** (`POST .../sourcing/paste`, replaces — it is a
snapshot). A single line can be corrected with `POST .../sourcing`.

Requirements here are the order planned **on its own** (its quantity, its overrides), not the
queue's shared batch — so the sum across orders can legitimately exceed what the queue builds.

Sources come from `assets.py`: `GET /api/industry/assets`, `POST /assets/refresh`,
`POST /assets/refresh-corp` (Director-gated; a 403 is the normal answer for a non-director),
`POST /assets/paste`, `POST /assets/sources`, `DELETE /assets/sources/{key}`, and named sets via
`GET/POST/DELETE /api/industry/source-sets`. Sources are flat and opt-in; being wrong here is
asymmetric, so nothing counts until ticked. Tables: `pp_asset_sources`, `pp_asset_stock`,
`pp_source_sets`, `pp_industry_sourced`, `pp_locations`.

## Step 6 — Install

`indRenderInstall(d.install)` — passed in, never fetched, because fetching it re-planned the queue.
`install_block(ctx, res)` in `orders.py` builds it from the plan's ready wave plus free slots
(`jobs.running_counts`, `slots._slot_pool`). `_indGroupJobs` collapses a character's jobs to one line
per product and buckets them by run count; `_indSlotRow`/`_indSlotPips` draw the two pools
separately; `why` explains a job held short (bound by a consumer, or matched to the plan's pace).

Eligibility: `skills.analyze_plan_skills` returns both the gap report and an `eligibility` map;
`schedule.assign_characters` uses it so nobody is named for a job they cannot install. The map is
popped before the response leaves the endpoint (sets of character ids, internal only).

`POST /api/industry/jobs/refresh` (`jobs.py`, via `char_cache.py`) pulls running jobs;
`GET /api/industry/jobs` backs the "In progress" list.

## Step 7 — Progress

`progress.py`. Tracked **per type**, rolled up to orders through each order's end product — ESI
jobs cannot be tagged with our order id, and the queue aggregates demand on purpose, so per-order
attribution is ambiguous by construction.

Three signals: the forward-only completion ledgers (`pp_industry_completions`,
`pp_reaction_completions`), the running-job caches, and manual marks
(`POST /api/industry/progress/done` → `pp_industry_manual_done`). A manual mark never overrides a
higher measured signal and never writes to the ledgers.

`GET /api/industry/progress?simulate=0..100` fabricates state for previewing the UI; it writes
nothing.

Marking done invalidates the account's share caches (`shares.invalidate_context_shares`).

## Step 8 — Quote and share

Margin is snapshotted per order (`pp_industry_orders.margin_pct`); the queue's `price` is blended
(`_blend_margin`) and `margin_mixed` says so. The frontend must not recompute the queue price from
the planner slider.

`shares.py`: `POST /api/industry/orders/{id}/share` (idempotent — also "show me the link I already
made"), `GET`, `DELETE` (revoke). The public side is
`GET /api/industry/build-status/{share_id}` on `public_router`, served by the `/b/{id}` page route
in `main.py` with `static/build.html`. The payload is assembled field by field, never filtered from
the plan: product, quantity, label, stage names + run counts, percentage, ETA, quoted price — and no
character names, systems, structures, other orders, or **cost of any kind**. Every successful render
snapshots onto the share row, so a finished-and-cleared order still serves its last state
(`archived`) rather than 404ing. Table: `pp_industry_shares`.

## Step 9 — Deliver and clear

`DELETE /api/industry/orders/{id}` and the queue re-plans. The share link survives on its snapshot.
Completed jobs feed `GET /api/industry/lifetime`.

---

## Endpoint index

| Endpoint | Module | Frontend caller |
|---|---|---|
| `GET /api/industry/search` | graph | `_indSearch` |
| `POST /api/industry/plan` | graph | `indRunPlan` |
| `POST /api/industry/plan_sweep` | graph | `_indLoadSweep` |
| `GET /api/industry/slots` | slots | `indLoadSlots` |
| `GET /api/industry/blueprints`, `POST /blueprints/refresh` | blueprints | `indLoadBlueprints`, `indRefreshBlueprints` |
| `GET /api/industry/jobs`, `POST /jobs/refresh` | jobs | `indLoadRunning`, `indRefreshJobs` |
| `GET /api/industry/lifetime` | jobs | `indLoadLifetime` |
| `POST /api/industry/orders`, `GET`, `PATCH /{id}`, `DELETE /{id}`, `POST /reorder` | orders | the order chips + order modal |
| `POST /api/industry/queue-plan` | orders | `indRefreshStatus` |
| `POST /api/industry/orders/force-above` | orders | `indBuildAllAbove` |
| `POST /api/industry/queue-plan/compare` | orders | — (endpoint only; TODO 2f-residual #3) |
| `GET/POST /api/industry/queue-plan/packing` | orders | — (diagnostic by design) |
| `GET /api/industry/assets`, `POST /assets/refresh`, `/assets/refresh-corp`, `/assets/paste`, `/assets/sources`, `DELETE /assets/sources/{key}` | assets | Settings → Blueprints & formulas → Stock on hand |
| `GET/POST /api/industry/source-sets`, `DELETE /{set_id}` | assets | sourcing panel |
| `GET /api/industry/bpc`, `POST /bpc/scan` | bpc | `indLoadBpcPrices` |
| `GET /api/industry/progress`, `POST /progress/done` | progress | `indLoadProgress`, `indCycleDone` |
| `GET/PUT /api/industry/settings` | settings | `_indApplySavedSettings`, `_indSaveSettings` |
| `POST /api/industry/onboarding/complete`, `/reset` | settings | `indWizSave`, `indResetOnboarding` |
| `GET/POST /api/industry/blacklist` | settings | `indLoadBlacklist`, `indBlacklist` |
| `GET/POST /api/industry/reaction-policy` | settings | `indLoadReactionPolicy`, `indSetReactionPolicy` |
| `GET/POST /api/industry/per-order-plans` | settings | — (endpoint only; TODO 2f-residual #3) |
| `GET/POST/DELETE /api/industry/orders/{id}/share` | shares | `indShareOrder`, `indRevokeShare` |
| `GET /api/industry/build-status/{share_id}` | shares (public) | `static/build.html` via `/b/{id}` |
| `GET/POST /api/industry/orders/{id}/sourcing`, `POST /sourcing/paste` | sourcing | sourcing panel |
| `POST /api/industry/refresh-stale` | freshness | `indRefreshStaleCaches` |

Supporting modules with no endpoint of their own: `schedule` (demand aggregation + the slot
scheduler), `structures` (rig families, ME/TE and fee inputs), `routing` (per-job build site),
`categories` (reaction taxonomy), `char_cache` (the shared per-character ESI snapshot loop),
`_router` (the two routers).

## Tables

| Table | Holds |
|---|---|
| `pp_industry_orders` | the build queue: product, quantity, label, priority, margin, force-build ids, ME/TE overrides, source key(s), `sources_owned`, `build_reactions`, status |
| `pp_industry_settings` | per-account build options + `onboarded` |
| `pp_industry_sourced` | per (order, material) hand-noted quantities |
| `pp_industry_manual_done` | per-type hand done-marks |
| `pp_industry_shares` | customer link ids + the archived payload snapshot |
| `pp_industry_completions`, `pp_reaction_completions` | forward-only completion ledgers (lifetime turnover/profit, progress) |
| `pp_char_blueprints`, `pp_char_manufacturing_jobs`, `pp_char_industry_jobs`, `pp_char_skills` | per-character ESI caches |
| `pp_asset_sources`, `pp_asset_stock`, `pp_source_sets`, `pp_locations` | stock sources, their contents, named sets, where a container is |
| `pp_bpc_scan`, `pp_bpc_observations` | the public-contract blueprint index and its history |
| `pp_markets` | doubles as the structure registry (build columns, rig tiers) |
| `pp_characters` | account ↔ character mapping every module scopes by |

## Feature flags

All default `False` (admin-preview) per rule 2. `industry` gates the tab itself; everything below
gates one step of the path above.

| Flag | Step it changes |
|---|---|
| `industry` | the tab |
| `required_skills` | 6 — install eligibility and the skill report |
| `industry_install_skill_aware` | 6 — the checklist agrees with the schedule about who can install |
| `industry_share` | 8 |
| `industry_manual_done` | 7 |
| `industry_blacklist` | 2 |
| `industry_corp_assets` | 5 |
| `industry_sourcing` | 5 |
| `industry_plan_sources` | 3 + 5 — a build owns its containers |
| `industry_rig_routing` | 2 — per-job build site |
| `industry_default_build_system` | 0/2 — assume a build system when none is set |
| `industry_group_structures` | 0/2 — alliance-shared structures as suggestions |
| `industry_per_order_plans` | 4 — plan each order on its own |
| `industry_reaction_policy` | 2 — which reactions a build runs |

## Tests

`test_industry.py` is the main suite. Adjacent: `test_required_skills.py`,
`test_skill_time_mults.py`, `test_cost_basis.py`, `test_job_summary.py`,
`test_group_structures.py`. Frontend behaviour has no browser harness (TODO 2e-residual);
`scripts/lint_js.mjs` / the `lint-js` CI job is the only automated guard over `static/industry*.js`.

---

## Observations

> **Fenced deliberately.** Everything above describes what exists. This section is what a read of it
> turned up that is worth *deciding* about — collected here rather than mixed into the description,
> and intended as input for the item-13 manifestos and the second pass of item 12. Nothing here is
> a recommendation yet.

1. **`docs/code-layout.md` does not mention `app/industry/` at all.** The file whose stated job is
   "where things live" maps `app/planner*.py` module by module and skips 22 industry modules and
   9.2k lines. The endpoint index above is currently the only such map.
2. **Six endpoints had no frontend caller; three were residue and are gone (2026-08-07, TODO 16).**
   `to-install` (superseded by the inline `install` block), `skill-coverage` and `skill-advisor`
   (engine, endpoint, flag and `app/industry/advisor.py` all deleted — the rendering had been
   removed on purpose months earlier and nothing replaced it) were removed. `queue-plan/compare`
   and `per-order-plans` (TODO 2f-residual #3) and `queue-plan/packing` (diagnostic by design) are
   deliberate and stay.
3. **Stock is expressed in four places** — the plan modal's "Materials from", the sourcing panel's
   "Pulling from", Settings → Blueprints & formulas → Stock on hand's tick list, and saved source sets — and under two
   different ownership models that coexist behind `industry_plan_sources` (account-wide tick list
   vs. a build owning its boxes). `plan_source_keys` exists to reconcile them per request.
4. **Setup for the single most cost-relevant input is in another tab.** The facility drives every
   material and time figure, but describing a real structure happens in Structures & Markets, shared
   with Reactions. The wizard links out; the tab then nudges about it indefinitely.
5. **Fifteen flags, and on prod all fifteen sit at `testers` — none is public, including `industry`
   itself.** So the path is *not* fragmented in practice: a tester sees the whole tab, everyone else
   sees none of it. What the flags cost is fifteen code paths carrying an off state, not fifteen
   different user experiences. (An earlier draft of this line claimed the opposite, reasoning from
   the code defaults; the live read of `GET /api/features` says otherwise — which is what CLAUDE.md
   rule 1 warns about. See [industry-audit-2026-08.md](industry-audit-2026-08.md).)
6. **Four settings exist at both order and account level**, each with its own reconciliation rule:
   margin (per-order snapshot, blended for the queue), force-build (per order, unioned queue-wide
   unless planning apart), ME/TE overrides (same), and reaction policy (account rule, per-order
   `build_reactions` exception). The rules are individually justified and collectively hard to state
   in one sentence.
7. **The sourcing panel's numbers can legitimately disagree with the shopping list's** — one plans
   the order alone, the other the shared batch. This is documented in `sourcing.py` and surfaced in
   the panel's help text, but a builder comparing the two figures has to know why.
8. **The container-as-output gap is visible in the flow** (TODO 2f-residual #1): boxes are bound as
   an *input* record at steps 3 and 5, and nothing anywhere in steps 6–9 says where a job's output
   lands.
9. **Nothing in the product states the path.** The How-it-works page is Planetary Industry only
   (one mention of "Planetary Industry", no mention of manufacturing or reactions); the industry
   onboarding covers setup and stops. Steps 1–9 exist nowhere the user can read them — which is
   what the companion user-facing doc is for, and why it has no home in the UI yet.
10. **The frontend was one 3.6k-line file** carrying the planner modal, the build page, the
    checklist, sourcing, shares, the order modal and the setup modal, against 22 backend modules.
    Closed 2026-08-16 (TODO §34): it is now ten `static/industry*.js` files split along the steps
    above — `industry.js` (shell + status), `-setup`, `-blueprints`, `-plan`, `-shopping`, `-steps`,
    `-render`, `-queue`, `-running`, `-rules`. Which file a function is in is not pinned by any
    test; the source-level guards read them as one string (`_industry_js()` in `test_industry.py`).
