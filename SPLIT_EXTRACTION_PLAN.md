# Plan — User baskets + split-extraction planning

Two loosely-coupled workstreams from one feature request: let regular players
build their own product baskets, and let the planner reuse a single extractor
planet for two P0 streams ("split P1 production") to cut planet count.

Decisions (locked):
- **Baskets:** private-only. Any logged-in user owns baskets visible only to
  their own context; admins keep the existing global baskets.
- **Split packing:** user choice per plan — **Off / Conservative / Aggressive**.
  Conservative merges only leftover/overproduction residuals when richness + CC
  budget allow, never reducing needed output. Aggressive actively minimizes total
  planet count, accepting lower per-stream output / requiring richer planets.
- **Split scope:** both single-product and basket planning paths.
- **2-ECU template + rich display:** deferred to Phase 3 (reminder todo).

---

## Mechanic model (the assumption everything rests on)

A split extractor planet = **2 ECUs on one planet**, the 10-head budget divided
between them (e.g. 6+4), each ECU on a different P0 deposit, feeding **two Basic
Industry lines → two different P1s**. Binding constraint is **CPU/PG (command-
centre level)** via `layout.compute_resources`, not heads — two ECU bases + heads
+ two basic lines is tight at CC5 and may not fit at low CC.

**Head yield is NOT a static number.** Per-head output depends on where the head
is dropped on the resource heatmap (hotspot placement) and **decays over the
extraction program** as the deposit depletes — it is not something we can compute
or rely on. So the split is expressed as a **recommended head-allocation ratio**
(`heads_A : heads_B`, summing ≤ 10) proportional to the two legs' residual P0
demand, using the planet-level richness in `pp_planets` as the static proxy for
relative planet strength. It is presented as planning guidance, with the UI
stating actual yield varies with hotspot placement and depletion. Whole-planet
budget math keeps the existing 48k/cycle reference for slot counting; the head
ratio is layered on top for the two legs only.

Opportunity is narrow by construction — a split is only valid when two P0 types
**co-occur in one planet type's 5-set** (`PLANET_P0_MAP`) AND a real `pp_planets`
row exists with adequate richness for **both**. The UI must be honest: report
"merged N planets" (or "no valid merges found"), never silently no-op.

---

## Workstream A — Private user baskets  ✅ SHIPPED (2026-06-10)

Done: `context_id` migration (rebuild drops the legacy global `name UNIQUE`, per-owner
uniqueness via `IFNULL(context_id,-1), name` index, existing baskets → global); `GET
/api/baskets` soft-auth scoping (globals + own); `POST` opened to `require_context` with
admin-only `make_global`; `PUT/DELETE` owner-or-admin-on-global; share snapshot (`bk`
payload key + `inline_basket` on `FuelBlockPlanRequest`, resolved ahead of `basket_id`);
frontend basket manager modal (`#basketModal`, "My baskets…" in the product step + Admin
tab button), owned/global tags, admin global toggle. Verified end-to-end in-container
(ownership rules, admin globals, inline resolve) + `test_distribution.py` (only the
known DE-IHK use_existing=False pre-existing failure). Deployed; `planetary.js?v=108`.

Original spec below.



### Schema
- `pp_baskets`: add `context_id INTEGER` (NULL = global/admin-owned). Migration =
  `ALTER TABLE pp_baskets ADD COLUMN context_id INTEGER`; existing rows stay NULL
  (global). Add to `ensure_*` table-creation in `admin.py`.

### Endpoints (`app/admin.py`)
- `GET /api/baskets` — soft auth (resolve `context_id` if a `pp_session` exists,
  else None). Return `context_id IS NULL` (global) **+** `context_id = caller`.
  Unauth callers see only global. Tag each row with `owned: bool` + `global: bool`.
- `POST /api/baskets` — was admin-gated → `require_context` (any logged-in user).
  Sets `context_id = caller`. Admin-only optional `global: true` creates a NULL-
  context global basket.
- `PUT/DELETE /api/baskets/{id}` — allow if caller owns it (`context_id` matches)
  **or** (admin **and** the row is global). 403 otherwise.
- Validation of items (real PI type, tier ≥ 1) unchanged.

`BASKET_CONFIG_BASE + basket_id` per-char config in `pp_plan_config` is unchanged
— those rows are already per-character (hence per-context). Two users' private
baskets can collide on `basket_id` only if ids are global PKs (they are), so the
sentinel stays unique. Good.

### Shares (snapshot to avoid leakage)
Shares store the computed plan (renders fine for anyone), but "re-run / tweak"
needs the basket definition a non-owner can't read. Fix: **embed a basket
snapshot** in the fuel-block share payload — new key `basket = {name, run_size,
unit_label, items:[{type_id, qty}]}`. `_resolve_target_basket` gains an
`inline_basket` fallback used when `basket_id` isn't resolvable for the caller.
Basket items aren't locatable, so the anonymizer is unaffected; add nothing to
the `_SHARE_*` sets.

### Frontend (`static/planetary.js`, `static/index.html`)
- Product picker lists global + own baskets; own ones get an "owned" chip.
- Move basket create/edit out of the Admin-only tab into a lightweight
  user-facing "My baskets" manager (modal or wizard-step section). Admin tab keeps
  the global-basket editor (with the `global` flag).
- `_refreshBaskets` already keeps the picker live — reuse.

---

## Workstream B — Split-extraction planning  ✅ SHIPPED (2026-06-10)

