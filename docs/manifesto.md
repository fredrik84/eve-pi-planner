# eve-pi-planner — what each service is FOR

Three services live in this app — the PI planner, Reactions, and Industry — and each is here for a
different reason. This file states those reasons: **purpose, target state, and the honest gap
between them**, per service.

It exists to be *scored against*. The repo already carries cross-cutting rules — minimise planet
interactions, automate the math or drop the feature, the best UI is read-only, effort is the
constraint the other goals fit inside, does this add a step to the path a builder walks every time
(all in [CLAUDE.md](../CLAUDE.md)). Those are rules about *how*. This is about *what for*, which is
what "is this feature up to code" needs and did not have.

**It has teeth.** A feature that does not serve its service's stated purpose, or costs more effort
than it removes, is a candidate for removal — not a candidate for a follow-up item. Removal has
happened before and was right both times (the moves panel in `routing.py`, the "unlimited copies"
banner in the notice stack); the point of writing this down is that the next such call is made
against a stated goal rather than a feeling. The closed-with-reasoning table in
[TODO.md](../TODO.md) is the other half of that discipline.

**Who all of this is for: any EVE player, casual to serious.** Someone with two PI planets and no
corp has to get a true answer, and so does a capital builder running four accounts. That is a
constraint, not a slogan — it is why the Industry setup screen ships facility presets that cost a
build correctly rather than demanding a structure, and why the PI planner works off a shared planet
database rather than requiring you to survey space yourself. Where depth and accessibility genuinely
conflict, the resolution is that the *accessible* path must still be **correct** — never a
simplified model that quietly gives a worse answer.

---

## Contents

- **PI planner** — one target, one plan, least interaction per ISK
- **Reactions** — a slot business in its own right
- **Industry** — lowest net cost, fastest delivery, inside the effort constraint
- **Scoring a feature** — the questions, and what a failing score means

---

## PI planner — one target, one plan, least interaction per ISK

**Purpose.** Turn "I want to produce X" into a colony layout across the characters you actually
have, and then keep it running with as few trips to the planets as possible. The scarce resource
being optimised is not ISK and not planet slots — it is **player attention**. PI pays reasonably
well for capital tied up and terribly well for hours spent, so every feature is judged by whether it
removes a click, a trip, or a decision.

**Who.** Anyone with planets. This is the service most likely to be someone's first contact with
the app, and the one where a casual player is a first-class user rather than a degraded case.

**Target state — and it is an end state.** PI is the service that can be declared *done*, and it is
close. Done looks like this:

- You state a target product and an overproduction figure; you get one plan, and it is right the
  first time — no tuning pass to make it usable.
- The plan is expressed in what you must actually do in the client: which planets, which templates,
  which pads to empty and when.
- Once running, the app tells you what needs doing and when, and is quiet the rest of the time. The
  dashboard, the alert engine and the agenda are that promise; you should be able to run PI by
  responding to the app rather than inspecting it.
- Everything derivable is derived. There is no knob for a number the app can work out.

When those hold for the products people actually run, PI is finished, and further work on it is
maintenance. **Being finished is a success state, not a problem to solve** — a service that has met
its goal and stopped growing is exactly right, and new PI features should get harder to justify over
time, not easier.

**What it deliberately does not do.** It does not model point-to-point hauling (workflow is pooled
and P1 is fungible once extracted — closed 2026-07-08). It does not simulate the exact CPU/PG fit or
buffer intermediates through storage (closed 2026-08-05: `FIT_HEADROOM = 0.10` exists so the
estimate need not be exact, and no exported template has ever been rejected in-game). It is not an
interactive calculator — Setup Analysis is *advice on improving the plan*, and the moment it starts
answering "what if I moved this one pin" it has become the thing it is meant to replace.

**Honest gap.**

1. **Data coverage is a shared-effort problem, not a code problem.** The planner is only as good as
   `pp_planets`, which is filled by contributions and an admin review queue. A player whose space is
   thinly covered gets a worse plan and no clear signal that *that* is why. This is the biggest gap
   between "casual player gets a true answer" and reality.
2. **Density is not yield, and the app knows it in only one place.** `pp_planet_yield_avg` carries
   the measured signal; ranking and advice still lean on density. Any figure a user could read as
   output and that is sourced from density is a live inconsistency.
3. **Player-designed layouts have no action attached.** Hybrid-colony detection ships; broader
   tracking of hand-built colonies (TODO 3) has never had an answer to "and then what". Under this
   manifesto, that is not an unscoped feature — it is a feature that does not yet have a purpose,
   and it stays closed until it does.
4. **Skyhooks are blocked, not missing.** ESI does not expose the cargo and there is no formula to
   fall back on. Nothing to do until CCP ships an endpoint.

## Reactions — a slot business in its own right

**Purpose.** Answer the question a reactor operator actually has: *given the slots and the
characters I have, what should I be running right now, and what is it worth?* Reactions is a
**standalone business**, not a feeder subsystem. Industry consuming reaction output is one customer
among others — an important one, and the reason the reaction policy and formula-concurrency work
exists — but the tool's goal is met when the operator's slots are earning, whether or not a single
manufacturing job ever consumes the output.

**Who.** Anyone with reaction slots — which in practice means anyone with a character in a corp with
a refinery, not just the moon-mining industrialist.

**Target state — a direction, not a destination.** Prices move, the goo mix changes, CCP reprices
the game. The standing property to hold is: **the tool decides what can be decided, and is explicit
about what it is leaving to you.**

