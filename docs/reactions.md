# eve-pi-planner — Reactions

The moon-goo reactor tool: what to run, what it is worth, and how it feeds the rest.
Back to [CLAUDE.md](../CLAUDE.md).

Find a section: `grep -n '^## ' docs/reactions.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Reactions suggestion engine (`app/reactions/advisor.py`)** — what the advisor suggests and on what basis
- **A formula is one reaction at a time (`reactions_formula_cap`)** — why ten free slots can be planned as one job
- **The shopping list buys a chain once (`_shopping_roots`)** — why only the top row of an assign is exploded
- **What you already hold is not work (`reactions_use_stock`)** — how held intermediates shorten a chain, and the one place stock is deliberately not consulted
- **One slot model: a chain's stages reuse a reactor (`reactions_parallel_stages`)** — why stages can't run in parallel, what reuses what, and where idle reactors go
- **Absence becomes knowledge, but only after a paste (`app/reactions/library.py`)** — when an undeclared formula means "you don't own it", and what gets reported instead of planned
- **Formulas to acquire, as a shopping section** — why the formula list sits beside the materials rather than in them
- **Re-planning ONE customer order** — the per-order clear, and the single give-back rule it shares with Clear all
- **Landing a stage in one go (`_align_stage_jobs`)** — why the spread matters more than the total, and what moves to close it
- **One request, one answer (`request_memo`)** — why an order report was rebuilding the same evidence five times
- **An order's runs follow capacity, not fairness (reverted experiment)** — why hosts get different run counts, and what the even split cost
- **One run count per product per stage (`level_stage_runs`)** — why levelling across assigns is sound, and what it deliberately doesn't touch
- **One run count per product, across every character (`reactions_level_runs`)** — the cross-character leveller: how the number is chosen, why it also saves slots, and the one thing it still can't merge
- **Job length is the player's call (`pp_reaction_job_target`)** — the one preference in the job layout: days or runs per job, why `auto` is the default, and why it is not a group setting
- **Run counts you can type (`reactions_tidy_runs`)** — bounded rounding of intermediate runs, and why the end product is never rounded
- **A stage is a DEPTH, not a position in a list** — why siblings share a stage, and how existing rows were repaired
- **Knowing when the next stage can start (`chain_stage_state`)** — the ESI signal behind "stage 2 is ready"
- **Stages on the dashboard are `tier_order`, shown absolute** — how chain order is rendered, and why the number is never re-ranked
- **Pricing: a sell-order price is not achievable profit** — the pricing rule that governs every profit figure shown for reaction goods
- **Where the rest lives** — pointers to reaction content that belongs to another service

---

## Reactions suggestion engine (`app/reactions/advisor.py`)

Split out of `app/reactions/jobs.py`, which had grown to ~1,500 lines covering three unrelated
jobs (ESI job fetching, the persistent slot plan, and this). `advisor.py` holds the two-stage
wizard engine — the knapsack over WHAT to run, then bin-packing onto WHO runs it — plus
`/api/reactions/suggest`. It imports from `jobs.py` **one way only**; `_character_capacities`
deliberately stayed in `jobs.py` (it's about slots, and the customer-order allocation path needs
it too), which is what keeps the dependency acyclic. `__init__.py` imports jobs before advisor
for the same reason.

## A formula is one reaction at a time (`reactions_formula_cap`)

A formula is a physical item and it is **locked into the reactor while a job runs on it**, so one
Ferrofluid formula is one concurrent reaction however many reactor slots are free. This package
allocated against slots only, so it told players to install parallel jobs they cannot.

`app/reactions/jobs.py::formula_concurrency_caps` is the one place the cap comes from, and it
**reuses the Industry evidence layer** (`app.industry.blueprints.formula_print_floor` — personal
blueprint cache ∪ enabled asset stock ∪ distinct observed `blueprint_id`s, paste wins outright)
rather than reimplementing it. The import is inside the function: `app/industry/slots.py`
deliberately does not import `app.reactions`, and this keeps the module-import graph acyclic in the
other direction too. Applied by the Suggest bin-pack (`advisor.py`), the customer-order assign
(`_allocate_and_insert`) and the quoted estimate on an order (`orders.py::_order_report`).

Two rules it must not break: **a missing key means unknown, and unknown never refuses** (no
evidence, or an incomplete blueprint picture, caps nothing — the same rule
`_assigned_slot_capacity` and `_print_limits` follow); and **chain tiers are sequential**, so the
cap is per tier and one formula may serve tier 0 and then tier 1.

## The shopping list buys a chain once (`_shopping_roots`)

Every pending plan row used to be exploded to raw leaves. A chain assign stores a row per tier AND
one for the product, and a walk from the product already covers its intermediates — so a two-tier
chain's goo was counted twice and the player was told to buy double. `_shopping_roots` keeps only
the rows nothing else covers: **a chain is the assign that wrote it, `(character_id, created_at)`**
(all three insert paths write one timestamp for the whole chain), and within that group only the
highest tier is a root. A separate assign of the same product is its own group, so a product
deliberately assigned on its own to sell is still real demand. The per-order materials report never
had the bug — it explodes once from `target_qty`.

## What you already hold is not work (`reactions_use_stock`)

Reactions planned every chain from raw goo — `_explode_chain_tiers` walked the recipe and never
asked whether the intermediate was already in the hangar. `reaction_stock_pool` (enabled sources
only, via `owned_quantities`) is now threaded through `_explode_chain_tiers`, `_ordered_chain_tiers`
and `_explode_shopping_list` and **consumed as the walk goes**, which is the whole design:

* a unit is spent **once per plan** — two branches needing the same intermediate cannot both claim
  it, which is why the pool is threaded through the recursion instead of read fresh at each node;
* a tier the holding covers outright is **dropped along with everything below it** — you do not
  react the inputs of something you already have;
* a partial holding **shortens** the tier rather than dropping it;
* what stock covered is always reported (`stock_covered`, or a toast on assign). A stage that
  silently disappears is indistinguishable from a bug.

Wired into every path that commits or quotes a concrete batch: suggest (one pool for the run), the
order report (two pools off the same holding — the materials walk and the stage walk answer
different questions and must each spend it once), order allocation (consumed host by host),
adopt-orphan, and `assign_reaction`, which trims the client-supplied tiers (reading each run's size
from the formula the PLAN uses, `reached[tid]["via"]` — several formulas output the same product at
wildly different batch sizes, 20 units vs 10,000 in this SDE, so an arbitrary `reactions` row
misjudges coverage by that whole factor; no graph means no trim). **The opportunity list
stays stock-blind on purpose:** it is cached and its callers scale its tiers linearly, and stock
coverage is not linear — so the trim happens at the point rows are created instead.

**Known edge, accepted:** there is no reservation ledger. Two planning runs made back to back both
see the same units, so stock can be promised twice ACROSS plans (never within one). Reserving needs
a commitment ledger this package doesn't have; until then the honest reading is "this is what you
hold right now", which is also what the player sees in their hangar.

## One slot model: a chain's stages reuse a reactor (`reactions_parallel_stages`)

**Within one chain, stage 2 cannot start before stage 1 finishes** — EVE requires the materials to
exist at install time and stage 1's output IS stage 2's input. Not our choice, and not negotiable;
"run the stages in parallel" would mean a job you cannot install.

What follows from it is that a chain's stages **reuse** one reactor rather than each holding one.
`_concurrent_load` (`jobs.py`) has always said so, but until 2026-08-07 only the assign guard asked
it: `_character_capacities` and the dashboard's `free_slots` counted every planned row. So a
3-stage chain of one job each was authorised as needing one slot and reported as occupying three —
and since `_character_capacities` feeds the wizard's bin-pack and the customer-order allocation,
both planned less work than the account had reactors for. All three now go through
`_concurrent_load`; the advisor reserves its own peak the same way (a tier may reuse the slots its
own suggestion holds at other tiers). The manual-assign modal's `chainJobs + 1` reservation became
`chainPeak + 1` for the same reason.

`_widen_to_idle_slots` then spends what nobody claimed. Every step is sized to fit the CADENCE
window (`ceil(runs × cycle / cadence)`), which answers "how much work fits" and not "how fast does
it finish" — so a 3-stage chain ran one reactor for three cadence windows with the rest idle. The
pass runs AFTER allocation (it can never take a slot from a suggestion that wanted one), gives each
extra job to whichever step gains the most hours, and stops at the formulas held and the runs there
are. Widening a tier that is not the busiest costs nothing at all, so it happens even on a full
character. **It moves `jobs` only** — runs, cost and profit are not its business — and the count is
reported as `totals.idle_slots_used` so the extra jobs read as a choice, not an overbooking.

Off ⇒ every one of those numbers is the old per-row sum. `test_parallel_stages.py` pins both.

## Absence becomes knowledge, but only after a paste (`app/reactions/library.py`)

Everywhere else in this codebase, **absent evidence never serialises work**: a product nobody has
said anything about is uncapped, so the tool never refuses work the player can really do. That rule
produced a real failure — an account with ~238 hand-declared formulas ordered Reinforced Carbon
Fiber and was told to react Carbon Fiber, a sub-reaction whose formula it does not hold — because
"not declared" was read as "unknown" rather than "not owned".

`library.py` inverts the rule in exactly one place, under exactly one condition:

* **Completeness comes from a PASTE, never a toggle.** At least one pasted batch (`batch <> ''` in
  `pp_industry_blueprints`) naming at least one reaction formula. Same reasoning that already lets
  a paste win outright over observed jobs in `formula_print_floor`; a "my library is complete"
  checkbox would be the knob CLAUDE.md rule 3 exists to avoid. Rows typed in one at a time never
  make a library complete — three typed formulas are a statement about three formulas.
* **Held is the UNION of every evidence source** (`held_formula_products`) — declared, ESI-scanned,
  in enabled stock, observed on a job. Reporting a formula as missing when it is in the user's
  hangar is the expensive error, so the "you have it" side is read as widely as possible.
* **Report, never substitute.** `missing_formulas()` returns `{complete, formulas, unresolved}` and
  nothing else touches the plan: no step is dropped, re-planned, or flipped to a market buy. The
  rows carry the FORMULA's own `type_id` (contracts list the formula, not the product), and are
  priced from the same public-contract index Industry uses, via `blueprint_type_prices` — a formula
  has no row in `blueprints`, so it maps product → `reactions.reaction_id` instead. **Nothing here
  is in any shopping list or cost total**, exactly like `metrics.missing_blueprints`.

Three surfaces render it, all off the one helper: the Suggest wizard (whole batch, chain tiers
included), a customer order (`_order_report`, off the same `sequence` the quote is built from), and
the manual-assign modal (`POST /api/reactions/missing-formulas`, cached per product so a run-count
keystroke isn't a request). Gated by `reactions_missing_formulas`; off ⇒ every report is empty.

**The sharp edge — unresolved names.** Making absence load-bearing means a formula whose NAME
fails to resolve is indistinguishable from one the user does not own, so a CCP rename turns into a
confident, wrong "go buy this". It has happened: a client copy carried `Fullerides Reaction
Formula` where the SDE has the singular (fixed in `ee633be` by the parser's product-name fallback,
which is why that exact string now resolves). So the import KEEPS what it could not resolve
(`pp_blueprint_paste_unresolved`, replaced per batch, deleted with the batch), and every report
carries it for the UI to show beside the finding — an import status line that scrolled away days
ago is not a warning.

## Formulas to acquire, as a shopping section

`/api/reactions/shopping-list` carries a `formulas` block beside `materials`: the formulas the
CURRENT plan needs and the account does not hold, from the same `missing_formulas` helper the
wizard, an order, a manual assign and the dashboard all use. The shopping list is the page you open
when you are about to go and buy things, so that is where the list of things to buy belongs.

It stays its own section, and out of every total, for a reason that is not cosmetic: **a formula is
a CONTRACT purchase, one item at a time, searched by name.** It cannot be multibought by quantity
the way goo can, so it needs different actions (copy the names, check the Jita contract price) and
must never be summed into a material cost — the player may already hold one somewhere we cannot
see, or may decide not to buy at all. The copy button therefore emits one formula NAME per line,
not the name-and-quantity TSV the material lists use.

## Re-planning ONE customer order

`DELETE /api/reactions/orders/{id}/assignments` ("Clear its jobs") frees every slot an order holds
and hands its runs back, keeping the order itself. It closes a gap in the order flow: "Assign next
batch" only ever adds, `Clear all` wipes the whole account, and cancelling the order to free its
slots throws the order away — so re-planning one order (different characters, a changed batch, a
formula since sold) meant one of those three blunt instruments.

The give-back rule lives in **one** place, `give_back_order_runs`, shared with `Clear all`: the
credit is the TOP row of each chain, which is exactly what `assigned_runs` was incremented by, and
it never goes below zero. Two paths computing that separately is what stranded orders #36-#39.

An order with a counter but no rows — the stranded shape — is also repaired by this endpoint, so
there are now two ways out of it: clear it explicitly, or just try to assign it
(`_heal_stranded_counter`). A row whose job is already RUNNING is cleared too and the job carries
on in-game as an orphan; the count comes back in `running_cleared` so the UI says so before the
player commits, not after.

## Landing a stage in one go (`_align_stage_jobs`)

Stage 2 cannot start until the LAST job of stage 1 lands, so a stage finishing at 2h / 2h / 14h is
a 14-hour stage with two reactors idle from hour two — and two extra trips to the keyboard. The fix
is the reactions translation of the manufacturing planner's `_align_cohorts` ("lift every job to
the longest one already running beside it, so a wave lands in one go"), reached from the other
direction: there, run counts are free to grow, so a short job is given more runs in fewer slots;
here the run counts are fixed by the chain, so what moves is the SPLIT — a step that would be done
in two hours does not need three reactors while the step everyone is waiting on has one.

`_align_stage_jobs` is **slot-neutral**: it only moves a job from one step to another in the same
stage, so it runs after `_widen_to_idle_slots` rather than competing with it, and needs nobody's
capacity. A step is never taken below one job, never pushed past its formulas or its run count, and
a move is only made when it genuinely lowers the stage's finish time. Measured on a synthetic
stage of 80/10/10 runs at three reactors each: 27h → 12h, same nine reactors.

The idle-slot pass now scores by the same objective — the reduction in the STAGE's finish time, not
in one step's own hours, since an hour off a step that was already finishing early buys nothing.
The customer-order path re-balances the same way after `_fit_chain_slots` (which minimises the SUM
of tier durations — correct while each tier was its own stage, wrong now that siblings share one).

## One request, one answer (`request_memo`)

The evidence layer — owned blueprints, the print floor, enabled stock, the pasted library — is
expensive on a real account (fourteen characters' blueprint JSON, the whole asset table, every
observed job) and a **single customer-order report asked for it five times**: two stock pools, the
formula caps, and `missing_formulas` rebuilding the same blueprint evidence again underneath. That
is why opening an order felt slow.

`request_memo` computes each answer once per request. **Scoped to the request, not a TTL cache**,
and the distinction is load-bearing: pasting a window or re-scanning assets must show up on the very
next page load, not in thirty seconds, and a test that seeds rows and reads them back in the same
breath has to see its own writes. The scope is opened by a middleware in `app.main`, so a direct
call — every test, any background job — gets no memoisation at all. `reaction_stock_pool` still
hands each caller its own COPY, since the pool is consumed as a plan is walked.

## An order's runs follow capacity, not fairness (reverted experiment)

Shares are PROPORTIONAL to each host's free slots: the roomiest character does the most work, so
every host finishes at roughly the same time and the order completes as early as its capacity
allows. That is also why one product shows different run counts on different characters, which is a
real cost paid every time the order is installed by hand.

An EVEN split was tried on 2026-08-08 to make those numbers identical, and reverted the same day:
handing a 2-slot character the same 250 runs as a 10-slot one put a single step at **14 days**. The
two goals genuinely conflict and finishing sooner wins. If it is revisited, the answer is not an
even split but a bound on how far apart hosts may FINISH — pick the hosts whose capacity is
comparable, split evenly among those, and leave the rest out of the order.

## One run count per product per stage (`level_stage_runs`)

Reported from use: *"for Carbon Fibers I see 125 runs, 90 runs, 75 runs — it's all over the
place."* Those are three separate assigns, each of which sized its own chain's Carbon Fiber
requirement exactly and correctly. Nothing was wrong with any one of them; what was wrong was
reading three numbers off the screen and typing three different values into three consecutive jobs
on one character.

`level_stage_runs` gives every job of one product, in one stage, on one character the same run
count. **It is sound because the product is fungible** — Carbon Fiber made for one chain is Carbon
Fiber, it lands in the same hangar, and the stage above draws from the pool rather than from a
particular job. Only the TOTAL for a (character, stage, product) has to hold, and how it splits
across that product's jobs is ours to choose. The total is preserved, or rounded UP where it
doesn't divide evenly (125/90/75 → 97/97/97), never down.

Conservative about everything else, deliberately: the row count is untouched (no slot claimed or
released), rows keep their own chain and order so `_shopping_roots`, `chain_stage_state` and the
per-order give-back all still see the plans they saw before, and it writes only when the numbers
actually differ. Runs on dashboard load beside `restage_plan_rows`.

**Superseded on the flag:** with `reactions_level_runs` on, `level_product_runs` (next section) runs
in its place and levels the same product across every character, re-splitting the jobs to do it.
This pass is what runs with the flag off, and its conservatism is the reason it can.

**What this does NOT do:** align END times across DIFFERENT products of an assembled plan. That
means moving jobs between products, and a job carries its chain — a chain that lost its rows for
one product would stop waiting on it and could announce "stage 2 is ready" while those jobs were
still running. `_align_stage_jobs` does it safely inside a single suggestion, where the whole chain
is in hand; doing it across assigns needs chain identity reworked first.

## One run count per product, across every character (`reactions_level_runs`)

Reported again from use, and the version of the complaint the section above does not answer:
Carbon Fiber at **125 runs on Chislen, 90 on Sajkisen414, 90 on Nuori, 75 on Ekaoni** — and the
same shape on every other product. *"Why haven't we tried doing 120 runs for all of them? We'll
save slots, align runtime and lower login cadence."*

`level_product_runs` picks **one run count per product** and re-splits the work into as many jobs
as that number needs. On the reported plan it lands on **125 everywhere**: the count the busiest
character was already using, so nothing gets slower, every job now ends at the same hour, and the
fifteen jobs become **twelve**. It replaces `level_stage_runs` on dashboard load when the flag is
on; off, that older within-a-character pass still runs.

**How the number is chosen.** Per stage, the options for each product are every run count that
divides some chain's requirement into a whole number of jobs, plus the round number just above each
(`_level_options`). An option is discarded if it needs more reactors than a character has free, if
it overshoots the product's total by more than **50%**, or if it hands any ONE chain more than
**3×** what it asked for. What is left is scored by `_choose_stage_layout`, searching over target
DURATIONS rather than run counts — which is what makes alignment expressible — in the user's stated
priority order (TODO 28):

1. does the stage **land together** (every job finishing within 10% of the last)? A layout that
   lands wins outright, whatever it overshoots;
2. the **slot count** — the same work in fewer, fuller jobs;
3. the **surplus**, and (with `reactions_tidy_runs` on) whether the number is one you can type.

**Surplus is spent to land a stage or take a reactor back, and for nothing else** — *"it's fine to
build a bit too much if it doesn't line up."* The two orderings that look more responsible are both
wrong: ranking goo above slots picked 75 runs in 19 jobs over 125 in 12 to save 75 runs of stock,
and scoring raw spread instead of a landed/not-landed flag bought a *little* alignment for a *lot*
of goo — pushing a product that cannot reach the stage's duration half way there and paying in full
for a stage that still doesn't land.

**The ceiling that keeps it honest:** with no job length set, no job may run longer than the longest
job already planned in that stage. Without it the cheapest answer in slots is always "one enormous
job per character" — on the reported plan, four jobs of 375 runs, three times the runtime, while
eleven reactors idle.

### Job length is the player's call (`pp_reaction_job_target`)

Everything above follows from the chains and the reactors. How long to leave a batch cooking does
not — *"maybe we should let the user decide the average length they want to do the reaction for."*
So there is one setting, `get_job_target`, in its own per-account table:

| mode | means | run counts | finish times |
| --- | --- | --- | --- |
| `days` | every job runs about this long | differ per product (a 6h reaction gets half the runs of a 3h one) | **land together** |
| `runs` | every job carries about this run count | the same everywhere | differ per product |
| `auto` | no opinion — the default | levelled per product | never longer than the stage's longest job already planned |

It is a **target, not a ceiling**: jobs grow to fill it, which is what lands a stage together and
keeps the number of logins down. The surplus budget and the 3×-per-chain ceiling still bound what
that costs, and a target the free reactors cannot reach simply doesn't produce options — the
product keeps what it has rather than being half-applied.

**`auto` is the default and that matters:** a plan built before this existed must not have its jobs
resized by a number nobody chose.

Deliberately NOT part of the group/account pricing settings, which a group manager sets for every
member — nobody's alliance should be choosing their login cadence. One setting, shown in two
places: the Reactions card and the wizard's "Run on a…" cadence, either of which writes it, because
a wizard sizing a weekly batch while the plan is levelled to a fortnight is two answers to one
question. A `runs` target has no exact hour window (the products differ), so the wizard states it
at the standard 3-hour reaction and says so.

**What it may not do.** A chain's intermediate is consumed by the job above it *on the same
character*, so a chain's requirement is a floor and work never moves between characters — only the
split changes. The **top row of every chain is untouched**: its run count is what the batch's cost,
output value and profit were computed from, and what a customer order gives back when cancelled.
And a chain never loses its last row of a product, because `chain_stage_state` reads readiness per
chain: a chain that stopped mentioning a product it is waiting on would announce the stage above as
ready while those jobs were still running.

That last rule is also the limit. Two separate chains on ONE character still get a job each rather
than sharing one, even though the output is fungible and lands in the same hangar. Sharing needs
chain identity reworked first (a real `chain_id`, so one row can belong to more than one chain).

**Why the budget is 50% and not `tidy_runs`' 15%.** Reported from use: Oxy-Organic Solvents at 35
runs on some characters and 18 on others while Carbon Fiber and Thermosetting Polymer levelled
fine. Small requirements are the case that breaks a rounding-sized budget — 35 and 18 have no
common count inside 15% at all (35 on both overshoots the small chain by a third; 18 on both needs
a second reactor the character may not have free), so the product was left alone, which quietly
ranked "cheap" above "one number" and inverted the priority list.

The per-chain **3×** ceiling is not implied by the total: a chain needing 2 runs beside one needing
10,000 can be handed 1,000 and barely move the total, which is 500× the work that chain wanted.
Below 10 runs a chain is exempt from the ratio, the same range `tidy_runs` already treats as free.

A product whose chains are too far apart even for that — 3 runs on one and 1000 on another — gets
no options at all and is left exactly as it was. `test_level_runs.py`.

**Committing a plan blocks the view (`_rxRunSteps`).** Levelling runs on the plan re-read, so the
run counts a player sees are only true once that read lands — an assign is a POST per suggestion
*and then* that read. All of it now runs behind a blocking overlay with one line per step, and the
steps run **one at a time**: each assign has to see the slots the one before it took, and firing
them together let two suggestions claim the same free reactor. A refused step keeps its server
reason on screen and the rest still run; a `critical` step (deleting the row an edit replaces)
stops the ones after it instead.

## Run counts you can type (`reactions_tidy_runs`)

A stage's run counts come out of the chain maths exactly — 79 of one thing, 41 of another, 213 of a
third — and every one of them is typed by hand into the industry window, one job at a time. Reading
an exact figure per job is the friction the plan exists to remove.

`tidy_runs()` rounds an INTERMEDIATE step's per-job run count UP to the next tidy number, taking the
largest step (1000/500/250/100/50/25/10/5/2) that lands within **15%** of the true requirement:
79 → 80, 41 → 45, 213 → 225, 137 → 150. Three rules make it safe:

* **Never down.** The stage above consumes this one's output; coming up short means it cannot run.
* **Never the end product.** Its run count is what the batch's cost, output value and profit were
  all computed from — moving it would make every one of those figures wrong.
* **Never below 10 runs.** "3 runs" is already easy, and rounding it to 5 is a 67% overshoot.

Applied at `_insert_assignment_rows` (the one choke point for what gets committed) and mirrored in
the wizard's own tier rows, so the preview and the plan show the same number. The surplus is stock,
not waste — and with `reactions_use_stock` on, the next plan spends it.

**The shopping list follows the plan, not the ideal.** `_explode_shopping_list` takes a `planned`
map of what each intermediate row will really produce and buys for that where it exceeds the bare
requirement, so a rounded-up job is a job the player can actually fill. It only ever raises the
figure: a plan holding LESS than the requirement is a short plan, not a cheaper shopping run.

## A stage is a DEPTH, not a position in a list

The bug this replaced, reported 2026-08-08 and worth stating plainly: every insert path stamped
`tier_order` with `enumerate(...)` over the chain-tier list. Reinforced Carbon Fiber's three inputs
— Carbon Fiber, Oxy-Organic Solvents, Thermosetting Polymer — are each **one reaction off raw goo
and fuel blocks**, with no dependency on each other at all, and they were stamped stages 0/1/2. The
dashboard then labelled them Stage 1/2/3 and greyed two out as "wait for the one above", and
`_concurrent_load` counted three genuinely simultaneous jobs as one reactor.

`_resolve_reachable` now carries **`depth`** on every node (leaves 0, otherwise `1 + max(input
depths)`) — deliberately NOT `reaction_count`, which is the size of the subtree and happens to
equal depth only for a straight chain. `tier_ranks()` turns an `_ordered_chain_tiers` result into
dense 0-based stages where **steps sharing a depth share a stage**, and every insert path
(`assign_reaction`, `adopt_orphan`, `_allocate_and_insert`) uses it. `assign_reaction` takes the
stage from the client's `ChainTier.tier` when present, re-derives it from the graph when not, and
only falls back to list position for a chain it cannot resolve at all.

Two consequences that matter beyond the label: the load of a stage is the **sum** of the steps in
it (three siblings are three reactors at once — the advisor and the assign guard both accumulate
per stage now), and the idle-slot pass naturally pours spare reactors into stage 1, which is what
the whole chain is waiting on.

**Existing rows are repaired, not left wrong.** `restage_plan_rows` re-derives `tier_order` from
the graph for any group whose stored stages disagree with it, runs on dashboard load, is idempotent
(a repaired account does no writes), and does nothing at all when the graph can't price the chain.

## Knowing when the next stage can start (`chain_stage_state`)

"Fill stage 1, then tell me when stage 2 can go" needs a completion signal, and ESI already has one:
an industry job reported `ready` (finished, uncollected) or `delivered` (collected), or whose
`end_date` has passed, is work that is over. A stage is DONE when every one of its rows has such a
job; a stage is READY when every stage below it in **its own chain** is done (chains grouped by the
assign that wrote them, so two plans on one character don't gate each other). Stage 1 is always
ready.

The dashboard renders that as a green "Stage N is ready to start" banner, un-greys the slots of a
ready stage, and relabels it "ready to start now" instead of "after stage 1 finishes". The
`end_date` check matters because the jobs cache is up to five minutes stale — "can I start yet"
should not wait on a refresh.

**...and it pushes.** `reaction_stage_ready` is a twelfth alert kind (`app/alerts.py`
`_stage_ready_alerts`), computed off the same `chain_stage_state` the page renders, so a
notification and the screen can never disagree. It flows through the existing engine for free:
per-account mute, min-severity, the PI Dashboard's own card (green — the only alert here that is an
opportunity rather than a problem) and Pushover/ntfy/Discord. Cooldown 12h, because a ready stage
STAYS ready until it is installed.

That required fixing the dedupe: `_process_context` only consulted the cooldown when an alert had a
`planet_id`, so every reaction kind — which has none — re-sent on all six schedulers' 15-minute
ticks. Alerts may now carry their own `dedupe_id` (per character for the two existing reaction
kinds, per character+chain+stage for this one), and only a genuinely keyless alert skips the check.

## Stages on the dashboard are `tier_order`, shown absolute

Every `pp_reaction_assignments` row carries `tier_order` — 0 is the deepest intermediate (react
first), the end product sits at `len(chain_tiers)` — and it has always been in the dashboard
payload and the `ORDER BY`. Until 2026-08-07 `static/reactions.js` never read it: the loadout drew
tier 0 and tier 1 side by side with nothing saying which had to finish first, which reads as "the
tool isn't sequencing" when it is (`_concurrent_load` already counts the worst tier, not the sum).

Planned slots and the "To install" checklist now group by stage via `_rxStageLabel`, dimming and
dashing anything past stage 1. The displayed number is **`tier_order + 1`, absolute** — not
re-ranked against whatever is still pending. When stage 1 is already running its plan rows are
gone from `pending`, and re-ranking would relabel the stage-2 rows "start now" while their input
is still cooking. A gap in the numbering is the honest reading: the missing stage is in the
filled squares.

**A queued later stage is ONE square with a `+N` badge.** A 10-slot character was drawing thirteen
squares — ten startable jobs plus a chain's queued stage 2 — and wrapping onto a second line, which
reads as "you assigned more than I have". Identical later-stage jobs (matching on product, stage,
run count and order) now fold into one square carrying `+N`: the same instruction, and how many more
times to repeat it once the stage below lands.

**Startable jobs are never folded**, deliberately. The first version folded every planned job and a
fully-committed character came out as two squares and a lot of nothing — the row IS the reactors,
and one square per job is what makes a full character look full. Only the jobs that are not holding
a reactor collapse.

Empty squares are counted against the **peak stage** rather than the row count, so a queued later
stage no longer hides free reactors; `free_slots / slots` on the row label stays the authoritative
number, and the "To install" checklist below still lists every job.

## Pricing: a sell-order price is not achievable profit

Reaction goods are repriced aggressively, so the sell-order price is not what you can actually
realise. Use **instant-sell (buy orders)** as the "what you can make" signal everywhere the user
reads a profit figure; never `sell_volume` or `net_profit_order`.

## Where the rest lives

- Module map for `app/reactions/` — [code-layout.md](code-layout.md).
- Which reactions an Industry build runs (`industry_reaction_policy`), and why a reaction formula
  caps concurrency like a blueprint copy — [industry-running.md](industry-running.md) and
  [industry-planning.md](industry-planning.md).
- The `reaction_completed` alert — [pi.md](pi.md), shared alert engine.