Done: `split_mode` (off/conservative/aggressive, `_norm_split_mode`) on both `PlanRequest`
and `FuelBlockPlanRequest`; shared post-pass `_consolidate_split_extractors` in `planner.py`
(+ `_pick_split_host`, `_solve_split_heads`, `_ext_leg_qualities`, `_ext_actual_p0_per_day`)
called after factory assignment in both `_run_plan` and `_run_fuelblock_plan`. Persisted:
`pp_profiles.split_mode` column + `ProfileSave` + save/list SQL; share key `sx`; frontend
`_wiz.splitMode`, `_planRequest`, profile + share restore. UI: stats-bar segmented control
(Off/Conserv./Aggr., `setSplitMode` → `_rerunPlan`) + "N planets saved" / "no merges
available", split planets render as one row with two P0→P1 legs + head split + per-leg
quality (`_splitExtRow`). Stats: `split_mode`/`split_planets`/`planets_saved`; quality + P0/day
aggregations expand split legs. Anonymizer verified (split system relabels, legs carry no
locatable field). `test_distribution.py` unchanged (split off = identical; only the known
DE-IHK failure). `planetary.js?v=109`.

**Model that shipped (important):** feasibility is in PLANET-units (10 heads = 1 planet),
the same quality-agnostic 48k baseline the slot budget uses, so a **conservative** split
preserves exactly the baseline production the non-split plan targeted (floor per P0 =
`min(fair-share, what the plan already places)`, so scarce/capped P0s are never shed).
**Aggressive** packs into 10 heads even when a leg underfills (heads ∝ need). A split saves a
planet only when two co-locatable P0s share ≥1 planet of combined slack — so conservative
yields **0 merges when characters are planet-capped** (no real overproduction headroom); the
UI says "no merges available" rather than pretending. Per-leg head counts are guidance only
(yield varies with heatmap placement + depletion — not a static number). Validated via a
direct unit test (conservative ends exactly at need; aggressive trades output for fewer
planets) since the live test data is planet-capped.

Original spec below.

## Workstream B — Split-extraction planning (spec)

### Request / persistence wiring
- New field `split_mode: str = "off"` (`off` | `conservative` | `aggressive`) on
  **both** `PlanRequest` (planner.py) and `FuelBlockPlanRequest`
  (fuelblock_planner.py). Validate against the allowed set; unknown → `off`.
- Persist in all three (per CLAUDE.md "Profiles & shares"): `pp_profiles` column
  `split_mode`, share key `sx` (stores the string), frontend save/restore.
- UI: **"Split P1 production on extractor planets"** segmented control / select
  (Off · Conservative · Aggressive) in the plan step; live `_rerunPlan` on change.
  Tooltip noting actual head yield varies with hotspot placement + depletion.

### Algorithm — shared post-pass
New shared helper in `planner.py` (both run-functions call it after extractor +
factory assignment, before stats): `_consolidate_split_extractors(...)`.

1. **Residuals.** For each P0 type, from required P0/day vs the chosen planets'
   per-slot output, derive the last slot's needed heads `r` (`r < 10`); the idle
   `10 - r` heads = the overproduction the user described.
2. **Compatibility graph.** Candidate pair `(A, B)` is valid iff:
   - ∃ planet type `T` with both A and B in `PLANET_P0_MAP[T]`, and
   - ∃ reachable `pp_planets` row (chosen-system / char-reach scoped) with
     richness ≥ threshold for **both** legs, and
   - `heads_A + heads_B ≤ 10`, and
   - the 2-ECU + 2 basic-line layout fits the host char's CC budget
     (`compute_resources`).
3. **Greedy merge (mode-dependent).** Pair compatible residuals, collapse two
   single-extractor slots into one split planet on a char who can reach it.
   - **Conservative:** merge **only** when both legs still meet required output at
     the chosen planet's richness — never drop below need. Fewer merges.
   - **Aggressive:** keep packing to minimize total planet count, allowing legs to
     move onto split planets even when not strictly leftover, preferring richer
     planets. May trade per-stream output for fewer planets.
   Each successful merge frees one planet slot. Head ratio per leg ∝ residual P0
   demand (see mechanic model), capped at 10 heads total.
4. **Feasibility.** Fewer planets only loosens constraints; recompute
   `effective_planets` usage and report savings. No core re-solve.

### Data model
Extractor entry becomes optionally a split: add `split: true` +
`legs: [{p0_type_id, p1_type_id, heads, quality_pct, p0_name, p1_name,
best_planet_type}]` on the existing `(system, planet_num)` parent. Non-split
entries unchanged (back-compat).

Stats: add `split_planets` (count) and `planets_saved`.

Touch points to update for the new shape: `renderAssignments` (basic split
rendering — see Phase 3 for the rich card), the share anonymizer walk (legs ride
on a parent that already carries the system — verify no leg-level system field
escapes), and `test_distribution.py` (split invariants: heads sum ≤ 10, each leg
meets need, no planet double-used).

---

## Phase 3 (deferred — reminder todo) — 2-ECU template + rich display

- `app/layout.py`: `generate_split_extractor_layout(p0_a, p0_b, heads_a, heads_b,
  planet_type, cc_level)` → two ECUs with the head split, two Basic Industry
  lines, shared launchpad; validate via `compute_resources`, overflow the grid if
  it doesn't fit (surface as a shortage, like factory unpinned).
- Extend `/api/layout/bundle` token format to encode a split extractor; add to the
  planner's "PI Templates (.zip)" button.
- Rich plan card: one planet showing both legs (P0→P1 each), the head split, and
  per-leg quality %.

---

## Build order
1. **A — private baskets** (independent, ships value alone).
2. **B — split-planning math + basic display** (the checkbox).
3. **C — Phase 3** template + rich card (deferred).

Test each against the container with `test_distribution.py` (per CLAUDE.md). Bump
`planetary.js?v=N` on JS changes. Deploy: `docker compose build && docker compose
up -d --force-recreate`.
