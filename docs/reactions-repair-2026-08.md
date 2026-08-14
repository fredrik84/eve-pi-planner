# eve-pi-planner — Reactions repair spec (2026-08-14)

What a three-reviewer audit of the Reactions service found, what to do about it, and how the work
splits across implementing agents. Back to [CLAUDE.md](../CLAUDE.md). Backlog entries:
[TODO.md](../TODO.md) §29, §30, §31.

Find a section: `grep -n '^## ' docs/reactions-repair-2026-08.md` and read from that line.

## Contents

- **The finding behind all of it** — why this code is hard to reason about
- **Decisions already taken** — the four the user settled, and what each rules out
- **Still open** — three questions with a recommended default, so nothing blocks
- **WS1 — Profit truth and the clock** — the pricing rule, and the two duration models
- **WS2 — The cadence ceiling, and what it costs** — the soft ceiling, and surfacing surplus
- **WS3 — Reactions standalone, and the rollout** — un-gating the service from Industry
- **How the work splits** — file ownership per agent, and the collisions to avoid
- **Flags, tests and the verification gate** — what ships gated, what must be pinned

---

## The finding behind all of it

**No two Reactions surfaces agree about the same plan.** That is the audit's convergent conclusion
and it is the answer to "we've had so much trouble reasoning about this code". Five instances, each
independently verified:

| Surface A | Surface B | They disagree about |
| --- | --- | --- |
| Suggest wizard | Dashboard | ISK committed (`tidy_runs` applied after totals) |
| Order confirmation | Dashboard | the order's own job layout (cadence split runs a page later) |
| The cadence the user set | The plan returned | whether the ceiling binds at all |
| The measured job duration | The quoted job duration | which of two clocks was read |
| One user's Reactions tab | Another's | thirteen feature flags, none public |

Underneath four of those five is one mechanical cause worth stating on its own, because it is the
trap a reader falls into: **`jobs.py` carries two different floats both called "cycle hours"**.
`_reaction_cycle_times` (`jobs.py:1298`) returns **raw SDE** hours and says so deliberately, so the
leveller works in raw-SDE space and converts the *cadence* into it (`jobs.py:1510`). Everything
downstream of the graph works in time-efficiency-reduced hours (`graph.py:58`), and `jobs.py:2550`
re-derives the reduced form by hand because it bypasses the graph. Which unit you get is decided by
which function you entered through, and nothing in the type system tracks it.

Fixing the units is not a workstream of its own — it is a constraint on WS1 and WS2, both of which
must state, in a comment at every conversion, which space they are in.

---

## Decisions already taken

Settled by the user on 2026-08-14. Each closes a branch; do not re-litigate them.

1. **The cadence is a stated target, not an absolute.** When a stage cannot fit the window at its
   leanest layout, the plan may exceed it — but it must **say so on the row**. This is the cheap
   branch: it makes WS2 mostly a truth-telling change rather than a solver rewrite. It also makes
   `docs/reactions.md:742` and the docstring at `jobs.py:1502-1504` — both of which currently assert
   a HARD ceiling — wrong on policy as well as wrong on fact. Correct both.
2. **The rhythm is a duration, not a calendar.** "Nothing longer than N days." No weekday, no
   time-of-day, no timezone. This closes the "your next session is Saturday" design entirely, and it
   means `_collection_slot`'s 24-hour bucket (`jobs.py:975`) is modelling a rhythm the user does not
   have — see WS2.
3. **Report what ease costs, and suggest a remedy.** Not just a number: the line says what was spent
   and what could be done about it. The `marginal_saving` pattern from `app/industry/*`, adapted.
4. **Reactions becomes genuinely standalone.** It owns its own cadence setting and its own paste
   route; Industry reads the same stored value. This is the manifesto's named gap
   (`docs/manifesto.md:126-129`) being closed rather than re-documented.

**Documentation is part of the work, not a follow-up.** Decision 1 makes `docs/reactions.md:742` and
the docstring at `jobs.py:1502-1504` wrong on policy as well as on fact; decision 4 makes
`docs/manifesto.md:126-129` describe a closed gap. **WS2 owns the two cadence corrections; WS3 owns
the manifesto paragraph.** A workstream is not done while a doc it invalidated still says the
opposite — that divergence is how this code became unreasonable-about in the first place.

## Still open

Three questions the audit raised that the user has not answered. Each has a recommended default so
implementation is never blocked; take the default and note it in the commit, or ask first if the
answer would change the shape of the work.

**a. Does `ISK committed` charge job install fees, shipping and collateral?** `_value_reaction_batch`
charges all three (`graph.py:229-235`); `_plan_totals` charges none (`jobs.py:2402`). Two readings:
the tile is "what I must go and buy" (materials only, correct as computed, wrongly labelled), or it
is a cost base for profit (must charge everything).
*Recommended:* **both, split.** Relabel the existing tile **"Materials committed"** — it is literally
the priced shopping list and that is a useful number — and derive profit from a separate full cost
that nets job fees, freight and collateral. Do not derive profit from a materials-only figure under
any label.

