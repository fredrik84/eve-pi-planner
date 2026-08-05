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

- **The to-install checklist is no longer skill-blind** — SHIPPED 2026-08-05, behind
  `industry_install_skill_aware`. The answer to the question this entry left open ("check first
  whether its most-loaded-first ordering is doing something the scheduler's rule isn't") is yes, and
  deliberately: the checklist counts FREE slots now, the scheduler counts TOTAL slots over days. So
  the assignment is not reused — the RANKING is (`schedule.skill_tier`), with capacity still
  deciding within a tier. A job no capable character has a slot for is still assigned and carries
  `skill_ok: False`; `skill_ok` is recomputed for whoever is actually named. See CLAUDE.md.
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

## 2f. Stop sharing job runs between orders — WIRED 2026-08-05, behind `industry_per_order_plans`

**The physical fact this rests on: a job outputs to exactly ONE container.** Capital builders run a
container per build — it is both where the materials are sourced and where the output lands, and it
is how they know what is finished for which customer when three orders are in flight. A batch shared
between two orders has nowhere to deliver.

Shipped: `plan_queue_per_order` returns the full `plan_queue` contract and is reachable —
`pp_industry_settings.per_order_plans` (own write path, `GET/POST /api/industry/per-order-plans`),
a `per_order_plans` field on `QueuePlanRequest`, and the branch in `_run_queue_plan`. Design notes
in CLAUDE.md under "Planning each order on its own". Default OFF and gated on the ladder, because
switching re-costs every quote.

**The comparison that was promised and never delivered** is `POST /api/industry/queue-plan/compare`
(both plans, same inputs). Measured as a what-if on the two real queued builds — two customers
instead of one — on 2026-08-05:

| | 2× Archon (ctx 1) | 2 orders × 2 Phoenix (ctx 9022) |
|---|---|---|
| net cost | +2.45% (+138.8M) | +0.96% (+88.2M) |
| blueprints | +39.8% | +4.6% |
| makespan | −1.27% | −6.08% |
| jobs / build steps | 60→92 / 6→12 | 49→74 / 4→8 |
| wave starts (logins) | 3 → 3 | 4 → 9 |

So it is not always slower — smaller batches fill idle slots — but it always buys more prints and
materials, and it scatters the landings. On a single-order queue (which is what both prod accounts
actually have) the two plans are **byte-identical**, verified in prod.

**Four things had to become first-come-first-served, and three were live errors in the unwired
code** — two orders cannot both spend the same thing: stock (curated boxes cap the order, the
queue-wide remainder caps that), contracts (`cost_for_runs`/`cost_for_copies` now report the
listings they spent; without it the split read 76.7% *cheaper* on blueprints), owned copies (a
BPC's runs are spent when run; originals exempt), and job fees (`_order_cost` ignored per-job
routing — 220.5M against 511.4M on a real build). Covered by six new test groups in
`test_industry.py`, including "a single order plans the same either way".

**Cross-order alignment is now explicit** (the old item 4): `build_tasks` gained `plan_out` /
`start_out` / `align_hint`, each order is packed once unaligned, the union is aligned keyed per
order, and each order is replayed with the answer. The `_DELIVERY_OVERSHOOT` give-back is measured
on the scheduled makespan exactly as in `plan_queue`.

**What is left**

1. **Container as job OUTPUT** (item 5's other half) — still not modelled, and it is the point of
   the whole exercise. Every scheduled job now carries `order_id`, which is the hook: the order
   already names the box its materials come from, and the output belongs in the same one. Needs a
   UI answer for "no container bound" (corp hangars need Director; not everyone has one).
2. **Print locking ACROSS orders.** Per-order copy RUNS are consumed correctly now, but two orders
   sharing one BPO each see it and may each schedule a concurrent job off it. Fixing it properly
   means making the print a scheduling resource rather than a per-plan cap — bigger than it looks,
   and it only bites an account planning apart with a single original per type.
3. **No UI.** The setting and the comparison are endpoints only; nothing on the build page offers
   either yet. Deliberate — the numbers above were the gate on going further.

## 2g. Slot alignment — RESOLVED 2026-08-03 in two rounds

Long thread with a capital builder. Jobs in one wave finished at 2h32m / 5h05m / 10h11m, so the
builder had three separate moments to log in at, and a third of their slots sat idle in between.

**Round 1 — `_PACE_OVERSHOOT = 1.0`.** A job holding ONE run can only grow by taking a
second, which is a 100% increase *by definition* — so every smaller allowance tried first (5%, a flat
20 minutes, 2% of the makespan) was arithmetically incapable of merging a 1-run job however much
slack it had. Four rounds of tuning moved nothing and each null result got explained away as a
dependency constraint instead of checked against the arithmetic. Measured on the real 206-hour
Archon: **232 jobs → 159, +32 minutes (+0.26%)**.

**It was called resolved here and was not.** The builder came back with the same complaint against
the new plan — Hypnagogic Neurolink Enhancer 10h11m, Sulfuric Acid 7h39m, Oxy-Organic Solvents
5h05m, still three logins. Sweeping `_DELIVERY_OVERSHOOT` from 2% to 100% on the real Archon moved
**not one job**, which is what showed round 1 had found a real bug but not the mechanism: the +1-run
step can only ever add ONE run past a type's window, and Oxy-Organic needed three. Widening the
allowance until it could reach grew a *different* job to 15h18m, past the 10h12m the wave was
landing at — more slack, worse alignment.

**Round 2 — `_align_cohorts`, the missing idea being a TARGET.** A window says how long a job may
take before it holds something up; it cannot say when to LAND, and a builder logs in at landings. So
every type that starts at the same moment forms a cohort, and each one is packed up to the longest
job the cohort already has — no new pace, same principle as `pace_cap`, scoped to what the builder
is looking at when they log in. Measured on the real Archon: **159 jobs → 143, and Sulfuric Acid,
Oxy-Organic Solvents and Hypnagogic all land together at 10h12m.** The 5h05m and 7h39m trips are
gone.

**The bound moved to where the quoted number is.** `_DELIVERY_OVERSHOOT` (2%) still governs, but
`plan_queue` now enforces it on the SCHEDULED makespan and drops the alignment wholesale if it did
not pay. Enforcing it inside the packer failed twice, both instructive:
- *Per type* it cannot work — Oxy-Organic's own window is 2h33m against a 4h08m allowance, so any
  per-type bound rejects the merge the feature exists for.
- *Per plan on the packer's own model* it is too pessimistic: that model ignores slot contention (on
  purpose), read 211h where the schedule delivered 210.46h, and spent the phantom difference giving
  back exactly the Oxy-Organic merge — four fewer slots for zero minutes of delivery.

Guards that must stay: a **deliverable is exempt** (`no_consumer`) — alignment buys slots by
finishing components later and a finished product has no later to give; flipping `_PACE_OVERSHOOT`
without that exemption slowed a finished product and the suite caught it. `pace_cap` is what stops
alignment setting a new pace: packing to the plan's pace with no deadline at all gives 34 jobs and a
**544h** makespan.

**The makespan cost is 206.89h → 210.46h (+1.7%) and that number is measured against a fiction** —
that the builder is at the keyboard the instant every job lands. Re-priced against a login cadence,
the aligned plan wins outright, and by more the less often they log in:

| | jobs | present instantly | every 6h | every 12h | every 24h |
|---|---|---|---|---|---|
| before | 159 | 206.1h / 5 logins | 220.1h | 232.1h | 280.1h |
| aligned | 143 | 207.7h / 4 logins | **214.1h** | **220.1h** | **256.1h** |

Worth keeping in view if the 2% bound is ever revisited: it is enforced against the instant-presence
number, which is the one yardstick we know the builder does not live by.

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

**Reopened and re-fixed 2026-08-05:** the inventory above claimed the BPC scan lease was covered and
it was not — prod still had `pp_bpc_scan.lease_until` / `started_at` / `ended_at` at float4. The
symptom was a test that failed about half the time (`an expired lease is reclaimable`), because a
lease set to `now - 1` rounds back into the future inside a ~128-second bucket. Now in
`_EPOCH_COLUMNS`. Lesson worth keeping: the list is hand-maintained, so a new epoch column added
anywhere needs an entry — and "the write-up says it's covered" is not the same as asking the live
schema, which is what found this.

Widening is non-lossy but does NOT recover precision already discarded — rows written before the
migration keep their rounded values, so historical job durations stay wrong. Only new writes are
exact. `test_epoch_precision.py` covers the round trip, idempotency, and the targeting.

---

## 8. Default the build system — SHIPPED 2026-08-05, behind `industry_default_build_system`

Job installation fee = EIV × (system cost index + facility tax + 4% SCC), and the index only applied
once a system was CONFIGURED — 1 of 26 prod accounts had one, so manufacturing was understated by
the index share (76% of the fee in Jita, spanning 0.14%–17.25% across New Eden) and reactions quoted
no install fee at all.

Answered in three tiers, most specific first, each labelled in `cost_basis.basis`: `configured` (the
user's own Reactions system — unchanged and still first) → `structure` (the system of a structure
they told us they build in, with that structure's own facility tax — not a guess at all) →
`reference` (Jita). The open question in the old write-up was whether to pick a system on the user's
behalf; the answer is that a building they described is not "on their behalf", and only the last
tier is. Jita is the right REFERENCE because its index tops the range, so the quote errs
conservative — and it is stated as an assumption, with the fix one click away, because a wrong
default is harder to notice than an absent one. Both defaulted tiers are behind the flag: they move
every existing account's costs. Design notes in CLAUDE.md; `test_cost_basis.py` covers the
precedence and the label.

## 6. `eslint --rule no-undef` in CI — SHIPPED 2026-08-05

`scripts/lint_js.mjs` + a `lint-js` job in `.github/workflows/build.yml`. The recipe from the
investigation held: scrape every top-level `function`/`let`/`const`/`var` name out of `static/*.js`
into the config's globals (908 of them), add the browser globals, enable ONLY `no-undef`. No runtime
dependency for the app — `npx eslint@9` in CI, runnable locally with `node scripts/lint_js.mjs`.

It found exactly one thing on the way in, and it was real: `_indCopyText` read the deprecated
implicit `window.event` as a free variable. Now explicit, and the tree is green.

**Deliberately NOT gating the deploy.** The job reports independently rather than being a `needs:`
of `build`, because this repo pushes straight to main and a lint step that can fail on an npx
download would block a hotfix. It exists to make the failure visible, which is all the original bug
needed — nobody was reading a stack trace, the symptom was 27 rows in a table.

## 9. Name the SYSTEM a container is in + let a build source from several — SHIPPED 2026-08-04

Both halves built together (one code path), behind `industry_plan_sources`; design notes in
CLAUDE.md under "Where a container IS, and which build owns it".

The researched plan held up: `root_of` (the walk that already found the hangar flag) returns the
root asset's `location_id`, resolved station → `/universe/stations/{id}` / structure →
`markets.structure_info` — now the single call site for that lookup — and cached in `pp_locations`,
including the **unresolvable** answer so an ACL 403 isn't re-asked every scan. Grouping is real
`<optgroup>`/section headers off one server-built `place` label, in all three lists that show
containers. There was no fourth: **Reactions and PI surface no container lists at all** —
`pp_asset_sources` is read only by Industry — so that premise in the write-up was wrong.

The second half turned out to be more than a set: binding used to switch a box on ACCOUNT-WIDE, so
one build's can was every other build's stock. `source_keys` + `sources_owned` make a plan own what
it may spend, with three rules keeping it non-retroactive (an uncurated order still uses the account
pool; a mixed queue is the union; an empty set is not ownership). `pp_source_sets` keeps a repeat
multi-box answer at one pick.

Left undone: the **output** half of the original note ("and where its output lands") is still not
modelled — a job's output container is item 2f's territory, not this one's.

## 7. Reaction assignment idempotency + capacity — SHIPPED 2026-08-05, behind `reactions_assign_guard`

`POST /api/reactions/assign` was a bare INSERT: re-posting the same suggestion appended a second
full set of rows, and nothing stopped the total exceeding the character's real reaction slots. Both
halves are now built, and the decision the old entry was waiting on went this way:

- **Idempotent replace.** Re-assigning the same (character, product, tier) replaces that group's
  rows. The objection was a user who deliberately assigns one product twice to get more parallel
  jobs — but that is what `job_count` is for, and it sets the row count WITHIN the group, so the
  capability is expressed and not lost. Customer-order rows (`order_id IS NOT NULL`) are never
  touched: they were committed against real capacity by a different flow.
- **Capacity counted per TIER, not per row.** Chain tiers are sequential, so a four-tier chain at
  two jobs a tier is eight rows and never more than two slots at once; summing rows would refuse it.
  `_concurrent_load` takes the worst tier. Over capacity is a 409 with the numbers in it, and the
  frontend now shows the server's message instead of a generic "Assign failed" — a refusal that
  says "needs 12 at once, this character has 10" is actionable.
- **Unknown capacity never refuses** — a character we cannot read slots for returns 0 and is not
  capped, the same rule the blueprint print caps follow.

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

## 10. Choose whether — and which — reactions the plan builds — SHIPPED 2026-08-05, behind `industry_reaction_policy`

Built as specified, per ACCOUNT (the open question, answered by the owner: a standing way of
operating, like the blacklist — not per order). Two halves.

**The rule.** `pp_industry_settings.reaction_policy`, a JSON `{build_reactions, buy_categories}`
written by its own endpoint (`GET/POST /api/industry/reaction-policy`, `set_reaction_policy`) —
deliberately not a field on the debounced settings PUT. Applied in `resolve_unit_costs` beside
`never_build_ids`, so setting the DECISION is what makes the subtree below it vanish and what keeps
the parent costed against what it will actually pay; nothing is pruned by hand. Categories live in
the new shared `REACTION_CATEGORIES` (`app/industry/categories.py`) which the rig families in
`structures.py` now read for their GROUP SETS while keeping their own rig labels — one curated map,
two readers, not one idea welded to the other. The registry (labels + descriptions) is echoed to the
frontend on every read and write; the UI hardcodes none of it. An uncategorisable reaction is BUILT,
which is why "we don't run reactions at all" is its own switch rather than three ticks.

Precedence, most specific first: `build_reactions_anyway` (per order, `pp_industry_orders.
build_reactions`, unioned across the queue exactly like `force_build_ids` since the queue builds one
shared batch) → `force_build_ids` → this policy → `never_build_ids`. A reaction with no buy price is
still built; a reaction you ORDERED is exempt from the policy in its own build. The rule reaches
every plan path — checklist, sourcing list, customer share link — through
`apply_account_build_options`.

**The price of the convenience.** `reaction_policy_report` on both plan builders, signed as *what
BUILDING these would save*, which is the single figure that reads correctly in both directions:
policy on → what buying them in added to this build; order overriding it → what reacting them saved.
Rendered as one quiet row beside `_indMarginalBar` — a decision surface, not a notice, so nothing
was added to the block trimmed on 2026-08-04 — with the per-family checkboxes folded behind the
switch, a `not reacted` badge on the shopping row, and a `reacts` tag + checkbox on the order chip.
Nothing in the wording touches the Reactions tab: an account can buy reaction outputs for its builds
and still run a reaction business there.

Both cheap checks came out clean: a reaction pool of zero plans fine (no reaction jobs, sane
makespan — `test_a_reaction_pool_of_zero_still_plans`), and the default is byte-identical to the old
plan (asserted by dict equality, not by eye). `test_industry.py` 711 → 763 checks, all green,
plus `test_features.py` 17/17.

Deliberately left out: no per-category ISK breakdown beyond the per-item list (the items carry it);
no way to express "buy this family but only above N units", which would be a threshold and this is a
standing rule.

---

## 11. A reaction formula is an item too — SHIPPED 2026-08-04 (`bcdaa39`, `e9ac4d4`)

A formula locks into the reactor for the job, so one formula is one concurrent reaction — but
`owned_blueprints()` built its blueprint→product map from the SDE `blueprints` table alone and **not
one of the 112 `reaction_id`s is in it**, so every formula ESI returned was dropped at that join (50
sat unused in prod). A 2× Phoenix queue therefore planned Axosomatic Neurolink Enhancer as 17
simultaneous jobs off the ONE formula that account holds; 16 could not be installed.

A `reaction_id` IS the formula item's own type_id, so the fix needed no new fetch, table or scope:
`SELECT reaction_id, output_type_id FROM reactions` unioned into the map, a positive `quantity`
expanded into that many entries (`_STACK_CAP` 200 — formulas STACK, and a blanket one-per-type cap
would have been as wrong as no cap), and the count capping `n_wide` rather than `cap` — it binds on
CONCURRENCY, since formulas can't be copied and runs-per-job never binds.

Both open decisions were settled as the write-up recommended:

- **Unknown ownership never caps, at BOTH levels** — per type (`_print_limits` → `(None, False)`)
  and per ACCOUNT (`prints_known()` gates the whole cap on `cached >= characters`, because a union
  over the characters that have a cached list is a floor: prod account 1 has 2 of 14 cached and
  still shows prints for 159 types). Missing evidence is not evidence of scarcity.
- **The plan never buys a formula** — durable, reused by every later build, so what can't be bought
  is REPORTED: `print_limits` / `metrics.print_limited_steps` say what holding another would save.
  Measured with one formula of each held: 2× Phoenix 761.6h → 1611.3h, Archon 525.1h → 1008.0h.

Design notes in CLAUDE.md under "one print runs one job at a time". Pinned by
`test_a_reaction_formula_is_an_item_too_and_unknown_ownership_never_serialises` and
`test_a_half_connected_account_is_never_capped_on_what_it_half_shows` (the second exists
specifically so the coverage gate can't be "simplified" away later). `test_industry.py` 763 checks,
green in-container; live in prod on `55e1de4`.

---

## 12. Describe the Industry workflow end to end (2026-08-05)

Industry has been extended a lot in a short time — make-or-buy overrides, an always-buy blacklist,
a reaction policy, per-order sourcing and source sets, corp hangars, manual done-marks, print and
formula caps, customer share links, onboarding — and each landed with its own design note in
CLAUDE.md. What does NOT exist anywhere is the **whole flow in one place**: what the feature set
supports, and how a builder is actually meant to work with it from "a customer asks for a Phoenix"
through to "it is built and delivered".

Wanted: a short summary plus a step-by-step description of the intended workflow — the path a
builder walks every time, which controls belong to which step, and which are the occasional ones
behind it. Two audiences and they are different documents: the **user-facing** one (what the tab
does and the order to use it in — candidate for the How-it-works page or the onboarding screen)
and the **developer-facing** one (a map of the modules and endpoints each step goes through, which
CLAUDE.md's per-feature notes hang off).

First step is a read of what is actually there rather than a write-up from memory: `app/industry/`
is 22 modules now, and parts of the intended path exist only as UI ordering in `static/industry.js`.
Worth doing before the next feature, because "does this add a step to the path a builder walks every
time" is the design test everything here is supposed to meet, and right now that path isn't written
down.

---

## 13. A manifesto per service — what PI, Reactions and Industry are FOR (2026-08-05)

Companion to item 12, and it comes first when the audit runs: item 12 describes what the Industry
flow *is*, this one states what each service is *for*, so the audit has something to measure
against. Without it "is this up to code" has no code to be up to.

The repo already carries the house style — minimize planet interactions, automate the math or drop
the feature, the best UI is read-only, effort is the constraint the other goals fit inside, does
this add a step to the path a builder walks every time — but those are cross-cutting *rules*. What
is missing is, per service: **its purpose, the end state it is aiming at, and the path from where
it is now to there.**

- **PI planner** — one target, one plan, least interaction per ISK.
- **Reactions** — a slot business; what the tool is supposed to decide for the user and what it
  deliberately leaves to them.
- **Industry** — lowest net cost and fastest delivery, inside the effort constraint (this one is at
  least half written already, at the top of CLAUDE.md).

Wanted: a short manifesto for each — purpose, target state, and the honest gap — written so the
audit in item 12 can score against it feature by feature: does this serve the stated goal, does it
cost more effort than it removes, and if not, does it come out. It is also the thing to hold a new
feature against BEFORE building it, which is the cheaper end of the same test.

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
