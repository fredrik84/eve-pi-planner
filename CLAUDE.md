# eve-pi-planner — Developer Notes

## Project Goal

Optimize a player's EVE Online Planetary Industry (PI) setup across multiple characters with the least effort for distributing and delivering materials. The planner assigns extractor planets (where P0 raw materials are harvested) and factory planets (where P0→P1→…→P4 processing happens) across all characters to hit a user-specified overproduction target.

---

See also [CONTRIBUTING.md](CONTRIBUTING.md) for the condensed, PR-facing version of the design
philosophy and code style rules below — point external contributors there first.

## Development guidelines (deploy & change policy)

These are standing rules for ALL changes. Follow them unless the user explicitly says otherwise.

1. **Always test.** Write proper test cases for new features and run them against the container
   before calling anything shipped. Existing suites: `test_distribution.py` (planner correctness,
   needs `DEBUG_PI`/`DEBUG_CONTEXT_ID`), `test_features.py` (feature flags + skill-roi public
   surface), `test_optimizer.py` (LP-solver correctness for `/api/optimize` — synthetic
   hand-computable cases + a live smoke test; run the in-process cases inside the container, not
   the bare host, since `highspy`/`numpy` are only installed there), `test_alerts.py`
   (the shared alert engine + notification prefs migration — seeds fake `pp_char_planets` rows
   and a fabricated `pp_sessions` cookie to exercise the real `/api/dashboard` endpoint without
   a live ESI login), `test_min_cc.py` (layout CPU/PG fitting: the FIT_HEADROOM promise, `min_cc`,
   the head-drop fallback — pure in-process layout math, run it in the container),
   `test_skill_enough.py` (the "already enough skill" half of `/api/skill-roi`, seeded rows +
   fabricated cookie), `test_page_access.py` (the per-group `require_page` backend gate — that a
   group with no restrictions stays a no-op, that a restricted group really is 403'd, and that
   the public customer build-status link is NOT gated), `test_disconnect_character.py`
   (`DELETE /api/characters/{id}` — that every per-character table is cleared, that another
   account's character is untouchable, and that account-level history survives),
   `test_delete_account.py` (`DELETE /api/me` — that nothing keyed to the account survives, that a
   second account is unaffected, and that bug reports are anonymised rather than deleted).
   Add to these or create a new
   `test_*.py` in the same urllib/`--url` style. Assert
   *durable invariants*, not runtime state an admin can change (e.g. don't assert a flag's enabled
   value equals its code default — admins toggle it).
2. **Gate new features.** Every NEW feature ships behind a feature flag (`app/features.py`
   `FEATURE_REGISTRY`, default `False` = admin-preview), rolled out to the public from the Admin →
   Features tab. We have no staging environment, so this IS the staging mechanism.
   **Hot-patches / fixes to EXISTING features do NOT need a flag** — fix them in place.
3. **User simplicity is a core design element.** Maximize automation, minimize manual config. The
   best UI is read-only: surface a computed answer rather than a knob. Add a configurable field only
   when the math genuinely can't decide for the user. (See also the PI-planner design principle:
   minimize planet interactions; automate the math or drop the feature.)
4. **Reuse code; build generic endpoints — but no reuse-by-conditional.** Extract shared helpers and
   write general endpoints. Do NOT bolt `if mode == ...` branches onto an endpoint to make it serve
   two callers; that gets messy fast. Prefer a clean shared helper called by two thin endpoints, or a
   parameter that's genuinely orthogonal (like `FuelBlockPlanRequest.basket_id`), over a flag that
   forks the body.
5. **Static data first, live data when needed.** Prefer SDE / Fuzzwork (static) for anything that
   doesn't change per-player. Use ESI for live per-character data. **Live data trumps everything —
   UNLESS the value can be reliably derived from a known, documented formula** (then compute it; see
   the extraction-decay and factory-rate models, which are formula-derived rather than scraped).
