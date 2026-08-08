# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped is in the one-line list at the bottom, with
the reasoning in CLAUDE.md and the git log, and everything reasoned-through-and-rejected is in
**Closed**. Items in that table should not be reopened without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

Reviewed 2026-08-05.

---

## 19. Reactions must say what you need to ACQUIRE, and show the stages in order — DONE 2026-08-07

Two defects found by using the tool on a real account (context 1, ~238 hand-declared formulas across
41 products) right after the declaration work shipped. **Both shipped 2026-08-07, nothing open** —
kept here for the reasoning, which is load-bearing and should not be re-derived.

### 19a. An undeclared formula is read as "unknown", so plans include stages you cannot run — DONE 2026-08-07

Observed: a customer order for **Reinforced Carbon Fiber** correctly capped at the 10 formulas
declared for it, then happily suggested reacting a pile of **Carbon Fiber** — a formula the account
does not hold. The chain traverses into sub-reactions without checking whether the user can run them:

```
Reinforced Carbon Fiber (57457) via formula 57493 consumes
   Carbon Fiber           x200   sub-reaction formula 57490   <- NOT declared
   Oxy-Organic Solvents   x1     sub-reaction formula 57491
   Thermosetting Polymer  x200   sub-reaction formula 57494
```

The cause is the deliberate rule that **absent evidence never serialises work** — a product nobody
has said anything about stays uncapped so the tool never refuses work the player can really do. That
is correct while the app genuinely does not know. It stops being correct once the user has declared
their whole library, because then **absence is knowledge**: not in the 238, not owned. The app
cannot currently tell a complete declaration from a partial one, so it takes the permissive reading.

**Decided (2026-08-07), two parts:**

1. **Completeness is inferred from a PASTE**, not from a toggle. Pasting an industry window is a
   complete statement about that character — the same reasoning already used for the
   paste-overrides-floor rule in `formula_print_floor`. No new knob (CLAUDE.md rule 3).
2. **Report what to ACQUIRE, mirroring Industry** rather than silently substituting. Industry
   already solves exactly this shape: `metrics.missing_blueprints` (built in
   `app/industry/schedule.py`, rendered by `_indMissingBpWarn` in `static/industry.js`) lists what
   the build cannot proceed without, with runs needed and a contract price, and is deliberately kept
   out of both `shopping_list` and the cost total. Reactions needs the same thing for formulas —
   *"you don't hold a formula for these"* — and it must behave identically from **all three** entry
   points: the Suggest wizard's multistage chains, a manual assign, and a customer order.

**⚠ The sharp edge, flagged before building:** making absence load-bearing means a formula whose
NAME fails to resolve silently becomes "you don't own this". That already happened once — a client
copy carried `Fullerides Reaction Formula` where the SDE has `Fulleride Reaction Formula`, fixed in
`ee633be` with a product-name fallback. Any implementation must make unresolved names **loud**, not
merely reported, or a rename turns into a wrong plan.

