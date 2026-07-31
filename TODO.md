# eve-pi-planner — TODO

Live backlog. Reviewed 2026-07-30. Anything not listed here is either shipped (see the git log /
release notes) or in **Closed** at the bottom — items in that section have been reasoned through
and should not be reopened without new evidence.

Each item states what it is, why it's open, and the first concrete step, so it can be picked up
cold.

---

## 1. Required-skills-to-build (Industry) — SHIPPED 2026-07-30, behind `required_skills`

Built and live in admin-preview. `blueprint_skills` (SDE, from `activities.<activity>.skills` for
BOTH manufacturing and reactions — 9,221 rows, 83 distinct skills), `pp_char_skills` (per-character
full skill list), `app/industry/skills.py`, and a panel on the Industry plan. `test_required_skills.py`,
19 assertions.

One premise in the original write-up was wrong and worth remembering: **no new ESI fetch shape was
needed**. `_fetch_skills` in app/esi.py was already pulling the character's entire skill list from
`/characters/{id}/skills/` and discarding all but the ~10 in `SKILL_IDS`, so this needed no new call,
no new scope, and no extra rate-limit budget — only somewhere to put the rest.

`assign_characters` was made skill-aware on top of this (2026-07-30): candidates are tiered
capable → unknown → incapable, capacity still decides within a tier, and a forced fallback is
stamped `skill_ok: False` rather than hidden. Wired into both `/api/industry/plan` and the queue
plan.

Follow-ups, neither blocking:

- **The to-install checklist is still skill-blind.** `install_block()` in `app/industry/orders.py`
  (behind `POST /api/industry/to-install`, and inlined into the queue plan) derives its OWN
  assignment for the ready wave: it walks `_slot_pool`'s free slots most-loaded-first and names a
  character per job, ignoring what `assign_characters` already decided. So the main screen's
  "start these now" list can still tell you to install a job on a character who lacks the skills,
  even though the plan below it marks that same job ⚠. Note the data is already in hand — wave 0's
  tasks carry `skill_ok` and an assignee by the time `install_block` sees them, so this is about
  reconciling two assignment paths, not fetching anything new. The honest fix is probably for
  install_block to respect the scheduler's assignment instead of recomputing one; check first
  whether its most-loaded-first ordering is doing something the scheduler's spread-the-work rule
  isn't, because that difference is deliberate.
- **Item 2 (skill-optimization advisor) is now unblocked** — it was waiting on the full-skill-list
  fetch, which `pp_char_skills` now provides.

## 2. Skill-optimization advisor page (Industry)

Tell the user what to train to raise overall output: more reaction/manufacturing slots if they're
constantly full, CC-upgrade / Interplanetary Consolidation for PI, higher Industry / Advanced
Industry for job time. Extends the existing PI `skill_roi` advisor.

- **Needs scoping first** — this is the vaguest item on the list. Include a "train X for Y SP →
  +Z% output" framing with a threshold so it doesn't nag someone already training toward max.
- **No longer blocked** (2026-07-30): the full skill list it was waiting on now lands in
  `pp_char_skills` whenever `required_skills` is on. Scoping is the only thing left in the way.

## 3. Hand-built / custom colony layouts

Hybrid-colony detection shipped. Broader tracking of player-designed layouts (colonies that don't
match any template we generate) is still unscoped.

- **First step:** decide what the feature would actually *do* for the user before building anything —
  detection alone has no action attached to it today.

## 4. Layout engine — known gaps

Both documented in CLAUDE.md, neither with a demand signal yet:

- Intermediate **storage facilities** aren't modelled. The hand-built SHPC reference adds 3 on top of
  the 3 launchpads to buffer P2/P3 via storage round-trips; our generator routes intermediates
  tier-to-tier directly.
- **CPU/PG is budgeted, not simulated** — `compute_resources` estimates from idealised pin
  coordinates; there's no simulation of the real in-client fit.

---

## 5. Job timestamps are stored at float4 precision (Postgres)

`pp_job_runs.started_at` / `ended_at` and `pp_job_leases.lease_until` are declared `REAL`. On SQLite
that's an 8-byte double, but on Postgres `real` is **float4** — about 7 significant digits, and a Unix
epoch timestamp needs 10. Every stored job time is therefore quantised to ~64 seconds (measured on
prod 2026-07-30: wrote `1785409464.304`, read back `1785409400.0`).

Consequences, all on the admin Jobs page: displayed last-run times can be a minute off, and the
run **duration** (`ended_at - started_at`) is meaningless for anything shorter than that — a 12s job
shows as 0s or as ±64s. The lease is unaffected in practice (64s of slop against a 900s TTL).

Fix is a widening migration, `ALTER TABLE ... ALTER COLUMN ... TYPE double precision` on the three
columns — a table rewrite under an ACCESS EXCLUSIVE lock, but the table is a few thousand rows so it
is effectively instant. Mind the fresh/stale-DB migration trap in CLAUDE.md: the ALTER must not share
a transaction with the `CREATE TABLE IF NOT EXISTS` in `ensure_job_tables()`, or a failure takes the
tables with it.

Found while fixing the "healthy daily jobs reported as never run" bug (`job_summary`, 2026-07-30);
left alone deliberately because that fix needed no schema change.

---

## Perf regression protocol (fuel-block planner)

The fuel-block slowness reported 2026-07-06 is **fixed** — verified in code 2026-07-30: the
Redis-shared `packed_rate` cache (`app/fuelblocks.py`, via `_layout_cache_get_or_compute`) and the
cheap preview path (`is_preview`, `app/fuelblock_planner.py`) are both in place. This is the
procedure to confirm it stayed fixed; run it after touching `fuelblock_planner.py`,
`fuelblocks.py`, `layout.py`, or the layout caches.

