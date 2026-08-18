# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them. A `-residual` id is the
leftover of an item that shipped; the shipped part is in the archive under the bare number.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-18.

---

## 34-residual. The next two files, if the size problem comes back

§34 split the three big Industry modules and `static/industry.js` (archive). Not a task yet, just
the list of what to pick up if it returns:

- `app/industry/orders.py` is 1,057 and `assets.py` 1,004 — under the bar that opened §34, so
  deliberately not split.
- **Adjacent, and a separate item if it is ever opened:** `app/reactions/jobs.py` is **3,920 lines**
  and `static/reactions.js` is **3,936** — same shape, same argument, different service. The
  frontend seams from §34 did generalise, so a sibling item for Reactions is a reasonable thing to
  open. **Do not widen §34 into it.**

The method is the reusable part and it is written down: propose the split in writing, have it
reviewed before a line moves, derive the cross-module imports from the AST rather than by hand, and
prove each chunk byte-identical to its source range.

## 36-residual. Per-order shopping lists show the queue's stock, not the order's

`industry_sourced_counts` shipped (archive). The per-order plans path annotates `have`/`to_buy` from
the queue-wide pool, so with `per_order_plans` on, the figure is the queue's view rather than each
order's. **Correct for the combined list that is actually rendered today** — revisit only if
per-order shopping lists ever get their own UI, which is the thing that would make the distinction
visible.

## 37a. Alert cadence — what is left