6. **Default to `main` only — `dev` is opt-in, not routine.** Each push (to either branch)
   triggers its own CI build + ArgoCD deploy + Discord notification chain, so pushing to both
   doubles the notification volume for one logical change. Normal changes (the vast majority) go
   straight to `main` — commit and push there directly. Only route through `dev` first when there's
   a real reason to soak-test before prod: a big or disruptive change (new feature, anything
   touching the planning algorithm, schema/migration changes) where you genuinely want to watch it
   run before it hits prod. In that case: commit and push to `origin/dev` — CI builds a
   `:dev`-tagged image (`.github/workflows/build.yml`, `branches: [main, dev]`), ArgoCD Image
   Updater rolls the live dev pod at `dev.eveindustry.net` the same way prod does. For quick
   local iteration (UI tweaks, etc.) there's also a local `docker compose` stack — separate from,
   and not automatically kept in sync with, the live k8s `dev` namespace. Once it looks right:
   `git checkout main && git pull && git merge dev && git push origin main` — **that push is the
   prod deploy**. Don't push the same small change to both branches "to keep them aligned" — that's
   the pattern that causes the doubled pings; `dev` drifting behind `main` between real dev-test
   uses is expected and fine. End commit messages with the Co-Authored-By trailer.
   **Deployment off `main` is fully
   automated**: GitHub Actions builds `:latest` (~20-40s, not correlated with image size), the
   ArgoCD image updater detects the new digest (polls ghcr every 30s) and commits to evpi-gitops,
   then ArgoCD syncs and rolls the pod (its own git-poll runs ~every 60-110s, separate from the
   image updater's poll — the two stack). Real measured end-to-end push-to-running time is in the
   low single-digit minutes, not a fixed number — see `evpi-gitops`'s deploy-latency notes if this
   needs re-tuning. Runs on the 3-node k3s HA cluster (`node01-03.failed.name`).

   **Namespace layout (since 2026-07-04):** prod and dev are two fully independent stacks — separate
   namespaces (`production` / `dev`), each with its own Postgres, Redis, and EVE SSO callback
   secret, built from one shared Kustomize base with per-environment overlays in the `evpi-gitops`
   repo (`apps/{eve-pi-planner,postgres,redis}/base` + `overlays/{prod,dev}`). Resource names are
   **identical** in both namespaces (`eve-pi-planner`, `postgres`, `redis` — no `-dev` suffix);
   namespace alone provides the separation. `sudo k3s kubectl -n production ...` / `-n dev ...`.
   This replaced an older setup where dev shared prod's database inside one `eve-pi` namespace — a
   real bug came from that: logging into a character on the dev site could rotate that character's
   EVE SSO refresh token and invalidate whatever prod had stored for the same character, since EVE
   refresh tokens are single-use/rotating regardless of which of our systems asks for one. The
   `eve-pi` namespace still exists but now hosts only `eve-pi-ops` (donation-alert,
   pod-health-check) — an unrelated app that was never part of this migration; don't delete it.

   **Domains (since 2026-07-30):** prod is `eveindustry.net`, dev is `dev.eveindustry.net`. Each
   environment serves on exactly **one** canonical host; every other name we answer on
   (`www.eveindustry.net` and the legacy `eve-pi.failed.name` / `eve-pi-dev.failed.name`) is a
   permanent 301 to it via the `canonical-redirect` Traefik middleware in the matching
   `evpi-gitops` overlay. Two things force that shape, so don't "helpfully" start serving the app
   on a second origin: an EVE application registers exactly **one** callback URL (prod and dev are
   therefore two separate applications with different `EVE_CLIENT_ID`s — they cannot share one),
   and the session cookie is set on whichever host completed `/auth/callback` with no `Domain`
   attribute, so a second serving origin would be permanently logged out. Legacy names must stay
   in the `Certificate` `dnsNames` for as long as we honour old links — a redirect still has to
   complete a TLS handshake first. Changing a domain is a three-step cutover, in this order: DNS →
   cert names (wait for `Ready`) → routes/redirect, then flip the callback in the developer portal
   and the `EVE_CALLBACK_URL` key of the `eve-pi-env` secret together (Reloader only watches the
   TLS secret, so that one needs a manual `rollout restart`).
7. **Commit messages ARE the release notes — be extra vigilant.** The release step in
   `.github/workflows/build.yml` builds the changelog **directly from the commit log** since the
   previous tag (`git log <prev>..<tag>`), grouped into Features / Fixes / Performance / Maintenance
   by the `feat:`/`fix:`/`perf:` prefix — verbatim, with no editing pass in between. (It used to use
   `gh release create --generate-notes`, but that itemizes merged **PRs** only, so this repo's
   direct-push-to-`main` flow got an empty "What's Changed" + a bare compare link — switched to the
   commit-log build 2026-07-17.) A vague commit (`fix stuff`, `wip`, `updates`) becomes a vague,
   useless line in the public changelog. Every commit message must stand on its own as a one-line
   changelog entry: single-line `feat:`/`fix:`/`chore:` description, no body, stating *why* the
   change was made, not just *what* changed. This is not cosmetic — treat it as seriously as the
   code change itself.
   **Cutting a release** (after a batch of shipped changes on `main` is stable — not every commit,
   only on a meaningful milestone or when asked):
   ```
   git checkout main && git pull origin main
   git tag -a vX.Y.Z -m "vX.Y.Z"   # PATCH for fixes, MINOR for features, MAJOR for breaking changes
   git push origin vX.Y.Z
   ```
   **Deciding X.Y.Z:** find the last tag (`git tag --sort=-v:refname | head`), then read what
   shipped since it (`git log <last-tag>..HEAD --oneline`). Any `feat:` commit in that range means
   at least a MINOR bump; if everything since the last tag is `fix:`/`chore:`/`docs:`/`perf:`, it's
   a PATCH; MAJOR is for an actual breaking change (none yet as of `v0.1.0`). Decide the number
   yourself from the commit log and just tag it — "cut a release" is the go-ahead, it doesn't need
   a round-trip to confirm the version.
   Pushing the tag triggers `.github/workflows/build.yml`, which builds
   `ghcr.io/fredrik84/eve-pi-planner:vX.Y.Z` (alongside the usual `:latest`) and creates a GitHub
   Release whose notes are the categorized commit log since the previous tag (see rule 7). The
   release step needs full history, so the workflow's checkout uses `fetch-depth: 0`. First
   release: `v0.1.0`.
   This is independent of the `:latest`/ArgoCD deploy path — tagging does not trigger a new
   deploy, it only marks/publishes a version of whatever is already live on `main`.
8. **Preserve user privacy.** User data is never exposed publicly. Every endpoint that returns
   character names, systems, planets, or any locatable data **must** be gated by `require_context`
   (own data only) or `require_admin` (admin tools). The only exceptions are: (a) the Admin → Users
   page, which needs character names for management and is already admin-gated; (b) anonymous/full
   shares, where the user has explicitly chosen to publish (`anonymize=False`). When adding a new
   endpoint, default to session-scoped. Never add a publicly accessible endpoint that returns
   per-user data, even in aggregate form that could be re-identified.
9. **No ads, no third-party data sharing.** No analytics scripts, tracking pixels, ad networks, or
   any third-party JS may be added to the frontend. No user data (characters, systems, plans, usage
   patterns) is ever sent to a third party. ESI (CCP's official API), Fuzzwork (static SDE
   mirror), and `images.evetech.net` (CCP's own type-icon render service, same `evetech.net`
   domain as ESI — used by the Reactions dashboard's slot icons) are the only external services
   this app contacts, and only for game data — not telemetry. The Prometheus `/metrics` endpoint
   is infrastructure-internal (token-gated, default off) and contains only aggregate counts,
   never per-user data.

---

## Code layout

`app/main.py` is **composition only** — routers, startup/shutdown, the page routes (`/`, `/s/{id}`,
`/b/{id}`) and the `/api/*` catch-all. The original Find-Buildables analyzer (`/api/analyze`,
`/api/optimize`, `/api/share`, `/api/share/{id}`) used to hang off the app object here; it now lives
in **`app/analyzer.py`** like every other feature. Two things to know about it: its `/api/share` is
the ORIGINAL inventory share (`app/shares.py`, its own tiny store), unrelated to the plan shares in
`pp_shares` that `/s/{id}` serves — similar names, different features; and `/api/optimize` is the
only caller of `highspy`+`numpy` (~55 MB of the image), lazily imported inside the solve so they
cost nothing at startup. Retiring the feature = delete `analyzer.py`, `pi.py`, `optimizer.py`,
`shares.py` and those two requirements.

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

**Fuel-block planner** lives in `app/fuelblock_planner.py` (own `APIRouter`, registered in
`main.py` after `planner_router`): `FuelBlockPlanRequest`, `_run_fuelblock_plan`,
`_compute_fuelblock_budget`, `_assign_fuelblock_factories`, `_system_security`, and the
`/api/plan-fuelblock` + `/api/fuelblock-bom` endpoints. It imports the shared helpers from
`planner.py`; the only planner→fuelblock dependency is a **local import** inside `debug_plan`
(avoids a circular import). The BOM/ME math (`resolve_bom`, `compute_basket_p1_reqs`,
`resolve_rig`/`sec_band`, `me_keep_factor`, `apply_effective_me`) is consolidated in
`app/fuelblocks.py`.

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

**Extractor template = 10 heads where possible; basics scale.** `generate_extractor_layout` keeps
all 10 extractor heads (full P0 extraction, matching the planner's flat 48k P0/cycle model) and
scales **only** the basic (P1) factory count down to fit a lower CC (8→6→4→1 at CC5→4→3→2 on a
small planet). Heads are a **last-resort** lever, pulled only when even 1 basic doesn't fit (CC1/CC2,
or a big planet with expensive head spokes) — before that we exported templates the client would
reject. The summary reports `heads_requested` alongside `heads` so the UI can say what the low
command centre cost. `generate_layout` passes `cc_level` into the tier-1 path (don't drop it —
extractor templates must scale with the toon's real CC, not default to CC5). **Factory** planets
still scale by CC — the packed facility count (`component_factory_rate`/`_packed_rate` →
`generate_layout` `max_count`) drops at lower CC, so the planner places more factory planets.

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

**`min_cc` — the level a layout actually needs.** `min_cc_for(cpu, pg)` returns the lowest CC level
whose budget fits the draw *with headroom* (None if nothing does); it's in every `compute_resources`
result and in the extractor/factory/split summaries. Levels above it buy nothing for that template.
Note a maximal template always reports `min_cc == cc_level` (the generator packs to the budget by
design), so the actionable version for extractors is the **`cc_ladder`** in the extractor summary:
what each of CC1–CC5 fits on this planet (`heads`, `basics`, `product_per_hour`, `pg_pct`/`cpu_pct`,
`over`). That's the answer to "how far do I need to train Command Center Upgrades?" — rendered as
the clickable ladder row on extractor cards. Cheap to compute (~6ms/level); do NOT build it by
recursing into `generate_extractor_layout` (use the internal `_fit`).

**Extractors are power-grid-bound, never CPU-bound** (~32-45% CPU at any level, vs 87-89% PG). The
layout card leads with PG and mutes CPU on extractor cards for that reason; factories vary (a P4
chain is CPU-bound at 78%/70%). `resources.binding` says which.

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

Frontend JS is split across files loaded in order from `index.html`: **`utils.js`** (loaded first — shared formatting helpers: `fmtIsk`/`_fmtIsk`/`_fmtHours`/`_fmtDHM`/`_esc`/`_fmtWalletDate`/`_fmtCacheTime`), **`app.js`** (tab nav, ESI login popup, mobile pull-to-refresh, DOMContentLoaded boot), **`planetary.js`** (the core — shared state + `_featureActive`, the PI-planner wizard + `renderFinalPlan`, Characters/header, profiles/shares, tab-entry hooks like `onPlanetDbTabOpen`), **`dashboard.js`** (Dashboard tab: overview, maintenance routine, spare-capacity, the global `rescanAll`), **`admin.js`** (Admin tab: planet submissions, feature flags, baskets, admin users, bug triage), **`planetdb.js`** (Planet DB tab: constellation/region filter, planet list + chunked table, import modal), **`refill.js`** (PI-Planner refill tool: saved-plans bar, build/refill mode, P1-stack distribution), **`analysis.js`** (Setup Analysis tab), and **`layout.js`** (Factory Layout tab). Feature files were carved out of `planetary.js` for maintainability. The split is load-order-safe because the JS is **all declarations except one top-level statement** (the `DOMContentLoaded` listener in core) — functions are global and resolve at call time, so feature files just load after `planetary.js`. When carving more out: cut only at top-level boundaries (verify each file with `node --check`), keep shared state/util in `planetary.js`, and never split a wizard/dashboard interdependency you can't trace. Asset cache-busting is automatic — `index.html` ships `?v=dev` and `app/main.py` stamps the running build's `GIT_COMMIT` onto every asset URL at serve time (`ASSET_VERSION`/`_page()`), so there is **no `?v=` number to bump** any more. Deploy of static-only changes can be a `docker cp` into the container, but always `docker compose build && up -d --force-recreate` to bake in before calling it shipped.

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

## Industry: per-component build overrides + customer build-status links

**"Build it anyway" overrides.** The make-or-buy engine has two shortcuts that BUY a component the
cost engine would build: the marginal-saving threshold (the slider) and the speed cap. Both are
judgements about what's *worth a job*, which is the user's call — so every "low saving" shopping-list
row reports `marginal_saving` (the ISK building that batch would have saved, negative when an
unowned blueprint copy makes building dearer) and offers a **Build it** button.
`BuildParams.force_build_ids` / `BuildOptions.force_build_ids` defeat **only those two shortcuts** —
a component the cost engine says is outright cheaper to buy is still bought, whatever the user
clicks. Overrides persist on the order (`pp_industry_orders.force_build_ids`, a JSON id list) and
are **unioned across the queue** in `_run_queue_plan`: the queue builds one shared batch per
component, so an override can only be all-or-nothing for that component. The preview's own set lives
in `_indForcedTypes` (frontend) and is cleared once the order carries it.

**Blueprint ME/TE for prints you don't own.** A product with no owned blueprint used to be costed at
the global fallback — **ME 0 / TE 0**, the un-researched worst case — which inflated materials AND job
time on every component the plan buys a copy for. The contract index already stores each listing's
research (`pp_bpc_observations.me/te`, captured by the existing scan — no extra ESI traffic), so
`bpc.representative_me_te(info)` picks the copy the plan would actually buy (**cheapest per run**,
ties toward better research) and its ME/TE seeds `params.me_by_product`. Price and efficiency then
describe the same purchase; costing against one copy's price and another's research was the specific
mismatch to avoid. Precedence: **user override > owned blueprint > contract copy > ME 0/TE 0**, with
`params.me_source` recording which, so the plan can show it. Each build step in `requirements` carries
`me`, `te`, `me_source` (`owned`/`contract`/`override`/`default`/`reaction`); the UI renders a
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

**Industry performance.** Two things dominated page and share-link load, both measured before
changing anything:
* The **recipe graphs are cached per process** (`graph._cached_graph`, 15-min TTL, `clear_graph_cache()`
  to drop it). `load_manufacturing_graph` reads ~4,800 blueprints and every material row — 68ms
  locally, more against Postgres — and it ran on EVERY plan call, several per page. Every consumer is
  read-only (no caller mutates a recipe), so one copy serves them all. The TTL is a backstop for an
  SDE rebuild under a long-lived process; a deploy restarts the pod anyway. **Tests that seed their
  own synthetic SDE must call `clear_graph_cache()`** — `test_industry._seed_con` does.
* The page ran **three full queue plans** per load (queue-plan, to-install, progress). The checklist
  is a view of the plan, not a separate question, so `/api/industry/queue-plan` returns it inline as
  `install` (built by the shared `orders.install_block(ctx, res)`, which `/api/industry/to-install`
  also uses); the frontend renders from that instead of a second POST. Progress and the plan are
  independent, so they now run concurrently (`Promise.all`).
Measured on a Revelation: share link 706ms cold-process → **47ms** warm; page ≈73ms serial → ≈25ms.
Keep it that way: anything that needs a whole-queue plan should take one that already exists rather
than calling `_run_queue_plan` again.

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
- Progress reads the same ledgers the builder's own view does (`_done_by_type`/`_running_by_type` +
  owned quantities, capped at need), so a customer can never see a rosier number than the builder.
- Public reads are cached 60s (`indshare:<id>`); a public page whose every render costs two plans
  would otherwise be an amplification lever.

## Industry: first use

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
users) replays it; the button is in Setup & slots, hidden unless `_featuresIsAdmin`. It writes
`onboarded = 0` and **not NULL**, because the backfill claims NULL rows and would otherwise undo the
reset at the next pod restart.

What first use still assumes:
- **Pricing needs nothing** — `resolve_market_data` falls back to Jita.
- **Blueprints are optional** (ME 0/TE 0 + a "connect a character" reminder), assets are opt-in.
- **The build system comes from REACTIONS settings** (`account_build_defaults` → `reaction_system`),
  so a Reactions-less account quotes job fees light by the system cost index — warned by
  `_indCostBasisWarn`, which must link to **Markets & Logistics** (where that field lives); it used
  to open Setup & slots, which cannot set it.
- **Slots need the skills scope AND Mass Production / Mass Reactions trained** (`_eligibility`).
  With neither trained the pools are 0, `schedule` starts nothing and the plan renders a 0h
  makespan with an empty checklist — not a crash, but it looks like one. The excluded characters
  and the reason are listed in Setup → Job slots.

## Industry: running a build, not just planning one

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

**Marking a job done by hand** (`pp_industry_manual_done`, `POST /api/industry/progress/done`).
Progress inference is right most of the time and wrong in ways only the user can see: a batch built
on a character that never granted the jobs scope, work done before the account was connected, a
component acquired by trade. The mark is the **third done-signal**, combined by `resolve_done(need,
completed, from_stock, manual)` — the max of the three, capped at the requirement — so it can raise
a count but never hide observed work. Stored per TYPE (the grain everything else here uses), and
epoch-gated exactly like the completion ledgers so a tick from a finished build can't read as
progress on a re-queued one. Runs `-1` (`_ALL`) means "all of it, whatever the plan currently says";
a concrete number would go stale the moment a quantity changed. It **never writes to the completion
ledgers** — those feed lifetime turnover and profit, and a tick is not evidence of an ISK-bearing
job (same rule the simulated-progress preview follows).

**Corp hangars over ESI** (`refresh_corp_assets`, `POST /api/industry/assets/refresh-corp`). The
module docstring used to say corp assets were deliberately not read, because `/corporations/{id}/
assets/` needs the **Director** role and ESI offers nothing weaker. That reasoning still holds for
most players — the paste path is theirs — but directors run their builds out of corp hangars, so
this reads them into the same opt-in source list. Adds `CORP_ASSETS_SCOPE` +
`CORP_DIVISIONS_SCOPE` (division names: a director picking a hangar needs the names they gave it) to
the ONE unified scope superset. **A 403 is not an error** — it is the expected answer for a
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
list short) are both too costly to guess at. One tick in Setup undoes it.
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
  per-planet richness columns in `pp_planets`.
