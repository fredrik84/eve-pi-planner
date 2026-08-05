# Industry audit — the workflow scored against the manifesto (2026-08-05)

Pass 2 of TODO item 12. Pass 1 described the Industry flow without judgement
([industry-workflow.md](industry-workflow.md), [industry-workflow-user.md](industry-workflow-user.md));
[manifesto.md](manifesto.md) then stated what each service is for. This scores the first against the
second, feature by feature, using the manifesto's five questions:

1. Which service's purpose does it serve?
2. Does it remove more effort than it adds — does it add a step to the every-time path?
3. Does it decide something, or ask something?
4. Does it give a true answer to the casual user, not just the serious one?
5. Is its cost visible?

Verdicts are **PASS**, **WEAK** (serves the purpose but pays for itself poorly, or is unfinished),
and **FAILS** (does not serve a stated purpose in its current form). Per the manifesto, a FAILS
means the feature comes out or gets a written Closed verdict — not a backlog item.

Feature states below are the **live** ones read from `GET /api/features` on prod, not the code
defaults. Rule 1 in CLAUDE.md exists precisely because those differ.

---

## The headline finding

**Every one of the 15 Industry flags is at `testers`. None is public. Including `industry` itself.**

For context, the same read across the whole registry (39 flags):

| Group | public | testers | admin | hidden |
|---|---|---|---|---|
| Planner / Setup Analysis / Dashboard / Characters / Notifications / Planet DB | 14 | 1 | 1 | 1 |
| Reactions | 1 | 2 | 2 | — |
| Industry | **0** | **15** | — | — |

Three things follow, and they set the frame for everything below.

1. **The manifesto's audience claim is currently untested for Industry.** "Any EVE player, casual to
   serious" is the stated audience; the number of non-tester players who have ever seen the tab is
   zero. Every casual-user property the service was built to have — the presets that cost a build
   correctly, the wizard that can always be completed, the nudge instead of a gate — has been
   verified only against builders who asked for the features in the first place.
