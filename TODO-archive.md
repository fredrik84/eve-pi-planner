# eve-pi-planner — TODO archive

Shipped work and closed-with-reasoning verdicts, split out of [TODO.md](TODO.md) so the live
backlog stays small to read. **Closed items should not be reopened without new evidence** — the
verdict column says what the evidence would have to be.

---

## Shipped — detail in CLAUDE.md and the git log

| # | What | When |
|---|---|---|
| 1 | Required-skills-to-build (`required_skills`), incl. the skill-aware start-now checklist (`industry_install_skill_aware`) | 07-30, 08-05 |
| 2 | Skill-optimization advisor (`industry_skill_advisor`) | 08-01 |
| 2b | Job-time skills read from a real `pp_char_skills` row set; everything else keeps the V/V fallback and reports `skill_time_basis: "assumed"`. Two plausible signals were tried and rejected against prod data first — the ESI scope (proves a scan happened, not that a column was filled) and "any industry-era column is non-zero" (two accounts show Mass Production V with Industry 0, which the game forbids). `test_skill_time_mults.py` | 08-01 |
| 2c | Running a build, not just planning one — blacklist, manual done-marks, corp hangars, per-order sourcing | 08-02 |
| 2d | Industry first-use onboarding (`pp_industry_settings.onboarded`) | 08-03 |
| 2e | Test-suite audit: 500 → 450 assertions, cutting source-text checks that would pass if the logic broke. The trim found a live bug — a type with no consumer paced against the whole queue's makespan (`71ffff8`). **Check WHY a test passes before deleting it** | 08-03 |
| 2f | Per-order plans (`industry_per_order_plans`) + `/queue-plan/compare`; cross-order alignment made explicit; stock, contracts, owned copies and job fees all corrected to first-come-first-served | 08-05 |
| 2g | Slot alignment, two rounds: `_PACE_OVERSHOOT = 1.0` (232 → 159 jobs), then `_align_cohorts` (159 → 143, three login trips collapsed to one). An allowance grows a job; only a TARGET lands it | 08-03 |
| 5 | Epoch timestamps widened from float4 (`widen_epoch_columns`, 22 columns / 15 tables; `pp_bpc_scan`'s three added 08-05 after prod contradicted the write-up) | 07-31, 08-05 |
| 6 | `no-undef` over `static/*.js` — `scripts/lint_js.mjs` + the `lint-js` CI job (non-blocking by design) | 08-05 |
| 7 | Reaction assign is idempotent and capacity-checked (`reactions_assign_guard`); capacity counts the worst TIER, since chain tiers are sequential | 08-05 |
| 8 | Build system defaults to a structure you build in, else Jita as a labelled reference (`industry_default_build_system`) | 08-05 |
| 9 | A container names the SYSTEM it is in, and a build may source from several (`industry_plan_sources`) | 08-04 |
| 10 | Choose whether — and which — reactions a plan builds (`industry_reaction_policy`) | 08-05 |
| 11 | A reaction formula is an item too: concurrency capped by formulas held; unknown ownership never serialises | 08-04 |
| 12 | The Industry flow end to end, twice: `docs/industry-workflow.md` (nine steps + the module/endpoint/table map) and `docs/industry-workflow-user.md` (the same path for the user). Written from a read of the 22 modules and `static/industry.js`, not from memory; the judgement-bearing half is fenced under Observations. UI home still open — see 12-residual | 08-05 |
| 13 | `docs/manifesto.md` — purpose, target state and honest gap for PI (an end state), Reactions (a business in its own right) and Industry (a direction), plus the five questions a feature is scored against and what a failing score means | 08-05 |
| — | `docs/industry-audit-2026-08.md` — item 12 re-run against the manifesto: the every-time path cleared, `industry_per_order_plans` and `industry_skill_advisor` failed, two dead routes found, and the live flag read that corrected two of the pass-1 claims | 08-05 |
| — | Alliance-shared build structures as suggestions (`industry_group_structures`) | 08-05 |
| — | Pin a rig FAMILY to a structure and every job in it is installed there, whatever the routing scores (`pp_industry_settings.build_pins`, on the `industry_rig_routing` flag). A pin can only pick among sites already legal for that job's activity; one it can't honour falls back to the automatic routing and says so. The pin decides WHERE, `fittable_families` still decides what BONUS | 08-06 |
| 19b | Reaction plan STAGES on the dashboard: planned slots sort by `tier_order`, carry an `S<n>` badge, later stages dim/dash, and the "To install" checklist splits under stage banners. The number is `tier_order + 1` absolute, never re-ranked against what's still pending | 08-07 |
| 16 | Dead Industry surface removed: the unrendered skill advisor (module, endpoint, flag, test), `/api/industry/to-install` and `/api/industry/skill-coverage`. No behaviour change; the checklist-vs-plan guard is now structural | 08-07 |
| 20 | Clear all no longer strands a customer order: order rows are cleared AND the order's `assigned_runs` handed back (top row per chain, clamped at 0), with already-stranded orders repaired on the next assign. `test_clear_all_orders.py` | 08-07 |
| 27 | Customer orders load faster: `request_memo` (the evidence layer was rebuilt five times per report) plus a 2-minute cache of the priced graph (`_load_goo_and_reached`, 270ms -> 0ms warm), and progress/stale-render fixes on assign and clear | 08-08 |
| 28 | One run count per product across EVERY character (`level_product_runs`, `reactions_level_runs`): Carbon Fiber at 125/90/90/75 on four characters becomes 125 everywhere in 12 jobs instead of 15, chosen by spread → slots → surplus and never running longer than the stage's longest job already planned. `test_level_runs.py` | 08-08 |
| 26 | One run count per product per stage (`level_stage_runs`): three assigns that each sized Carbon Fiber separately (125/90/75) become one number typed three times, total preserved and rounded up, row count and chain identity untouched | 08-08 |
| 25 | Formulas-to-acquire section on the reactions shopping list — the plan's missing formulas with Jita contract prices and a copy-names button, kept out of the materials tables and every cost total (a formula is a contract buy, not a multibuy line) | 08-08 |
| 24 | "Clear its jobs" on a customer order (`DELETE /api/reactions/orders/{id}/assignments`): frees its slots, hands its runs back, keeps the order — so one order can be re-planned without Clear all or cancelling it. Give-back rule shared with Clear all | 08-08 |
| 23c | Reaction stages land together (`_align_stage_jobs`): slots move off steps that would finish early onto the one gating the stage, slot-neutral — fewer logins to install and collect | 08-08 |
| 23b | Intermediate run counts rounded to typeable numbers (`reactions_tidy_runs`), bounded at 15% over, with the shopping list buying for the rounded plan | 08-08 |
| 23 | Reaction stages are dependency DEPTH, not list position — siblings (Carbon Fiber / Oxy-Organic Solvents / Thermosetting Polymer) share one stage and run together, existing plans repaired in place, plus "stage N is ready to start" read off ESI job states | 08-08 |
| 22 | Reactions shopping list stopped double-counting chains — only the top row of each assign is exploded, so a two-tier plan no longer asks for twice the goo. `test_shopping_roots.py` | 08-07 |
| 21c | Reactions spend what you already hold (`reactions_use_stock`): an intermediate in an enabled source shortens or drops its stage and everything below it, in the plan and in the materials walk, consumed once per plan and always reported. `test_reaction_stock.py` | 08-07 |
| 21a-b | One slot model for reaction chains (`reactions_parallel_stages`): stages reuse a reactor instead of each reserving one, so free slots show what can really start — and reactors nobody claimed are spent splitting the slowest step across more jobs. Runs, cost and profit untouched. `test_parallel_stages.py` | 08-07 |
| 19a | "You don't hold a formula for these" (`reactions_missing_formulas`): once a PASTED window makes the library complete, an undeclared formula is one you don't own — reported with runs and a contract price on all three planning surfaces, and kept out of every shopping list and cost total. Unresolved paste names are KEPT and shown beside the finding, because a rename otherwise reads as "you don't own this". `app/reactions/library.py`, `test_missing_formulas.py` | 08-07 |
| 19 | **Pages live at URLs.** Twelve pages addressable (`/reactions`, `/manufacturing`, …), back/forward, deep-link entry, gating bounces that REWRITE the address instead of pushing a page you cannot open, and the Admin/PI-Planner sections addressable too (`/admin/bugs`, `/planner/refill`). Routes registered explicitly, never as `/{page}` — a wildcard before the static mount returns the SPA document for every missing asset, which a browser reports as a syntax error inside the file rather than a 404. The twelve `localStorage.getItem('activeTab')` guards now ask the router, which fixed an unreported bug: two browser tabs answered "which page am I on" for each other. `test_routing.py` + `test_routing_client.js` (the first test that RUNS client code). **Only ID-bearing deep state is left — see §19 in TODO.md, and it is a privacy decision before it is code** | 08-15 |
| 21 | **Refill to a DEADLINE, not to "full"** (`refill_deadline`): name the time you want to come back and each factory's P1 drop is sized to run dry then. Per-factory burn rate and run size now travel on `p1_inputs` (`units_per_day`/`units_per_run`) — a combined plan sums consumption across products, so a plan total and a share can no longer be turned back into one factory's rate. Four ceilings in a fixed order (pad contents → launchpad space → the P1 you hold → whole runs), and each refusal is stated rather than fudged: a deadline the pads can't reach gets the soonest one they can, a factory already stocked past it is "skip, come back in X", and the time lost to rounding is reported. Deadline picked and read in local time, shown next to EVE time, stored as an instant. `test_refill_deadline.js` (runs the real split) + `test_refill_rates.py`. **Residual: the deadline lives in one browser — §21b** | 08-15 |
| 19c | **A URL can name one ROW**, not just a page (§19 phase 3b): `/manufacturing/order/123`, `/planetary-planning/plan/12`, `/planner/refill/plan/12`. The privacy question that gated the whole entry, answered per record: the endpoint behind each id already refuses a stranger *without confirming the id exists* (`_order_row` raises one 404 for "not yours" and "no such row" alike; `GET /api/plan-snapshots/{id}` answers `{payload: null}` for both), so a link that reaches somebody not entitled to it lands them on the plain page **in silence** — the same thing a mistyped id gives them, because telling those two apart IS the disclosure. The server route looks NOTHING up: it serves the same document every page does, so a recipient learns nothing from the fact that it answered. A refused record is dropped from the address bar with a REPLACE, so it does not even leave a back-button entry. `TAB_RECORDS` in `app.js` and `SPA_RECORDS` in `main.py` are a fourth list `test_routing.py` holds in step with the other three; the bounce, the throw, the leave-the-page close, the late-loader case, two overlapping opens (the slow answer must not win — it used to repaint the dialog as the other order, which is the order a Save would then have written to) and a restricted page refusing the record under it all run for real in `test_routing_client.js`, with source-level backstops for the four properties a stubbed opener cannot notice going missing. **A COLONY was asked for and deliberately not built** — closed the same day as won't-build (see the verdict below): there is no single-colony view to land on, and its natural id is locatable data. §19 is done | 08-16 |
| 18b | **Config export/import** (`config_export_import`): the whole build configuration as one portable JSON file — build rules, structures with their rigs and families, freight rates and tax, component overrules, stock sources, placeholder slots — under Settings → Backup & transfer. A serialiser over the readers and writers that already existed, no new table and no migration, which is what §18's storage half predicted. Two ids are NOT portable and are remapped rather than written through: a build pin travels as the structure's `location_id` (the stored `s:<row id>` is the account's own key — verbatim it names somebody else's building), and a stock-source key the importing account never scanned is dropped and counted. Import validates the whole document first and reports every problem at once, so a bad file changes nothing rather than half-applying; a section whose flag is off is skipped BY NAME rather than 403ing the import or vanishing; importing twice is idempotent (structures match on location, placeholders on name); deleting is opt-in and previewed. The file identifies you and the download says so, from the list the exporter itself fills. `app/config_io.py`, `static/configio.js`, `test_config_io.py` (mutation-verified), `docs/config-shape-2026-08.md` | 08-16 |
| 21b | The refill deadline is DERIVED, not stored. `pi_sim.colony_drain_state` reads each factory colony's per-input consumption off its real pins (constant `quantity / cycle_time` — deterministic, unlike extraction) into `pp_char_planets.drain`; `planner.factory_drain` is the one place that drains it forward from the colony checkpoint. The `factory_refill` alert had been taking a full 3-launchpad buffer from the plan snapshot and anchoring it to `scanned_at` — assuming every colony was topped to full the instant we last polled ESI — so it contradicted the Dashboard agenda, which already used the observed number. `localStorage.refillDeadlineMs` is gone: the deadline follows the player to any device and the server quotes the same instant the page does. `test_factory_drain.py`, `test_refill_deadline.js` | 08-16 |
| 34 | **Industry split into readable modules.** `static/industry.js` 4,705 → ten files of 327-679, split along `docs/industry-workflow.md`'s steps rather than by size; `schedule.py` 2,056, `graph.py` 1,383 and `blueprints.py` 1,517 → three packages, largest module 547. Every `__init__.py` re-exports **every** name the package defines, privates included, so no import anywhere changed. Cross-module imports were **derived from the AST**, not hand-listed, which makes "no import cycles" a checked property rather than a claim; one real cycle (`manual` → `paste`) was broken first, in its own commit. The split was written up and reviewed by two agents BEFORE a line moved — that is what caught a line range claimed by two files (which would have double-bound a toggle and fired two plan requests per flip) and three boundaries that would have orphaned a comment from its function. Plus a behaviour-preserving simplification pass: dead `indForceBuildType`, one reader each where two could drift, the four biggest renderers' inner builders lifted, `_indStageModelForPlan` memoised. **What it exposed is below — three tests that were not testing what they claimed** | 08-16, 08-17 |
| 34a | The three §34 could not fix. **`indPrioSpeed` fired two plan requests per flip** — an inline `onchange` AND a `DOMContentLoaded` listener, both calling `indRunPlan()`, racing over which response painted the card; the listener was the strict subset, so it went. **"The preview modal never sets `_indLastPlan`" was WRONG** — verified rather than taken on trust (`industry-plan.js:125` is one of three assignments); do not reopen without a reproduction, the report came from reading a 4,700-line file and missing an assignment mid-function. **`_indStepsHtml` split**, and the assertion that had blocked it was vacuous: mutating the header back did not turn it red, because it matched `s.longest` anywhere after the first `html +=` and both numbers also appear in a `title` attribute. It now asserts on the element's TEXT | 08-17 |
| 35 | Two controls off the Reactions card. "Come back every N days" now lives in exactly two places — Settings → General and the first-run wizard — instead of duplicating a setting onto the front page; the Settings row fetches the value itself if the Reactions tab was never opened, and hides when the account has no cadence surface, so nobody sees a dead control. "Advanced: full opportunity list" is **hidden, not deleted** (one `display:none`, commented), so restoring it is deleting an attribute — and since the fold-out computed lazily, hiding it also removed the tab's single most expensive computation for anyone who had expanded it. **Where the delay came from:** written up 08-16 and then not built — "start building" landed mid-discussion of §37 and was read as §37 alone, so the item sat complete-looking with the decision recorded and no code behind it | 08-17 |
| 36 | The shopping list stopped listing things you already had (`industry_sourced_counts`). **The report's premise was mistaken** and reproducing it in the code beat trusting it: nothing ever excluded customer orders. Two real gaps instead — `pp_industry_sourced` (the sourcing panel's ticks and pastes) was never read by the planner, and stock has never reduced anything you BUY, because `aggregate_demand` applies `on_hand` inside the `for tid in built` loop only. So `noted_stock_excess` stops the plan BUILDING what you hold and `_mark_already_held` stops the list telling you to BUY it, with multibuy copying the shortfall rather than the requirement. Two rules not to break: **a note and a bound box are two answers to one question — take the better, never the sum** (summing makes the plan believe it has twice what it has, and under-buy); and **quantities change, money does not** — `qty`/`line_cost` stay the full requirement, or a quote would quietly shrink whenever the builder happened to hold stock | 08-17 |
| 37 | **Alerts check before they nag, and nag less each time** (`alert_rescan_backoff`). Restart your PI in game without rescanning and the Discord alert used to fire every 2h forever. Now a due alert triggers a re-read of the one colony it is about and only what survives is sent; repeats double the kind's base interval up to 12h, derived from `pp_notification_log` and reset when the alert resolves, with the FIRST send never delayed; and a colony that could not be READ is held back rather than reported off stale data, with an amber character dot for the transient case, distinct from the red "re-add this character". Keeping ESI load low was the binding constraint (*"bashing them won't help me"*) — single-planet reads, an `esi_expires` gate, a dead-token filter and a hard per-tick budget give ~4 reads on day one for an ignored problem and 2/day after, against ~96 per character per day for the blanket-timer design that was rejected. Design in `docs/platform.md`. **The review findings are below — of eleven defects across three rounds, eight were mine** | 08-17 |
| 37b | **The same rule for the reaction alerts** (`alert_rescan_reactions`), plus instrumentation for both. `_log_rescan_outcomes` records the alerts that never became a send — `prevented` (the benefit) and `suppressed:<cause>` (the cost) — in `pp_notification_log` under statuses no send uses, because unlike the backoff rung those cannot be reconstructed afterwards. The `reaction_*` kinds already had the backoff (they carry `dedupe_id`); what they lacked was the rescan, now one `characters/{id}/industry/jobs/` read per CHARACTER answering for all three kinds, with `fetched_at`/`_JOBS_CACHE_TTL` as the `esi_expires` equivalent. `pp_notification_log` also finally got an index and a 45-day prune of the instrumentation rows only | 08-18 |
| 37c | **The lapsed jobs scope now names its own fix, so the alert can finally be held back.** `esi-industry.read_character_jobs.v1` is opt-in (`?reactions=1`) and re-authorising through the NORMAL login silently drops it; `pp_char_industry_jobs` then freezes and all three `reaction_*` kinds nag off data nothing can refresh. §37b deliberately SKIPPED those characters — suppressing an alert whose fix is nowhere explained is worse than a stale one — so this shipped the explanation first (`reactions_scope_lost` / `scope_lost` → a "job tracking disconnected, re-authorise with reactions enabled" prompt on the Characters card, in its collapsed summary as well, on the wallet-only branch, and above the Reactions dashboard **including its empty state**, which is exactly what a lone tracked character losing its scope produces) and only then turned the `continue` into `suppressed:no_jobs_scope`. **Row presence, not job presence, is the test** — and a snapshot with no row is still skipped, because all three kinds read from that table so there is nothing to be silent about | 08-18 |
| 38 | **Bug 3, "characters missing at the setup stage".** `#ppRolesCard` shipped with `display:none` and was revealed only by `_loadProductConfig`, so it was absent on a fresh page and `onProductChange` put it BACK to hidden after a typo — while the Constellation Filter reveals itself from the Planet DB load, which is exactly why the reporter saw one and not the other. My first read of the markup said "merely collapsed" and was wrong; reproducing beat reading. The rule it leaves behind, asserted both ways in `test_setup_page.py`: **a section of the setup page may explain that it is waiting for something, and may not disappear** — a card may ship hidden only when something outside the user's control makes it meaningless | 08-17 |
| 39 | **The build pipeline staged a node by tree POSITION, not by what it needs.** `_indComputeTiers` set a node's stage from `Math.max` over every DEPTH the walk reached it at from the root — so a simple reaction needing nothing but fuel blocks landed a stage late whenever it was also a direct root ingredient, because "near the root" read as "late stage" regardless of how few production steps it actually needed. Reported live: Rolled Tungsten Alloy/Dysporite/Caesarium Cadmide/Promethium Mercurite (fuel blocks only) were grouped into the same Stage 3 as Pressurized Oxidizers/Reinforced Carbon Fiber (which genuinely need Stage 2 output). Rewritten bottom-up and memoized: a build node's stage is `1 + max(stage of its own BUILD inputs)`, independent of which parent the walk reached it through first; the root(s) are pinned to one shared terminal "Finished" stage via `Math.max` against their own natural depth, so a queue of several products still converges on one final column. Found and fixed the same bug's twin while auditing every consumer of `col.t`/`.tier`: `static/industry-steps.js`'s pipeline-cell CSS still checked `col.t === 0` for "is this the Finished column", which stopped being true the moment tier 0 became "earliest" instead of "root" — would have shipped the final-column highlight on the wrong end of the pipeline. `test_industry_stage_depth.js` (runs the real functions in `vm`, no DOM needed) covers the reported scenario plus the single-hop-root and shared-bought-material-at-two-stages cases | 08-18 |