**Shipped 2026-08-07** behind `reactions_missing_formulas` (admin-preview). New `app/reactions/
library.py` is the whole inversion, in one place: completeness = at least one pasted batch naming a
formula (typed-in rows never qualify), held = the UNION of declared/ESI/stock/observed evidence, and
`missing_formulas()` reports `{complete, formulas, unresolved}` without touching a plan — no step
dropped, nothing flipped to a market buy, nothing in a shopping list or cost total. Rows carry the
FORMULA's type_id and are priced off the same public-contract index Industry uses (`bpc.py`'s new
`blueprint_type_prices`, since a formula has no row in `blueprints`). All three entry points render
it off that one helper: suggest (inline), order report (inline, off the quote's own `sequence`),
manual assign (`POST /api/reactions/missing-formulas`, cached per product). The sharp edge is
handled by KEEPING unresolved paste names (`pp_blueprint_paste_unresolved`, replaced per batch,
deleted with it) and showing them in the warning itself. `test_missing_formulas.py` pins both
directions. Rationale in `docs/reactions.md`.

**Residual closed 2026-08-07:** the dashboard now runs the same check over what is already planned
(`_plan_missing_formulas`, off every not-yet-running slot, rendered above the install checklist), so
a plan committed before the flag went on — or one whose formula has since been sold — is flagged
where the player is looking at it. Pinned in `test_missing_formulas.py`.

### 19b. The stages are sequenced correctly and the UI throws it away — DONE 2026-08-07

The backend has always modelled this: every `pp_reaction_assignments` row carries `tier_order`, the
dashboard query is `ORDER BY tier_order`, `tier_order` **is** in the API payload, and
`_concurrent_load` counts the WORST tier rather than the sum precisely because tier 0 must finish
before tier 1 can start.

**`tier_order` appears nowhere in `static/reactions.js`** — zero references. The frontend receives
the ordering and renders every stage flat, so tier 0 and tier 1 sit side by side with nothing saying
which must finish first. The user reads that as "it isn't sequencing"; it is, invisibly.

Fix is presentational only — group pending slots by stage, label them ("Stage 1 — start now",
"Stage 2 — after stage 1 finishes"), grey what is not yet startable. No backend change.

**Shipped 2026-08-07**, frontend only (`reactions.js`, `style-layout-admin.css`): planned slots sort
by `tier_order` and carry an `S<n>` badge, later stages are dimmed/dashed, and the "To install"
checklist is split under stage banners. The stage number is `tier_order + 1` **absolute**, not
re-ranked against what is still pending — once stage 1 is running its rows leave `pending`, and
re-ranking would tell you to start stage 2 while its input is still cooking. Rationale is in
`docs/reactions.md`.

## 23. Stages were positions in a list, not dependencies — DONE 2026-08-08

Reported from real use: *"Carbon Fiber and Oxy-Organic Solvents can be done at the same time... They
only need moon goo and fuel blocks. All of these should be just made at the same time and then stage
2 should be Reinforced Carbon."* Correct, and the tool was wrong.

Every insert path stamped `tier_order` with `enumerate(...)` over the chain-tier list, so
Reinforced Carbon Fiber's three inputs — each ONE reaction off raw goo, with no dependency on each
other — became stages 0/1/2. Item 19b then faithfully rendered that as Stage 1/2/3 with two greyed
out, and 21a's `_concurrent_load` counted three simultaneous jobs as one reactor (under-counting
capacity, the opposite of what 21a set out to fix).

**Fixed 2026-08-08, no flag** (a defect, not a feature). `_resolve_reachable` carries `depth`
(leaves 0, else `1 + max(input depths)` — deliberately not `reaction_count`, which is subtree size);
`tier_ranks()` gives dense stages where equal depth means one stage; all three insert paths use it;
`ChainTier.tier` carries it from client to server with a graph re-derivation as the authority and
list position only as a last resort. A stage's load is now the SUM of its steps in both the advisor
and the assign guard. `restage_plan_rows` repairs rows already stored under the old rule on
dashboard load — idempotent, graph-guarded, verified against the real 57457 chain (0/1/2/3 →
0/0/0/1).

**Plus the follow-up ask — "tell me when stage 2 can start".** `chain_stage_state` reads ESI job
states: `ready`/`delivered`/past `end_date` means finished, so a stage is done when all its rows
are, and the next stage is READY when everything below it in its own chain is done. The dashboard
shows a green "Stage N is ready to start" banner, un-greys that stage's slots and relabels it.
`test_parallel_stages.py` covers staging, readiness and the repair; rationale in `docs/reactions.md`.

**The push shipped 2026-08-08 too:** `reaction_stage_ready`, a twelfth alert kind computed off the
same `chain_stage_state` the page renders, so a notification and the screen cannot disagree. Gets
mute/severity/Dashboard-card/Pushover-ntfy-Discord plumbing for free; 12h cooldown since a ready
stage stays ready. Fixing its dedupe fixed a live bug: `_process_context` only applied the cooldown
to alerts with a `planet_id`, so BOTH existing reaction kinds re-sent on every 15-minute tick of
every scheduler process. Alerts now carry their own `dedupe_id`. `test_alerts.py`.

**Run counts tidied 2026-08-08** (`reactions_tidy_runs`), from "align the number of runs a bit
better — it's all over the place and that makes it annoying to start the jobs": an intermediate
step's per-job run count rounds UP to the largest tidy step within 15% of the requirement (79→80,
41→45, 213→225), never down, never the end product, never under 10 runs. Applied at
`_insert_assignment_rows` and mirrored in the wizard so preview and plan agree; the shopping list
takes a `planned` map so it buys for the rounded runs rather than the bare requirement.

**Still open:** a MANUAL "mark this stage done" for work ESI cannot see — a corp job installed by a
character without the Factory_Manager role never appears in any job list we can read, so its stage
can never complete on its own.

## 21. Idle reactors while a chain waits — slots are counted twice and stages never widen (2026-08-07)

