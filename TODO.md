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

**Backend: SHIPPED 2026-08-17.** All three are packages now, largest module 547 lines:

| Was | Now |
|---|---|
| `schedule.py` 2,056 | `schedule/` — `demand` 192, `splitting` 349, `tasks` 347, `scheduler` 183, `plan` 502, `per_order` 547 |
| `graph.py` 1,383 | `graph/` — `params` 323, `sde` 133, `costs` 257, `options` 258, `resolve` 332, `routes` 140 |
| `blueprints.py` 1,517 | `blueprints/` — `esi` 143, `manual` 507, `observed` 263, `paste` 493, `routes` 204 |

Each `__init__.py` re-exports **every** name the package defines, private ones included, so no
import anywhere changed — `app/reactions/graph.py` still imports `_fallback_build_system` and the
tests still reach for `_built_deps`, `_batch_key`, `_apply_kind_preference`.

**The cross-module imports were derived from the AST, not hand-listed** (the splitter is at
`/home/fredrik/.claude/jobs/*/tmp/pysplit.py`, worth rewriting if this is done again): each module
gets exactly the sibling names its code references, and the script refuses to emit a module that
needs something defined later. That is what makes "no import cycles" a checked property rather than
a claim.

**One genuine cycle had to be broken first, in its own commit:** `manual`'s
`_migrate_location_batches` called `_batch_key`, which lived beside the paste parser that imports
four names back from `manual`. `_batch_key` + `_PASTE_BATCH_DEFAULT` moved up into `esi`.

### What the split exposed — three tests that were not testing what they claimed

Each was passing before and would have kept passing wrongly:

1. **`_patch_db(B)` patched a package attribute nobody reads.** Four tests set
   `blueprints.get_connection` and then ran code whose submodules had imported `get_connection`
   into their own globals — so every call went to the REAL database and the tests still passed,
   exercising container state instead of their own fixture. `_patch_db` now walks a package's
   loaded submodules. `_patch_db_all` had documented this exact hazard for sibling modules; nothing
   had applied it to packages.
2. **`bp._manual_enabled` monkeypatching went inert** — three checks in
   `test_manual_blueprints.py` went red, which is the good outcome. The helper now patches every
   module binding the name, discovered by walking `sys.modules` rather than naming today's two.
3. **`G._default_system_on` / `_fallback_build_system`** — same shape in `test_cost_basis.py`;
   all three of its scenarios would have collapsed into one. Retargeted at `graph.options`.

Also: `inspect.getsource(<module>)` returns only `__init__.py` for a package, so
`test_industry.py`'s pin on `"build_pins_unapplied"` appearing twice became `0 != 2` and failed
loudly. Replaced with `_module_source`, the package-aware sibling of `_industry_js`.

**And one promise that was only ever a docstring is now checked:** `test_the_scheduler_stays_io_free`
asserts no `schedule/` submodule reaches for `get_connection`, `app.markets` or `@router`.

**Two review findings did NOT survive checking**, recorded so they are not re-raised: `blueprints.py`
was said to use `log` 36 times and use it **zero** times (the logger is defined and dead), and the
`graph/params.py` boundary correction to line 36 was right — that one was real, and cutting at 48
would have dropped `@dataclass` off `BuildParams` plus two constants Reactions imports.

**Verified:** 1,090 checks in `test_industry.py` plus `test_blueprint_paste`, `test_manual_blueprints`,
`test_manual_structures`, `test_reactions`, `test_features`, `test_routing`, `test_cost_basis` — all
green; every chunk proven verbatim against its source range; the app boots and serves, with
`/api/industry/plan`, `/api/industry/search`, `/api/industry/manual-blueprints` and
`/api/industry/blueprints/refresh` all answering 401 (route present, auth required) rather than 404.

**Note for the next reader:** `scripts/symbols.sh app/industry/schedule.py` no longer resolves — use
the DIRECTORY (`scripts/symbols.sh app/industry/schedule`), which maps the whole package.

**Still open:** `orders.py` is 1,057 and `assets.py` 1,004 — under the bar that opened this item, but
they are the next two if it comes back.

