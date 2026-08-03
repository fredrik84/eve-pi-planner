# eve-pi-planner — TODO

Live backlog. Reviewed 2026-07-30. Anything not listed here is either shipped (see the git log /
release notes) or in **Closed** at the bottom — items in that section have been reasoned through
and should not be reopened without new evidence.

Each item states what it is, why it's open, and the first concrete step, so it can be picked up
cold.

---

## 1. Required-skills-to-build (Industry) — SHIPPED 2026-07-30, behind `required_skills`

Built and live in admin-preview. `blueprint_skills` (SDE, from `activities.<activity>.skills` for
BOTH manufacturing and reactions — 9,221 rows, 83 distinct skills), `pp_char_skills` (per-character
full skill list), `app/industry/skills.py`, and a panel on the Industry plan. `test_required_skills.py`,
19 assertions.

One premise in the original write-up was wrong and worth remembering: **no new ESI fetch shape was
needed**. `_fetch_skills` in app/esi.py was already pulling the character's entire skill list from
`/characters/{id}/skills/` and discarding all but the ~10 in `SKILL_IDS`, so this needed no new call,
no new scope, and no extra rate-limit budget — only somewhere to put the rest.

`assign_characters` was made skill-aware on top of this (2026-07-30): candidates are tiered
capable → unknown → incapable, capacity still decides within a tier, and a forced fallback is
stamped `skill_ok: False` rather than hidden. Wired into both `/api/industry/plan` and the queue
plan.

Follow-ups, neither blocking:

- **The to-install checklist is still skill-blind.** `install_block()` in `app/industry/orders.py`
  (behind `POST /api/industry/to-install`, and inlined into the queue plan) derives its OWN
  assignment for the ready wave: it walks `_slot_pool`'s free slots most-loaded-first and names a
  character per job, ignoring what `assign_characters` already decided. So the main screen's
  "start these now" list can still tell you to install a job on a character who lacks the skills,
  even though the plan below it marks that same job ⚠. Note the data is already in hand — wave 0's
  tasks carry `skill_ok` and an assignee by the time `install_block` sees them, so this is about
  reconciling two assignment paths, not fetching anything new. The honest fix is probably for
  install_block to respect the scheduler's assignment instead of recomputing one; check first
  whether its most-loaded-first ordering is doing something the scheduler's spread-the-work rule
  isn't, because that difference is deliberate.
- **Item 2 (skill-optimization advisor) is now unblocked** — it was waiting on the full-skill-list
  fetch, which `pp_char_skills` now provides.

## 2. Skill-optimization advisor (Industry) — SHIPPED 2026-08-01, behind `industry_skill_advisor`

`app/industry/advisor.py` + a card on the Industry tab. Slots and job-time are ranked against each
other by reducing both to the same unit — percent more jobs finished per day — then sorted by gain
per SP. Slot skills are suppressed unless the pool is ≥70% busy (an unfilled slot produces nothing;
an idle pool gets an "already trained, go deploy" note instead); job-time skills always pay and are
always offered. PI advice is REUSED, not reimplemented: `skill_roi` was split into an endpoint plus
`skill_roi_for(context_id)`, reported under its own key because ISK/day cannot be ranked against a
throughput percentage. `test_skill_advisor.py`, 24 assertions.

Two things worth remembering:

- **Skill ranks are hardcoded** (6 constants, verified against the live SDE). The only source is
  `fsd/typeDogma.yaml`, which costs ~1.8 GB peak RSS and ~19s to parse — an OOM risk at pod startup
  against a 2Gi limit, for integers that never change.
- **Cheapest next level is not the lowest-rank skill.** With Mass Production at IV, +1 slot costs
  421,490 SP that way but only 2,000 SP via Advanced Mass Production 0→I, because level V is where
  the SP curve explodes. The code picks by SP; a test that assumed "low rank = cheap" was the thing
  that was wrong.