From using the tool: *"if we need to make stage 1 stuff, why do we occupy slots with stage 2 or 3
unless we have slots to spare"* and *"we should run multiple stages at the same time if we have
slots to spare"*.

**The premise correction first, so nobody re-derives it:** within ONE chain, stage 2 cannot start
before stage 1 finishes — EVE requires the materials to exist at install time, and stage 1's output
IS stage 2's input. That is not our sequencing choice, and it is exactly why `_concurrent_load`
counts the worst tier. What follows is everything around that constraint that we ARE leaving on
the table.

**21a and 21b shipped 2026-08-07** behind `reactions_parallel_stages` (admin-preview):
`_concurrent_load` is now THE slot model — the guard, `_character_capacities` and the dashboard all
ask it, the advisor reserves its own peak across tiers, and the manual-assign modal reserves
`chainPeak + 1` instead of `chainJobs + 1`. `_widen_to_idle_slots` (advisor.py) then spends
genuinely idle reactors on whichever step gains the most hours, capped by formulas held and runs
available, reported as `totals.idle_slots_used`. Runs, cost and profit are untouched.
`test_parallel_stages.py` pins both directions; rationale in `docs/reactions.md`.

**21a. Two slot models, and they disagree.** `_concurrent_load` (`jobs.py:455`), the guard that
decides whether an assign fits, counts the WORST TIER. `_character_capacities` (`jobs.py:988`) and
the dashboard's `free_slots` (`jobs.py:858`) count EVERY pending row. So a 3-stage chain of one job
each is authorised as needing 1 slot and then reported as occupying 3. `_character_capacities`
feeds the wizard's bin-pack and the customer-order allocation, so both under-allocate real work;
the manual-assign modal compounds it by reserving `chainJobs + 1` on one character.

**21b. A stage never widens into free slots.** `advisor.py:346` sizes a chain tier at
`ceil(runs × cycle / cadence)` — just enough jobs to finish within the cadence window — and never
asks whether eight free reactors could finish it sooner. Tiers are sequential, so an N-tier chain
then takes ~N cadence windows with one reactor working and the rest idle.

**21c. Intermediates in stock are ignored.** Reactions never reads `pp_asset_stock` for a chain
tier: `_explode_chain_tiers` always plans the whole chain from raw goo. Hold the intermediate
already and the tool still tells you to react it first, when the next stage could start now.

**Shipped 2026-08-07** behind `reactions_use_stock`. `reaction_stock_pool` (enabled sources, via
`owned_quantities`) is threaded through `_explode_chain_tiers` / `_ordered_chain_tiers` /
`_explode_shopping_list` and CONSUMED as the walk goes, so a unit is spent once per plan; a tier the
holding covers outright is dropped along with everything below it. Wired into suggest (one pool for
the run), the order report, order allocation (consumed host by host), adopt-orphan, and
`assign_reaction` — which trims the client-supplied tiers, since the opportunity list stays
stock-blind on purpose (it is cached and its tiers get scaled linearly, which stock coverage is
not). Coverage is always reported (`stock_covered` / an assign-time toast). `test_reaction_stock.py`.

**Known edge, accepted and documented:** no reservation ledger — two separate planning runs both
see the same units, so stock can be promised twice across plans. Within one plan it cannot.

**21d. Pipelining.** Split stage 1 into N batches; when the first completes, start stage 2 on that
output while batches 2..N still run. The honest version of "run stages at the same time", and real
throughput — but it needs partial-output tracking and changes what an assignment row means.
**Open, and only worth it if 21a-c don't satisfy the complaint.**

Order: 21a → 21b → 21c (all done) → 21d, which is the only one left.

## 22. The general shopping list double-counts every chain — DONE 2026-08-07

`reactions_shopping_list` exploded EVERY pending assignment row down to raw leaves. A chain assign
stores a row per tier AND a row for the product, so the top row's walk already included the
intermediate's materials — and then the intermediate's own row added them a second time. Measured on
a two-tier synthetic chain: 7,726 goo became 15,452. **The player bought twice what they needed for
anything multi-tier.** The per-order materials report was never affected (it explodes once from
`target_qty`).

**Fixed in place, no flag** (a defect in a shipped feature, CLAUDE.md rule 2). `_shopping_roots`
keeps only the rows nothing else covers: a chain is identified by the assign that wrote it —
`(character_id, created_at)`, which all three insert paths already produce in one call with one
timestamp — and within a group only the HIGHEST tier is a root. A separate assign of the same
product is its own group and still counts, so a product deliberately assigned on its own to sell is
never swallowed. A group whose top row was cancelled falls back to what remains.

