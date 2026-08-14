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

## 32. Roll Reactions out, or write down why not (2026-08-14) — DECIDE

**Held by the user (2026-08-14): they will set flags public when they judge the
service ready. Do not propose a rollout again; keep the recommendations current.**

§14's forcing question, for Reactions, and with more force: the manifesto calls this service
standalone and public-facing, and as of today it is standalone **for admins**. A normal logged-in
user gets no orders, no formula cap (so plans schedule parallel jobs off one formula), no tidy runs,
no stock subtraction, no parallel-stage slot reuse, no levelled run counts, no cadence, no ease-cost
line and no missing-formula report. **The good behaviour is the exception.** The last month of work
exists to replace exactly the tool the default user is getting.

Registry defaults as of 2026-08-14, **15 Reactions flags, none public** (live prod state is not
readable from a dev session and the DB row wins once created — these are code defaults only, so
check Admin → Features before acting on any row):

| flag | registry default | recommendation |
| --- | --- | --- |
| `reactions_formula_cap` | admin | **public, and then retire.** A formula is one reaction at a time — a game rule, not a preference. Off, plans schedule parallel jobs off a single formula, i.e. work that cannot be installed. "Would we ever turn this off again?" is no. |
| `reactions_tidy_runs` | admin | **public.** Bounded rounding (15%, `_TIDY_BUDGET`) of intermediate runs only; end products untouched. Now that the ease-cost line reports what the rounding costs and how to get it back, its price is visible rather than quiet. |
| `reactions_use_stock` | admin | **public.** Not subtracting what you already hold is the tool telling you to buy things in your own hangar. Fails soft to empty, so the risk is one-directional. |
| `reactions_parallel_stages` | admin | **public.** Its own description says it never changes what is suggested, what it costs or what it earns — only how many reactors do the work. A pure correctness fix to the slot count. |
| `reactions_level_runs` | admin | **public — but only now.** Promoting it before the 2026-08-14 cadence repair (archived) would have shipped the collapsed ceiling (a 7-day cadence answering 11.7 days) and the invisible surplus to everyone. Both are fixed and pinned; this is the one to watch on the way out. |
| `reactions_cadence` | admin | **testers first, then public.** New on 2026-08-14 and the newest code in the set. It is also the flag with the strongest claim to `public` eventually: the cadence is the tool's headline setting and gating it means gating the product. |
| `reactions_ease_cost` | admin | **testers.** New surface, and the number it reports (`surplus_isk` / `recoverable_isk`) wants a real account's eyes on it before it is stated to everyone as fact. |
| `reactions_missing_formulas` | admin | **testers.** Only reachable at all since 2026-08-14, so it has effectively never run for anyone. Its failure mode is confidently telling a user to buy a formula they hold; watch the unresolved-name reports for a round before widening. |
| `reactions_assign_guard` | testers | **public, and then retire.** It refuses to book more reaction slots than a character has. A guard against an impossible plan is not a preview feature. |
| `reactions_pack_hosts` | testers | **public.** Places an order on the fewest characters worth a login. Measured (7 characters → 3, ~1 day on a 12-day order) and squarely the effort constraint the manifesto names. |
| `reactions_stage_pipeline` | testers | **public.** Presentation only — the same jobs, run counts and stage order as the table it replaces, drawn the way Industry already draws a build. Keeping two renderings alive is the cost of leaving it. |
| `reactions_manual_done` | testers | **public.** An escape hatch for when ESI cannot tell us (5-minute stale cache, a job installed under a different product). A mark can only ever bring a stage forward and is never counted as ISK earned. |
| `reaction_orders` | admin | **reconsider — the gap that held it is closed.** This read "hold, §28b (slot reservation) is still open against it"; §28b was verified done on 2026-08-14 (later stages no longer reserve slots they cannot use, and an order is paced at quote time). What remains is a judgement, not a defect: customer orders are a second product surface rather than a refinement of the first, so decide it on its own merits rather than on a blocker that no longer exists. |
| `local_market` | admin | **hold.** Following an alliance/structure market needs a connected character and a market to follow; it is the one item here that does nothing at all for a user who has not set it up, so `public` would put an empty card in front of everybody. Reconsider once the setup card is reachable in two clicks. |
| `local_sell_hint` | admin | **hold, with `local_market`.** It is that feature's alert half and cannot be evaluated separately. |

**The decision this needs from the user is one line:** is the ladder being climbed, or is there a
known gap holding the gate? If it is the former, the twelve rows above marked public/testers move,
the three `hold` rows stay put with their reason on record, and the two `retire` candidates
(`reactions_formula_cap`, `reactions_assign_guard`) lose their flag entirely — delete the entry and its
`feature_enabled` / `_featureActive` call sites, per the registry's own rule at the top of
`app/features.py`). If it is the latter, name the gap here — that is then the next thing to build.

**Not for an implementing agent to do unilaterally.** Rolling a flag out is a product decision and
this entry is a recommendation, not a change. First step: pick one.

**Two things the decision should know, both found in verification rather than by design:**

* **Reactions-pasted formulas are Industry declarations in waiting.** The Reactions paste route is
  ungated by design (it is a route to an existing feature, not a new one) and writes to
  `pp_industry_blueprints`. It is invisible to Industry today — proved with `industry_manual_blueprints`
  off, where Industry's `manual_blueprints()` and `owned_blueprints()` both come back empty while the
  Reactions side sees the formulas. But an admin who later turns that flag on for the same user will
  find declarations on the Industry tab that the user never pasted there. Defensible — they are the
  user's own statements about their own prints — but it should be a decision, not a surprise.