## 2b. Job-time skills: real values, once they can be proven (2026-08-01)

`account_industry_time_mults` used to read `if ind == 0 and adv == 0: ind = adv = 5`, silently
upgrading an untrained account to V/V and quoting it a build ~47% faster than it can do.

Fixing it turned out to hinge on telling a real 0 from a stale column, and TWO plausible signals
were tried and rejected against real data before the right one:

- **ESI skills scope** — rejected. It proves the character was scanned at some point, not that a
  given column was filled. Would have jumped 13 of 26 accounts' job times by +47%.
- **"any industry-era column is non-zero"** — also rejected, and provably: two accounts show Mass
  Production V with Industry 0, which the game forbids (the SDE lists Industry III as a prerequisite
  of Mass Production). `mass_production` was added to the scan before `industry`, so a populated
  column says nothing about its neighbour.

What shipped: only a `pp_char_skills` row set counts, because the full ESI list records absence as
well as presence. Everything else keeps the V/V fallback and reports `skill_time_basis: "assumed"`,
which the plan surfaces as a warning. So this changed nothing on deploy (0 rows in prod) and
upgrades account-by-account as `required_skills` rolls out and characters rescan.
`test_skill_time_mults.py`, 15 assertions, including the exact stale-column shape prod showed.

## 2c. Running a build, not just planning one — SHIPPED 2026-08-02

Four requests from builders using the Industry tab, each behind its own flag: `industry_blacklist`
(always-buy list), `industry_manual_done` (tick a build step done by hand), `industry_corp_assets`
(read corp hangars/containers over ESI for directors), `industry_sourcing` (per-order material
checklist bound to the container the build is gathered into). Design notes in CLAUDE.md under
"Industry: running a build, not just planning one"; 40 new assertions in `test_industry.py`.

Two consequences worth knowing before touching this area:

- **Two new ESI scopes** (`esi-assets.read_corporation_assets.v1`,
  `esi-corporations.read_divisions.v1`) joined the unified superset, so every existing character
  holds a token without them until it is re-authorised. Only the corp scan needs them; the UI says
  who to reconnect rather than failing vaguely.
- **`pp_asset_sources.scope`** now records which scan owns a source, and a re-scan replaces
  everything that scan owned. Before this, a container that had been emptied kept its last known
  contents forever — the personal scan had the same bug, and this fixed it there too.

Open follow-ups, neither blocking:

- The sourcing panel plans the order on its own (one `build_plan` per open). Fine for a handful of
  orders; if someone queues 30, this becomes the page's most expensive call and wants the same
  treatment the to-install checklist got (derive it from a plan that already exists).
- Manual "done" marks are per TYPE, so two orders needing the same component share one tick. That is
  consistent with how every other progress signal works here, but it means a mark can't say "done
  for THIS order" — worth revisiting only if someone actually hits it.

## 2d. Industry first-use onboarding — SHIPPED 2026-08-03

Prompted by the first two external testers, who may never have set up Reactions. Three sharp edges
fixed first: the tab no longer blocks on a configured build structure, the structure-search empty
state carries the Connect-a-market-character button instead of pointing at a "step 1" that only
exists in the Reactions gate, and the missing-build-system warning links to the panel that can
actually set it.

Then the setup screen itself (`_indRenderWizard`), mirroring `_rxApplyGate`: where you build /
characters & slots / build system, with `pp_industry_settings.onboarded` per account. Design notes
in CLAUDE.md under "Industry: first use". The rule to preserve if this is touched again: **every
step must be completable without leaving the page and Save & continue is never disabled** — the
screen it replaced could strand a PI-only player behind a prerequisite chain they had no way to
finish.

Worth knowing: existing accounts are backfilled to `onboarded = 1` from having saved build options,
which stays correct across restarts ONLY because the frontend refuses to seed a settings row for an
account that hasn't finished setup. Those two live in different files and are one rule.

Not done: no browser test covers the wizard (same gap the rest of the frontend has, see item 6).

