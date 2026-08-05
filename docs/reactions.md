# eve-pi-planner — Reactions

The moon-goo reactor tool: what to run, what it is worth, and how it feeds the rest.
Back to [CLAUDE.md](../CLAUDE.md).

Find a section: `grep -n '^## ' docs/reactions.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Reactions suggestion engine (`app/reactions/advisor.py`)** — what the advisor suggests and on what basis
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
