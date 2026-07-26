# Industry / Manufacturing Planner — Design Spec

Status: **planning** (not yet built). Everything below ships behind a single `industry`
feature flag (default `False` = admin-only), per CLAUDE.md rule 2.

This is a design document to iterate on before implementation. It consolidates the scope
decisions and architecture agreed in planning.

---

## 1. Goal

Given a target product (from a T1 module up to a capital ship) and a quantity, plan the entire
build: decide build-vs-buy for every component, produce a priced shopping list, schedule the jobs
to fill the player's parallel slots (maximizing parallelism, front-loading independent work),
queue whatever exceeds current slot capacity, and alert the player when to start the next jobs and
what to buy. Capital / T2 / T3 builds that require reaction intermediates create **real reaction
jobs in the existing Reactions service**.

Same product philosophy as PI and Reactions: **least effort, maximum automation.** The make-or-buy
decision is *shown, not asked*; configuration is added only where the math genuinely can't decide.

## 2. Scope decisions (locked)

| Decision | Choice | Notes |
|---|---|---|
| Product depth | **Everything** (T1 / T2 / T3 / capitals) | One generic recipe-graph engine, not per-tier code. T3 reverse-engineering / randomized subsystem output deferred as a late edge case. |
| Make-or-buy | **Auto-optimize per component** on cost | Build if cheaper than buy by a margin; else buy. Optional manual override per blueprint. |
| BPC pricing | **Manual cost per blueprint** for v1 | Contract-scan and invention-cost estimation are deferred providers behind the same interface. |
| Material pricing | **Local/group markets → Jita fallback** | Reuse Reactions' `resolve_market_data` verbatim — zero new pricing code. |

## 3. Existing infrastructure reused (the head start)

| Need | Already in repo | Change required |
|---|---|---|
| Recipe graph source | `scripts/build_sde.py` parses `blueprints.yaml` (currently for reactions) | Parse `activities.manufacturing` too; add 3 tables |
| Job cost / tax / SCC | `app/industry_cost.py` — EIV × (cost_index + facility_tax + 4% SCC) | Add `activity="manufacturing"` cost-index parse |
| Live job tracking | Reactions reads `/characters/{id}/industry/jobs/` | Manufacturing = same endpoint, `activity_id=1` (vs `11`); scope already granted |
| Slot model | `reaction_slots()` in `reactions/jobs.py` | Manufacturing = base 1 + Mass Production + Advanced Mass Production, ≤ 11 |
| Freight | Reactions `import/export_isk_per_m3` + collateral | Reuse verbatim |
| Pricing (local→Jita, group sheet) | `app/markets.py:resolve_market_data`, group-sheet `alt_cost` logic | Reuse verbatim |
| Reaction economics | `reactions/graph.py:_value_reaction_batch` (single source of truth) | Reuse for reaction-built inputs |
| Alert engine | `compute_colony_alerts()` + `app/notifications.py` + `app/alert_settings.py:ALERT_KINDS` | Register new kinds; emit from an industry compute step |
| Slot UX / "to install" / wizard / multi-slot job splitting | Entire `app/reactions/` package | Mirror the structure |

## 4. Architecture — `app/industry/` (mirrors `app/reactions/`)

```
app/industry/
  _router.py     shared APIRouter (same circular-import dodge as reactions/_router.py)
  graph.py       producer graph (manufacturing ∪ reaction), ME/TE math, recursive
                 make-or-buy resolver, costing, demand aggregation
  settings.py    per-group: build system, facility tax, structure/rig bonuses, freight rates
  library.py     blueprint library — owned BPO ME/TE, manual BPC costs, buy/build overrides;
                 pluggable BPC-cost providers (manual → contract-scan → invention)
  schedule.py    DAG builder + resource-constrained list scheduler (dual slot pools),
                 persistent queue, demand-aggregation batching, excess ledger
  jobs.py        live ESI manufacturing-job tracking, plan slots, "to install" checklist
  orders.py      queued build orders; commit to real slots; status; spawns reaction orders
```

Registered on the app in `main.py` after the reactions router, same as fuelblock.

## 5. Data model

**SDE tables** (built offline by `build_sde.py`, read-only at runtime):
- `blueprints` — `blueprint_type_id, product_type_id, output_qty, base_time, max_runs`
- `blueprint_materials` — `blueprint_type_id, type_id, quantity`
- `type_volume` — if not already present, for freight math

