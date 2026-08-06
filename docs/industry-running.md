# eve-pi-planner — Industry: running a build

Executing a queued build and reporting it: what is done, where the materials are, and what the customer sees.
Planning one: [industry-planning.md](industry-planning.md). Back to [CLAUDE.md](../CLAUDE.md).
The whole flow in one place: [industry-workflow.md](industry-workflow.md).

Find a section: `grep -n '^## ' docs/industry-running.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Running a build, not just planning one** — the always-buy blacklist, reaction policy, hand done-marks, weighted progress, corp hangars, per-order sourcing
- **Where a container IS, and which build owns it (`industry_plan_sources`)** — source keys, which build owns which container, the pre-answered picker
- **Customer build-status links** — `/b/{share_id}`, what the payload may never contain, why shares are permanent and progress is shared

---

## Running a build, not just planning one

Four things builders asked for after living with the tool. Each is behind its own flag
(`industry_blacklist`, `industry_manual_done`, `industry_corp_assets`, `industry_sourcing`).

**Always-buy blacklist** (`never_build_ids`). The mirror of `force_build_ids`: some things a player
simply always buys, which is a standing way of operating rather than a judgement the cost math can
reach. Stored per account in `pp_industry_settings.never_build_ids` (JSON id array) with its **own
write path** (`GET/POST /api/industry/blacklist`, `set_blacklist`) — deliberately NOT a field on the
settings PUT, which is a debounced save of the whole plan form and would carry a stale list along
with every knob move. Applied in `resolve_unit_costs`, not at demand time: deciding to buy a
component while still costing its parent as if it were built is the mismatch that makes a total stop
matching its own shopping list. Three precedence rules, all deliberate:
- **`force_build_ids` wins** — a per-order "build it anyway" is the more specific instruction.
- **A blacklisted item with no buy price is still built**; refusing to build what can't be bought
  would leave the plan no way to get one at all.
- **A TARGET is never blacklisted out of its own build** (`prepare_plan_inputs` filters the order's
  own products out of the list before it reaches the params) — ordering it IS the newer instruction.
Both shopping-list builders (`build_plan` and `plan_queue`) stamp `blacklisted` on the row, because
a material bought under a standing rule otherwise looks like the engine got make-or-buy wrong.

**Which reactions a build runs** (`industry_reaction_policy`). The blacklist one rung coarser: a
builder who simply doesn't run reactions had to blacklist every output by hand. Stored per account
in `pp_industry_settings.reaction_policy` (JSON `{build_reactions, buy_categories}`) with its **own
write path** (`GET/POST /api/industry/reaction-policy`, `set_reaction_policy`) — not a field on the
settings PUT, for the reason already written above. Applied in `resolve_unit_costs` beside
`never_build_ids`, so the decision is what removes the subtree; nothing is pruned by hand.
- **Categories are a registry, `REACTION_CATEGORIES` in `app/industry/categories.py`** — composite,
  hybrid_polymer, biochemical, matched to a produced type by its SDE `group_id`. The rig families in
  `structures.py` read the SAME group sets and keep their OWN labels: a rig family answers "does
  this structure's rig apply to this job" and a category answers "does this account run this kind of
  reaction". They agree today and needn't forever. Labels are served, never hardcoded in the UI
  (same rule as `ALERT_KINDS`).
- **The default (2026-08-05, tester request): build hybrid polymers and biochemicals, buy composites
  & intermediates** (`DEFAULT_BUY_CATEGORIES` in `categories.py`, applied by `_parse_reaction_policy`
  / `default_reaction_policy`). Those two feed later steps of a build directly, so reacting them
  removes a purchase without adding a stage; composites sit under intermediates and are the family
  where buying is usually the better trade. It is a **code default only**: it reaches every account
  whose `reaction_policy` column is NULL — a row created by onboarding, source memory or the
  freshness stamp does NOT count as a choice, which is what `reaction_policy_stored` distinguishes —
  and it never overwrites a stored policy. An empty stored `buy_categories` means "build all three"
  and is obeyed as such, so the default keys off the key being ABSENT, not falsy.
- **A category may only speak for what it can identify.** An uncategorisable reaction (no group, or
  a group in no category) is BUILT. That is why "we don't run reactions at all"
  (`build_reactions: false`) is its own switch and not three ticks.
- Precedence, most specific first: `build_reactions_anyway` (per order, `pp_industry_orders.
  build_reactions`, unioned across the queue like `force_build_ids` because the queue builds one
  shared batch) → `force_build_ids` → this policy → `never_build_ids`. All the bulk rules resolve to
  "buy", so they cannot contradict each other. **A reaction with no buy price is still built**, and
  **a reaction you ORDERED is never bought out of its own build** (`reaction_policy_exempt_ids`).
- **It reports what it cost** (`reaction_policy_report`, on both plan builders). Signed as *what
  BUILDING these would save*, which is the one figure that reads correctly in both directions:
  policy in force → that is what buying them in added to this build; order overriding it → that is
  what reacting them saved. Same rule as `marginal_saving`: report the shortcut, don't take it
  quietly — a builder quoting against a competitor has to see that not reacting moved their floor.
- **The control is a decision surface, not a notice**: one quiet row beside `_indMarginalBar`, never
  in the trimmed `_indNotices` block, with the per-family detail folded behind the switch. It says
  nothing about the **Reactions tab**, which is a separate feature with its own slot planning — an
  account may buy reaction inputs for its builds here and still run a reaction business there.

**Marking a job running or done by hand** (`pp_industry_manual_done`, `POST
/api/industry/progress/done`).
Progress inference is right most of the time and wrong in ways only the user can see: a batch built
on a character that never granted the jobs scope, work done before the account was connected, a
component acquired by trade. The mark is the **third done-signal**, combined by `resolve_done(need,
completed, from_stock, manual)` — the max of the three, capped at the requirement — so it can raise
a count but never hide observed work.
**A mark also has a middle state, `running`** (`state` column, additive migration 2026-08-06,
`DEFAULT 'done'` so every pre-existing tick still means done). The same blind spot hides a *started*
job: "running" is inferred only from the ESI job caches, so a job on an unscoped character reads as
not started until it completes. `resolve_running(need, done, observed, manual)` states the
precedence in one rule — **a hand mark may only ever move a type forward**: whatever `resolve_done`
settled is taken off the top first (`need - done`), so a hand "running" on a batch the ledgers
already prove delivered resolves to zero and the type stays done; between the two running signals
the higher wins, same argument as `resolve_done`. One row per type, so the two states are
alternatives and the click cycle is one write each time. Stored per TYPE (the grain everything else here uses), and
epoch-gated exactly like the completion ledgers so a tick from a finished build can't read as
progress on a re-queued one. Runs `-1` (`_ALL`) means "all of it, whatever the plan currently says";
a concrete number would go stale the moment a quantity changed — so the UI stores the sentinel
whenever the user says "all", including when they type the full count into the partial editor.
**Partial marks cost one extra click and the whole-step case still costs one** (`indEditDoneRuns`
off the card's run count, which is already the number being corrected). Five of twelve runs
finished with the rest waiting on a slot is a real state, but it is the rare one, so it must not
tax the common "this one's finished". It **never writes to the completion
ledgers** — those feed lifetime turnover and profit, and a tick is not evidence of an ISK-bearing
job (same rule the simulated-progress preview follows).
**The headline percentage is weighted by JOB TIME, not run count** (`_weighted_pct`, falling back to
runs when a plan carries no schedule times). Runs are the unit you mark in and a terrible unit to
summarise with: bulk components arrive as hundreds of short runs while the capital part is a handful
of very long ones, so one finished reaction batch — 57 minutes of a multi-day build — reported
**71.8% done**. Each type row carries `job_hours` (summed across the parallel jobs its runs were
split into); the payload reports `pct`, `runs_pct` and `hours` so the tile can name what it measured,
and `_indApplyDoneLocally` mirrors the same weighting or the optimistic repaint would flash a
different number. The counters beside it are **runs**, and say so — they used to be labelled "jobs",
which one job carrying many runs makes plainly wrong.
**Ticking repaints; it does not re-plan.** Marking a step changes nothing the plan computes — not
the requirements, not the schedule, not the cost — only the progress read off it, so the browser
recomputes the affected numbers itself (`_indApplyDoneLocally`, `max(observed, manual)` — the same
rule as `resolve_done`) and redraws from the plan already on screen via `_indPaintStatus(d,
{local:true})`. The write still goes out and its answer replaces the local one when it lands. This
used to cost **two** whole-queue plans per click (the endpoint runs one, and the old
`indRefreshStatus()` afterwards ran another), which on a capital build is seconds of waiting to
watch a card turn green. Two things make it computable client-side: `observed_runs` /
`observed_running_runs` on each type row (the counts with the marks removed — the resolved ones
alone can't be un-mixed once a mark is folded in),
and a local repaint carrying the running-jobs list and sourcing panel across untouched instead of
re-fetching them. Preview mode opts out: its numbers are fabricated, so editing them is editing
fiction.
**The control is the pipeline card** (`ind-pipe-markable`): that card already renders this type's
done/cooking/waiting state, so the place showing the state is the place that corrects it, and a whole
card is an easy target. **One click advances it** (`indCycleDone` over `_indDoneState`): not started
→ running → done → not started, wrapping so a misclick costs clicks and never data. The step-by-step
chips keep a labelled button (`_indDoneBtn`, shared so every surface cycles identically) alongside
`always buy`, as the fallback for plans too shallow to draw a pipeline — both were bare dimmed
glyphs first, which on a chip already carrying a name, runs, a duration and an ME/TE tag were
effectively invisible. **The button's label is the NEXT state, not the current one** — it reads
`run` on a step that hasn't started and `done` once it is running, so it says what pressing it does;
only the finished state names itself, because there the press is an undo. The partial editor is
unchanged and still a `done` mark: `indEditDoneRuns` stops propagation, so opening it never also
cycles the card underneath.

**Corp hangars over ESI** (`refresh_corp_assets`, `POST /api/industry/assets/refresh-corp`). The
module docstring used to say corp assets were deliberately not read, because `/corporations/{id}/
assets/` needs the **Director** role and ESI offers nothing weaker. That reasoning still holds for
most players — the paste path is theirs — but directors run their builds out of corp hangars, so
this reads them into the same opt-in source list. Needs `CORP_ASSETS_SCOPE` +
`CORP_DIVISIONS_SCOPE` (division names: a director picking a hangar needs the names they gave it),
and those are the **one exception to the single-superset rule** — they live in `DIRECTOR_SCOPES` and
are requested only by `/auth/login?director=1`, behind the explicit "Connect a director" button.
They were briefly folded into the shared superset, which meant every login asked a whole userbase to
hand over corporation-wide read access so the occasional director could skip a copy-paste; a tester
hit that consent screen and it was the right thing to complain about. `DIRECTOR_SCOPES` is a strict
superset of `REACTIONS_SCOPES`, or connecting a director would strip the scopes every other tool
needs. Known wrinkle: a director who later re-auths through a normal flow loses the corp scopes —
recoverable and visible (the panel offers "Connect a director" again), which is a far better failure
than asking everyone.
**Both scopes must also be enabled on the EVE application itself** (developer portal) — prod and dev
are separate applications, so that is two edits, and a scope the app doesn't carry fails at the SSO
screen rather than in our code. **A 403 is not an error** — it is the expected answer for a
non-director and is reported as "no Director role", never retried. Scanned once per CORPORATION, not
per character, and only on request (a full corp asset list is heavy).
Two supporting changes in `assets.py`: sources carry the **`scope`** that owns them (`char:<id>` /
`corp:<id>`), so a re-scan replaces everything that scan owns instead of only what it found this
time — an emptied container has to disappear, since counting stock you can't draw from is the
asymmetric error this module exists to avoid — and `_split_by_source` takes a `cont_key` so corp
keys (`corp:<cid>:h<n>`, `corp:<cid>:c<item>`) can't collide with personal ones.

**Per-order material sourcing** (`app/industry/sourcing.py`, `pp_industry_sourced`,
`pp_industry_orders.source_key`). "What have I already gathered for this build, and what's still to
buy." Players dedicate a container per build and haul into it, so **the box is the record**: an
order names one stock source and whatever is in it counts as sourced with no ticking at all — rescan
after hauling and the checklist moves itself. Anything ESI can't see is **pasted** from the client
(`POST …/sourcing/paste`, sharing `assets.parse_stock_paste` with the pasted-stock source so the two
can't disagree about what a hangar contains); the **higher of paste and box wins** per material, so
a note never erases real contents and a scan never erases a note.
A per-row "got it" button was the first cut and was wrong: an Archon has 50+ distinct materials, so
one confirmation per material is data entry, not a checklist. The paste **replaces** the order's
notes rather than merging — it's a snapshot, so a material since consumed has to drop back to zero,
and merging would make every past paste a floor the count could never fall below. Items the build
doesn't need are ignored, not flagged (people select the whole hangar). The per-material control
that remains is `clear`, for correcting one line.
**Binding a source also ENABLES it** (`enable_bound_source`, called from both `create_order` and
`update_order`). "This build pulls from that box" and "the planner may count that box" used to be
two switches with only one thrown, so the checklist said you had the materials while the shopping
list beside it still told you to buy them — naming the box you're hauling into settles both.
Binding enables; **unbinding never disables**, because auto-disabling could switch off a source the
user turned on themselves or that another order still draws from, and the two failure directions
(ignoring stock you have → build too much; counting stock you don't → build too little, shopping
list short) are both too costly to guess at. One tick in Setup undoes it. **This is now the LEGACY
path** — see per-plan sources below, where the account-wide side effect is exactly what changes.

## Where a container IS, and which build owns it (`industry_plan_sources`)

Two things reported from use, one code path. Both live in `app/industry/assets.py`.

**A container is identified by where it is.** A source row used to carry its own name and its parent
hangar division and nothing else, which is ambiguous exactly when it matters: picking which box a
build sources from with cans in several stations. `_split_by_source`'s `root_of` walk (the one that
already finds the hangar flag) now also yields the root asset's `location_id` — the station or
structure the whole chain sits in — resolved to `{name, system}` by `_resolve_location` and stored on
the row (`location_id` / `location_name` / `system_name`). Rules worth keeping:
- **Only CONTAINERS carry a location.** A hangar source key is `char:<id>` for every station that
  character has a hangar in, so it has no single location and must not claim one.
- **Station → `/universe/stations/{id}`** (public). **Structure → `/universe/structures/{id}`**
  through `markets.structure_info`, which is now the ONE place that call is made (the market
  hull/security detector was the other; it calls this too). Structures are **ACL-gated**: a 403 is
  the normal answer for somebody else's citadel and degrades to "no system name", never to a failed
  asset scan — the same rule container naming already followed.
- **Resolutions are cached in `pp_locations`** (global — an id→name mapping is a property of New
  Eden, and a row is only ever read back for an account that already holds assets there). The
  **unresolvable answer is cached too**, or every scan would re-burn the ESI error budget on the
  same 403.
- `_place_label` is the one string every list groups by, built server-side (`place` on each source)
  so the four pickers can't word it differently. It appends the system only when the location name
  doesn't already lead with it — every NPC station name opens with its system, and "Jita IV - Moon 4
  - CNAP · Jita" reads as a bug.
- **Every list that shows containers groups by it**: the Setup stock list (section headers), the plan
  modal's "Materials from" picker and the sourcing panel's (both `<optgroup>` per station/structure,
  via the shared `_indGroupSources` / `_indSourceOptionsHtml`). Reactions and PI surface no container
  lists of their own — `pp_asset_sources` is read only by Industry — so there was no fourth place.

**A build owns its containers.** `pp_industry_orders.source_keys` (JSON array) replaces the single
`source_key`, because reaction stock and manufacturing stock genuinely sit in different stations and
one build draws on both. `source_key` is still written as the set's first element, so nothing that
only knows about one box changed. `source_quantities_multi` sums the set (no double counting is
possible — an item belongs to exactly one source by construction, and the key set is de-duplicated),
and `_item_row`'s **higher of paste and box** rule is untouched: the sum is what the note is weighed
against.
The bigger change is what the set MEANS. Binding used to switch a box on account-wide, which made
one build's can every other build's stock. Now `plan_source_keys(ctx, orders)` resolves what a plan
may spend: an order whose set the user curated (`sources_owned`) counts that set **and nothing
else**, so sharing a container between two builds is a deliberate pick in both, not a side effect.
Three rules keep that from being retroactive or surprising, and all three are load-bearing:
- **An uncurated order still draws on the account-wide tick list.** Every order queued before this
  is uncurated, so an in-flight build cannot silently lose sight of a can it was already counting.
  `sources_owned` is set only when a caller sends `source_keys` — the old single field never claims
  ownership.
- **A mixed queue is the UNION of both.** The queue is planned as one aggregated batch, so denying
  it the pool an uncurated order is entitled to would have the planner buy materials the user has in
  hand — the expensive direction to be wrong in.
- **An empty set is not ownership.** Picking no box says nothing about where materials come from, so
  it falls back to the pool rather than to no stock at all; clearing every box hands ownership back
  (`sources_owned = 0`). Same rule in `graph._plan_on_hand` for the single-product preview, which
  takes `source_keys` so the preview is costed against the stock the resulting ORDER would count.
`enable_bound_sources` (legacy, single-key callers) and `remember_source_default` (the per-plan path)
are the two halves of what `enable_bound_source` used to do; only the first touches the account-wide
tick list. The remembered default is a SET now (`pp_industry_settings.last_source_keys`, first
element still in `last_source_key`), so a builder who gathers from two cans on every order gets both
pre-filled.
**Effort constraint:** the common case is still one box and still costs one dropdown, pre-answered.
Extra boxes are behind a `+ another box` button, and `pp_source_sets` (a named group — "reaction
stock" = three cans across two stations, `GET/POST/DELETE /api/industry/source-sets`) collapses a
repeat multi-box answer back to a single pick. A saved set is a **shortcut for choosing boxes**, never
a second thing a plan can be bound to: picking one expands to its keys on the order, so there stays
exactly one shape of binding for every reader.
**The picker arrives already answered.** Binding records the source as the account's default
(`pp_industry_settings.last_source_key`, written by `enable_bound_source`) and the next build's
picker pre-selects it — a builder running a can per build was otherwise answering the same question
on every order. Stored per account rather than read back off the orders table so it survives that
order being finished and cleared; the option is skipped when the remembered container no longer
exists in the scan.
**Both are chosen while planning the build**, not only afterwards: the plan modal carries a
"Materials from" picker (the scanned sources, or *paste what I already have*) and `OrderCreate`
takes `source_key`, because which box a build belongs to is decided at the same moment as what to
build. A paste made there lands on the new order's checklist and **nowhere else** — it is not
registered as planner stock, since stock that can't actually be drawn from is the one error that
makes the planner build too little. `source_quantities(ctx, key)` deliberately ignores the source's *enabled* flag: this
asks what's in a specific box the user pointed at, not what the planner may spend.
**The requirement is per ORDER, not the queue batch** — the queue aggregates demand across orders
(right for cost and scheduling, useless here: you can't haul 40% of a shared batch into one
customer's box), so this plans the order alone with its own quantity and overrides, and the sum
across orders can legitimately exceed what the queue will build.
**The panel renders NO material table** — that was the first cut and it was wrong. The shopping list
is already that table, and the two can only ever disagree: the queue's list nets stock off and
batches shared components once for every order, while sourcing measures one order against its full
requirement (`use_stock=False`, or the progress bar could never fill). Two tables of the same
materials showing different quantities is worse than one, however well each is explained. What the
panel knows that the shopping list cannot is per-BUILD state, so that is all it shows: which box this
build pulls from, how far the gathering has got, and the shortfall behind one `<details>` click for
when you don't want to scroll. For the same reason a sourcing row carries no unit price, market or
line cost (`_item_row`); the one exception is the SHORTFALL's cost, which decides whether to go
shopping at all. `test_the_sourcing_list_is_not_a_second_shopping_list` asserts that on the row
itself rather than the source text — the function reads a unit price to compute that shortfall. Deleting an order clears its notes
(ids get reused).

## Customer build-status links

**Share links are permanent.** Every successful render is snapshotted onto the share row
(`pp_industry_shares.last_payload/last_at`, written on a cache miss so at most once a minute), and a
share whose ORDER has gone — finished and cleared, or deleted — serves that snapshot flagged
`archived` instead of 404ing. A link handed to a customer has to survive the build being done; "404"
is the worst possible answer to "did my ship get built?". The customer page shows a "final state"
notice and stops polling. Only an unknown or REVOKED id is a genuine 404 — revoking is still a hard
kill.

**The customer sees the PRICE, never the cost.** The share payload carries `price` and nothing else
about money — not total/materials/job cost, not the margin. What it cost to build and what the
builder makes on it are not the customer's business; the quote is. `test_customer_build_status_leaks_nothing`
enforces both halves (banned cost words, `price` present).

**Customer build-status links** (`app/industry/shares.py`, `industry_share` flag). A builder mints a
login-free link per queued order (`POST /api/industry/orders/{id}/share`, idempotent; DELETE
revokes) that the customer opens at **`/b/{share_id}`** — product, quantity, stage list, progress bar
and ETA, auto-refreshing. Served from **`static/build.html`**, a standalone document with no app JS
and no session: the page must be incapable of showing account data even by accident. `/b/{id}` in
`main.py` injects Open Graph tags (same pattern as `/s/{id}`) so the link unfurls in Discord.

- **Privacy is the design** (rule 8): the payload in `build_status()` is assembled field by field,
  never filtered from a plan. It carries NO character names, systems/structures, ISK of any kind
  (cost, shopping list, margin), and nothing about the account's other orders. `test_industry.py`'s
  `test_customer_build_status_leaks_nothing` asserts this against the function source, so adding a
  leaky field fails the suite.
- **Stages** = depth from the shared product (`_stage_of_types` reuses the scheduler's `_depths`),
  so stage 1 is the deepest components and the last is "Final assembly" — the same ordering the
  builder's pipeline shows.
- **Two different plans on purpose:** structure/run counts come from a plan of THIS order alone
  (`_order_plan`, `use_stock=False`) — the queue plan aggregates every order, which would both
  misstate the customer's build and disclose the builder's other work — while the **ETA** comes from
  the whole-queue schedule, because contention for slots is real and the customer feels it.
- Progress reads the same signals the builder's own view does — `_done_by_type`/`_running_by_type`,
  owned quantities **and hand marks**, combined through the shared `resolve_done`, and weighted by
  **job time** through the shared `_hours_by_type` — so a customer can never see a rosier number
  than the builder, nor a staler one, nor one that is rosier only because it was measured
  differently (the build sheet read 10% while the customer's page read 48%, same build, same
  moment). Twice now this path has drifted by keeping its own copy of a rule; if you change how
  progress is computed, change it here in the same commit. The three used to be two here: a
  step ticked done moved the builder's bar and not the customer's, because this path had its own
  copy of the combination rule. That's what `resolve_done` is shared for.
- **Marking a step done invalidates every share on the ACCOUNT** (`invalidate_context_shares`), not
  just one order's: a mark is per type, and one type feeds several customers' orders. Without it the
  fix above is still a minute late, which looks identical to broken to whoever just pressed the
  button.
- Public reads are cached 60s (`indshare:<id>`); a public page whose every render costs two plans
  would otherwise be an amplification lever.
