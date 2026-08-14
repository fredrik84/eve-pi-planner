# Custom / hand-built colony layouts — a future feature

Not scheduled. Moved out of `TODO.md` on 2026-08-14 so the backlog stays open work only; this is an
idea with an unanswered design question, not a task waiting for a developer.

## Contents

- **What shipped** — hybrid-colony detection, which is the part that exists
- **What this would add** — tracking layouts we did not generate
- **The question that has to be answered first** — what it would *do* for the player

## What shipped

Hybrid-colony detection. The planner recognises a colony that mixes extraction and processing
rather than matching one of its own templates, and stops mis-reading it as a broken version of
something else.

## What this would add

Broader tracking of **player-designed layouts** — colonies that match no template the planner
generates. Today such a colony is detected as "not one of ours" and otherwise left alone.

## The question that has to be answered first

**Decide what the feature would actually do for the user before building anything.** Detection
alone has no action attached to it. The PI side's whole principle is minimising trips to planets
(see [manifesto.md](manifesto.md) and [pi.md](pi.md)), and a feature that only *notices* a layout
does not remove a trip, a click or a decision — so on the manifesto's own scoring it currently earns
nothing.

Plausible answers, none chosen:

* **Leave it alone, loudly.** Guarantee the planner never proposes overwriting a hand-built colony,
  and say so in the UI. Cheapest, and possibly all that is wanted.
* **Measure it.** Report what the custom layout yields against what a generated one would, so the
  player can see whether their design is worth keeping. Real value, but it needs the comparison to
  be honest about hotspot placement, which density alone cannot tell us (see the density-is-not-yield
  invariant in `CLAUDE.md`).
* **Adopt it.** Learn from the layout and offer it as a template elsewhere. The most work by far and
  the least evidence anyone wants it.

Until one of those is picked, there is nothing to estimate.
