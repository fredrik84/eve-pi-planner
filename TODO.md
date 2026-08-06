# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped is in the one-line list at the bottom, with
the reasoning in CLAUDE.md and the git log, and everything reasoned-through-and-rejected is in
**Closed**. Items in that table should not be reopened without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

Reviewed 2026-08-05.

---

## 18. Is all of this too complicated? — storage shape and precomputation (2026-08-05, LARGE)

A step back from feature work: **have we ended up doing this the hard way?** Two halves, and they
are related only in that both are about paying repeatedly for something that could be paid for once.

**Half A — storage shape.** The account's configuration is spread across a lot of typed columns in a
lot of tables. There are ~62 `pp_*` tables, of which the settings-shaped ones alone include
`pp_industry_settings`, `pp_reaction_settings`, `pp_account_reaction_settings`, `pp_alert_settings`,
`pp_notification_prefs`, `pp_notification_settings`, `pp_market_config`, `pp_job_config`,
`pp_plan_config` and `pp_source_sets`. The proposition to test: **the default configuration should be
a simple keyed blob** — one readable, serialisable object per account — rather than a column per
setting spread over a table per feature.

**This partly reopens a Closed item, deliberately and with new evidence.** "Per-account settings
consolidation (`settings_store.py`)" was closed **Won't do** on 2026-07-30, and its reasoning still
has to be answered rather than ignored: the duplication is the cheap part (~60-80 lines of upsert),
*validation* dominates the handlers and survives any scheme, 2 of 7 tables weren't settings rows at
all, and a JSON blob trades typed columns against this repo's additive-migration convention. What has
changed since:

1. **A worked example exists.** A tester supplied a real ravworks config export — one flat keyed JSON
   object carrying structures, rigs, declared slots and skills, per-category build allocation,
   job-length settings, blacklists and tax, with a `cookie_version` for versioning. It is shared
   alliance-wide and it works. See [docs/tester-feedback-2026-08.md](docs/tester-feedback-2026-08.md).
2. **Export/import is now wanted** (T13). The July verdict never considered serialisation; a config
   that must leave the account and come back changes the blob from a tidiness question into a
   load-bearing one.
3. **The settings surface is about to grow a lot.** Manual structures, manual blueprints, declared
   slots, a job-length policy and per-category build sites are all planned. "Prod holds only 10 rows
   total" was the July calculus and it is about to stop being true.

**Half B — precomputation.** How much of what a page load costs is recomputed every time for an
answer that did not change? There is prior art in both directions and the audit must read it before
proposing anything: `docs/industry-planning.md` ("Industry performance: one plan per page load" — the
graph cache, the inline install/progress blocks, the sessionStorage plan cache) shows real wins
already taken, and the Closed entry "Frontend CPU offload, phase 3" records an investigation that
found the hotspot was *cacheable server-side*, not a JS-offload candidate. The open question is what
is left: which reads rebuild a graph, re-resolve prices, or re-plan a queue to answer something that
could have been precomputed, and where the invalidation boundary honestly sits.

**First step is measurement, not restructuring.** The one thing that would make this item go wrong is
adopting the blob because it sounds simpler. Before any schema is touched:

1. Instrument a real page load on prod, per service, and record where the time and the queries
   actually go. Use the in-process prod debugging path in CLAUDE.md rather than reasoning from the
   code.
