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

## 21. Refill to a DEADLINE, not to "full" (2026-08-15, requested)

On Refill a Plan, say **when** the colonies should run dry and let the tool work out how much P1 to
distribute to each factory to land there. Today refilling is "fill it up", so the moment you top up
on a Tuesday you have committed to your next login being whenever that happens to run out.

**The real ask, in the user's words:** refilled on a weekday, wants the next empty to fall on
Saturday around 14:00. The only ways to get that now are to wait until Saturday and refill from
empty (having first fetched all the current P1), or to do the arithmetic by hand per factory.

This is the same principle as §23c/§2g on the Industry side — a plan should land on the player's
schedule rather than the player working around the plan's — applied to PI, where it has more bite
because PI is the side whose whole promise is *minimize interactions with planets* (CLAUDE.md).

**What makes it tractable:** the consumption side is already known. Each factory's input burn rate
per hour is derivable from the schematic and the factory count that `renderPlanDistribution`
(`static/refill.js`) already computes to fill them. Quantity for a deadline is
`rate × hours_until(deadline)`, capped by storage capacity, floored at a whole number of runs.

**The parts that need a decision, not just arithmetic:**

* **A deadline shorter than the current contents is not a refill** — it is "don't refill this one
  yet, and here is when to come back". Say that, rather than quietly suggesting 0.
* **Capacity is a hard ceiling.** If the deadline needs more P1 than a factory can hold, the honest
  answer is the soonest deadline it CAN reach, not a number that will not fit.
* **Whose clock.** EVE time (UTC) throughout, since "Saturday 14:00" is a fleet-op time — but state
  it in the UI rather than assuming the reader knows.
* Round to whole runs, and report the drift that rounding causes, in the same spirit as
  `reactions_tidy_runs` (§23b): a typeable number the player can actually enter beats an exact one
  they cannot.

**First step:** confirm the per-factory burn rate is already exposed where the refill table is
built, or what it would take to surface it, before designing any UI.

## 20. Planet database wipe — RESTORED, one follow-up left (2026-08-15, INCIDENT)

**Restored.** Production's `pp_planets` was found with 0 rows: 5,302 planets, the shared reference
data the whole PI planner runs on. Reloaded from the 08-12 nightly dump — 5,302 rows back, id
sequence realigned (it was still at 625 and would have collided on the next insert), Redis
invalidated, verified live on `/api/planets`.

**Cause, in order.** `planet_db` was a feature flag gating the Planet DB tab, sitting on the **admin
rung** in production. `d916c92` (12-08 23:39) retired it along with 17 others on the stated premise
that all eighteen had been "public since June" — that premise was not checked against the live
`pp_features` rows and was wrong for this one. Retiring it published the tab, and with it a "Clear
all" button wired to `DELETE /api/planets`, which was gated on `require_context` — any logged-in
player — and deletes the table for everybody. The nightly dumps bracket the wipe to the ~3.5 hours
between that deploy and the 13-08 03:00 backup.

**Shipped since:** the flag is back with its gate (`nav-feat-pdb`, CSS + pre-paint class);
`clear_planets` requires an admin and records to the audit log; the button carries `.pp-admin-only`;
`test_planetdb_guard.py` fails on any unscoped global delete that is not admin-gated, so the shape
cannot come back quietly. `app/audit.py` + Admin → Audit now record global deletes, account
deletions, cleanups, privilege removals and refused access.

**The follow-up: set the `planet_db` rung deliberately.** The registry default is `False` (admin),
which is where it now sits, but that is the default asserting itself rather than a decision. If the
tab should be visible to testers or the public, set the rung in Admin → Features — the destructive
control on it is separately admin-gated now, so publishing the tab no longer publishes the wipe.

**The lesson worth keeping:** a flag's rung is a fact about PRODUCTION. Read the live `pp_features`
row before retiring one; the registry default says nothing about where it actually sits.

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
no browser (§2e-residual), so nothing tests real clicking, rendering or focus.

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
