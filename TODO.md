# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped is in the one-line list at the bottom, with
the reasoning in CLAUDE.md and the git log, and everything reasoned-through-and-rejected is in
**Closed**. Items in that table should not be reopened without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

Reviewed 2026-08-05.

---

## 12. Describe the Industry workflow end to end (2026-08-05)

Industry has been extended a lot in a short time — make-or-buy overrides, an always-buy blacklist, a
reaction policy, per-order sourcing and source sets, corp hangars, manual done-marks, print and
formula caps, alliance-shared buildings, customer share links, onboarding — and each landed with its
own design note in CLAUDE.md. What does NOT exist anywhere is the **whole flow in one place**: what
the feature set supports, and how a builder is meant to work with it from "a customer asks for a
Phoenix" through to "it is built and delivered".

Wanted: a short summary plus a step-by-step of the intended workflow — the path a builder walks
every time, which controls belong to which step, and which are the occasional ones behind it. Two
audiences and they are different documents: the **user-facing** one (what the tab does and the order
to use it in — candidate for the How-it-works page or onboarding) and the **developer-facing** one
(a map of the modules and endpoints each step goes through, which CLAUDE.md's per-feature notes hang
off).

First step is a read of what is actually there rather than a write-up from memory: `app/industry/`
is 22 modules, and parts of the intended path exist only as UI ordering in `static/industry.js`.
Worth doing before the next feature, because "does this add a step to the path a builder walks every
time" is the design test everything here is supposed to meet, and right now that path isn't written
down.

## 13. A manifesto per service — what PI, Reactions and Industry are FOR (2026-08-05)

Companion to item 12, and it comes first when the audit runs: item 12 describes what the Industry
flow *is*, this one states what each service is *for*, so the audit has something to measure
against. Without it "is this up to code" has no code to be up to.

The repo already carries the house style — minimize planet interactions, automate the math or drop
the feature, the best UI is read-only, effort is the constraint the other goals fit inside, does
this add a step to the path a builder walks every time — but those are cross-cutting *rules*. What
is missing is, per service: **its purpose, the end state it is aiming at, and the path from where it
is now to there.**

- **PI planner** — one target, one plan, least interaction per ISK.
- **Reactions** — a slot business; what the tool decides for the user and what it deliberately
  leaves to them.
- **Industry** — lowest net cost and fastest delivery, inside the effort constraint (half written
  already, at the top of CLAUDE.md).

Wanted: a short manifesto for each — purpose, target state, honest gap — written so the item-12
audit can score against it feature by feature: does this serve the stated goal, does it cost more
effort than it removes, and if not, does it come out. It is also what to hold a NEW feature against
before building it, which is the cheaper end of the same test.

## 2f-residual. A job's output container, and prints across orders (2026-08-05)

Per-order planning shipped (see Shipped below) and these three are what it deliberately left:

1. **Container as job OUTPUT.** The point of the whole exercise, and still not modelled: an order
   names the box its materials come from, and the output belongs in the same one. Every scheduled
   job now carries `order_id`, which is the hook. Needs a UI answer for "no container bound" — corp
   hangars need the Director role and not everyone has one.
2. **Print locking ACROSS orders.** Per-order copy RUNS are consumed correctly, but two orders
   sharing one BPO each see it and may each schedule a concurrent job off it. Fixing it properly
   means making the print a scheduling resource rather than a per-plan cap — bigger than it looks,
   and it only bites an account planning apart with a single original per type.
3. **No UI.** The account setting and `/api/industry/queue-plan/compare` are endpoints only; nothing
   on the build page offers either. Deliberate — the measured cost of splitting was the gate on
   going further, and it is now known (+2.45% net on a 2× Archon, +0.96% on a Phoenix queue).

## 2e-residual. No browser tests for any frontend behaviour (2026-08-03)

`test_nav_gating.py` (17 assertions) is entirely string matching against CSS and JS — weak by
construction (renaming a class breaks it, an overridden rule passes it) and the only guard on nav
gating. It is a proxy, not a proof. The `lint-js` CI job is the first real step; a browser harness
is still absent, and introducing one was **dismissed** for `api()`/`toast()` specifically (see
Closed) on the grounds that manual testing catches the residual breakages at the expected rate.

Reopen this if that stops being true — a second UI regression that a browser test would have caught
is the evidence to act on.

## 3. Hand-built / custom colony layouts

Hybrid-colony detection shipped. Broader tracking of player-designed layouts (colonies that don't
match any template we generate) is still unscoped.

- **First step:** decide what the feature would actually *do* for the user before building anything
  — detection alone has no action attached to it today.

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
| — | Alliance-shared build structures as suggestions (`industry_group_structures`) | 08-05 |

---

## Closed — do not reopen without new evidence

| Item | Verdict |
|---|---|
| Layout engine: intermediate storage facilities + simulated CPU/PG fit | **Won't build** (2026-08-05). Both were documented gaps for months with no demand signal: the generator routes intermediates tier-to-tier instead of buffering them through storage, and `compute_resources` estimates the fit from idealised pin coordinates. `FIT_HEADROOM = 0.10` exists precisely so the estimate need not be exact — it leaves ~10% of both budgets free so a template that fits on paper fits in the client. Reopen if an exported template is actually rejected in-game, which is the evidence neither gap has ever produced. |
| Per-account settings consolidation (`settings_store.py`) | **Won't do** (2026-07-30). The duplication is the cheap part (~60-80 lines of upsert); validation, which dominates the handlers, survives any scheme. 2 of 7 tables aren't settings rows at all. Trades typed columns for a JSON blob against this repo's additive-migration convention. Prod holds only 10 rows total, so the old "too risky" framing was wrong — it's low *value*, not high risk. |
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