**b. Do `net_profit_order` and `sell_volume` stay on screen?** The opportunity fold-out renders three
sell-side fields: `Sell order value` (`static/reactions.js:29`), "Profit (order)" (`:30`) and "Sell
depth" (`:33`). The pricing rule says "never" about the *achievable-profit signal*; these are
labelled as something else.
*Recommended:* **keep, relabel explicitly** — "Profit if you sell to orders (patience required)" and
leave "Sell depth" as market context. Add a comment at the render site citing this decision so the
next audit does not re-flag it. **Owned by WS1** (see the split table) — it is the same pricing pass.

**c. Should a customer order be paced at all?** `_allocate_and_insert` argues explicitly that an
order runs flat out because a customer is waiting (`jobs.py:3123-3129`); `split_order_tops_to_cadence`
paces it a page load later (`jobs.py:1747`). Both are current, both documented as deliberate, and
they disagree about the same rows.
*Recommended:* **pace it, at quote time.** Decision 1 makes pacing non-destructive (it can overrun
and say so), and the real defect is that the two surfaces disagree — which is fixed by moving the
split into the quote, not by removing it. Update the `_allocate_and_insert` docstring
(`jobs.py:3123-3129`), which will then be stating a policy the code no longer follows.
**Owned by WS2** — it is the same cadence pass, and `_allocate_and_insert` (`jobs.py:3112+`) is
WS2's alone.

---

## WS1 — Profit truth and the clock

**These two ship together. Not optional, and the reason is arithmetic:** the sell-price error
overstates profit ~3.3× (an estimate on a plausible plan — see 1a — not a figure derived from stored
data; source it before quoting it as measured), and the 0%-default clock understates speed **~2.14×**
(measured multiplier 0.4680, `docs/reactions.md:669`; `_reaction_time_mult`'s own docstring calls the
graph clock "2.1x slow"). So profit-per-day is **~1.5×** overstated, the two errors opposing.

Do not confuse 2.14× with the **1.81×** ratio that also appears in this area: that is 119/65.9, the
*leveller's* measured ceiling against its skills-fallback ceiling (`168/0.85/3.00`,
`docs/reactions.md:678`) — a different clock and a different baseline.

**After repricing but before the clock fix, the displayed profit-per-day falls by ~3.3× rather than
the ~1.5× the finished change should produce.** The error at that moment is not 3.3× — it is the
clock error alone, ~2.14× understated. A verifier seeing the number move the wrong way mid-change
must know this is the expected intermediate state.

### 1a. The pricing rule holds only in the advisor

`CLAUDE.md:184` and `docs/reactions.md:958-962` state it as an invariant: use instant-sell (buy
orders) as the achievable-profit signal **everywhere the user reads a profit figure**. Three
violations, none of them flag-gated — they are live for every user today:

| Site | What it does | Renders as |
| --- | --- | --- |
| `jobs.py:2395` (`_plan_totals`) | `output_value += runs * oq * m["sell_price"]` | "Expected output value" tile (`reactions.js:1113-1115`) |
| `graph.py:620-622` (`reactions_job_detail`, `GET /api/reactions/job-detail`, def at `graph.py:581`) | returns `output_value`/`net_profit` at sell; computes `instant_value` the frontend never reads | running-job modal + an ROI derived from it (`reactions.js:2838-2868`) |
| `jobs.py:355` (`log_reaction_completions`) | `sell = market[tid]["sell_price"]` → the completions ledger | **"Lifetime net profit"** (`reactions.js:1139`) |

`_value_reaction_batch` already computes `instant_value` / `net_profit_instant` when passed a
`buy_price` (`graph.py:242-245`), so the backend change is small at each site. The third is ranked
above the second: lifetime profit is the one figure a user reads as *fact* rather than forecast, and
it compounds over every completed job for the life of the account.

**A fourth sell-priced field is on screen** and is the subject of open question (b): the opportunity
fold-out renders `Sell order value` (`reactions.js:29`) beside "Profit (order)" (`:30`) and "Sell
depth" (`:33`). Decide all three together.

**The 3.3× is an illustration, not a measurement.** Materials 500m; output 4,000 units at 162.5k
sell / 140k buy; job+ship+collateral ~15m. Shown: 650m − 500m = 150m. Rule-compliant: 560m − 500m −
15m = 45m. Both errors push the same way. Before this figure is quoted anywhere outside this
document, derive it from a real account's plan.

**Also required by open question (a), and easy to miss reading WS1 top to bottom:** relabel the
existing tile **"Materials committed"** and build a separate full cost base that nets job install
fees, freight and collateral, deriving profit from that. Repricing the three sites above without this
leaves profit netted against an incomplete cost.

**Do not touch the advisor's pricing.** It is fully compliant end to end — candidate filter
(`advisor.py:116`), ranking (`:131`), LP objective (`:235`), row build (`:404-407`), hints
(`:424,454-457,466`). Verified; re-auditing it is wasted effort. Material *direction* is also
correct everywhere (purchasables at the order book's `sell_price` + freight — what acquisition
costs) and must not be "fixed" by symmetry.

