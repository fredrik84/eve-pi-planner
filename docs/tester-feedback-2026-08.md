# Tester feedback, 2026-08-05 — categorised, sized, prioritised

Thirteen pieces of feedback from testers, read against the code and against
[manifesto.md](manifesto.md). **Nothing here is started.** This is the plan: what each item actually
means once you look at what already exists, what it would cost, and what order the list is worth
doing in.

Items are `T1`–`T13` so they can be discussed before they claim TODO numbers. **They are thirteen
independent items**, prioritised individually — not a programme. Where two genuinely share a data
model or a code path that is called out on the item, and nothing else is coupled.

---

## Contents

- **What the feedback is about** — the shape of it, and who is asking
- **Themes** — the seven groups the thirteen items fall into
- **The ravworks reference** — a real shared config, and what it tells us
- **T14** — importing one, deferred but shaping what to build now
- **Item by item** — ask, what exists today, the actual work, size
- **Priority** — the ranked list, and the reasoning
- **Relationship to the audit items** — what this does and does not say about TODO 14
- **Open questions**

---

## What the feedback is about

Roughly half the list — placeholder characters with slot counts (T2), manual structures (T3),
manual blueprints (T9), manual progress tracking (T10) — is the same underlying want: **configure
the planner by hand instead of through ESI.**

This is **advanced-user configuration**, not a beginner on-ramp. The people asking are running real
industry operations and want to describe their setup themselves — declaring the slots, structures
and prints they use rather than granting scopes and letting the tool read them. Some of that is
about not wiring up ESI at all; some is about control over inputs the ESI path either can't see
(rig fitting, prints held by someone else) or gets wrong for their case.

**Whose setup is being described: their own** (decided 2026-08-05). A placeholder character stands
in for one of the user's own alts they have not connected; a manual structure is one they build in
themselves. Nothing here models capacity belonging to another player, which keeps every item
single-account and keeps rule 8 out of it — with the one exception of T13, where an exported config
is deliberately handed to someone else.

Two consequences for how to treat the list:

1. **These are not the thing standing between Industry and a public rollout.** A new user is served
   by the ESI path, which is the accurate one and the one that needs no configuration. Manual entry
   is what a serious builder wants *in addition*. (I initially read it the other way round; see
   *Relationship to the audit items* below.)
2. **Each earns its place on its own.** There is no unlock moment where a batch of them together
   changes what the tool can do, so there is no reason to bundle them. Ranked list below.

The rest of the list is a mix of correctness (T4), wording (T1), scheduling policy (T11, T12),
defaults (T5) and portability (T13), each standing alone.

## Themes

| # | Theme | Items |
|---|---|---|
| A | **Wording and consent clarity** — say what things are and what you're asking for | T1 |
| B | **Configure by hand instead of via ESI** | T2, T3, T9, T10 |
| C | **Structure modelling** — hull/rig correctness and per-part routing | T4, T7 |
| D | **Blueprint control** — ME/TE defaults, and which print a plan uses | T6, T9 |
| E | **Scheduling policy** — how long one job may be | T11, T12 |
| F | **Reaction policy defaults** | T5 |
| G | **Config portability** | T13 |
| — | **Small UX** | T8 |

## The ravworks reference

