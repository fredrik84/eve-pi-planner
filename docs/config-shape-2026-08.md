# Is the configuration stored the hard way? — measured, 2026-08-14

§18 asked two questions: is the account's configuration stored the hard way, and how much of a page
load is recomputed for an answer that did not change. Both are answered here with numbers, because
the entry it came from partly reopened a "Won't do" and deserved evidence rather than an opinion.

## Contents

- **Half A — storage shape** — what a blob would actually buy, measured
- **Half B — precomputation** — where a page load's time really goes
- **What was changed** — the one real repeat, and its guard
- **What survives** — config export/import, which turned out to be a separate question

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
(`_forget_settings_memo`), and `test_settings_memo.py` pins it two ways — by exercising writers, and
by a **source scan asserting that every function writing `pp_industry_settings` also drops the
memo**. The scan exists because the per-writer assertions did not catch removing the invalidation
from a writer the test did not happen to call; there are nine writers and the tenth will be added by
someone who has not read this file.

## What survives

**Config export/import** — TODO §18b. Independent of storage shape, which is the whole finding here.
