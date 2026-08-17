# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-17.

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

## 35. Take two controls off the Reactions card — SHIPPED 2026-08-17

**Both done, no flag** (rule 2 is about NEW features; this removes surface from existing ones).

1. **"Come back every N days" is off the card.** The number now lives in exactly two places, which
   is what the user asked for: *"I don't mind it being on the onboarding wizard and then in
   settings. It's a logical (familiar at least) place for a setting."* Settings → General →
   "Reactions — how often you play" (`#genCadenceSubsec`), and the first-run wizard's "Run on a…".
   `_rxPaintCadenceSetting` replaces `_rxCadenceHtml`, and both writers (`_rxSaveCadence`,
   `_rxLoadCadence`) keep the Settings row and the wizard dropdown in step the way the wizard
   already was. The Settings section fetches the value itself if the Reactions tab was never
   opened, and hides the whole subsection when the account has no cadence surface — so nobody sees
   a dead control. Dead `.rx-cadence` CSS removed with it.
2. **"Advanced: full opportunity list" is hidden**, not deleted — one `style="display:none"` on
   `#rxAdvancedCard`, with a comment saying so. `/api/reactions/opportunities`,
   `_onRxAdvancedToggle` and `_rxLoadAdvancedTable` are untouched, so bringing it back is deleting
   an attribute. Free side effect: the fold-out was lazy (it only computed when open), so hiding it
   also removes the tab's single most expensive computation from every account that had expanded it.

**Verified:** `test_reactions.py`, `test_routing.py`, `test_routing_client.js` and the JS lint all
green; the served page carries both changes.

**Where the delay came from, since it is worth not repeating:** this was written up on 2026-08-16
and then not built — "start building" landed mid-discussion of §37 and was taken to mean §37 alone.
The item sat complete-looking in the backlog with the decision recorded and no code behind it.

## 36. The shopping list listed things you already had — SHIPPED 2026-08-17

Behind `industry_sourced_counts` (default off).

**What was actually wrong**, after reproducing it in the code rather than from the report: the
premise in the original write-up was mistaken. Nothing ever excluded customer orders — every order
with `status='queued'` is planned together, labelled or not, and there was no toggle to find. Two
real gaps instead, and the user confirmed the second was what they were seeing:

1. **Notes were never read by the planner.** `pp_industry_sourced` — the ticks and pastes in the
   sourcing panel — was a checklist for the user and nothing else. Bound containers WERE already
   netted off (`plan_source_keys` / `_stock_for`); the notes were not.
2. **Stock never reduced anything you BUY.** `aggregate_demand` applies `on_hand` inside the
   `for tid in built` loop only (`schedule.py`) — it stops you re-building a component you hold,
   and has never touched a bought material. So half the fix could not come from the stock pool at
   all, which is why part 1 alone would not have closed the report.

**The fix, in two halves.** `noted_stock_excess` folds notes into the pool so the plan stops
BUILDING what you hold, and `_mark_already_held` annotates each shopping row with `have`/`to_buy`
so the list stops telling you to BUY it. The frontend shows what is left, marks a fully-covered row,
and — the part that actually matters — **multibuy copies the shortfall**, not the requirement.

**Two rules worth not breaking:**

* **A note and a box are two answers to one question: take the better, never the sum.** That is the
  sourcing panel's own rule (`_item_row`: `min(need, max(held, noted))`). Summing them would make
  the plan believe it had twice what it has and under-buy — the expensive direction.
* **Quantities change, money does not.** `qty` and `line_cost` stay the full requirement. The
  material is still consumed by the build, and a quote that quietly shrank whenever the builder
  happened to have stock would understate what the job costs to run. `sourcing.py` already says
  only one of these two lists may talk about money; this keeps it that way.

**Verified:** `test_industry.py` 1067 checks including a six-case table for the double-count rule
and a five-case one for the annotation, plus two mutation runs (drop the cap, sum instead of taking
the better) that each turn the suite red. `test_reactions.py`, `test_features.py`, `test_routing.py`,
`test_routing_client.js`, lint green.

**Still open:** the per-order plans path annotates from the queue-wide pool, so with
`per_order_plans` on the `have` figure is the queue's view rather than each order's. Correct for the
combined list that is actually rendered; revisit if per-order shopping lists ever get their own UI.

## 37. We alert too often, and the worst case is an alert the user cannot act on (2026-08-17)