## 2e. Test suite: what the assertions are actually worth (2026-08-03)

Audited after the industry work pushed `test_industry.py` to ~500 assertions. Cut to 450 by removing
checks that matched SOURCE TEXT rather than behaviour — they break on harmless refactors and would
still pass if the logic broke. `test_page_access.py`'s one `inspect.getsource` was converted to ask
the router's route table instead, which is the same length and actually true.

**The trim earned its keep by finding a live bug**: a deleted test had been passing for the wrong
reason, and chasing why exposed that a type with no consumer was paced against the whole queue's
makespan — a 20-run deliverable taking an hour alone became a ten-hour job beside a 100-hour order.
Fixed in `71ffff8`. Worth remembering the next time a test looks redundant: check WHY it passes
before deleting it.

Deliberately kept, with eyes open:

- **Source checks that assert ABSENCE** — `test_customer_build_status_leaks_nothing` scans the
  payload builder for banned cost words. Inspection is the right tool for "this field must never
  appear"; a behavioural test can only prove the fields that ARE there.
- **`test_nav_gating.py` (17 assertions)** is entirely string matching against CSS and JS files. It
  is weak by construction — renaming a class breaks it, an overridden rule passes it — but it is the
  only guard on nav gating and there is no browser test infrastructure. It is a proxy, not a proof.

Still open: no browser tests for any frontend behaviour (see item 6 for the eslint work that would
be the first step).

## 2f. Stop sharing job runs between orders — DESIGN, not yet built (2026-08-03)

**The physical fact this rests on: a job outputs to exactly ONE container.** Capital builders run a
container per build — it is both where the materials are sourced and where the output lands, and it
is how they know what is finished for which customer when three orders are in flight. A batch shared
between two orders has nowhere to deliver. That is not a preference about tidiness, it is a
constraint, and the current design contradicts it.