---

## 34 detail — what the split exposed: three tests that were not testing what they claimed

Each was passing before, and would have kept passing wrongly. This is the reusable part of §34.

1. **`_patch_db(B)` patched a package attribute nobody reads.** Four tests set
   `blueprints.get_connection` and then ran code whose submodules had imported `get_connection`
   into their own globals — so every call went to the REAL database and the tests still passed,
   exercising container state instead of their own fixture. `_patch_db` now walks a package's
   loaded submodules. `_patch_db_all` had documented this exact hazard for sibling modules; nothing
   had applied it to packages.
2. **`bp._manual_enabled` monkeypatching went inert** — three checks in
   `test_manual_blueprints.py` went red, which is the good outcome. The helper now patches every
   module binding the name, discovered by walking `sys.modules` rather than naming today's two.
3. **`G._default_system_on` / `_fallback_build_system`** — same shape in `test_cost_basis.py`; all
   three of its scenarios would have collapsed into one. Retargeted at `graph.options`.

Also: `inspect.getsource(<module>)` returns only `__init__.py` for a package, so `test_industry.py`'s
pin on `"build_pins_unapplied"` appearing twice became `0 != 2` and failed loudly. Replaced with
`_module_source`, the package-aware sibling of `_industry_js`.

**One promise that was only ever a docstring is now checked:** `test_the_scheduler_stays_io_free`
asserts no `schedule/` submodule reaches for `get_connection`, `app.markets` or `@router`.

