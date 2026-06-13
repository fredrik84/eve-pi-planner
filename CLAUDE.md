# eve-pi-planner — Developer Notes

## Project Goal

Optimize a player's EVE Online Planetary Industry (PI) setup across multiple characters with the least effort for distributing and delivering materials. The planner assigns extractor planets (where P0 raw materials are harvested) and factory planets (where P0→P1→…→P4 processing happens) across all characters to hit a user-specified overproduction target.

---

## Code layout

`app/planner.py` is the core. `_run_plan(req, context_id)` orchestrates; the heavy lifting is in named helpers (refactored out of one giant function):
- `_compute_slot_budget` → factory count + `_compute_factory_shares`
- `_build_need_list` → Bresenham-ordered extractor slots
- `_assign_extractors` → Pass 1 (existing) → swap → Pass 2 → post-swap; calls `_run_swap_pass`
- `_attach_extractor_planet_details` → pins existing colonies per-char, then maps new slots to concrete planets + quality via `_waterfill_new_slots` (lever 1: per-character regret assignment — places the slot with the largest best-vs-next-best gap first, so a resource whose only alternative is a thin planet wins a shared planet type over one with a good fallback; order-independent, quality-optimal per char)
- `_assign_factory_planets_to_chars` → factory planet placement + overflow
- `_max_matching` / `_max_matching_slots` / `_can_add_p0` → bipartite feasibility (a slot with a committed planet is pinned to it)

**Shared plan helpers** (in `planner.py`, used by both the single-product and fuel-block
paths): `_load_char_planet_config`, `_build_p1_info_raw`, `_fetch_planets_and_recs`,
`_factory_candidates(only_bt=)`, `_build_p1_info`, `_build_char_list(with_ccu=)`,
`_set_computed_ext_cap`, `_run_extractor_pipeline`, `_pick_factory_system`. These were
extracted to de-duplicate the two run functions (~150 lines of copy-paste).

**Fuel-block planner** lives in `app/fuelblock_planner.py` (own `APIRouter`, registered in
`main.py` after `planner_router`): `FuelBlockPlanRequest`, `_run_fuelblock_plan`,
`_compute_fuelblock_budget`, `_assign_fuelblock_factories`, `_system_security`, and the
`/api/plan-fuelblock` + `/api/fuelblock-bom` endpoints. It imports the shared helpers from
`planner.py`; the only planner→fuelblock dependency is a **local import** inside `debug_plan`
(avoids a circular import). The BOM/ME math (`resolve_bom`, `compute_basket_p1_reqs`,
`resolve_rig`/`sec_band`, `me_keep_factor`, `apply_effective_me`) is consolidated in
`app/fuelblocks.py`.

(The old `planner_v1.py` pre-refactor backup has been deleted.)

**Factory planet-type filter.** `FuelBlockPlanRequest.factory_planet_types` (None →
`DEFAULT_FACTORY_PLANET_TYPES = ["Barren","Temperate"]`, the smallest planets / least link
+ power-grid footprint) restricts where factory planets are placed. `_factory_candidates`
(in `planner.py`, shared) takes `allowed_types=[...]` (the old `only_bt=True` =
`["Barren","Temperate"]`); it filters the pool **and** the per-system option counts, Barren
first. The no-DB-planet fallback in `_assign_fuelblock_factories` tags slots with the first
allowed type (was `"Any"`) so templates get a concrete type/diameter. The plan result
echoes `factory_planet_types` (chosen) + `available_planet_types` (distinct types actually
in the chosen systems, via `_available_factory_planet_types`). Fuel-block lines are P1–P3
so any type physically works; **P4 stays Barren/Temperate** (single-product planner keeps
`only_bt=True`, and `generate_layout` coerces P4 anyway). UI: plan-step chip row
(`#factoryTypeChips`, `_wiz.factoryPlanetTypes`, live `_rerunPlan`), greyed for types with
no planets, can't deselect the last. Persisted in profiles (`factory_planet_types` column)
+ shares (`fpt`). **Shortage warning:** factory planets that can't get a real planet of the
allowed types in the factory system are tagged with the fallback type + `planet_num=None`
(they do NOT count toward `unplaced_factories`, which is the separate "ran out of character
*slots*" case). The result reports `factory_planets_unpinned` (count of `planet_num=None`
factory assignments when `best_fac_system` is set); the UI shows a `.plan-ptype-shortage`
warning under the chips that clears when the user widens the planet types (more real planets
become available → unpinned drops to 0).

**CCU + planet-size scaled templates.** The PI Templates (.zip) was hardcoded to CC5; now
the bundle token format is `id[:lp[:count[:cc[:planet_type]]]]` (`/api/layout/bundle` in
`planetary.py`, backwards compatible). `cc`/`planet_type` pass through to `generate_layout`,
which already honours `cc_level` (CPU/PG budget) and `PLANET_DIAM[planet_type]` (bigger
planet → longer links → fewer facilities fit), so each factory's template matches the
**actual command-centre level and planet** of the toon hosting it. Zip filenames are tagged
`…_CC{n}` to keep variants distinct. The fuel-block frontend builds tokens from the real
placements with **`expand=0`** and lists factory + extractor templates explicitly: factory
tokens from `factory_assignments` (distinct `(product type_id, planet_type, f.ccu)`), and one
extractor token per `(p1_type_id, a.effective_ccu, best_planet_type)` from each assignment's
`extractors` — so a mixed CC5/CC4 fleet gets each extractor tagged at the **extractor toon's**
CCU, not the factory's (the old `expand=1` path leaked the factory CC onto extractors). The
P1→P0 planet type comes from the slot's `best_planet_type`.