### 1b. Two clocks, and the profit surface is on the wrong one

| Value | Source | Applied to | Read by |
| --- | --- | --- | --- |
| `time_efficiency_pct` | **typed by hand**, default `0.0` (`settings.py:40`) | the graph's `cycle_time` at load (`graph.py:58`) | wizard sizing + ETA, `_ordered_chain_tiers`, order quote (`orders.py:144`), frontend runtime preview, **and the dashboard's makespan divisor** (`jobs.py:2470,2550`) |
| `_reaction_time_mult` | **measured** from ESI job durations, persisted, else skills (`jobs.py:981`) | raw SDE cycles (`jobs.py:1298`) | `level_product_runs` (`:1486,:1566`), `split_order_tops_to_cadence` (`:1770`) |

`reaction_time_mult_for` (`jobs.py:1097`) is not a third clock — it is the measured one behind a
wrapper whose `type_id` is currently unused (`jobs.py:1100`). Keep the parameter: `docs/reactions.md`
records why a real answer must eventually be per-product.

**No user-facing duration or ETA is computed from the measurement** — it reaches the user only as run
and job counts, via the leveller. The knob's own UI note (`reactions.js:2304`) says
*"Time efficiency can't be detected"* — **that is false in this codebase** and must go.

**The change:** `time_efficiency_pct` becomes derived from `reaction_time_mult_for` wherever a
measurement exists, falling back to skills where it does not; the typed field survives **only as an
explicit override**, clearly labelled as one. CLAUDE.md rule 3 (no knob for a computable number) and
rule 5 (live data trumps, unless reliably derivable) both point the same way. Making the field
derived touches more of `settings.py` than the default alone: `29, 40, 73, 94-100, 235`.

**No double-application hazard**, confirmed: `_reaction_cycle_times` (`jobs.py:1298`) reads the
`reactions` table raw and is unaffected by `time_efficiency_pct`, so WS1 deriving this field cannot
double-count against WS2's raw-SDE space.

**This is not already-rejected work.** The three rejected attempts (`struct_time_pct`, `rx_bonus.te`,
`_routed_reaction_time_mult`) all tried to *derive the bonus from structure config* and each
over-claimed by ~22 points against measured reality. This wires in the **measurement that already
won**. Anything that proposes deriving it again must first be checked against `_reaction_time_mult`
on an account that has really reacted — that comparison is what retired the routed version.

### 1c. The wizard's rate ignores chain depth

`advisor.py:432`, `profit_per_day = reward / (cadence_hours / 24)`, and the totals line `:540`,
`net_profit_per_day = net_profit / (cadence_hours / 24)` — one window, while
each tier is independently sized to fill a window (`:370`) and tiers run strictly sequentially. A
3-stage weekly chain occupies ~3 windows and reports ~3× its true rate.

Worse and more crudely: `max_completion_hours` is a `max()` (`advisor.py:340,509`) and line 509 only
updates it for rows carrying a `reward` — **chain tiers are excluded from the wizard's ETA
entirely.** It never sees the intermediate stages. `_plan_totals` gets this right, summing per-stage
maxima and documenting why (`jobs.py:2344-2346`); the wizard, which is where the purchase decision
is actually made, does not.

Compounding: the sort key is `net_profit_instant / top_level_runs` (`advisor.py:131`) and the LP
objective is absolute profit (`:235`). `max_chain_depth` *filters* depth but nothing *discounts* it,
so the deepest chains — the ones whose rate is most overstated — are also the ones the ranking
over-prefers.

### 1d. One tile, two definitions of "per day"

`_unplanned_running_totals` (`jobs.py:2444-2446`) computes `net_profit / (runs × cycle / 24)` per job
and **sums**, which is exactly the over-counting rule `_plan_totals`' docstring abandoned
(`jobs.py:2344-2346`). That sum is then added onto the makespan-derived rate (`jobs.py:2812`). Also
sell-priced (`jobs.py:2440`). Pick one definition — the makespan one — and use it for both halves.

### 1e. Minor: an order's price apportioned twice

`_plan_totals:2381-2384` apportions `client_price × runs/top_level_runs` to every non-consumed row
of an order. A tier counts as consumed only if a surviving row's recipe lists it
(`_plan_intermediates:2316-2319`), so a tier whose only consumer was dropped by `_trim_tiers_by_stock`
(`jobs.py:1906-1910`) becomes a phantom end product and books a second slice of the invoice. Narrow,
but it inflates revenue silently rather than failing loudly. Fix with the rest of `_plan_totals`.

---

## WS2 — The cadence ceiling, and what it costs

### 2a. The ceiling does not merely soften — it collapses