**Two review findings did NOT survive checking**, recorded so they are not re-raised: `blueprints.py`
was said to use `log` 36 times and uses it **zero** times (the logger is defined and dead); and the
`graph/params.py` boundary correction to line 36 was right — cutting at 48 would have dropped
`@dataclass` off `BuildParams` plus two constants Reactions imports.

**Note for the next reader:** `scripts/symbols.sh app/industry/schedule.py` no longer resolves — use
the DIRECTORY (`scripts/symbols.sh app/industry/schedule`), which maps the whole package.

## 37 detail — eleven defects three review rounds caught, eight of them mine

Kept because the shapes recur, not because the code is interesting. The feature's own design is in
[docs/platform.md](platform.md).

**Round one, before §37 shipped:**

1. **A failed colony detail read used to WIPE the colony row.** `_fetch_planets` swallowed the
   exception and fell through to an UPSERT that wrote `is_extractor=0` and NULLs over products,
   pads, sim state, storage and `esi_expires`, with a fresh `scanned_at`. Pre-existing — the hand
   rescan has always done this — but this feature would have made it automatic and unattended.
   **This is also why `_default_scan` cannot key off `fetched`:** that counter increments BEFORE
   the detail request, so it counts attempts, not successes.
2. **The backoff counted log ROWS, and one send writes one row per channel.** A user with three
   channels hit the 12h cap on their second alert and could never reset the chain, because a gap of
   microseconds is never longer than any interval.