2. Count the true settings surface — which tables are genuinely per-account configuration, which are
   ledgers, caches or shared data that must NOT move into a blob (the July verdict's "2 of 7" point,
   re-counted against today's schema).
3. Answer the July objections explicitly: where does validation live under a blob, and what replaces
   an additive `ALTER` when a field's meaning changes.
4. Only then propose a shape — and it may legitimately come back "the storage is fine, the
   precomputation isn't", or the reverse.

Worth noting as evidence for the audit rather than as separate items: the schema carries visible
accretion — `pp_baskets_old`, `pp_profiles_new`, `pp_session` alongside `pp_sessions`,
`pp_characters_context`, and two pairs of near-identically-named settings tables. Whatever the
verdict on the blob, that is worth a pass on its own.

## 14. Roll Industry out, or write down why not (2026-08-05)

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

## 15. `industry_per_order_plans` should not sit at `testers` half-landed (2026-08-05)

The flag's own description states its purpose: *a job outputs to exactly ONE container, so a batch
shared between two builds has nowhere to deliver — this is what lets a builder run a container per
customer.* Container-as-output is not modelled (2f-residual #1), and the setting has no UI
(2f-residual #3, with `available` already sitting in the read response for a frontend nobody wrote).

So what is rolled out to testers today is the half that **costs money** — planning apart is +2.45%
net on a 2× Archon, +0.96% on a Phoenix queue, measured — without the capability that spends it.
The compare endpoint is the manifesto's rule followed exactly (put the number on it before the
switch); the problem is the rung, not the design.

Two acceptable outcomes, no third: land 2f-residual #1 and #3 together and keep it at `testers`, or
drop the flag to `hidden` until they land.

## 16. Remove the dead Industry surface (2026-08-05)

Three things are maintained with no caller. One commit, no behaviour change:

- **`app/industry/advisor.py` + `industry_skill_advisor` + `/api/industry/skill-advisor`.** The
  rendering was removed on purpose (`industry.js:63` — training advice is not about THIS build, and
  a card telling a character to start Industry I is not what somebody checking on a running build
  came for). That reasoning is right; the conclusion left 255 lines, an endpoint and a flag behind
  it. Delete it, or give it a home where training advice belongs — which is not the build page. The
  PI half (`skill_roi_for`) is already shared and unaffected either way.
- **`/api/industry/to-install`.** Superseded by the inline `install` block that rides along with
  `queue-plan`.
- **`/api/industry/skill-coverage`.** No caller; `analyze_plan_skills` is called directly by both
  plan paths.

Per [docs/manifesto.md](docs/manifesto.md), residue is removable rather than backlog — the point of
this item is to actually remove it.

## 17. Stock sources have four surfaces (2026-08-05, low)

One concept — *which boxes may this build spend* — is expressed in the plan modal's "Materials
from", the sourcing panel's "Pulling from", Setup → Stock on hand's tick list, and saved source
sets, under **two** ownership models that coexist behind `industry_plan_sources` (account-wide tick
list vs. a build owning its boxes). `plan_source_keys` exists solely to reconcile them per request.

Altitude, not correctness — every surface is individually justified and the feature is right. Worth
scoping only once `industry_plan_sources` settles which ownership model wins, since that decision
removes one of the two on its own.

## 12-residual. The user-facing workflow has no home in the product (2026-08-05)

Item 12 shipped as two documents (see Shipped). The user-facing one was written as "a candidate for
the How-it-works page or onboarding" and is still only a doc — which is one of the audit's own
findings: **nothing in the product states the path.** The How-it-works page is Planetary Industry
only (one mention of "Planetary Industry", none of manufacturing or reactions), and the Industry
onboarding covers setup and stops. Steps 1-9 exist nowhere a user can read them.

Blocked behind item 14 rather than open: writing the tab's workflow into a page for an audience that
cannot open the tab is work in the wrong order. Pick the rung first.

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

**#1 and #3 are one piece of work, not two.** The 2026-08-05 audit made the link explicit: #3's
flag (`industry_per_order_plans`) exists *because* of #1 — a job outputs to exactly one container,
so a shared batch has nowhere to deliver — which means the flag currently ships the half that costs
ISK without the half that justifies it. Do them together, or neither; see item 15 for the rung
decision in the meantime.

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
| 12 | The Industry flow end to end, twice: `docs/industry-workflow.md` (nine steps + the module/endpoint/table map) and `docs/industry-workflow-user.md` (the same path for the user). Written from a read of the 22 modules and `static/industry.js`, not from memory; the judgement-bearing half is fenced under Observations. UI home still open — see 12-residual | 08-05 |
| 13 | `docs/manifesto.md` — purpose, target state and honest gap for PI (an end state), Reactions (a business in its own right) and Industry (a direction), plus the five questions a feature is scored against and what a failing score means | 08-05 |
| — | `docs/industry-audit-2026-08.md` — item 12 re-run against the manifesto: the every-time path cleared, `industry_per_order_plans` and `industry_skill_advisor` failed, two dead routes found, and the live flag read that corrected two of the pass-1 claims | 08-05 |
| — | Alliance-shared build structures as suggestions (`industry_group_structures`) | 08-05 |
| — | Pin a rig FAMILY to a structure and every job in it is installed there, whatever the routing scores (`pp_industry_settings.build_pins`, on the `industry_rig_routing` flag). A pin can only pick among sites already legal for that job's activity; one it can't honour falls back to the automatic routing and says so. The pin decides WHERE, `fittable_families` still decides what BONUS | 08-06 |

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
