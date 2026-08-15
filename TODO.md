# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-05.

---

## 21b. A deadline nothing but this browser knows (2026-08-15, residual of §21)

§21 shipped behind `refill_deadline` (see docs/pi.md). The chosen deadline is stored as an instant
in `localStorage` (`refillDeadlineMs`), which honours the "never store a local time" decision but
keeps it in one browser: it doesn't follow the player to another device, and nothing server-side
can quote it. That last part is the real gap — the Dashboard's "Up next" agenda and the
`factory_refill` alert both answer "when should I log in" from a full-pad cadence, so a player who
refilled to Saturday 14:00 is still told a different time by the rest of the app.

**First step:** decide whether the deadline belongs on the plan snapshot (`pp_plan_snapshots`, one
per plan) or on the account (one "next login" for everything) — the alert side wants the second,
the refill table wants the first. Store UTC either way.

## 19. Deep links that carry an ID — the one part of URL routing still open (2026-08-15)

Phases 1–3a shipped 2026-08-15. Every page has a URL, back/forward work, and the two pages that
are really several — Admin's eleven sections and the PI Planner's two modes — are addressable
(`/admin/bugs`, `/planner/refill`). The guards that used to ask `localStorage` "which page am I on"
now ask the router, which also fixed a bug nobody had reported: two browser tabs open on the site
answered that question for each other.

**What is left is only the state identified by an ID** — `/industry/order/123`, a specific plan, a
colony. It is deliberately not built, because it is not a mapping question:

> **Privacy check before any deep link ships (rule 8):** an id in a path is visible to whoever the
> link is sent to. `/industry/order/123` in a pasted link tells the recipient an order id exists;
> anything beyond a page name needs the same scrutiny the share links already got.

**First step is a decision, not code:** for each id worth linking, what does the id itself disclose
to someone who was sent the link but is not entitled to the record — and does the endpoint behind it
already refuse them? The page-name routing shipped needs neither answer, which is why it went first.

Mechanically the rest is cheap: `TAB_SUBPAGES` in `static/app.js` already carries a page's second
segment, `routeForPath` already parses one, and `noteSubPage` is how a module tells the router where
it is. An id segment is a third of the same shape.

**The risk that has not gone away:** `test_routing_client.js` executes the router, but there is still
no browser (§2e-residual), so nothing tests real clicking, rendering or focus. The admin-nav
regression that followed Phase 2 was fixed on a hypothesis the vm harness could not reproduce
(a loader throwing inside `onAdminTabOpen` aborting the switch) and only **confirmed by the user in
a live browser on 2026-08-15** — which is the shape of every routing bug here until there is one.

## 18b. Config export / import (2026-08-14, from §18's answer)

**All that survives of §18** — the rest was answered and closed; see `TODO-archive.md` and
`docs/config-shape-2026-08.md` for the measurements.

A tester supplied a real ravworks config export: one flat keyed JSON object carrying structures,
rigs, declared slots and skills, per-category allocation, job-length settings, blacklists and tax,
with a version field, shared alliance-wide. Export/import is wanted (T13), and it was the strongest
of the three arguments for reshaping storage into a blob.

**It does not need the reshape.** A serialiser over the readers and writers that already exist
produces the same portable object without touching a single table — `get_settings`,
`_policy_payload`, `_pins_payload`, `effective_reaction_settings` and the source sets are already
the whole surface. Storage shape and portability turned out to be independent questions, which is
why §18's storage half is closed and this is not.

**First step:** write down what an export must contain and what it must NOT (anything identifying —
character names, structure IDs a stranger could locate), since it is meant to be shared. Then one
`GET /api/config/export` and one `POST /api/config/import` over the existing readers, versioned,
with import validating before it writes anything.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