3. **The scan budget was per context, not per tick** — it multiplied by the number of accounts.
4. **The cost per colony was 4 ESI requests, not 1**, because `universe/names/` and
   `universe/planets/{pid}/` were unconditional. Both answers are immutable, so both are now
   skipped when the DB already has them. The hand rescan got the same saving for free.
5. **`_refresh_token` wrote `NULL` into `pp_characters.refresh_token`, which is `TEXT NOT NULL`.**
   The IntegrityError was swallowed by its own outer `except`, so a revoked character was never
   marked dead and kept a green dot forever — and the docstring above the code claimed this had
   already been fixed. A prerequisite, since the dead-token filter has nothing to filter on until a
   dead token is recorded as dead. Own commit.

**Round two, on §37b:**

6. **`prevented` was credited when an alert merely ESCALATED.** `expiring`→`expired` on one planet
   and `reaction_finishing_soon`→`reaction_completed` on one character are two kinds sharing a
   dedupe target: the first disappears, a notification still goes out, and the measurement counted
   it as a save. Credit now requires the whole target to be clear, which undercounts instead — the
   direction a number the feature is judged on should err in.
7. **A failed corp jobs read stored a truncated snapshot and reported success.** A corp-installed
   reaction never appears in the personal read, so `[]` from a failed corp fetch deletes the
   finished job, reports the problem as fixed, and leaves the next tick computing from the
   truncation. The §37-round-one trap, one endpoint over. Both reads are strict on the alert path
   now; the ONE tolerated failure is a 401/403 on the corp queue, which means the character granted
   the scope without holding the role — permanent, so the empty answer is true rather than partial.