**App tables** (per-account / per-group, `pp_` prefix, `ensure_once` migrations):
- `pp_industry_settings` — build system, facility tax, structure/rig bonuses, freight rates
  (per-group default + per-account override, same shape as reaction settings)
- `pp_industry_library` — one row per blueprint: `owns_bpo`, `bpc_unit_cost`, ME, TE,
  `build_override` (auto | force-build | force-buy)
- `pp_industry_orders` — the build queue: `product_type_id, qty, priority, mode` (parallel |
  serial), `deadline`, `status`
- `pp_industry_assignments` — materialized schedule: which job (blueprint, runs) is assigned to
  which character/slot, its dependencies, and — when the job is a spawned reaction — a reference to
  the `pp_reaction_orders` row it created (the cross-service link)
- `pp_industry_completions` — finished-since-last-tick tracking for the `industry_job_completed`
  alert (mirrors `pp_reaction_completions`)

## 6. The engine

### 6.1 Producer graph (manufacturing ∪ reaction)

The resolver spans **both** graphs. For any `type_id` it asks: produced by a manufacturing
blueprint, by a reaction (`reactions/graph.py`), or neither? The two graphs compose into one
dependency DAG:
- Manufacturing-produced → an industry job (manufacturing slot pool).
- Reaction-produced and chosen to build → a **real reaction order** via `reactions/orders.py`
  (reaction slot pool). Costed by `reactions/graph.py:_value_reaction_batch`. No reimplementation.
- PI-produced → treated as buy-or-"you already make it" (PI is colony-based, not a queueable job).
- Neither (minerals, moon goo, datacores, raw PI) → terminal **buy** node.

### 6.2 ME / TE math (`graph.py`)

CCP's per-run formulas, kept exact (reference EVE Ref):
- Material qty: `max(runs, ceil(round(baseQty · runs · (1 − ME/100) · structureBonus · rigBonus, 2)))`
- Time: `baseTime · runs · (1 − TE/100) · (1 − Industry skill) · (1 − Advanced Industry) · structure/rig`