**Extractor template = always 10 heads; basics scale.** `generate_extractor_layout` keeps all
10 extractor heads (full P0 extraction, matching the planner's flat 48k P0/cycle model) and
scales **only** the basic (P1) factory count down to fit a lower CC (8→6→3→1 at CC5→4→3→1).
`generate_layout` passes `cc_level` into the tier-1 path (it previously dropped it, so extractor
templates always came out CC5). **Factory** planets still scale by CC — the packed facility
count (`component_factory_rate`/`_packed_rate` → `generate_layout` `max_count`) drops at lower
CC, so the planner places more factory planets. A genuinely impossible combo (e.g. Storm Ø30000
at CC1) still overflows the grid — that's the physical reason B/T is the default.

**On-planet refining cap (basics/8).** An extractor planet's P1 output isn't just extraction — its
Basic Industry Facilities convert P0→P1, and 8 basics = full conversion of a 100%-quality planet
(the 48k baseline). Fewer basics fit on a low-CC or big planet (`fitted_extractor_basics(type, cc)`
in layout.py, cached), so it refines proportionally less. The supply-limited throughput now uses
**min(quality, basics-factor)** per slot (`_basics_factor` in planner.py; `_ext_actual_p0_per_day` /
`_actual_p0_per_day_by_p0` take a `cc` arg) — whichever of extraction richness or on-site refining
binds. Single-product `_build_char_list` switched to `with_ccu=True` and the assignment carries
`effective_ccu` so the cap uses each toon's CC. First cut: this only adjusts the **reported
effective output** (supply_ratio / effective_products_per_day), NOT the budget — the planner doesn't
yet place *more* extractor planets to compensate. Split legs aren't capped (edge case). A possible
mitigation that lifts the cap: drop the separate storage facility and buffer P0 in the launchpad
(frees ~700 PG → often restores a basic on big planets) — not built.

**Heads cost PG by distance (planet size).** `HEAD_COST` is only the flat part; extractor heads
attach to hotspots spread across the planet via spokes whose CPU/PG scale with distance like
links. `compute_resources` adds `HEAD_SPOKE_PLANAR (0.095) × radius` km of spoke per head, so a
big planet (Gas Ø40000, Storm Ø30000) makes each head far costlier — calibrated to a real Gas CC5
build (~835 PG with 9 heads). Effect: on a Gas planet the template **drops a basic (8→7)** so all
10 heads fit (18.5k/19k PG) instead of the old flat model claiming 10 heads + 8 basics fit (it
didn't — you'd run out of PG on the 10th head in-game). Small planets (Ø6000–8000) are barely
affected. This only changes the **exported template** (basic count), not the planner's flat 48k
production model.

**Per-character CCU** defaults to each toon's real ESI Command Center Upgrades skill
(`command_center_upgrades`, skill id 2505, fetched in `esi.py`); `_build_char_list` uses the
config override if set, else the ESI skill (≥1), else 5. The Setup→Character Roles CCU dropdown
is a what-if override, not the source of truth.

### Factory Layout generator (`app/layout.py`)

Separate feature from the multi-character planner. Pick any product **P1–P4** and get
ONE importable EVE PI template (you replicate it across as many planets as you want —
they're identical, so there is no "count"):
- **P1** → `generate_extractor_layout`: a P0→P1 extractor planet (ECU with N heads →
  Basic Industry Facilities → launchpad), replacing the DalShooth miner templates.
- **P2/P3/P4** → a self-contained factory planet (P1 imported, chained up to Px).

`POST /api/layout {type_id, planet_type, launchpads}` dispatches by tier (launchpads
default per tier via `default_launchpads`: **1 for extractors, 3 for factories**);
`GET /api/layout/download?...` returns one template as a named `.json` attachment;
`GET /api/layout/bundle?type_ids=ID[:lp],...&expand=0|1` returns a **ZIP** of templates
(tokens may carry per-id launchpads; `expand=1` adds the whole P0→P1 chain for each id —
used by the planner's "PI Templates (.zip)" button). `/api/pi-products` returns tiers 1–4.
Frontend tab "Factory Layout" is a **searchable multi-select** (datalist) — add several
products, each rendered as a card with its own launchpad control + download, plus a
"Download all (.zip)" bundle. The SVG preview plots **raw lat/lon, longitude flipped**
to match the in-game orientation (EVE's coordinate plane is flat — no cos(lat)).

**Planet restriction:** Advanced Industry Facilities exist on every planet, so P2/P3
factories run on any planet. Only the **P4 High-Tech Production Plant** is Barren/
Temperate-only (coerced). Extractors are restricted to planets that yield the P0
(`_p0_planets` via `PLANET_P0_MAP`). (Reference for the design: a hand-built SHPC factory; the
DalShooth/EVE_PI_Templates repo was format reference only — its templates are
single-tier-per-planet and not what this generates.)

- **Architecture: one planet does the whole chain.** Tree topology
  `Launchpad ── P4 plant ── P3 facility ── P2 facilities`. P1 is imported into the
  launchpad and routed *down through* the P4/P3 pins to the P2 facilities (pins pass
  through commodities they don't consume); each tier's output routes *up* to the next
  (P2→P3→P4); the final product routes back to the launchpad.
- **GOTCHA — pin `S` is the produced item's TYPE ID, not the SDE `schematic_id`** (e.g.
  9832 Coolant, 2872 SHPC). Verified against real in-game templates. `S=null` on
  launchpads. Getting this wrong makes the template import with the wrong recipes.
- **Compact ratio (constants in `layout.py`):** `P2_PER_P3_INPUT=2` (balanced: 2×5/hr =
  10/hr = one P3's consumption), `P3_PER_P4_INPUT=1` (the P4 then runs at partial rate,
  ~0.5 SHPC/hr — the standard one-planet tradeoff; a fully balanced P4 line is ~31
  facilities and won't fit one CC-5 planet). A P4 factory = 1 P4 + 3 P3 + 12 P2 = 16
  facilities. Matches the SHPC reference facility/schematic distribution exactly.
- **Launchpads = P1 input buffer.** `LAUNCHPADS_PER_FACTORY=3` (each holds 10,000 m³ →
  30,000 m³ preload), request field `launchpads` (1–8). LP0 is the hub (links to the P4
  root); the rest chain to LP0. Every P2 facility's P1 inputs are routed from **all**
  launchpads (path `[LP_k, LP0, P4, P3, P2]`, Q = input qty) so they drain together —
  EVE PI is pull-based, so the facility still pulls only its recipe amount. A 3-launchpad
  P4 factory = 19 pins / 18 links / 88 routes.
- **Chained topology + symmetric geometry (link CPU/PG ∝ link length).** Each
  instance has a `link_parent` (physical neighbor) separate from its `consumer` (who
  eats the output). A P3's P2 inputs are built as `m` parallel **columns** (m =
  facilities per input = 2): the first input type links to the P3, and every later
  input type *chains linearly* onto its column's tail — so a column is `P3 → type0 →
  type1 → type2`, staying one facility wide whether the P3 has 2 OR 3 P2 inputs (don't
  fan a primary out to multiple secondaries — that produced 4-wide bushy arms and
  overlaps). P4's P3 inputs use m=1 and link straight to the P4 (separate branches).
  `_layout_product` places the branches (cardinal-arm layout below); `_path` (BFS on
  the link tree) resolves the multi-hop route paths.
- **Geometry: EVE's planet space is a FLAT (lat,lon) plane — NO cos(lat).** Measured
  from the hand-built factory: the minimum pin separation is a fixed `~0.0120` in raw
  `sqrt(dLat²+dLon²)` (the tightest pairs all land on 0.0120); great-circle distances
  are *not* the constraint. An earlier cos(lat) "correction" spread longitude ~3× and
  scattered the layout in-game — don't reintroduce it. `_to_latlon` is a plain planar
  offset; `MIN_SEP=0.0124` and `_enforce_min_sep` (symmetric-push relaxation) keep every
  pair ≥ the floor. The SVG preview plots raw lat/lon with equal aspect, so it matches
  the in-game shape. Link crossings are fine (the reference file has them too).
- **Cardinal-arm layout (matches the hand-built factory).** `_layout_product`: P4 at
  centre; the 3 P3 branches go straight out along cardinal directions (right/up/down —
  `_branch_dirs`), the **left arm is reserved for the launchpad column**. Within a branch
  each tier steps `STEP=0.0135` further out; the paired primary P2s straddle the axis by
  `STRADDLE=0.0144` and each chained secondary sits directly beyond its primary.
  Launchpads stack vertically in the reserved arm (hub LP0 in the middle).
- **CPU/PG budget model (`compute_resources`).** EVE values (not in our SDE, hardcoded):
  command-centre budget per level (`CC_BUDGET`; CC5 = 25,415 CPU / 19,000 MW), per-structure
  cost (`STRUCT_COST`), `HEAD_COST` per extractor head, and link cost = `(15+0.2·km, 10+0.15·km)`
  where km = raw-planar pin distance × planet radius. **The budget is fixed by command-centre
  level, NOT planet size** — planet size only changes link length. `generate_layout` finds the
  largest `count` that fits (`max_count`); `count=None` defaults to it; the UI lets you exceed it
  and shows the per-card resource line red ("OVER BUDGET"). Validated against the hand-built SHPC
  file (22 pins → CPU 84% / PG 81%).
- **`count` packs N production units onto ONE planet sharing the launchpads** (request
  field `count`; `build_factory_template(n_trees=)`): NOT separate planets. count=1 uses
  the cardinal layout above (unchanged); count>1 uses `_radial_unit_layout` — each unit
  radiates from the shared central launchpad hub in its own sector. For P2, count = number
  of flat P2 facilities (`build_flat_p2_template`). Output/imports in the summary are
  totals for the count. Frontend per-card "Factories"/"Chains" control refetches and the
  preview redraws (the layout genuinely grows). Extractors ignore count.
- **Pipeline:** `_build_instances` (facility tree, `link_parent`+`consumer`) →
  `build_factory_template` (pins, links from `link_parent`, `_path` multi-hop routes) →
  `generate_layout` (N identical factory planets; P2 products use the flat
  `build_flat_p2_template`, launchpads + ≤12 facilities, packed across planets).
  `_effective_output_rate` accounts for the throttled P4. Reuses `sde.load_pi_data`.
- **Structure type IDs are planet-specific**, resolved from the SDE by name
  (`_structure_ids`): e.g. Barren Launchpad 2544, Barren Adv Industry Facility 2474,
  Barren High-Tech Production Plant 2475. Facility role by output tier: P2/P3=Advanced
  IF, P4=High-Tech Production Plant. **P4 plants only exist on Barren & Temperate** — a
  P4 request on any other planet type is coerced to Barren.
- **Template JSON shape:** `{CmdCtrLv, Cmt, Diam, Pln, P[pins], L[links], R[routes]}`;
  pin indices in `L`/`R` are **1-based**; a route `P` is the pin path (source first,
  dest last) and must be a valid walk along links.
- Frontend (`planetary.js`, appended block): `onLayoutTabOpen`/`generateLayout`, summary
  (output/hr, P1 to load per launchpad) + per-planet cards with a tier-colored tree SVG
  preview and Download .json / Copy JSON buttons. `_layoutTierMap` (from `/api/pi-products`)
  colors SVG pins by tier.
- **Not yet modeled:** intermediate storage facilities (the SHPC reference adds 3
  storage facilities on top of the 3 launchpads to buffer P2/P3 intermediates via
  storage round-trips — this generator routes intermediates tier-to-tier directly), and
  CPU/PG is not simulated.

Frontend: `static/planetary.js` (wizard + plan render), `static/index.html` (bump `planetary.js?v=N` on every JS change — browsers cache aggressively). Deploy of static-only changes can be a `docker cp` into the container, but always `docker compose build && up -d --force-recreate` to bake in before calling it shipped.

**Always run `test_distribution.py` against the container after planner changes** (see Testing) — this was repeatedly the difference between "looks done" and "actually correct."

---

## Testing

### Debug endpoint

The planner exposes a `/api/debug/plan` POST endpoint that runs the full planning algorithm and returns a distribution analysis. It requires:
- `DEBUG_PI=1` set in the container environment
- `DEBUG_CONTEXT_ID=<id>` to bypass cookie auth (use the context_id from `pp_characters`)

Example:
```bash
curl -s -X POST http://localhost:8000/api/debug/plan \
  -H "Content-Type: application/json" \
  -d '{
    "type_id": 2872,
    "overproduction_pct": 20,
    "use_existing": true,
    "constellations": ["L7-RDZ", "TPB-KG"],
    "preferred_systems": 2,
    "chosen_systems": ["0-U2M4", "PVF-N9"],
    "factory_system": ""
  }'
```

The debug endpoint returns per-P0-type distribution analysis: expected vs actual extractor counts, whether the distribution is within acceptable rounding tolerance, and any out-of-system assignments.

### Inspecting the database

Saved profiles are in `pp_profiles`. Config (per-character planet/extractor limits) is in `pp_plan_config`.

```bash
docker exec eve-pi-planner-web-1 python3 -c "
import sqlite3
con = sqlite3.connect('data/sde.db')
con.row_factory = sqlite3.Row

# List profiles
for r in con.execute('SELECT id, name, type_id, overproduction_pct, constellations FROM pp_profiles'):
    print(dict(r))

# Show per-character config for a product
for r in con.execute('SELECT * FROM pp_plan_config WHERE product_type_id=2872'):
    print(dict(r))
"
```

### Running the planner directly

```python
import sys; sys.path.insert(0, '.')
from app.planner import PlanRequest, _run_plan

req = PlanRequest(
    type_id=2872,
    overproduction_pct=-8,
    use_existing=True,
    constellations=['L7-RDZ', 'TPB-KG'],
    preferred_systems=2,
    chosen_systems=['0-U2M4', 'PVF-N9'],
    factory_system='',
)
result = _run_plan(req, context_id=1)
for a in result['assignments']:
    print(a['character_name'], 'ext=', len(a['extractors']), 'fac=', a['factory_planets'])
```

The existing `test_distribution.py` script tests distribution correctness against the debug endpoint.

---

## Planning Algorithm

### Extractor slot distribution (Bresenham / proportional)

P1 materials are required in different ratios depending on the product. Example for SHPC:
- 3 materials require 160 P1 units each (higher weight)
- 6 materials require 80 P1 units each (lower weight)

Extractor slots are distributed proportionally using a Bresenham-style accumulator so every slot assignment strictly follows the ratio — no drift over many slots. Heavier materials get more extractors.

**Overproduction and extra slot assignment**

When overproduction is configured (e.g. 10%), the extra extractor slots beyond what factories strictly need are also distributed proportionally — but the *last* extra planet (if there is one to spare beyond a clean multiple) should be assigned to the P0 material type with the **lowest average planetary value** in the chosen systems. A low-value planet produces fewer P0 units per cycle, so assigning the "bonus" slot there compensates for that type's lower output and brings total extraction closer to balance.

Example: 8 chars per material type for the high-weight group and 4 per type for the low-weight group. If there is one overproduction slot left over after proportional fill, assign it to whichever P0 type has the weakest available planet (lowest `value` in `pp_planets`), rather than to an already-strong type.

### Overproduction %

The overproduction input is a **baseline** overproduction relative to extractors running at 48,000 P0/cycle (the EVE PI reference rate for a full-bar planet). This is what the formula uses and what is reported back in the stats bar — so the reported % closely matches the input value.

Actual extraction rate depends on planet richness (stored as a 0–100+ value in `pp_planets`; 100 = full bar ≈ 48,000 P0/cycle). The P0/cycle stat in the plan result shows the quality-adjusted actual extraction vs the required rate separately.

### Factory output rate

The number of factories is derived from the overproduction target and the available extractor slots via an equilibrium formula so that all planet slots are used (no idle slots). The formula assumes a factory reference output rate.

For SHPC (P4), a single factory produces approximately **0.5 units/hour** (accounting for the full P2→P3→P4 processing chain). The SDE rate `cycles_per_day × output_qty` only reflects the *final* step's cycle and over-counts P4 throughput (8 factories / 192/day instead of ~14/168).

**Factory rate is auto-derived (no UI field).** `_run_plan` computes `effective_fph`: a user override (`PlanRequest.factory_output_per_hour`, kept for API/profile/share compat) wins; else **P4 → 0.5/hr**, else the SDE per-hour rate (`output_qty × 3600/cycle_time`, unchanged for P1–P3). It's passed into both `_compute_slot_budget` and `products_per_day` so factory count and products/day always agree (the manual "Factory rate (u/hr)" wizard field was removed). Reported in stats as `effective_factory_output_per_hour`.

- **Comma gotcha:** `parseFloat("0,5")` returns `0` in JS. `_factoryRate()` (now returns null since the field is gone) handled `,`→`.`; keep using it if the override field is ever re-added.

### Supply-limited throughput (plan stat)

`products_per_day` = `prod_per_factory_day × factories` — it assumes the factories stay
**fed at 100%**. When extraction can't keep a resource supplied (thin planets, an
over-aggressive `min_density_pct`, or too few extractor planets) the real output is lower.
`_run_plan` finds the **binding resource** = the one with the lowest `actual P0/day extracted
÷ P0/day the recipe needs` (`_actual_p0_per_day_by_p0` per resource — handles split legs;
need per P0 = `p1_fracs[pid] × products_per_day × 150`, the same 150 P0/P1 basic-industry
ratio `p0_per_day` uses). It reports `supply_ratio` (capped 0–1), `bottleneck_p0`,
`supply_limited` (ratio < 0.995), and `effective_products_per_day` /
`effective_isk_per_day` = nominal × ratio. The bottleneck is **per-resource, not aggregate**
— an over-produced resource can't mask a starved one (which the aggregate
`_actual_p0_per_day / p0_per_day` would). Only computed when planet quality data exists (else
actual defaults to baseline → ratio 1 → no discount). UI (`renderFinalPlan`): when
`supply_limited`, the units/day + ISK/day tiles show the effective number in amber with
"N% fed, capped by <resource>" and the if-fully-fed figure in the tooltip. The **fuel-block
planner** (`_run_fuelblock_plan`) reports the same fields (binding resource caps blocks/day;
the block-gross tile is discounted by `supply_ratio` in the UI too). The OG share meta still
shows nominal. Each P1 requirement also carries `units_per_day` (= products/day × P1-per-
product) — the refill tool (`_buildPlanSnapshot` → snapshot `consumption` map) uses it to show
"≈ N days of production" from a pasted P1 stash (min over P1 of have ÷ units_per_day).

### Factory-planet refill cadence (plan stat)

`_run_plan` reports `factory_refill_hours` — how long a factory planet's P1 input buffer
lasts before a refill. Model: factory planets import **P1** (0.38 m³) into launchpads
(assumes **3 launchpads = 30,000 m³**, matching the Factory Layout default); consumption
= `products_per_day × Σ p1_fracs / factories × 0.38 m³`. Shown in the plan stats bar
("refill / factory (3 LP)"); also `factory_input_m3_day`, `factory_launchpads_assumed`.

### Character config

Characters can be configured per-product in `pp_plan_config`:
- `planet_limit = 0` — exclude this character from the plan entirely
- `extractor_limit = N` — character dedicates N planet slots to extraction; remaining slots are factory-eligible
- `extractor_limit = 0` — character is factory-only (no extraction)
- No entry — character defaults to all-extractor (extractor_limit = None)

When no characters have extractor_limit configured (all None), the planner enters **auto mode**: it computes an even factory/extractor split across all characters and consolidates factory planets onto as few characters as possible to minimize transport effort.

### Factory planet consolidation

Factory planets (Barren/Temperate) are consolidated onto few characters via `_compute_factory_shares`:
- **Explicit mode** (extractor_limit configured): factories distributed evenly across designated factory chars
- **Auto mode** (no config): factories spread across the **minimum number of chars** but **evenly among those chars** (e.g. 8 factories → 4+4 across 2 chars, 15 → 5+5+5 across 3). Greedy max-packing a single char is deliberately avoided — see "factory_avoid" below.

If a character can't place all assigned factory slots (e.g. their extractor already occupies a Barren/Temperate planet in the factory system), unplaced factories overflow to the next eligible character. `pick()` in `_assign_factory_planets_to_chars` seeds `char_fac_used` with both the char's extractors **and already-placed factory planets** so the overflow pass never assigns the same planet twice.

**`per_char_fac_cap`:** a single character's factory share can never exceed the count of distinct Barren/Temperate planets in the factory system (a char can only host one colony per planet). Without this the planner produced e.g. "6 factories on 5 planets" with a reused planet.

### Factory character selection (user-steered)

`PlanRequest.factory_character_ids` (UI: per-character "host factories" button on the plan, `★ factories` when active) is a **priority list, NOT a factory-only flag**. In auto mode the chosen chars host the auto-computed factories first (spread evenly across exactly those chars); they still extract on spare slots. Overflow spills to other chars only if the chosen ones can't physically hold all factories. Stored in profiles (`factory_character_ids`) and shares (`fc` key). Earlier versions wrongly forced these chars to `extractor_limit=0` (factory-only) — don't reintroduce that.

### Scarce-planet extraction (key findings)

A P0 that grows on only one planet type which is also Barren/Temperate (e.g. **Autotrophs** → only on a Temperate planet, 01B-88 P6 in the test data) creates contention between extraction and factories. Two mechanisms resolve it:

- **`factory_avoid` / `_factory_avoid_cids`:** only chars whose factory share equals the full B/T count (`share >= per_char_fac_cap`, i.e. they need *every* B/T planet) keep those planets off their extractor candidate list. Chars with spare B/T capacity may still extract on a B/T planet. Threaded through Pass 1, both swap passes, Pass 2, and `_attach_extractor_planet_details`.
- **Idle-factory reuse (`char_nonfac_ext`):** a char's *existing* factory planets are only reserved (kept off extractor candidates) when the char actually has factory slots carved out this plan (`effective_planets > computed_ext_cap`). Pure-extractor chars repurpose idle existing factory planets — essential so all N non-factory chars can each extract a scarce P0 on their own copy of the planet (8 non-factory chars → 8 Autotrophs achievable, hitting the 2:1 ratio).
- **P0 slot cap** (`_p0_slot_cap`): caps Bresenham demand at realistic placements — `sum over chars of min(computed_ext_cap, distinct reachable planets)` — so it doesn't generate more slots of a scarce type than can be placed.
- **`ext_slots` clamp:** `ext_slots = min(formula, sum(computed_ext_cap))`. Factory-only chars (`extractor_limit=0`) have idle slots beyond their factory cap that can be neither factory nor extractor; without the clamp those become unplaceable phantom extractor slots.

---

## Region / constellation filtering

The wizard can filter the available constellations by **region** (e.g. "Perrigen Falls").
`scripts/populate_geo.py` builds three optional tables from Fuzzwork's small CSVs (no full
SDE download): `constellations(name, region)`, `system_geo(system, constellation)`, and
`system_jumps(system, neighbour)` — adjacency from `mapSolarSystemJumps.csv` (both
directions, indexed on `system`) for "which systems neighbour each other".

**Neighbour-aware system suggestions:** `_system_recommendations(..., max_jumps)` ranks
multi-system combos that cluster within `max_jumps` first (prefer-but-fall-back: combos
beyond N jumps still appear, sorted after). Each rec carries `within_jumps` + `jumps`
(cluster diameter). Built on `system_jumps` (BFS per candidate up to `max_jumps`); the
constellation filter still applies first (candidates are constellation-scoped). Wired as
`PlanRequest.max_jumps` (default 1) → profile column `max_jumps` + share key `mj`; UI
"Max jumps" field shows only when Systems ≥ 2 (`ppToggleMaxJumps`), and the recs step
shows an `adjacent`/`N jumps` badge plus a fall-back note when nothing fits within N.
**Planet DB import is simplified by these:** Constellation and Type columns are now
optional — `import_planets` fills constellation from `system_geo` by system name, and
infers planet type from which P0 columns the row fills (matched against `PLANET_P0_MAP`,
which has a unique P0 set per type). `_col` only uses positional fallback when there's no
header row (column-map mode requires explicit headers). `GET /api/constellations` returns
`{constellations, regions}` (region map; empty if the table is missing). The wizard is
**region-first** (`loadConstellations`/`renderConstellations`): a region dropdown lists the
regions present in the Planet DB (with counts), and only the *chosen* region's constellation
checkboxes are rendered — large multi-region Planet DBs were too slow to render all at once.
Choosing a region (`onRegionChange`) selects all its constellations (then fine-tune by
unchecking); selection is a Set persisted in `localStorage` (`ppConstellations` + `ppRegion`)
and restored from profiles/shares via `_applyConstellationSelection`. **Re-run
`populate_geo.py` after any SDE rebuild** (rebuilding `data/sde.db` drops the geo tables).

## Shared plan links + rich previews (Open Graph)

Plan shares are server-stored in `pp_shares` (`POST/GET /api/pp-shares`, payload incl.
`pn` product name + `plan.stats`). Links are now **path-based** `/s/<id>` (was the hash
`#s=<id>`). The hash fragment is never sent to servers and crawlers don't run JS, so the
old links could not unfurl. The `GET /s/{id}` route in `app/main.py` (registered **before**
the `StaticFiles` mount at `/`) serves `static/index.html` with injected Open Graph +
Twitter meta (title = product name, description = `products_per_day · isk_per_day ·
factories · systems` via `_share_meta`/`_fmt_isk`) so Discord/Messenger/Slack show a
preview, plus `<script>window.__SHARE_ID__=…</script>` so the SPA restores the plan.
`_tryRestoreFromHash` (planetary.js) reads the id from `window.__SHARE_ID__`, the `/s/<id>`
path, **or** the legacy `#s=` hash (old links still work). Missing/invalid id → generic
site meta, SPA loads normally. **Icons/preview image:** `static/favicon.png` (32),
`apple-touch-icon.png` (180), `icon-512.png`, and `og-image.png` (1200×630, logo on a dark
card) — generated from `~/Claude-Workspace/logo.png` via Pillow corner flood-fill (not a flat
colour key: the bg was a near-uniform gray ~#E0E4E8 but the hexagon interior is dark, so a
global key would punch holes; flood-fill + 1px alpha erode removes the AA fringe). `index.html`
carries the favicon links + generic `og:image`/`og:title`/`twitter:card=summary_large_image`;
the `/s/{id}` route injects per-share OG **right after `<head>`** (so its title/description/image
precede the generic ones — crawlers take the first) and points `og:image`/`twitter:image` at
`{base}/og-image.png`.

**Share privacy (opsec).** A shared link that spreads is an opsec leak: it would reveal the
owner's character names, systems and planets — enough to find them with in-game locator
agents. So `POST /api/pp-shares` takes `anonymize: bool = True` (**safe default**). When
true, `_anonymize_share_payload` relabels everything locatable *before* storing, so the DB
never persists names for anon shares. It's a **two-pass** walk (collect → apply) keyed on
field names (`_SHARE_SYS_STR/_LIST`, `_SHARE_CONST_*`, `_SHARE_CHAR_NAME/ID/_LIST`):
systems→`System A/B…`, constellations→`Constellation A…`, characters→`Pilot N`, char
ids→`char N`, consistently. The second pass also remaps **system-valued dict keys** (e.g.
`factory_capacity` is keyed by system name) — a single-pass field scrub would miss those.
The result carries `anon: true`; the SPA shows a `.pp-anon-note` banner on restore. The OG
preview only ever uses counts/economics, so it's safe for both modes. UI: two buttons —
**Share (anon)** = `wizardShare(false)`, **Share full…** = `wizardShare(true)` (confirm()
warning, sends `anonymize:false`, stores real names — for trusted recipients only). When
adding new plan fields that hold a system/constellation/character, add their key to the
`_SHARE_*` sets or they will leak into anon shares.

## Bug reporting (`app/bugs.py`, `pp_bugs` table)

Any logged-in pilot can file a report; only admins read/triage them. **Admin = account owns
a character whose name is in `esi.ADMIN_CHARACTERS`** (permanent bootstrap set, `{"ekaoni"}`)
**OR in the `pp_admins` table** (DB-backed, managed from the Admin tab). EVE names are globally
unique and SSO-verified, so a name match proves ownership — no separate admin auth.
`esi.is_admin(pp_session)` checks the session context's characters against the bootstrap set ∪
`pp_admins` (`_db_admin_names`); `esi.require_admin` is the 403 dependency. `is_admin` is
surfaced in `GET /api/characters` so the SPA shows the Admin tab/button. Bug endpoints: `POST
/api/bugs` (login-gated), `GET /api/bugs` (admin, `?status=`), `POST /api/bugs/{id}/status`
(admin, status ∈ `open|complete|ignored`). UI: header **Report bug** opens `#bugModal`; the
bug **list/triage moved into the Admin tab** (`loadBugs`/`renderBugs`/`filterBugs`).

## Admin tab & custom baskets (`app/admin.py`)

A new top-nav **Admin** tab (`#tab-admin`, shown only when `is_admin`; `app.js` `switchTab`
→ `onAdminTabOpen`) hosts three sections: Custom baskets, Admin users, Bug reports.

- **Admin users** (`pp_admins` table; `ensure_admin_table` in `esi.py`): `GET/POST/DELETE
  /api/admins` (admin-gated). Bootstrap names (`ADMIN_CHARACTERS`) show as "permanent" and
  can't be removed; everyone else is add/remove from the UI.
- **Custom baskets** (`pp_baskets` + `pp_basket_items`): a basket is a named set of PI
  commodities (P1–P4) + per-run quantities, planned by the **same engine as the built-in fuel
  block**. `GET /api/baskets` is **public** (the wizard lists baskets in the product picker);
  `POST/PUT/DELETE /api/baskets` are admin-gated. Each item is validated as a real PI type at
  tier ≥ 1.
- **Per-character config sentinel:** a basket's `pp_plan_config` rows are keyed by
  `BASKET_CONFIG_BASE + basket_id` (`BASKET_CONFIG_BASE = 2_000_000_000`, above real type_ids
  and the fuel-block `4312`). This sentinel is stored as the product `type_id` in profiles/
  shares, so they encode the basket with **no extra field** (`_basketIdFromTid` derives it).

**Engine generalization** (`fuelblock_planner._resolve_target_basket`): `FuelBlockPlanRequest`
gained `basket_id`. `None` → built-in fuel block (`resolve_bom`, ME applied, racial block
pricing, `BLOCKS_PER_RUN`=40). Set → custom basket (`fuelblocks.resolve_basket_components`
from the DB, **no ME**, ISK = Σ component market value/day, `run_size`/`unit_label` from the
basket). `_compute_fuelblock_budget` / `_assign_fuelblock_factories` are reused unchanged. The
plan result carries `unit_label`; `renderFinalPlan` uses it instead of hardcoded "fuel blocks".

UI (`planetary.js`): `_wiz.basketId` (null = fuel block); `_refreshBaskets` keeps the picker
options live after admin edits; the manufacturing-ME card shows only for the built-in fuel
block (`builtinFb = _wiz.fuelblock && !_wiz.basketId`).

## Two different "counts" — do NOT conflate

- **5 = P0 resources per planet *type*** → `PLANET_P0_MAP` (planetary.py). Each type's
  5-set is unique (verified vs EVE University), so a planet's resources identify its type.
  Used for: import type-inference, the `planet_types` display label, and extractor-template
  planet selection. **Not** used for extractor/factory *assignment* — that reads the
  per-planet richness columns in `pp_planets`. (Historically this map wrongly had 6 each,
  which broke import type-inference; corrected to 5.)
- **6 = max planets per character** → `max_planets = 1 + interplanetary_consolidation`
  (the character's skill, from the DB). Unrelated to `PLANET_P0_MAP`. Don't change one
  thinking it's the other.

## Access control

The Planet DB (`pp_planets`) is a single **global, shared** table (no `context_id`). Reads
(`GET /api/planets`, `/api/constellations`) are open; **writes require a login** —
`POST /api/planets/import` and `DELETE /api/planets` use `Depends(require_context)` (from
`app.esi`), which 401s without a valid `pp_session`. Everything else (`pp_characters`,
`pp_profiles`, `pp_shares`, `pp_plan_config`) is per-`context_id` and session-gated.

**Contribution review queue (`pp_planet_submissions`).** Only **admins** write to `pp_planets`
directly. `POST /api/planets/import` branches on `esi.is_admin(pp_session)`: admins →
`_write_planet_rows` (live, **merging upsert** — `INSERT … ON CONFLICT(system, planet_num) DO
UPDATE` with `CASE WHEN excluded.col != 0` per P0 so a blank/0 cell keeps the current value and
a sparse paste never wipes good data; returns `{queued:false,...}`); everyone else →
the paste is stored verbatim in `pp_planet_submissions` (status `pending`) and nothing touches
the live DB (`{queued:true, submitted:N}`). Parsing was split out of `import_planets` into
`_parse_planet_rows(text, con) -> (rows, skipped, errors)` (no writes) + `_write_planet_rows(con, rows)`
so both the direct path and approval reuse it. Admin review endpoints (admin-gated via
`require_admin`): `GET /api/planet-submissions?status=pending` (re-parses each `raw_text` for a
preview, flags each planet `exists` new-vs-overwrite against live `pp_planets`),
`POST /api/planet-submissions/{id}/approve` (re-parses + `_write_planet_rows`, marks `approved`),
`POST /api/planet-submissions/{id}/reject` (marks `rejected`, no write). UI: a **Planet
submissions** section at the top of the Admin tab (`loadPlanetSubmissions`/`renderPlanetSubmissions`/
`reviewPlanetSubmission` in `planetary.js`, new/overwrite chips); `submitPlanetImport` handles the
`queued` response ("submitted for review" vs "imported"). A **Contribute** tab
(`#tab-contribute`, static, no JS hook — `switchTab` is generic) documents remote sensing (Agency →
Resource Harvesting → Planets → Planetary Industry, hover a P0 → `Resource Density: %`), the
spreadsheet format, and the review flow.

## Profiles & shares

Both persist plan inputs. When adding a new `PlanRequest` field that a user sets, wire it into **all three**: `pp_profiles` column (+ `ProfileSave` model + save/list SQL), the share payload, and the frontend save/restore.

- **Profiles** (`pp_profiles`, per context): includes `overproduction_pct, preferred_systems, constellations, use_existing, factory_system, factory_output_per_hour, factory_character_ids`. Profiles do **not** store `chosen_systems` (a step-3 runtime choice).
- **Shares** (`pp_shares`, server-stored JSON, v2): payload keys `tid, pn, op, ps, ue, fs, fr, fc, cs, cc, plan`. `fr`=factory rate, `fc`=factory char ids, `cs`=chosen systems, `cc`=constellations. v2 stores the full computed `plan` so a link renders identically without re-running; the input keys let the recipient re-run/tweak.

---

## Key constants

| Constant | Value | Meaning |
|---|---|---|
| 48,000 | P0/cycle baseline | Extraction rate for a 100-richness planet (full bar), 1-hour extractor cycle |
| `_kk` | `48_000 × 24` | Baseline P0 per day per extractor slot |
| `_cycles_per_day` | 24 | Extractor cycles per day (1-hour cycle assumed) |
| Planet value scale | 0–100+ | Raw richness from SDE; 100 = full bar; values > 100 are boosted/exceptional planets |