8. **A "querying before `Expires`" violation I introduced.** The corp path re-fetched
   `characters/{id}/` — which ESI caches for a day — every 15 minutes. Now cached until the
   response's own `Expires` header. CLAUDE.md's rule is absolute for a reason; a TTL of our own
   choosing would not have satisfied it.
9. **A prune I added could have caused a duplicate-notification storm.** It ran on the tick's own
   connection, whose entire send log is uncommitted until the end (`_process_context` never
   commits) and which the Postgres cursor wrapper rolls back on any statement failure — so a
   timeout on that DELETE would have discarded every send row for the tick AFTER the pushes went
   out, and the next tick would have sent the lot again. Own connection, after the commit.

**Round three, on §37c** (the same shape a third time, which is why it is worth writing down):

10. **`scope_lost` warned about characters that had lost nothing.** `reaction_capable` is false for
    TWO reasons — no scope, or no reaction skill trained — and the flag only asked whether a jobs
    row existed. A scope-holding alt with no Mass Reactions gets a row written for it like any
    other, so it drew a permanent "job tracking was disconnected" that re-authorising could never
    clear, while the Characters card (which does test the scope) showed nothing. **A derived flag
    must restate the condition it means, never borrow a nearby boolean that happens to correlate.**
11. **Two surfaces disagreeing is the failure mode to look for**, because suppressing an alert is
    only defensible while the prompt is guaranteed to be showing: the wallet-only branch returns
    before the Reactions block is built, and one exception in the job-formatting loop cleared the
    row set for every character while the notification side went on suppressing off its own query.
    Both fixed — the prompt renders in the wallet branch, and row presence is read in its own `try`
    ahead of the formatting.

**The method that found them:** written proposal → adversarial review agents → execute →
mutation-verify each new assertion → re-review. Round two found four defects in round one's fixes,
and round three found two more in a change written by someone who had just read all of them, which
is the argument for re-reviewing a fix rather than trusting it.

## 20. Planet database wipe — CLOSED (2026-08-15, INCIDENT)

**Restored and closed the same day.** Production's `pp_planets` was found with 0 rows: 5,302
planets, the shared reference data the whole PI planner runs on. Reloaded from the 08-12 nightly
dump — 5,302 rows back, id sequence realigned (it was still at 625 and would have collided on the
next insert), Redis invalidated, verified live on `/api/planets`. The rung was then set deliberately
by the user: **`planet_db` stays on admin**, which closes the last follow-up.

**Cause, in order.** `planet_db` was a feature flag gating the Planet DB tab, sitting on the **admin
rung** in production. `d916c92` (12-08 23:39) retired it along with 17 others on the stated premise
that all eighteen had been "public since June" — that premise was not checked against the live
`pp_features` rows and was wrong for this one. Retiring it published the tab, and with it a "Clear
all" button wired to `DELETE /api/planets`, which was gated on `require_context` — any logged-in
player — and deletes the table for everybody. The nightly dumps bracket the wipe to the ~3.5 hours
between that deploy and the 13-08 03:00 backup.

**Shipped in response:** the flag is back with its gate (`nav-feat-pdb`, CSS + pre-paint class);
`clear_planets` requires an admin and records to the audit log; the button carries `.pp-admin-only`;
`test_planetdb_guard.py` fails on any unscoped global delete that is not admin-gated, so the shape
cannot come back quietly. `app/audit.py` + Admin → Audit record global deletes, account deletions,
cleanups, privilege removals and refused access.

**The two lessons worth keeping.** A flag's rung is a fact about PRODUCTION — read the live
`pp_features` row before retiring one, because the registry default says nothing about where it
actually sits. And a destructive control must not depend on a flag for its gating: the flag decides
who SEES the page, `require_admin` decides who can wipe the table, and conflating those is what
turned a visibility change into data loss.

---

## Closed — do not reopen without new evidence

