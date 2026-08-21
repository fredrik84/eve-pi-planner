# Production phases: Phase 5 and manual acceptance protocol

This is the click-by-click acceptance check for the shared Reactions and Manufacturing workflow.
It tests what a player sees on dev; it is not a substitute for the automated suites.

## Contents

- [Phase 5 summary](#phase-5-summary) — the proposed final orchestration and cleanup phase
- [Before testing](#before-testing) — account, ESI, formula, and evidence setup
- [Phase 1 — task-first pages](#phase-1--task-first-pages) — layout and primary flows
- [Phase 2 — automatic planning](#phase-2--automatic-planning) — cadence, recurring work, warnings, and parity
- [Phase 3 — Manufacturing hand-off](#phase-3--manufacturing-hand-off) — one build creating and maintaining reaction work
- [Phase 4 — shared work and ownership](#phase-4--shared-work-and-ownership) — aggregation, priority, and safe removal
- [Phase 5 acceptance criteria](#phase-5-acceptance-criteria) — checks to run after Phase 5 is built
- [Result sheet](#result-sheet) — what to record when something differs

## Phase 5 summary

Phases 1–4 establish the two planners, automate assignment, hand ready Manufacturing reactions to
Reactions, and preserve shared work correctly. Phase 5 should make that combined system easy to
operate when reality differs from the plan.

The proposed scope is:

1. **One clear attention queue.** Slot shortages, missing formulas, quantity conflicts, and jobs
   still running after a build is removed use the same small vocabulary: waiting, ready, running,
   blocked, and done. Each warning explains what was kept safe and offers only relevant choices.
2. **Automatic recovery.** After an ESI refresh or capacity becoming free, blocked ready work is
   retried in priority order. Manual retry remains available; silent deletion and duplicate work do
   not.
3. **Two-way traceability.** A Manufacturing reaction stage links directly to its Reactions order,
   and that order identifies and links to every Manufacturing build that owns a share.
4. **Consistent controls and feedback.** Both pages put metrics first, use the same refresh outcome,
   close transient menus, and return the user to Overview after creating work.
5. **Remove the old parallel paths.** Once the dev acceptance checks pass, retire redundant
   Manufacturing-side reaction allocation/splitting displays and compatibility response fields.
   Manufacturing keeps planning demand and readiness; Reactions remains the sole owner of reaction
   slot allocation, queueing, cadence, and live runtime.

Intentional difference: Manufacturing may show later dependent stages, while Reactions reserves
slots only for work that is ready. Alignment must not make Manufacturing reserve future reaction
work early.

## Before testing

Use `https://dev.eveindustry.net` and a tester account with:

- at least one ESI-connected character with reaction skills and at least one free reaction slot;
- reaction formulas inventoried through ESI or pasted in Settings;
- one Manufacturing product whose in-house build tree contains reactions;
- permission to see **Reactions → Customer orders** and Manufacturing build rules.

Choose and write down:

- **R1:** a simple reaction product for customer-order tests;
- **M1:** a manufactured product that needs R1, or another known reaction, when reactions are made;
- **M2:** a second manufactured product that shares at least one reaction input with M1;
- the connected character's total/free reaction slots before starting.

Use small quantities for ordinary tests. Tests marked **LIVE EVE ACTION** install or observe a real
job and should only be run when that is acceptable. For every failure, record the order/build names,
the time, expected result, observed result, and a screenshot. Keep the browser console open only if
you are comfortable doing so; a visible user-facing failure is enough to report the bug.

## Phase 1 — task-first pages

### P1-R1 — Reactions keeps decisions and capacity visible

1. Click **Reactions**, then **Overview**.
2. Confirm **Plan value & capacity** is the first content card.
3. Confirm the character/capacity rows are visible without opening a fold or pressing More.
4. Click **More**, then **Refresh ESI jobs**.
5. Wait for the refresh to finish and reload the browser once.

Pass when the metrics remain at the top, characters remain visible, and **More** is closed after the
action and after reload. The More popup must not remain stuck on screen.

### P1-R2 — creating a reaction order returns to work

1. Click **Reactions → Customer orders → + New order**.
2. In **Product**, search for and select R1.
3. Enter a small value in **Units wanted**; optionally enter `Phase 1 test` in **Client**.
4. Leave **Recurring** off. Click **Review**.
5. Check the product, units, runs, material cost, and total shown in the review.
6. Click **Create order**.

Pass when the modal closes, **Overview** is selected, a success message appears, and the new work is
visible without another navigation click. The button must not remain on `Creating…`.

### P1-M1 — Manufacturing keeps decisions and capacity visible

1. Click **Manufacturing → Overview**.
2. If a build exists, confirm its metrics/summary and character/job-slot capacity appear before its
   task list. If no build exists, confirm the empty-state setup and lifetime information appear
   before **Add manufacturing work**.
3. Click **More**, then **Refresh ESI jobs** when that action is present.
4. Reload the browser once.

Pass when the metrics/summary remain at the top and **More** closes after use and reload.

### P1-M2 — adding Manufacturing work returns to Overview

1. Click **Add manufacturing work**.
2. In **Product to build**, search for and select a cheap test product.
3. Enter a small **Quantity** and `Phase 1 test` in **For**.
4. Click **Preview**, inspect the result, then click **Add to build**.

Pass when the modal closes, **Overview** is selected, and the new build appears in **Your build**
without an extra click.

## Phase 2 — automatic planning

### P2-R1 — weekly cadence has a real default and ceiling

1. Open **Settings → Build rules**.
2. Find the reaction job-length rule described as the longest reaction job/window.
3. Clear its value (or enter `0`) and click **Save build rules**.
4. Reopen **Settings → Build rules**.

Pass when the saved/effective value is **7 days**, not blank. Create or re-plan enough R1 work to
need multiple jobs and inspect the assigned cards on **Reactions → Overview**. Normal jobs should be
split around the seven-day ceiling; no unexplained `9d 1h` job should appear. A formula whose single
run cannot fit is allowed only with an explicit overrun explanation.

### P2-R2 — a recurring customer order assigns immediately

1. Click **Reactions → Customer orders → + New order**.
2. Select R1 and enter a quantity that fits the currently free slots.
3. Check **Recurring** and leave **every 7 days**.
4. Click **Review**, then **Create order**.
5. On **Overview**, note the assigned run count. Return to **Customer orders** and open the order.

Pass when the modal closes, the first cycle is assigned immediately, the Overview updates without a
reload, and the order says it repeats every seven days with a next-cycle time.

### P2-R3 — completing a recurring cycle preserves recurrence

1. Open the recurring order from **Reactions → Customer orders**.
2. Click **Complete this cycle** and confirm.
3. Reopen the order.

Pass when current slots are freed, the order remains recurring rather than becoming a one-off done
order, and the next batch is scheduled for its cadence point. Do not wait seven days during this
protocol; the displayed next-cycle time is the acceptance evidence.

### P2-R4 — insufficient slots asks instead of hiding the problem

1. Record the free-slot count on **Reactions → Overview**.
2. Create another recurring order with enough R1 units to require more slots than are free: use
   **Customer orders → + New order**, select R1, check **Recurring**, **Review**, then **Create order**.
3. Read the warning and open the saved order.

Pass when the user gets a visible warning that capacity is insufficient, the order is still saved,
and the UI offers meaningful choices such as retry, skip this cycle, or stop recurring. Existing
assignments must not disappear. If the test unexpectedly fits, increase the quantity and repeat.

### P2-M1 — equivalent transient controls behave alike

1. On **Manufacturing → Overview**, open **More** and choose **Refresh ESI jobs**.
2. While the page refreshes, change to **Materials & blueprints**, then return to **Overview**.
3. Reload the browser.

Pass when More does not remain sticky, the chosen page does not acquire a duplicated toolbar, and
the status refresh does not hide the top metrics/summary. Manufacturing need not expose reaction-only
controls such as recurring customer cycles.

## Phase 3 — Manufacturing hand-off

### P3-1 — a ready reaction becomes one linked Reactions order

1. Open **Settings → Build rules** and set reactions to be made here for the category used by M1.
   Click **Save build rules**.
2. Click **Manufacturing → Add manufacturing work**.
3. Select M1, enter a small quantity, enter `Phase 3 M1` in **For**, click **Preview**, then
   **Add to build**.
4. On **Manufacturing → Pipeline**, find the reaction stage and note its required runs/runtime.
5. Open **Reactions → Customer orders**.

Pass when exactly one matching order has a **Manufacturing** badge, its run requirement agrees with
the ready Manufacturing stage, and Reactions—not Manufacturing—has assigned its slots and cadence.
The order should identify its Manufacturing source.

### P3-2 — increasing a build changes only the outstanding demand

1. Return to **Manufacturing → Overview**.
2. Click the pencil (**✎**) on `Phase 3 M1`, increase **Quantity**, and submit the edit.
3. Return to **Reactions → Customer orders** and reopen the linked order.

Pass when its target increases to the new requirement and only the additional outstanding work is
allocated. Existing/running work must not be duplicated.

### P3-3 — decreasing a build respects committed work

1. Edit the same Manufacturing build and reduce **Quantity**, but keep it above already committed
   output. Submit the edit and inspect the linked Reactions order.
2. If safe, try a second reduction below already running/completed reaction work.

Pass when uncommitted demand shrinks. A reduction below committed work produces a clear conflict or
floor instead of deleting or pretending to undo the committed runs.

### P3-4 — removing pending work releases it

1. Create another small M1 build labelled `Phase 3 remove` and let its linked reaction remain
   pending—not installed in EVE.
2. On **Manufacturing → Overview**, click its **Remove from the build** (**✕**) button and confirm.
3. Check **Reactions → Customer orders** and **Overview**.

Pass when the pending linked order is cancelled/closed and its pending slot reservation is released.

### P3-5 — running work is never silently erased (**LIVE EVE ACTION**)

1. Install one linked reaction job in EVE for a disposable test build.
2. In the site, click **Reactions → Overview → More → Refresh ESI jobs**.
3. Confirm the job is shown as running, then remove its source Manufacturing build.

Pass when the running reaction remains tracked and the site warns that committed work could not be
removed. It must not create a replacement copy of the same run.

## Phase 4 — shared work and ownership

### P4-1 — two builds share one physical reaction order

1. Open **Settings → Build rules** and ensure **Plan each build apart** is off. Save.
2. Add M1 with label `Phase 4 owner A`: **Manufacturing → Add manufacturing work → Preview → Add
   to build**.
3. Add M2 with label `Phase 4 owner B` the same way. Use quantities that make both demand the same
   reaction component.
4. Open **Manufacturing → Pipeline**, then **Reactions → Customer orders**.

Pass when the shared component is planned as one batch and produces one physical linked reaction
order, not one duplicate per Manufacturing build. Its total runs equal the combined ready need, and
both owners remain attributable.

### P4-2 — removing one owner adjusts, not destroys, shared work

1. Remove `Phase 4 owner A` with its **✕ Remove from the build** button and confirm.
2. Reopen the shared order under **Reactions → Customer orders**.

Pass when the same shared order remains for owner B, its target/share decreases correctly, and no
new duplicate order appears. Remove owner B only after recording this result; the final removal
should close pending shared work and free its reservations.

### P4-3 — priority decides who receives scarce capacity

1. Ensure two open reaction customer orders need the same last free capacity. Create them with
   **Customer orders → + New order → Review → Create order** if needed.
2. In **Customer orders**, use **▲ Higher priority** and **▼ Lower priority** so the intended winner
   is first.
3. Free one slot by completing/removing disposable pending work, then click **Reactions → Overview
   → More → Refresh ESI jobs**. Also revisit **Manufacturing → Overview** if either order is linked.

Pass when the higher-priority ready order receives capacity before the lower-priority order. Running
jobs must not be pre-empted; priority governs the next available assignment.

### P4-4 — per-build isolation remains an explicit alternative

1. Open **Settings → Build rules**, enable **Plan each build apart**, and click **Save build rules**.
2. Preview two small builds sharing a component.

Pass when the UI explicitly warns that shared components will be made separately and shows the cost
trade-off. Separate reaction demand in this mode is intentional, not a Phase 4 aggregation failure.
Turn the setting off after the test unless separate delivery containers are desired.

## Phase 5 acceptance criteria

These checks are **pending implementation**. Run them after Phase 5 is built:

1. Create a formula-missing order, a slot-blocked order, and a linked quantity conflict. Confirm one
   attention surface lists each once, names its source, explains what is safe, and presents only
   valid actions.
2. Resolve the missing formula or free a slot, then click **Refresh ESI jobs** once. Confirm eligible
   work retries automatically in visible priority order without duplicate assignments.
3. From a Manufacturing reaction stage, click its linked order and arrive at the correct Reactions
   order. Follow its Manufacturing owner link back. For shared work, verify every owner is shown.
4. Compare both Overview pages during waiting, ready, running, blocked, and done states. Confirm the
   same terms and refresh result are used, while Manufacturing still shows later dependent stages.
5. Repeat P3-1 through P4-3 after the legacy paths are removed. Network/API compatibility cleanup is
   accepted only if the visible workflow and all automated suites remain unchanged.

## Result sheet

Copy one row per test into the issue or testing note:

| Test | Pass / fail | Build or order names/IDs | Observed result | Screenshot/time |
|---|---|---|---|---|
| P1-R1 |  |  |  |  |

When a number differs, record both numbers and where each was shown—for example, `Cost to produce:
358m; Shopping list: 260.95m`—plus whether pasted/ESI inventory was present. That distinction lets
us tell a valid stock deduction from inconsistent costing.
