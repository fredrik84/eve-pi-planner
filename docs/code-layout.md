# eve-pi-planner — code layout

Module-by-module map of `app/` and `static/`. Read this before adding a route, a module, or a
large frontend block. Back to [CLAUDE.md](../CLAUDE.md).

**Don't read this file whole.** `grep -n '^#' docs/code-layout.md`, then read the one section you
need. For the functions *inside* a module, don't read the module either — run
`scripts/symbols.sh <file>`.

What a subsystem *does* lives in the service file ([pi.md](pi.md), [reactions.md](reactions.md),
[industry-planning.md](industry-planning.md), [industry-running.md](industry-running.md),
[platform.md](platform.md)); this file only says where it is.

## Contents

| Section | Covers |
| --- | --- |
| `app/main.py` — composition only | routers, startup, page routes, the analyzer's odd corners |
| `app/planner.py` and the planner family | the acyclic split: algo ← planner ← advisor ← dashboard, plus `planner_store` |
| `_run_plan` helpers and shared plan helpers | the named helpers the orchestrator calls |
| Fuel-block planner | `fuelblock_planner.py` + the BOM/ME math in `fuelblocks.py` |
| Factory planet-type filter | `factory_planet_types`, the shortage warning |
| CCU + planet-size scaled templates | the bundle token format, per-toon CC |
| Extractor templates | 10 heads where possible; basics scale down |
| `FIT_HEADROOM = 0.10` | why nothing is built to 100% of budget |
| `min_cc` and the CC ladder | "how far must I train Command Center Upgrades?" |
| Extractors are power-grid-bound | never CPU-bound; `resources.binding` |
| Storage-less extractor option | launchpad as hub, P0-led template names |
| On-planet refining cap (basics/8) | `fitted_extractor_basics`, why it must stay one loop |
| Head cost is FLAT | size moves links only — do not reintroduce spokes |
| OPEN: `PLANET_DIAM` not calibrated | prod diameters are all 0; the 2× discrepancy |
| Per-character CCU | ESI skill vs the what-if override |
| Factory Layout generator (`app/layout.py`) | the standalone template exporter |
| Frontend JS (`static/*.js`) | load order, why the split is safe, asset cache-busting |
| CSS bundles | the `style-*.css` slices and their cascade order |
| After planner changes | run `test_distribution.py` against the container |

## Code layout

## `app/main.py` — composition only

`app/main.py` is **composition only** — routers, startup/shutdown, the page routes (`/`, `/s/{id}`,
`/b/{id}`) and the `/api/*` catch-all. The original Find-Buildables analyzer (`/api/analyze`,
`/api/optimize`, `/api/share`, `/api/share/{id}`) used to hang off the app object here; it now lives
in **`app/analyzer.py`** like every other feature. Two things to know about it: its `/api/share` is
the ORIGINAL inventory share (`app/shares.py`, its own tiny store), unrelated to the plan shares in
`pp_shares` that `/s/{id}` serves — similar names, different features; and `/api/optimize` is the
only caller of `highspy`+`numpy` (~55 MB of the image), lazily imported inside the solve so they
cost nothing at startup. Retiring the feature = delete `analyzer.py`, `pi.py`, `optimizer.py`,
`shares.py` and those two requirements.

## `app/planner.py` and the planner family

`app/planner.py` is **plan orchestration** — `_run_plan`, `/api/plan`, `/api/debug/plan`,
`/api/pi-lifetime`, and the shared plan math (P1 requirement tracing, `_effective_fph`,
`_factory_refill_hours`, `_char_footprint`, `_p0_available_by_char*`). It was ~3,900 lines
carrying four unrelated jobs; the other three now live in siblings, imported **one way only** so
the chain is acyclic:

```
planner_algo  <-  planner  <-  planner_advisor  <-  planner_dashboard
```