**A. Preview must do no factory geometry** (the fix that matters most, and the one a refactor is
most likely to silently undo). In the container:

```python
import app.planner_advisor as pa, app.fuelblock_planner as fp
calls = []
orig = pa._factory_pack_max_diameter
pa._factory_pack_max_diameter = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
# preview  = no chosen_systems  -> expect 0 calls
# full plan = chosen_systems set -> expect >0 calls (was 21 when first measured)
```

Expected: **0 calls in preview, non-zero with `chosen_systems`.** A non-zero preview count means
`is_preview` stopped being threaded through and every recommendation request is paying full
factory-placement geometry again.

**B. Cold-process cost must not return.** Restart the pod, then time the first fuel-block plan and a
second identical one:

```
ssh node01.failed.name "sudo k3s kubectl -n production rollout restart deploy/eve-pi-planner"
# then, from the app's own instrumentation:
ssh node01.failed.name "sudo k3s kubectl -n production logs -l app=eve-pi-planner --tail=200 \
  | grep -E 'fuelblock\.(fetch_planets_and_recs|extractor_pipeline)'"
```

The first call after a restart should be close to the warm one — that's the whole point of the L2
Redis cache. A large cold/warm gap means the cache degraded to in-process only (the exact bug fixed
on 2026-07-06, and again in `fuelblocks.py` afterwards).

**C. Cross-replica sharing.** With 2 replicas, a plan computed on one pod should leave the other
warm. Issue the same request repeatedly and confirm timings don't alternate fast/slow — alternation
means the Redis layer isn't being hit and each pod is caching alone.

**Regression threshold:** the original user-visible symptom was "pressed find, stuck >30s". Treat
anything over a few seconds on a warm path as a regression worth tracing rather than tuning.

---

## Closed — do not reopen without new evidence

| Item | Verdict |
|---|---|
| Per-account settings consolidation (`settings_store.py`) | **Won't do** (2026-07-30). The duplication is the cheap part (~60-80 lines of upsert); validation, which dominates the handlers, survives any scheme. 2 of 7 tables aren't settings rows at all. Trades typed columns for a JSON blob against this repo's additive-migration convention. Prod holds only 10 rows total, so the old "too risky" framing was wrong — it's low *value*, not high risk. |
| Distribution "lever 1 — cross-character rich-planet reuse" | **Wrong lever, not unfinished work.** Per-character planet-pick shipped (`db56e2e`, `_waterfill_new_slots` regret heuristic). The residual "thin planets" symptom is lever 2 over-allocating, governed by the **min-density cap**, plus genuine data constraints (a P0 with one planet in-system). |
| P1 extractor→factory routing | **Won't build** (2026-07-08). Workflow is pooled and P1 is fungible once extracted; routing would impose a fake point-to-point constraint. Revisit only if actual point-to-point hauling automation is described. |
| Frontend CPU offload, phase 3 | **Rejected for now**, not deferred — the investigation found the real hotspot was cacheable server-side (already done), not a JS-offload candidate. Reasoning trail is in the project notes; don't redo it. |
| Skyhook storage bar | **Blocked** — ESI does not expose skyhook cargo; no deterministic formula to fall back on. Manual-checkpoint design was proposed and declined. Revisit only if CCP ships an endpoint. |
| Deleting the legacy Find-Buildables analyzer | **Keep it** (2026-07-30). Live, ungated, default PI-planner sub-tab; `highspy`+`numpy` are lazy-imported so they cost image size only. Promoted to `app/analyzer.py` instead. |
| Browser-level tests for `api()`/`toast()` | **Dismissed** (2026-07-30). Would mean introducing a browser-test harness this repo doesn't have; manual testing already catches the residual breakages at the expected rate. |
| Alert-engine rename | **Done** (2026-07-30). `app/colony_alerts.py` → `app/alerts.py`, `compute_colony_alerts()` → `compute_alerts()`, `test_colony_alerts.py` → `test_alerts.py`. Pure rename, zero behaviour change; `test_alerts.py` passes in-container incl. the live `/api/dashboard` layer. |
| Remove the dead `_muted` assignment | **Done** (2026-07-30). Deleted; `pyflakes app/planner_dashboard.py` is clean. `_alert` stays — it still supplies the display thresholds; only the mute set was dead (muting moved inside `compute_alerts()`). |
| Disconnect a character | **Done** (2026-07-30). Premise was stale: the UI button and `DELETE /api/characters/{id}` already shipped. The real bug was that it cleared 2 of 10 per-character tables. Now deletes all of them, clears the market-reader + saved-plan references, re-points the session instead of logging you out, revokes the ESI grant, and keeps `pp_bugs` + the completions ledgers. Hard delete, not soft unlink — a retained row keeps a live refresh token. `test_disconnect_character.py`, 6 groups. |
| `DELETE /api/me` orphaned rows | **Done** (2026-07-30). Cleared 3 per-character + 4 context tables, orphaning ~20 others. Now works from shared `_CHAR_OWNED_TABLES` + `_CONTEXT_OWNED_TABLES` in `app/esi.py` (9 + 19 tables, verified). Completions ledgers and per-character records DO go here (unlike the per-character disconnect — the account itself is going away); `pp_bugs` is anonymised so admins keep the report; group-scoped markets/settings survive. `pp_shares`/`pp_inventory_shares` have no owner column and cannot be cleaned by account, by construction. `test_delete_account.py`, 5 groups. |
