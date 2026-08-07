# Test protocol — the 2026-08-05/06 shipping round

What to test, where, and what a failure looks like. Written 2026-08-07 against commit `3567535`
(remote, local and prod all agree).

**You need an admin or tester account.** 26 of the 44 flags are at `testers`, so an ordinary account
sees none of Part B. Flag states: 18 public, 26 testers, nothing below.

Nothing in Part B has been exercised against a live queue — all of it is verified against seeded
fixtures only. That is the reason this document exists.

---

## Contents

- **Part A** — live for everyone, no flag. Test these first: they already affect real users
- **Part B** — behind tester flags
- **Part C** — shipped with no UI, API only
- **Known gaps found while writing this** — read before filing a bug

---

## Part A — Live for everyone (no flag)

### A1. Structure time bonuses — the big number change

**Where:** Industry → *Plan a new build* → set **Facility** to one of your own structures → Preview.

**Expect:** job durations noticeably shorter than the same plan against "NPC station". The hull role
bonus is Raitaru −15%, Azbel −20%, Sotiyo −30%, Tatara −25% on reactions, all before rigs.

**Bug if:** durations are identical between your structure and an NPC station, or an **Athanor**
shows a reaction time bonus — it correctly has none, and that zero is deliberate.

### A2. Rig fit correction

**Where:** Settings → **Structures & Markets** → a structure's build card.

**Expect:** on a **Raitaru or Athanor**, "Capital Ship" is not offered in the rig-family picker.
If you had already ticked it, a warning names the claim and what it was inflating, and the claim now
earns nothing beyond the hull role bonus.

**Bug if:** a legal combination is warned about, or an Azbel/Sotiyo loses capital-ship coverage.

### A3. Rename, consent copy, entry point

**Where:** Settings nav; Industry → **Job slots**.

**Expect:** the panel reads **"Structures & Markets"**. Connect buttons say "Connect a character" and
explain that one login grants markets, blueprints, jobs, assets, skills and planets, and why it is
one set. The Job slots modal has a **"Set up in Settings"** section pointing at both
**Structures & Markets** and **Blueprints & formulas**.

**Bug if:** any "Markets & Logistics" text survives, or a connect button still says "market
character".

### A4. System typeahead

**Where:** Structures & Markets → account settings → **"Your reaction / build system"**.

**Expect:** typing 2+ characters gives a dropdown of system, security, constellation · region. Enter
takes the first. Free text still saves as before.

**Bug if:** typing does nothing, or the field stops accepting a hand-typed name.

### A5. Reaction policy default

**Where:** Industry → plan something that needs reactions (a capital component is a good case).

**Expect:** composites **bought**, hybrid polymers and biochemicals **built**. A dismissible line
explains the default changed and where to flip it back.

**Bug if:** you had previously set a policy and it changed anyway. That is the failure that matters
most here — a stored choice must be untouched.

---

## Part B — Behind tester flags

### B1. Placeholder character slots (`industry_placeholder_slots`)

**Where:** Characters tab → add a placeholder → set manufacturing / reaction **slots** (0–11). Then
Industry → **Job slots**.

**Expect:** declared slots counted in the pool; the placeholder visibly marked; it can be handed jobs
but never renders as skill-verified. Job times still report **"assumed"**.

**Bug if:** job times flip to "measured", or a placeholder outranks a real character with proven
skills in the start-now checklist.

### B2. Manual structures (`industry_manual_structures`)

**Where:** Structures & Markets → add a structure by hand: name, hull, system (typeahead).

**Expect:** saves with no ESI involvement; security derived from the system rather than asked;
appears in the Industry **Facility** dropdown; **"price from here" is not available**, with a one-line
reason.

**Bug if:** price-from can be ticked, or the structure never reaches the Facility list.

### B3. Formula evidence (`industry_formulas_from_stock`)

