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

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