`jobs.py:937`: `if r < max(1, min_runs) or (r > max_runs and r > min_runs): continue`. Work the truth
table: when `min_runs > max_runs`, the candidate `r == min_runs` passes both clauses (`r > min_runs`
is false) while every larger candidate is dropped. **The option set collapses to exactly one answer,
`min_runs`, whatever duration that implies.** Reproduced against the real branch with
`max_runs=119` (a 7-day window on Carbon Fiber: 119 × 3h SDE × 0.468 = 167.1h ≈ 7d),
`min_runs=200`, `total ≈ 1000`, `cap ≥ 5` → the only option returned is `{"runs": 200, "jobs": 5}`
= 200 × 3h × 0.468 = **280.8h = 11.7 days on a 7-day cadence.** Note `cap` in
`_level_options(total, cap, max_runs, …)` is the **job** ceiling; 119 is `max_runs`.

**It collapses by two routes, not one.** `min_runs` is not guaranteed to be in `cands` (built from
divisors of `total` plus tidy steps). When it is absent, `out` comes back empty and the `:952`
fallback returns `floor = max(1, min_runs, …) ≥ min_runs` regardless. Either way the answer is at
least `min_runs`.

Three independent routes abandon the ceiling, and **all three must be closed** — closing only the
loop leaves two:

1. **The give-ground loop.** `floor_runs[worst_tid] = nxt` (`jobs.py:1609`) is raised with no
   comparison against `max_runs`, escaping via `cur + max(1, cur//4)` (`:1606`) when no higher
   capped option exists.
2. **The seed.** `d_floor = stage_work / stage_room` (`jobs.py:1544`) → `floor_runs[tid]` (`:1550`)
   is computed with no reference to `max_runs` (`:1568`), so a reactor-tight stage breaches on
   attempt 0, before the loop runs at all.
3. **`_level_options`' own fallback.** `floor = max(1, min_runs, -(-total // cap))` (`jobs.py:952`)
   ignores `max_runs` outright and needs no loop to get there.

**It is wider than the cadence.** With no cadence set, `stage_cap_hours` falls back to "the longest
job the plan already runs" (`jobs.py:1510-1513`) and the same branch exceeds *that*, breaking the
promise stated at `jobs.py:1493-1494` — *"levelling then makes a plan tidier and shorter, never
slower."* So this hits every account with `reactions_level_runs` on, cadence or not.

**Per decision 1, the fix is not to force the ceiling.** It is to make the breach *deliberate and
visible*: the solver may exceed the ceiling when a stage genuinely cannot fit, must record by how
much and why, and the row must show it. Today a pending plan row shows runs and **never a duration**
(`reactions.js:871` formats hours for running jobs only), and Reactions has no equivalent of
Industry's `why.ceiling_met` (set at `app/industry/schedule.py:846`, named at `:598`, read at
`:1065`) — `:846` is the model to follow.

Correct `jobs.py:1502-1504` and `docs/reactions.md:742` in the same change. Comments that state the
opposite of the branch three lines below them are how this code became unreasonable-about, which is
the complaint that started this.

### 2b. The order is quoted uncapped and re-shaped a page later

`_allocate_and_insert` (def at `jobs.py:3112`) takes no cadence argument and reads none.
`split_order_tops_to_cadence` (`jobs.py:1747`) runs from the dashboard read (`jobs.py:2493`), outside
the `_level_runs_on` branch so it fires even with levelling off. Consequences, in order of cost:

* **The commit-time quote and the dashboard disagree about the same order.** The player shows a
  customer one layout and the next page load says another. **Fix by pacing at quote time**: give
  `_allocate_and_insert` (`jobs.py:3112+`) the cadence it currently does not take, and correct its
  docstring at `:3123-3129`, which argues the opposite policy. This is open question (c)'s
  recommended default and it is WS2's work — do not leave it to the reader to find.
