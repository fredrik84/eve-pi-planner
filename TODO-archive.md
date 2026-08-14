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