Today `aggregate_demand` deliberately combines every queued order into ONE demand and builds each
shared component once (`app/industry/orders.py`, `schedule.py`, documented in CLAUDE.md and in
`progress.py`'s module docstring). It is right for cost and wrong for how the work is actually run.

**What changes**

- Plan each order on its own — its own quantity, overrides, ME/TE, blueprint copies, runs. The
  machinery already exists: `sourcing._order_requirement` and `shares._order_plan` both do exactly
  this today, for exactly this reason.
- The queue's job is then SCHEDULING those per-order jobs against one shared slot pool, plus
  aligning them (see the pace/compaction rules in CLAUDE.md) so a builder still logs in once.
- Container becomes input AND output on the order, not just a source.

**What it costs, honestly**

- **Builds get more expensive.** Shared-batch savings disappear: two orders needing the same
  component build it twice, and buy two sets of blueprint copies. The user has accepted this; it
  should still be stated in the UI rather than discovered.
- **Rounding waste multiplies.** A reaction making 2/run rounds up per order now, not once.
- `_blend_margin` can go — it exists only because a shared batch has no per-order cost. Per-order
  planning gives each order its own real cost, which is strictly better for quoting.
- Progress stops needing to be per-TYPE-only (`progress.py`'s central compromise) and can be per
  (order, type). The manual-done grain follow-up in item 2c resolves itself.
- The share's "two different plans on purpose" note simplifies to one plan.
- **Cross-order alignment must become explicit.** It works today only as a side effect of
  aggregation; with orders separate, the pace has to be computed across all orders' jobs.

**Perf.** N plans per page load instead of 1. Mitigate by sharing ONE `prepare_plan_inputs` across
them (already the pattern in `_run_queue_plan(want_full=True)` and `force_build_above`) — the DB-heavy
half doesn't depend on which order is being planned.

**Migration.** No schema change for the split itself; `force_build_ids` / `me_te_overrides` /
`margin_pct` / `source_key` are already per order. The union logic in `_run_queue_plan` is what goes.
Queued orders keep working; their numbers move (up), which is worth saying in the release note.

**Containers are not universal.** Corp hangar containers need the Director role; everyone else has
personal containers or pasted stock. So per-order input/output labelling must degrade to "no
container bound" without breaking the plan — the sourcing panel already behaves this way.

**STATE (2026-08-03): half-built and deliberately unwired.** `schedule.plan_queue_per_order` exists
and is committed — per-order params, first-come-first-served stock allocation down the queue, its own
`_order_cost`, and namespaced scheduling keys (`Task.key` / `Task.order_id`) so one order's jobs can
never satisfy another's dependency. **Nothing calls it.** No endpoint, no flag, no UI. It changes
nothing until wired.

**What is left**
1. A `industry_per_order_plans` flag + request/account option, and a branch in `_run_queue_plan`.
2. A compare endpoint returning both plans' cost, makespan and job count — the numbers were promised
   and never delivered. Measure on the real Archon queue (context 1) before switching anyone.
3. Container as job OUTPUT as well as input (item 5), which is the point of the whole exercise.
4. Cross-order alignment has to become explicit — today it works only because aggregation puts every
   order in one pace calculation.

## 2g. Slot alignment — RESOLVED 2026-08-03, and how

Long thread with a capital builder. Jobs in one wave finished at 2h32m / 5h05m / 10h11m, so the
builder had three separate moments to log in at, and a third of their slots sat idle in between.

**The fix that worked: `_PACE_OVERSHOOT = 1.0`.** A job holding ONE run can only grow by taking a
second, which is a 100% increase *by definition* — so every smaller allowance tried first (5%, a flat
20 minutes, 2% of the makespan) was arithmetically incapable of merging a 1-run job however much
slack it had. Four rounds of tuning moved nothing and each null result got explained away as a
dependency constraint instead of checked against the arithmetic. Measured on the real 206-hour
Archon: **232 jobs → 159, +32 minutes (+0.26%)**. Plateaus at 100%; past that what remains is bounded
by runs available, blueprint copy caps and real dependencies.

Guards that must stay: `_DELIVERY_OVERSHOOT` (2% of makespan) is the real bound and protects the
quote; a **deliverable is exempt from overshoot entirely** (`no_consumer`) — the allowance buys slots
by finishing components later and a finished product has no later to give. Flipping the constant
without that exemption slowed a finished product, and the suite caught it.

**How to measure this again** — the thing that finally ended the guessing. There is a diagnostic
endpoint (`GET /api/industry/queue-plan/packing`, also POST) printing per type: runs, jobs, runs per
job, own/pace/consumer windows, which bound bit, and the consumer that set it. Better still, run the
planner in-process inside the prod pod:
```
ssh -o BatchMode=yes node02.failed.name \
  "sudo k3s kubectl -n production exec -i <pod> -- python3 -" < script.py
```
with `from app.industry.orders import queue_plan_packing; queue_plan_packing(None, <context_id>)`.
Monkeypatching `app.industry.schedule._PACE_OVERSHOOT` in that script gives a real what-if curve on
real data in seconds. Five rounds of reconstructing the user's build from descriptions were all
wrong; one probe settled it.

## 3. Hand-built / custom colony layouts

Hybrid-colony detection shipped. Broader tracking of player-designed layouts (colonies that don't
match any template we generate) is still unscoped.

- **First step:** decide what the feature would actually *do* for the user before building anything —
  detection alone has no action attached to it today.

## 4. Layout engine — known gaps

Both documented in CLAUDE.md, neither with a demand signal yet:

- Intermediate **storage facilities** aren't modelled. The hand-built SHPC reference adds 3 on top of
  the 3 launchpads to buffer P2/P3 via storage round-trips; our generator routes intermediates
  tier-to-tier directly.
- **CPU/PG is budgeted, not simulated** — `compute_resources` estimates from idealised pin
  coordinates; there's no simulation of the real in-client fit.

---

## 5. Epoch timestamps stored at float4 precision (Postgres) — FIXED 2026-07-31

`pp_job_runs.started_at` / `ended_at` and `pp_job_leases.lease_until` are declared `REAL`. On SQLite
that's an 8-byte double, but on Postgres `real` is **float4** — about 7 significant digits, and a Unix
epoch timestamp needs 10. Every stored job time is therefore quantised to ~64 seconds (measured on
prod 2026-07-30: wrote `1785409464.304`, read back `1785409400.0`).

Consequences, all on the admin Jobs page: displayed last-run times can be a minute off, and the
run **duration** (`ended_at - started_at`) is meaningless for anything shorter than that — a 12s job
shows as 0s or as ±64s. The lease is unaffected in practice (64s of slop against a 900s TTL).

Found while fixing the "healthy daily jobs reported as never run" bug (`job_summary`, 2026-07-30).

**Fixed by `app.db.widen_epoch_columns()`**, called from `_ensure_all_tables()` at startup. Two
things the original write-up got wrong, both found by inventorying the live schema rather than
trusting the note:

- It was **22 columns across 15 tables**, not 3. The same `REAL`-for-an-epoch pattern was in
  `pp_char_planets` (scan/ESI-cache times), `pp_colony_yield`, every completions/orders/shares
  table, and the BPC scan lease — the Jobs page was just the only place it was visible.
- **Only epochs needed it.** The schema's other float4 columns (percentages, volumes, ISK amounts,
  security status, day counts) are fine at 7 significant digits; it's specifically an epoch's 1.79e9
  magnitude that overruns them. Widening everything would have been a bigger migration for no gain,
  so `_EPOCH_COLUMNS` is an explicit list and `types.volume` is asserted to be left alone.

Widening is non-lossy but does NOT recover precision already discarded — rows written before the
migration keep their rounded values, so historical job durations stay wrong. Only new writes are
exact. `test_epoch_precision.py` covers the round trip, idempotency, and the targeting.

---

## 8. Default the build system (job fees are quoted light without one)

Job installation fee = EIV × (system cost index + facility tax + 4% SCC). The index is fetched and
correct (ESI `industry/systems/`, 5,485 systems, both activities, 6h cache) — but it only applies
once a system is CONFIGURED, and in prod **1 of 26 accounts has one**. The two engines then degrade
differently, which the warnings shipped 2026-08-02 now state explicitly:

- **Manufacturing** still charges the SCC and facility tax, so the fee is understated by the index
  share only. Not trivial: in Jita the index is **76%** of the whole fee (0.1715 vs a 0.055 rate
  without it), and it spans 0.14%–17.25% across New Eden.
- **Reactions** zero the entire rate, so profits are quoted with no install fee at all.

Surfacing it was the safe half (option 1, done). The open question is whether to pick a system on
the user's behalf when none is set — the account's own structure system, or Jita as a
worst-case-ish reference. Deliberately NOT done yet: it would silently change every existing
account's costs, and a wrong default is harder to notice than an absent one. If it is done, it
should be visibly labelled as a default, the same way `skill_time_basis` and `cost_basis` are.

## 6. Adopt `eslint --rule no-undef` in CI

A dead `if (!r.ok)` left behind by the fetch()→api() migration referenced a variable that doesn't
exist, so **every successful** reaction assign threw a ReferenceError, was caught, and was reported
to the user as failed. Because `POST /api/reactions/assign` blindly appends, each retry added
another full set of rows — two suggestions became 27 assignment rows on a 10-slot character
(reported 2026-08-01, `static/reactions.js:1256`).

`node --check` cannot catch this: it is valid syntax, and only fails when the line runs. `eslint`
with a single rule does catch it, proven both ways on 2026-08-01 — re-introducing the bug reports
`'r' is not defined` at that exact line, and the fixed tree reports **zero** `no-undef` findings
across every file in `static/`. So there is no backlog of similar bugs to clear first; adopting the
rule is purely preventive and starts from green.

The wrinkle is that this codebase's JS is plain `<script>` files sharing ~813 implicit globals, so
a naive run reports every cross-file helper as undefined. The working recipe (used for the audit)
is to scrape top-level `function`/`let`/`const`/`var` names from all of `static/*.js` into the
config's `globals` map, then lint with only `no-undef` enabled. That is ~20 lines of setup and no
new runtime dependency for the app itself.

**Related, still open:** `assign_reaction` has no idempotency and no capacity check, which is what
turned a UI bug into 27 rows. See the note under item 7.

## 7. Reaction assignment has no idempotency or capacity guard

`POST /api/reactions/assign` (`app/reactions/jobs.py`) is a bare INSERT: re-posting the same
suggestion appends a second full set of rows, and nothing stops the total exceeding the character's
actual reaction slots. The frontend bug above is fixed, but any transient failure plus a retry can
still duplicate.

Both plausible fixes change real semantics, so this needs a decision rather than a patch:

- **Idempotent replace** — re-assigning the same (character, type_id, tier_order) replaces that
  group's rows instead of appending. Kills the duplication class outright, but breaks a user who
  deliberately assigns the same product twice to one character to get more parallel jobs (today
  that is what `job_count` is for, so the loss may be theoretical).
- **Capacity cap** — refuse rows beyond the character's reaction slots. Careful: chain tiers are
  SEQUENTIAL, not concurrent (tier 0 must finish before tier 1 starts), so a naive "count all rows
  against slots" cap would wrongly reject legitimate deep chains.

The fuel-block slowness reported 2026-07-06 is **fixed** — verified in code 2026-07-30: the
Redis-shared `packed_rate` cache (`app/fuelblocks.py`, via `_layout_cache_get_or_compute`) and the
cheap preview path (`is_preview`, `app/fuelblock_planner.py`) are both in place. This is the
procedure to confirm it stayed fixed; run it after touching `fuelblock_planner.py`,
`fuelblocks.py`, `layout.py`, or the layout caches.

**A. Preview must do no factory geometry** (the fix that matters most, and the one a refactor is
most likely to silently undo). In the container:

```python
import app.planner_advisor as pa, app.fuelblock_planner as fp
calls = []
orig = pa._factory_pack_max_diameter
pa._factory_pack_max_diameter = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
# preview  = no chosen_systems  -> expect 0 calls
# full plan = chosen_systems set -> expect >0 calls (was 21 when first measured)
```

Expected: **0 calls in preview, non-zero with `chosen_systems`.** A non-zero preview count means
`is_preview` stopped being threaded through and every recommendation request is paying full
factory-placement geometry again.

**B. Cold-process cost must not return.** Restart the pod, then time the first fuel-block plan and a
second identical one:

```
ssh node01.failed.name "sudo k3s kubectl -n production rollout restart deploy/eve-pi-planner"
# then, from the app's own instrumentation:
ssh node01.failed.name "sudo k3s kubectl -n production logs -l app=eve-pi-planner --tail=200 \
  | grep -E 'fuelblock\.(fetch_planets_and_recs|extractor_pipeline)'"
```

The first call after a restart should be close to the warm one — that's the whole point of the L2
Redis cache. A large cold/warm gap means the cache degraded to in-process only (the exact bug fixed
on 2026-07-06, and again in `fuelblocks.py` afterwards).

**C. Cross-replica sharing.** With 2 replicas, a plan computed on one pod should leave the other
warm. Issue the same request repeatedly and confirm timings don't alternate fast/slow — alternation
means the Redis layer isn't being hit and each pod is caching alone.

**Regression threshold:** the original user-visible symptom was "pressed find, stuck >30s". Treat
anything over a few seconds on a warm path as a regression worth tracing rather than tuning.

---

## Closed — do not reopen without new evidence

| Item | Verdict |
|---|---|
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