ME/TE for a blueprint come from `library.py` (a researched BPO, or a BPC's fixed values).

### 6.3 Recursive make-or-buy (`graph.py`)

Depth-first from the target, **memoized per `type_id`** (the same component recurs everywhere),
visited-set guards the graph's few cycles. For each node:
- `buy_cost` = market price × qty (via `resolve_market_data`, local→Jita).
- `build_cost` = Σ(resolved child costs) + job install fee (`industry_cost.py`) + BPC cost
  (`library.py`).
- Pick min, with a build-margin threshold so a trivial saving doesn't spawn a job. `build_override`
  forces one side when set.

Output: a pruned build tree + a flat, priced shopping list with per-line price `source` labels
and the group-sheet-vs-market `alt_cost` hint (both reused from the reactions pattern).

### 6.4 Demand aggregation, batching, excess ledger (`schedule.py`)

Before scheduling, sum component demand across **all queued orders** and resolve make-or-buy once
on the combined quantities. Consequences:
- Shared components (e.g. a part two capitals both need) build in **one combined batch**, not two
  — material rounding wastes proportionally less, one install fee, fewer BPC copies.
- Batch sizes bounded by BPC run limits (`max_runs` / a BPC's run count) and by not starving
  parallelism.
- Any genuine leftover (demand isn't a clean multiple of a batch's output) is carried forward as
  **stock in a running ledger**, available to later orders/waves — so capital 1's excess feeds
  capital 2 instead of being rebought. This is the "consider excess materials per job" requirement.

### 6.5 Scheduler (resource-constrained, dual slot pools)

List scheduler over the composed DAG (classic RCPSP, greedy is good enough):
- Priority = critical-path length to the finished product.
- A job is **ready** when its built inputs are complete; bought inputs are available at t=0.
- Two slot pools: manufacturing slots and reaction slots (separate skills), filled independently
  but living in the same DAG — a manufacturing job can't promote until the reaction job feeding it
  is done.
- Greedily assigns ready jobs to free slots across all characters. This automatically front-loads
  every leaf build in parallel and pulls later-tier work forward the instant its inputs land — the
  "fill the slots even for a later step" behavior.

Output: makespan, per-wave timeline, per-character slot occupancy across both pools.

### 6.6 Queueing (persistent, cross-order, rolling fill)

The scheduler is **persistent state**, not a one-shot snapshot:
- **Queue whole builds.** `pp_industry_orders` holds multiple queued targets. The scheduler
  interleaves across all of them — global slot fill, not per-order — so one order's independent
  leaf jobs run in the idle slots another isn't using.
- **Rolling fill.** When ready work exceeds available slots, install the optimal subset now; the
  rest stays `queued` across the 15-min ticks. As live ESI jobs complete and free slots, the
  next-ready jobs promote — and that promotion is what fires the `industry_job_ready` alert. The
  queue *is* what the alerts read.
- **Serial vs parallel is one lever, top-level only.** Shared components always aggregate
  regardless; the choice governs only whether the final assemblies run concurrently (min
  wall-clock, **default**) or one-after-another (frees slots/BPCs sooner, opt-in per order via
  `pp_industry_orders.mode`).

## 7. Pricing

- **Materials:** `resolve_market_data(context_id, type_ids)` — walks the account's followed markets
  (structure → region → Jita) and falls back to Jita, tagging each price with `source`. Zero new
  code. Group-sheet-vs-market `alt_cost` hint carried into the shopping list.
- **Product sell side:** if a target sell venue is entered, reuse the reactions instant-sell vs
  sell-order rule so profit isn't overstated (see `feedback_eve_pi_reactions_sell_order_price`).
- **BPC cost:** one interface in `library.py` — `bpc_unit_cost(blueprint_type_id)` /
  `bpc_me_te(...)` — with providers stacked by priority so the engine never changes:
  1. **Manual** (v1) — entered cost / 0 if BPO owned.
  2. **Contract scan** (deferred) — Jita/region public item-exchange contracts matched to blueprint
     type + run count, cached like the market feeds.
  3. **Invention estimate** (deferred) — datacores (priced via the same `resolve_market_data`) +
     invention job cost ÷ success chance → effective per-BPC cost, for self-invented T2/T3.

## 8. Reactions cross-service integration

When the resolver reaches a reaction-produced input it decides to build, it creates a **real order
in the Reactions service** (`reactions/orders.py` → `pp_reaction_orders`), allocated to reaction
slots, costed by `reactions/graph.py`. The industry scheduler treats that reaction job as an
upstream task in the reaction slot pool. `pp_industry_assignments` stores the link to the spawned
`pp_reaction_orders` row. The "to install" checklist shows reaction rows tagged by service and
deep-links them into the Reactions tab that owns them. No reaction logic is duplicated.

## 9. Metrics

- **Time:** makespan (wall-clock), total job-hours, longest critical path.
- **Cost breakdown:** raw materials (local→Jita) · BPC costs (manual) · job install fees
  (per node) · freight in (materials → build system) + out (product → market) · taxes
  (SCC + facility, embedded in job cost).
- **Net:** margin + ISK/hour when a target sell price is given.

## 10. Alerting

Pure reuse of the existing engine. Register kinds in `alert_settings.py:ALERT_KINDS`; emit them
from `compute_industry_alerts(context_id)` folded into the same aggregated list
`compute_colony_alerts()` / `app/notifications.py` consume — so in-page Dashboard alerts and pushes
(Pushover / ntfy / Discord) can never drift, and each kind gets per-account muting + severity +
cooldown for free. Detection matches **live ESI jobs against the persistent queue** (same diff
`reactions/jobs.py` does).

| Kind | Fires when | Action carried |
|---|---|---|
| `industry_job_ready` | Char has a free slot **and** a queued job whose inputs are built/bought | "Start *X ×N runs* on *Char* — slot free now" |
| `industry_slots_idle` | Free slots exist with pending queue work not started | "3 idle slots — install the next wave" |
| `industry_blocked` | Next job blocked on an unacquired **bought** input | "Buy *X* to unblock *Y*" (deep-links shopping list) |
| `industry_finishing_soon` | A job ends within threshold | "*Char* frees a slot in 20 min" |
| `industry_job_completed` | A job finished since last tick | "Finished *X ×N* — Y steps left" |

Content (char, blueprint, runs, slot) comes from the scheduler's next-wave output; the in-page
version deep-links the "to install" checklist. `industry_job_ready` + `industry_blocked` together
are the core "start the next jobs and what to buy" ask. Thresholds/cooldowns reuse
`pp_alert_settings`, default-muted where a kind would otherwise be chatty.

## 11. UX

New **Industry** tab (admin-gated). Wizard: pick target product + quantity → one screen with
(a) headline metrics (time, total cost, profit), (b) build tree collapsed by tier with build/buy
badges, (c) shopping list, (d) "to install now" checklist of jobs ready this wave (both slot pools,
tagged by service), (e) live per-character slot occupancy from ESI. Build-vs-buy chips flip
automatically (shown, not asked) with an optional manual override. A queue view lists queued orders
with priority and serial/parallel mode.

## 12. Feature gating

Single `industry` key in `FEATURE_REGISTRY`, default `False` = admin-preview. Rolled out from
Admin → Features when ready. No public per-user endpoints (rule 8): every endpoint
`require_context` or `require_admin`.

## 13. Phasing

- **Phase 0 — SHIPPED.** SDE manufacturing tables (`blueprints`, `blueprint_materials`, parsed in
  `scripts/build_sde.py`) + per-activity cost-index in `app/industry_cost.py`. Requires an SDE
  rebuild to populate on an already-built DB.
- **Phase 1 — SHIPPED.** Recursive make-or-buy spanning manufacturing ∪ reaction graphs
  (`app/industry/graph.py`), cost metrics, priced shopping list. `POST /api/industry/plan`.
- **Phase 2 — SHIPPED.** Demand aggregation (MRP low-level-code explosion → shared-batch costs),
  excess/stock ledger + `on_hand` netting, resource-constrained dual-pool list scheduler
  (`app/industry/schedule.py`): makespan, waves, BPC-run-cap job splitting. All in
  `test_industry.py`. `schedule.py` is pure — no endpoint, DB or market access; callers resolve
  the inputs (see `graph.prepare_plan_inputs`) and hand them in. Its own `POST
  /api/industry/plan-queue` endpoint (targets passed in the request) was superseded by Phase 3's
  `/api/industry/queue-plan` (targets = the persisted queue) and has been removed.
- **Phase 3 — IN PROGRESS.** Done: per-character manufacturing slot pools from ESI skills
  (`app/industry/slots.py`); the persistent build queue (`pp_industry_orders` + CRUD in
  `app/industry/orders.py`) + `/api/industry/queue-plan`; the **frontend Manufacturing tab**
  (search → plan, queue, slot pool); **auto-read owned blueprints** from ESI for real per-product
  ME/TE (`app/industry/blueprints.py`, opt-in `read_blueprints` scope); **build system + tax
  auto-derived** from the account's Reactions settings; **live manufacturing-job tracking**
  (`app/industry/jobs.py`, activity_id 1) → *free* slot counts + the **"to install" checklist**
  (`/api/industry/to-install`) + in-progress job list. Remaining: alerting (5 kinds into
  `compute_colony_alerts`) and **spawning real reaction orders** into the Reactions service.
- **Phase 4** — blueprint library UI (manual BPC costs, owned-BPO ME/TE) + treat items you already
  produce (PI / Reactions output) or hold in stock as available inputs (`on_hand` is already wired).
- **Phase 5 (deferred)** — invention-cost BPCs, contract scanning, T3 reverse-engineering.

**Not yet wired (defaults in place, real values are later phases):** ME/TE come from request
params (Phase 4 library supplies per-blueprint); slot counts are request params (Phase 3 derives
per-character from ESI skills); freight cost isn't in the totals yet (Phase 4).

## 14. Risks / open questions

- **Make-or-buy ↔ scheduling coupling.** Buying a component removes slot pressure and can shorten
  makespan. v1 decides make-or-buy on **cost alone**, then schedules. A later refinement lets slot
  saturation / a deadline feed back into the buy decision. Keep decoupled for v1.
- **Graph size / cycles.** The full blueprint graph is large (~thousands) with a few tier
  self-references. Memoized DFS + visited-set handles it; SDE build stays a one-time offline step.
- **T3 quirks** (reverse-engineering relics, randomized subsystem output) — deferred within the
  "everything" umbrella; the generic engine covers T1/T2/capital cleanly.
- **BPC run-limit vs parallelism tension** — batcher trades material-efficiency against
  first-completion time along the same speed↔cost axis as the serial/parallel lever.
