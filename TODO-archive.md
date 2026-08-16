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
| 21b | The refill deadline is DERIVED, not stored. `pi_sim.colony_drain_state` reads each factory colony's per-input consumption off its real pins (constant `quantity / cycle_time` — deterministic, unlike extraction) into `pp_char_planets.drain`; `planner.factory_drain` is the one place that drains it forward from the colony checkpoint. The `factory_refill` alert had been taking a full 3-launchpad buffer from the plan snapshot and anchoring it to `scanned_at` — assuming every colony was topped to full the instant we last polled ESI — so it contradicted the Dashboard agenda, which already used the observed number. `localStorage.refillDeadlineMs` is gone: the deadline follows the player to any device and the server quotes the same instant the page does. `test_factory_drain.py`, `test_refill_deadline.js` | 08-16 |

---

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
