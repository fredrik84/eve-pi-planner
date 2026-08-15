# eve-pi-planner — the PI planner

The Planetary Industry side: planning colonies, simulating them forward, and the advice on the Setup Analysis tab.
Back to [CLAUDE.md](../CLAUDE.md).

Find a section: `grep -n '^## ' docs/pi.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Planning Algorithm** — extractor slot distribution, overproduction, factory rates, supply-limited throughput, scarce planets
- **Region / constellation filtering** — how a plan is scoped to space the player actually flies
- **PI colony forward-simulation (`app/pi_sim.py`)** — the two rates per output, projecting pads forward from a checkpoint
- **Setup Analysis tab + "Current setup" demand (`/api/my-setup-plan`)** — the endpoint behind the tab
- **Setup Analysis: what the advice must do** — sustain-the-cycle policy, the fix ladder, refining-limited detection, redeploy candidates — read before changing any advice
- **Shared alert engine (`app/alerts.py`) + configurable thresholds (`app/alert_settings.py`)** — one detection engine, per-kind muting, notification prefs
- **Fill-factories meter (Dashboard)** — the dashboard meter
- **Refill "empty pads" toggle** — the m³-capped refill split
- **Refill to a deadline (`refill_deadline`)** — sizing the drop to a time the player picks, the four ceilings on it, and the two clocks
- **Dashboard "Up next" agenda** — what needs doing next, at a glance
- **Skill-ROI advisor (Setup Analysis)** — which skill buys the most output, and the already-enough case
- **Shared plan links + rich previews (Open Graph)** — `/s/{id}` and how it unfurls in Discord
- **Fuel-block performance: the regression procedure** — how to prove a fuel-block change did not regress the plan — run this before shipping one

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

**Factory rate is auto-derived (no UI field).** `_run_plan` computes `effective_fph`: a user override (`PlanRequest.factory_output_per_hour`, kept for API/profile/share compat) wins; else **P4 → 0.5/hr**, else the SDE per-hour rate (`output_qty × 3600/cycle_time`, unchanged for P1–P3). It's passed into both `_compute_slot_budget` and `products_per_day` so factory count and products/day always agree. Reported in stats as `effective_factory_output_per_hour`.

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
lasts before a refill. Model: factory planets import **P1** (**0.19 m³/unit** — verified
in-game; do NOT use 0.38, that doubled consumption and halved the interval) into launchpads
(assumes **3 launchpads = 30,000 m³**, matching the Factory Layout default); consumption
= `products_per_day × Σ p1_fracs / factories × 0.19 m³`. Shown in the plan stats bar
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

## PI colony forward-simulation (`app/pi_sim.py`)

ESI's `GET /characters/{id}/planets/{planet_id}/` reports stored contents only as of the colony's
last server checkpoint (`last_cycle_start`) — it does NOT stream live launchpad amounts; the
in-game client (and tools like Rift) reproduce them by **simulating production forward** from the
checkpoint. `pi_sim.colony_sim_state(detail, pi_data)` builds an aggregate-flow state from one
planet's ESI detail: output rate = `min(extraction P0/sec, factory P0-capacity/sec)` converted via
the schematic ratio; `project(state, now)` = checkpoint contents + rate × (min(now, program expiry)
− checkpoint t0). Extractor planets only (ECU present); factory planets that import P1 can't be
simulated (import schedule unknown) → fall back to the raw snapshot. Stored per planet in
`pp_char_planets.sim_state` (JSON) at scan time; **`list_characters` projects it to request time**
so the Characters tab "In pads ~est" shows live-ish values (validated: Silicon 1186 sim vs 1120
in-game, ~6% high — ignores extraction decay, so a touch optimistic). Refresh re-anchors the
checkpoint. Checkpoint tag before this: `checkpoint-before-pi-sim`.

**Two rates per output.** Each `sim_state.outputs[]` carries `rate` AND `rate_sustained`:
- `rate` = **full factory rate** (`count × output_qty / cycle_time`). The launchpad fills at this
  because extraction decay front-loads P0 and storage buffers the facilities — matches the in-game
  pad. Used by `project()` and the Characters-tab pad estimate.
- `rate_sustained` = **long-run sustainable** = `min(factory rate, extraction refined)`, using the
  install-time extraction rate (`qty_per_cycle / cycle_time`). A poor planet whose extraction can't
  keep the basics fed reports the lower extraction rate; a rich planet stays factory-limited (full
  rate). The right number for "can this colony meet a daily quota" → the **Setup Analysis** tab uses
  it (via `list_characters` per-planet `production` = `rate_sustained × 86400`). Falls back to `rate`
  for sim states scanned before it existed → a Characters **refresh** is needed to populate it.
  **Do NOT apply a decay average here:** the launchpad/storage buffers the front-loaded extraction,
  so an actively-cycled colony holds the factory rate as long as PEAK extraction covers it. A decay
  factor (tried via CCP's `1/(1+0.012·t)` curve, dogma attrs 1683/1687) under-counted every
  factory-limited colony and contradicted the in-game numbers — reverted.

## Setup Analysis tab + "Current setup" demand (`/api/my-setup-plan`)

The **Setup Analysis** tab compares **supply** (each colony's extractor `production`, P1/day from
the sim — see above) against a plan's **demand** (`consumption`, P1/day a product's factories eat),
showing per-P1 over/under, a refill cadence, and rebalance / add-factory suggestions. Demand comes
from a saved plan snapshot **or** a **derived "Current setup" profile** built from the player's own
deployed factories — so a player with PI already running gets the analysis with zero plan setup.

`GET /api/my-setup-plan` (planner.py, session-scoped via `session_context_id`): groups the
context's **configured non-extractor factory planets** (`pp_char_planets.is_extractor=0` with a
non-empty `products` = highest-tier output) **by product**, and for each returns a snapshot-shaped
profile — `consumption` (`_compute_p1_fracs(tid) × products_per_day`), `products_per_day`
(`count × _effective_fph(tid) × 24`), `factory_refill_hours`, `factories[]` (real `char · system P#`
locs), `unit_label`. **Strictly context-filtered** (join `pp_characters ON context_id`) — unscoped
queries leak other accounts' factories. Frontend (`planetary.js`): `_fetchSetupPlans()` →
prepended to `_analyzeSnaps` (marked `derived`, shown first with a ◆), and `renderAnalysis` adds a
"built from" `<details>` of the factory locs so the user can verify/spot stale data.