- **`app/planner_algo.py`** — the assignment algorithm: bipartite feasibility
  (`_max_matching_slots`/`_can_add_p0`), `_compute_slot_budget`, `_compute_factory_shares`,
  `_build_need_list`, the extractor passes (`_assign_extractors`, `_run_swap_pass`, `_absorb_remaining`,
  `_waterfill_new_slots`), split extractors, `_assign_factory_planets_to_chars`, and the shared plan
  helpers `_build_char_list`/`_factory_candidates`/`_run_extractor_pipeline`. **A LEAF — it imports
  nothing from `planner.py`.** Keep it that way: its helpers take `con`/`req`/char lists as
  arguments rather than reaching for request state, which is what makes the algorithm testable
  without a session, and is what keeps the split acyclic.
- **`app/planner_advisor.py`** — advice about an EXISTING setup: `/api/analyze-placements`,
  `/api/factory-fit`, `/api/my-setup-plan` (+`derive_setup_plans`), `/api/skill-roi`,
  `/api/redeploy-candidates` (+ the reseat geometry), `/api/expansion`, and the layout caches
  (`_layout_cache_get_or_compute`, `_UNITS_PER_PLANET`, `_FACTORY_FIT`, `_FACTORY_PACK_MAXDIAM`).
- **`app/planner_dashboard.py`** — `/api/dashboard` (a ~400-line read-only aggregation) plus
  `_pad_fill_meter`. It computes no plan; it reads state and re-groups what `compute_alerts`
  and the advisor already produce.

Each of the three has its **own `APIRouter`**, mounted by `main.py`. The CRUD half — per-character
plan-config, `pp_shares`, profiles, plan snapshots and colony flags — is in
**`app/planner_store.py`** (same pattern, its own router); `planner_models`,
`planner_serialization` and `planner_recommendations` are unchanged. Add a new saved-plan field in
`planner_store`. **Importing a planner helper from another module? Check which of these it lives in
now** — `fuelblock_planner`, `fuelblocks`, `admin` and `esi_data` all had to be repointed.

## `_run_plan` helpers and shared plan helpers

`_run_plan(req, context_id)` orchestrates; the heavy lifting is in named helpers (refactored out of one giant function):

- `_compute_slot_budget` → factory count + `_compute_factory_shares`
- `_build_need_list` → Bresenham-ordered extractor slots
- `_assign_extractors` → Pass 1 (existing) → swap → Pass 2 → post-swap; calls `_run_swap_pass`
- `_attach_extractor_planet_details` → pins existing colonies per-char, then maps new slots to concrete planets + quality via `_waterfill_new_slots` (lever 1: per-character regret assignment — places the slot with the largest best-vs-next-best gap first, so a resource whose only alternative is a thin planet wins a shared planet type over one with a good fallback; order-independent, quality-optimal per char)
- `_assign_factory_planets_to_chars` → factory planet placement + overflow
- `_max_matching_slots` / `_can_add_p0` → bipartite feasibility (a slot with a committed planet is pinned to it)

**Shared plan helpers** (in `planner.py`, used by both the single-product and fuel-block
paths): `_load_char_planet_config`, `_build_p1_info_raw`, `_fetch_planets_and_recs`,
`_factory_candidates(only_bt=)`, `_build_p1_info`, `_build_char_list(with_ccu=)`,
`_set_computed_ext_cap`, `_run_extractor_pipeline`, `_pick_factory_system`. These were
extracted to de-duplicate the two run functions (~150 lines of copy-paste).

## Fuel-block planner (`app/fuelblock_planner.py`, `app/fuelblocks.py`)

**Fuel-block planner** lives in `app/fuelblock_planner.py` (own `APIRouter`, registered in
`main.py` after `planner_router`): `FuelBlockPlanRequest`, `_run_fuelblock_plan`,
`_compute_fuelblock_budget`, `_assign_fuelblock_factories`, `_system_security`, and the
`/api/plan-fuelblock` + `/api/fuelblock-bom` endpoints. It imports the shared helpers from
`planner.py`; the only planner→fuelblock dependency is a **local import** inside `debug_plan`
(avoids a circular import). The BOM/ME math (`resolve_bom`, `compute_basket_p1_reqs`,
`resolve_rig`/`sec_band`, `me_keep_factor`, `apply_effective_me`) is consolidated in
`app/fuelblocks.py`.

## Factory planet-type filter

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

## CCU + planet-size scaled templates

