# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-16.

---

## Nothing open

The backlog is empty as of 2026-08-16. §18b (config export/import) and §19 (URL routing, including
the deep links that carry an id) both closed that day — §19's last piece, deep-linking a colony, was
closed as **won't build** rather than shipped, and the reasoning is in the archive.

**Standing residual, deliberately not an item:** there is still no browser test (§2e-residual).
`test_routing_client.js` runs the router for real, but nothing exercises clicking, rendering or
focus, and the record openers are stubbed there — so the real dialog appearing and a saved plan
actually restoring are pinned only by source-level checks. Every routing bug in this repo so far has
been found by the user in a live browser, and that is still the shape to expect.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
