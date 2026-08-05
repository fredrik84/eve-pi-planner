# Manufacturing — how the tab works, in the order you use it

The path from "a customer asks for a Phoenix" to "it is built and delivered", written as the steps
you actually walk. Everything here describes what the tab does **today**.

Developer-facing map of the same flow: [industry-workflow.md](industry-workflow.md). Why each piece
works the way it does: [industry-planning.md](industry-planning.md) (up to the moment a build
starts) and [industry-running.md](industry-running.md) (after it starts).

Not every control below is on your screen: most of what is past step 3 is rolled out per account,
so a tab with fewer buttons is a tab with fewer features switched on, not a different tool.

---

## Contents

- **Step 0 — Set up, once** — the three-step first-run screen and what each answer changes
- **Step 1 — Open the tab** — the two landing states, and what refreshes itself
- **Step 2 — Plan a product** — the search, the four knobs, and what the preview tells you
- **Step 3 — Add it to your build** — what rides along with the order, and why the whole queue re-plans
- **Step 4 — The build page** — the headline numbers, the order chips, and what each tag means
- **Step 5 — Gather the materials** — binding a box, pasting stock, copying what is missing
- **Step 6 — Install the jobs** — who starts what, how many runs, and where
- **Step 7 — Watch it cook** — where progress comes from, and marking a step done by hand
- **Step 8 — Quote it and share it** — margin, price, and the link the customer opens
- **Step 9 — Deliver and clear** — removing the order, and what survives it
- **The occasional controls** — everything deliberately behind the common path

---

## Step 0 — Set up, once

The first time you open Manufacturing you get a setup screen with three numbered steps. It is
shown once per **account**, not once per browser, and every step can be finished without leaving
the page.

1. **Where you build** — required, and already answered. A dropdown of generic facilities (NPC
   station, T1/T2-rigged structures) plus any structure you have described yourself. A structure's
   rigs change the materials and time of every job, so this drives every cost and duration figure
   the tab produces. The presets are real answers: nobody needs a structure configured to get a
   true plan.
2. **Characters & slots** — optional. Connecting a character brings its real job slots, skills and
   blueprints. Without one, plans run against un-researched blueprints and default skills.
3. **Build system & fees** — optional, and folded away. Job installation fees are the system's cost
   index × the job's value plus tax; leave it blank and only the 4% SCC surcharge is counted, so
   fees come out light and nothing else in the plan is affected.

**Save & continue** is never disabled. If you want your own structure's exact ME and TE, add it in
**Structures & Markets** (search it, hit the hammer, turn on *Manufacture here* with its rig tiers) —
that panel is shared with Reactions and needs a character with market access.

After setup, a **Setup & slots** button sits at the top of the tab: job slots, connected blueprints,
stock on hand, and a preview-mode slider for seeing the live views before you have live data.

If you never described a build structure of your own, a one-line nudge sits above the tab saying
costs are being figured against a generic facility. It is dismissible and does not block anything.

## Step 1 — Open the tab

Two landing states:

- **Nothing queued** — a short card, your lifetime turnover/profit/job tiles if you have ever
  finished a manufacturing job, and one button: **Plan a new build**.
- **Something queued** — the build page (step 4). If something is cooking, that is what you came
  to look at, so planning folds away into a modal.

Three caches feed the tab — running jobs, owned blueprints, and asset stock — and they refresh
themselves when you open it if they have gone stale. That happens after the page has painted, never
blocking it, and the **Refresh** button stays for "do it now". A stale cache is silently wrong
(overstated free slots, buying what is already in your hangar), which is why it is not left to you
to remember.

## Step 2 — Plan a product

**Plan a new build** opens a modal. It is a preview: nothing is committed until you add it.

The form, left to right:

- **Product to build** — search any buildable by name.
- **Quantity**.
- **For** — optional label. The customer, contract or fleet this is for; it shows on the order chip
  and on the customer's status link.
- **Materials from** — optional. The boxes this build is gathered into. What is in them counts as
  this build's stock, so the plan stops asking you to buy what you already have.

Below it, the options that shape every number:

- **Facility** — same list as step 0.
- **Build everything — ignore small savings and slow batches** — the master toggle. Turning it on
  drops both shortcuts below. Building at an outright loss is still refused.
- **Prioritize speed — buy slow bulk materials to finish sooner**.
- **Build only if it saves N% of the build** — the marginal-saving threshold. Higher = buy more,
  build less: faster and simpler, slightly pricier. Dragging it shows live what the change costs in
  ISK and saves in time.
- **Charge N% over cost** — what you quote a customer. Priced off net cost, so reusable leftovers
  stay yours.

**Preview** produces, top to bottom: cost and time tiles; any notices worth acting on; the
**borderline components** strip ("worth building instead?" — the components the engine chose to buy
by a small margin, with a one-press "build everything above this saving"); the reaction-policy strip
(whether this build makes its own reactions); the **step-by-step** list; the **pipeline** diagram;
the **shopping list** (copyable in EVE Multibuy format, per stage); reusable leftovers; and a
collapsed debug tree.

