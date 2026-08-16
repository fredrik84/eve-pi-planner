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

## 19c. A colony has no page to deep-link TO — decide whether to build one (2026-08-16)

**The rest of §19 shipped** (phase 3b, 2026-08-16 — see TODO-archive.md). An order and a saved plan
are now addressable: `/manufacturing/order/123`, `/planetary-planning/plan/12`,
`/planner/refill/plan/12`. The privacy question that gated the whole entry was answered per record
and the answer was the same both times — **the endpoint behind the id already refuses a stranger
without confirming the id exists**, so a link that reaches somebody not entitled to it lands them on
the plain page in silence, which is what a mistyped id gives them too.

**A colony was asked for and is NOT built, for a reason worth stating rather than working around.**
There is no single-colony view to land on. A colony appears as rows across Setup Analysis, the
Characters list and the plan — never as a thing you open — so a deep link to one has nothing to
open, and the honest first step is a product decision, not a route:

* **Is a colony detail view worth having at all?** It would be a new page in a tool whose whole PI
  principle is *fewer* interactions (CLAUDE.md). If the answer is no, this closes and §19 is done.
* **If yes, its id is the problem.** A colony is identified by character + planet, which is exactly
  the "character names, systems, planets, or any locatable data" rule 8 names. The two shipped
  records got a free pass because their ids are opaque integers whose endpoints already refuse a
  stranger; `/planetary-planning/colony/<character>/<planet>` discloses in the PATH, before any
  endpoint is asked. That needs a token like the plan shares have, not an id — a different
  mechanism, and a bigger piece of work than the routing it looks like.

**First step:** answer the first bullet. The mechanism only has to be designed if it is yes.

**The risk that has not gone away:** `test_routing_client.js` executes the router — including, now,
the record layer and its bounce — but there is still no browser (§2e-residual), so nothing tests
real clicking, rendering or focus. The record openers are stubbed in that harness, so what is NOT
pinned is the real dialog appearing, and `openSavedPlanFull` actually restoring a plan. The
admin-nav regression that followed Phase 2 was fixed on a hypothesis the vm harness could not
reproduce and only **confirmed by the user in a live browser on 2026-08-15** — which is the shape of
every routing bug here until there is one.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
