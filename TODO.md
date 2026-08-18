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

## 39. Build pipeline stages a node by its distance from the root, not by what it needs

Reported live (2026-08-18): the pipeline puts Pressurized Oxidizers and Reinforced Carbon Fiber in
Stage 3 — correctly, they need Stage 2 output — but groups Rolled Tungsten Alloy, Dysporite,
Caesarium Cadmide and Promethium Mercurite into the same Stage 3, even though those four need
nothing but fuel blocks and could run as early as Stage 1.

**Likely cause, not yet fixed:** `_indComputeTiers` (`static/industry-shopping.js:14-46`) walks the
build tree from the root and sets `e.tier = Math.max(e.tier, depth)`, where `depth` is how many
hops the walk took to REACH that node from the root — not how many build layers sit BELOW it. A
simple item consumed directly near the root gets a shallow depth (⇒ a late stage number) regardless
of how few steps its own recipe needs. This is the same category of bug reactions.md already named
and fixed for the reactions side — "[a stage is a DEPTH, not a position in a
list](docs/reactions.md)" (`chain_stage_state`/`level_stage_runs` compute a chain's stage from its
OWN inputs, recursively; the pipeline instead reads it off tree position).

**First step:** replace the pre-order "depth when first reached" with a post-order pass — for each
build node, `stage = 0` if it has no build children, else `1 + max(stage(child) for child in
inputs if child.decision === 'build')`. Verify against the live report: the four fuel-block-only
reactions should land at the same stage as whatever else builds straight off fuel blocks, one stage
earlier than Pressurized Oxidizers/Reinforced Carbon Fiber.

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

## 41. Manufacturing page is one long scroll — split it like the user described

The page has grown to carry Do This Now, the metrics tiles, missing blueprints, the shopping list,
and the build pipeline all on one continuous scroll, and it now reads as too much to take in at
once.

User's proposed split (2026-08-18): **Do This Now + metrics** as the landing dashboard; **Missing
Blueprints + Shopping List** combined into one section (with an alert badge when blueprints are
missing, since that's the thing that actually blocks a build); **Build Pipeline** broken out as its
own view.

**First step:** this needs an actual IA proposal before any code — which pieces become tabs/sub-nav
vs. collapsible sections on one page, and how that fits the existing `TAB_SUBPAGES`/nav model in
`static/app.js` (CLAUDE.md: adding an SPA page means `index.html`, `TAB_SLUGS`, `SPA_PAGES` in
`app/main.py`, and the nav button all agree, checked by `test_routing.py`). Reactions doesn't have
a directly reusable layout to copy — it's deliberately kept single-page — so this is a design pass,
not a mechanical split.

## Nothing else open beyond the above

That is the whole backlog as of 2026-08-18: three fresh items just opened (§39-41), two "if it comes
back" notes, one measurement to read in a few days, two recorded-not-scheduled risks, and a bug row
to close. Everything else is in
[TODO-archive.md](TODO-archive.md) — the one-line shipped list, the detail worth keeping, and the
closed-with-reasoning verdicts. **Read it before reopening anything.**

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 (*"the browser test is not something I want us to do"*). Routing is pinned by
`test_routing_client.js` (which runs the router for real) plus source-level checks; live-browser
bugs stay the user's to catch. Don't propose a headless-browser suite again.