**Shared helpers** `_effective_fph(type_id, pi_data, override)` (P4 → 0.5/hr, else SDE rate) and
`_factory_refill_hours(products_per_day, p1_fracs, factories)` (0.19 m³/unit, 3-LP buffer) were
extracted from `_run_plan` so the planner and this endpoint can't drift (the 0.38→0.19 m³ fix would
have been one line if they'd been shared from the start). **v1 uses the flat per-product rate** —
it ignores CC level + planet size, so demand is over-stated for CC4 / big-planet factories; the v2
path is to sum per-planet `component_factory_rate(product, pi_data, planet_type, ccu)` (fuelblocks.py)
using each factory planet's stored `planet_type` + the character's `ccu`.

## Setup Analysis: what the advice must do

Every number and every fix on this tab reflects **sustaining the full production cycle**, measured on
honest refined/exported throughput (`per_day` = `rate_sustained`, the P1 that actually reaches
factories) — never raw head extraction (`ext_per_day`). The headline and the per-material drilldown
must use the same basis; they once contradicted each other ("98% fed, needs fixing" over bars all
reading "+2% healthy"), which leaves the user with no idea what to fix. Raw extraction is
diagnostics only (the avg-P0/hr admin mode, and deciding heads-limited vs refining-limited).

- **Never emit "leave it / you're only mildly tight" advice.** Always steer to full sustain plus a
  +10% decay buffer (`_HEALTHY_BUFFER = 0.10`). Prefer redeploy-to-a-richer-planet (no overshoot)
  over adding a colony, but always give a path to 100%+.
- **Suggest up to the buffer, not only when short.** `_burndownSection` surfaces any material with
  less than a +10% extraction buffer. Short = urgent/red; the rest = an optional, non-alarming
  "headroom top-up". Gating on short-only forced a one-fix-per-decay-cycle daily grind.
- **Fix ladder:** reseat > lower extraction cycle > redeploy same planet > redeploy another planet.
  "Lower extraction cycle" is a **global** lever (it lifts average P0/hr everywhere), so it belongs
  in the card footer, never as a per-colony rung.