* **The split writes N rows where there was 1 with no free-slot check.** `jobs.py:1767-1822` queries
  only `pp_reaction_assignments` joined to `pp_characters` — no `_character_capacities`, no `room`,
  no `budget`. The only bound on `jobs` is the formula cap (`:1797-1800`). Compare
  `level_product_runs`, hardened for precisely this (the *"12 slots assigned to characters that only
  have 10"* rationale at `jobs.py:1523`, the `budget =` line it justifies at `:1529`). Add the same
  check.
* **Those order top rows are excluded from `inner` (`jobs.py:1421-1424`)**, so `rows_by_char_stage` /
  `committed` never count them and the leveller sizes stage 1 against reactors the order's own rows
  are holding. Count them. This is **adjacent to TODO §28b item 2**, not item 1 — item 1 is the
  opposite mechanism (later stages *over*-reserving slots they cannot use).
* When the formula cap binds (`jobs.py:1797-1800`) the job stays over the cadence and nothing says
  so — same surfacing requirement as 2a.

### 2c. `_collection_slot` models a daily rhythm the user does not have

`ceil((h − 3)/24)` (`jobs.py:975`). Per decision 2 the rhythm is a duration of the user's choosing,
typically a week. Under a 7-day cadence every layout that respects the ceiling is collected in the
same session, so the term is mostly noise — **and where it is not noise it is harmful**, because
`slot` sits *ahead of* `surplus` and `untidy` in both score tuples (`jobs.py:1232,1263`). The solver
will therefore pay real goo, or hand the player a number they must read twice, to finish on a day
they do not log in.

**Change:** bucket by the account's cadence when one is set, falling back to 24h when it is not.

**And carry the grace into the ceiling.** `max_runs = int(_cap_h / cyc)` (`jobs.py:1568`) and
`per_job_cap = int((cadence_h / mult) / raw)` (`jobs.py:1794`) truncate at exactly 168.0h with no
tolerance, while `_collection_slot` says 171h is still day 7. A layout landing at 168h04m is rejected
and costs an extra job and an extra reactor — for four minutes that `_CADENCE_GRACE` exists to say
nobody cares about. One constant, applied in both places.

### 2d. What ease costs is computed and then thrown away

`grep -rn marginal_saving app/` returns `app/industry/*` only. The surplus **is** computed —
`_level_options` returns a per-candidate `surplus` (`jobs.py:945`), `_choose_stage_layout` sums it
into its score (`jobs.py:1213,1255`) — and then discarded: it appears in no API payload and in no
line of `static/reactions.js`. The passes that spend ISK to save clicks, stacked: `tidy_runs` 15%
(`_TIDY_BUDGET` at `graph.py:879`, the function at `:882`), `_LEVEL_BUDGET` 50% per stage
(`jobs.py:856`), the shared-count uniform layout (`jobs.py:1240-1266`), the cadence ceiling,
`_lean_hosts` (`jobs.py:3036`).

`docs/manifesto.md`'s scoring question 5 — *"Is its cost visible? Any shortcut that trades ISK for
time must report what it cost rather than taking it quietly"* — Reactions fails it outright, while
`docs/reactions.md` itself says the surplus "comes straight off the margin".

Per decision 3, surface it **with a remedy**, not as a bare number. Shape:
*"This layout holds 14m ISK of surplus intermediates to save you 2 logins and one reactor. Drop to
per-product run counts to recover ~9m, at 3 more numbers to type."* The point is that the player can
act on it, which is what distinguishes this from a nag.

Reactions has an existing convention arguing the other way — `jobs.py:2823-2826`, *"Over-production
produces nothing here on purpose: 21 runs of spare goo is stock, not a problem"*. That reasoning is
right for a **warning** and wrong for a **number**; the asymmetry it defends (shout on
under-production, silent on over) stays exactly as it is.

### 2e. The 50% budget is denominated in runs, not ISK

`_stage_affordable` (`jobs.py:1288-1295`) sums `runs` across a stage's products and tests
`made − need <= need * 0.50`. Runs of different products cost wildly different ISK. A stage of Carbon
Fiber (1,956 runs, cheap) beside a costly sibling (196 runs) has `need = 2,152`, so **1,076 runs of
surplus pass the test** — and every one of them may land on the expensive product, multiplying its
material bill while the ratio reads a comfortable 50%. Both fallback paths bypass the budget entirely
(`jobs.py:1270-1273`, `:948-954`).

Re-denominate in ISK. This is also what makes 2d's reported number meaningful — a budget you cannot
state in ISK cannot report a cost in ISK.

### 2f. The surplus loop only closes by luck

`_build_opportunities_uncached` calls `_ordered_chain_tiers(..., reached)` with **no stock pool**
(`graph.py:520`) and the result is Redis-cached (`graph.py:471-481`); held stock enters only at step
6a (`advisor.py:355`), *after* the LP has chosen. So a product whose intermediates you already hold
ranks identically to one bought from scratch. Week 1 over-produces X; week 2 the LP picks a chain
needing no X because X's zero marginal cost was never in the objective; X sits.

**Making the LP stock-aware may also be the cheapest fix to 2d** — a design judgement, not an
established fact, but it would turn dead surplus back into ranked value rather than merely reporting
it. Note the tension with the documented reason the opportunity
list is stock-blind (it is cached and its callers scale its tiers linearly, and stock coverage is not
linear, `docs/reactions.md:131-133`); the resolution is to make stock a term in the *objective*, not
a trim of the cached tiers. **Treat this as the stretch item of WS2** — if it does not fit cleanly,
ship 2a-2e and open a TODO rather than forcing it.

---

## WS3 — Reactions standalone, and the rollout

### 3a. The cadence is gated on a Manufacturing flag

`_rxLoadCadence` reads `/api/industry/build-setup` and sets `_rxCadenceAvail = r.available.job_length`
(`reactions.js:451`), which is `feature_enabled_for("industry_job_length_policy")`
(`app/industry/build_setup.py:40,56`); `_rxCadenceHtml` returns `''` when false (`reactions.js:460`).
The backend agrees — `_reaction_cadence_hours` returns 0.0 without that same flag (`jobs.py:1147`).

So the user's headline requirement — one rhythm — is switched off by a decision made about another
tab. Per decision 4: **the cadence becomes Reactions-owned**, behind its own flag, with Industry
continuing to read the same stored `max_reaction_job_days` value so there is still exactly one
number. Do not duplicate the storage; the shared-setting design is the one thing this area already
does well (written from both forms, read by `app/industry/graph.py:1177` and
`app/reactions/jobs.py:1147`).

### 3b. Two cadence controls, neither aware of the other

* `wizRCadence` — "Run on a… Daily/Weekly/2 weeks/Monthly", default 168h (`static/index.html:672`),
  posted as `cadence_hours`, sizes batches in `_suggest_reactions` (`advisor.py:159-432`; per the split table WS3 does not edit that interior math). **Not
  persisted**, re-picked every wizard open.
* `rxCadence` — "Come back every N days", persisted as `max_reaction_job_days`, read by
  `_reaction_cadence_hours`, used as the per-job ceiling in levelling.

`grep max_reaction_job_days app/reactions/advisor.py` is empty. To a player these are one concept
asked twice, in two units, in two places, with only one remembered. The normal path is therefore:
pick "Weekly" in the wizard, leave the ceiling at its documented default of unset, and have
`level_product_runs` re-shape that plan with no ceiling at all.

**Merge them.** The wizard's dropdown becomes a view of the stored setting: seeded from it, and
writing back when changed. One number, two places, exactly as the cadence row and Build rules already
manage.

### 3c. Missing formulas is unreachable without an Industry flag

`library.py` only reports once a *pasted* industry window makes the library complete, and the only
paste UI is `_indBpPasteFormHtml` (`static/industry.js:1189`) inside `#indManualBpSubsec`, hidden
unless `industry_manual_blueprints` is active (`industry.js:1088`). So `reactions_missing_formulas`
— a Reactions-group flag — cannot fire for anyone without an Industry flag. **The feature is
unreachable by construction.** Give Reactions its own route to the same paste endpoint.

Formula caps and stock degrade more gracefully: `held_formula_products` → `app.industry.blueprints`
(`library.py:98`) and `reaction_stock_pool` → `app.industry.assets.owned_quantities` (`graph.py:689`)
both fail soft to empty, which reads as "you own nothing" — the safe direction, but it means those
features silently do nothing for a reactions-only user. Worth a note in the UI, not a rewrite.

Credit where due and do not undo it: Settings → *Structures & Markets* and *Blueprints & formulas*
are already visible to any logged-in user (`planetary.js:2855-2860`), and the asset/stock panel is
ungated. The shared setup is reachable; it is the paste, the cadence and the per-order plumbing that
are not.

### 3d. The rollout decision

Registry defaults, `app/features.py` (live prod state is not readable from a dev session — the DB row
wins once created; these are registry defaults only):

| flag | default rung |
| --- | --- |
| `reaction_orders`, `local_market`, `local_sell_hint` | admin |
| `reactions_formula_cap`, `reactions_tidy_runs`, `reactions_use_stock` | admin |
| `reactions_parallel_stages`, `reactions_level_runs`, `reactions_missing_formulas` | admin |
| `reactions_assign_guard`, `reactions_pack_hosts`, `reactions_stage_pipeline`, `reactions_manual_done` | testers |
| `industry_job_length_policy` | testers |

**Not one Reactions flag defaults to public.** A normal logged-in user gets no orders, no formula cap
(so plans schedule parallel jobs off one formula), no tidy runs, no stock subtraction, no
parallel-stage slot reuse, no levelled run counts, no host packing, the old table instead of the
pipeline, no manual mark, no missing-formula report and no cadence — precisely the tool the last
month's work existed to replace. The good behaviour is the exception.

TODO §14 forces this choice for Industry and has no Reactions equivalent, with more force here
because the manifesto calls Reactions standalone and public-facing. **Write the equivalent forcing
entry**: for each of the thirteen, promote it or record why not. The registry's own rule applies —
retire a flag when the answer to "would we ever turn this off again?" is no. `formula_cap`,
`tidy_runs`, `use_stock`, `level_runs` and `parallel_stages` are the obvious first candidates, but
**not before WS1 and WS2 land**: promoting `level_runs` today ships the collapsed ceiling and the
invisible surplus to everyone.

### 3e. Say when an answer is provisional

Two instances of one failure — the app does not tell the player when its own number is a guess:

* `reaction_time_mult_for` falls back to skills-only until something has been measured
  (`jobs.py:1118-1121`), and its own docstring (`:1040-1043`) concedes the point. The *direction* is
  right and deliberate — under-claiming the bonus means too many short jobs, never a broken cadence —
  and must not be reverted. What is missing is one line saying the estimate tightens after the first
  real job.
* A breached cadence (2a) is equally silent.

Both are cheap, and together they are the honest half of trades the app has already made.

### 3f. Knobs by accretion

`reaction_system` is asked for on the settings card while `industry_default_build_system` already
infers the equivalent for Industry; leaving it blank means *"job install fees are left out of every
estimate"* (`reactions.js:2302`) and the app says so in a note rather than fixing it. `max_chain_depth`
in the wizard is a search-space limiter, not a player judgement — the tool ranks by profit
anyway. Neither appears on the manifesto's list of what Reactions *deliberately* leaves to the user
(run vs sell, risk tolerance, corp politics). Derive the first; consider moving the second off the
wizard's front page. Low priority, but they are the same rule-3 failure as the time-efficiency knob.

**If 3f is attempted, WS1 does it, not WS3** — the `reaction_system` note sits at `reactions.js:2302`,
inside the settings-card block WS1 already owns for the time-efficiency change. Two agents in that
block is the collision to avoid; the cheapest resolution is that 3f rides along with WS1 or waits.

---

## How the work splits

Three agents, one per workstream, each on a fresh context. The split is by **function ownership, not
by file** — `jobs.py` is 3,261 lines and every workstream touches it, so the boundaries below are
the contract that keeps two agents out of one function.

Line ranges below were verified against source on 2026-08-14. They will drift as the work lands —
re-index with `scripts/symbols.sh app/reactions/jobs.py` rather than trusting them blind.

| | WS1 — profit & clock | WS2 — cadence & cost | WS3 — standalone & rollout |
| --- | --- | --- | --- |
| `jobs.py` | `log_reaction_completions` (355), `_plan_totals` + `_plan_intermediates` (2298-2448), `_unplanned_running_totals` (2444-2446), **`get_industry_jobs` lines 2805-2856 only** | `_level_options` (890-955), `_choose_stage_layout` (1155-1274), `_stage_affordable` (1276-1296), `level_product_runs` (1314-1745), `split_order_tops_to_cadence` (1747-1822), `_collection_slot` (975), `_allocate_and_insert` (3112+), **`get_industry_jobs` Step 4 row construction (~2581-2790) only** | `_reaction_cadence_hours` (1147) — **gate only** |
| `graph.py` | `_value_reaction_batch` costs (229-235) + result (236-245), `reactions_job_detail` (581, 620-622), `cycle_time` (58) | `tidy_runs` (`_TIDY_BUDGET` 879, fn 882), opportunity build (471-520) *(stretch — see below)* | — |
| `advisor.py` | `profit_per_day` (432), `net_profit_per_day` (540), `max_completion_hours` (340, 509), the sort key (131) and LP objective (224-259) | — | `SuggestRequest.cadence_hours` (565) and the `suggest_reactions` entry point (777-791) — **no interior lines**; the cadence math at 159-170/242-269/303/419 is unchanged by 3b |
| `settings.py` | `time_efficiency_pct` (29, 40, 73, 94-100, 235) | — | — |
| `index.html` | — | — | `wizRCadence` (672) |
| `reactions.js` | tiles (1112-1115), lifetime (1139), job modal (2838-2868), opportunity fold-out (22-40), settings card (2169, 2302-2304) | row duration (871), breach badge, the ease-cost line (aggregated in JS from per-row fields — see below) | cadence control (439-490), wizard dropdown load/save, paste route |
| `features.py` | — | new flag for the ease-cost line | all other flag work |
| docs | — | `docs/reactions.md:742`, `jobs.py:1502-1504` | `docs/manifesto.md:126-129` |

**Collisions to police.** Every one of these was found by verification, not by design — treat the
list as the contract, not as advice.

* **`get_industry_jobs` (`jobs.py:2451-2856`) is the highest-risk shared region.** WS1 must change
  how `_unplanned_running_totals`' per-day sum is combined at `:2812` and owns the tiles; WS2 must
  add breach and surplus fields to the pending-row dicts built in Step 4. That is two agents in one
  400-line function. **The line split above is binding: WS1 takes 2805-2856, WS2 takes ~2581-2790.**
  If either needs to cross it, they stop and coordinate.
* **`_reaction_cadence_hours` (`jobs.py:1147`)** — WS3 owns the function and its gate; WS2 owns
  everything downstream of its return value. If WS3 changes the signature it says so before WS2
  starts.
* **`advisor.py` is shared by WS1 and WS3, and two successive attempts to split it overlapped
  anyway** — first WS3's "159-432" swallowing WS1's `max_completion_hours` (340) and `profit_per_day`
  (432), then a narrower grant that still collided with WS1's LP range at 242-259 (246 is
  `slot_demand`, cadence; 255 is `chosen.sort` on `net_profit_instant`). **The resolution is that WS3
  needs no interior `advisor.py` lines at all.** 3b makes the wizard dropdown a view of the stored
  setting, which is `index.html:672`, the `reactions.js` load/save, the `SuggestRequest` default
  (565) and the entry point (777-791). The interior cadence math is untouched by it. If WS3 finds
  itself editing inside 159-470, it has misread the task.
