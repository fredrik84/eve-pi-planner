# eve-pi-planner — Reactions

The moon-goo reactor tool: what to run, what it is worth, and how it feeds the rest.
Back to [CLAUDE.md](../CLAUDE.md).

Find a section: `grep -n '^## ' docs/reactions.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Reactions suggestion engine (`app/reactions/advisor.py`)** — what the advisor suggests and on what basis
- **A formula is one reaction at a time (`reactions_formula_cap`)** — why ten free slots can be planned as one job
- **Stages on the dashboard are `tier_order`, shown absolute** — how chain order is rendered, and why the number is never re-ranked
- **Pricing: a sell-order price is not achievable profit** — the pricing rule that governs every profit figure shown for reaction goods
- **Where the rest lives** — pointers to reaction content that belongs to another service

---

## Reactions suggestion engine (`app/reactions/advisor.py`)

Split out of `app/reactions/jobs.py`, which had grown to ~1,500 lines covering three unrelated
jobs (ESI job fetching, the persistent slot plan, and this). `advisor.py` holds the two-stage
wizard engine — the knapsack over WHAT to run, then bin-packing onto WHO runs it — plus
`/api/reactions/suggest`. It imports from `jobs.py` **one way only**; `_character_capacities`
deliberately stayed in `jobs.py` (it's about slots, and the customer-order allocation path needs
it too), which is what keeps the dependency acyclic. `__init__.py` imports jobs before advisor
for the same reason.

## A formula is one reaction at a time (`reactions_formula_cap`)

A formula is a physical item and it is **locked into the reactor while a job runs on it**, so one
Ferrofluid formula is one concurrent reaction however many reactor slots are free. This package
allocated against slots only, so it told players to install parallel jobs they cannot.

`app/reactions/jobs.py::formula_concurrency_caps` is the one place the cap comes from, and it
**reuses the Industry evidence layer** (`app.industry.blueprints.formula_print_floor` — personal
blueprint cache ∪ enabled asset stock ∪ distinct observed `blueprint_id`s, paste wins outright)
rather than reimplementing it. The import is inside the function: `app/industry/slots.py`
deliberately does not import `app.reactions`, and this keeps the module-import graph acyclic in the
other direction too. Applied by the Suggest bin-pack (`advisor.py`), the customer-order assign
(`_allocate_and_insert`) and the quoted estimate on an order (`orders.py::_order_report`).

Two rules it must not break: **a missing key means unknown, and unknown never refuses** (no
evidence, or an incomplete blueprint picture, caps nothing — the same rule
`_assigned_slot_capacity` and `_print_limits` follow); and **chain tiers are sequential**, so the
cap is per tier and one formula may serve tier 0 and then tier 1.

## Stages on the dashboard are `tier_order`, shown absolute

Every `pp_reaction_assignments` row carries `tier_order` — 0 is the deepest intermediate (react
first), the end product sits at `len(chain_tiers)` — and it has always been in the dashboard
payload and the `ORDER BY`. Until 2026-08-07 `static/reactions.js` never read it: the loadout drew
tier 0 and tier 1 side by side with nothing saying which had to finish first, which reads as "the
tool isn't sequencing" when it is (`_concurrent_load` already counts the worst tier, not the sum).

Planned slots and the "To install" checklist now group by stage via `_rxStageLabel`, dimming and
dashing anything past stage 1. The displayed number is **`tier_order + 1`, absolute** — not
re-ranked against whatever is still pending. When stage 1 is already running its plan rows are
gone from `pending`, and re-ranking would relabel the stage-2 rows "start now" while their input
is still cooking. A gap in the numbering is the honest reading: the missing stage is in the
filled squares.

## Pricing: a sell-order price is not achievable profit

Reaction goods are repriced aggressively, so the sell-order price is not what you can actually
realise. Use **instant-sell (buy orders)** as the "what you can make" signal everywhere the user
reads a profit figure; never `sell_volume` or `net_profit_order`.

## Where the rest lives

- Module map for `app/reactions/` — [code-layout.md](code-layout.md).
- Which reactions an Industry build runs (`industry_reaction_policy`), and why a reaction formula
  caps concurrency like a blueprint copy — [industry-running.md](industry-running.md) and
  [industry-planning.md](industry-planning.md).
- The `reaction_completed` alert — [pi.md](pi.md), shared alert engine.
