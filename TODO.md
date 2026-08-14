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

## 18. Is all of this too complicated? — storage shape and precomputation (2026-08-05, LARGE)

**Priority: soonish** (user, 2026-08-14) — not urgent, but not to be left to drift either.

A step back from feature work: **have we ended up doing this the hard way?** Two halves, and they
are related only in that both are about paying repeatedly for something that could be paid for once.

**Half A — storage shape.** The account's configuration is spread across a lot of typed columns in a
lot of tables. There are ~62 `pp_*` tables, of which the settings-shaped ones alone include
`pp_industry_settings`, `pp_reaction_settings`, `pp_account_reaction_settings`, `pp_alert_settings`,
`pp_notification_prefs`, `pp_notification_settings`, `pp_market_config`, `pp_job_config`,
`pp_plan_config` and `pp_source_sets`. The proposition to test: **the default configuration should be
a simple keyed blob** — one readable, serialisable object per account — rather than a column per
setting spread over a table per feature.

**This partly reopens a Closed item, deliberately and with new evidence.** "Per-account settings
consolidation (`settings_store.py`)" was closed **Won't do** on 2026-07-30, and its reasoning still
has to be answered rather than ignored: the duplication is the cheap part (~60-80 lines of upsert),
*validation* dominates the handlers and survives any scheme, 2 of 7 tables weren't settings rows at
all, and a JSON blob trades typed columns against this repo's additive-migration convention. What has
changed since:

1. **A worked example exists.** A tester supplied a real ravworks config export — one flat keyed JSON
   object carrying structures, rigs, declared slots and skills, per-category build allocation,
   job-length settings, blacklists and tax, with a `cookie_version` for versioning. It is shared
   alliance-wide and it works. See [docs/tester-feedback-2026-08.md](docs/tester-feedback-2026-08.md).
2. **Export/import is now wanted** (T13). The July verdict never considered serialisation; a config
   that must leave the account and come back changes the blob from a tidiness question into a
   load-bearing one.
3. **The settings surface is about to grow a lot.** Manual structures, manual blueprints, declared
   slots, a job-length policy and per-category build sites are all planned. "Prod holds only 10 rows
   total" was the July calculus and it is about to stop being true.

**Half B — precomputation.** How much of what a page load costs is recomputed every time for an
answer that did not change? There is prior art in both directions and the audit must read it before
proposing anything: `docs/industry-planning.md` ("Industry performance: one plan per page load" — the
graph cache, the inline install/progress blocks, the sessionStorage plan cache) shows real wins
already taken, and the Closed entry "Frontend CPU offload, phase 3" records an investigation that
found the hotspot was *cacheable server-side*, not a JS-offload candidate. The open question is what
is left: which reads rebuild a graph, re-resolve prices, or re-plan a queue to answer something that
could have been precomputed, and where the invalidation boundary honestly sits.

**First step is measurement, not restructuring.** The one thing that would make this item go wrong is
adopting the blob because it sounds simpler. Before any schema is touched:

1. Instrument a real page load on prod, per service, and record where the time and the queries
   actually go. Use the in-process prod debugging path in CLAUDE.md rather than reasoning from the
   code.
2. Count the true settings surface — which tables are genuinely per-account configuration, which are
   ledgers, caches or shared data that must NOT move into a blob (the July verdict's "2 of 7" point,
   re-counted against today's schema).
3. Answer the July objections explicitly: where does validation live under a blob, and what replaces
   an additive `ALTER` when a field's meaning changes.
4. Only then propose a shape — and it may legitimately come back "the storage is fine, the
   precomputation isn't", or the reverse.

Worth noting as evidence for the audit rather than as separate items: the schema carries visible
accretion — `pp_baskets_old`, `pp_profiles_new`, `pp_session` alongside `pp_sessions`,
`pp_characters_context`, and two pairs of near-identically-named settings tables. Whatever the
verdict on the blob, that is worth a pass on its own.

## 17. Stock: reserve what a plan has claimed, then one pool is enough (2026-08-05, reframed 2026-08-14)

**Reframed by the user 2026-08-14, and it turns the item inside out:** *"If we track what is
assigned to slots I'm actually fine with doing account wide stock. Otherwise we should track it via
the plans. I still want us to inventory containers if they choose it as a material source."*

So the two ownership models are not really a taste question. **Per-plan boxes are doing a job that
belongs to a reservation ledger** — stopping two plans from spending the same units — and they are
doing it by partitioning the pool, which is a blunt instrument that also costs the user a decision
per build.

**What is actually true today** (verified 2026-08-14, not assumed):

* `owned_quantities` / `source_quantities_multi` read `pp_asset_stock` raw. **Nothing anywhere
  subtracts units already promised to a queued order or an installed job.** Reactions states the
  same gap outright — *"there is no reservation ledger"* (`docs/reactions.md`, `graph.py`) — so this
  is one missing mechanism, not two.
* The gap is only between PLANNING and INSTALLING. Once a job is installed the materials physically
  leave the container, so the next ESI scan is correct on its own. What can be double-promised is
  the window in between.
* **Containers picked as a material source ARE inventoried already.** The scan writes every source
  it discovers into `pp_asset_sources` and its contents into `pp_asset_stock`; `enabled` only gates
  the account-wide pool, and a per-plan read goes by key and ignores it. The user's requirement here
  is met — no work needed, and do not "fix" `enabled` into the per-plan read.

**The work, in order:**

1. **A reservation ledger.** Units committed by a queued order or an uninstalled scheduled job are
   subtracted from what the next plan may count. Keyed per (context, type), released when the order
   is cleared or when the scan shows the materials gone. This is the piece that makes the rest
   optional.
2. **Then account-wide stock is safe**, and the per-build set stops being a correctness mechanism.
   Keep it as a *tracking* convenience — the user's builders bind a can per build to track what they
   have acquired, which is a real use even when the arithmetic no longer needs it — but it stops
   being the thing standing between two builds and a double-spend.
3. **Only then** collapse the surfaces: `plan_source_keys` exists solely to translate between the
   two models per request and goes with the model it reconciles.

**Do not start at step 3.** Retiring either ownership model before the ledger exists removes the only
thing currently preventing a double-spend.

## 2f-residual. Print locking across orders — bounded, not modelled (2026-08-05, part-done 2026-08-14)

**Half done, and the honest half is written down rather than claimed.** A print is one item and is
locked while a job runs on it. Planned as one batch that was always respected; planned per order,
each order was built on its own and saw the whole holding, so two orders each planned up to
`prints` concurrent jobs off the SAME original.

**Shipped 2026-08-14:** an order's claim is carried to the next one (`prints_used` →
`params.prints_claimed` → `_less_claimed`), first come first served down the queue — the same rule
already used for stock, contracts and copy-runs. Over-booking drops from *orders × prints* to
*orders*. `test_print_locking.py`.

**What is left, and why it is not a rounding error.** `_less_claimed` floors at 1, because an order
with no print left still has to plan its jobs and emitting zero would be a plan that cannot be
executed. So **N orders can still each plan one concurrent job off a single original.** Closing that
means making the print a **time-shared resource inside `schedule()`** — claimed when a job starts,
released when it ends — rather than a per-plan cap. That is the real fix and it is a scheduler
change, not a parameter change.

Worth doing when someone actually plans several orders apart against a single original per type;
until then the bound above is the safe direction to be wrong in (too few concurrent jobs, never too
many).

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