* **The ease-cost line's payload key.** 2d is a *plan-level* number, and a new top-level key would
  land in the return dict at `jobs.py:2830-2856` — WS1's side of the binding split. So **WS2 emits
  per-row fields on `characters[].pending` and aggregates in JS**; it does not add a top-level key.
  The row-level breach badge has no such constraint.
* **Those per-row fields must be priced and derived, not raw.** 2d's sentence needs the surplus in
  **ISK** (`unit_cost` is backend-only) and the counterfactual — reactors and logins saved, what a
  per-product layout would recover — which only `_choose_stage_layout` knows. So emit
  `surplus_isk` / `jobs_saved` / `recoverable_isk`, never `surplus` in runs. And note the leveller
  runs in Step 1 (`jobs.py:2486`), *before* the read that builds the rows: those values are either
  persisted on `pp_reaction_assignments` or recomputed in Step 4. Both ends are WS2's — a design step
  to settle early, not a collision.
* **WS2's stretch item 2f needs `advisor.py`, which WS2 does not own.** Making the LP stock-aware
  means editing the objective (224-259) and the sort key (131) — the same lines WS1 changes for 1c.
  **Either 2f is reassigned to WS1, or it waits until WS1 has landed.** Do not run them together.
* **`graph.py`'s `tidy_runs` (882) and `cycle_time` (58) are safely separate** — verified: different
  functions, no shared state, and `_reaction_cycle_times` reads the `reactions` table raw, so WS1
  deriving `time_efficiency_pct` cannot double-count against WS2's raw-SDE space. WS2 must still not
  "fix" units at `graph.py:58`.