§37 (check before nagging, back off on repeats), §37b (the same for reaction jobs, plus
instrumentation) and §37c (the lapsed-jobs-scope prompt, which turned the last `continue` into a
real `suppressed:no_jobs_scope`) have all shipped — archive, and the design is in
[docs/platform.md](docs/platform.md#check-before-nagging-and-nag-less-each-time-alert_rescan_backoff).
**Nothing here is a build any more.** What is left is one measurement to read and two things
recorded so they are not rediscovered as surprises.

- **Watch the first week, now that it is measurable.** Two questions, one query:

  ```sql
  SELECT status, COUNT(*) FROM pp_notification_log
   WHERE sent_at > '2026-08-19' AND status <> 'ok' GROUP BY status ORDER BY 2 DESC;
  ```

  `prevented` is the feature paying for itself. `suppressed:retry_brake` piling up means a
  character is sitting amber with its alerts silently paused — the failure mode worth catching.
  `suppressed:over_budget` means the per-tick scan cap is genuinely being hit. Needs a few days of
  real ticks with `alert_rescan_backoff` and `alert_rescan_reactions` on before it says anything.
  `suppressed:no_jobs_scope` (§37c) is the one to read as a to-do rather than a statistic: each one
  is a character whose reaction jobs are frozen, and the page now says exactly how to fix it.

- **The scan budget is per PROCESS, not per app.** Prod runs 6 (2 replicas × 3 workers), each with
  its own module global. The advisory lock serialises them and `_recently_notified` empties the
  second runner's alert list before it reaches a scan, so real spend stays near one process's
  ceiling — but the constant's own guarantee is 20 per process. Making it a true app-wide cap means
  moving it into the DB alongside the job lease. **Only worth doing if the account count grows a
  lot** — no action today.

- **Clock skew is unguarded.** The `esi_expires` gate compares ESI's absolute `Expires` against
  local `time.time()`, so a pod whose clock runs fast buys premature requests. Low risk on NTP'd
  nodes and not worth a mechanism, but it is the one way the never-query-before-`Expires` rule
  could break without a code change. **Recorded, not scheduled.**

## 38-residual. Close bug report 3

The bug is **fixed** (archive). The report row is still open: `POST /api/bugs/{id}/status` with
`complete`, or the Admin tab. It needs an admin session, so it is the user's to do. **Do not UPDATE
`pp_bugs` by hand.**

## 40. Manufacturing's queue-plan is uncached server-side — investigated, deliberately NOT built yet

Investigated 2026-08-18. **The felt-latency symptom Reactions had is not actually present here**,
which changes the shape of this item:

- `onIndustryTabOpen` → `indRefreshStatus` (`static/industry.js:335`) already paints instantly from
  a localStorage cache (`_indReadPlanCache`, keyed on the queue signature + the deployed JS
  version, 15-minute max age) and only THEN calls `POST /api/industry/queue-plan` in the background
  to check it and repaint if anything changed. That's a stale-while-revalidate pattern Reactions
  never had — the queue status view does not block on the network the way the Reactions dashboard
  did before §39's sibling fix.
- The single-product preview modal (`indRunPlan` → `POST /api/industry/plan`,
  `static/industry-plan.js:104`) is deliberately always-live, no cache — it's an interactive
  what-if tool (drag a knob, see the new plan), so caching it would be wrong, not slow.
- `_run_queue_plan` (`app/industry/orders.py:605`) — the shared core both endpoints call — IS
  genuinely expensive and uncached server-side: full recipe-graph resolution, live market pricing,
  a `build_plan` tree walk per queued product, skill/character assignment. So there's a real
  server-load argument for caching it, same as Reactions — just not a user-facing latency one.

**Why this wasn't just built the way §39/Reactions' cache was:** Reactions' cache had maybe eight
write endpoints across three files to invalidate correctly. Manufacturing's plan depends on order
CRUD, build-rule settings, ME/TE overrides, the always-buy blacklist, the reaction policy, structure
and rig configuration, and sourcing/stock ticks — spread across `app/industry/orders.py`,
`settings.py`, `structures.py`, `blueprints/*.py`, and more. Missing one of those invalidation paths
means silently serving a stale cost/schedule in a tool whose entire job is telling a builder what to
buy and what it costs — the one place in this app where "quietly wrong" is worse than "quietly
slow." That's a correctness call, not a mechanical port of the Reactions pattern, so it wasn't made
solo.

**Left for a real decision:** either (a) a genuine reduction-in-server-load pass — enumerate every
write path above and wire `cache_invalidate` the way Reactions got it, accepting the larger surface
and testing it thoroughly; or (b) leave it alone — the felt-latency problem this item was opened to
fix already has a working answer, and the remaining cost is server CPU, not user-facing lag.

## 41. Manufacturing AND Reactions are both one long scroll — split BOTH to one shared pattern

Manufacturing has grown to carry Do This Now, the metrics tiles, missing blueprints, the shopping
list, and the build pipeline all on one continuous scroll — too much to take in at once. Reactions
has the same shape of problem (dashboard + metrics + shopping list + orders + the advanced
opportunity table all on one page), softened today only by `<details>` folds on the heavier
sections (`rxShoppingCard`, `rxOrdersCard`, `rxAdvancedCard` — collapsed by default, see
`static/index.html` ~592-680).

**Explicitly one item, not two** (2026-08-18, user): design the split ONCE and apply it to both
pages, so Manufacturing and Reactions don't end up with two different navigation/section idioms for
the same underlying problem (landing dashboard vs. drill-down detail). Reactions' existing
`<details>` folds are the closer-to-shipped half of a pattern already — worth treating as the
starting draft for the shared design rather than starting from a blank page, but they're folds on
ONE page, not the tabs/sub-nav split Manufacturing needs, so the two aren't automatically the same
thing yet.

User's proposed split for Manufacturing (2026-08-18): **Do This Now + metrics** as the landing
dashboard; **Missing Blueprints + Shopping List** combined into one section (with an alert badge
when blueprints are missing, since that's the thing that actually blocks a build); **Build
Pipeline** broken out as its own view. Reactions' equivalent grouping is not yet proposed — do that
as part of the same design pass, not as an afterthought once Manufacturing's shape is already
locked in.

**First step:** a single IA proposal covering both pages before any code — which pieces become
tabs/sub-nav vs. collapsible sections, and how that fits the existing `TAB_SUBPAGES`/nav model in
`static/app.js` (CLAUDE.md: adding an SPA page means `index.html`, `TAB_SLUGS`, `SPA_PAGES` in
`app/main.py`, and the nav button all agree, checked by `test_routing.py`). Write the pattern down
(section boundaries, when something is a fold vs. a separate tab, badge/alert convention) before
touching either page's markup, and apply it to Manufacturing and Reactions in the same pass so
neither is left as the odd one out.

## Nothing else open beyond the above

That is the whole backlog as of 2026-08-18: §39 shipped (archive), two fresh items still open
(§40-41), two "if it comes back" notes, one measurement to read in a few days, two
recorded-not-scheduled risks, and a bug row to close. Everything else is in
[TODO-archive.md](TODO-archive.md) — the one-line shipped list, the detail worth keeping, and the
closed-with-reasoning verdicts. **Read it before reopening anything.**

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 (*"the browser test is not something I want us to do"*). Routing is pinned by
`test_routing_client.js` (which runs the router for real) plus source-level checks; live-browser
bugs stay the user's to catch. Don't propose a headless-browser suite again.
