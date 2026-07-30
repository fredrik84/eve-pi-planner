# eve-pi-planner — TODO

Live backlog. Reviewed 2026-07-30. Anything not listed here is either shipped (see the git log /
release notes) or in **Closed** at the bottom — items in that section have been reasoned through
and should not be reopened without new evidence.

Each item states what it is, why it's open, and the first concrete step, so it can be picked up
cold.

---

## 1. Remove the dead `_muted` assignment

`app/planner_dashboard.py:213` assigns `_muted` and never reads it. Pyflakes flags it. Left in place
deliberately during the 2026-07-29 planner split so that change stayed a provable pure move.

- **First step:** delete the line; confirm `python3 -m pyflakes app/planner_dashboard.py` is clean.

## 2. Disconnect a character

Requested 2026-07-21. Characters can only ever be *added* (ESI login, `?market=1`, `?reactions=1`,
`?wallet=1`) — there is no in-UI removal. Surface it in the Settings modal under a Characters tab.

- **Open decision (blocks implementation):** unlink the reactions/PI *association* only, or delete
  the `pp_characters` row plus dependents — `pp_char_planets`, `pp_reaction_assignments`,
  `pp_char_industry_jobs`, and `pp_market_config.market_character_id` if it pointed at that char.
- **Constraint:** must be `require_context`-scoped (own characters only) per the privacy rule.
- **Already handled:** `_market_character` falls back to the first scoped char, so removing the
  designated market character degrades gracefully.

## 3. Required-skills-to-build (Industry)

The manufacturing plan should list the skills the account is **missing** to actually build the
target — you can't make a Revelation without capital production skills, and today the plan happily
schedules a job nobody can install.

- **Work:** parse `activities.manufacturing.skills` from blueprints.yaml into an SDE
  `blueprint_skills` table via SDE backfill; fetch each character's FULL skill list (we currently map
  only a handful in `SKILL_IDS`); gather required skills across the whole build tree; compare against
  the account's best character; show missing skill + level.
- **Size:** the largest open item — SDE backfill plus a new ESI fetch shape.

## 4. Skill-optimization advisor page (Industry)

Tell the user what to train to raise overall output: more reaction/manufacturing slots if they're
constantly full, CC-upgrade / Interplanetary Consolidation for PI, higher Industry / Advanced
Industry for job time. Extends the existing PI `skill_roi` advisor.

- **Needs scoping first** — this is the vaguest item on the list. Include a "train X for Y SP →
  +Z% output" framing with a threshold so it doesn't nag someone already training toward max.
- **Depends on** the full-skill-list fetch from item 4; do them in that order.

## 5. Hand-built / custom colony layouts

Hybrid-colony detection shipped. Broader tracking of player-designed layouts (colonies that don't
match any template we generate) is still unscoped.

- **First step:** decide what the feature would actually *do* for the user before building anything —
  detection alone has no action attached to it today.

## 6. Layout engine — known gaps

Both documented in CLAUDE.md, neither with a demand signal yet:

- Intermediate **storage facilities** aren't modelled. The hand-built SHPC reference adds 3 on top of
  the 3 launchpads to buffer P2/P3 via storage round-trips; our generator routes intermediates
  tier-to-tier directly.
- **CPU/PG is budgeted, not simulated** — `compute_resources` estimates from idealised pin
  coordinates; there's no simulation of the real in-client fit.

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