Rejected: "skip anything another assigned product consumes" (would eat a deliberate standalone job)
and a new `chain_id` column (could not group the rows already in the table). The list now also
spends held stock (21c), so it stops asking for goo to make an intermediate that is in the hangar.
`test_shopping_roots.py`.

## 20. The two Reactions "clear" paths disagree about customer orders — DONE 2026-08-07

Found while diagnosing the Clear-all button (whose actual bug was a native `confirm()`, fixed in
`6c17728`).

- `_clear_assignment_group` (per-product re-assign) deliberately **skips** order-linked rows:
  *"Rows raised by the customer-order flow belong to an order that was committed against real
  capacity; a suggestion re-assign must not silently eat them."*
- `unassign_all_reactions` ("Clear all") deletes them — no `order_id` filter — and leaves
  `pp_reaction_orders.assigned_runs` untouched.

Because `assigned_runs` is monotonic, Clear all could leave an order claiming its full run count
with zero assignment rows: it looked fully assigned, scheduled nothing, and could not be
re-assigned. Orders #36-#39 on context 1 were in that shape (`assigned=2000, rows=0`).

**Fixed in place, no flag** (a defect in a shipped feature). Of the two honest options, Clear all
now **clears order rows AND hands the runs back** — the button says clear all, and the asymmetry
with its sibling is kept deliberately: a per-product re-assign is a narrow action, Clear all is the
player saying clear everything. The credit is the TOP row of each chain (what `assigned_runs` was
incremented by — `_shopping_roots` already identifies exactly those rows), clamped at zero, and the
response reports which orders moved so the UI can say so. The confirm text and a toast now spell it
out. Orders ALREADY stranded are repaired by `_heal_stranded_counter` on the assign path — narrow
by design: only an open order with a counter and no rows at all, and only when the player takes a
deliberate action, never on a read. `test_clear_all_orders.py`.

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

## 16. Remove the dead Industry surface — DONE 2026-08-07

Three things were maintained with no caller. Removed in one commit, no behaviour change:

- **`app/industry/advisor.py` + `industry_skill_advisor` + `/api/industry/skill-advisor`** — deleted
  outright, engine and flag and `test_skill_advisor.py` with it. The rendering was removed on
  purpose months earlier (training advice is not about THIS build); nothing replaced it and nothing
  imported the module but its own endpoint registration. The PI half (`skill_roi_for`) is a
  different module and is untouched. If training advice returns, it belongs on a page about the
  character.
- **`/api/industry/to-install`** — a four-line wrapper that re-planned the queue and called
  `install_block`. The checklist now only ever comes from `res["install"]` on a plan that was
  already computed, so "the checklist and the plan disagree" is impossible by construction rather
  than fixed by convention. `test_industry.py`'s guard was re-pointed at that property instead of
  at the deleted function's source text.
- **`/api/industry/skill-coverage`** — no caller; `analyze_plan_skills` is called directly by both
  plan paths.

976 checks in `test_industry.py` still pass, plus `test_required_skills.py`, `test_job_summary.py`
and `test_features.py`. Docs updated in `industry-workflow.md`, `industry-planning.md`,
`industry-planner-spec.md`, `manifesto.md` and the audit.

## 17. Stock sources have four surfaces (2026-08-05, low)

One concept — *which boxes may this build spend* — is expressed in the plan modal's "Materials
from", the sourcing panel's "Pulling from", Settings → Blueprints & formulas → Stock on hand's
tick list, and saved source
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
| 19b | Reaction plan STAGES on the dashboard: planned slots sort by `tier_order`, carry an `S<n>` badge, later stages dim/dash, and the "To install" checklist splits under stage banners. The number is `tier_order + 1` absolute, never re-ranked against what's still pending | 08-07 |
| 16 | Dead Industry surface removed: the unrendered skill advisor (module, endpoint, flag, test), `/api/industry/to-install` and `/api/industry/skill-coverage`. No behaviour change; the checklist-vs-plan guard is now structural | 08-07 |
| 20 | Clear all no longer strands a customer order: order rows are cleared AND the order's `assigned_runs` handed back (top row per chain, clamped at 0), with already-stranded orders repaired on the next assign. `test_clear_all_orders.py` | 08-07 |
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
