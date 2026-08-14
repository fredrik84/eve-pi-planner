# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-05.

---

## 29. Reactions: the profit numbers are not instant-sell (2026-08-14, HIGH, ungated)

The pricing invariant (`CLAUDE.md:184`, `docs/reactions.md:958-962`) holds in the advisor and
nowhere else. `_plan_totals` values output at `sell_price` (`jobs.py:2395`) and nets it against
materials only — no job fee, freight or collateral, all three of which `_value_reaction_batch`
charges. The running-job modal does the same and derives an ROI from it (`reactions_job_detail`,
`graph.py:620-622`), and the completions ledger records **"Lifetime net profit"** at sell price
(`jobs.py:355`), compounding over the life of the account. **None of it is flag-gated.** Profit
reads ~3.3× high on an illustrative plan — derive the real figure before quoting it as measured.

Ships with the clock fix, not separately: the typed `time_efficiency_pct` (default 0%,
`graph.py:58`) drives every user-facing duration and ETA while the *measured* `_reaction_time_mult`
(`jobs.py:981`) is read only by the cadence machinery, understating speed **~2.14×** (measured
0.4680, `docs/reactions.md:669`). Profit-per-day is therefore ~1.5× out, the two errors opposing —
so repricing alone makes it look worse before it looks right.

Full spec, evidence and work split: [docs/reactions-repair-2026-08.md](docs/reactions-repair-2026-08.md)
§WS1. Fix in place, no flag (defect in a shipped feature). Needs a guard test that fails if any
user-facing profit field derives from `sell_price` — the rule was written first and drifted past.

## 30. Reactions: the cadence ceiling collapses, and ease costs are invisible (2026-08-14, HIGH)

`_level_options` (`jobs.py:937`) drops a run count only when `r > max_runs AND r > min_runs`, so
once `min_runs > max_runs` the option set collapses to exactly `min_runs` whatever duration it
implies — 11.7 days on a 7-day cadence, reproduced at `max_runs=119`, `min_runs=200`. Three
independent routes abandon the ceiling (the give-ground loop `:1609`, the seed `:1544-1550`,
`_level_options`' own fallback `:952`) and all three must be closed. It is wider than the cadence:
with none set, the same branch exceeds `stage_cap_hours` and breaks the "never slower" promise at
`jobs.py:1493-1494`. The docstring at `:1502-1504` and `docs/reactions.md:742` both assert a HARD
ceiling and are factually false.

**Decided 2026-08-14: the cadence is a stated target, not an absolute** — the plan may exceed it and
must say so on the row. Today a pending row shows runs and never a duration.

Also here: an order is quoted uncapped and re-shaped a page load later
(`split_order_tops_to_cadence`, no free-slot check, rows invisible to the leveller — adjacent to
§28b item 2, not item 1); `_collection_slot` buckets by 24h on a weekly rhythm and outranks surplus;
the 50% stage budget is denominated in runs, not ISK; and the surplus the solver computes
(`jobs.py:945`) reaches no payload and no UI, so Reactions fails the manifesto's scoring question 5
outright. Spec: §WS2.

## 31. Reactions is not answerable without Industry, and none of it is public (2026-08-14)

The manifesto's untested claim, tested, failing on three counts: the cadence control is gated on
`industry_job_length_policy` (`reactions.js:451`, `jobs.py:1147`), `reactions_missing_formulas` is
unreachable because the only paste UI sits behind `industry_manual_blueprints`, and formula caps and
stock fail soft to "you own nothing". **Decided 2026-08-14: make it genuinely standalone** — a
Reactions-owned cadence flag reading the same stored `max_reaction_job_days`, and its own paste route.

Also: two cadence controls on one tab in two units with only one persisted (merge them), and **not
one Reactions flag defaults to public** — the default user gets the tool the last month's work
replaced. Needs the §14 forcing question written for Reactions, but **after 29 and 30 land**:
promoting `level_runs` today ships the collapsed ceiling to everyone. Spec: §WS3.

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

**Stage landing aligned 2026-08-08**, from "keep the login cadence low... finish everything as
close to each other as possible": `_align_stage_jobs` moves slots off the steps of a stage that
would finish early and onto the one holding it up, until the spread cannot narrow further —
slot-neutral, so it needs nobody's capacity, and it is the reactions translation of the
manufacturing planner's `_align_cohorts` (there run counts grow to land together; here they are
fixed by the chain, so the SPLIT moves instead). 27h -> 12h on a synthetic 80/10/10 stage with the
same nine reactors. `_widen_to_idle_slots` now scores by the stage's finish time rather than a
single step's hours, and the customer-order path re-balances the same way after `_fit_chain_slots`.

**Still open — aligning END times across DIFFERENT products of an assembled plan.** `level_stage_runs`
(2026-08-08) gives one product one run count per stage, and `_align_stage_jobs` levels finish times
inside a single suggestion, but a plan built from several assigns can still have its products
finishing at different times. Fixing that means moving JOBS between products, and a job carries its
chain: a chain that lost its rows for a product would stop waiting on it and could announce
"stage 2 is ready" while those jobs were still running. **Chain identity has to be reworked first**
— most likely a real `chain_id` stamped at insert, which item 22 also wanted — so this is not a
small follow-up.

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

## 28. Reaction job layout — the priority order, stated by the user (2026-08-08)

**This ordering governs. Optimise strictly in this sequence, and do not trade a higher one away for
a lower one:**

1. **Even distribution of job runs per slot per product.** Every job of a product carries the SAME
   run count — one number to read and type, wherever it is installed.
2. **Slot efficiency.** Use the reactors that are free. Idle capacity while a step drags on is the
   failure mode, not a saving.