| Item | Verdict |
|---|---|
| Deep-linking a PI COLONY (§19c) | **Won't build** (2026-08-16, user decision: "we don't need to share individual colonies"). This **closes §19 entirely** — the rest of it shipped the same day (see the shipped list). Two reasons the entry is worth keeping rather than just deleting. **There was nothing to link to:** a colony appears as rows across Setup Analysis, the Characters list and the plan, never as a page you open, so a deep link would have meant inventing a colony detail view first — a new page in a tool whose PI principle is *fewer* interactions. **And an id would not have been enough:** an order and a saved plan are opaque integers whose endpoints already refuse a stranger without confirming the id exists, which is what made those two safe; a colony is character + planet, which is exactly the "character names, systems, planets, or any locatable data" rule 8 names — it discloses in the PATH, before any endpoint is asked. It would have needed an unguessable token like the plan shares have, not an id segment. Reopen only if somebody actually asks to send one colony to another player, and then build it as a SHARE, never as a route. |
| Layout engine: intermediate storage facilities + simulated CPU/PG fit | **Won't build** (2026-08-05). Both were documented gaps for months with no demand signal: the generator routes intermediates tier-to-tier instead of buffering them through storage, and `compute_resources` estimates the fit from idealised pin coordinates. `FIT_HEADROOM = 0.10` exists precisely so the estimate need not be exact — it leaves ~10% of both budgets free so a template that fits on paper fits in the client. Reopen if an exported template is actually rejected in-game, which is the evidence neither gap has ever produced. |
| Per-account settings consolidation (`settings_store.py`) | **Won't do** (2026-07-30). The duplication is the cheap part (~60-80 lines of upsert); validation, which dominates the handlers, survives any scheme. 2 of 7 tables aren't settings rows at all. Trades typed columns for a JSON blob against this repo's additive-migration convention. Prod holds only 10 rows total, so the old "too risky" framing was wrong — it's low *value*, not high risk. **Partly reopened 2026-08-05 as item 18** on new evidence (a working keyed-blob config from ravworks, export/import now wanted, and a settings surface about to grow) — the objections above are what that audit has to answer, not skip. |
| Distribution "lever 1 — cross-character rich-planet reuse" | **Wrong lever, not unfinished work.** Per-character planet-pick shipped (`db56e2e`, `_waterfill_new_slots` regret heuristic). The residual "thin planets" symptom is lever 2 over-allocating, governed by the **min-density cap**, plus genuine data constraints (a P0 with one planet in-system). |
| P1 extractor→factory routing | **Won't build** (2026-07-08). Workflow is pooled and P1 is fungible once extracted; routing would impose a fake point-to-point constraint. Revisit only if actual point-to-point hauling automation is described. |
| Frontend CPU offload, phase 3 | **Rejected for now**, not deferred — the investigation found the real hotspot was cacheable server-side (already done), not a JS-offload candidate. Reasoning trail is in the project notes; don't redo it. |
| Skyhook storage bar | **Blocked** — ESI does not expose skyhook cargo; no deterministic formula to fall back on. Manual-checkpoint design was proposed and declined. Revisit only if CCP ships an endpoint. |
| Deleting the legacy Find-Buildables analyzer | **Keep it** (2026-07-30). Live, ungated, default PI-planner sub-tab; `highspy`+`numpy` are lazy-imported so they cost image size only. Promoted to `app/analyzer.py` instead. |
| Browser-level tests for `api()`/`toast()` | **Dismissed** (2026-07-30). Would mean introducing a browser-test harness this repo doesn't have; manual testing already catches the residual breakages at the expected rate. |
| Alert-engine rename | **Done** (2026-07-30). `app/colony_alerts.py` → `app/alerts.py`, `compute_colony_alerts()` → `compute_alerts()`, `test_colony_alerts.py` → `test_alerts.py`. Pure rename, zero behaviour change; `test_alerts.py` passes in-container incl. the live `/api/dashboard` layer. |
| Remove the dead `_muted` assignment | **Done** (2026-07-30). Deleted; `pyflakes app/planner_dashboard.py` is clean. `_alert` stays — it still supplies the display thresholds; only the mute set was dead (muting moved inside `compute_alerts()`). |
| Disconnect a character | **Done** (2026-07-30). Premise was stale: the UI button and `DELETE /api/characters/{id}` already shipped. The real bug was that it cleared 2 of 10 per-character tables. Now deletes all of them, clears the market-reader + saved-plan references, re-points the session instead of logging you out, revokes the ESI grant, and keeps `pp_bugs` + the completions ledgers. Hard delete, not soft unlink — a retained row keeps a live refresh token. `test_disconnect_character.py`, 6 groups. |
| `DELETE /api/me` orphaned rows | **Done** (2026-07-30). Cleared 3 per-character + 4 context tables, orphaning ~20 others. Now works from shared `_CHAR_OWNED_TABLES` + `_CONTEXT_OWNED_TABLES` in `app/esi.py` (9 + 19 tables, verified). Completions ledgers and per-character records DO go here (unlike the per-character disconnect — the account itself is going away); `pp_bugs` is anonymised so admins keep the report; group-scoped markets/settings survive. `pp_shares`/`pp_inventory_shares` have no owner column and cannot be cleaned by account, by construction. `test_delete_account.py`, 5 groups. |

## Closed from TODO.md on 2026-08-14

Removed from the live backlog because they are finished — the reasoning that produced each
is in the code, its docs and the git log, and re-reading it every session cost more than it
was worth.