**Adjacent, deliberately not in scope.** `app/reactions/jobs.py` is **3,920 lines** and
`static/reactions.js` is 3,936 — same shape, same argument, different service. The frontend seams
above did generalise, so a sibling item for Reactions is now a reasonable thing to open; do not
widen this one into it.

### 34a. The three the refactor could not fix — CLOSED 2026-08-17

1. **`indPrioSpeed` fired two plan requests per flip — FIXED.** The checkbox carried
   `onchange="indOnPrioSpeed()"` in `index.html` AND a `change` listener added in a
   `DOMContentLoaded` block, both calling `indRunPlan()` under the same condition, racing over
   which response painted the card. The listener was the strict subset (the handler also saves the
   setting, refreshes the status card, and loads the sweep when no plan is on screen), so the
   listener went and the inline handler stayed.

2. **The preview modal DOES set `_indLastPlan` — the finding was wrong.** Verified rather than
   taken on trust: it is assigned in three places, and `indRunPlan` (`static/industry-plan.js:125`)
   is one of them, immediately before `_indRenderPlan`. Every build-page repaint likewise assigns
   it first (`industry.js:360`, `:374`) or passes `_indLastPlan` itself
   (`industry-steps.js:186`, `:192`). So `_indStageModelForPlan()` always reasons about the plan on
   screen, and there is nothing to fix. **Do not reopen without a reproduction** — the original
   report came from reading a 4,700-line file and missing an assignment in the middle of a long
   function.

3. **`_indStepsHtml` split — DONE, and the test that blocked it was vacuous.** The stage
   aggregation is now `_indStepStages(d, model)`; the renderer is handed the stages. The blocking
   assertions were rewritten to pin each half at its real home rather than requiring both to live
   in one function.

   **The part worth remembering:** mutating the header back to the bare start offset did NOT turn
   the old assertion red. It matched `s.longest` anywhere after the first `html +=` — and both
   numbers also appear in that span's `title`, so the check could never tell "renders it" from
   "mentions it in a tooltip". It was green against the exact defect it was written for. It now
   asserts on the element's TEXT, after the attributes, and fails when the header is reverted.

**Verified:** `test_industry.py` 1,069 checks, `test_routing_client.js`, lint; plus mutation runs
for both halves of the rewritten assertion.

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

- ~~**The `reaction_*` kinds keep the old cadence.**~~ **DONE 2026-08-17** (`alert_rescan_reactions`).
  Precisely: the BACKOFF already covered them (they carry `dedupe_id`, which is all
  `_consecutive_cooldown_h` needs) — what they were missing was the rescan. Now one
  `characters/{id}/industry/jobs/` read per CHARACTER, answering for all three kinds at once, with
  `fetched_at`/`_JOBS_CACHE_TTL` as the `esi_expires` equivalent and the opt-in jobs scope as a
  a skip (not a suppression) for a character that never granted the opt-in jobs scope. The
  personal read had to be made strict (`read_industry_jobs` raises
  where `fetch_industry_jobs` returns `[]`) because an empty job list is exactly what a *resolved*
  completed-reaction alert looks like — the §37 colony-wipe trap, one endpoint over. The corp read
  is strict too, with one exception: a 403 means the character granted the scope but does not hold
  the corp role, which is permanent and makes `[]` the true answer rather than a gap in one.
  **Left open by this:** a character that dropped the jobs scope keeps nagging off a frozen
  snapshot, because suppressing it would silently remove a working alert and no page explains the
  fix. Worth surfacing "re-authorise with reactions enabled" next to the reaction alerts; once it
  exists, suppression becomes the right answer and the skip in `_rescan_targets` should become a
  `suppressed:no_jobs_scope`.