3. **Aligned end time.** The jobs of a stage should land together, so one login collects them.
4. **Shortest total run time.** Last. Makespan is not worth breaking any of the three above.

Three attempts on 2026-08-08 all failed by optimising these out of order, and the shape of each
failure is worth keeping:

- `level_stage_runs` (kept) gives one run count per product per stage **within a character** — (1),
  but only locally, so numbers still differ across characters.
- The even per-host split (**reverted**) chased (1) across characters by giving every host the same
  runs. It ignored (2): a 2-slot character got the same 250 runs as a 10-slot one and a single step
  hit **14 days**.
- The uniform per-host job layout (**reverted with it**) chased (1) again by capping every host to
  the smallest host's slots — (2) sacrificed even harder.

**The shape that satisfies all four, and what to build next.** Solve for a target duration `D` per
STAGE across the slots actually available, rather than per host:

    jobs_i   = ceil(total_runs_i * cycle_i / D)      # per product in the stage
    runs_i   = ceil(total_runs_i / jobs_i)           # every job of product i carries this — (1)
    choose the smallest D with sum(jobs_i) <= slots_available   # fills the slots — (2)

Every product's jobs are then equal (1), the whole slot pool is used (2), every job in the stage
ends at ~D (3), and D is minimised last (4). `tidy_runs` rounds `runs_i` at the end.

**The one real constraint to respect:** a chain's intermediate feeds the stage above it ON THE SAME
CHARACTER — this package does not model shipping half-finished goods between hangars — so the
per-stage solve distributes JOBS across characters but a chain's stages must stay together. That is
also what makes the "one number everywhere" goal reachable: the run count is a property of the
product, the character only decides where the jobs sit.

**BUILT 2026-08-08 as `level_product_runs` (`reactions_level_runs`).** The D-per-stage solve above,
with the per-chain floor made explicit: a candidate run count `r` costs chain *g* `ceil(T_g / r)`
jobs, and only counts if every chain's character has the reactors for it, the product overshoots by
no more than 50%, and no single chain is handed more than 3x what it asked for. Scored spread → slots → surplus → typeable, searching over target
durations. On the reported plan (375 / 360 / 360 / 300 of Carbon Fiber split 3/4/4/4) it lands on
**125 runs everywhere in 12 jobs** instead of 125/90/90/75 in 15. Two things were added to the
shape above and are load-bearing:

* **a ceiling: no job may run longer than the longest job already in that stage.** Minimising slots
  with no ceiling always ends at "one enormous job per character" — here four jobs of 375 runs, 3×
  the runtime, eleven reactors idle. (4) is last in the list, not absent.
* **surplus is spent to LAND a stage or take a reactor back, and for nothing else** — the user's
  own rule, *"it's fine to build a bit too much if it doesn't line up."* A 15% budget could not buy
  one number for a small product at all (Oxy-Organic Solvents stayed at 35 and 18 runs), and
  scoring raw spread instead of a landed/not-landed flag bought a little alignment for a lot of goo.
* **the top row of a chain is never touched**, and a chain never loses its last row of a product —
  `chain_stage_state` reads readiness per chain, so a chain that stopped mentioning a product it is
  waiting on would call the stage above ready while those jobs ran.

**Job length is the player's call — but check what actually exists before building on it
(corrected 2026-08-13).** This entry described `pp_reaction_job_target` / `get_job_target` /
`/api/reactions/job-target` with `days`/`runs`/`auto`. **None of that is in the code.** The setting
that does exist is `max_reaction_job_days` (`app/industry/settings.py`, Industry settings, behind
`industry_job_length_policy`): a ceiling in DAYS on one reaction job, default `None` — deliberately,
since guessing a cadence for somebody splits their batches into jobs they never asked for. It is
read by the Industry planner (`graph.py` → `params.max_reaction_job_hours`) and **not** by the
Reactions customer-order allocator, so an order still has no cadence at all — which is exactly the
gap 28b item 2 names, and it is still open.

**Still open — two chains on ONE character sharing a job.** The output is fungible and lands in one
hangar, so two chains each holding a 60-run job of the same product should be one 120-run job. That
means a row belonging to more than one chain, i.e. the `chain_id` rework item 22 also wanted.
Everything else in the priority list is done.

## 28b. Slot reservation and order cadence (2026-08-08, from live use)

Two things reported while using the order flow, neither fixed yet:

1. **A later stage reserves slots it cannot use.** "Reinforced Carbon Fibers are slot allocated
   which blocks 10 slots from doing anything else." Stage-2 rows are created at assign time and
   counted against capacity from that moment, but they cannot be installed until stage 1 lands.
   21a made the accounting the PEAK across stages rather than the sum, which was the conservative
   choice — the honest one for "what can I start now" is to count only the startable stage, and
   let a later stage claim its slots when it becomes ready (`chain_stage_state` already knows
   when that is). Needs care: the assign guard must still refuse a chain that can never fit.
2. **An order can be quoted at an absurd cadence.** 250 runs of Thermosetting Polymer came out at
   14 days. The even-split experiment (reverted, see below) caused that instance, but the general
   gap is real: nothing bounds how long a single step may take. The wizard has a cadence; an order
   has none, and `_fit_chain_slots` will happily put a fortnight's work on one reactor. Wanted: a
   ceiling on any one step's duration, spilling to more hosts (or refusing) rather than quoting it.

**Reverted 2026-08-08:** the even per-host split. It made every character show the same run counts,
and put a 2-slot character on the same 250 runs as a 10-slot one — a single step at 14 days. Tidy
numbers are not worth a fortnight. If revisited: bound how far apart hosts may FINISH, splitting
evenly only among hosts of comparable capacity.

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


---

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