| # | What | Verdict | When |
|---|---|---|---|
| 29 | Reactions: the profit numbers are not instant-sell (2026-08-14, HIGH, ungated) | Reactions profit figures moved to instant-sell (buy orders) against a FULL cost base — materials + job fees + freight + collateral. Covered the lifetime ledger, the dashboard tiles and the job modal, all three of which were live and ungated. Guard test `test_reactions_profit_clock.py` fails on any new sell-priced profit field | 08-14 |
| 30 | Reactions: the cadence ceiling collapses, and ease costs are invisible (2026-08-14, HIGH) | The cadence ceiling no longer collapses: all three escape routes closed, and a breach that genuinely cannot be avoided is now REPORTED on the row (`cadence_over_h`, `⏱ +Nh`) rather than taken silently. Levelling budget re-denominated in ISK; orders paced at quote time so the quote and the dashboard agree; ease cost surfaced with a remedy | 08-14 |
| 31 | Reactions is not answerable without Industry, and none of it is public (2026-08-14) | Reactions owns its cadence (`reactions_cadence`) and its own formula-paste route, so a reactions-only account no longer needs a Manufacturing flag. `reactions_missing_formulas` was unreachable by construction before this and now fires. Rollout question survives as §32 | 08-14 |
| 19 | Reactions must say what you need to ACQUIRE, and show the stages in order | Reactions says what to ACQUIRE and shows stages in dependency order | 08-07 |
| 23 | Stages were positions in a list, not dependencies | Stages are dependencies, not list positions | 08-08 |
| 28b | Slot reservation and order cadence (2026-08-08, from live use) | Both halves done. (1) A later stage no longer reserves slots it cannot use — pending rows count by their WORST TIER, not the sum, so a chain's stages stop each holding a reactor (`_character_capacities`, behind `reactions_parallel_stages`). (2) An order can no longer be quoted at an absurd cadence: `_allocate_and_insert` takes the cadence at quote time and a step that still overruns says so. Verified 2026-08-14 | 08-14 |
| 22 | The general shopping list double-counts every chain | General shopping list no longer double-counts a chain | 08-07 |
| 20 | The two Reactions "clear" paths disagree about customer orders | The two Reactions 'clear' paths agree about customer orders | 08-07 |
| 16 | Remove the dead Industry surface | Dead Industry surface removed | 08-07 |
| 33 | Two Reactions knobs for numbers the app can work out | Both done. `reaction_system` is derived when unset (behind `reactions_default_system`), reusing Industry's resolver and naming its basis, because a blank system meant profit overstated by the whole install fee once profit was netted against a full cost. `max_chain_depth` moved off the wizard front page and its default raised 2 → 5: measured first, and at 2 a depth-4 chain ranking SECOND overall was invisible, while the candidate set saturates at 18 by depth 5 — a search limiter, not a preference | 08-14 |
| 21d | Pipelining a chain's stages | **Parked by the user 2026-08-14** — "keep it per stage for now". 21a-c shipped 2026-08-07 and are what the stage-at-a-time model buys; the remaining idea needs partial-output tracking and redefines an assignment row. Written up in `docs/reactions.md` | 08-14 |
| 28 | Reaction job layout — the priority order | **Complete.** Priorities 1-3 met, 4 last by construction; the order itself now lives in `docs/reactions.md` because it GOVERNS rather than being a task. The last gap — two chains on one character sharing a job — was **closed won't-do by the user 2026-08-14**: they track jobs against the chain they belong to, and merging output makes a plan they cannot keep straight. Do not merge them | 08-14 |
| 12-residual | The user-facing workflow has no home in the product | **Done.** How-it-works pages for Manufacturing and Reactions, mirroring the PI one. Copy is fetched from `static/help/*.html` on first open rather than inlined in `index.html` — it is documentation, and inlining shipped it to every visitor on every load | 08-14 |
| 15 / 2f-1 / 2f-3 | `industry_per_order_plans` half-landed at testers | **Done — it keeps its rung.** Its condition was to land 2f #1 and #3 together or drop the flag to hidden. Both landed: a build's OUTPUT container is now a property of the PLAN (`pp_industry_orders.output_source_key`, inherited by every job, falling back to the first materials box and SAYING so), and the setting finally has a UI with its measured cost on it (+2.45% net on a 2x Archon). Per-job configuration was the framing that kept this stuck; the user settled it as per-plan | 08-14 |

## Rollout decisions — held by the user

Taken out of the live backlog on 2026-08-14: the user sets flags public when they judge a service
ready, so these are standing recommendations rather than open work. **Do not re-propose a rollout.**
Keep the tables current if the underlying facts change, and read them when the decision is made.

## 14. Roll Industry out, or write down why not (2026-08-05)

**Held by the user (2026-08-14): they will set flags public when they judge the
service ready. Do not propose a rollout again; keep the recommendations current.**

The audit's headline finding, and the one that reframes the rest. **All 15 Industry flags sit at
`testers` on prod; none is public — including `industry` itself.** Against that, the PI side is 14
of 17 public and Reactions is mixed. So the audience the manifesto names — any EVE player, casual to
serious — has never used this service, and every casual-user property it was built to have (the
facility presets that cost a build correctly, the wizard that can always be completed, the nudge
instead of a gate) has only ever been verified against the builders who asked for the features.

This is not a request to flip the flags. It is a request to decide which it is: a **known gap**
holding the gate — and then name it, because it is the next thing to build — or **inertia**, in
which case the ladder exists to be climbed and `industry` goes to `public` while the rest follow on
their own merits.

First step: pick one. Everything else in the Industry backlog is second-order to it.

## 32. Roll Reactions out, or write down why not (2026-08-14) — DECIDE

**Held by the user (2026-08-14): they will set flags public when they judge the
service ready. Do not propose a rollout again; keep the recommendations current.**

§14's forcing question, for Reactions, and with more force: the manifesto calls this service
standalone and public-facing, and as of today it is standalone **for admins**. A normal logged-in
user gets no orders, no formula cap (so plans schedule parallel jobs off one formula), no tidy runs,
no stock subtraction, no parallel-stage slot reuse, no levelled run counts, no cadence, no ease-cost
line and no missing-formula report. **The good behaviour is the exception.** The last month of work
exists to replace exactly the tool the default user is getting.

Registry defaults as of 2026-08-14, **15 Reactions flags, none public** (live prod state is not
readable from a dev session and the DB row wins once created — these are code defaults only, so
check Admin → Features before acting on any row):