**Where:** Settings → **Blueprints & formulas** → *Stock on hand — materials and reaction formulas*
→ *Paste a hangar* → paste → *Add as stock* → then **tick the new source**. Then plan something using
those reactions. Reachable from both tabs: Reactions ⚙ Settings and Manufacturing → Job slots both
link to it.

**Expect:** formula names resolve with quantities; concurrent reaction jobs capped at how many
formulas you hold. The paste help and placeholder both name reaction formulas and say they cap
concurrent jobs.

**Bug if:** formula names do not resolve, or the plan still schedules more parallel jobs than you
hold formulas.

⚠ **A paste overrides all other evidence for the formulas it names.** Paste the box that actually
holds them, or you will under-count and serialise work you can really run. This is a deliberate
decision (see `formula_print_floor` in `app/industry/blueprints.py`), not a bug.

### B4. Reactions formula cap (`reactions_formula_cap`)

**Where:** Reactions → *Suggest*, and a customer order.

**Expect:** with one formula, one job at a time regardless of free slots. The order's quoted time
**lengthens** accordingly, with a line saying a formula is locked while a job runs on it.

**Bug if:** suggestions still fill every free slot, or the customer quote ignores the cap.

### B5. Build pins (inside `industry_rig_routing`)

**Where:** Structures & Markets → structure card → **"Always build here"** per category.

**Expect:** the pinned category builds there even when another site scores better; the checklist
shows `@ Site (pinned)`; job fees are charged to that structure's system. Unpinned components still
follow their consumer.

**Bug if:** a pin is silently ignored with no note, or unpinned routing changes behaviour.

### B6. Manual "running" state (inside `industry_manual_done`)

**Where:** Build page → click a step in the pipeline.

**Expect:** cycles **not started → running → done → not started**. The button reads "run", then
"done". Partial done-runs still work. A running mark flips the order chip to *building*.

**Bug if:** a hand mark walks a measured "done" backwards, or lifetime turnover/profit moves — a mark
must never touch the earnings ledgers.

---

## Part C — Shipped with no UI (API only)

### C1. Reaction job-length ceiling (`industry_job_length_policy`)

No control exists. From a logged-in browser console:

```js
await fetch('/api/industry/job-length-policy', {method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify({days:2})})
```

Then re-plan a large reaction batch.

**Expect:** work split across more slots, no job longer than about two days. Where slots or formulas
cannot supply the concurrency, the plan reports the ceiling was **not met** rather than silently
missing it.

---

## Known gaps found while writing this

Read these before filing a bug — they are known, and two of them are the reason a tester will get
stuck.

1. ~~**Nothing in the UI mentions formulas or blueprints.**~~ **Fixed 2026-08-07.** The panel moved
   to **Settings → Blueprints & formulas** (its own nav item, alongside Structures & Markets), linked
   from both the Reactions ⚙ Settings redirect and Manufacturing → Job slots. The paste form now
   names reaction formulas, its placeholder shows one next to a material, and the help text states
   that formulas found there cap how many reaction jobs can run at once. Re-test B3 by finding the
   path yourself before reading the instructions.
2. **Pasting BLUEPRINTS does nothing.** Only reaction formulas are counted from stock; manufacturing
   blueprints are structurally excluded, because an asset row states no ME, TE or runs and a plan
   that credits an unknown-ME print is worse than one that admits it cannot see it. There is no
   manual blueprint entry anywhere — that is item **T9** in
   [tester-feedback-2026-08.md](tester-feedback-2026-08.md) and it is unbuilt.
3. **Two features have no UI control at all** — the reaction job-length ceiling (C1) and per-order
   plans. Both are endpoint-only, which the audit
   ([industry-audit-2026-08.md](industry-audit-2026-08.md)) flagged as residue.
4. **`continue-on-error` on the `lint-js` CI job is unverified.** It is set and GitHub parses it, but
   no run has failed lint since, so the "lint fails, run stays green" path has never executed.