- ~~**Nothing measures the win.**~~ **DONE 2026-08-17.** `_log_rescan_outcomes` writes the two
  outcomes that never became a send into `pp_notification_log` under statuses no send uses:
  `prevented` (the benefit) and `suppressed:<no_token|retry_brake|scan_failed|over_budget>`
  (the cost). Every existing reader filters `status='ok'` and so ignores them;
  `/api/notifications/log` was the one that did not and now excludes them via `_SENDS_ONLY_SQL`,
  because the user's log is a list of things that were sent. Suppressions are deduped per cause per
  12h — an unresolved problem is due on all 96 ticks of the day. `prevented` is not deduped, because
  a problem found fixed does not recur next tick. Query in `docs/platform.md`.
  *(The RUNG was never at risk: `_consecutive_cooldown_h` derives it from send timestamps whenever
  you care to look.)*
- **Watch the first week on the rung**, specifically: whether any character sits amber for long
  (transient scan failures that never recover would silently pause its alerts), and whether the
  per-tick budget is ever actually reached. Now answerable from the log rather than by eye —
  `suppressed:retry_brake` and `suppressed:over_budget` are exactly those two questions.
- **The scan budget is per PROCESS, not per app.** Prod runs 6 (2 replicas x 3 workers), each with
  its own module global. The advisory lock serializes them and `_recently_notified` empties the
  second runner's alert list before it reaches a scan, so the real spend stays near the ceiling of
  one process — but the constant's own guarantee is 20 per process. If that ever needs to be a
  true app-wide cap it has to move into the DB alongside the job lease.
- **Clock skew is unguarded.** The `esi_expires` gate compares ESI's absolute `Expires` against
  local `time.time()`, so a pod whose clock runs fast buys premature requests. Low risk on NTP'd
  nodes and not worth a mechanism today, but it is the one way the never-query-before-`Expires`
  rule could be broken without a code change.

## 38. Bug 3 — "Characters missing at setup stage of planning" — FIXED 2026-08-17

**The report, verbatim** (filed 2026-07-14): *"Went into planning mode and I only see the production
target and constellation filter parts of the setup page. The character part is missing."*

**It was missing, and my first guess at why was wrong.** The initial read of the markup said the
Character Roles card was merely collapsed. It was not: `#ppRolesCard` shipped with
`style="display:none"` and was revealed only by `_loadProductConfig`, which runs when a typed
product resolves to a type id. So on a fresh setup page the card was absent — and `onProductChange`
put it *back* to `display:none` on any product that failed to resolve, so the character section
also vanished mid-edit after a typo. Meanwhile the Constellation Filter reveals itself from the
Planet DB load, independent of any product, which is exactly why the reporter saw that one and not
this one. Reproducing beat reading the markup, as the item said it would.

**The fix.** The card is present from the first paint and says what it is waiting for. The rows
genuinely cannot be built without a product — per-character planet counts are stored per product —
but that is a sentence, not a reason to disappear. The hint lives on the TITLE line because the body
is collapsed by default, so the answer is visible without a click. `_ppRolesWaiting()` is the single
writer of that state, so the first paint and a cleared product cannot drift apart.

**The rule this leaves behind**, in `test_setup_page.py`: a section of the setup page may explain
that it is waiting for something, and may not disappear. A card may ship hidden only when something
outside the user's control makes it meaningless — `#ppLocationCard` may, because the Planet DB can
genuinely hold no constellations; `#ppRolesCard` may not, because characters are the planner's main
input. Both directions are asserted, and both mutations (ship it hidden again; hide it again on an
unresolved product) turn the suite red.

**Still to do: close the report.** `POST /api/bugs/{id}/status` with `complete`, or the Admin tab —
it needs an admin session, so it is the user's to do. Do not UPDATE `pp_bugs` by hand.

## Nothing else open

The rest of the backlog is §34 (backend: blueprints.py still to split), §37a and §38 as of 2026-08-17. §18b (config export/import) and §19 (URL routing,
including the deep links that carry an id) both closed that day — §19's last piece, deep-linking a
colony, was closed as **won't build** rather than shipped, and the reasoning is in the archive.

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 ("the browser test is not something I want us to do"). Routing is pinned by `test_routing_client.js` (runs the router for real) plus
source-level checks; live-browser bugs stay the user's to catch. Don't propose a headless-browser
suite again.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
