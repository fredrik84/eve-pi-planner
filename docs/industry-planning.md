# eve-pi-planner — Industry: planning a build

Deciding what to build vs buy, what it costs, when it lands and what to quote. Everything behind the `industry` flag up to the moment a build starts.
Running one: [industry-running.md](industry-running.md). Back to [CLAUDE.md](../CLAUDE.md).
The whole flow in one place: [industry-workflow.md](industry-workflow.md).

Find a section: `grep -n '^## ' docs/industry-planning.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Make-or-buy overrides and the marginal-saving strip** — the two shortcuts that buy instead of build, `force_build_ids`, and where the user overrules them
- **Blueprint ME/TE: where the numbers come from, and the two override paths** — precedence (override > declared > owned > contract > 0/0), the job chip vs the order edit row
- **Blueprint copies: coverage, per-job research, and one print per job** — runs coverage, `_print_limits`, reaction formulas, why unknown ownership never caps, and prints declared by hand (`industry_manual_blueprints`)
- **Scheduling: slots, pace, cohort alignment and compaction** — `_PACE_OVERSHOOT`, `_align_cohorts`, `pace_cap`, the reaction job-length ceiling — why a job may run long but never past its consumer
- **The Step-by-step view must account for its own total** — step offsets overlap on purpose; do not make them sum
- **Planning each order on its own (`industry_per_order_plans`)** — the compare endpoint, first-come-first-served consumption, cross-order alignment
- **Build options, and who installs each job** — `pp_industry_settings`, one option set per queue, `assign_characters`
- **Alliance-shared buildings (`industry_group_structures`)** — shared structures offered as suggestions
- **Defaulting the build system (`industry_default_build_system`)** — default to a structure you build in, else Jita as a labelled reference
- **The start-now checklist and the schedule agree on who CAN install (`industry_install_skill_aware`)** — skill-aware install eligibility
- **Quoting: margin → price** — net cost as the base, per-order margins, why the queue tile must not carry `data-ind-price`
- **The build page's notice stack (trimmed 2026-08-04)** — the bar a notice must clear, what was cut and why — read before adding any banner
- **Industry performance: one plan per page load** — the graph cache, the inline install/progress blocks, the sessionStorage plan cache
- **First use** — onboarding flow and the per-account `onboarded` flag

---

## Make-or-buy overrides and the marginal-saving strip

**"Build it anyway" overrides.** The make-or-buy engine has two shortcuts that BUY a component the
cost engine would build: the marginal-saving threshold (the slider) and the speed cap. Both are
judgements about what's *worth a job*, which is the user's call — so every "low saving" shopping-list
row reports `marginal_saving` (the ISK building that batch would have saved, negative when an
unowned blueprint copy makes building dearer).
**The decision lives in `_indMarginalBar`, above the plan, and nowhere else.** It used to be a
*Build it* button on the shopping-list row, which meant: in the QUEUED view the list is collapsed
*and* was rendered without `allowForce`, so once a build was queued the user could neither see what
building would save nor act on it — the override existed only in the preview modal, before the
build was real. The strip lists only the borderline components (positive saving, biggest first, six
then "+N more"), states the total at stake, and each chip is the action.
It also carries a **"build everything worth more than X" slider**, which is deliberately NOT a
second make-or-buy threshold: the saving-% slider decides what gets *suggested* here, and this one
decides how many of those suggestions you accept in one go, over the list that threshold already
produced. They cannot fight, because they act at different stages. (An absolute-ISK make-or-buy
threshold WOULD fight: the engine's rule is `max(pct × total, MIN_BUILD_SAVING_ISK)`, so on a 2.4b
hull the 3% term is ~72M and an ISK floor set below that would change no decision at all — a control
you can drag while nothing happens.) One or many, `_indForceBuildMany` sends **one** request and
triggers **one** re-plan; seven chips must not mean seven plans of a batch each one changes.
Three things make the slider legible rather than mystifying: **the chips themselves mark up as you
drag** (the list is the feedback — a "builds 3 of 7" counter over an unchanged row of chips reads as
a control wired to nothing), **every borderline component is listed** rather than a top-six the
selection would then exceed, and **the position is remembered** (`localStorage.indMargCut`, clamped
to the current build's range) so a refresh doesn't silently change what the strip appears to offer.
Restoring the position applies nothing by itself — the button is still a deliberate press.
The strip also says out loud that **taking some changes which components are borderline afterwards**.
That is the plan re-costing a batch the user just changed, but without saying so it looks like the
tool inventing new work each time you accept its advice — so the bulk button doesn't stop at one
pass. `POST /api/industry/orders/force-above` **iterates to a fixpoint**: plan, take everything at
or above the cut-off, re-plan (building a component makes its own inputs a bulk demand, which can
make THOSE worth building), repeat until a round adds nothing. One press, and afterwards there is
nothing left above the cut-off. It terminates because the forced set only grows and every round must
add at least one type; `_FORCE_ROUNDS` is a safety net against a pathological graph, not the
expected exit. All rounds share ONE `prepare_plan_inputs` — only `force_build_ids` changes between
them, and the expensive half doesn't depend on it — so the whole thing costs one set of DB reads
however many passes it takes. The single chips stay a single pass: clicking one component is a
choice about that component, not a policy. The shopping list keeps the
`low saving` badge and the "saves X if built" note as an *explanation* of why a row is there — one
place to decide, one place to look things up, no second list.
Where the override is stored depends on context (`indBuildAnyway`): with a queue it PATCHes the
**first** order's `force_build_ids` — the queue unions them across orders, so one order carries it
for the whole batch, and that order chip's `⚒` tag is how you take it back — while the preview keeps
it in the session map until the build is queued.
`BuildParams.force_build_ids` / `BuildOptions.force_build_ids` defeat **only those two shortcuts** —
a component the cost engine says is outright cheaper to buy is still bought, whatever the user
clicks. Overrides persist on the order (`pp_industry_orders.force_build_ids`, a JSON id list) and
are **unioned across the queue** in `_run_queue_plan`: the queue builds one shared batch per
component, so an override can only be all-or-nothing for that component. The preview's own set lives
in `_indForcedTypes` (frontend) and is cleared once the order carries it.

## Blueprint ME/TE: where the numbers come from, and the two override paths

**Blueprint ME/TE for prints you don't own.** A product with no owned blueprint used to be costed at
the global fallback — **ME 0 / TE 0**, the un-researched worst case — which inflated materials AND job
time on every component the plan buys a copy for. The contract index already stores each listing's
research (`pp_bpc_observations.me/te`, captured by the existing scan — no extra ESI traffic), so
`bpc.representative_me_te(info)` picks the copy the plan would actually buy (**cheapest per run**,
ties toward better research) and its ME/TE seeds `params.me_by_product`. Price and efficiency then
describe the same purchase; costing against one copy's price and another's research was the specific
mismatch to avoid. Precedence: **user override > declared by hand > owned blueprint > contract copy >
ME 0/TE 0**, with
`params.me_source` recording which, so the plan can show it. Each build step in `requirements` carries
`me`, `te`, `me_source` (`owned`/`declared`/`contract`/`override`/`default`/`reaction`); the UI renders a
colour-coded `ME n · TE n` chip on every job chip that opens an inline editor. Overrides live in
`BuildOptions.me_te_overrides` (`{"<type_id>": [me, te]}`, string keys — JSON) and persist on the
order (`pp_industry_orders.me_te_overrides`), unioned across the queue exactly like
`force_build_ids`, and are threaded into the customer share so its stages match the builder's.
A build buying several copies of MIXED research is approximated by the one representative value —
that's what the per-product override is for.

**Two places set an override, for two different moments.** The `ME n · TE n` chip on a job chip
edits the SESSION map (`_indMeTe`) and re-plans what's on screen — that's the "while I'm planning"
path. Once an order is queued the planner modal is no longer where you'd look, so the **order edit
row** (`indEditOrder`, the ✎ on an order chip) also edits ME/TE — for the order's OWN product
blueprint, the one a player is most likely to own or to have bought a copy of that doesn't match
anything the plan can see. Components stay on their job chips. Rules that matter:
- The inputs are **seeded with whatever the plan resolved** (`_indOrderMeTe` → order override, else
  `_indReqMeTe`, else 0/0) and show the source in the tooltip. `indSaveOrder` therefore sends
  `me_te_overrides` **only when the value actually moved** — sending it unconditionally would turn
  every rename into a permanent override pinning today's guess, and the plan could then never
  improve on it (e.g. once the player owns the print). Don't "simplify" that comparison away.
- A save **merges** into the order's existing map, so component overrides on the same order survive
  editing the product's; `indClearOrderMeTe` deletes just the product's key.
- An override shows as an amber `ME n · TE n` tag on the order chip (same reasoning as the `⚒`
  forced-build tag: an assumed efficiency drives every material number, so it can't be invisible).
- Reactions have no blueprint ME/TE — the editor is omitted when `me_source == 'reaction'`.

## Blueprint copies: coverage, per-job research, and one print per job

**An owned COPY only covers the runs it has.** Ownership used to be binary — `tid in params.owned`
meant free and ready — which is right for an ORIGINAL and wrong for a copy: holding a 4-run copy
against a 20-run batch is sixteen runs with nowhere to come from, priced at nothing and reported as
"you have the blueprint". `acquisition_costs` therefore keeps owned-BPC types in its list, and
`aggregate_demand` charges `cost_for_runs(bp, runs - covered)` where `covered` is the whole batch for
a BPO and the copy's remaining runs for a BPC. The shortfall is reported per requirement as
`runs_short` and surfaced by `_indCopyShortWarn`. Note copies come whole, so a shortfall of 16 and a
batch of 20 both cost four 5-run copies — owning one is not always cheaper.

**EVERY copy counts, and ME/TE is per JOB off the copy it runs on.** `owned_blueprints()` used to
collapse a product's whole holding down to ONE print (BPO > BPC, then ME, then TE) and discard the
rest, so `covered` above saw one copy's runs: measured on prod, Capital Armor Plates 212 owned runs
across 14 copies counted as **5**, Nitrogen Fuel Block 3,975 across 21 counted as **175** — the plan
then reported a huge `runs_short` and charged for copies the builder already had. It now returns
`{me, te, kind, runs, copies, copy_count}`: `copies` is the whole holding ordered the way it will be
consumed (`_copy_rank` — best RESEARCHED first, an original winning ties because it never runs out),
`runs` is total coverage (-1 = an original owned anywhere in the set), `me`/`te` describe the first
copy a job will run off.
- **Classification uses `runs == -1`, not `quantity`** (`classify_blueprint`). ESI's quantity is -1
  for a singleton and -2 for a copy, but a POSITIVE quantity is a stack of ORIGINALS fresh from the
  market — reading `quantity == -1` as the only original filed those as copies carrying -1 runs,
  i.e. covering nothing. 26 blueprints in prod, each telling its owner to buy a print they hold.
- **Per job:** `build_tasks` assigns copies to jobs best-first (`_jobs_on_copies`), so each job
  carries its own copy's ME/TE — materials and duration legitimately differ between jobs of one type
  in one batch — and a chunk longer than the copy it lands on is SPLIT rather than emitted as a job
  nobody can install. Runs past everything owned are built at `buy_me_te` (the contract copy the
  plan would buy), not at the best copy's research. The scheduled job carries `me`/`te`.
- **The aggregate is runs-weighted, deliberately.** Demand is aggregated BEFORE jobs are split, so a
  per-job figure has no meaning there: `me_te_for(tid, activity, runs)` takes the batch size and
  returns a runs-weighted blend over exactly the copies best-first consumption will spend on it
  (`blend_me_te`), with the remainder at `buy_me_te`. Using the best copy for the whole batch would
  over-credit every run after that copy runs out. `runs=None` (a bare call) weights the whole
  holding, which is the conservative reading. The `requirements` row reports that blend.
- **Precedence: user override > declared by hand > owned blueprint > contract copy > ME 0/TE 0.** An
  override sets `me_source[tid] = "override"`, and `copies_for` returns nothing for such a type, so
  the per-copy path can never outrank what the user said explicitly.

**Prints declared by hand (`industry_manual_blueprints`).** `GET /characters/{id}/blueprints/` is
PERSONAL-only and there is no corp-hangar blueprint endpoint without the Director role, so a builder
whose prints live in a corp hangar could state their ME/TE nowhere at all and every such build was
planned at ME 0 / TE 0 — materials and duration both wrong. Pasting a hangar as STOCK answers the
FORMULA case only (an asset row carries no ME, TE or runs, so it deliberately credits nothing for a
manufacturing blueprint). `pp_industry_blueprints` — `(context_id, id, type_id, me, te, runs,
quantity, prefer)` — is the declaration layer, edited in **Settings → Blueprints & formulas** beside
the ESI panel (`/api/industry/manual-blueprints`, GET/POST/DELETE). Its encoding is the one
`owned_blueprints` already used: **runs blank/-1 = a BPO**, anything else a BPC with that many runs;
`quantity` expands into that many separate physical prints, exactly like an ESI stack.
- **The library arrives by PASTE, one industry window per character.** Typing ~100 formulas one at a
  time is not a usable path, and EVE's Industry → Blueprints window copies as exactly this data:
  `[N x ]<name> TAB ME TAB TE TAB runs TAB category`. `parse_blueprint_paste` reads it (headers and
  blank lines skipped, headers not required, category ignored, `N x ` a stack, **a repeated line a
  separate physical print**, quantities summed per (product, ME, TE, runs), names resolved through
  `types` + the shared `_blueprint_product_index`, anything unresolved reported not dropped);
  `POST /api/industry/manual-blueprints/paste/preview` shows the counts without writing.
  **Each paste is a NAMED BATCH** (`batch`/`batch_name` columns, key = a stable digest of the name —
  not `hash()`, which is per-process randomised), modelled on `add_pasted_source`: re-pasting a name
  replaces that batch and touches no other, so a second character's window cannot wipe the first's and
  a re-paste cannot double a holding. Hand-typed rows carry `batch=''` and no paste may delete them.
  `DELETE …/manual-blueprints/batches/{batch}` drops one batch.
- **The window often states WHERE its prints are — and that is recorded, never an identity.** The
  industry window copies in two layouts: the short one above (a container is selected), and a long
  one (nothing selected) that carries where each print is —
  `… runs TAB ? TAB structure TAB container TAB category`. That layout is **inferred from two real
  client copies, not documented**, so `_split_location` counts BACK from the trailing category
  (`[-3]` structure, `[-2]` container, `<7` fields = no location) and refuses numeric hits rather
  than depending on absolute indices or on the `0` at index 4, whose meaning is unknown. Both
  layouts parse, mixed in one paste. The structure/container land in their own columns per row and
  are **recorded and displayed only** — nothing keys, groups, replaces, counts or plans off them.
  **One paste is still ONE batch, keyed on its name**, and a re-paste replaces every row that batch
  previously declared whatever containers it names this time.
  *Keying a batch on its container was tried and reverted (unpushed, 2026-08-07).* It double-counted
  on a MOVE: five formulas pasted in "Santo BPO", dragged into "New Can" in game, re-pasted → the
  new container was replaced and the old container's batch was left standing, 5 → 10. That fails in
  the dangerous direction, since an over-counted print cap stops the reaction concurrency cap
  biting and the planner schedules parallel jobs off prints the user does not have. Rows already
  written under the old `paste:loc:` keys are re-keyed to `_batch_key(batch_name)` by
  `_migrate_location_batches` (in `ensure_manual_blueprints_table`, so once per process), which
  makes them ordinary named batches instead of orphans nothing could ever replace. The regression is
  pinned by the `MOVED_A`/`MOVED_B` case in `test_blueprint_paste.py`.
  Location's one remaining job is the **default batch name** when the user types none
  (`_default_batch_name`: one container → `container — structure`, several in one structure → the
  structure, several structures → the first plus a count, none → "Industry window"); the preview
  puts it in the name box so the user can keep it across a move. A short-layout paste can still be
  asked for a structure (the UI offers the `/api/markets` build-structure rows the Industry Facility
  dropdown uses, plus free text) — that records the place on the rows and may name the batch, and a
  typed name always wins. `list_blueprint_batches` groups on the batch key alone and summarises the
  location: `places` is how many it spans, `structure`/`container` are filled in only when that is
  exactly one.
- **The merge rule is REPLACEMENT, per product**, documented in full in `owned_blueprints`'
  docstring. For a product with at least one declared print the declaration IS the holding and the
  ESI reading for that product is dropped; other products are untouched. Batches SUM with each other
  (they are different characters' windows), but the drop is account-wide for the product a batch
  names — so a product two characters hold needs both windows pasted. The UI says so in one line. Not addition, because the
  two sources share no key a user can type (an ESI row's identity is its `item_id`, invisible in the
  client), so adding would double-count every re-typed print — silently and unboundedly. Replacement's
  failure is bounded and visible on the plan. It is also the rule a pasted hangar already gets.
- **It does not double-count against the formula evidence layer either.** A declared product is
  marked `source: "manual"` on its `owned` entry, and `formula_print_floor` skips it entirely
  (precedence **a0**, above the paste rule): a hand-declared formula, a pasted one and one observed
  in a job are one physical item at least as often as three, and the declaration is the only one of
  the three that claims to be the whole holding.
- **`me_source` reports `"declared"`, never `"owned"`.** A typed number is the user's word; an ESI
  read is a measurement. The chip on every job says which. The declaration beats the ESI read because
  ESI can only see the PERSONAL hangar — the corp-hangar print the builder will actually install is
  one it structurally cannot see, so they are usually not two descriptions of one print. The
  per-order override still wins over both: it names one order's print, which is more specific than an
  account-level statement.
- **BPO-vs-BPC preference** (`prefer` = `''`/`bpo`/`bpc`, a property of the PRODUCT — setting it on
  any row sets it for all rows of that product). When a holding contains both kinds, the plan is told
  which to spend, because the two differ in two numbers at once and the math cannot choose: an
  ORIGINAL costs no copies and covers any batch but is ONE print, so one job at a time; N COPIES run
  N jobs side by side but have finite runs and are consumed. `_apply_kind_preference` only ever
  narrows, and only when both kinds are present, so a preference can never empty a holding. A row
  with `quantity = 0` declares no print and carries only the preference — how an account whose prints
  ESI *can* see states a choice without retyping the holding.
- Manual prints feed the copies pool, `_print_limits` and the cost basis exactly as ESI-read ones do
  — they are the same `{me, te, kind, runs}` entries in the same `copies` list.
- Covered by `test_manual_blueprints.py`, including the conservatism case: with the flag off nothing
  is read, and a plan run against a holding whose dicts RAISE on the two new keys (`source`,
  `prefer`) is byte-for-byte the plan run against a plain one.
- Covered by `test_every_copy_the_account_holds_counts`,
  `test_a_stack_of_originals_is_not_a_copy_that_covers_nothing`,
  `test_each_job_runs_off_the_copy_it_is_installed_on`,
  `test_an_override_still_beats_every_copy_you_own` and
  `test_a_single_copy_account_plans_exactly_as_before` (a one-print account plans byte for byte as
  it did).

**A manufacturing job also cannot exceed the blueprint COPY's runs.** The SDE `max_runs` is the
blueprint type's per-job limit; what actually binds is the copy — the largest single copy in
`owned[tid].copies` for ones the account holds, `bp_acquire[tid].runs_per_copy` for ones the plan
would buy (one contract is one copy). With copies in hand but nothing listed to size a bought one,
the copies you hold are the only evidence there is and a bought copy is assumed no larger: an
over-split batch is merely inefficient, a job bigger than its copy cannot be installed at all. This only became load-bearing WITH the packing above: making jobs longer is exactly what
pushes a job past a copy's runs, and a 20-run job off 5-run copies is not a plan, it's a plan that
cannot be installed. An owned BPO has no such limit. Reactions have no blueprint and are untouched.

**And ONE PRINT RUNS ONE JOB AT A TIME.** A blueprint is a physical item: while it is installed it
is locked, so parallelism is bounded by how many prints you hold, not by free slots. `build_tasks`
capped runs and slots and never this — measured in the container, one owned 4-run BPC planned FOUR
simultaneous jobs off it, and one owned BPO planned TEN (unlimited *runs* read as unlimited
*parallelism*, quoting a 1.6h makespan for 20 sequential runs). `_print_limits(params, tid,
activity, runs)` returns `(prints, can_buy)`: prints = one per entry in `owned[tid].copies` (an
ORIGINAL is one item too) plus the copies the plan already buys to cover the runs those don't.
- **Where copies are listed, the plan BUYS the prints that fill the idle slots** — decided after
  packing, off the final job count, so every print it asks for is one that buys time (the same
  "slots are only spent where they buy time" rule).
- **Reported separately, always.** A copy bought to fill a SLOT is not a copy bought because the
  RUNS are short; on a capital that difference is billions the builder did not ask to spend. It has
  its own list (`blueprint_parallel`), its own metrics (`blueprint_parallel_cost` /
  `_copies`), its own per-requirement count (`copies_for_slots`, a FOURTH number beside `runs`,
  `blueprint.runs` and `copies_to_buy`) and its own line in the UI — the SPEND is real money and
  stays visible whatever else gets trimmed off that page. Never folded into
  `blueprint_cost` — same rule as `marginal_saving` and the `blacklisted` badge.
- **Never buy what cannot be bought.** `bpo_only` and a type with nothing listed cap instead,
  running fewer, longer jobs; a type with neither an owned copy nor a listing is UNKNOWN and stays
  uncapped, because blueprint scope is opt-in and an unconnected character looks exactly like an
  empty drawer.
- The per-job run cap still binds underneath (`ceil(R/cap)` is a floor on job count the print cap
  may not push below — that batch is short of RUNS, which is a different report).
- `_jobs_on_copies` deals the runs out one print at a time, each taking an even share or its
  proportional share of the capacity left, whichever is larger. Purely even stranded runs on a
  5-run/1-run pair and needed a second job back on the first copy — a job the plan counted as
  concurrent and physically was not.
- **A REACTION FORMULA is an item too** — it locks into the reactor for the job, so one formula is
  one concurrent reaction. Three differences from a copy, all load-bearing: it binds on CONCURRENCY
  only (formulas can't be copied, so runs-per-job never binds); formulas **STACK**, so the cap is
  how many ITEMS are held (`owned_blueprints` expands a positive `quantity` into that many entries,
  `_STACK_CAP` 200 — 20× Synth Mindflood is 20 reactors, and a blanket one-per-type cap would be as
  wrong as no cap); and the plan **never buys one**, because a formula is durable and reused by
  every later build (the rule `acquisition_costs` already applies to originals). Formulas were
  invisible until 2026-08-04: `owned_blueprints` built its blueprint→product map from the SDE
  `blueprints` table alone and **not one of the 112 `reaction_id`s is in it**, so every formula ESI
  returned was dropped at that join (50 sat unused in prod). A `reaction_id` IS the formula item's
  own type_id, so the fix is `SELECT reaction_id, output_type_id FROM reactions` unioned into the
  map — no new fetch, table or scope.
- **UNKNOWN ownership never caps, at BOTH levels.** Capping on absent evidence is the single most
  damaging way this could go wrong, and "absent" happens twice over:
  - **Per TYPE** — `_print_limits` returns `(None, False)` for a type with no observation at all.
  - **Per ACCOUNT** — `owned_blueprints` unions only the characters that HAVE a cached blueprint
    list, so on a partly-connected account every count in it is a **floor**: prod account 1 has
    **2 of 14** characters cached and still shows prints for **159 types**, while account 9022 has
    3 of 3 and 31 of its 50 formula types genuinely are held singly. Same numbers, opposite
    meanings. `blueprint_coverage(ctx)` → `BuildParams.blueprint_coverage` → `prints_known()`
    gates the whole cap on `cached >= characters`; anything less plans **exactly as it does
    today**. A character without the blueprints scope can never have a cache, so such an account
    stays "unknown" until it connects one — deliberately NOT a partial-credit scheme, and never a
    cap over just the subset we can see.
  Silently not capping is its own kind of lie once the user knows the feature exists, so the plan
  says which state it is in: `print_coverage` (`{characters, cached, missing, complete,
  prints_counted}`). That state used to get its own banner ("this schedule assumes unlimited
  blueprint copies"); it was cut on 2026-08-04 as prose nobody acts on, and now rides in the
  build-time tile's tooltip instead. **The gate itself is untouched and must stay that way.** Pinned by
  `test_a_reaction_formula_is_an_item_too_and_unknown_ownership_never_serialises` and
  `test_a_half_connected_account_is_never_capped_on_what_it_half_shows` — the second exists
  specifically so the coverage check can't be "simplified" away later.
- **What can't be bought is REPORTED, not spent on** — `print_limits` (`{name, noun, held, jobs,
  extra, hours, hours_if_held}`, `metrics.print_limited_steps`) says what holding more prints would
  save on that step, and no ISK anywhere moves for it. Measured with one formula of each held: a
  2× Phoenix goes 761.6h → 1611.3h and an Archon 525.1h → 1008.0h — the honest cost of a single
  reactor formula, against a plan that had been running 10 jobs a type off it.
- Measured on a real Archon (10 mfg slots, one 10-run copy per component): the cap alone costs
  507.6h → 525.1h and 10 fewer jobs; with copies purchasable the makespan is held at 507.6h for 18
  copies. Covered by `test_one_print_cannot_run_two_jobs_at_once`,
  `test_a_print_that_cannot_be_bought_caps_instead_of_being_invented`,
  `test_copies_bought_to_fill_slots_are_reported_apart_from_the_runs_they_cover` and
  `test_the_print_cap_is_paid_for_in_time_not_in_lost_runs`.

Work in **runs per job**, never in job count: runs are indivisible, so the question is how many fit
the window (`int(window / per_run)`), capped by what one blueprint copy may carry. Computing it as
`work / window` uses the AVERAGE job length and is exactly what hid the uneven-split case.
Makespan-preserving by construction — no earliest-start ever moves — and
`test_one_products_runs_are_not_spread_thinner_than_its_own_pace` plus
`test_slack_comes_from_the_consumer_not_from_stage_mates` both run the real scheduler to prove it.
Slot contention is ignored in the slack model, which is the safe direction: consolidating only ever
reduces the number of jobs competing for slots.

## Scheduling: slots, pace, cohort alignment and compaction

**Slots are only spent where they buy time** (`build_tasks(..., depths=, deps=)`). A stage finishes
when its SLOWEST job does, so a job that lands early buys the plan nothing: its slot idles, and —
the part that actually matters — **it costs the builder a second login**. Jobs finishing at 2h32m
and 5h05m mean logging in twice to start work that one trip could have covered. Least effort is the
constraint everything here fits inside, so a job runs as long as it may.

**The question is never "how much longer is this job", it is "does this move the delivery".** Runs
are indivisible and rarely divide the pace evenly — four 2h 33m runs against a 5h 05m pace is 1.996
runs a job, and refusing that by 26 seconds leaves four jobs holding four slots — so overshoot is
allowed. **`_PACE_OVERSHOOT` is 100%, and it has to be**: a job holding ONE run can only grow by
taking a second, which is a doubling by definition, so every smaller allowance tried here (5%, a
flat 20 minutes, 2% of the makespan) was arithmetically incapable of merging a 1-run job however
much slack it had — an 18-slot component stayed at 18 slots through four attempts at tuning it. It
is not the safety bound. `_DELIVERY_OVERSHOOT` (2% of the whole build's makespan) is, and it is what
protects the quote; `_ALIGN_FLOOR` (20 minutes) sits under the first for short windows.
**Measured on a real 206-hour Archon: 232 jobs → 159, a third of the slots back, for 32 minutes
(+0.26%).** It plateaus at 100% — past that nothing more merges, because what remains is bounded by
runs available, blueprint copy caps and genuine dependencies.
A **deliverable is exempt from the overshoot entirely** (`no_consumer`): it may still be packed to
its own natural length, but the allowance buys slots by finishing components later and a finished
product has no later to give — a percentage of a
short window is seconds, and nobody is served by that. A builder does not log in for fun: they log
in to set everything going at once, and whether the jobs then land ten or twenty minutes apart is
immaterial. What matters is that the slots are working while they are away and that the ones they
don't need are free for the next order. Both matter and for different reasons: the first was added because
rounding up a half-hour component with half an hour of room doubled it and moved everything
downstream; the second because a builder quoting 8 days against a competitor's 14 cannot spend hours
to save logins, while on a 14-day build a few minutes is nothing.

**An allowance grows a job; only a target lands it** (`_align_cohorts`). The rules above decide how
long a job MAY take before it holds something up. That is not the same question as when it should
LAND, and a builder logs in at landings — which is why the allowance above, correct as it is, left
the same builder with Hypnagogic Neurolink Enhancer at 10h11m beside Sulfuric Acid at 7h39m and
Oxy-Organic Solvents at 5h05m. The `+1 run` step can only add ONE run past a window and Oxy-Organic
needed three; sweeping `_DELIVERY_OVERSHOOT` from 2% to 100% on the real Archon moved **not one
job**, and widening it far enough to reach grew a *different* job to 15h18m, past the pace the wave
was landing at. So: every type starting at the same moment is a cohort, and each is packed up to the
longest job that cohort already has. Measured on the real Archon: **159 jobs → 143**, with Sulfuric
Acid, Oxy-Organic and Hypnagogic all landing together at 10h12m, for +1.7% makespan — and that
+1.7% is measured against the assumption that the builder is present the instant every job lands. At
a 12h login cadence the aligned plan delivers in 220.1h against the old plan's 232.1h.
**The bound is enforced in `plan_queue`, on the SCHEDULED makespan**, which drops the alignment
wholesale if it cost more than `_DELIVERY_OVERSHOOT`. Don't move that check back into the packer:
per type it rejects the merge it exists for (Oxy-Organic's window is 2h33m against a 4h08m
allowance), and per plan the packer's own model has no slot contention — it read 211h where the
schedule delivered 210.46h and gave back that merge for nothing. A deliverable is exempt, for the
same reason it is exempt from the overshoot.

**Compaction fills up to the pace the plan already runs at; it never sets a new one.** No job is
ever made longer than the longest job the plan already had (`pace_cap`). Slack says a component
*could* take the whole critical path — taking it is a different question, and the answer is no:
stretching four 2h 33m runs into one 10h 11m job is free only in a model with unlimited slots and no
interest in when that item is finished. In the real plan it put the item seven and a half hours
further away and held a slot for all of it. Shipped that way for one deploy, reported immediately.

Each type is given the fewest jobs that still land by its deadline, where the deadline is **when the
job consuming it can start**, bounded by that cap. A type with NO consumer is a deliverable and answers to **itself, never
to the makespan** — pacing a finished product against the slowest thing in the queue trades the one
number a customer feels for slots nobody asked to free (a 20-run product taking an hour alone became
a ten-hour job the moment a 100-hour order was queued beside it). Slack is for components; first
delivery is not slack. Two rules follow,
and both were learned from real builds rather than guessed:

* **A type has its own slack before any dependency is considered.** `_balanced` splits R runs over n
  slots unevenly — 35 runs over 29 slots is 6 jobs of 2 and 23 of 1 — and the batch finishes when
  the biggest chunk does, so every other job may carry that many runs too. That is 18 slots instead
  of 29, not a minute slower. Reported from a real Sulfuric Acid batch. This needs no dependency
  graph and applies on every path into `build_tasks`.
* **Slack comes from the consumer, not from stage-mates.** An earlier version paced each type
  against the other types at its own depth in the same pool, which is a crude proxy: a type alone at
  its stage paces against itself and stays fully split, and two types feeding one job from different
  depths never see each other.
* **And it travels DOWN a chain.** The backward pass runs **consumers first** (`latest_start`,
  `_packed_duration`), because a component's deadline is when the job eating it must START, and that
  is only known once that job has itself been stretched. Capping at the consumer's EARLIEST start —
  the first version — meant a component whose consumer was off the critical path inherited nothing.
  Reported from a real plan: four things in one wave finishing at 2h32m, 2h47m, 5h05m and 10h11m,
  four separate moments to log in at, from work that could have landed together.

**A ceiling on how long ONE REACTION job may run (`industry_job_length_policy`).** Compaction is
right and still leaves a builder with 5,000 runs of a reaction parked in a single reactor for weeks
— reactions have no per-job run cap, so nothing else in the packer stops it. `max_reaction_job_days`
(per account, `pp_industry_settings`, applied through `apply_account_build_options` /
`prepare_plan_inputs` like every other build option, converted once to
`BuildParams.max_reaction_job_hours`) is the builder saying "never more than two or three days".
- **It is the same KIND of bound as `pace_cap`** — a "never longer than", not a target — so it rides
  in the same `min()` (`window = min(hard_window, pace_cap, job_ceiling)`) and the existing
  `window`/`_packed_jobs`/`_packed_duration` machinery does the splitting. There is deliberately no
  second splitting path. `_tightest(p)` is the pair, and it is exactly `pace_cap` when no ceiling is
  set. `hard_window` stays out of it for the reason it always has: the pace may be overshot by a
  hair to reach a whole run, a consumer's start may not. The ceiling also clips the `+1 run`
  allowance and the `_align_cohorts` target — alignment LENGTHENS a job, which is what a ceiling
  forbids, so a type with one keeps its own landing.
- **It can only ever shorten.** A smaller window means more, shorter jobs; a deadline that was met
  stays met, and a ceiling looser than the pace changes nothing at all.
- **It cannot manufacture slots or formulas**, and gets no say on concurrency: `_packed_jobs` never
  exceeds `n_wide`, which already carries the reactor pool and the formula cap (one formula is one
  concurrent reaction, never bought). An unreachable ceiling is honoured as far as the concurrency
  goes and the shortfall is stated — `why.ceiling_h` / `why.ceiling_met` per job, `bound_by:
  "job_length"` when it is what bit, and `job_length_limits` /
  `metrics.job_length_limited_steps` per plan. Silently missing a target the user set is worse than
  never offering one.
- **Reactions only, on purpose.** Splitting a MANUFACTURING batch spends blueprint COPIES, which
  cost ISK; a reaction formula is durable and reused by every later build, so splitting a reaction
  is very nearly free. That asymmetry is the whole reason this half ships alone (the manufacturing
  half is T11 in `TODO.md`).
- Covered by `test_a_reaction_job_can_be_held_to_a_maximum_length`,
  `test_the_job_length_ceiling_can_only_ever_shorten`,
  `test_a_consumer_deadline_still_beats_the_job_length_ceiling`,
  `test_the_job_length_ceiling_never_touches_manufacturing`,
  `test_a_job_length_ceiling_cannot_manufacture_slots_or_formulas` and
  `test_a_plan_with_no_job_length_ceiling_is_byte_for_byte_the_old_plan` — the last one runs the
  packer against params the field does not exist on, so anything reading it unguarded fails there.

## The Step-by-step view must account for its own total

**The Step-by-step view's parts must account for its total** (`_indStepsHtml`). Each step is an
OFFSET into one wall clock, not a length, and the steps used to show only that offset — so a real
2× Phoenix queue read "Finished — 2 jobs ≈ +14h" directly above "Done — built in ≈ 13d 12h", with
the 12d 21h the hull job actually runs for visible only inside the collapsed "show items" fold.
Both numbers were right and the screen was lying about what they meant. Every step therefore
carries its longest job AND when the step has fully landed (`s.longest` / `s.end`, max over the
waves it was collapsed from), the last stage to land equals the makespan by construction, and the
Done line states that it is wall clock — naming the step that drives it — rather than sitting there
looking like a sum. **Do not "fix" this by making the offsets add up**: they overlap on purpose, and
`test_the_step_by_step_parts_account_for_the_whole` asserts that they don't sum, alongside the
renderer actually rendering the reconciliation.

## Planning each order on its own (`industry_per_order_plans`)

`plan_queue` aggregates every queued order into ONE demand and builds each shared component once.
That is right for cost and wrong for how the work is run: **a job outputs to exactly one
container**, builders run a container per build, and a batch shared between two orders has nowhere
to deliver. `plan_queue_per_order` plans each order alone and schedules the lot against one slot
pool. Off by default; the setting is per ACCOUNT (`pp_industry_settings.per_order_plans`, its own
write path `GET/POST /api/industry/per-order-plans`, gated on the rollout ladder) because it is a
standing way of operating, and `_run_queue_plan` branches on it.

**It costs money, so the number comes before the switch.** `POST /api/industry/queue-plan/compare`
runs both plans off the same inputs and reports cost, makespan, job count and the per-order split.
Measured as a what-if on the two real queued builds (2026-08-05, two customers instead of one):

| | 2× Archon | 2 orders × 2 Phoenix |
|---|---|---|
| net cost | +2.45% (+138.8M) | +0.96% (+88.2M) |
| blueprints | +39.8% | +4.6% |
| makespan | −1.27% | −6.08% |
| jobs / build steps | 60→92 / 6→12 | 49→74 / 4→8 |

Splitting is **not always slower** — more, smaller batches fill idle slots — but it always buys
more prints and more materials, and it lands the work at more separate moments (the Phoenix queue
goes from 4 wave starts to 9), which is the effort cost to watch.

**Four things are consumed first come first served down the queue, and each was a real error when
it wasn't.** Two orders cannot both spend the same thing, and queue order is the only fair rule:

- **Stock.** A curated order (`sources_owned`) is capped by its OWN boxes; the queue-wide remainder
  caps that in turn. Only what a batch is actually netted against is deducted — `aggregate_demand`
  nets stock off BUILT types.
- **Contracts.** `cost_for_runs` / `cost_for_copies` now report the listings they spent (`used`),
  and each order plans against a pool with the earlier ones removed. Without it both orders took
  the cheapest copy and the split read **76.7% CHEAPER on blueprints** than the batch it is.
- **Owned copies.** A BPC's runs are spent when they are run, so crediting one copy to two orders
  reports a shortfall of zero twice. Originals are exempt — they run forever.
- **Job fees.** `_order_cost` reached for `mfg_cost_index`/`rx_cost_index` directly instead of
  `params.job_fee_rate`, ignoring per-job ROUTING; on a real build that alone made planning apart
  look 6.84% dearer (220.5M of fees against 511.4M) when the two plans were identical.

**Cross-order alignment has to be explicit.** `_align_cohorts` only sees the types in one
`build_tasks` call, so orders planned apart would never be aligned against each other and the
builder would log in once per order. Each order is packed once with `align=False` (`plan_out` /
`start_out`), the union is aligned keyed per order, and each order is replayed with `align_hint` —
which lands exactly where the local answer would have gone, so a hinted single order is identical
to an un-hinted one. The `_DELIVERY_OVERSHOOT` give-back is applied on the SCHEDULED makespan, same
rule as `plan_queue`.

**It returns the same shape as `plan_queue`, deliberately** — the checklist, progress, the customer
share and the whole build page read that contract. Per-type rows are merged across orders (runs
summed, `orders: [id…]` naming who they are for; ownership fields come from the first order that
built the type), `per_order` carries what the aggregated plan cannot, and every scheduled job
carries `order_id`. `_blend_margin` does NOT run on this path: it exists only because a shared
batch has no per-order cost, and here each order has a real one, so `metrics.price` is their sum.

**Still not modelled:** a job's output CONTAINER (the point of the whole exercise — item 2f.3 in
TODO.md, item "2f-residual"), and print locking ACROSS orders — two orders each see the one BPO they share and may each
schedule a concurrent job off it. Per-order copy *runs* are consumed correctly; concurrency is not.

## Build options, and who installs each job

**Build options are stored per account** (`app/industry/settings.py`, `pp_industry_settings`) and
applied in `prepare_plan_inputs` — the single point every plan path resolves through. They used to
live only in the browser and travel as request fields, so any plan run WITHOUT a browser used library
defaults (no facility time bonus, 3% threshold) and disagreed with what the user was looking at: the
same bug produced the checklist naming a job the plan scheduled last, and a customer share link
quoting 14d 4h against an 8d 8h plan. `apply_account_build_options(ctx, opts)` fills only fields the
caller did NOT explicitly set — keyed on pydantic's `model_fields_set`, because a default and a
deliberate value are otherwise indistinguishable and the live UI must still be able to tweak a knob
without saving first. The frontend PUTs `/api/industry/settings` (debounced) whenever a knob moves and
seeds the form from it on load, guarded by `_indRestoringSettings` so restoring controls never writes
the browser's state back over the account's.

**One set of build options for the whole queue.** `/api/industry/to-install` is a **POST** taking the
same `QueuePlanRequest` as `/api/industry/queue-plan`, and the frontend builds both bodies from one
`_indQueueBody()`. This is not tidiness: the checklist used to plan with DEFAULTS while the status
card beside it planned with the user's real settings (facility, threshold, speed, ME/TE overrides),
so the two disagreed about which jobs were even ready — the checklist said "start the Revelation" off
a plan that bought every component, while the screen showed two stages of component jobs that nothing
was telling anyone to start. Any new whole-queue endpoint must take the same options.

**Who installs each job, at every stage.** `/api/industry/to-install` names a character for the jobs
you can start *right now* (off FREE slots). Everything after that used to be anonymous — a plan said
"stage 1: 12 jobs" and never said who runs them. `schedule.assign_characters(waves, characters)`
(pure, I/O-free like the rest of that module) now stamps `character_id`/`character_name` on every
scheduled job: it walks the waves in time order, releases a character's slot when that job ends, and
gives each job to whoever has the most capacity free (which spreads work instead of hammering one
toon). Safe by construction — the scheduler's pool sizes are the sum of the characters' own slots and
slots are interchangeable, so an aggregate-feasible schedule is always assignable; a job with no
capacity is left unassigned rather than given a fictional owner. Called by both plan paths
(`/api/industry/plan` and `_run_queue_plan`) with `_slot_pool(ctx)["characters"]`. Note this uses
TOTAL slots (the schedule spans days, busy slots free up) while to-install deliberately uses free
ones.

## Alliance-shared buildings (`industry_group_structures`)

An alliance builds in the same few structures, and describing one — hull, rig tiers, rig families,
system, tax — is real work. Measured in prod (2026-08-05) two accounts had independently configured
the SAME four structures, maintained twice. A group manager shares theirs (`POST
/api/markets/share`, a COPY to `owner_kind='group'`, re-sharing a location updates rather than
duplicates); every other member sees them as **suggestions** and takes one with `POST
/api/markets/adopt`.

Three rules, all load-bearing:

- **A suggestion is inert until adopted.** `build_structures` — what the planner routes jobs into —
  stays the account's own list. Adopting changes where jobs go and what they cost, so it is a
  decision, not a side effect of an alliance-mate describing a building.
- **Scoped to the ALLIANCE by construction**, not by a filter that can be forgotten: `member_group`
  joins a real character's `alliance_id` to `pp_groups.alliance_id`. Another alliance on the same
  install is a different group id and is invisible — they don't dock in our structures.
- **Your own row always wins.** A location the account has already described is never suggested
  back, and adopting one it already has is a no-op: one building with two rig answers is two
  answers that can disagree.

Only a **manager** may share, because a wrong rig answer adopted by everyone is an efficiency the
plan quotes and nobody can see is wrong. `_SHAREABLE_COLS` is what travels — everything describing
the building, rig families and its own system and tax included; a shared structure whose rigs each
member must re-answer has shared nothing. Covered by `test_group_structures.py`.

## Defaulting the build system (`industry_default_build_system`)

Job fee = EIV × (system cost index + facility tax + 4% SCC), and the index only counts once a build
system is configured — in prod **1 of 26 accounts** had one, so manufacturing was quoted light by
the index share (76% of the fee in Jita) and reactions charged no install fee at all.
`account_build_defaults(ctx, with_basis=True)` now answers in three tiers, most specific first, and
**says which it used** (`cost_basis.basis`, same rule as `skill_time_basis`):

`configured` (the system the user set for Reactions — unchanged, and still first) → `structure` (the
system of a structure they told us they BUILD in, with that structure's own tax — not a guess, they
described the building) → `reference` (Jita, when we know nothing). Jita is the honest REFERENCE
because its index tops the range, so a quote built on it is conservative — but it will be wrong for
a null-sec builder, and the notice says so and offers the fix. The last two tiers are behind the
flag: they change the cost of every existing account's build.

## The start-now checklist and the schedule agree on who CAN install (`industry_install_skill_aware`)

`install_block` derived its own assignment and ignored the skill-aware one `assign_characters` had
already made, so the main screen could say "start this on X" directly above a plan marking that job
blocked for X. The two paths legitimately differ on capacity — the schedule spans days and counts
TOTAL slots, the checklist is about what fits now and counts FREE ones — so the fix is not to reuse
the assignment but to share the RANKING: `schedule.skill_tier(eligibility)` (2 capable / 1 unknown /
0 incapable), capacity deciding within a tier exactly as before. A job with no capable character
free is still assigned, carrying `skill_ok: False`, because an instruction that says what is wrong
beats no instruction. `skill_ok` is recomputed for the character actually named — a stale ✓ carried
over from the scheduler's pick is worse than no mark. The eligibility map rides on the plan as
`_eligibility` and is popped before the response: it is sets of character ids, which neither
serialise nor belong in a browser.

## Quoting: margin → price

**Quoting a customer: margin → price.** `BuildParams.margin_pct` (default `MARGIN_DEFAULT_PCT` = 10,
clamped 0–100) produces `metrics.price` alongside `metrics.margin_pct`. The base is **net cost, not
total spend**: a build that over-produces reusable intermediates keeps them, and their value is
already credited out of net cost, so quoting off total spend would bill the customer for materials
the builder keeps. The margin is stored per account like the other build options AND snapshotted on
the order (`pp_industry_orders.margin_pct`) when it's queued — a customer holding a quote must not
see the price move because the builder changed their default afterwards; the share uses the order's
value when it has one. The UI slider re-prices client-side (`_indPriceOf`) rather than re-planning:
margin is arithmetic on a cost the server already returned.

**The whole-queue price uses each ORDER's margin, not one blanket rate** (`_blend_margin` in
orders.py, called at the end of `_run_queue_plan`). `plan_queue` marks the entire queue up at
`params.margin_pct`, which meant editing a customer's margin moved nothing on the builder's own
"Your Build" sheet while the share link that customer holds already quoted the new figure — the two
disagreeing about the same order. Queue cost is a shared-batch total with no per-order split, so
each order's share is apportioned by its **standalone** cost (`targets[].unit_cost × quantity`,
exposed by `plan_queue` from its own memoised unit costs); the shared-batch saving is spread
pro-rata rather than invented per order, and `net_cost` stays the base every price derives from.
With one margin across the queue it reduces exactly to the old formula. `metrics.margin_mixed` says
whether the orders disagree and `metrics.margin_pct` is then the effective blended rate, so the
number shown always explains the price. **The status tile renders `metrics.price` and must NOT carry
`data-ind-price`** — that attribute is the planner slider's live re-price hook, and it was
overwriting the queue's per-order price with the slider's rate (the slider deliberately sets the
margin for NEW builds only). The single-product preview still re-prices live off the slider, which
is correct there. Covered by `test_queue_price_uses_each_orders_own_margin`.

## The build page's notice stack (trimmed 2026-08-04)

The page had accumulated a column of coloured banners above the plan. They are now ONE block
(`_indNotices` in `static/industry.js`, `.ind-notes`), and the bar for being in it is: **does this
change what the builder DOES or what they SPEND, or correct a number they would otherwise
believe?** A notice a reader will not act on is not worth its space, however true it is. What the
user objects to is **the plan narrating its own reasoning** — the three panels cut on their
instruction (implied hauls, "this assumes unlimited BPCs", the skill-training card) all reported
our state of knowledge instead of helping them act.

Corollaries:
- **A notice that fires under normal conditions becomes wallpaper.** The missing-build-system
  warning fired for ~96% of accounts; the honest fix was a sensible default, not a permanent
  warning about an unset field.
- **A control is not a notice.** Decision surfaces (the marginal-saving strip, the reaction build
  policy) belong with the other make-or-buy controls, never as another banner in `_indNotices`.
- **Prefer removing to adding**, and when adding is right, fold detail behind the common case
  (checkboxes behind a link, per-type rows in a tooltip) rather than in front of it.
- When deleting several at once, offer the user a **cut list to veto before deploying**.

Kept: unpriced materials (cost is a floor), skill-time basis (delivery times are assumed V/V),
cost basis (job fees exclude the system index — with the button that fixes it), copies short of
runs, copies bought for parallelism (real money, one line, per-type detail in the tooltip), prints
the plan is short of (one line, best trade named), missing blueprints (contract prices), the
**reaction-default line** (2026-08-05 — one dismissible line, shown only to accounts still on the
default, saying the default moved and where to set their own; it clears the bar because it moved
what a build COSTS for someone who changed nothing, and the fix is one click below it — it is an
announcement, not a second control), and the skill-gap blocker list — which renders in the **preview modal only**, as it always has.

Cut, deliberately: **"Parts to move"** (`plan_moves`, gone server-side too — a builder routing jobs
to two structures knows the parts travel); the **skill/training advisor card** (`advisor.py` and
its endpoint, tests and flag all still live — it just isn't about THIS build, so it isn't on this
page); the **"unlimited blueprint copies" coverage banner** (now a tooltip, see above); the SDE
`blueprint_skills`-backfill notice and the standalone "no skill data yet for X" box (both were
banners about our own state of knowledge; the unknown-characters line survives inside a real gap
report). Don't re-add any of these without a reason that clears the bar above.

## Industry performance: one plan per page load

**Industry performance.** Two things dominated page and share-link load, both measured before
changing anything:
* The **recipe graphs are cached per process** (`graph._cached_graph`, 15-min TTL, `clear_graph_cache()`
  to drop it). `load_manufacturing_graph` reads ~4,800 blueprints and every material row — 68ms
  locally, more against Postgres — and it ran on EVERY plan call, several per page. Every consumer is
  read-only (no caller mutates a recipe), so one copy serves them all. The TTL is a backstop for an
  SDE rebuild under a long-lived process; a deploy restarts the pod anyway. **Tests that seed their
  own synthetic SDE must call `clear_graph_cache()`** — `test_industry._seed_con` does.
* The page ran **three full queue plans** per load (queue-plan, to-install, progress). Both of the
  others are views OF the plan, not separate questions, so `/api/industry/queue-plan` now returns
  both inline: `install` (via the shared `orders.install_block(ctx, res)`) and `progress` (via
  `queue_progress(ctx, res=...)`). **One plan per page load.** Progress needs the queue planned with
  no stock netted off (`use_stock=False`, or its bar could never fill), so `_run_queue_plan(...,
  want_full=True)` produces that second variant from the inputs it has ALREADY resolved rather than
  from a second request that would repeat every DB read in `prepare_plan_inputs` — graph, names,
  blueprints, contract index, slot pool. With no stock enabled the two are identical and the second
  `plan_queue` is skipped outright.
* The status card **paints the last plan first and checks it after** (`_indReadPlanCache` /
  `_indWritePlanCache`, sessionStorage). The cache is keyed on the queue itself — order ids,
  quantities, force-build and ME/TE overrides — and the orders list is fetched before it is read, so
  a plan is only ever shown for the queue that is actually there (which also makes flashing another
  account's build impossible). **The running build id is part of that key** — otherwise a deploy
  that changes how plans are computed would keep serving the pre-deploy plan for fifteen minutes and
  the change would look like it hadn't shipped. Capped at 15 minutes, past which the ETAs are
  visibly wrong and it waits instead.
Measured on a Revelation: share link 706ms cold-process → **47ms** warm; page ≈73ms serial → ≈25ms.
Keep it that way: anything that needs a whole-queue plan should take one that already exists rather
than calling `_run_queue_plan` again.

## First use

A first-run setup screen (`_indRenderWizard`), mirroring the Reactions gate (`_rxApplyGate`) down to
its step chrome — two onboarding screens that look unrelated read as two different products. Three
steps: **where you build** (required, the Facility dropdown inline), **characters & slots**
(optional, the real `/api/industry/slots` readout plus its excluded-character reasons), **build
system & fees** (optional, folded, reusing `_rxAccountSettingsFormHtml`).

**Every step is completable without leaving the page, and Save & continue is never disabled** —
that property is the entire reason a blocking screen is acceptable here. The version this replaced
blocked the tab until a build *structure* was configured, which is more than the planner needs (the
presets in `IND_FACILITIES` — NPC station, T1/T2 ME/TE rigs — cost a build correctly) and could
**dead-end**: adding a real structure needs structure search → a market-scope character → which a
player who has only ever used PI does not have. Zero job slots is likewise a warning, not a barrier.
Once past it, an account with no structure of its own gets a dismissible notice instead
(`localStorage.indFacilityNudge`).

**The flag is per ACCOUNT** (`pp_industry_settings.onboarded`), written by its own endpoint
(`POST /api/industry/onboarding/complete`) — not a field on the settings PUT, which is a debounced
save of the plan form and must not be able to set or reset it. A browser flag would re-ask on every
new device and forget on a cache clear.
Two halves of one rule keep the migration honest: `ensure_industry_settings_table` backfills
`onboarded = 1` for any row that already has `updated_at` (saved build options ⇒ this account has
plainly used the tab), and the frontend does **not** seed settings for an un-onboarded account
(`if (!_indHasSavedSettings && _indOnboarded)`). Without that guard the backfill would mark someone
part-way through setup as established on the next pod restart. `_indOnboarded` also defaults to
**true** so a failed settings fetch shows an established user their tab, never a setup screen.
That backfill has a side effect worth a control: nobody who has used the tab can ever see the screen
again, including whoever has to check it. `POST /api/industry/onboarding/reset` (**`require_admin`**,
and it only ever resets the CALLER's own account — it is a test affordance, not a tool over other
users) replays it; the button is in the Job slots modal (folded under an **Admin** `<details>` below
the divider), hidden unless `_featuresIsAdmin`. It writes
`onboarded = 0` and **not NULL**, because the backfill claims NULL rows and would otherwise undo the
reset at the next pod restart.

What first use still assumes:
- **Pricing needs nothing** — `resolve_market_data` falls back to Jita.
- **Blueprints are optional** (ME 0/TE 0 + a "connect a character" reminder), assets are opt-in.
- **The build system comes from REACTIONS settings** (`account_build_defaults` → `reaction_system`),
  so a Reactions-less account quotes job fees light by the system cost index — warned by
  `_indCostBasisWarn`, which must link to **Structures & Markets** (where that field lives); it used
  to open the setup modal, which cannot set it.
- **Slots need the skills scope AND Mass Production / Mass Reactions trained** (`_eligibility`).
  With neither trained the pools are 0, `schedule` starts nothing and the plan renders a 0h
  makespan with an empty checklist — not a crash, but it looks like one. The excluded characters
  and the reason are listed in Setup → Job slots.