A tester pointed at [ravworks](https://ravworks.com) for structure/rig modelling, and supplied a
real exported config (an alliance-shared one for Perrigen Falls). It is the single most useful
artefact in this feedback round: it is simultaneously a worked example of T3, T7, T11, T12 and T13,
and it exposed a bug in our own numbers.

**How ravworks models a structure** (`hidden_my_structures`):

```json
{"id": "Structure 3", "name": "MTO2-2 - glizzymaker3000", "structure": "Azbel",
 "security": "Null / Wormhole",
 "Rig1": "Standup L-Set Basic Capital Component Manufacturing Efficiency I",
 "Rig2": "Standup L-Set Basic Large Ship Manufacturing Efficiency I",
 "Rig3": "Standup L-Set Capital Ship Manufacturing Efficiency I"}
```

Five things worth taking from that shape:

1. **Structures are purely declared — there is no `location_id` and no ESI anywhere in it.** Hull by
   name, security band, a free-text label the user writes intent into ("MTO2-2 - do not use"), and
   three rig slots naming actual rigs. This is T3's design, already validated by a tool people use.
2. **Rigs are named, not tiered.** We store `me_rig`/`te_rig` as 0/1/2 plus a hand-picked family
   list; ravworks stores the rig's real name and derives everything from it. Naming the rig makes
   the size (M/L/XL), the tech level, and the covered families all one choice instead of three.
3. **Security is a band, and the bands match ours exactly** — "High / Low / Null / Wormhole", with
   null and wormhole collapsed, which is what `SECURITY_RIG_MULT` already does.
4. **Build sites are assigned per CATEGORY** (`hidden_allocation_dict`): `cap_ship_select:
   "Structure 3"`, `comp_react_select: "Structure 4"`, `structure_select: "Structure 2"`. Explicit
   allocation, not inferred from rig coverage — see T7.
5. **Slots and skills are declared, not read** (`manu_slots: 10`, `react_slots: 10`, and a
   `skill_<type_id>: level` map) — see T2.

**Our rig math checks out against it.** The config carries the derived multipliers, so they can be
reproduced: Structure 3 is an Azbel in null with an L-Set T1 capital-ship rig →
`cap_ship_rig_me: 0.042`, `cap_ship_rig_te: 0.42`, which is exactly our `_ME_RIG[1]=2.0` and
`_TE_RIG[1]=20.0` times the 2.1 null multiplier. Structure 2's T2 rig gives
`structure_rig_me: 0.0504` = 2.4% × 2.1, matching `_ME_RIG[2]=2.4`. **The rig half of our model is
right.** The hull half is not — see T4.

## Item by item

### T1 — Wording: "Markets & Logistics", and a "market character" that asks for everything

**Ask.** New users don't know that manufacturing structures are added under *Markets & Logistics*.
And "Connect a market character" then requests full industry read — assets and everything — not
just markets.

**Today.** Both complaints are factually correct. Structures live in `pp_markets` because one row is
simultaneously "price from here" and "build here" — the data model is right, the *label* is a
leftover from when it only did markets. And `MARKET_SCOPES = REACTIONS_SCOPES` in `app/esi.py`: one
unified superset covering skills, planets, jobs, markets, structure search, blueprints and assets.

**Important: do not fix this by splitting the scopes.** The superset exists to fix a real bug
documented at length in `app/esi.py` — EVE refresh tokens carry only the scopes from the *last*
auth, so disjoint per-flow scope sets meant connecting a character one way silently stripped what
the other way had granted, with no way back. Splitting them re-breaks that. (The corp-Director
scopes are the one deliberate exception, and the comment explains why.)

**The work is honesty, not architecture:**
- Rename the settings panel to name both jobs it does (structures *and* markets).
- Change "Connect a market character" to say what it actually grants, in the user's words, with the
  reason — *one login covers markets, blueprints, jobs and assets, because EVE only remembers the
  last set of permissions you granted.*
- Add an entry point to add a build structure from the Industry tab, so discovering it doesn't
  depend on guessing which settings panel owns it.

**Size: S.** Copy, one nav affordance, no data model. **No flag** — it's a fix to existing features.

### T2 — Placeholder characters for Industry (reuse the PI dummy service)

**Ask.** A tester wants to set his own slot counts and configure dummy accounts rather than connect
characters. "We already do this for PI, can we reuse the dummy service?"

**Today.** Yes — and the answer is nearly free. `pp_characters.is_dummy` already exists, with
`POST/PUT /api/characters/dummy` behind the `dummy_characters` flag. But dummies carry only PI
fields (`interplanetary_consolidation`, `command_center_upgrades`), and
`app/industry/slots.py::_slot_pool` filters them out explicitly:

```sql
WHERE context_id=? AND COALESCE(is_dummy,0)=0
```

**The work:** extend the dummy model with the four industry skill columns the pool already reads
(`mass_production`, `advanced_mass_production`, `mass_reactions`, `advanced_mass_reactions` — all
already on `pp_characters`), drop the exclusion, and let `_eligibility` treat a dummy's declared
levels as its skill data. The UI is an extension of the existing dummy editor. Ideally the user sets
*slots* (1–11) and we store the implied skill levels, since slots are what they know.

Watch: `_eligibility` currently requires the skills scope, which a dummy has by definition not
granted — that check needs a dummy branch, not removal.

**Size: S/M.** The pattern, table, endpoints and UI all exist. **Flag:** extend `dummy_characters`
or add `industry_dummy_slots`.

**Ravworks reference:** it declares `manu_slots: 10`, `react_slots: 10` and a `skill_<type_id>:
level` map outright — no ESI. Slots-not-skills is the input model to copy, and the skill map is a
reminder that job *time* skills are a second declared input we would otherwise still be guessing at
(`skill_time_basis: "assumed"`).

### T3 — Manually added structures (no ESI structure scan)

**Ask.** A tester doesn't want to scan structures; he wants to add the ones he uses by hand, stored
per account.

**Today.** `POST /api/markets` takes a `location_id` — obtained from `/api/markets/search`, which
needs a connected market character. Everything *after* that point is already manual:
`POST /api/markets/{id}/build` sets `build_mfg`, `build_rx`, rig tiers, rig families, facility tax,
and even accepts manual `hull` and `security` overrides for when ESI couldn't detect them.

So the only ESI-shaped part is obtaining a `location_id`. **The dummy-character pattern solves this
exactly** — synthetic negative ids that can't collide with real EVE ids. A manual structure is a
`pp_markets` row with a negative `location_id`, a user-typed name, a system picked by name (T8),
hull from a dropdown, and the rig configuration that already exists.

One caveat to state up front: a manual structure can be used for **building** but not for
**pricing** — reading a structure's market genuinely requires ESI. The row's `price_from` must be
forced off, or the user gets an empty market silently.

**Size: M.** New creation path + UI, reusing the whole build-config surface. **Flag:**
`industry_manual_structures`.

**Ravworks reference:** its structures are *only* manual — `{name, structure (hull), security,
Rig1..Rig3}` with no location id anywhere. That validates the design, and suggests naming rigs
directly rather than asking for tier + families separately (see T4).

### T4 — Hull, rigs, and a real bug in our time bonuses

**Ask.** Does the automatic import take Azbel/Raitaru/Tatara into account when selecting rigs? The
tester suggested looking at ravworks.

**The direct answer.** Hull **is** detected (`_detect_structure_meta`) and **is** used — for the
role bonus and to classify a structure as manufacturing vs reaction. Hull is **not** used to
constrain which rigs you may claim: `_ME_RIG` tiers apply to any hull and rig *families* are a free
manual pick, so a Raitaru can be configured with coverage it could never fit.

**But looking at it turned up something worse.**

#### The bug: every structure's TIME role bonus is modelled as zero

`app/industry/structures.py`:

```python
_MFG_HULL_ROLE = {"raitaru": (1.0, 0.0), "azbel": (1.0, 0.0), "sotiyo": (1.0, 0.0)}
_RX_HULL_ROLE  = {"athanor": (0.0, 0.0), "tatara": (0.0, 0.0)}
```

The second number is the time role bonus, and it is `0.0` everywhere. The real values, verified
against EVE Ref type data and independently reproduced by the ravworks config's derived
multipliers:

| Hull | ME role | TE role | Job-cost role | Our TE today |
|---|---|---|---|---|
| Raitaru | 1% | **15%** | — | 0% |
| Azbel | 1% | **20%** | 4% | 0% |
| Sotiyo | 1% | **30%** | 5% | 0% |
| Tatara (reactions) | 0% | **25%** | — | 0% |
| Athanor | 0% | **0% — none** | — | 0% ✅ |

Athanor is confirmed (2026-08-05) to have **no** reaction role bonus — it is the reprocessing
refinery, and the reaction time bonus is the Tatara's alone. Our `(0.0, 0.0)` is correct for that
one hull, and it must stay 0 when the others are filled in. Note it can still *host* reaction jobs,
so its rigs still apply; only the hull role bonus is absent.

The ravworks config agrees exactly where the two overlap: its Raitaru-allocated category carries
`structure_struct_te: 0.15`, its Azbel categories `0.2`, its Tatara reaction categories `0.25` with
`struct_me: 0.0`.

**Consequence: every job duration we quote for a build in a real structure is too long, by 15–30%
before rigs.** That is not a cosmetic error — it feeds makespan, first-delivery, the scheduler's
pacing and cohort alignment, and indirectly make-or-buy, since the speed cap buys components whose
build time exceeds a threshold. "Fastest delivery" is half the Industry goal in
[manifesto.md](manifesto.md), and this understates our own answer to it.

One further gap in the same table: the **job-cost role reduction** (Azbel 4%, Sotiyo 5%) is not
modelled at all. Athanor and Tatara do need separating — but as 0% vs 25%, not as two unknowns.

#### Rig size is a real axis we don't have

M-Set / L-Set / XL-Set is **not** a strength ladder — bonus strength is the same at every size, and
what grows is *coverage*: EVE University puts it as "a single large structure can have the same rig
bonuses as six medium structures, while a single XL structure can have the combined bonuses of up to
12 medium". Compare two real rigs: `Standup M-Set Basic Small Ship Manufacturing Material
Efficiency I` covers exactly Frigate, Destroyer and Shuttle; `Standup XL-Set Ship Manufacturing
Efficiency I` covers Ships and Subsystems wholesale. Both give -2% ME.

Our `me_rig`/`te_rig` 0/1/2 is the **tech level** (T1/T2), which is correct and confirmed above. Rig
**size** is the missing axis, and it is what the hull actually constrains.

#### The SDE already carries all of it

The rig types expose, as dogma attributes: **rig size** ("medium"/"x-large"), **structure fitting
restrictions** ("can only be fitted to Citadel" / "can be fitted to Engineering Complex, Refinery"),
the **ME/TE bonus values**, the **security multipliers** (1 / 1.9 / 2.1 — our exact constants), and
the **affected categories/groups**.

That matters because `structures.py` currently says family membership "has to be a curated group-id
map in code" since the SDE subset carries `types.group_id` and nothing else. **That constraint is
about what `scripts/build_sde.py` chooses to import, not about the SDE.** Importing rig dogma
attributes would make hull↔rig compatibility, rig coverage and rig bonuses derived data instead of
three hand-maintained tables — and would let the UI do what ravworks does: pick a rig by name and
have everything else follow.

#### The work, in three separable pieces

1. **Fix the hull role bonuses.** Fill in the TE column above, add the job-cost role reduction,
   split Athanor from Tatara. Small, and it corrects live numbers. **Do this on its own, first.**
2. **Model rig size**, and constrain the rig picker by hull.
3. **Derive rigs from the SDE** rather than curating them — the larger, better version of (2).

**Size: S for (1), M for (2), L for (3).** **(1) is a correctness fix and needs no flag.**

### T5 — Stage-1 builds should react Hybrid and Biochem — **decided: make it the default**

**Ask.** For stage 1 builds we should do reactions in Hybrid and Biochem, because they're used in
later steps and can be done directly.

**Today.** `industry_reaction_policy` already has exactly these three families — `composite`,
`hybrid_polymer`, `biochemical` (`app/industry/categories.py`) — and the exact split being asked for
is already expressible: buy composites, build hybrid polymers and biochemicals.

**Decided (2026-08-05): this is a defaults change, not a planner change.** The default reaction
policy becomes *build hybrid polymers and biochemicals, buy composites*, with the reasoning written
down — those two families feed later steps directly, so building them removes a purchase without
adding a stage. The make-or-buy engine is untouched; a user who disagrees flips the same policy
control that exists today.

**Applies to every account with no stored policy** (decided 2026-08-05), not just new ones — which
is most of them, and is the point: an account that never opened the policy control is exactly the
one this default is for. It does mean plans people are already looking at will change, so the build
page needs a line saying the default changed and where to flip it back. Accounts that *have* set a
policy are untouched.

**Size: XS.** **No flag** — it's a default inside an existing flagged feature.

**Shipped 2026-08-05** as `DEFAULT_BUY_CATEGORIES = ("composite",)` (`app/industry/categories.py`),
plus a dismissible line on the build page for accounts still on the default. The stage axis below
was NOT shipped and is still open.

**Ravworks reference — this may reopen the decision.** Its blacklist has both a *family* axis and a
**stage** axis: `No_1st_reacts` and `No_2nd_reacts` alongside `No_bio_reacts`, `No_hyb_reacts`,
`No_gas_phase_reacts`. The tester's phrase was "for **stage 1** builds", which is ravworks
vocabulary — so they may be asking for the stage axis (build first-stage reactions, buy second) and
not only the family axis we already have. Our three categories are families only. **Worth
confirming before shipping the default**, since the two axes give different plans.

### T6 — Override blueprint ME/TE, but try to default it

**Ask.** Override blueprints to use specific ME/TE, but default it sensibly.

**Today.** Both halves already ship. Precedence in `prepare_plan_inputs` is
**user override > owned blueprint > contract-derived > 0/0**, with `me_source` recorded per type so
the plan can say where the number came from. The UI has per-type editing (`indEditMeTe`) and a
per-order chip showing an override is active.

**So the gap is discoverability and reach, not capability:**
- Overrides are per **order**, not per account — there's no "my Revelation BPO is always ME 10/TE 20".
- Setting one means finding the right row in the plan and clicking a small control.
- A user with no connected character doesn't obviously learn that 0/0 is being assumed.

**The work:** an account-level ME/TE library (product → ME/TE), sitting below the order override and
above the contract-derived value in the same precedence chain. This is **the same store T9 needs** —
the one genuine coupling in this list. Build it once, as T9's data model, and T6 is most of the way
done for free.

**Size: S if built with T9**, otherwise M.

### T7 — Override where a part is built (capital parts here, hull there)

**Ask.** Clear separation of builds — capital parts in one system/structure, the capital itself in
another.

**Today.** `industry_rig_routing` already routes **per job** by rig family: for each buildable type,
the site whose rigs cover that product's SDE group wins, fees follow the routing, and `build_sites`
names the structures so the checklist can say where to install each job. The described setup — parts
in one structure, hull in another — is exactly the case that feature was built for and should
already work *when the rig families are configured to imply it*.

**What's missing is the explicit override.** Routing is inferred from rig coverage; there is no way
to say "this type builds *here*, regardless". So this is a small addition on a solved problem: a
per-type site pin, checked before `resolve_job_sites`' scoring.

Worth confirming with the tester whether rig-family routing already produces their split — if it
does, this drops to a **transparency** item (show which site each job routed to and why, which
`build_sites` already half-does) rather than a new control.

**Size: S/M.** **Flag:** extend `industry_rig_routing`.

**Ravworks reference:** it does exactly this, explicitly — `hidden_allocation_dict` maps each
product category to a structure id (`cap_ship_select: "Structure 3"`, `comp_react_select: "Structure
4"`). No inference from rig coverage at all: the user says where each category is built. That is a
stronger answer than a per-type pin, and it is closer to how the tester described their setup
(capital parts in one building, the hull in another). Suggests pinning per **category** rather than
per type — far fewer decisions for the same result.

### T8 — System autocomplete in the logistics settings

**Ask.** Check whether the REACTION/BUILD system field is in use; if so, suggest systems as the user
types.

**In use: yes, definitively.** `rxAcctSystem` drives job installation fees for **reactions and
manufacturing both** — the Industry planner reads the same value, and `industry_default_build_system`
exists precisely because "almost nobody has set it" made every quote light by the system cost index
(76% of a Jita fee). It is free text today, resolved by name via `_resolve_system_id` against
`system_geo` and rejected if unknown — so a typo is a silent no-op until you notice the fee.

**The work:** a typeahead endpoint over `system_geo` (which already holds system, constellation,
security) and an input that uses it. `/api/constellations` in `app/planetary.py` is the pattern to
copy.

**Size: S.** **No flag** — it makes an existing field usable. **Best effort-to-value ratio in the
list.**

**Ravworks reference:** it carries `manu_system`, `react_system` **and** `inv_system` separately,
plus `override_indices` with per-activity `*_cost_index_override`. We have one account-level field
serving both reactions and manufacturing. Splitting it is a small extension of the same field and
directly serves the same want as T7.

### T9 — Manual BPO/BPC entry, and choosing which print a plan uses

**Ask.** Let users add their own BPC/BPOs. Empty runs = BPO; otherwise a BPC with max runs. Let the
plan flag whether to use the BPO outright or a BPC when both exist.

**Today.** Owned blueprints come only from ESI (`pp_char_blueprints` → `owned_blueprints()`), which
returns `{me, te, kind, runs, copies, copy_count}` per product — with `runs = -1` meaning an
original. **The tester's proposed encoding is the one the code already uses**, which is a good sign
for the design.

`copies` is consumed in `_copy_rank` order and feeds `_print_limits` (a print is locked while a job
runs on it), so a manual entry lands in a well-defined place.

**The work:**
- A `pp_industry_blueprints` table: context, product/blueprint type, ME, TE, runs (null = BPO),
  quantity.
- Merge into `owned_blueprints()` as a source alongside the ESI cache — with an explicit precedence
  rule, since a user who hand-enters a print that ESI also sees must not have it counted twice.
- The BPO-vs-BPC preference flag the tester describes, which affects both cost (a BPO consumes no
  copy) and `_print_limits`.
- Carries T6's account-level ME/TE defaults — same table, same precedence chain.

Watch: `params.buy_me_te` and the contract-derived ME/TE path already write into
`me_by_product`/`me_source`. A manual entry must slot into that chain explicitly rather than being
appended to it, or the precedence becomes order-dependent.

**Size: L.** The biggest single item in the list. **Flag:** `industry_manual_blueprints`.

### T10 — A "running" middle state, tracked by hand

**Ask.** Cycle *not started → running → done* by clicking in the build pipeline. And a button that
reads "run" first, becoming "done" once running.

**Today.** Progress has three signals (completion ledgers, running-job caches, manual done-marks)
tracked **per type**, and `industry_manual_done` gives a one-click done plus a partial-amount mark.
There is no manual **running** state — that one is inferred from the ESI job cache only.

**The work:** extend `pp_industry_manual_done` to a state rather than a done-count (or add a state
column), teach `queue_progress` to fold a manual *running* mark in the same way it folds done, and
make the pipeline card cycle. The rule that a manual mark never overrides a higher measured signal
must extend to the new state.

Worth noting it improves the **ESI path too**, not only the manual one: a job installed on an alt
that never granted the jobs scope currently shows as not started until it completes.

**Size: M.** **Flag:** extend `industry_manual_done`.

### T11 — Job length as an exposed trade-off — **decided: expose the axis with its price**

**Ask.** Optimise for "as many things as we can run simultaneously" — with the reasoning that the
tester builds by max runs per BPC to save money on copies, since more slots used means more BPCs
needed.

The phrasing and the reasoning point opposite ways: more parallel jobs means *more* copies, fewer
and longer jobs means *fewer*. **Decided (2026-08-05): build it as one axis with the price
attached** — show what fewer, longer jobs saves in copies and costs in hours, and let the user pick.
That is the manifesto's "every shortcut states its price" rule applied to the biggest remaining
scheduling decision, and it means neither reading has to be guessed at.

**Today.** The scheduler already holds both ends of this. `pace_cap` is a "never longer than" bound
(computed as the plan's own longest job) and `_packed_duration`/`window` decide the split;
`_print_limits` already computes what an *extra* print would buy in time, which is the same data
read in the other direction — what fewer jobs saves in prints.

**The work:** a policy input that moves the job-length target, with a live readout of the two
numbers it trades (copies used, hours added) — the same shape as the marginal-saving slider's sweep,
which already reads a whole curve rather than re-planning per pixel.

**Size: L.** It changes the objective the scheduler optimises, and every cost figure moves with it.
**Flag:** `industry_job_length_policy`. **Per CLAUDE.md rule 6 this is a `dev`-soak change**, not a
straight-to-`main` one.

**Ravworks reference:** it exposes `split_threshold: 1.0` beside `max_job_time`, i.e. the split
decision is a separate knob from the ceiling. Worth understanding what their threshold means before
designing ours.

### T12 — A ceiling on reaction job length

**Ask.** Configure a maximum time reactions should take. Reactions have unlimited runs, so 5,000
runs fit in one slot and take weeks; a 2–3 day ceiling should split the work across more slots.

**Today.** No user-facing ceiling exists, but the machinery does — `pace_cap` is exactly a maximum
job length, and a user ceiling is close to `pace_cap = min(pace_cap, user_ceiling)` for the reaction
pool.

**Why this is a separate, smaller item than T11**, despite living on the same axis: **splitting a
reaction is nearly free.** Reaction formulas are durable and reusable, so splitting costs only slot
occupancy — there is no copy to buy, which is the entire complication in T11. So this half can ship
as a straightforward bound while T11's trade-off is still being designed.

**Size: M.** **Flag:** can share `industry_job_length_policy` or ship on its own.

**Ravworks reference:** `max_job_time: 2.0` — a plain global ceiling, which is precisely what the
tester asked for and confirms the setting is worth exposing as a single number rather than a
policy.

### T13 — Export/import a configuration as readable JSON — **decided: shareable with other players**

**Ask.** All of these should be global account overrides, and a shareable configuration in readable
JSON — export and import.

**Today.** Account settings are spread across `pp_industry_settings`, the blacklist, the reaction
policy, `pp_markets` build rows, and `pp_source_sets`. There is no export.

**Decided (2026-08-05): the export is handed to another player.** That engages rule 8 directly —
structure names, systems and character names are locatable data about someone else's operation — so
the export must be **assembled field by field**, the same rule the plan shares and the customer
build-status links already follow, rather than dumped from the tables. Deciding per field what may
leave the account is the design work, and it is most of the item.

Two further notes:
- **There is less to export until other items land.** Manual structures (T3), manual blueprints
  (T9) and a job-length policy (T11/T12) are among the most useful things to hand someone, and none
  exists yet. Export built first would version a schema about to change under it — so this is
  naturally late, though nothing blocks a first version covering what exists today.
- **This must not become the settings-consolidation refactor**, which was examined and **closed** in
  [TODO.md](../TODO.md). Export reads the existing tables; it does not restructure them.

**Ravworks reference — the tester handed us a working example of the artefact.** Their config is one
flat JSON object carrying: a `cookie_version` (so versioning is solved the obvious way), declared
structures with names and systems, declared slots and skill levels, per-category build allocation,
job-length settings, blacklists, broker fee and sales tax. It is shared **alliance-wide** (Perrigen
Falls), which is the actual use case — not player-to-player exchange with strangers.

That reframes the privacy question usefully: the config deliberately contains structure names and a
system (`MTO2-2`), because inside an alliance that *is the point*. And we already have a feature for
exactly that trust boundary — **`industry_group_structures`**, which shares structures to an alliance
group so one person describes a building and nobody else has to.

**So there are two candidate designs, and they should be compared before either is built:**
- *File export/import* — portable, works across tools and alliances, needs the field-by-field
  privacy pass because a file goes anywhere.
- *Extend group sharing* — no file, no privacy pass (the group boundary already exists and is
  already enforced), and it covers the alliance-wide case that motivated the request. But it cannot
  be handed to someone outside the group, and it does not answer "back up my setup".

My read: group sharing covers the stated use case at a fraction of the cost; export is the more
general answer and the one that survives someone leaving the alliance. Worth deciding explicitly
rather than defaulting to the file because the tester said "JSON".

**Size: M** for export (most of it the privacy pass), **S/M** if extending group sharing instead.
**Flag:** `industry_config_export`.

### T14 — Import a ravworks configuration (deferred)

**Ask.** A future feature: import a ravworks config directly. Explicitly deferred (2026-08-05) — noted
here so it is not lost, and because it changes how some of the items above should be built.

**We already have a sample** — the alliance-shared export a tester supplied, analysed under *The
ravworks reference* above. Mapping its fields onto ours:

| Ravworks | Ours |
|---|---|
| `hidden_my_structures` (hull, security, Rig1–3, name) | manual structures — **T3** |
| `manu_slots`, `react_slots` | declared slots — **T2** |
| `skill_<type_id>: level` | declared skills — **T2**'s neighbour; nothing today |
| `hidden_allocation_dict` (per-category build site) | per-category build allocation — **T7** |
| `max_job_time`, `split_threshold` | job-length policy — **T11/T12** |
| `No_*` family/stage flags, `No_list` | blacklist + reaction policy — **T5** |
| `manu_system`, `react_system`, `inv_system` | the system field — **T8** |
| `brokers_fee`, `sales_tax` | existing account settings |

**The point that matters: almost every field maps to something we have not built yet.** Ravworks
import is a **capstone, not a starting point** — it cannot land before T2, T3, T7 and T11/T12 exist,
because there would be nowhere to put most of the file. Two consequences worth carrying into those
items now, while they are still being designed:

1. **It is a free correctness test for them.** If a real ravworks config round-trips into our model
   without losing anything, the manual-configuration items are demonstrably complete. If a field has
   nowhere to go, that gap is a design finding rather than a bug found later.
2. **It argues for [TODO 18](../TODO.md)'s keyed blob.** Importing a flat keyed config into a flat
   keyed config is a field mapping; importing one into columns spread over ten tables is a migration
   every time either side changes.

**Risks to note before building it.** Their format is undocumented and versioned by a field called
`cookie_version` (`0.08` in our sample) — so it can change without notice, and one sample is not a
spec. An importer should map what it recognises, report what it ignored, and never silently drop a
field. Also decide whether import is additive or replaces the account's config; replacing is the
obvious reading and the destructive one.

**Size: M** once its dependencies exist, and near-zero value before then. **Flag:**
`industry_config_import`.

---

## Priority

Ranked individually, by value against effort. Nothing here depends on anything above it except the
one coupling noted (T6 into T9).

| Rank | Item | Size | Why here |
|---|---|---|---|
| 1 | **T4a** hull time role bonuses | S | **A live bug.** Every job duration in a configured structure is 15–30% too long, which moves makespan, delivery dates and make-or-buy. Verified numbers, small fix |
| 2 | **T8** system typeahead (+ per-activity systems) | S | A field that silently no-ops on a typo, driving every job fee |
| 3 | **T5** reaction policy default | XS | Decided — but confirm the family-vs-stage question first |
| 4 | **T1** wording + consent + entry point | S | Trust, and the complaint most likely to repeat with every new tester |
| 5 | **T2** placeholder characters for Industry | S/M | Pattern, table and endpoints already exist |
| 6 | **T12** reaction job-length ceiling | M | Real pain, and the cheap half of the job-length axis — no copies involved |
| 7 | **T7** per-category build-site allocation | S/M | Ravworks shows the shape; may partly exist already via rig routing |
| 8 | **T3** manual structures | M | Reuses the synthetic-id pattern and the whole build-config surface |
| 9 | **T4b** rig size + hull-constrained rig picker | M | The rest of the tester's original question; wants T3's UI in place |
| 10 | **T10** manual "running" state | M | Improves the ESI path as well as the manual one |
| 11 | **T9 + T6** manual blueprint library | L | Highest value of the large items, and it carries T6 |
| 12 | **T13** export or group-share config | S/M–M | Decide the design first; group sharing may cover the real case cheaply |
| 13 | **T11** job-length trade-off | L | Changes the scheduler's objective; `dev` soak. After T12 proves the bound |
| 14 | **T4c** derive rigs from SDE dogma attributes | L | The right long-term answer to T4/T7; replaces three hand-maintained tables |
| — | **T14** import a ravworks config | M | **Deferred.** Blocked on T2/T3/T7/T11/T12 — most of the file has nowhere to land until those exist |

**If you want a first pass that is one sitting:** ranks 1–4 (T4a, T8, T5, T1) are all small, touch no
data model between them, and one of them fixes numbers people are quoting customers from today.

## Relationship to the audit items

**A correction to my first draft of this plan.** I initially read the manual-configuration items as
a *beginner* on-ramp and argued they were the gate for rolling `industry` public — that TODO 14's
"known gap or inertia" question was answered by them. **That was wrong.** These are advanced-user
configuration; a new user is served by the ESI path, which is the accurate one and needs no setup.
So this feedback does **not** answer item 14, and rolling out should be decided on its own grounds.

What the feedback does touch:

- **TODO 17** (stock sources have four surfaces) is unaffected either way — none of these items adds
  or removes a source surface.
- **T4 is the only item that should not wait**, because it is a correctness gap in figures users are
  quoting customers from today.
- **T11 and T12** are the two items that touch the planning algorithm, and per CLAUDE.md rule 6 both
  want a `dev` soak rather than a straight push to `main`.

## Open questions

1. **T7 — does rig-family routing already give them the split?** Capital parts in one structure and
   the hull in another is the exact case `industry_rig_routing` was built for. If it already works
   once families are configured, T7 collapses to a transparency item.
2. **T13 — file export, or extend `industry_group_structures`?** The stated use case is
   alliance-wide sharing, which the group feature already has a trust boundary for. See T13.
3. **T2/T3 — should manual and ESI data coexist per account?** Two connected characters plus three
   placeholders, or a scanned structure plus a hand-added one, is the likely real case — and it is
   what forces the precedence rules in T2, T3 and T9 to be explicit rather than incidental.
