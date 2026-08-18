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

## 40. Manufacturing tab-open: same cache/prefetch treatment as Reactions

Reactions' dashboard fetch (`GET /api/reactions/jobs`) was slow on every tab-open — it repaired and
re-priced the whole plan synchronously every time, even flipping back to a tab you'd just left.
Fixed 2026-08-18 (commit `be4e467`): a 20s Redis cache on the response, invalidated explicitly on
every write that can change it, plus a hover-prefetch on the nav button.

User asked for the Manufacturing/Industry tab to get the same treatment; not yet measured there.

**First step:** find Industry's equivalent tab-open fetch (`onIndustryTabOpen`, `static/industry.js`
— likely the queue/plan endpoint in `app/industry/orders.py` or `sourcing.py`) and check whether it
re-plans/re-prices synchronously on every open the way Reactions did. If so, apply the same
pattern: a short TTL Redis cache plus `cache_invalidate` calls on the write endpoints that touch the
queue, orders, or settings it depends on.

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
