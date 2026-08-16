# eve-pi-planner — working on the repo

How to test, deploy, release and debug. The rules themselves are stated in
[CLAUDE.md](../CLAUDE.md); this file is the mechanics behind them. Back to
[CLAUDE.md](../CLAUDE.md).

## Contents

| Section | Read it when |
| --- | --- |
| [Test suites](#test-suites) | adding a test, or wondering what already covers something |
| [Local test runs](#local-test-runs-docker-compose-not-prod) | a test fails locally — check here before calling it a regression |
| [Debug endpoint](#debug-endpoint) | exercising the planner end to end |
| [Inspecting the database](#inspecting-the-database) | reading profiles/config out of a container |
| [Running the planner directly](#running-the-planner-directly) | reproducing a plan in-process |
| [Debugging prod in-process](#debugging-prod-in-process) | a user reports the planner doing something odd |
| [Deploying](#deploying-main-vs-dev) | any push — especially deciding `main` vs `dev` |
| [Namespaces and domains](#namespace-layout-since-2026-07-04) | touching k8s, SSO callbacks or DNS |
| [Cutting a release](#cutting-a-release) | a batch on `main` is stable, or you were asked |
| [Feature flags](#feature-flags-appfeaturespy) | shipping a new feature behind a flag |
| [Frontend lint](#frontend-lint-scriptslint_jsmjs-ci-job-lint-js) | after any large JS edit |

---

## Test suites

Write proper test cases for new features and run them against the container before calling
anything shipped. Add to these or create a new `test_*.py` in the same urllib/`--url` style.

A passing test proves nothing on its own — **reintroduce the bug it claims to catch and watch it go
red** before trusting it. Several tests in this table were green against a defect they were written
for, and only the mutation showed it (see the guard notes in `test_settings_memo.py`,
`test_print_locking.py` and `test_routing_client.js`).
Assert *durable invariants*, not runtime state an admin can change (e.g. don't assert a flag's
enabled value equals its code default — admins toggle it).

| Suite | Covers |
| --- | --- |
| `test_distribution.py` | planner correctness; needs `DEBUG_PI`/`DEBUG_CONTEXT_ID` |
| `test_features.py` | feature flags + skill-roi public surface |
| `test_optimizer.py` | LP-solver correctness for `/api/optimize` — synthetic hand-computable cases + a live smoke test. Run the in-process cases **inside the container**, not the bare host: `highspy`/`numpy` are only installed there |
| `test_alerts.py` | the shared alert engine + notification prefs migration. Seeds fake `pp_char_planets` rows and a fabricated `pp_sessions` cookie to exercise the real `/api/dashboard` without a live ESI login |
| `test_min_cc.py` | layout CPU/PG fitting: the FIT_HEADROOM promise, `min_cc`, the head-drop fallback. Pure in-process layout math — run in the container |
| `test_skill_enough.py` | the "already enough skill" half of `/api/skill-roi`; seeded rows + fabricated cookie |
| `test_page_access.py` | the per-group `require_page` backend gate — that a group with no restrictions stays a no-op, that a restricted group really is 403'd, and that the public customer build-status link is NOT gated |
| `test_disconnect_character.py` | `DELETE /api/characters/{id}` — every per-character table cleared, another account's character untouchable, account-level history survives |
| `test_delete_account.py` | `DELETE /api/me` — nothing keyed to the account survives, a second account is unaffected, bug reports are anonymised rather than deleted |
| `test_epoch_precision.py` | the epoch round trip, idempotency and targeting (see the `double precision` rule in CLAUDE.md) |
| `test_fresh_db_tables.py` | fresh-DB migration traps; fakes Postgres abort semantics — a SQLite-based test proves nothing here |
| `test_routing.py` | that the four lists naming the pages agree (panels in `index.html`, `TAB_SLUGS`/`TAB_SUBPAGES` in `app.js`, `SPA_PAGES` in `main.py`), that every page URL serves and that a non-page path still 404s instead of being swallowed by a wildcard |
| `test_factory_drain.py` | when a factory colony runs its imported inputs dry (TODO §21b): consumption read off real pins and scaling with them, on-planet chains draining only what the player delivers, the run-dry instant staying put as the countdown to it shrinks, and — part 4 — that the `factory_refill` alert reads observed stock rather than a from-full cadence anchored to `scanned_at`. In-process, run in the container |
| `test_refill_rates.py` | the per-factory refill rates behind "refill to a deadline": `_p1_batch_sizes` over the whole SDE, and per-factory `units_per_day` summing to the plan total through the real `derive_setup_plans` over a seeded multi-factory account (several factories of ONE product, two products sharing a P1 — with one factory each the invariant is vacuous). A `--url` half repeats it on a live account. Both fail loudly rather than passing over an empty loop |
| `test_refill_deadline.js` | **runs the real `_deadlineSplit`** in a `vm` — the four ceilings on a deadline quantity and the ORDER they apply in, which no string match can see. Node is on the host, not in the web image, so run it **outside** the container: `node test_refill_deadline.js` |
| `test_routing_client.js` | **the one test that RUNS client code.** The routing region of `app.js` is executed in a `vm` context with stubbed `location`/`history`/`localStorage`, so history entries, back/forward and the multi-tab bug are checked behaviourally rather than by string match. Node is on the host but not in the web image, so this one runs **outside** the container: `node test_routing_client.js` |

### Local test runs (docker compose, not prod)

These failures are *not* regressions:

- Fuel-block cases in `test_distribution.py` fail locally: no market data in the local container,
  so basket BOM demand resolves to 0 upstream of any planet placement. They pass on prod.
- `test_reactions.py` must run *inside* the container
  (`docker compose cp test_reactions.py web:/srv/app/`).
- Local `pp_planets` lacks the `diameter` column that prod has.
- `scripts/seed_hybrid_fixture.py` can hit a UNIQUE violation on reseed when orphaned
  `pp_char_planets` rows survive a context-scoped wipe — delete by `character_id` directly.

---

## Debug endpoint

The planner exposes a `/api/debug/plan` POST endpoint that runs the full planning algorithm and
returns a distribution analysis. It requires:

- `DEBUG_PI=1` set in the container environment
- `DEBUG_CONTEXT_ID=<id>` to bypass cookie auth (use the context_id from `pp_characters`)

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

It returns per-P0-type distribution analysis: expected vs actual extractor counts, whether the
distribution is within acceptable rounding tolerance, and any out-of-system assignments.
`test_distribution.py` tests distribution correctness against this endpoint.

## Inspecting the database

Saved profiles are in `pp_profiles`. Config (per-character planet/extractor limits) is in
`pp_plan_config`.

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

## Running the planner directly

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

---

## Deploying: `main` vs `dev`

**Default to `main` only — `dev` is opt-in, not routine.** Each push (to either branch) triggers
its own CI build + ArgoCD deploy + Discord notification chain, so pushing to both doubles the
notification volume for one logical change. Normal changes (the vast majority) go straight to
`main` — commit and push there directly.

Only route through `dev` first when there's a real reason to soak-test before prod: a big or
disruptive change (new feature, anything touching the planning algorithm, schema/migration
changes) where you genuinely want to watch it run before it hits prod. In that case: commit and
push to `origin/dev` — CI builds a `:dev`-tagged image (`.github/workflows/build.yml`,
`branches: [main, dev]`), ArgoCD Image Updater rolls the live dev pod at `dev.eveindustry.net` the
same way prod does. For quick local iteration (UI tweaks, etc.) there's also a local
`docker compose` stack — separate from, and not automatically kept in sync with, the live k8s
`dev` namespace. Once it looks right:

```
git checkout main && git pull && git merge dev && git push origin main
```

**That push is the prod deploy.** Don't push the same small change to both branches "to keep them
aligned" — that's the pattern that causes the doubled pings; `dev` drifting behind `main` between
real dev-test uses is expected and fine.

**Don't make the dev-vs-main call unilaterally mid-session — ask.** Picking `dev` on my own
judgment once created a real git mess: `dev` was 8 commits behind `main` because every other change
that session had gone straight to `main`, and switching branches mid-task meant stashing and
conflict recovery. What the session has actually been doing outweighs the general policy.

**Deployment off `main` is fully automated**: GitHub Actions builds `:latest` (~20-40s, not
correlated with image size), the ArgoCD image updater detects the new digest (polls ghcr every 30s)
and commits to evpi-gitops, then ArgoCD syncs and rolls the pod (its own git-poll runs ~every
60-110s, separate from the image updater's poll — the two stack). Real measured end-to-end
push-to-running time is in the low single-digit minutes, not a fixed number — see `evpi-gitops`'s
deploy-latency notes if this needs re-tuning. Runs on the 3-node k3s HA cluster
(`node01-03.failed.name`).

### Namespace layout (since 2026-07-04)

Prod and dev are two fully independent stacks — separate namespaces (`production` / `dev`), each
with its own Postgres, Redis, and EVE SSO callback secret, built from one shared Kustomize base
with per-environment overlays in the `evpi-gitops` repo
(`apps/{eve-pi-planner,postgres,redis}/base` + `overlays/{prod,dev}`). Resource names are
**identical** in both namespaces (`eve-pi-planner`, `postgres`, `redis` — no `-dev` suffix);
namespace alone provides the separation. `sudo k3s kubectl -n production ...` / `-n dev ...`.

This replaced an older setup where dev shared prod's database inside one `eve-pi` namespace — a
real bug came from that: logging into a character on the dev site could rotate that character's EVE
SSO refresh token and invalidate whatever prod had stored for the same character, since EVE refresh
tokens are single-use/rotating regardless of which of our systems asks for one. The `eve-pi`
namespace still exists but now hosts only `eve-pi-ops` (donation-alert, pod-health-check) — an
unrelated app that was never part of this migration; **don't delete it**.

### Domains (since 2026-07-30)

Prod is `eveindustry.net`, dev is `dev.eveindustry.net`. Each environment serves on exactly **one**
canonical host; every other name we answer on (`www.eveindustry.net` and the legacy
`eve-pi.failed.name` / `eve-pi-dev.failed.name`) is a permanent 301 to it via the
`canonical-redirect` Traefik middleware in the matching `evpi-gitops` overlay.

Two things force that shape, so don't "helpfully" start serving the app on a second origin: an EVE
application registers exactly **one** callback URL (prod and dev are therefore two separate
applications with different `EVE_CLIENT_ID`s — they cannot share one), and the session cookie is
set on whichever host completed `/auth/callback` with no `Domain` attribute, so a second serving
origin would be permanently logged out. Legacy names must stay in the `Certificate` `dnsNames` for
as long as we honour old links — a redirect still has to complete a TLS handshake first.

Changing a domain is a three-step cutover, in this order: DNS → cert names (wait for `Ready`) →
routes/redirect, then flip the callback in the developer portal and the `EVE_CALLBACK_URL` key of
the `eve-pi-env` secret together (Reloader only watches the TLS secret, so that one needs a manual
`rollout restart`).

---

## Cutting a release

After a batch of shipped changes on `main` is stable — not every commit, only on a meaningful
milestone or when asked:

```
git checkout main && git pull origin main
git tag -a vX.Y.Z -m "vX.Y.Z"   # PATCH for fixes, MINOR for features, MAJOR for breaking changes
git push origin vX.Y.Z
```

**Deciding X.Y.Z:** find the last tag (`git tag --sort=-v:refname | head`), then read what shipped
since it (`git log <last-tag>..HEAD --oneline`). Any `feat:` commit in that range means at least a
MINOR bump; if everything since the last tag is `fix:`/`chore:`/`docs:`/`perf:`, it's a PATCH;
MAJOR is for an actual breaking change (none yet as of `v0.1.0`). Decide the number yourself from
the commit log and just tag it — "cut a release" is the go-ahead, it doesn't need a round-trip to
confirm the version.

Pushing the tag triggers `.github/workflows/build.yml`, which builds
`ghcr.io/fredrik84/eve-pi-planner:vX.Y.Z` (alongside the usual `:latest`) and creates a GitHub
Release whose notes are the categorized commit log since the previous tag. The release step needs
full history, so the workflow's checkout uses `fetch-depth: 0`. First release: `v0.1.0`.

This is independent of the `:latest`/ArgoCD deploy path — tagging does not trigger a new deploy, it
only marks/publishes a version of whatever is already live on `main`.

### Why commit messages matter so much

The release step builds the changelog **directly from the commit log** since the previous tag
(`git log <prev>..<tag>`), grouped into Features / Fixes / Performance / Maintenance by the
`feat:`/`fix:`/`perf:` prefix — verbatim, with no editing pass in between. (It used to use
`gh release create --generate-notes`, but that itemizes merged **PRs** only, so this repo's
direct-push-to-`main` flow got an empty "What's Changed" + a bare compare link — switched to the
commit-log build 2026-07-17.) A vague commit (`fix stuff`, `wip`, `updates`) becomes a vague,
useless line in the public changelog.

---

## Feature flags (`app/features.py`)

Admin-controlled rollout (no staging env — this is how we stage). `pp_features(key, enabled,
updated_at)` stores public-visibility state; the known set is the code-defined `FEATURE_REGISTRY`
(key, label, description, default). A feature missing from the registry can't be toggled.
`ensure_features_table()` seeds missing rows at their default. Endpoints: `GET /api/features`
(public — returns every registered feature with its current `enabled` + the caller's `is_admin`),
`POST /api/features/{key}` (admin-gated via `require_admin`, body `{enabled}`).
`feature_enabled(key)` is a backend helper for server-side gating if ever needed.

**Frontend gating** (`planetary.js`): `_loadFeatures()` caches `/api/features`; `_featureActive(key,
dflt=false)` returns `enabled OR is_admin` (admins preview everything), falling back to `dflt` when
not yet loaded — pass `dflt=true` for retrofitted existing features so they never vanish on a load
hiccup, omit it for new features (fail-closed). Call sites: `onDashboardTabOpen`,
`onAnalyzeTabOpen`, `_refreshBaskets`, `renderFinalPlan` (split control). Admin → **Features**
sub-tab (`loadAdminFeatures`/`toggleFeature`) flips flags. `FEATURE_REGISTRY` in `app/features.py`
is the source of truth for the current flag set — don't duplicate the list here, it drifts.

---

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
Run it locally with `node scripts/lint_js.mjs`. It does **not** gate the deploy: this repo pushes
straight to main, and a lint step that can fail on an npx download must not block a hotfix. It
exists to make the failure visible, which is all the original bug needed.

---

## Epoch timestamps must be `double precision`, never `REAL`

On SQLite `REAL` is an 8-byte double; on **Postgres `real` is float4** — about 7 significant
digits, and a Unix epoch needs 10. So an epoch stored in a `REAL` column is quantised to ~64-128
seconds (measured on prod: wrote `1785409464.304`, read back `1785409400.0`).

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

## Fresh/stale-DB migration traps

All found on dev, invisible on prod because prod's DB grew with the schema:

1. `CREATE TABLE IF NOT EXISTS` followed by a failing `ALTER` **silently loses the table** — on
   Postgres a failed statement aborts the transaction and `_PgConn.execute` rolls back the
   uncommitted CREATE. `app/db.py add_columns()` now commits pending DDL before risking an ALTER;
   route every CREATE→ALTER site through it.
2. Lazy table creation (table made only by the endpoint that owns it) 500s other modules that query
   it directly. Create tables at startup.
3. `build_sde` used to skip everything if `types` had any rows — an all-or-nothing gate leaves a
   half-built SDE forever.

## Contribution review queue (`pp_planet_submissions`)

Only **admins** write to `pp_planets` directly. `POST /api/planets/import` branches on
`esi.is_admin(pp_session)`: admins → `_write_planet_rows` (live, **merging upsert** —
`INSERT … ON CONFLICT(system, planet_num) DO UPDATE` with `CASE WHEN excluded.col != 0` per P0 so a
blank/0 cell keeps the current value and a sparse paste never wipes good data; returns
`{queued:false,...}`); everyone else → the paste is stored verbatim in `pp_planet_submissions`
(status `pending`) and nothing touches the live DB (`{queued:true, submitted:N}`).

Parsing was split out of `import_planets` into `_parse_planet_rows(text, con) -> (rows, skipped,
errors)` (no writes) + `_write_planet_rows(con, rows)` so both the direct path and approval reuse
it. Admin review endpoints (admin-gated via `require_admin`):
`GET /api/planet-submissions?status=pending` (re-parses each `raw_text` for a preview, flags each
planet `exists` new-vs-overwrite against live `pp_planets`),
`POST /api/planet-submissions/{id}/approve` (re-parses + `_write_planet_rows`, marks `approved`),
`POST /api/planet-submissions/{id}/reject` (marks `rejected`, no write).

UI: a **Planet submissions** section at the top of the Admin tab
(`loadPlanetSubmissions`/`renderPlanetSubmissions`/`reviewPlanetSubmission` in `planetary.js`,
new/overwrite chips); `submitPlanetImport` handles the `queued` response ("submitted for review" vs
"imported"). A **Contribute** tab (`#tab-contribute`, static, no JS hook — `switchTab` is generic)
documents remote sensing (Agency → Resource Harvesting → Planets → Planetary Industry, hover a P0 →
`Resource Density: %`), the spreadsheet format, and the review flow.

## Profiles & shares

Both persist plan inputs. When adding a new `PlanRequest` field that a user sets, wire it into
**all three**: `pp_profiles` column (+ `ProfileSave` model + save/list SQL), the share payload, and
the frontend save/restore.

- **Profiles** (`pp_profiles`, per context): includes `overproduction_pct, preferred_systems,
  constellations, use_existing, factory_system, factory_output_per_hour, factory_character_ids`.
  Profiles do **not** store `chosen_systems` (a step-3 runtime choice).
- **Shares** (`pp_shares`, server-stored JSON, v2): payload keys
  `tid, pn, op, ps, ue, fs, fr, fc, cs, cc, plan`. `fr`=factory rate, `fc`=factory char ids,
  `cs`=chosen systems, `cc`=constellations. v2 stores the full computed `plan` so a link renders
  identically without re-running; the input keys let the recipient re-run/tweak.