- *What the tool decides:* what is worth running (the knapsack over what, then the bin-pack over
  who), what it is genuinely worth (instant-sell, never a sell-order price), whether a character can
  physically take the job (capacity counted against the worst tier, since chain tiers are
  sequential), and whether assigning twice books it twice (it does not).
- *What it deliberately leaves to the user:* whether to run the goo at all versus sell the inputs,
  what risk they will carry on a multi-day chain, and which corp/alliance politics govern where they
  can react. None of those are computable from anything the app can see, and pretending otherwise
  would be a knob for a number the app cannot work out — the exact failure the PI side is written
  against.

The direction of travel is that the first list grows and the second stays honest. A feature that
adds a knob belongs in the second list only if a real player judgement lives behind it.

**Honest gap.**

1. **Reactions is the least-documented service by a wide margin.** `docs/reactions.md` is about 40
   lines and two of its four sections are pointers elsewhere. That is a gap in *this* discipline,
   not just in prose: there is no written statement of what the advisor will and will not decide, so
   the boundary above is stated here for the first time.
2. **The standalone-business framing is not visible in the product.** Most of the recent reaction
   work landed as *Industry* features (reaction policy, formulas as a concurrency cap, reaction
   goods priced inside a build). If Reactions is a business in its own right, the tool should be
   answerable without ever opening Manufacturing — and that claim has not been tested against the
   current UI.
3. **The pricing rule is right and load-bearing.** Instant-sell as the profit signal is correct and
   must survive every future feature; it is called out here because it is the single rule whose
   violation would make every number in the service dishonest.

## Industry — lowest net cost, fastest delivery, inside the effort constraint

**Purpose.** Two things are being optimised — **lowest net cost and fastest delivery** — and
**effort is not a third goal, it is the constraint the other two fit inside.** The builder this is
for already has a working method: a spreadsheet and a habit. A tool that produces a better plan but
costs more clicks to operate than the spreadsheet did brings nothing, however good its numbers. So:
automate everything automatable, and open a knob only where the judgement is genuinely the user's.
(This paragraph is the one part of the manifesto that predates it — the long-form version is at the
top of [CLAUDE.md](../CLAUDE.md) and is not repeated here.)

**Who.** The working builder, including one who has never built a capital. The service has to be
usable on its first opening by someone with no structure, no corp hangar and one character — which
is what the setup screen's "every step completable right here" rule buys.

**Target state — a direction, not a destination.** Industry is the service still moving, and it does
not have a definable end state: the game's economy, the ships people order and the structures people
own all keep changing. The standing properties to hold:

- **A plan is right the moment it appears.** Fine-tuning is for the person who wants it, never a
  step on the way in.
- **The path a builder walks every time stays short.** Nine steps are described in
  [industry-workflow-user.md](industry-workflow-user.md); the occasional controls sit behind the
  common ones. That ratio is the thing to defend.
- **Every shortcut states its price.** Buying instead of building is a good trade for effort and
  delivery, but every ISK of it is passed to the customer and makes the quote harder to win — so the
  marginal-saving threshold and the speed cap report what they cost (`marginal_saving`) and can be
  overruled, rather than being quietly taken.
- **What the customer sees and what the builder sees agree.** A share link, the start-now checklist
  and the build page must plan with the same options. Every past violation of this produced the same
  class of bug.

**Honest gap.**

1. **The service has never been public.** All fifteen Industry flags sit at `testers` on prod;
   none is public, including `industry` itself. Against that, the PI side is 14 of 17 public — the
   profile of a service that met its goal. So the audience this manifesto names, "any EVE player,
   casual to serious", has never used Industry: every casual-user property it was built to have has
   been verified only against the builders who asked for the features. The feature set keeps growing
   at a rung the audience cannot reach, and that — not any individual feature — is the gap.
2. **Marking materials in is modelled; marking output out is not.** Containers are bound as an input
   record and nothing says where a job's output lands (TODO 2f-residual #1). It is the stated point
   of the exercise and still the hole in it.
3. **One 3.6k-line frontend file against 22 backend modules.** The backend split; `static/industry.js`
   did not. It is the highest-risk file in the repo and the only guard over it is a `no-undef` lint
   (TODO 2e-residual).
4. **Two endpoints exist with no UI on purpose and two with no UI by accident**
   (`queue-plan/compare` and `per-order-plans` deliberately; `skill-coverage` and `to-install`
   read as residue). Under this manifesto, residue is removable, not backlog.
5. **Four settings live at both order and account level** with four different reconciliation rules.
   Each is individually justified; collectively they are hard to state in one sentence, which is the
   symptom worth watching.

---

## Scoring a feature

Hold anything — shipped or proposed — against these, in order:

1. **Which service's purpose does it serve?** If the answer needs a paragraph, that is the finding.
2. **Does it remove more effort than it adds?** For Industry, specifically: does it add a step to
   the path a builder walks *every time*? If it does, it has to remove more than it adds; if it is
   for the occasional case, it belongs behind the common one.
3. **Does it decide something, or ask something?** A knob is only justified when the judgement is
   genuinely the user's and the app cannot compute it. Anything else is a computed answer waiting to
   be surfaced read-only.
4. **Does it give a true answer to the casual user, not just the serious one?** A simplified path
   that gives a worse answer fails; a simplified path that gives the same answer with fewer inputs
   is the goal.
5. **Is its cost visible?** Any shortcut that trades ISK for time, or accuracy for effort, must
   report what it cost rather than taking it quietly.

**A failing score means the feature comes out**, or gets a written verdict in
[TODO.md](../TODO.md)'s Closed table saying why it stays. Those are the two acceptable outcomes; a
third one — leaving it in place and unexamined — is what this file exists to prevent.