| flag | registry default | recommendation |
| --- | --- | --- |
| `reactions_formula_cap` | admin | **public, and then retire.** A formula is one reaction at a time — a game rule, not a preference. Off, plans schedule parallel jobs off a single formula, i.e. work that cannot be installed. "Would we ever turn this off again?" is no. |
| `reactions_tidy_runs` | admin | **public.** Bounded rounding (15%, `_TIDY_BUDGET`) of intermediate runs only; end products untouched. Now that the ease-cost line reports what the rounding costs and how to get it back, its price is visible rather than quiet. |
| `reactions_use_stock` | admin | **public.** Not subtracting what you already hold is the tool telling you to buy things in your own hangar. Fails soft to empty, so the risk is one-directional. |
| `reactions_parallel_stages` | admin | **public.** Its own description says it never changes what is suggested, what it costs or what it earns — only how many reactors do the work. A pure correctness fix to the slot count. |
| `reactions_level_runs` | admin | **public — but only now.** Promoting it before the 2026-08-14 cadence repair (archived) would have shipped the collapsed ceiling (a 7-day cadence answering 11.7 days) and the invisible surplus to everyone. Both are fixed and pinned; this is the one to watch on the way out. |
| `reactions_cadence` | admin | **testers first, then public.** New on 2026-08-14 and the newest code in the set. It is also the flag with the strongest claim to `public` eventually: the cadence is the tool's headline setting and gating it means gating the product. |
| `reactions_ease_cost` | admin | **testers.** New surface, and the number it reports (`surplus_isk` / `recoverable_isk`) wants a real account's eyes on it before it is stated to everyone as fact. |
| `reactions_missing_formulas` | admin | **testers.** Only reachable at all since 2026-08-14, so it has effectively never run for anyone. Its failure mode is confidently telling a user to buy a formula they hold; watch the unresolved-name reports for a round before widening. |
| `reactions_assign_guard` | testers | **public, and then retire.** It refuses to book more reaction slots than a character has. A guard against an impossible plan is not a preview feature. |
| `reactions_pack_hosts` | testers | **public.** Places an order on the fewest characters worth a login. Measured (7 characters → 3, ~1 day on a 12-day order) and squarely the effort constraint the manifesto names. |
| `reactions_stage_pipeline` | testers | **public.** Presentation only — the same jobs, run counts and stage order as the table it replaces, drawn the way Industry already draws a build. Keeping two renderings alive is the cost of leaving it. |
| `reactions_manual_done` | testers | **public.** An escape hatch for when ESI cannot tell us (5-minute stale cache, a job installed under a different product). A mark can only ever bring a stage forward and is never counted as ISK earned. |
| `reaction_orders` | admin | **reconsider — the gap that held it is closed.** This read "hold, §28b (slot reservation) is still open against it"; §28b was verified done on 2026-08-14 (later stages no longer reserve slots they cannot use, and an order is paced at quote time). What remains is a judgement, not a defect: customer orders are a second product surface rather than a refinement of the first, so decide it on its own merits rather than on a blocker that no longer exists. |
| `local_market` | admin | **hold.** Following an alliance/structure market needs a connected character and a market to follow; it is the one item here that does nothing at all for a user who has not set it up, so `public` would put an empty card in front of everybody. Reconsider once the setup card is reachable in two clicks. |
| `local_sell_hint` | admin | **hold, with `local_market`.** It is that feature's alert half and cannot be evaluated separately. |

**The decision this needs from the user is one line:** is the ladder being climbed, or is there a
known gap holding the gate? If it is the former, the twelve rows above marked public/testers move,
the three `hold` rows stay put with their reason on record, and the two `retire` candidates
(`reactions_formula_cap`, `reactions_assign_guard`) lose their flag entirely — delete the entry and its
`feature_enabled` / `_featureActive` call sites, per the registry's own rule at the top of
`app/features.py`). If it is the latter, name the gap here — that is then the next thing to build.

**Not for an implementing agent to do unilaterally.** Rolling a flag out is a product decision and
this entry is a recommendation, not a change. First step: pick one.

**Two things the decision should know, both found in verification rather than by design:**

* **Reactions-pasted formulas are Industry declarations in waiting.** The Reactions paste route is
  ungated by design (it is a route to an existing feature, not a new one) and writes to
  `pp_industry_blueprints`. It is invisible to Industry today — proved with `industry_manual_blueprints`
  off, where Industry's `manual_blueprints()` and `owned_blueprints()` both come back empty while the
  Reactions side sees the formulas. But an admin who later turns that flag on for the same user will
  find declarations on the Industry tab that the user never pasted there. Defensible — they are the
  user's own statements about their own prints — but it should be a decision, not a surprise.
* **`industry_reaction_policy` is not in the table above and that is deliberate.** It is
  reaction-*named* but sits in the Industry group and governs Industry's behaviour, so it rolls out
  with Industry (§14), not with this. Noted because a reader will go looking for it.
| 2f-residual | Print locking across orders | **Done properly.** A print is now a time-shared resource inside `schedule()`: a job needs a free slot AND a free print, and the print is released when the job ends. Two orders planned apart contend for the same original because the resource is keyed on the real `type_id`. A first attempt subtracted an earlier order's claim, which only BOUNDED the over-booking — a claim is permanent, a print is merely busy — and was replaced rather than kept. `test_print_locking.py` pins that jobs on one print never overlap, that N prints allow N, and that an unobserved type is never serialised | 08-14 |
| 17 | Stock sources / reserve what a plan has claimed | **Done.** `app/industry/reservations.py` behind `stock_reservations`: work assigned to a slot but not yet installed holds its inputs, netted off inside `owned_quantities` and `source_quantities_multi` so Manufacturing and Reactions cannot disagree about what is free. DERIVED from the assignments rather than a stored ledger, so it cannot drift out of step with the work it describes; a running job claims nothing, because its materials have already left the box. The user's ruling settled the surrounding question: per-build boxes stay as a TRACKING convenience, and with the ledger in place account-wide stock is safe. `test_stock_reservations.py` | 08-14 |
| 18 | Is all of this too complicated? — storage shape and precomputation | **Answered, both halves — `docs/config-shape-2026-08.md`.** Half A: measured 62 `pp_` tables, 10 settings-shaped, **13 rows total**; reads are single-digit ms, so the blob rewrite stays Won't-do and the July reasoning survives. Revisit only if settings-shaped tables pass ~15. Half B: the heavy paths were already cached; what was left was one CHEAP read repeated — `get_settings` six times per `account_setup`. Memoised per request with invalidation on every writer, 9.4ms → 5.5ms, guarded by a source scan. Export/import turned out to be independent of storage shape and survives as §18b | 08-14 |
