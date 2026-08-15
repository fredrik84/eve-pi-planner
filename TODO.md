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

## 18b. Config export / import (2026-08-14, from §18's answer)

**All that survives of §18** — the rest was answered and closed; see `TODO-archive.md` and
`docs/config-shape-2026-08.md` for the measurements.

A tester supplied a real ravworks config export: one flat keyed JSON object carrying structures,
rigs, declared slots and skills, per-category allocation, job-length settings, blacklists and tax,
with a version field, shared alliance-wide. Export/import is wanted (T13), and it was the strongest
of the three arguments for reshaping storage into a blob.

**It does not need the reshape.** A serialiser over the readers and writers that already exist
produces the same portable object without touching a single table — `get_settings`,
`_policy_payload`, `_pins_payload`, `effective_reaction_settings` and the source sets are already
the whole surface. Storage shape and portability turned out to be independent questions, which is
why §18's storage half is closed and this is not.

**First step:** write down what an export must contain and what it must NOT (anything identifying —
character names, structure IDs a stranger could locate), since it is meant to be shared. Then one
`GET /api/config/export` and one `POST /api/config/import` over the existing readers, versioned,
with import validating before it writes anything.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
