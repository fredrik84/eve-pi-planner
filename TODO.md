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

## Nothing else open

That is the whole backlog as of 2026-08-18: two "if it comes back" notes, one measurement to read
in a few days, two recorded-not-scheduled risks, and a bug row to close. **No open item is a
build.** Everything else is in
[TODO-archive.md](TODO-archive.md) — the one-line shipped list, the detail worth keeping, and the
closed-with-reasoning verdicts. **Read it before reopening anything.**

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 (*"the browser test is not something I want us to do"*). Routing is pinned by
`test_routing_client.js` (which runs the router for real) plus source-level checks; live-browser
bugs stay the user's to catch. Don't propose a headless-browser suite again.