2. **The flag count is not rollout sprawl, and I said otherwise in pass 1.** All fifteen move
   together at one rung; a tester sees the whole tab, a non-tester sees none of it. The registry
   comments say this was deliberate ("this was asked for by the builders using the tab, so
   admin-only would be a rung nobody needed"). What the flags cost is *code paths*, not a fragmented
   user experience. Correction filed below.
3. **The PI/Industry contrast is real and it matches the manifesto.** PI is 14/17 public — the
   profile of a service that has met its goal. Industry is 0/15 — a service still being built. The
   manifesto's decision to give PI an end state and Industry a direction is confirmed by the rollout
   data rather than just asserted.

**This is the one finding that should change what happens next.** The gap between the Industry
service and its stated purpose is not any individual feature below; it is that the feature set has
kept growing at a rung where the audience it is written for cannot reach it.

## The every-time path, scored

The manifesto's load-bearing Industry test is Q2: *does it add a step to the path a builder walks
every time?* Scoring the nine steps from pass 1:

| Step | Every time? | Verdict |
|---|---|---|
| 0 Set up | Once per account | **PASS.** Three steps, one required and pre-answered, none able to strand anyone. |
| 1 Open the tab | Every time | **PASS.** Zero interactions. Stale caches refresh themselves after the paint; the manual buttons remain for "do it now". This is the manifesto's "read-only is the best UI" done properly. |
| 2 Plan a product | Per order | **PASS.** Search, quantity, Preview. Four knobs, all persisted, all pre-set to a working answer — the plan is right when it appears, which is the stated property. |
| 3 Add to build | Per order | **PASS.** One button; every override set in the preview rides along rather than being silently discarded. |
| 4 The build page | Every visit | **PASS.** Read-only. The cached-plan-then-verify paint is what keeps it a tab you leave open. |
| 5 Gather materials | Per order, repeatedly | **WEAK** — see below. |
| 6 Install jobs | Per wave | **PASS.** The strongest feature in the service: it names who installs what, in run-count buckets you can type straight into the client, and never names someone who cannot install it. |
| 7 Watch it cook | Passive | **PASS.** Three signals, hand mark as the honest escape hatch, no ESI polling added. |
| 8 Quote and share | Per customer | **PASS.** One button, idempotent, and the privacy boundary is assembled field by field rather than filtered. |
| 9 Deliver and clear | Per order | **PASS.** |

The every-time path is short and it holds. **The manifesto's central Industry claim survives the
audit.** What does not survive is at the edges.

### Step 5 is the weak one

Sourcing is the only step in the path where the user does bookkeeping the app cannot do for them,
and it is expressed four ways (plan-modal "Materials from", the panel's "Pulling from", Setup →
Stock on hand's tick list, saved sets) under two ownership models that coexist behind
`industry_plan_sources`. `plan_source_keys` exists solely to reconcile them per request.

Scored: Q1 pass (it serves lowest-net-cost — stock you own is stock you do not buy). Q2 **marginal**
— binding a box removes real effort forever after, but four entry points for one concept is three
more than the concept has. Q3 pass (the box IS the record; nothing is asked that could be computed).
Q4 **fail** — a casual player with no container discipline gets the paste path, which is a snapshot
that must be redone by hand every time they buy something.

**Verdict: WEAK, and the finding is the four surfaces, not the feature.** The feature is right. Its
presentation has accreted.

## Feature-by-feature

| Feature | Verdict | Reasoning |
|---|---|---|
| `industry` (the tab) | **PASS** | The service's purpose is the manifesto's purpose. |
| `required_skills` | **PASS** | Q1: fastest delivery is meaningless if nobody can install the job. Q3: decides rather than asks. Costs an untouched account nothing when off. |
| `industry_install_skill_aware` | **PASS** | Removes a contradiction between two views of one plan — the exact class of bug the manifesto names as load-bearing. |
| `industry_share` | **PASS** | Serves the builder's actual job (answering "how's my Revelation coming along") at one click, and its privacy boundary is stricter than it needs to be. |
| `industry_manual_done` | **PASS** | Q3 done right: it exists *because* inference is wrong in ways only the user can see. Never writes to the earnings ledger. |
| `industry_sourcing` | **WEAK** | See step 5. |
| `industry_plan_sources` | **WEAK** | Correct model, and it is what makes a container-per-customer possible — but it is the second of two coexisting ownership models, and it is the reason step 5 has four surfaces. |
| `industry_corp_assets` | **PASS** | Q4 exemplary: Director-only is EVE's constraint, so the paste path works for everyone and counts identically. A 403 is reported as "no Director role", not a failure. |
| `industry_blacklist` | **WEAK** | Q1 pass, but it overlaps `industry_reaction_policy`, whose own description says it exists "instead of blacklisting every output by hand". Two mechanisms answer "never build this". |
| `industry_reaction_policy` | **PASS** | Decides a class where the blacklist asked per item, states what buying instead added to the cost (Q5), and keeps a per-order exception. |
| `industry_rig_routing` | **PASS** | Q1 directly: routing every job to one structure's rigs overstated ME/TE on any build spanning families. Fees follow the routing, so the ISK describes a build that can happen. |
| `industry_default_build_system` | **PASS** | 76% of a Jita fee was silently missing. Q5: the plan always says which system it used. |
| `industry_group_structures` | **PASS** | Removes work for everyone but the first person to describe a building. Own row always wins. |
| `industry_per_order_plans` | **FAILS, as shipped** | See below. |
| `industry_skill_advisor` | **FAILS** | See below. |

### `industry_per_order_plans` — FAILS as shipped

The flag's own description states its purpose: *"A job outputs to exactly ONE container, so a batch
shared between two builds has nowhere to deliver — this is what lets a builder run a container per
customer."*

Container-as-output is **not modelled** (TODO 2f-residual #1). So the feature currently ships the
expensive half of its own rationale — planning apart costs +2.45% net on a 2× Archon, +0.96% on a
Phoenix queue, measured — without the capability that spends it. And it has no UI: the setting and
`/queue-plan/compare` are endpoints only, with `available` sitting in the read response for a
frontend that was never written.

Scored: Q1 **fail in its current state** (the purpose it names is not implemented), Q2 fail (no
surface at all), Q5 pass — the compare endpoint exists precisely so the number comes before the
switch, which is the manifesto's rule followed exactly.

**This is not a removal candidate — it is a half-landed feature, and the half that is missing is the
point of it.** The verdict is that it should not stay in this state: either the output-container
work lands and the UI with it, or the flag comes down to `hidden` until it does. What it must not do
is sit at `testers` where a tester can pay 2.45% for the half that costs money.

### `industry_skill_advisor` — FAILS

The engine, the endpoint and the flag are all live. Nothing renders it — removed as a page-density
decision, with the reasoning in a comment at `industry.js:63`: *"training advice is not about THIS
build, and a card suggesting a character start Industry I is not what somebody checking on a running
build came for."*

That reasoning is correct and it is the manifesto's Q2 applied properly. But the conclusion drawn
from it was to remove the *surface* and keep everything behind it, which leaves a flag at `testers`,
an endpoint on the gated router, and `advisor.py` (255 lines) being maintained for nobody. Q1: it
serves no path any user walks. Q2: it costs page density nowhere because it is nowhere.

**Verdict: it comes out, or it moves.** The engine is good and the PI half (`skill_roi_for`) is
already shared, so the honest options are to delete `advisor.py` and its flag, or to give it a home
somewhere training advice belongs — which is not the build page. Leaving it as maintained dead
weight is the third outcome the manifesto exists to prevent.

### Unflagged surfaces

| Surface | Verdict | Reasoning |
|---|---|---|
| `to-install` endpoint | **FAILS** | Superseded by the inline `install` block in `queue-plan`. Dead route on a gated router. Delete. |
| `skill-coverage` endpoint | **FAILS** | No caller. Its engine (`analyze_plan_skills`) is called directly by the two plan paths, so the endpoint is residue from before that. Delete. |
| `queue-plan/packing` | **PASS** | Explicitly a diagnostic, GET-openable so reading it needs no console. Developer tooling is not user surface and is not scored on Q2. |
| BPC contract index (`bpc.py`) | **PASS** | Q5 in its purest form: a capital BPC is the single largest invisible cost, and prices are *reported, never folded into the build cost* — one seller's ask must not flip make-or-buy. |
| Lifetime tiles | **PASS** | Opt-in-by-use; invisible until you have finished a job. |
| Preview mode | **PASS** | Lets someone see the live views before they have live data; lives in the Setup modal, not the path; writes nothing. |
| `static/industry.js` | **WEAK** | 3.6k lines against 22 backend modules, guarded only by `no-undef`. Not a feature and not scoreable on Q1–Q5, but it is where the risk in this service now sits. |

## Where the workflow does not match the manifesto

Four genuine mismatches, in the order I would act on them:

1. **The service is written for an audience that cannot reach it.** 0/15 public. Everything else here
   is second-order.
2. **A feature is charging for a capability that does not exist** (`industry_per_order_plans`).
3. **Two engines are maintained with no surface** (`skill-advisor`, plus the `skill-coverage` and
   `to-install` routes). The manifesto calls this residue and makes it removable.
4. **One concept has four surfaces** (stock sources) and one question has two mechanisms (blacklist
   vs reaction policy). Both are altitude problems, not correctness problems.

And one thing the audit **cleared** that I expected to find: the every-time path. Steps 1, 2, 3, 4
and 6 are the manifesto working as written, and step 6 in particular ("start 8 jobs of 165 runs and
1 of 166") is the clearest example in the repo of automating the math and handing back the action.

## Corrections to pass 1

Both filed against my own pass-1 documents, from the live flag read:

- **`industry-workflow.md` observation #5** claimed the builder's path is "flag-dependent to an
  unusual degree" and that "an account with the base flag only gets steps 0–4 and 6". No account is
  in that state — all fifteen flags sit at one rung together. What the flags cost is code paths, not
  a fragmented experience. Corrected in the file.
- **`manifesto.md` Industry gap #1** called this "fifteen flags, all defaulting off, most in testers
  state", framing it as a deferred rollout decision. The accurate framing is that the whole service
  is at one rung and has never been public. Corrected in the file — and it is a sharper gap than the
  one I originally wrote.

## Proposed TODO lines

Not applied — these are verdicts for you to accept, amend or reject.

- **New item: roll Industry out, or state why not.** The service has 15 flags at `testers` and 0
  public. First step is deciding whether the gate is a known gap (which one?) or inertia.
- **New item: `industry_per_order_plans` should not sit at `testers` unfinished.** Either land
  container-as-output + the UI (folds in 2f-residual #1 and #3), or drop the flag to `hidden`.
- **New item: remove the dead industry surface** — `advisor.py` + its flag + endpoint,
  `/api/industry/skill-coverage`, `/api/industry/to-install`. One commit, no behaviour change.
- **Amend 2f-residual** to record that #1 (container as output) is what #3's flag is *for*, which
  the current wording separates.
- **New item (low): stock sources have four surfaces.** Altitude cleanup, not a bug; worth scoping
  only once `industry_plan_sources` settles which ownership model wins.