* **`reactions.js` is 3,529 lines and all three edit it.** Sequence the frontend: WS1's tiles and
  fold-out, then WS2's row additions, then WS3's controls. After any large JS edit check for NUL
  bytes — `python3 -c "print(open('static/reactions.js','rb').read().count(b'\x00'))"` — expect 0
  (CLAUDE.md invariant; the Edit tool has landed template-literal separators as literal NULs before).

**Order.** WS1 and WS2 are independent and can run together, given the `get_industry_jobs` line
split. WS3's rollout decision (3d) must land **after** both, for the reason stated in 3d; the rest of
WS3 is independent. 2f runs last or not at all.

## Flags, tests and the verification gate

**Flags** (CLAUDE.md rule 2 — hot-patches to existing features do NOT need a flag; new features do):

* WS1 in full: **no flag.** Every item is a defect in a shipped feature, and three of them are live
  for every user with no gate at all.
* WS2 2a/2b/2c/2e: **no flag** — defects in `reactions_level_runs`, fixed in place behind the flag
  that already exists. 2d (the ease-cost line) is a new user-visible surface: **new flag**. 2f, if
  attempted: **new flag**.
* WS3 3a/3b: **new flag** for Reactions-owned cadence. 3c: no flag (it is a route to an existing
  feature). 3d: it *is* flag work. 3e: no flag.

