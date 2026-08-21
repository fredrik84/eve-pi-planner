# Is the configuration stored the hard way? — measured, 2026-08-14

§18 asked two questions: is the account's configuration stored the hard way, and how much of a page
load is recomputed for an answer that did not change. Both are answered here with numbers, because
the entry it came from partly reopened a "Won't do" and deserved evidence rather than an opinion.

## Contents

- **Half A — storage shape** — what a blob would actually buy, measured
- **Half B — precomputation** — where a page load's time really goes
- **What was changed** — the one real repeat, and its guard
- **What survives** — config export/import, which turned out to be a separate question
- **Export/import, as shipped** — what the file carries, what it cannot carry, and why

## Half A — storage shape: **not worth doing**

The proposition was that the default configuration should be one readable keyed blob per account
rather than a column per setting spread over a table per feature.

Measured on a live database: **62 `pp_*` tables, 10 of them settings-shaped, holding 13 rows in
total across all ten.** Reading the whole settings surface for an account costs single-digit
milliseconds.

So the July "Won't do" verdict survives, and its reasoning survives with it: the duplication is the
cheap part, validation dominates the handlers and survives any scheme, and a JSON blob trades typed
columns against this repo's additive-migration convention. Of the three things that had changed
since, two do not actually argue for a blob:

* **The ravworks worked example** proves a portable config object is useful. It does not show that
  the object has to be the storage format — it is a serialisation of one.
* **Export/import** is the real want, and a serialiser over the existing readers delivers it
  without a migration. Kept as TODO §18b.
* **"The settings surface is about to grow"** was the strongest argument and is the one to watch. It
  has not grown enough to change the answer: 13 rows is not a storage problem. **Revisit if the
  count of settings-shaped tables passes ~15, or if a single account's settings ever exceed a few
  hundred rows** — those are the numbers that would make this real.

## Half B — precomputation: **one real repeat, now fixed**

Profiled per read on a local dataset:

| Read | Cost |
| --- | --- |
| `account_setup` (the settings modal) | 9.4 ms |
| `owned_quantities` (stock pool) | 3.1 ms |
| `_build_opportunities_uncached` | 3.0 ms |
| `account_build_defaults` | 1.5 ms |
| `effective_reaction_settings`, `_load_goo_and_reached` | ~0 ms (already cached) |

The expensive paths this entry was written about had **already** been fixed — the graph cache, the
account snapshot, the sessionStorage plan cache. What was left was not a heavy recomputation but a
cheap one repeated: `account_setup` asked for `get_settings` **six times per call**, each opening
its own pooled connection, for a row that cannot change while the request is in flight.

## What was changed

`get_settings` is memoised for the life of one request (`request_memo`), taking `account_setup` from
**9.4 ms to 5.5 ms**.

The risk a cache creates is the part worth reading: a write followed by a read in the same request
would otherwise return the pre-write value, silently. Every writer therefore drops the memo
(`_forget_settings_memo`), and `tests/test_settings_memo.py` pins it two ways — by exercising writers, and
by a **source scan asserting that every function writing `pp_industry_settings` also drops the
memo**. The scan exists because the per-writer assertions did not catch removing the invalidation
from a writer the test did not happen to call; there are nine writers and the tenth will be added by
someone who has not read this file.

## What survives

**Config export/import** — TODO §18b. Independent of storage shape, which is the whole finding here.

## Export/import, as shipped (2026-08-16)

`app/config_io.py`, behind `config_export_import`. Two endpoints, `GET /api/config/export` and
`POST /api/config/import`, under Settings → **Backup & transfer** (`static/configio.js`).

**It confirmed the finding rather than testing it.** The serialiser reads through `account_setup`,
`_list_markets` and `_account_reaction_settings_override` and writes back through `apply_patch`,
`_upsert_settings` and the markets insert — no new table, no migration, no column touched. A blob
storage format would have bought this nothing it did not already have.

### What the file carries

The account's whole build configuration: the build-rules sections, the account's structures with
their rigs and families, the personal freight/tax/reaction-system override, the stock-source ticks
and named sets, and the placeholder characters' declared slots. **It identifies you** — structure
names, locations and system ids, and character names — which was a deliberate choice of full
fidelity over a scrubbed file, so the download warning is rendered from `EXPORT_DISCLOSES`, the list
the exporter itself fills.

### The two ids that are not portable

* **Build pins** are stored as `s:<pp_markets row id>`, the account's own primary key. They travel
  as the structure's `location_id` and are re-resolved against the importing account's rows. A pin
  written through verbatim would name whatever row happens to hold that id — a plan silently
  installing jobs in the wrong building, and the failure `tests/test_config_io.py` exists for.
* **Stock-source keys** name containers this account has scanned. Any key the importing account does
  not have is dropped and counted, never written as a pointer to nothing.

### What an import will not do

Validate-then-write, never halfway: every problem in the document is reported at once and nothing is
written until all of them are gone. **`_validate` therefore carries checks that look like a writer's
job** — that a settings value is a scalar and not a nested object, that a solar system resolves,
that a structure has a name and a known kind, that no location appears twice. It has to: the writes
span four stores and cannot be one transaction, so anything a later writer would reject has to be
caught before the first one commits. What survives that is bounded by every section being
idempotent — re-importing the same file finishes an interrupted run rather than doubling it.

**`replace_structures` means structures.** `pp_markets` holds two different things — the account's
structures and its followed REGION markets, which are the pricing chain. Regions are not in the
file, so a merge that considered them would find them unmentioned: a self-backup restored with the
box ticked would have emptied the pricing chain. Both the match and the delete are scoped to
`kind='structure'`. A section whose feature flag is off is reported as skipped by
name rather than 403ing the whole import (which is what `apply_patch` alone would do) or being
dropped in silence. Structures match on location and placeholders on name, so importing twice is the
same statement made twice rather than a doubled roster. Deleting is opt-in: `replace_structures`
removes what the file does not mention, and the preview says how many BEFORE the button that does it.

Real characters' skills are absent by design — those are measured from ESI, and writing them from a
file would be inventing capacity (`app/industry/slots.py`).