- **6 = max planets per character** → `max_planets = 1 + interplanetary_consolidation`
  (the character's skill, from the DB). Unrelated to `PLANET_P0_MAP`. Don't change one
  thinking it's the other.

## Disconnecting a character (`DELETE /api/characters/{id}`)

Removes one character from the calling account: a **hard delete**, not a soft unlink. A row left
behind with `context_id` cleared would still hold a live refresh token — i.e. we could still read
that character from ESI — which is exactly what a user clicking ✕ is asking us to stop doing
(rule 8). The endpoint has always behaved this way for placeholder and wallet-only characters;
what was missing was that it only cleared `pp_characters` + `pp_char_planets` and orphaned rows in
**eight** other tables.

- **`_CHAR_OWNED_TABLES`** (`app/esi_data.py`) is the delete list — per-character operational
  state, all of it re-createable by a rescan if the character is re-added. **Add a new
  `character_id`-keyed table to this list when you create one**, or a disconnect will silently
  leave its rows behind.
- **Ownership is checked FIRST** (`SELECT … WHERE character_id=? AND context_id=?`, 404 if it
  doesn't match). Every delete below it is keyed by `character_id` **alone** — without that check
  any logged-in user could wipe any character's rows by guessing an id.
- **Missing tables are skipped, not fatal** (`_table_exists`). Several of these belong to modules
  (industry, reactions, markets) whose `ensure_*` may not have run in this process yet, and on
  Postgres a statement against a missing table aborts the WHOLE transaction — rolling back the
  deletes that already succeeded. The probe is written in the `sqlite_master` form that
  `app.db._pg_translate` rewrites to `information_schema`.
- **References are cleared, not left dangling:** `pp_market_config.market_character_id` (else the
  `_market_character` fallback silently re-decides something the user chose explicitly) and the
  removed id is stripped out of every saved plan's `pp_profiles.factory_character_ids`.
- **Sessions are re-pointed, not dropped.** `pp_sessions` binds the character you logged in *with*,
  but is scoped to the account; if that's the character being removed, the session moves to any
  surviving character rather than logging the user out mid-action. Only a now-empty account ends
  the session (`logged_out: true` in the response).
- **Not deleted, deliberately:** `pp_bugs` (an admin support record, not the character's data — it
  already denormalises `character_name` so it stays readable) and
  `pp_industry_completions`/`pp_reaction_completions` (the ACCOUNT's earnings ledger; `character_id`
  is provenance, and deleting them would silently rewrite historical profit).
- **The ESI grant is revoked** best-effort after the commit (`esi.revoke_refresh_token`, 5s timeout,
  never raises). Dropping our copy already stops *us* using it, but the grant lives on at CCP until
  it expires, and "disconnect" should mean it's actually gone. The disconnect never fails on it.
- **Irreversible bit:** `pp_colony_yield` (measured yield per colony across reseats) cannot be
  re-derived by re-adding the character — ESI only reports the CURRENT extraction program. The UI
  confirm names that specifically; everything else comes back on a rescan.
- Covered by `test_disconnect_character.py` (in-process + a live HTTP layer for the
  `require_context` gate).

## Deleting an account (`DELETE /api/me`)

The account-level counterpart, in `app/esi.py`. It had the same bug in a worse place: it cleared
three per-character tables and four context tables while orphaning rows in roughly twenty others —
on the endpoint whose entire promise is "delete all my data".

- Works from **two shared lists in `app/esi.py`**: `_CHAR_OWNED_TABLES` (also used by the
  per-character disconnect, so the two can't drift) and `_CONTEXT_OWNED_TABLES`. Both are explicit
  lists, **not** a reflective "every table with a context_id column" sweep — adding a table should
  be a deliberate decision about whether an account deletion takes it, not something that starts
  happening silently. **Add new per-account tables to `_CONTEXT_OWNED_TABLES`.**
- **The opposite call to the per-character case on history:** the completions ledgers
  (`pp_industry_completions`, `pp_reaction_completions`) and every per-character work record ARE
  deleted here, because the account they belong to is itself going away.
- **`pp_bugs` is anonymised, not deleted** — `context_id`/`character_id` nulled and the name
  replaced with `(deleted account)`. The report is about the app, not the reporter, and admins
  still need open bugs triaged after someone leaves; but nothing identifying the account may
  survive.
- **`pp_markets` is keyed `(owner_kind, owner_id)`** — only `owner_kind='account'` rows are the
  user's. Group-level market lists survive; they belong to the group and are shared with other
  members. Same reasoning for `pp_reaction_settings` and the `pp_group_*` tables.
- **`pp_shares` / `pp_inventory_shares` cannot be cleaned by account** — they have no owner column
  at all, by construction: a share is an opaque id plus a payload, deliberately unattributable to
  the account that created it. There is nothing to delete by context, not an omission.
- Missing tables are skipped via the same `_table_exists` probe, for the same Postgres
  whole-transaction-abort reason described above.
- Covered by `test_delete_account.py` (seeds rows by introspecting each table's NOT NULL columns,
  so a new column can't silently turn a seed into a skipped assertion).

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

---

## Feature flags (`app/features.py`)

Admin-controlled rollout (no staging env — this is how we stage). `pp_features(key, enabled,
updated_at)` stores public-visibility state; the known set is the code-defined `FEATURE_REGISTRY`
(key, label, description, default). A feature missing from the registry can't be toggled.
`ensure_features_table()` seeds missing rows at their default. Endpoints: `GET /api/features`
(public — returns every registered feature with its current `enabled` + the caller's `is_admin`),
`POST /api/features/{key}` (admin-gated via `require_admin`, body `{enabled}`). `feature_enabled(key)`
is a backend helper for server-side gating if ever needed.

**Frontend gating** (`planetary.js`): `_loadFeatures()` caches `/api/features`; `_featureActive(key,
dflt=false)` returns `enabled OR is_admin` (admins preview everything), falling back to `dflt` when
not yet loaded — pass `dflt=true` for retrofitted existing features so they never vanish on a load
hiccup, omit it for new features (fail-closed). Call sites: `onDashboardTabOpen`, `onAnalyzeTabOpen`,
`_refreshBaskets`, `renderFinalPlan` (split control). Admin → **Features** sub-tab
(`loadAdminFeatures`/`toggleFeature`) flips flags. `FEATURE_REGISTRY` in `app/features.py` is the
source of truth for the current flag set — don't duplicate the list here, it drifts.

## Reactions suggestion engine (`app/reactions/advisor.py`)

Split out of `app/reactions/jobs.py`, which had grown to ~1,500 lines covering three unrelated
jobs (ESI job fetching, the persistent slot plan, and this). `advisor.py` holds the two-stage
wizard engine — the knapsack over WHAT to run, then bin-packing onto WHO runs it — plus
`/api/reactions/suggest`. It imports from `jobs.py` **one way only**; `_character_capacities`
deliberately stayed in `jobs.py` (it's about slots, and the customer-order allocation path needs
it too), which is what keeps the dependency acyclic. `__init__.py` imports jobs before advisor
for the same reason.

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
(`settingsSecAlerts`, gated by the `alert_settings` flag like `notifications` gates its own
section) — 5 threshold number-inputs (`.settings-field-row`, label left/control right/hairline
divider — replaces an earlier ad-hoc inline layout that misaligned once there were several rows of
differing label length) + a "Muted alerts" 2-column checkbox grid (`.settings-toggle-grid`),
Save/Reset.

## Fill-factories meter (Dashboard, `pad_fill` flag)

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

## Admin sub-navigation

The Admin tab (`#tab-admin`) is split into sub-pages via an inner nav (`adminSubPage(key)`,
remembered in `localStorage` as `adminPage`): **Planet submissions · Features · Baskets · User
management · Bug reports**. Each is a `.admin-subpage[data-page]` div toggled by the sub-nav; all
sections still load their data on `onAdminTabOpen` so the nav badges (pending submissions, open bugs)
populate. Add a sixth page = one `<button>` in `#adminSubnav` + one `.admin-subpage` div.

## Dashboard "Up next" agenda (`timeline` flag)

Account-level sorted list of the next maintenance tasks (Restart extractors / Haul extractor P1 /
Refill factories) with countdown + absolute clock time, on the Dashboard under Maintenance routine.
`_renderTimelineCard(t)` reuses the existing dashboard `*_due_hours` totals (no extra request).
**Deliberately NOT a single-cycle line** — extractor and factory cadences desync badly (several
extractor restarts per factory refill), so a "you are here on one timeline" viz is misleading; a
sorted agenda is honest. Gated by `timeline`; shows an "admin preview" tag only while not public.

## Skill-ROI advisor (`skill_roi` flag, Setup Analysis)

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

## Refill "empty pads" toggle

Factory launchpad contents come from the last ESI scan and are **not** simulated forward (only
extractors are), so the scanned "P1 already in the pad" goes stale and over-reports (ESI returns the
last-checkpoint contents, from before the factory drew them down — a rescan re-reads the same stale
checkpoint). The Refill tool's **"Pads emptied at drop-off"** toggle (`_refillIgnorePads`, default
ON) ignores `input_m3` and fills to a clean 30,000 m³ (3 LP), matching the usual "empty the pads when
you drop the next batch" workflow. Off = subtract the last-scan contents. m³/unit is 0.19 (verified);
the under-fill people hit was the stale `input_m3`, not the volume constant.

## How-it-works poster + social banner

`static/how-it-works.svg` (9:16 five-step infographic) is the hero on the How-it-works page, opened
in an in-page dark lightbox (`openImageLightbox`/`closeImageLightbox` — generic, reusable) instead of
the bare white file URL. The social/OG preview `static/og-image.png` (1200×630) is the 3:1 banner
(`eve_pi_banner.svg`) centred on a matching dark canvas; the og:image `?v=` is stamped automatically alongside every other asset in
`index.html` AND the `/s/{id}` OG injection in `main.py` when it changes. SVG source posters live in
`~/Claude-Workspace/` (`eve_pi_planner.svg`, `eve_pi_banner.svg`); re-render the OG with cairosvg +
Pillow.

## Mobile layout + "Add to Home Screen"

The site is usable on phones and meant to be **bookmarked to the iOS home screen** — a plain
shortcut, NOT a packaged PWA (no `manifest.webmanifest`, no service worker). `index.html` head
carries `viewport-fit=cover` + `theme-color` + the `apple-mobile-web-app-*` meta (capable=yes,
`black-translucent` status bar, title "EVE PI") so the bookmark opens full-screen; the existing
`apple-touch-icon` supplies the icon.

A single `@media (max-width: 760px)` block at the **end of `style-misc-responsive.css`** (the last
of the `style-*.css` files loaded — see below) does the rest:
- The left `.sidebar` becomes a **fixed bottom tab bar** (icon-over-label). Selectors are paired
  with `body.nav-collapsed .sidebar …` so a desktop-collapsed state can't out-specify them.
- **Only the lightweight pages show** on the bar, in this order (flex `order:` overrides, How-it-
  works and Setup-Analysis swapped vs. desktop): **Dashboard · Setup Analysis · How it works ·
  Admin** (admins only). The heavy tools — Planetary Planning, Factory Layout, Find
  Buildables/Refill (the `.nav-group`), Planet DB, Characters, and **Contribute** (no mobile value)
  — are `display:none` (and dropped from `MOBILE_TABS` in `app.js`). Dashboard stat tiles go 3-up on
  phones (`#dashboardContent .an-stats`); Setup-Analysis stats stay 2-up. Login and
  **Rescan both live in the header**, so the hidden Characters tab isn't needed on mobile. The
  header drops `#reportBugBtn` and keeps `EVE PI` on one line (`white-space:nowrap`).
- `#dashboardNavTab` is forced visible (`display:flex !important`) so the bar stays consistent when
  logged out (JS otherwise inline-hides it); its panel is the login CTA (which now points to the
  header **Login** button, not the hidden Characters tab).
- Hidden on phones in Setup Analysis: `.an-suggest-move` (rebalance "move factories" cards) and
  `.an-suggest-sep` (the manual "Move a character to another account" tool).
- `.an-stats` becomes a 2-up grid (a lone stat tile no longer stretches full-width with its value
  stranded left); `.pp-card-title` wraps so the analyze "Plan" dropdown gets its own full-width line.
- Two-column page grids (`.pp-layout`) stack; `.pp-card` gets `overflow-x:auto` so
  wide tables scroll inside the card instead of the whole page.

`app.js` DOMContentLoaded has a matching guard: `MOBILE_TABS` + `matchMedia('(max-width:760px)')`
— on a phone it never lands on a hidden tab, falling back to **Dashboard** (a `/s/<id>` share link
still opens the plan view). `app.js` also adds **pull-to-refresh** (`setupPullToRefresh`): dragging
down from `scrollTop 0` past a threshold triggers `rescanAll()` (only when the header `#rescanBtn`
exists, i.e. logged in), with a `#ptr-indicator` banner. Standalone home-screen apps have no native
pull-to-refresh, so this is ours. (No `?v=` bump needed on changes — the server stamps every asset
URL with the running build; see the asset-stamping note above.)

## Admin → Corp wallet (donations)

Admin-only view of the corp ISK balance + **player donations**, read via ESI so the owner doesn't
have to log the toon into the game (web SSO needs only a browser — handy for an alpha account that
can't run alongside other characters). Admin-gated, so no public feature flag.

**Scope handling — one app, opt-in scope.** The base `SCOPES` (skills + planets) is unchanged, so
the normal **Login** never asks the public for wallet access. `esi.WALLET_SCOPE =
esi-wallet.read_corporation_wallets.v1`; **`WALLET_SCOPES = WALLET_SCOPE`** — the connect flow
requests ONLY the wallet scope (no skills/planets/POCOs; the wallet toon is a read-only money viewer
and isn't planned over, so the callback's skill/planet fetches just fail silently). `/auth/login`
gained a `wallet: int = 0` query param — `?wallet=1` requests `WALLET_SCOPES` instead of `SCOPES`.
The EVE application (developers.eveonline.com) must **list** the wallet scope in its allowed set, but
listing ≠ requesting — it's only requested on the wallet flow. No second app needed.

**Granted scopes are stored.** `pp_characters.scopes` (TEXT, migrated via `ALTER TABLE`) holds the
JWT `scp` claim (a list, or a bare string for one scope) captured in `esi_callback` — so we can find
which character authorised wallet read. `_wallet_character(context_id)` returns the first character
in that context whose `scopes` contains `WALLET_SCOPE`.

**`esi.corp_wallet_summary(context_id)`** (called by `GET /api/corp-wallet`, `Depends(require_admin)`
which returns the admin's context id) reads, via that character's token: `/characters/{id}/` →
`corporation_id`, `/corporations/{id}/` → name, `/corporations/{id}/wallets/` → per-division balances
(403/401 → `{error:'role'}` = character lacks Accountant/Junior-Accountant/Director), and
`/corporations/{id}/wallets/1/journal/` → entries with `ref_type == 'player_donation'` (donor =
`first_party_id`, resolved via `_resolve_names`). Returns `{connected, balance (div1), total_balance,
total_donated, donations:[{date,amount,donor,reason}], corp_name, ...}`; `{connected:False}` when no
wallet character is linked; `{error:'token'|'fetch'}` otherwise. **Donations/`total_donated` cover
only the most recent journal page (~2500 rows)** — fine for a low-volume corp; the *balance* is
always current. Journal is the master division (1) only.

**Frontend:** Admin sub-page `data-page="wallet"` (`loadCorpWallet`, lazy-loaded from `adminSubPage`
only when opened, since it hits ESI). `connectCorpWallet()` mirrors `esiLogin()` but opens
`/auth/login?wallet=1`. The connected toon joins the admin's context like any character (shows in
Characters / may get PI-scanned — set `planet_limit=0` to exclude from plans if it clutters).
Gating test in `test_features.py` (`test_corp_wallet_gated` → 403 for anonymous).

## Local / alliance market pricing (`app/markets.py`, `local_market` flag)

Reactions pricing can follow one or more **markets** in a priority chain — a player-owned Upwell
**structure** market and/or a public NPC **region** market — falling back to **Jita** (Fuzzwork,
`app.market`) for anything not listed locally. Built because an alliance selling inputs below Jita
on its own structure market was invisible to the tool. Reactions-tab only for now (PI planner /
fuel blocks stay on Jita).

- **Opt-in scope** (`app/esi.py`): `MARKET_SCOPE = esi-markets.structure_markets.v1` +
  `SEARCH_STRUCT_SCOPE = esi-search.search_structures.v1` (+ reused `STRUCTURES_SCOPE` for name
  resolution). `MARKET_SCOPES` **unions** the base `SCOPES` (full PI+market char, like the reactions
  flow). Requested only via `/auth/login?market=1` (`esi_login(market=1)`), never on public Login.
  Frontend clone `connectReactionsMarket()` (`planetary.js`). **Prereq:** the two new scopes must be
  LISTED on the EVE application at developers.eveonline.com (listing ≠ requesting, same as wallet).
- **Config = per-account, group-seeded** — one table `pp_markets(owner_kind account|group, owner_id,
  kind structure|region, location_id, name, priority, active)`. `effective_markets(context_id)` =
  personal list → account's group-manager default list → `[]` (Jita only). Jita is NEVER a row (the
  implicit last fallback). CRUD `GET/POST/DELETE /api/markets`, `POST /api/markets/reorder`
  (require_context; group scope gated by `is_group_manager`). Mirrors the freight resolver in
  `app/reactions/settings.py` (`effective_reaction_settings`) — **freight was already built**, this
  reuses its account-settings UI.
- **Per-context state** `pp_market_config(context_id, market_character_id, onboarded)`. The
  **market character** (whose token reads the structure market) is user-designatable — `POST
  /api/markets/reader {character_id}` (must be a context char holding `MARKET_SCOPE`);
  `_market_character` returns the designated one if still scoped, else the first scoped char
  (back-compat default). `onboarded` is the one-time first-run flag, set by `POST
  /api/markets/complete` (requires ≥1 character in the context). `_markets_payload` also returns
  `characters` (each with `is_market`) and `market_character_id` so the UI can list them + pick the
  reader.
- **Reads** (`app/markets.py`): `fetch_structure_market(context_id, structure_id)` paginates
  `GET /markets/structures/{id}/` via `_market_character`'s token (first char in the context holding
  `MARKET_SCOPE` — clone of `esi_data._wallet_character`), aggregates the whole book per type with
  `_wavg_percentile` (volume-weighted 5th percentile, robust to a lone 1-unit order — matches
  Fuzzwork's shape so it's drop-in). Redis-cached by structure_id (book is identical whoever reads
  it). `fetch_region_market(region_id, type_ids)` is public, per-type, Redis-cached per (region,type).
  `resolve_market_data(context_id, type_ids)` walks `effective_markets` in priority order, takes the
  first market quoting each type, else Jita `fetch_market_data`; each entry carries an extra `source`
  label. **Drop-in for `fetch_market_data`** — the reactions call sites in `graph.py`/`jobs.py` were
  swapped to it (all already had `context_id` in scope). With no markets configured it returns
  exactly Jita, so behavior is unchanged for everyone until they set one up.
- **Freight applies to Jita-sourced items only.** In `_load_goo_and_reached`, `purchasable`'s import
  shipping (`import_isk_per_m3 × volume`) is added **only when `m["source"] == "Jita"`** — the haul
  from the remote hub. A material sourced from a followed local/alliance market gets NO import
  freight (the market is assumed at/near the reaction site; a user who follows a far-off market
  prices their own transport into that market's own numbers). Moon goo from the group sheet already
  had zero import cost (separate `goo` path). `_materials_report`'s `market_name` names the winning
  market per leaf.
- **Search** (`/api/markets/search?q=`): structures via `GET /characters/{id}/search/?categories=
  structure` (only ones the char can access) resolved to names via `/universe/structures/{id}/`;
  regions matched against SDE `constellations.region` names, resolved to ids via public
  `/universe/ids/`. Needs a connected market character for structure results.
- **UI** (`reactions.js`, gated by `_featureActive('local_market')`): the whole Reactions tab is
  **blocked behind an inline first-run gate** (`#rxGate`, `_rxApplyGate`/`_rxRenderGate`) until the
  user has added ≥1 character and clicked **Save & continue** (`_rxCompleteOnboarding` → `POST
  /api/markets/complete`). `onReactionsTabOpen` is async and returns early while gated (hides
  `#rxDashboard`). The gate has 3 steps — **(1) Add your characters** (required; lists context chars
  via `_rxCharListHtml` with a **market-character radio** among scope-holders → `_rxSetMarketReader`,
  plus `connectReactionsMarket`), **(2) Add local markets** (optional), **(3) Configure freighting
  costs** (optional, foldable, reuses `_rxAccountSettingsFormHtml`). `_rxApplyGate` **fails open** (no
  gate) if the feature's off or the fetch fails, so a hiccup never locks the tab. Once `onboarded`,
  the gate never shows again; `#rxMarketSetupCard` shows a one-line "Reaction pricing: A → B → Jita"
  summary and **all edits go through the Reactions ⚙ Settings modal** (`_rxOpenSettingsModal`, which
  hosts the market manager above the freight forms). The market list + search is a **reusable manager
  component** (`_rxMarketManagerHtml` / `_rxMountMarkets` / `_rxRenderMarketManager`) mounted into
  either the gate (`#rxOnboardMarkets`) or the Settings modal (`#rxSettingsMarkets`); `_rxMarketMount`
  tracks which is live so `_rxRefreshMarkets` re-renders the right one. `connectReactionsMarket`'s
  callback is `_rxAfterConnect` (refreshes gate / settings / tab depending on what's open). The leaf
  source name is threaded onto each `reached` leaf node (`market_name`) in `_load_goo_and_reached` and
  surfaced by `_materials_report`, rendered as a per-line **price-source badge** in the shopping list.
- Gating test `test_markets_gated` in `test_features.py`; `local_market` in the registry.

## Notifications (`app/notifications.py`, `app/notifiers.py`)

Push alerts for any of the 11 alert kinds (`app.alert_settings.ALERT_KINDS`), checked by an
APScheduler job every 15 minutes (`make_scheduler`, `check_and_send_notifications`) — pure DB
math, no ESI calls, so it runs freely between rescans. Settings/prefs/log are per-`context_id` in
`pp_notification_settings`, `pp_notification_prefs`, `pp_notification_log`.

- **Event detection is not this module's job.** `_process_context` calls
  `app.alerts.compute_alerts(context_id)` — the same function `dashboard()` uses —
  and only filters/batches/sends. There used to be bespoke `_extractor_events`/`_factory_events`
  queries here, duplicating what the Dashboard computed; unified 2026-07-10 so a push and what's
  shown on screen can never drift apart. If you're tempted to add a new kind of push alert, add
  the kind to `compute_alerts()` first, not here.
- **Prefs = which kinds + a severity floor.** `pp_notification_prefs.notify_kinds` (JSON array of
  `ALERT_KINDS` keys) + `min_severity` (`"warn"` = everything, `"high"` = high only). Old
  `lead_hours`/`notify_extractors`/`notify_factories` columns are left in place (harmless, unused)
  rather than dropped — this codebase's migration convention is additive `ALTER TABLE ADD COLUMN`,
  never `DROP COLUMN`. **One-time migration** (in `ensure_notification_tables()`) derives
  `notify_kinds` for pre-existing rows from those old booleans
  (`notify_extractors→[expired,expiring]`, `notify_factories→[factory_refill]`) so an account that
  had already muted e.g. factory refills doesn't suddenly get pinged for it — but does **not**
  auto-enable the new kinds this unification added (`storage_full`, `ext_unrouted`, ...) for
  already-configured accounts, since silently expanding what an already-tuned account gets pinged
  about is the wrong default. A brand-new context (no prefs row at all) gets all 11 kinds enabled,
  matching the old out-of-the-box default. `GET /api/notifications/prefs` echoes back
  `ALERT_KINDS` as `available_kinds` (same registry `/api/alert-settings` uses) so the frontend
  never hardcodes labels.
- **Channels** (`notifiers.py`, `_CHANNEL_MAP`): Pushover, ntfy.sh, Discord webhook. Each is a
  `BaseNotifier.send(title, body, url=None, fields=None)`. `fields` (a list of `{name, value,
  inline}`) is Discord-only — when present, `DiscordNotifier` sends a **rich embed** instead of
  plain text; Pushover/ntfy ignore it and use `title`/`body`. Discord content is truncated at 2000
  chars (hard API limit) as a fallback safety net — the embed path avoids hitting it in practice
  since fields don't count against the same limit.
- **Batching, not per-event spam.** `_process_context` groups same-kind alerts into ONE message
  each (`_format_batch`, `_KIND_LABELS` for the per-kind title/noun), rather than firing one
  notification per planet — an earlier per-event version produced a wall of pings when several
  things expired close together. **Cooldown is per-kind** (`_COOLDOWN_HOURS`): 2h for the
  time-decaying kinds (expiry/storage), 4h for factory refill, **24h for the correctness-based
  kinds** (`ext_unrouted` etc.) — those are persistent structural problems until the player fixes
  them, not something that resolves itself, so a short cooldown would just nag every 15 minutes
  about the same unfixed issue. Cooldown is checked **before** collecting each alert into its
  batch, so one recently-notified planet doesn't block others in the same run.
- **`POST /api/notifications/resend-last`:** replays the most recent logged batch (grouped by
  `sent_at` to the minute, since all sends in one scheduler run land within seconds of each other)
  to all enabled channels, tagged `[Replay]`. Built because testing the real formatting meant
  waiting for something to actually be due — this fires immediately from history instead. No fake
  countdown (`hours_left` isn't meaningful for a replay); just character + planet.
- **`POST /api/notifications/test`** sends a one-off "channel is working" ping when adding a new
  channel — separate from resend-last, which replays real event data.