* **`industry_reaction_policy` is not in the table above and that is deliberate.** It is
  reaction-*named* but sits in the Industry group and governs Industry's behaviour, so it rolls out
  with Industry (§14), not with this. Noted because a reader will go looking for it.

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

## 14. Roll Industry out, or write down why not (2026-08-05)

**Held by the user (2026-08-14): they will set flags public when they judge the
service ready. Do not propose a rollout again; keep the recommendations current.**

The audit's headline finding, and the one that reframes the rest. **All 15 Industry flags sit at
`testers` on prod; none is public — including `industry` itself.** Against that, the PI side is 14
of 17 public and Reactions is mixed. So the audience the manifesto names — any EVE player, casual to
serious — has never used this service, and every casual-user property it was built to have (the
facility presets that cost a build correctly, the wizard that can always be completed, the nudge
instead of a gate) has only ever been verified against the builders who asked for the features.

This is not a request to flip the flags. It is a request to decide which it is: a **known gap**
holding the gate — and then name it, because it is the next thing to build — or **inertia**, in
which case the ladder exists to be climbed and `industry` goes to `public` while the rest follow on
their own merits.

First step: pick one. Everything else in the Industry backlog is second-order to it.

## 15. `industry_per_order_plans` should not sit at `testers` half-landed (2026-08-05)

The flag's own description states its purpose: *a job outputs to exactly ONE container, so a batch
shared between two builds has nowhere to deliver — this is what lets a builder run a container per
customer.* Container-as-output is not modelled (2f-residual #1), and the setting has no UI
(2f-residual #3, with `available` already sitting in the read response for a frontend nobody wrote).

So what is rolled out to testers today is the half that **costs money** — planning apart is +2.45%
net on a 2× Archon, +0.96% on a Phoenix queue, measured — without the capability that spends it.
The compare endpoint is the manifesto's rule followed exactly (put the number on it before the
switch); the problem is the rung, not the design.

Two acceptable outcomes, no third: land 2f-residual #1 and #3 together and keep it at `testers`, or
drop the flag to `hidden` until they land.

## 17. Stock sources have four surfaces (2026-08-05, low)

One concept — *which boxes may this build spend* — is expressed in the plan modal's "Materials
from", the sourcing panel's "Pulling from", Settings → Blueprints & formulas → Stock on hand's
tick list, and saved source
sets, under **two** ownership models that coexist behind `industry_plan_sources` (account-wide tick
list vs. a build owning its boxes). `plan_source_keys` exists solely to reconcile them per request.

Altitude, not correctness — every surface is individually justified and the feature is right. Worth
scoping only once `industry_plan_sources` settles which ownership model wins, since that decision
removes one of the two on its own.

## 12-residual. The user-facing workflow has no home in the product (2026-08-05)

Item 12 shipped as two documents (see Shipped). The user-facing one was written as "a candidate for
the How-it-works page or onboarding" and is still only a doc — which is one of the audit's own
findings: **nothing in the product states the path.** The How-it-works page is Planetary Industry
only (one mention of "Planetary Industry", none of manufacturing or reactions), and the Industry
onboarding covers setup and stops. Steps 1-9 exist nowhere a user can read them.

**Unblocked and approved by the user 2026-08-14: build How-it-works pages for Manufacturing and
Reactions, the way Planetary Industry already has one.** It was previously held behind item 14 on
the grounds that writing a tab's workflow for an audience that cannot open the tab is work in the
wrong order; the user is handling feature access separately, so that objection no longer holds. The
source material exists — steps 1-9 are already written in `docs/industry-workflow-user.md`; what is
missing is a place in the PRODUCT to read them.

## 2f-residual. A job's output container, and prints across orders (2026-08-05)

Per-order planning shipped (see Shipped below) and these three are what it deliberately left:

1. **Container as PLAN output.** The point of the whole exercise, and still not modelled: an order
   names the box its materials come from, and the output belongs in the same one. Every scheduled
   job now carries `order_id`, which is the hook. Needs a UI answer for "no container bound" — corp
   hangars need the Director role and not everyone has one.

   **Design settled by the user (2026-08-14): the container is a property of the PLAN — a reaction
   plan or a manufacturing plan — not of a job.** That is the whole answer to the modelling question
   this item has been sitting on. One box per plan, chosen once, inherited by every job in it;
   nothing is configured per job, and the "shared batch has nowhere to deliver" problem disappears
   because the batch belongs to one plan by construction. It also means the setting is one control
   on the plan, not a column on `pp_industry_schedule` rows, and it applies to both services rather
   than being an Industry-only concept.
2. **Print locking ACROSS orders.** Per-order copy RUNS are consumed correctly, but two orders
   sharing one BPO each see it and may each schedule a concurrent job off it. Fixing it properly
   means making the print a scheduling resource rather than a per-plan cap — bigger than it looks,
   and it only bites an account planning apart with a single original per type.
3. **No UI.** The account setting and `/api/industry/queue-plan/compare` are endpoints only; nothing
   on the build page offers either. Deliberate — the measured cost of splitting was the gate on
   going further, and it is now known (+2.45% net on a 2× Archon, +0.96% on a Phoenix queue).

**#1 and #3 are one piece of work, not two.** The 2026-08-05 audit made the link explicit: #3's
flag (`industry_per_order_plans`) exists *because* of #1 — a job outputs to exactly one container,
so a shared batch has nowhere to deliver — which means the flag currently ships the half that costs
ISK without the half that justifies it. Do them together, or neither; see item 15 for the rung
decision in the meantime.

---

---

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
