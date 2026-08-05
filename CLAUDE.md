# eve-pi-planner — Developer Notes

## Reference docs (read on demand, not up front)

This file is the always-loaded core. Detail lives in `docs/` — open the one you need:

| File | Read it when |
| --- | --- |
| [docs/code-layout.md](docs/code-layout.md) | adding a route/module, or finding where something lives |
| [docs/industry.md](docs/industry.md) | touching the Industry tab (make-or-buy, overrides, build running, customer links) |
| [docs/features.md](docs/features.md) | touching alerts, notifications, admin, markets, skill-ROI, mobile, bugs, account deletion |
| [docs/industry-planner-spec.md](docs/industry-planner-spec.md) | the Industry planner's original spec |
| [TODO.md](TODO.md) | **source of truth for open work** and closed-with-reasoning verdicts — read before proposing anything |

Keep it that way: new long-form detail goes in a `docs/` file, not here.

## Project Goal

Optimize a player's EVE Online Planetary Industry (PI) setup across multiple characters with the least effort for distributing and delivering materials. The planner assigns extractor planets (where P0 raw materials are harvested) and factory planets (where P0→P1→…→P4 processing happens) across all characters to hit a user-specified overproduction target.

### The Industry planner's goal, and the constraint around it

**Lowest net cost, fastest delivery.** Those are the two things being optimised.

**Effort is not a third goal — it is the constraint the other two have to fit inside.** The builder
this is for already has a working method: a spreadsheet and habit. A tool that produces a better
plan but costs more clicks to operate than the spreadsheet did brings nothing to the table, however
good its numbers are. So: **automate everything that can be automated, and open a knob only where
the judgement is genuinely the user's.** A plan should be right the moment it appears, with no
fine-tuning required to get there; fine-tuning is for the person who wants it, never a step on the
way in.

Today that means two knobs — **margin** and the **low-savings threshold** — plus per-item overrules
for the handful of decisions worth arguing with. Everything else the plan works out for itself.

The tension those two knobs sit in: a builder sells against competitors, and **net cost sets the
floor under the price they can quote**. Buying a component instead of building it is a good trade
for effort and delivery time — that is what the marginal-saving threshold and the speed cap are for
— but every ISK of that convenience is passed to the customer and makes the quote harder to win.
Which is why those shortcuts are judgements the user can overrule (the "worth building instead?"
strip, `force_build_ids`) rather than settled policy, and why the plan reports what each one cost
(`marginal_saving`) instead of quietly taking it. Neither answer is right for every builder; what
is wrong is deciding for them without showing the price.

**The test for anything new here:** does it add a step to the path a builder walks every time? If
it does, it has to remove more than it adds. A control that only the occasional case needs belongs
behind the common one, not in front of it — which is why marking a step done is one click on the
card and a partial mark costs a second one, and why 50 materials are gathered by pasting a hangar
rather than confirmed one at a time.

### The PI side's principle: minimize interactions with planets

Easier PI management for as much profit as we can muster, by cutting the player's manual trips —
picking up P1, distributing it, dropping it at factories. Judge every PI feature by whether it
*reduces* clicks/trips/decisions; if it adds steps without a clear payoff, automate the math away
or drop the feature. Prefer batched m³-capped deliveries, long extractor programs, at-a-glance
overviews. Setup Analysis is **advice on improving the plan, not an interactive calculator**. The
real tension: long programs mean fewer restarts but lower average yield — surface the tradeoff,
default to fewer interactions. There is no ESI write API, so "automate" means doing all the math
and handing back the single number or action to take.

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
   **Don't make the dev-vs-main call unilaterally mid-session — ask.** Picking `dev` on my own
   judgment once created a real git mess: `dev` was 8 commits behind `main` because every other
   change that session had gone straight to `main`, and switching branches mid-task meant stashing
   and conflict recovery. What the session has actually been doing outweighs the general policy.
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

## Epoch timestamps must be `double precision`, never `REAL`

On SQLite `REAL` is an 8-byte double; on **Postgres `real` is float4** — about 7 significant digits,
and a Unix epoch needs 10. So an epoch stored in a `REAL` column is quantised to ~64-128 seconds
(measured on prod: wrote `1785409464.304`, read back `1785409400.0`).

`app.db.widen_epoch_columns()` runs at startup from `_ensure_all_tables()` and widens every column
in `_EPOCH_COLUMNS` still sitting at float4. Three things to know:

- **The list is hand-maintained.** A new epoch column added anywhere needs an entry, or it silently
  rounds. It is explicit rather than a reflective sweep because only EPOCHS need it — percentages,
  volumes, ISK and security status are all fine at 7 digits, and `types.volume` is asserted to be
  left alone.
- **Asking the live schema is the only proof.** The 2026-07-31 pass claimed the BPC scan lease was
  covered; on 2026-08-05 prod still had all three `pp_bpc_scan` columns at float4. The tell was
  `test_scan_lease_is_single_writer_across_replicas` failing about half the time — a lease set to
  `now - 1` rounds back into the future inside its bucket.
- **Widening is non-lossy but recovers nothing.** Rows written before the migration keep their
  rounded values; only new writes are exact.

`test_epoch_precision.py` covers the round trip, idempotency and the targeting.

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

## Frontend lint (`scripts/lint_js.mjs`, CI job `lint-js`)

**One rule: `no-undef`.** A dead `if (!r.ok)` left by the fetch()→api() migration referenced a
variable that no longer existed, so every SUCCESSFUL reaction assign threw a ReferenceError, was
caught, and was reported to the user as failed — and the assign endpoint appended, so each retry
added another full set of rows (two suggestions → 27 rows on a 10-slot character). `node --check`
passes it: valid syntax, fails only when the line runs.