These options are saved to your account, not just this browser — anything that plans on your behalf
without a browser open (a customer's share link, the start-now checklist) uses them too.

## Step 3 — Add it to your build

**Add to build** queues the order. What rides along with it: the quantity, the label, every
"build it anyway" you set in the preview, any blueprint ME/TE you entered by hand, the margin, and
the boxes you picked. If you pasted a hangar in the plan form, that lands on this build's material
checklist.

Adding re-plans the **whole queue together** — that is the reason to queue rather than calculate.
Components shared between two orders are built in one batch, so the second capital in the queue is
cheaper than the first.

## Step 4 — The build page

This is the screen you live on. Top to bottom:

**Headline tiles**, in the order how-far-along → when-it-lands → what-it-costs → the job counters:

- **Progress** — weighted by job time, not run count, so a percentage matches the work rather than
  the number of runs. The tooltip carries both numbers.
- **First delivery** and **Whole queue** — when the first order in line is deliverable, versus when
  everything queued is finished. Shown separately only when they differ; otherwise one **Time left**.
- **Net cost** (after crediting back reusable leftovers), **Materials**, **Total spend**, **Job fees**.
- **Sell price** — each order at its own margin, blended when they differ.
- **Still to start** and **In the cooker** — runs, not jobs.

**Order chips**, one per order, in the order the scheduler will build them. Each carries its
position in line, the label, the quantity and product, its state, its own ETA, and a tag for each
exception you set on it: ⚒ for components you forced to be built (click to take it back), a margin
tag when it differs from your current default, an ME/TE tag when you set the efficiency by hand, and
a *reacts* tag when this build makes its own reactions against your account rule. Buttons on the
chip: edit (rename, quantity, margin, ME/TE), 📦 materials, ↗ share, ✕ remove.

Then **Do this now** (step 6), the pipeline, and **In progress** — the jobs already running, per
character, with time left.

## Step 5 — Gather the materials

The 📦 button on an order chip opens its material panel: what this one build needs, how far along
the gathering is, and what is still to buy.

- **Pulling from** — the boxes this build is gathered into. Anything in them counts automatically:
  rescan your assets after hauling and the checklist moves on its own. Several boxes can be bound
  (a reaction can and a manufacturing can, say), and a set you use repeatedly can be saved and
  picked in one go.
- **Paste what you've got** — for stock nothing can see (a station you have not scanned, someone
  else's hangar, a contract already unloaded). Select the pile in the EVE client, copy, paste. It
  **replaces** what you noted before — it is a snapshot — and anything this build does not need is
  ignored. You are told what matched, what was ignored and what was not recognised.
- **Copy what's missing** — the shortfall in EVE Multibuy format, which is the actual point: walking
  to the market with what is left.
- **N still short** — collapsed by default. The shopping list above is already the material table;
  this panel exists for the per-build state the shopping list cannot know.

Requirements here are for **this order alone**, not the queue's shared batch — you cannot haul 40%
of a shared batch into one customer's box — so the sum across orders can legitimately exceed what
the queue will actually build.

## Step 6 — Install the jobs

**Do this now** is written as instructions, not status. One card per character with free slots,
showing its slot pools (Industry and Reactions, separately labelled), and one line per product:

- the product name,
- exactly what to install — `8× 165 runs · 1× 166 runs` is nine installs, spelled out, because a
  range or a total hands you a division problem,
- where to install it, when the plan is routed across more than one structure,
- whether it is an industry or a reaction job,
- how long it takes, with a `?` when the length is not the obvious one — a job may be held to a
  shorter run because something downstream needs it sooner, or matched to the plan's pace.

Under it: how many more jobs are ready but waiting on a busy slot, and how many further rounds
unlock as these finish.

Nobody is named for a job they cannot install — characters missing the required skills are not
handed the work.

## Step 7 — Watch it cook

**Refresh** pulls job status from EVE and re-plans what is left. Progress is inferred from three
signals: completed jobs in your ledgers, jobs currently running, and anything you marked done by
hand.

The hand mark exists because inference is wrong in ways only you can see: a batch built on a
character that never granted the jobs scope, work done before you connected the account, a
component you bought or were given instead of building, or a job cache that has not caught up. One
click on the card marks a step done; a second click on the same card marks a partial amount. A mark
never overrides a higher measured number, and it never counts toward your lifetime profit — it is a
statement about this queue, not evidence of an ISK-bearing job.

## Step 8 — Quote it and share it

Your price is net cost plus the margin snapshotted **on the order** — so changing one customer's
quote does not move anybody else's, and the number the customer sees is the number on your sheet.

The ↗ button on an order chip mints a link (`/b/…`) the customer can open with no account and no
idea what this tool is: what is being built, which stage it is on, how far through, when it should
be done, and the quoted price. It carries no character names, no systems or structures, no other
order of yours, and no cost — not the total, not the materials, not the fees, not your margin.

Pressing ↗ again shows the same link rather than making a new one. **Revoke** kills it immediately.
Otherwise it is permanent: when the build is finished and cleared, the link keeps serving that last
state instead of breaking, because "404" is the worst possible answer to "did my ship get built?".

## Step 9 — Deliver and clear

Remove the order with ✕ on its chip. The queue re-plans without it. The customer's link keeps
working against its last state. Finished manufacturing jobs feed the lifetime turnover and net
profit tiles, which appear on the tab only once you have actually completed a job.

## The occasional controls

Deliberately behind the common path — you can use the tab for months without touching any of them:

- **Reorder** (only when more than one order is queued) — position is not cosmetic: the first order
  wins a contested slot and its finish time is the "first delivery" number.
- **Edit an order** — rename, change the quantity, the margin, or the blueprint ME/TE.
- **Always-buy list** — components you never want built, whatever the maths says.
- **Reaction policy** — whether, and which, reactions your builds run, with a per-order exception.
- **Saved container sets** — name a group of boxes and pick them in one go.
- **Corp hangars** — scanned only on request, and only if a character has the Director role.
  Everyone else pastes a hangar instead, which counts identically.
- **Preview mode** (under Setup & slots) — fabricate progress at any percentage to see how the live
  views look before you have live data. Nothing is saved.
- **Blueprint and stock refresh** (under Setup & slots) — the manual version of what the tab already
  does for you when it opens.
