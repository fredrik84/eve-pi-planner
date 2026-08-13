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
- **The headline numbers are valued from the PLAN, not from stored costs** — why committed ISK read 4.93m and profit/day read 0, and how an order's price is handled
- **Landing a stage in one go (`_align_stage_jobs`)** — why the spread matters more than the total, and what moves to close it
- **One request, one answer (`request_memo`)** — why an order report was rebuilding the same evidence five times
- **An order's runs follow capacity, not fairness (reverted experiment)** — why hosts get different run counts, and what the even split cost
- **An order stops at the character not worth a login (`reactions_pack_hosts`)** — the marginal-gain rule, why it needs no cadence, and two earlier attempts that did nothing
- **One run count per product per stage (`level_stage_runs`)** — why levelling across assigns is sound, and what it deliberately doesn't touch
- **One run count per product, across every character (`reactions_level_runs`)** — the cross-character leveller: how the number is chosen, why it also saves slots, and the one thing it still can't merge
- **The leveller has to respect the formula cap too** — why a re-split could ask for a job you cannot install
- **One run count for a whole STAGE, not just per product (`reactions_level_runs`)** — why matching durations was not enough, and why a shared count has to be a candidate
- **Run counts you can type (`reactions_tidy_runs`)** — bounded rounding of intermediate runs, and why the end product is never rounded
- **A stage is a DEPTH, not a position in a list** — why siblings share a stage, and how existing rows were repaired
- **Knowing when the next stage can start (`chain_stage_state`)** — the ESI signal behind "stage 2 is ready"
- **Stages on the dashboard are `tier_order`, shown absolute** — how chain order is rendered, and why the number is never re-ranked
- **Marking a reaction running or done by hand (`reactions_manual_done`)** — why a mark is a floor and not a replacement, and what it does to the slot count
- **The stage list is Manufacturing's pipeline (`reactions_stage_pipeline`)** — why the two tabs share a grid, and why rows are characters
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

**The list is computed PER ROW now, not by walking the chain (2026-08-10).** `_plan_materials`
replaced the root walk outright, and `_shopping_roots` no longer serves this path at all (it still
identifies what a cancelled order owes back — `give_back_order_runs`).

Why the walk had to go. It derived materials by recursing over the chain in aggregate UNITS, which
got three things wrong in sequence, each found only after the one before it was fixed:

1. **A per-root multiplication.** `planned` — how much of each intermediate the plan would really
   run — is an account-wide total and was applied as a floor *inside every root's walk*. Correct
   while one chain lived on one character; pooling made one chain several roots, and each bought the
   whole account's intermediates. Reported as **11 million Atmospheric Gases against a real 1.16m,
   and 244k fuel blocks against 24.6k**.
2. **Rounding per BATCH, not per job.** The game rounds materials once per job:
   `ceil(5 x 120 x 0.978) = 587` a job, 19 jobs = **11,153** fuel blocks. Aggregated,
   `ceil(5 x 2280 x 0.978)` = **11,150**. Three short is one job you cannot install.
3. **Stock deducted twice.** A holding shortens a chain when it is ASSIGNED
   (`_trim_tiers_by_stock`); subtracting it again at read time bought goo for 440 runs of a product
   whose 4 planned jobs were going to run 480 — reported as **78,240 Hydrocarbons short**, exactly
   40 runs' worth.

All three have one cause: **the plan rows already state the work, and re-deriving it from a recipe
can only disagree with them.** A row is one in-game job. So the list is now: for each row, its
formula's direct inputs at `ceil(quantity x runs x (1 - ME))`; an input another row produces is
skipped (the plan makes it); an input nothing in the plan produces is real unscheduled work and is
derived from its recipe, where a stock holding legitimately applies because nothing will be
installed for it.

The invariant, pinned in `test_shopping_roots.py` against the reported plan's six hand-computed
totals: **the list equals what the game asks for, job by job** — and neither where the jobs sit nor
what sits in the hangar changes it.

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

## The headline numbers are valued from the PLAN, not from stored costs