The frontend is plain `<script>` files sharing ~900 implicit globals, so a naive run reports every
cross-file helper as undefined. The script scrapes top-level `function`/`let`/`const`/`var` names
out of all of `static/*.js`, feeds them in as globals, adds the browser set, and enables nothing
else — style rules over this much JS would be noise, and the point is a guard that starts green.
Run it locally with `node scripts/lint_js.mjs`. It does **not** gate the deploy:
this repo pushes straight to main, and a lint step that can fail on an npx download must not block a
hotfix. It exists to make the failure visible, which is all the original bug needed.

## Two different "counts" — do NOT conflate

- **5 = P0 resources per planet *type*** → `PLANET_P0_MAP` (planetary.py). Each type's
  5-set is unique (verified vs EVE University), so a planet's resources identify its type.
  Used for: import type-inference, the `planet_types` display label, and extractor-template
  planet selection. **Not** used for extractor/factory *assignment* — that reads the
  per-planet richness columns in `pp_planets`.
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

## Gotchas that have already bitten us

**DB / Postgres**
- Do **not** reintroduce psycopg2's `ThreadedConnectionPool` — it broke under concurrency; the pool
  is a `queue.Queue`. Always use `with get_connection()`, never a bare connection you close by hand.
- Scope every per-user query by `context_id`. Redis caches `/api/characters`, the Planet DB and
  admin stats — invalidate on write.
- `datetime('now')` must translate to TEXT via `TO_CHAR()`; TEXT columns can't be compared against
  `timestamptz`. See `_pg_translate()` in `app/db.py`.
- **Fresh/stale-DB migration traps** (all found on dev, invisible on prod because prod's DB grew
  with the schema):
  1. `CREATE TABLE IF NOT EXISTS` followed by a failing `ALTER` **silently loses the table** — on
     Postgres a failed statement aborts the transaction and `_PgConn.execute` rolls back the
     uncommitted CREATE. `app/db.py add_columns()` now commits pending DDL before risking an ALTER;
     route every CREATE→ALTER site through it. Pinned by `test_fresh_db_tables.py`, which fakes
     Postgres abort semantics — a SQLite-based test proves nothing here.
  2. Lazy table creation (table made only by the endpoint that owns it) 500s other modules that
     query it directly. Create tables at startup.
  3. `build_sde` used to skip everything if `types` had any rows — an all-or-nothing gate leaves a
     half-built SDE forever.

**Editing frontend JS**
- The Edit tool can land template-literal separators as literal NUL bytes. After any large JS edit:
  `python3 -c "print(open('static/x.js','rb').read().count(b'\x00'))"` — expect 0.

**Local test runs (docker compose, not prod)** — these failures are *not* regressions:
- Fuel-block cases in `test_distribution.py` fail locally: no market data in the local container, so
  basket BOM demand resolves to 0 upstream of any planet placement. They pass on prod.
- `test_reactions.py` must run *inside* the container (`docker compose cp test_reactions.py web:/srv/app/`).
- Local `pp_planets` lacks the `diameter` column that prod has.
- `scripts/seed_hybrid_fixture.py` can hit a UNIQUE violation on reseed when orphaned
  `pp_char_planets` rows survive a context-scoped wipe — delete by `character_id` directly.

**ESI**
- Only colony detail (`/characters/{id}/planets/{planet_id}/`) is cache-gated, on ESI's own
  `Expires` (~10 min). Colony list, skills and the rest are fetched every rescan, and pad estimates
  are re-projected live via `pi_sim.project()`, so a rescan is never a no-op.
- **Never add a force-refresh bypass.** CCP's best-practice page states that querying before
  `Expires` risks an ESI ban for circumventing caching — that would take down the whole app.
  If staleness is reported, check `next_data_at`/`esi_expires` in `pp_char_planets` first (expect
  ~10 min, not hours) before suspecting a logic bug.

**Planet density is not yield**
- The richness/density % in the Planet DB (`pp_planets`, 0-100+) says how plentiful and common the
  resource *hotspots* are — it is **not** an output figure. A 19%-density planet can still average
  the full 48,000 P0/hour if the heads are placed well. Sizing anything (e.g. a template's Basic
  Industry Facility count) off density systematically under-builds good planets with sparse
  hotspots. For "how much does this planet actually produce", use the measured signal —
  `pp_planet_yield_avg.measured_pct` (`app/yield_stats.py`) or the colony's own ESI extraction rate.
  Density is legitimate for *ranking* candidates and for hotspot-placement advice only.

**Reactions pricing**
- A reaction good's **sell-order price is not achievable profit** — that market is repriced
  aggressively. Use instant-sell (buy orders) as the "what you can make" signal; never
  `sell_volume` / `net_profit_order`.

## Debugging prod in-process

When a user reports the planner doing something odd, stop reconstructing their data from
descriptions and go read it — run the real code inside the prod pod:

```
ssh -o BatchMode=yes node02.failed.name \
  "sudo k3s kubectl -n production exec -i <pod> -- python3 -" < /path/to/script.py
```

Get the pod with `sudo k3s kubectl -n production get pods -l app=eve-pi-planner --no-headers`
(pick one showing `1/1`). Pipe a **file** on stdin — nested quotes inside `-c "..."` get mangled.
Inside the script, import and call directly (`queue_plan_packing`, `_run_queue_plan`,
`prepare_plan_inputs`); find the context via `SELECT context_id, character_name FROM pp_characters`.
Monkeypatching module constants there gives a what-if curve on real data in seconds (e.g. sweeping
`app.industry.schedule._PACE_OVERSHOOT` produced "232 jobs → 159 for +32 minutes").