**CCU + planet-size scaled templates.** The bundle token format is
`id[:lp[:count[:cc[:planet_type]]]]` (`/api/layout/bundle` in
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

## Extractor templates — 10 heads where possible, basics scale

**Extractor template = 10 heads where possible; basics scale.** `generate_extractor_layout` keeps
all 10 extractor heads (full P0 extraction, matching the planner's flat 48k P0/cycle model) and
scales **only** the basic (P1) factory count down to fit a lower CC (8→6→4→1 at CC5→4→3→2 on a
small planet). Heads are a **last-resort** lever, pulled only when even 1 basic doesn't fit (CC1/CC2,
or a big planet with expensive links) — before that we exported templates the client would
reject. The summary reports `heads_requested` alongside `heads` so the UI can say what the low
command centre cost. `generate_layout` passes `cc_level` into the tier-1 path (don't drop it —
extractor templates must scale with the toon's real CC, not default to CC5). **Factory** planets
still scale by CC — the packed facility count (`component_factory_rate`/`_packed_rate` →
`generate_layout` `max_count`) drops at lower CC, so the planner places more factory planets.

## `FIT_HEADROOM = 0.10` — nothing is built to 100% of budget

**Nothing is built to 100% of the budget (`FIT_HEADROOM = 0.10`).** Every fitting decision —
extractor basics, heads, packed factory units — and the `min_cc` advice leave ~10% of BOTH the CPU
and power-grid budgets free. Our link/head-spoke costs are an estimate off idealised pin
coordinates and the player's real placement never matches to the watt, so a template that fits on
paper and not in the client is worse than one facility fewer. `compute_resources` reports both
`over` (physically impossible, >100% — the red OVER BUDGET state) and `over_fit` (past the
headroom; buildable but tighter than we ship). **Every fitting loop uses `over_fit`.** Consequences
to know: an 8-basic extractor now needs CC5 (87% PG; at CC4 it'd be 97%), CC4 gets 6 basics and CC3
gets 4, and P2 `max_count` dropped 22→20. Changing this constant changes plan sizing — it feeds
`fitted_extractor_basics` → `_basics_factor` → throughput — so **bump `_LAYOUT_CALC_VER`** in
planner.py (v2 = the headroom change) to invalidate the 30-day Redis layout cache.

## `min_cc` and the CC ladder

**`min_cc` — the level a layout actually needs.** `min_cc_for(cpu, pg)` returns the lowest CC level
whose budget fits the draw *with headroom* (None if nothing does); it's in every `compute_resources`
result and in the extractor/factory/split summaries. Levels above it buy nothing for that template.
Note a maximal template always reports `min_cc == cc_level` (the generator packs to the budget by
design), so the actionable version for extractors is the **`cc_ladder`** in the extractor summary:
what each of CC1–CC5 fits on this planet (`heads`, `basics`, `product_per_hour`, `pg_pct`/`cpu_pct`,
`over`). That's the answer to "how far do I need to train Command Center Upgrades?" — rendered as
the clickable ladder row on extractor cards. Cheap to compute (~6ms/level); do NOT build it by
recursing into `generate_extractor_layout` (use the internal `_fit`).

## Extractors are power-grid-bound, never CPU-bound

**Extractors are power-grid-bound, never CPU-bound** (~32-45% CPU at any level, vs 87-89% PG). The
layout card leads with PG and mutes CPU on extractor cards for that reason; factories vary (a P4
chain is CPU-bound at 78%/70%). `resources.binding` says which.

## Storage-less extractor option + P0-led names

**Storage-less extractor option + P0-led names.** `build_extractor_template(no_storage=)` /
`generate_extractor_layout` / `generate_layout` / `bundle_templates` / `fitted_extractor_basics` all
take `no_storage`: the launchpad becomes the hub (buffers the bursty P0 + exports P1) instead of a
separate Storage Facility — one fewer structure (~700 PG freed), so a big/low-CC planet fits another
basic (and the 10th head powers on), at the cost of a smaller P0 buffer. Wired as
`PlanRequest.extractor_no_storage` (feeds the basics cap via `_basics_factor(..., no_storage)`) and
the **PI Templates bundle** (`/api/layout/bundle?...&no_storage=1`, also `/api/layout`
`LayoutRequest.no_storage`). UI: "Storage-less extractors" checkbox in the Setup card
(`targetNoStorage`, `_wiz.extractorNoStorage`), persisted in shares/auto-restore (`xns`), NOT in
profiles (no DB column) and NOT yet a checkbox in the Factory Layout tab. Extractor template `Cmt`
(in-game name + zip filename) leads with the **P0** you select for hotspots, then the P1:
`"Felsic Magma → Silicon (Lava)"`; split planets too.

## On-planet refining cap (basics/8)

**On-planet refining cap (basics/8).** An extractor planet's P1 output isn't just extraction — its
Basic Industry Facilities convert P0→P1, and 8 basics = full conversion of a 100%-quality planet
(the 48k baseline). Fewer basics fit on a low-CC or big planet
(`fitted_extractor_basics(type, cc, no_storage, diam)` in layout.py, cached), so it refines
proportionally less. **That function is a thin cache over `fit_extractor` — the SAME loop that
sizes the exported template**, so what the planner assumes a colony refines and what the .zip
tells you to build cannot disagree. They were two copies of the loop until 2026-08-04, and the
planner's copy had drifted: no head-drop stage, and no `diam` parameter at all, so the plan was
stuck on the planet-TYPE default while the export could already size to a real planet. Put a new
fitting lever in `fit_extractor`, never in a caller;
`test_the_plan_and_the_exported_template_fit_the_same` (test_min_cc.py) asserts the two agree
across every type × CC × diameter. Pinned extractor slots carry `diameter`, which
`_ext_actual_p0_per_day` / `_actual_p0_per_day_by_p0` pass through to `_basics_factor`.
The supply-limited throughput uses
**min(quality, basics-factor)** per slot (`_basics_factor` in planner.py; `_ext_actual_p0_per_day` /
`_actual_p0_per_day_by_p0` take a `cc` arg) — whichever of extraction richness or on-site refining
binds. Single-product `_build_char_list` switched to `with_ccu=True` and the assignment carries
`effective_ccu` so the cap uses each toon's CC. First cut: this only adjusts the **reported
effective output** (supply_ratio / effective_products_per_day), NOT the budget — the planner doesn't
yet place *more* extractor planets to compensate. Split legs aren't capped (edge case). A possible
mitigation that lifts the cap: drop the separate storage facility and buffer P0 in the launchpad
(frees ~700 PG → often restores a basic on big planets) — not built.

## Head cost is FLAT — planet size moves LINKS only

**Heads cost a FLAT 110 CPU / 550 PG — planet size moves LINKS, nothing else.** `HEAD_COST` is the
whole per-head cost; EVE charges by head count and by nothing else (EVE University: "each
additional head consumes an amount of CPU (110) and Powergrid (550)"). Between 2026-06-13 and
2026-08-04 heads were modelled as "spokes" costing `HEAD_SPOKE_PLANAR (0.095) × radius` km of extra
link, calibrated to one measured Gas CC5 build (~835 PG/head). **Don't reintroduce that** — it was
wrong in two ways at once:
- The residual PG that calibration was chasing is **link** cost we under-count on a big planet,
  because `PLANET_DIAM["Gas"] = 40000` is far below a real gas giant. Attributing it to heads put
  the size term in the wrong place.
- The invented term grew **without bound** with the radius, so the later real-per-planet-diameter
  feature fed it real sizes and collapsed the template: a Gas extractor went 8 basics → 5 at the
  Ø40000 default and → **1 basic** at a real Ø110000, while an 8-basic Gas extractor places fine
  in the client. That's the bug the user reported ("lowering the diameter gives me MORE
  factories").
Now: heads flat, size only in the link formula, so a bigger planet sheds basics gently (Gas 8 at
Ø40k, 7 at Ø110k, 6 at Ø221k). Changing this changes plan sizing (`fitted_extractor_basics` →
`_basics_factor`) — `_LAYOUT_CALC_VER` is at **v3**. Covered by
`test_head_cost_is_flat_and_size_only_moves_links` in `test_min_cc.py`.

## OPEN: `PLANET_DIAM` is not calibrated

**OPEN: `PLANET_DIAM` is not calibrated and prod has no real diameters.** The type defaults (Gas
40000, Ice 6000, Storm 30000) don't match the SDE's celestial radii under either reading of
`mapDenormalize.radius`, and every `pp_planets.diameter` in production is **0** — so
`scripts/populate_planet_radius.py` has never landed and every layout runs on the type default.
That script's `diameter_km = radius_m / 500` is also unverified: for F18-AY VIII it yields 221,320
while the client reads ~110,000, so one of the two is off by 2×. Settle it against a real in-game
exported template's `Diam` field before populating, because link PG (and therefore how much fits)
scales directly with it.

## Per-character CCU

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

## Frontend JS (`static/*.js`)

Frontend JS is split across files loaded in order from `index.html`: **`utils.js`** (loaded first — shared formatting helpers: `fmtIsk`/`_fmtIsk`/`_fmtHours`/`_fmtDHM`/`_esc`/`_fmtWalletDate`/`_fmtCacheTime`), **`app.js`** (tab nav, ESI login popup, mobile pull-to-refresh, DOMContentLoaded boot), **`planetary.js`** (the core — shared state + `_featureActive`, the PI-planner wizard + `renderFinalPlan`, Characters/header, profiles/shares, tab-entry hooks like `onPlanetDbTabOpen`), **`dashboard.js`** (Dashboard tab: overview, maintenance routine, spare-capacity, the global `rescanAll`), **`admin.js`** (Admin tab: planet submissions, feature flags, baskets, admin users, bug triage), **`planetdb.js`** (Planet DB tab: constellation/region filter, planet list + chunked table, import modal), **`refill.js`** (PI-Planner refill tool: saved-plans bar, build/refill mode, P1-stack distribution), **`analysis.js`** (Setup Analysis tab), and **`layout.js`** (Factory Layout tab). Feature files were carved out of `planetary.js` for maintainability. The split is load-order-safe because the JS is **all declarations except one top-level statement** (the `DOMContentLoaded` listener in core) — functions are global and resolve at call time, so feature files just load after `planetary.js`. When carving more out: cut only at top-level boundaries (verify each file with `node --check`), keep shared state/util in `planetary.js`, and never split a wizard/dashboard interdependency you can't trace. Asset cache-busting is automatic — `index.html` ships `?v=dev` and `app/main.py` stamps the running build's `GIT_COMMIT` onto every asset URL at serve time (`ASSET_VERSION`/`_page()`), so there is **no `?v=` number to bump** any more. Deploy of static-only changes can be a `docker cp` into the container, but always `docker compose build && up -d --force-recreate` to bake in before calling it shipped.

## CSS bundles

CSS is likewise split into `style-*.css` files loaded in order from `index.html`, sliced at the
original file's section-comment boundaries with **zero rule reordering** (each file is a contiguous
slice of what used to be one `style.css`, verified byte-identical when concatenated back together):

**`style-base.css`** (page shell/header/sidebar nav/input grid/buttons), **`style-components.css`**
(pills/warnings/tables/tier badges/pipeline summary + Planetary Planning intro),
**`style-contribute.css`** (Contribute tab + bug reporting), **`style-wizard.css`** (plan
results/wizard/shopping list), **`style-layout-admin.css`** (Factory Layout + Admin tab),
**`style-analysis-dashboard.css`** (Setup Analysis + Dashboard, incl. fill-factories meter/refill
controls/characters/schedule-sync/agenda/skill-ROI), and **`style-misc-responsive.css`** (image
lightbox/how-it-works poster/remaining Admin sections + **the final mobile `@media` block — this
file must stay last**, since its overrides target selectors defined in every earlier file). When
carving further: only cut at existing section-comment boundaries and never move a rule past a
`@media` block that shares its selectors, or the cascade order (and thus the rendered result) changes.

## After planner changes

**Always run `test_distribution.py` against the container after planner changes** (see Testing) — this was repeatedly the difference between "looks done" and "actually correct."

---