`ISK committed`, `Expected output value` and `Expected profit / day` used to be plain sums of each
row's `input_cost`/`reward`, written once at assign time. Both were wrong, and reported as such
(2026-08-10):

* **`ISK committed` read 4.93m against a plan holding ~590m of materials.** Chain-tier rows are
  stored at zero cost (three insert sites do it deliberately — the chain's cost used to roll up into
  its one top row), and pooling made intermediates the bulk of the rows. So almost the entire plan
  was counted as free.
* **`Expected profit / day` read 0.** A customer order's top row is stored at zero *reward* on
  purpose, because an order's revenue is what the client agreed to pay and nothing here can derive
  it. A plan of orders plus their intermediates therefore had no row carrying profit at all.

`_plan_totals` replaces both sums. **Committed is the plan's own materials at today's unit cost** —
literally the shopping list, priced (`_plan_materials`), which is the ISK you actually have to
spend. **Output value counts only END products**, the rows nothing else in the plan consumes
(`_plan_intermediates`, read from the recipes rather than from whether a cost happens to be zero —
that proxy is what mislabelled an order's top row). **Profit per day divides by the plan's
makespan**, the sum over stages of the longest job in each, because stages run in sequence.

### An order needs a price, and without one it is UNKNOWN — never zero

`pp_reaction_orders.client_price` is the one figure the tool cannot work out, so it is the one
number the user types (CLAUDE.md rule 3 allows exactly this: a knob only where the math genuinely
cannot decide). Optional, and NULL means *not told*.

A priced order is valued at the agreed price, apportioned by how much of it is assigned so far, so
a half-assigned order books half its invoice. **An unpriced one is valued at MARKET instead** — a
stand-in, not the invoice, but the honest floor ("if the client fell through you could sell these")
and far better than the alternative. Reported first as excluded-from-profit and then as a hard zero,
both of which produced the same complaint: an order that was the only thing occupying every reactor
showed an expected value and a profit per day of **0**. `unpriced_orders` now means *part of this is
a market estimate*, which is what the dashboard says.

**The price is editable after the fact** (`POST /api/reactions/orders/{id}/price`), which the first
cut got wrong by shipping it as a create-form field only: every order made before it, and every
order where the number is agreed after the work is planned — the normal way round — could never be
given one, so its revenue stayed unknown forever.

The same block drives the order's own **profit panel**, shown both in the review step before the
order is created (the preview endpoint takes the typed price) and in the order detail afterwards:
what the client pays, what it costs to produce, the profit, the margin on the price, and a warning
when the order loses money.

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

## An order stops at the character not worth a login (`reactions_pack_hosts`)

Reported from a live order (#45, 1000 runs of Reinforced Carbon Fiber): stage 1 spread over 5
characters with two holding ONE job each, stage 2 over **seven** with five holding one each — while
the characters that already had jobs sat on free reactors. *"To lessen logins we should try and run
as lean as possible... it's fully possible to not spread the Stage 2 work over all characters."*

**The rule is marginal gain, and it deliberately needs no cadence.** An order's wait is its
reactor-hours divided by the reactors running them, so a host with `F` free slots added to the `S`
already committed cuts that wait by `F / (S + F)`. `_lean_hosts` takes hosts roomiest-first while
that is worth a login (`_WORTH_A_LOGIN`, 20%) and stops at the first one that isn't. It is the same
reasoning `_fit_chain_slots` already uses to hand slots to tiers, one level up.

On the reported account — three 10-slot characters, four 5-slot ones — the 4th buys 5/35 = 14% and
the order lands on three:

| chars | reactors | order #45 |
| --- | --- | --- |
| 7 (before) | 33 | ~12.3 days |
| **3** | **30** | **~13.5 days** |
| 1 | 10 | ~41 days |

**Four fewer logins each way for ~1.2 days of a 12-day order.** Note the honest comparison: the
sprawl over seven was only using 33 of the account's 50 reactors, because the small characters have
five each — so packing gives up far less speed than the character count suggests.

**Why a relative gain rather than a job-length ceiling.** A duration ceiling needs a number nobody
has set (`max_reaction_job_days` is `None` by default, deliberately — see TODO 28) and it answers
the wrong question: "is this job too long" instead of "is this character worth the trip". A relative
gain needs no unit at all and scales itself — a small order lands on ONE character because the
second buys nothing, a large one still spreads because every host pulls real weight.

**It also serves "don't buy more formulas".** Every host runs the whole chain and so needs one
formula of every tier for itself (`formula_concurrency_caps`); fewer hosts is strictly fewer
formulas the order demands at once.

**The LEVELLER has to obey this too, or the two passes undo each other.** `_lean_hosts` lived only
in `_allocate_and_insert`, so an order was packed onto three characters at assign time and then
`level_product_runs` — which re-splits the whole plan on every dashboard load — saw a spare reactor
on a fourth and put a single job there. Reported while watching the page: *"it did right... but when
I was looking at it suddenly swapped the 3x7 slots to 3x7 slots + 1x1 slot."*

Step 5b now applies the same test when it places overflow: a character **already in the plan** is a
login you are making anyway, so its reactors cost nothing extra; a character outside it joins only
if its room is worth `_WORTH_A_LOGIN` of what the plan can already reach. When the involved
characters are full that share is 1.0 and it joins immediately — which is the case where spreading
is genuinely necessary rather than merely available.

**Two earlier attempts are recorded because both looked right and did nothing.** The first packed
hosts until their free slots covered `_useful_slots` — the theoretical most an order could ever use,
`sum(runs per tier)`, in the thousands for any real order — so every host always cleared it and the
feature was a no-op on the very order that prompted it. The second required a fixed floor of free
slots per host, which removed the one-job tail but still had no answer for "these three characters
could hold the whole thing". Both asked a question about the ORDER; the question is about the
CHARACTER.

**This is not the reverted even split** (see "An order's runs follow capacity, not fairness"). That
one changed the JOBS, handing a 2-slot character the same 250 runs as a 10-slot one. Here the split
across the hosts that remain is untouched and still proportional to their reactors.

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
divides the ACCOUNT's requirement for it into a whole number of jobs, plus the round number just
above each (`_level_options`). An option is discarded if it needs more reactors than the stage has,
or if it overshoots that requirement by more than **50%**. What is left is scored by
`_choose_stage_layout`, searching over target
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

**The ceiling that keeps it honest:** no job may run longer than the longest
job already planned in that stage. Without it the cheapest answer in slots is always "one enormous
job per character" — on the reported plan, four jobs of 375 runs, three times the runtime, while
eleven reactors idle.

### Job length was a setting, and is not one any more (removed 2026-08-10)

There used to be one preference here, `pp_reaction_job_target`: `auto`, `days` per job or `runs` per
job, stored per account and shown both on the Reactions card and as the wizard's cadence. It is
**gone**, and the automatic rule above is the only rule.

Removed on the user's own report: *"the job length dropdown is not useful for me at all, it barely
works and I need to set it to automatic. If I set it to 7 days it doesn't align to 7 days."* And
that is the honest reading of what it could do — a target only produces options the free reactors
can actually reach, so on a busy account most of what was asked for came back as something else,
with nothing on screen explaining the gap. A knob that silently does not do what it says is worse
than no knob (CLAUDE.md rule 3: the best UI is read-only). On the reported plan automatic lands on
120 runs with a couple of stragglers at 110, which is what the 7-day setting had been reaching for
anyway.

If a job length is ever wanted again, the missing piece is not the setting — it is telling the
player what the reactors can actually deliver, and why the number they typed was not it.

**A product is POOLED across characters** (2026-08-08). This package used to hold that an
intermediate must be reacted by the character that consumes it — no model of moving half-finished
goods between hangars — so each chain's requirement was a floor of its own. That is not how the
account works: *"we do not need to use the same character to build the entire chain."* The output
goes to a shared hangar, and the old assumption was expensive — eight characters each running a
35-run Oxy-Organic Solvents job is eight reactors doing what two would. So the requirement is the
ACCOUNT's, the run count is the product's, and which character holds a job is just where there was
room. On the reported plan that is 8 Oxy jobs down to 2, and 23 Carbon Fiber jobs down to 18.

Two things depended on the old assumption and moved with it:

* **`_shopping_roots`** takes the top tier of the CHAIN, not of one character's share of it. A
  pooled intermediate can sit on a character holding nothing else of its chain, where it would look
  like a chain of its own and have its goo bought a second time on top of the real top row's walk.
  Still grouped per character as well, because an order writes one timestamp across every host it
  uses and each host's top row is a real root.
* **`_gate_stages_account_wide`** holds a stage back until every stage below it is finished across
  the whole account. `chain_stage_state` answers that per chain, which was the whole truth while a
  chain held all its own stages; now a chain can hold no row for a stage it waits on, and "every
  step of MY chain below this is done" would be vacuously true the moment the plan was made.

**What it may not do.** A row committed to a **customer order** is never re-shaped: its run count is
the batch that order was quoted on, and cancelling hands exactly those runs back
(`give_back_order_runs`).

**A speculative chain's TOP row is levelled too** — it was excluded until 2026-08-08, and that is
what left a product showing three numbers after the pass had run. The same product is an
intermediate under one chain and a standalone job to sell on the next character, and the player
types both; levelling one and not the other read the row's position in a chain as if it were a
property of the product. Its `input_cost`/`reward` scale with the runs (both are linear in them),
so the plan's cost and profit stay true.

**The plan must fit the reactors, counting every row.** The budget for a stage is the character's
reactors minus the jobs really running in game, minus the rows it holds in its OTHER stages —
deliberately NOT `free_slots`, which nets pending rows off by their busiest tier. That number
answers "what can I start now"; this one answers "how many rows may the plan hold", and using the
first let this pass grow stage 1 into reactors stage 2 already had a row in ("12 slots assigned to
characters that only have 10"). A row is a line in the plan whether or not it can be installed yet.

Where a stage cannot fit, the greediest product is pushed onto a longer run count and the stage is
solved again — and the loop is **seeded with the shortest duration the tightest character can
actually run** (all its work ÷ the reactors it may use). Stepping up one run at a time from the
asked-for length, as the first version did, never got from "5 days" to what the reactors could do
and left 20 jobs on an 11-slot character.

**Three ways it used to quietly do nothing**, all fixed the same day and all worth not
reintroducing:

* a product with no affordable common count was left alone entirely. Now it falls back to the
  smallest count its work can be split into with the reactors there are — that count always exists,
  which is what makes one number per product a property of this pass rather than something it
  manages when the arithmetic is kind;
* every product in a stage was sized against the character's WHOLE free-slot pool, so two products
  each promised the same four reactors and the plan asked for eight — resolved by dropping one
  product from the pass, which left it showing its old numbers. **A stage is now solved as a
  whole:** size it, look at what each character was promised, and where that exceeds what it has,
  force the greediest product onto a longer run count (fewer jobs) and solve again. Run counts only
  rise, so it settles. Splitting the pool evenly instead (`free // products`) was tried and was
  worse — it handed each product one reactor when one of them needed four, which is what made a
  5-day target come out as 94-run jobs;
* and the per-product pick inside the stage search took the LARGEST count that fit, paying eight
  runs of goo for 8 jobs of 36 where 8 jobs of 35 was the same layout.

That last rule is also the limit. Two separate chains on ONE character still get a job each rather
than sharing one, even though the output is fungible and lands in the same hangar. Sharing needs
chain identity reworked first (a real `chain_id`, so one row can belong to more than one chain).

**Why the budget is 50% and not `tidy_runs`' 15%.** Reported from use: Oxy-Organic Solvents at 35
runs on some characters and 18 on others while Carbon Fiber and Thermosetting Polymer levelled
fine. Small requirements are the case that breaks a rounding-sized budget — 35 and 18 have no
common count inside 15% at all (35 on both overshoots the small chain by a third; 18 on both needs
a second reactor the character may not have free), so the product was left alone, which quietly
ranked "cheap" above "one number" and inverted the priority list.

**A second, per-CHAIN ceiling was retired on 2026-08-09** — 3× what any one chain asked for, on top
of the 50% total. It was there because a chain needing 2 runs beside one needing 10,000 could be
handed 1,000 and barely move the total. Pooling made it unreachable: the search sees ONE requirement
per product per stage, so "any one chain" and "the whole product" became the same quantity and the
per-chain rule reduced to the total budget it sat beside. It was carried for a day as dead
plumbing — list-shaped `totals`/`caps`/`per_group` arguments with exactly one element in them — and
the whole of it went with the ceiling. `test_level_runs.py`.

**Committing a plan blocks the view (`_rxRunSteps`).** Levelling runs on the plan re-read, so the
run counts a player sees are only true once that read lands — an assign is a POST per suggestion
*and then* that read. All of it now runs behind a blocking overlay with one line per step, and the
steps run **one at a time**: each assign has to see the slots the one before it took, and firing
them together let two suggestions claim the same free reactor. A refused step keeps its server
reason on screen and the rest still run; a `critical` step (deleting the row an edit replaces)
stops the ones after it instead.

## The leveller has to respect the formula cap too (2026-08-13)

Reported: *"it also suggested to do 21 slots of Carbon Fiber... but I only have 20 formulas"* — and
21 Thermosetting Polymer against 20 of those.

Every ASSIGN path applied `formula_concurrency_caps` — the wizard bin-pack, the manual assign, the
customer-order allocation, the quoted estimate. **`level_product_runs` did not**, and it re-splits
the whole plan on every dashboard load against the SLOTS a character has. So a plan that was legal
the moment it was placed came back, minutes later, asking for a job the account physically cannot
install. The cap now bounds the per-product job count in the stage solve
(`min(stage_room, fcaps[tid])`), and unknown still caps nothing — the same rule every other consumer
of that evidence follows.

**A tighter cadence makes it bite harder, which is how it surfaced.** A 7-day ceiling at 3 h/run
caps a job at 56 runs, so 1045 runs of Carbon Fiber needs 19-21 jobs instead of 10 — and a product
that used to sit comfortably under its formula count now runs straight past it. The bug predates the
cadence; the cadence is what made it reachable.

## One run count for a whole STAGE, not just per product (`reactions_level_runs`)

Reported: *"I don't want to have to look for Carbon Fibers for each slot every time I start it to
figure out how many runs. The more similar number of job runs (preferably equal) between products
the better. Makes it faster to start and less taxing."*

`level_product_runs` already gave every job of ONE product the same count. Across products in a
stage it did not, and `_ALIGN_TOL` was never going to close that: it lands a stage by matching
DURATIONS, and with every product on the same cycle time 95 runs and 100 runs finish 5% apart —
comfortably inside the tolerance, so the stage already counted as landed and nothing was trying to
close the last gap. 95 then won on surplus, because Carbon Fiber's 1045 divides by it exactly.

Two changes in `_choose_stage_layout`:

* **`numbers` is scored** — how many DIFFERENT run counts the stage asks you to type — ranked after
  slots and before surplus. Landing a stage is about when its jobs finish; this is about what you
  read off the screen while starting them, and they are not the same question.
* **A shared count is offered as a candidate layout.** This is the load-bearing half. Each product's
  own pick takes the cheapest count that lands by the target, and cheapest means least surplus, so a
  shared number never falls out on its own however it is scored afterwards. The stage can only
  settle on one number if "one number" is a thing being proposed.

On the reported stage (Carbon Fiber 1045, Thermosetting Polymer 1100, Oxy-Organic Solvents 100, all
3.00 h/run) it moves 95/100/100 in 23 jobs to **110/110/100 in 21 jobs**. A product that genuinely
cannot reach the shared count keeps its own — one number is a preference, not a rule — and
`_stage_affordable` plus `_level_options`' budget still bound the goo any of it spends.

**Landing under a whole day, not just past one.** Reported as a small matter and treated as one:
a job of 7d07h is worse than one of 6d23h, because the player comes back on a whole-day rhythm,
finds it unfinished, and slips a few more hours every cycle. `_cadence_drift` scores a duration 0
when it lands on or within `_CADENCE_GRACE` (3h) under a day boundary and 1 otherwise, ranked after
`numbers` and before the surplus — a usability property like the shared count is.

It needs `_seed_cadence_counts` to fire at all. A product only ever proposes `ceil(total/j)` and the
tidy rounding above it, so Carbon Fiber offers 105 and 110 and nothing between; at 1.53 h/run that is
7d00h18m with no 6d23h alternative in the set. The seeder adds, for each candidate, the largest count
that costs the SAME jobs and still lands under a boundary — searching down only as far as
`ceil(total/j)`, so coverage never drops.

**It is ranked below the job count, so it will not always fire, and that is deliberate.** On the
reported stage it cannot: Thermosetting Polymer needs 1100 runs over 10 jobs, `ceil(1100/10)` is
exactly 110, and 109 × 10 = 1090 does not cover it — so landing under 7 days there costs an
eleventh job. A reactor is worth more than 18 minutes of drift.

**The cadence is a real ceiling, and it is the setting you already had.** Reported: *"I'd prefer to
be able to schedule my jobs on a Saturday and handle the next stage a week later on a Saturday."*
`max_reaction_job_days` (Build rules → "Longest reaction job", behind `industry_job_length_policy`)
has existed since the Industry scheduler shipped and the Industry side reads it — **the Reactions
stage solve never did.** It capped a stage at `stage_cap_hours`, the longest job the plan happened
to already run, which is why a Reactions plan could quote a fortnight on one reactor with the
ceiling sitting right there unused.

**The ceiling is measured in REAL hours, and that was the bug that made it useless.**
`_reaction_cycle_times` returns the raw SDE cycle and documents that the structure/skill bonus is
deliberately not applied, *"it scales every reaction by the same factor, and everything here compares
durations against each other"*. That was sound until the cadence arrived: a common factor cancels out
of a comparison between two durations and does **not** cancel out of a comparison with seven days.
Reported in game: one run of Carbon Fiber is 1h24m14s against the SDE's 3h, so the ceiling allowed 56
runs a job where the truth is 119, and the job count came out roughly double.

`_reaction_time_mult` supplies the factor and `stage_cap_hours` becomes `cadence / mult`, so the
existing raw-hours math is untouched. Effect on the reported stage:

| | ceiling | Carbon Fiber | stage 1 jobs |
| --- | --- | --- | --- |
| before | 56 runs | 55 × 19 | 41 |
| **after** | **119 runs** | **117 × 9** | **20** |

**Measured off real jobs, not derived from settings.** ESI gives every job a `duration` and a run
count, so the ratio is an observation — 0.4680 across 13 jobs on the reported account, matching the
player's hand measurement to four decimals. Deriving it was tried first and is wrong:
`struct_time_pct` is the INDUSTRY facility's number (62% there) and reactions run in a different
structure (44.9%), which would have allowed 173-run jobs — **10 real days against a 7-day ceiling.**
A measurement cannot drift away from the structure the player actually reacts in.

**And the measurement is REMEMBERED, which the first cut got wrong.** Reactors are idle exactly when
a player re-plans, so reading only live jobs meant the ceiling flipped between 119 and 65 runs a job
depending on whether anything happened to be cooking — reported as *"I set the cadence to 7 days and
now it did 65 runs per"*, which is `168 / 0.85 / 3.00`, the skills fallback. The last real
measurement is persisted (`pp_industry_settings.reaction_time_mult`) and reused until a newer one
replaces it, so an idle account still plans at the rate it actually reacts at.

**A first suggestion is right without ever having reacted (`route_job`).** Measurement cannot help
a new account — *"if they start jobs from the suggestion it'll be either wrong, or they have done all
the work manually"* — so with nothing measured the bonus is ROUTED:
`reaction_time_mult_for(context_id, type_id)` asks `structures.route_job` the same question the rest
of the app asks when it costs and times a job, and takes its `time_mult`. Per PRODUCT, because
`bonus_for` resolves the rig against the produced type's group — a Composite rig does nothing for a
job it does not cover, so one account-wide number would be wrong for everything outside the rig's
families.

That distinction is the whole reason the two earlier attempts failed, both by over-claiming:
`struct_time_pct` is the MANUFACTURING facility's (62% where reactions get 44.9%), and
`rx_bonus.te` is the structure's best case over any rig it carries (67% on a Tatara whose Carbon
Fiber jobs get 44.9%) — that one would have allowed 199-run jobs, 11.6 real days against a 7-day
ceiling. Routing cannot drift that way because it is the same call, the same sites and the same
product the planner already uses.

Measurement still wins where it exists: it is the only source that cannot be wrong about the
structure the player really reacts in. Routing answers the account that has not reacted yet.

With no structures configured either, it falls back to the skills multiplier alone, which under-claims the bonus on
purpose: a smaller claimed bonus means a bigger apparent job, fewer runs allowed, and a job that
lands inside the ceiling. Over-claiming is the direction that breaks the promise the cadence makes.

**The cadence reaches the order's own product too (`split_order_tops_to_cadence`).** Reported on a
7-day setting: stage 1 obediently came down to 6.88-day jobs while stage 2 sat at **14 days**, so the
whole order still took three weeks and the cadence bought nothing. `level_product_runs` deliberately
never reshapes a customer order's top row — its run count is the batch the order was quoted on, and
cancelling hands exactly those runs back (`give_back_order_runs`) — so the cadence stopped one stage
short of the thing being sold.

It is split separately, and **the total is preserved exactly**, which is what makes it safe: the
batch goes into the fewest jobs that each fit the window, with the remainder riding on one of them
rather than rounded up across all of them — *"I'd rather we underfill 1 slot to line up the others"*.
1001 runs over a 119-run ceiling becomes 2 jobs of 112 and 7 of 111, summing to 1001, with the input
cost split by the same proportions. Same product, same character, same chain timestamp, same total:
nothing the order's arithmetic reads has changed, only the row COUNT, and every consumer of that
counts rows rather than assuming one. Held to the formulas owned like everything else.

**One number, two places.** The control sits on the Reactions card as a single row above the
pipeline it shapes (`_rxCadenceHtml`, "Come back every N days") and reads/writes the same
`/api/industry/build-setup` that Build rules does — so whichever you touch last is what both show,
and there is no second setting to drift. It hides itself when `available.job_length` is false rather
than showing a control that 403s, which is the contract that surface already has for every section.
Changing it re-fetches the plan instead of re-rendering the cached one: the run counts on screen are
exactly what just changed.

`_reaction_cadence_hours` feeds that cap, so `_level_options` drops any run count above it. It
is HARD, not advisory: a stage that cannot fit the window at its leanest layout is split finer until
it does. On the reported stage at 1.53 h/run:

| cadence | layout | longest job | jobs |
| --- | --- | --- | --- |
| none / 14d | 110 / 110 / 100 | 7.01 d | 21 |
| **7 d** | **105 / 100 / 100** | **6.69 d** | **22** |
| 3.5 d | 53 / 53 / 50 | 3.38 d | 43 |

Fitting the week costs one extra reactor here, and a half-week cadence costs twenty-one. That is the
trade, and it is the right way round: **a cadence you cannot rely on is not a cadence.** Unset means
0, which is the old behaviour exactly — a plan built before anyone chose a cadence must not be
resized by one.

**What it costs, stated plainly: time.** 110 runs is a longer job than 95, so the stage lands about
two days later on a twelve-day order. That is the stated priority order — *"I want it to be easy"*
above *"I want to be time efficient"* — and it is the trade being made, not a free win.

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

## Marking a reaction running or done by hand (`reactions_manual_done`)

Reported from use: *"we can't mark reaction as done manually. We should have the same functionality
as manufacturing."*

ESI is the signal for what is running and what has landed, and it is right nearly always. The
exceptions are the ones that strand a player: the job cache is up to five minutes stale, a job
installed under a different product than planned matches no plan row, and a chain reacted before
this tool ever saw it has no job to read at all. In every one of those the dashboard says *"after
stage 1 finishes"* about a stage that finished an hour ago, and there was no way to say otherwise.

So: the same three states, the same click cycle and the same wording as an Industry build step
(`app/industry/progress.py`) — not started → installed → done → not started, one write per click,
wrapping round so a misclick costs a click and never data.

**A mark is a FLOOR under what was observed, never a replacement for it.** `chain_stage_state`
takes the higher of the ESI count and the hand count per (character, product, stage), exactly as
`resolve_done` does on the Industry side. A tick can therefore bring a stage forward; it can never
hide a job ESI can actually see. A `running` mark does *not* release the stage above — running is
not finished, and gating on it would be the one way this feature could make a plan wrong.

**Keyed on (character, product, stage), deliberately not on the row id.** `level_product_runs`
re-splits a product's work and DELETES and re-inserts rows to do it, so a mark keyed on a row id
would silently detach from the job it was about. The triple is what the dashboard groups by, what
the pipeline draws a card for, and what the player is pointing at. `_RX_ALL` (-1) means "however
many jobs the plan holds today", which is what keeps a whole-group mark meaningful across a
re-split; a partial mark stores a count and is capped at what the plan currently holds.

**What a mark does to the slot count, and why it differs by state.** A job marked DONE has given
its reactor back — counting it would idle a slot the character really has — so it drops out of
`pending_load` and out of the loadout's squares. One marked RUNNING is the opposite: it is cooking
right now and ESI simply cannot see it yet, so it holds its slot exactly as an ESI-reported job
would, and its square turns green instead of red. Getting these two the wrong way round is the one
mistake here that would cost the player real capacity. The frontend filters the squares on the same
rule the backend uses for `free_slots`, so the row count and the slot count cannot contradict each
other on one screen.

**A marked row stays in `pending`**, carrying its mark, rather than vanishing — a row that
disappeared when ticked would be a mark you could set and never clear. The checklist skips marked
rows because it is about work left to do; the pipeline draws them because it is about state.

Like the Industry equivalent, `/api/reactions/mark` writes nothing to the completion ledgers: those
feed lifetime turnover and profit, and a tick is a statement about this plan's progress, not
evidence of an ISK-bearing job that really ran.

## The stage list is Manufacturing's pipeline (`reactions_stage_pipeline`)

Reported from use: *"the list of stages can be condensed to the same way we do build pipeline in
manufacturing. So we have a consistent way of displaying data."*

The "To install" table grouped its rows under stage BANNERS — a stage was a horizontal rule in a
list. Industry draws the identical idea as a grid: columns are stages, rows are where the work
physically happens, cells are the steps. Two pictures of one concept, on two tabs of one tool.

`_rxPipelineHtml` renders the same `todoGroups` rows into that grid, **reusing Manufacturing's own
`.ind-pipe*` classes rather than cloning them into `rx-` lookalikes.** The stylesheet is what stops
the two drifting apart again, which is the actual complaint — a private copy would look consistent
on the day it shipped and not six months later.

**Rows are characters, and that is not an arbitrary mapping.** Manufacturing's rows are buildings
because a build happens in one. A reaction chain's intermediate has to be in the hangar of the
character reacting the thing above it (`_allocate_and_insert` — a chain never splits across
characters), so the character IS the place the work happens, and a row is exactly one login's worth
of it. Columns carry the stage's total job count in the header; a cell carries the product, its
total runs, how many jobs to install, and — for anything past stage 1 — whether its inputs have
landed (`chain_stage_state`) or it is still waiting on the stage below.

It REPLACES the table rather than sitting beside it. Two readings of one list is the inconsistency
being removed, so shipping both would have been the bug.

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
