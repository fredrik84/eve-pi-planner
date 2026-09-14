# Reactions and Manufacturing code health — 2026-09-14

The core Manufacturing suite is healthy, but the broader production test suite is not green.
This pass fixed reproducible allocation defects and removed obsolete code. It did not establish
that every planning path is correct or resolve the existing test failures listed below.

## Changes

- Reaction customer orders now size character eligibility and per-character allocation against
  concurrent stages. Sequential stages reuse reactors in both Balanced and Fastest modes;
  siblings share a stage budget. The existing feature flag retains the legacy sum budget when
  disabled. Formula caps, capacity-proportional run shares, stock accounting and the final physical
  reservation guard remain in force.
- A three-stage chain with one product per stage can use one reactor. In the two-reactor fixture,
  two sequential 100-run stages with one-hour cycles now take two 50-run jobs each, meeting the
  48-hour cadence plus its three-hour grace. Previously each stage got one 100-hour job.
- Manufacturing's scheduler reuses the actual released slot number. Previously, with slot 0
  finishing while slot 1 remained busy, the next job was also labelled slot 1. Zero-duration
  dependencies now unlock their consumers instead of being mistaken for unstarted work.
- Scheduling priority is sorted once per simulation, rather than sorting ready work at each
  completion event. Started jobs also leave the pending scan.
- Removed the unused `_lean_hosts`, `_compact_hosts`, `_WORTH_A_LOGIN`, an unused allocation total,
  a duplicated stage-load branch, and unreachable scheduler bookkeeping. Removed 154 unused
  imported names across Manufacturing modules and added one required `heapq` import. Router
  registration and intentional package exports were preserved.
- Removed two tests of the retired login-threshold policy and updated host-packing checks to call
  the active implementation. Added behavioral tests of actual allocation and scheduling.

## Verification

Tests ran against the current working tree copied into the local Docker container, with the local
server restarted for API checks. No production fixtures were created.

- `test_industry.py`: all 1,130 checks pass before and after the refactor.
- `test_production_allocation.py`: all 12 tests pass, including 24 batch/capacity combinations and
  50 seeded dependency graphs checking demand, stage capacity, physical slots and print limits.
  Six regression tests failed against the original code before their fixes.
- Passing existing suites: order cadence, parallel stages, reaction stock, profit clock, reaction
  job fees, manual blueprints, formula stock, stock reservations, cost basis and feature/API checks.
- Frontend syntax checks and the repository's `no-undef` lint pass. The reaction deadline,
  Manufacturing stage-depth and client-routing JavaScript tests pass. Python compilation and
  `git diff --check` pass; `static/reactions.js` contains no NUL bytes.
- An independent comparison against the original scheduler produced identical start/end times
  and unscheduled-job lists on 200 seeded dependency graphs with positive durations and print
  limits. Slot identifiers intentionally differ where the original reused an occupied number.
- Synthetic scheduler benchmark: 1,000 independent jobs, 20 slots, durations `(i*17)%100+1`,
  median of three runs in the same container: **0.3252s before, 0.1662s after** (about 49% less
  scheduler time). This is not an end-to-end page-load measurement.

## Existing failures and remaining risks

The following failures reproduced identically in an isolated checkout of baseline `f1e26c2` using
the same local database, as well as on the modified code:

| Suite | Failure |
| --- | --- |
| `test_reactions.py` | Formula-capped API fixture does not automatically assign; expected assignment rows are empty. |
| `test_level_runs.py` | Two old compaction expectations fail: 15 jobs become 21, and Oxy-Organic Solvents uses four jobs rather than the expected couple. |
| `test_reactions_standalone.py` | Three expectations disagree with current cadence clearing/default behavior and dropdown wiring. |
| `test_blueprint_paste.py` | Missing owned-product fixture entry raises `KeyError: 33359`. |

The PI distribution suite also fails its three fuel-block scenarios locally, matching the
documented local fixture limitation in `docs/workflow.md`. Two embedded checks additionally report
import skips. These results do not validate those paths.

The next quality work should reconcile these tests with the current freshness, cadence and
blueprint-ownership contracts, then add an isolated backend-test job to CI. Currently CI runs
frontend syntax/lint, with lint allowed to fail, and the image build does not depend on it; there
is no backend regression gate.

`app/reactions/jobs.py` still has approximately 5,300 lines and `static/reactions.js` approximately
4,660. Allocation, reconciliation, persistence and rendering have accumulated in those files.
Several dashboard reads still repair persistent plans, and broad exception handlers can obscure
failures; for example `_slot_pool` silently falls back if reaction reservation lookup fails.
These are maintenance risks, not newly reproduced defects in this pass. A subsequent module split
should isolate allocation and reconciliation behind their behavioral tests, following the existing
split-review guidance, rather than moving all the code at once.