- **Refining-limited** (`extSupply >= need` but `have < need`; `extPerDay > perDay * 1.05`): the
  colony can't refine what it pulls. The fix is on-planet refining capacity — higher CC level,
  smaller planet, storage-less extractor to free PG for another Basic Industry Facility, or split
  extraction — **not** reseating or adding heads. Detect and say so explicitly, even when extraction
  looks healthy.
- **`_reseatWontHelp`** pulls two classes out of the reseat list into a "Don't reseat these —
  <real fix>" block: refining-limited colonies, and freshly redeployed ones
  (`redeploy_at` within `_REDEPLOY_FRESH_DAYS` = 3, where the "decline" is just ramp noise).
- **Redeploy stays targeted, not noisy** — a single tail line ("if still short, redeploy a tapped
  colony to a richer planet"), never a per-colony wall.
- `_P0_PER_P1 = 150`; `redeploy_at`/`reseat_at` are epoch **seconds**.

### Redeploy candidates (`redeploy_proximity` gates the overlap half)

- **Overlap is measured on reachable footprints, not head positions or shared planets.** Two of your
  characters on the same planet is normal distribution, not a problem; overlapping *reachable areas*
  are, because the whole area depletes and reseating only moves heads within reach. Footprint =
  (centre `c` = the ECU pin's lat/lon, reach = farthest head + head radius) from
  `pp_char_planets.ext_heads`; overlap when `_gc_dist(a,b) < reachA + reachB` for the same P0 across
  characters. Threshold is client-side and user-set (`localStorage.ppHotspotOverlap`, default 50,
  Settings → General); the server returns every overlap above a 1% floor.
- **Depletion** = `pp_colony_yield.peak_day` trending down: window 6, min 5 programs, ≥15% decline
  start→current, ≤1 up-step tolerated. A thin-but-flat planet is deliberately not flagged. Reseat
  verdict: per-program decline < 8% **and** total < 45% → "a reseat still buys time"; past that,
  redeploy (`_RESEAT_GIVEUP_PER_PROG` / `_RESEAT_GIVEUP_TOTAL`).
- **Precedence:** a depleting member of an overlap cluster is the mover (it needs a fresh deposit
  anyway, and moving it fixes both). Depleting rows already covered by a displayed cluster are
  filtered out — don't double-list.
- **Urgency:** an overlap only *hurts* once it's eating yield, so clusters with a depleting member
  are "do soon" and the rest collapse into "whenever you next rebuild them".
- **Lead with a same-planet relocate for every reseat-can't-fix case.** Moving the extractor to a
  clearer area of the same planet keeps the existing command centre; dismantling for another planet
  (especially another system) is far more work and is a last resort, offered only when a richer
  planet is actually free.
- Fixes are **grouped by character** ("N fixes, one login") — you play one character at a time.
- Rejected UI: persistent localStorage "done" checkboxes read as cheap and improvised; move targets
  are chips instead.
- **Gotcha:** `/api/characters` colony objects are built in `app/esi_data.py` and are **Redis-cached**
  (`charlist_key`, busted on rescan). A field selected in SQL but missing from that dict is
  `undefined` frontend-wide — that hid the per-colony flag button and degraded redeploy matching.
  Add the field to that dict, then rescan before expecting to see it.

## Shared alert engine (`app/alerts.py`) + configurable thresholds (`app/alert_settings.py`)

**`compute_alerts(context_id, rows=None, now=None)`** is the single source of truth for
every colony warning in the app — both the Dashboard (`planner.py`'s `dashboard()`, for display)
and the notification scheduler (`app/notifications.py`, for pushes) call it and nothing else
re-implements detection. This was a deliberate unification (2026-07-10): notifications used to
have their own bespoke `_extractor_events`/`_factory_events` queries, duplicating (and able to
drift from) what the Dashboard showed. Returns a flat list of individual alert instances —
`{kind, severity, character_id, character_name, planet_id, location, hours_left, pct
(storage_full only)}` — across 11 kinds (`app.alert_settings.ALERT_KINDS`): four threshold-based
(`expired`, `expiring`, `storage_full`, `factory_refill`); four correctness-based, stored
per-scan by `app.esi._detect_colony_issues` (`ext_unrouted`, `fac_unfed`, `fac_output`,
`p0_mismatch` — always "high" severity, never muted-by-default); `schedule_sync` (an extractor
running a different program length than the fleet's norm, always "warn", computed fleet-wide via
`_extractor_program_lengths()`); and two Reactions kinds (`reaction_finishing_soon`,
`reaction_completed` — see `_reaction_alerts()`), which are not about PI colonies at all but are
folded into the same flat list so they inherit the mute/severity/dashboard/push plumbing. That
last pair is why the module is `app/alerts.py` and not `colony_alerts.py`. `factory_refill` has no
dedicated threshold fields of its own — deliberately reuses `expiring_hours` as "how far ahead to
flag" and `storage_high_ttf_hours` as the warn→high cutoff (same ideas as extraction expiry and
storage already use) rather than adding two more number fields for one kind. `dashboard()` passes
its own already-fetched `pp_char_planets` rows in via `rows=` to avoid a second query; the
scheduler (iterating many contexts) leaves it `None` and the function does its own fetch.
`dashboard()` re-groups the flat list into its existing display cards (per-character correctness
tallies, collapsed expired/expiring/factory-refill lines, the grouped storage card) — collapsed
cards derive their severity from whether **any** instance in the group is "high" (see
`factory_refill_high` in `dashboard()`), not a hardcoded value, so e.g. one imminent factory
refill escalates the whole "Factories due for refill" card even if others in the batch aren't
urgent yet.

**Thresholds and muting** (`app/alert_settings.py`) are per-account: `pp_alert_settings`
(context_id PK, one row per customizing account — no row = defaults, set to exactly what used to
be hardcoded, so nothing changed behavior until a user edits it): `expiring_hours` (3h),
`storage_warn_pct` (80), `storage_high_pct` (95) / `storage_high_ttf_hours` (2 — either escalates
a pad to "high"), `storage_urgent_hours` (3 — counted in the "(N within Xh)" header). `muted_kinds`
(JSON column, same row) lets ANY of the 11 kinds be turned off entirely, including the
correctness-based ones with no numeric threshold to tune. `get_alert_settings(context_id)` is the
single read path `compute_alerts()` and the settings endpoints all use, so they can't drift.
`GET/PUT /api/alert-settings` + `POST /api/alert-settings/reset`, all `require_context`-gated (own
account only). `ALERT_KINDS` is the key+label registry — `GET /api/alert-settings` echoes it back
as `available_kinds` so the frontend never hardcodes labels (the same registry is reused by
`GET /api/notifications/prefs` for the same reason). UI: Settings modal → **Alerts** section
(`settingsSecAlerts`, shown to any logged-in user — its flag, and the one that gated the
notifications section beside it, were both retired 2026-08-12) — 5 threshold number-inputs (`.settings-field-row`, label left/control right/hairline
divider — replaces an earlier ad-hoc inline layout that misaligned once there were several rows of
differing label length) + a "Muted alerts" 2-column checkbox grid (`.settings-toggle-grid`),
Save/Reset.

## Fill-factories meter (Dashboard)

"How far does the P1 in my extractor pads go toward filling all my factories?" Backend
`_pad_fill_meter(parsed, pi, types)` in planner.py (attached as `pad_fill` in the dashboard payload):
- **have** = P1 sitting in EXTRACTOR launchpads, per type_id, forward-projected via `pi_sim.project`
  (falls back to raw `pad_contents`).
- **need** = each factory's 30,000 m³ (3-LP) buffer split by **consumption ratio**, per material:
  `30000 × (frac/Σfrac) ÷ 0.19 m³/unit`. NOTE `_compute_p1_fracs` returns **P1-per-product recipe
  quantities, NOT fractions summing to 1** — normalise per factory (`frac/Σfrac`) or `need` blows up
  ~4000× (a real bug hit during dev). Full buffer per factory = 30000/0.19 ≈ 157,894 units.
- **fill %** = the BINDING material `min(have/need)` (you need them all); `materials[]` is the
  per-material breakdown (have/need/pct), weakest first.
Frontend (`dashboard.js`, gated by `_featureActive('pad_fill')`): a top Overview tile ("N% to fully
fill factories") + a "Fill factories from pads" card with the binding statement + per-material bars
(`.padfill-*`). Default off (admin-preview).

## Refill "empty pads" toggle

Factory launchpad contents come from the last ESI scan and are **not** simulated forward (only
extractors are), so the scanned "P1 already in the pad" goes stale and over-reports (ESI returns the
last-checkpoint contents, from before the factory drew them down — a rescan re-reads the same stale
checkpoint). The Refill tool's **"Pads emptied at drop-off"** toggle (`_refillIgnorePads`, default
ON) ignores `input_m3` and fills to a clean 30,000 m³ (3 LP), matching the usual "empty the pads when
you drop the next batch" workflow. Off = subtract the last-scan contents. m³/unit is 0.19 (verified);
the under-fill people hit was the stale `input_m3`, not the volume constant.

## Refill to a deadline (`refill_deadline`)

"Fill it up" commits the next login to whenever a full pad happens to empty. **Run dry at…**
(`_refillMode = 'deadline'`, flag `refill_deadline`) inverts that: the player names the time they
want to come back and `_deadlineSplit` (`static/refill.js`) sizes each factory's drop to land
there. Quantity = per-factory burn rate × time, then four ceilings, **in this order** — the order
is the feature, and `test_refill_deadline.js` runs the real function to pin it:

1. **What's already in the pads** (only when "Pads emptied at drop-off" is off) — split across a
   factory's inputs in ITS burn ratio, since a pad stocked to be eaten together empties together.
2. **Launchpad space** is a hard ceiling. A deadline that doesn't fit is answered with the soonest
   one that DOES (`info.capped[].hours`), never with a quantity that won't fit.
3. **The pasted P1**. With nothing pasted the requirement is shown in full — the question being
   asked is "how much do I bring", and a table of zeroes doesn't answer it.
4. **Whole runs** last, so no other ceiling can knock an amount off the step: floored to
   `units_per_run`, with the time that rounding costs reported (`info.driftH`), never hidden. The
   `1e-3` tolerance stops a deadline that lands a hair under a whole run from dropping a full one.

A factory already stocked past the deadline is **"skip it, come back in X"** (`info.skip`), not a
quiet 0 — it is not a refill you should make the trip for. When every factory is skipped the
readout says so instead of quoting a drop.

**The readout describes the table under it.** Summary hands every factory the *smallest* amount, so
it runs dry sooner than the per-factory split — `info.uniformEndurance` vs `info.endurance`, picked
by `_refillView` (which is why `_setRefillView` re-renders the readout too). The gap to the deadline
is stated without attributing it; `info.roundDriftH` (measured against the post-cap target) is the
part rounding is actually responsible for, and capacity/stock explain the rest in their own notes.

**Per-factory rate, not plan-total ÷ factories.** `p1_inputs` carries `units_per_day` and
`units_per_run` per factory (`_run_plan`, `derive_setup_plans`, `_run_fuelblock_plan`; `_p1_batch_sizes`
in `planner.py` derives the run size — always the P1→P2 input quantity, 40, whatever the final tier,
because P1→P2 is the first on-planet step). The combined "Current setup" plan sums `consumption`
across products, so once two products share a P1 a plan total and a share can't be turned back into
one factory's rate. Plans saved before this shipped fall back to plan-total × share, which is exact
for a single-product plan. `test_refill_rates.py` pins both halves.

**Two clocks, one stored.** The picker reads and writes **local** time and shows both
(`Sat 14:00 local · 12:00 EVE` — EVE time is UTC, and a fleet op is quoted in it). What's stored is
an **instant** (`refillDeadlineMs`, epoch ms): a stored local time silently means something else
after a DST change. Today that store is `localStorage`, so the deadline doesn't follow the player
across devices and nothing server-side (the Up-next agenda, `factory_refill` alerts) can quote it —
see TODO §21b.

## Dashboard "Up next" agenda

Account-level sorted list of the next maintenance tasks (Restart extractors / Haul extractor P1 /
Refill factories) with countdown + absolute clock time, on the Dashboard under Maintenance routine.
`_renderTimelineCard(t)` reuses the existing dashboard `*_due_hours` totals (no extra request).
**Deliberately NOT a single-cycle line** — extractor and factory cadences desync badly (several
extractor restarts per factory refill), so a "you are here on one timeline" viz is misleading; a
sorted agenda is honest. Public since June 2026; its flag and the "admin preview" tag it drove were retired 2026-08-12.

## Skill-ROI advisor (Setup Analysis)

`GET /api/skill-roi` (session-scoped): per character, the output gain from the next level of the two
yield skills — **Interplanetary Consolidation** (<5 → +1 planet ≈ one colony's average value/day,
from `total_value_day / total_planets`) and **Command Center Upgrades** (<5 → extra factory units
that pack onto each FACTORY planet, via `_units_per_planet` = layout-engine `max_count` at cc vs
cc+1, × per-unit value). Gain-only (no SP/train cost — the user's spend decision). Sorted by ISK/day,
top 12. Frontend: `_fetchSkillRoi()` + `_renderSkillRoiSection()` appended in `renderAnalysis`.

**The response has TWO halves — the other one says "stop training".** `enough[]` (rendered as the
"Already enough skill" card) is where a skill is already past what the character's colonies use.
PI advice defaults to "train everything to V", which is a long haul on a rank-4 skill for a level
plenty of colonies never touch. Two sources, in order:
- **Command Center Upgrades** — preferred basis is `pp_char_planets.upgrade_level`, the level the
  colonies are ACTUALLY upgraded to in-game (`basis: "deployed"`). Observed state can't over-claim:
  a player who really did upgrade to V reports V and gets no advice. Falls back to a **modelled**
  requirement (`_required_cc_extractor` / `_required_cc_factory` = lowest level fitting as many
  basics / packing as many units) for scans predating the column — that fallback is against our
  MAXIMAL archetype, so it's the conservative answer. Taken as the **max over ALL the character's
  planets, extractors included** — characters are rarely pure-factory.
- **Interplanetary Consolidation** — planet slots trained but not deployed. Related fix: the
  gain-side IC suggestion is now suppressed while `free_slots > 0`; telling someone to train a
  rank-4 skill for a slot while one sits empty is backwards.
`_units_per_planet` returns **0** (not `max_count`'s floor of 1) when not even one unit fits the
budget — otherwise the advice reads "this level runs your P4 planet" for a level that can't host it.
Covered by `test_skill_enough.py` (seeds colonies deployed below the trained level + a character
with idle slots, via a fabricated session cookie).
**Limitations (v1):** flat per-unit factory rate (same model as `my-setup-plan`); P4 factories are
1/planet so CCU shows no gain for them; **extractor-side CCU (more basics → more P0→P1 refining) is
NOT modelled yet** — the documented follow-up. Returns nothing when all characters are IC5/CCU5
(correct — nothing to train). Planetology / Advanced Planetology affect survey only, not yield, so
they're excluded.

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

## Fuel-block performance: the regression procedure

The 2026-07-06 "pressed find, stuck >30s" report is fixed (Redis-shared `packed_rate` cache in
`app/fuelblocks.py` via `_layout_cache_get_or_compute`, plus the cheap preview path `is_preview` in
`app/fuelblock_planner.py`). **Run this after touching `fuelblock_planner.py`, `fuelblocks.py`,
`layout.py`, or the layout caches** — all three checks, because they fail independently.

**A. Preview must do no factory geometry** — the fix that matters most, and the one a refactor is
most likely to silently undo. In the container:

```python
import app.planner_advisor as pa, app.fuelblock_planner as fp
calls = []
orig = pa._factory_pack_max_diameter
pa._factory_pack_max_diameter = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
# preview  = no chosen_systems  -> expect 0 calls
# full plan = chosen_systems set -> expect >0 calls (was 21 when first measured)
```

Expected **0 calls in preview, non-zero with `chosen_systems`**. A non-zero preview count means
`is_preview` stopped being threaded through and every recommendation pays full placement geometry.

**B. Cold-process cost must not return.** Restart the pod, then time the first fuel-block plan and a
second identical one:

```
ssh node01.failed.name "sudo k3s kubectl -n production rollout restart deploy/eve-pi-planner"
ssh node01.failed.name "sudo k3s kubectl -n production logs -l app=eve-pi-planner --tail=200 \
  | grep -E 'fuelblock\.(fetch_planets_and_recs|extractor_pipeline)'"
```

The first call after a restart should be close to the warm one — that is the whole point of the L2
Redis cache. A large cold/warm gap means it degraded to in-process only.

**C. Cross-replica sharing.** With 2 replicas, a plan computed on one pod should leave the other
warm: issue the same request repeatedly and confirm the timings don't alternate fast/slow.
Alternation means the Redis layer isn't being hit and each pod is caching alone.

**Regression threshold:** the original user-visible symptom was 30s. Treat anything over a few
seconds on a warm path as a regression worth tracing rather than tuning.