**Tests.** Assert durable invariants, not runtime state (CLAUDE.md rule 1). Run against the
container before anything is called shipped.

* **A guard test that fails if any user-facing profit field derives from `sell_price`.** This is the
  most valuable single test in the spec: the rule was written before the code that broke it and
  drifted past it unnoticed for days. Make the next drift loud.
* `test_reactions.py` — the ceiling holds where it can, and where it cannot, the breach is *reported*
  (both directions, per decision 1).
* `test_level_runs.py` — extend for all three breach routes in 2a, and for the ISK-denominated budget.
* New — the order quote at commit time equals the order layout on the next dashboard read.
* New — the wizard's totals equal the dashboard's for the same plan (C's F6; the reconciliation is
  the assertion).
* A test that pins the clock: with a measurement present, the quoted duration uses it.

**Do not re-audit** (verified sound, with reasoning recorded): the advisor's instant-sell compliance;
material cost direction; the order profit panel (`orders.py:55-76`); `give_back_order_runs`;
`_plan_materials`' per-job ceil and single stock application; `chain_stage_state` and
`_gate_stages_account_wide`; `_align_stage_jobs` / `_widen_to_idle_slots` slot-neutrality;
`split_order_tops_to_cadence`'s exact total preservation; the paste-as-completeness rule in
`library.py`; the under-production warning's one-directional design; every manual escape hatch.

**Do not propose reverting** — already tried, measured and rejected, each with its reasoning
recorded: the even per-host split (`docs/reactions.md:319-330` and TODO.md §28), the job-length
dropdown (`:466`), `_cadence_drift`'s 0/1 flag (`:620`), `_seed_cadence_counts` (`:635`), the
per-chain 3× ceiling (`:552`), and every structure-config-derived time multiplier —
`struct_time_pct`, `rx_bonus.te`, `_routed_reaction_time_mult` (`:671, :686, :690`).

**The verification gate.** Every change goes to a fresh agent that did not write it, given the diff
and the requirement it was meant to satisfy — never the implementer re-reading its own work. It
verifies against code and real data, not against the implementer's report, and it checks that new
tests pin the invariant rather than the current output. Nothing is pushed on the implementer's say-so.
For WS1 specifically the verifier must be told about the cancelling errors, or it will read a
correctly-repriced profit-per-day as a regression: profit ~3.3× overstated (illustrative), the clock
~2.14× understating speed, profit-per-day ~1.5× out, and an intermediate state after repricing where
the displayed rate falls by ~3.3×.

**This spec was itself put through the gate on 2026-08-14.** The verifier found a function named that
does not exist, a multiplier off by a factor (1.81× where the truth is 2.14×), a claim contradicted
by the spec's own table two paragraphs above it, three pieces of decided work assigned to nobody, and
the `get_industry_jobs` collision. All are corrected above. The lesson to carry into implementation:
**a confidently-written citation is not a verified one**, and the errors that survive review are the
ones that look like the surrounding text.