**SHIPPED 2026-08-17 behind `alert_rescan_backoff`** (default off — roll out from Admin → Features).
Full design and the reasoning behind each guard:
[docs/platform.md](docs/platform.md#check-before-nagging-and-nag-less-each-time-alert_rescan_backoff).

The reported bug: restart your PI in game, don't rescan, and the Discord alert fires every 2h
forever. What shipped:

1. **A due alert triggers a rescan of the one colony it is about**, then the alert is recomputed and
   only what survives is sent. Fix the problem in game and the alert simply never arrives.
2. **Repeats back off** — the kind's base interval doubles per consecutive send, capped at 12h,
   derived from `pp_notification_log` and reset when the alert resolves. The **first** send is never
   delayed; only repeats are.
3. **A colony that could not be read is held back rather than reported off stale data** (user's
   call), with an amber dot on the character card for the transient case, distinct from the red
   "re-add this character".

**Keeping ESI load low was the binding constraint** (*"bashing them won't help me"*): single-planet
reads, an `esi_expires` gate, a dead-token filter, and a hard per-tick budget. One read per alert
SEND, and sends back off — about four reads on day one for an ignored problem, two a day after,
against ~96 per character per day for the blanket-timer design that was rejected.

**Found and fixed on the way, as its own commit:** `_refresh_token` wrote `NULL` into
`pp_characters.refresh_token`, which is `TEXT NOT NULL` — the IntegrityError was swallowed by its
own outer `except`, so a revoked character was never marked dead and kept a green dot forever. The
docstring above the code claimed this had been fixed; it had not. It was a prerequisite here,
because the dead-token filter has nothing to filter on until a dead token is recorded as dead.

**Verified:** `test_alert_cadence.py` (44 checks, no ESI — the scan function is injected, and
`_process_context` itself is driven end to end with a fake notifier), plus five mutation runs: the
chain reset, suppress-on-failure, the two ESI guards, the feature-flag gate and the suppression
actually being APPLIED each fail the suite when broken. `test_alerts.py`, `test_features.py`,
`test_routing.py` and `test_epoch_precision.py` green; app boots and serves.

**A pre-ship review caught four things worth recording, because three of them were mine:**

1. **A failed colony detail read used to WIPE the colony row.** `_fetch_planets` swallowed the
   exception and fell through to an UPSERT that wrote `is_extractor=0` and NULLs over products,
   pads, sim state, storage and `esi_expires`, with a fresh `scanned_at`. Pre-existing — the hand
   rescan button has always done this — but this feature would have made it automatic and
   unattended. A failed read now writes nothing and is reported as `failed` in the return value.
   **This is also why `_default_scan` cannot key off `fetched`:** that counter increments BEFORE
   the detail request, so it counts attempts, not successes.
2. **The backoff counted log ROWS, and one send writes one row per channel.** A user with three
   channels hit the 12h cap on their second alert and could never reset the chain, because a gap of
   microseconds is never longer than any interval. Rows within `_SAME_SEND_WINDOW_S` are now one
   send.
3. **The scan budget was per context, not per tick** — it multiplied by the number of accounts.
   Now a module-level counter reset once per run.
4. **The cost per colony was 4 ESI requests, not 1**, because `universe/names/` and
   `universe/planets/{pid}/` were unconditional. Both answers are immutable, so both are now
   skipped when the DB already has them: 2 requests in the steady state, 40 per tick for the whole
   app at the ceiling. The hand rescan got the same saving for free.

### 37a. Still open

- **The `reaction_*` kinds keep the old cadence.** They carry no `planet_id` and read industry jobs,
  a different ESI path. Same argument applies to them; it is a second cut, not a widening of this
  one.
- **Nothing measures the win yet.** The rung a send went out on is derivable from the log but not
  recorded, so "how many alerts were merely stale" cannot be answered from the data. Worth adding
  before re-tuning the curve — backing off aggressively on an alert now known to be TRUE is a
  different trade from backing off on a probable ghost.
- **Watch the first week on the rung**, specifically: whether any character sits amber for long
  (transient scan failures that never recover would silently pause its alerts), and whether the
  per-tick budget is ever actually reached.
- **The scan budget is per PROCESS, not per app.** Prod runs 6 (2 replicas x 3 workers), each with
  its own module global. The advisory lock serializes them and `_recently_notified` empties the
  second runner's alert list before it reaches a scan, so the real spend stays near the ceiling of
  one process — but the constant's own guarantee is 20 per process. If that ever needs to be a
  true app-wide cap it has to move into the DB alongside the job lease.
- **Clock skew is unguarded.** The `esi_expires` gate compares ESI's absolute `Expires` against
  local `time.time()`, so a pod whose clock runs fast buys premature requests. Low risk on NTP'd
  nodes and not worth a mechanism today, but it is the one way the never-query-before-`Expires`
  rule could be broken without a code change.

## Nothing else open

The rest of the backlog is §34 (backend half), §35, §36 and §37 as of 2026-08-17. §18b (config export/import) and §19 (URL routing,
including the deep links that carry an id) both closed that day — §19's last piece, deep-linking a
colony, was closed as **won't build** rather than shipped, and the reasoning is in the archive.

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 ("the browser test is not something I want us to do"). Routing is pinned by `test_routing_client.js` (runs the router for real) plus
source-level checks; live-browser bugs stay the user's to catch. Don't propose a headless-browser
suite again.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
