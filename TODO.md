# eve-pi-planner — TODO

Live backlog. **Open work only** — everything shipped and everything
reasoned-through-and-rejected is in [TODO-archive.md](TODO-archive.md), and should not be reopened
without new evidence.

Each open item states what it is, why it's open, and the first concrete step, so it can be picked
up cold. Numbers are stable ids, not an order — CLAUDE.md refers to them.

**Don't read this file whole** — `grep -n '^## ' TODO.md` for the item you want, then read that
range.

Reviewed 2026-08-16.

---

## 34. Industry is too big to read — split it and simplify it (2026-08-16)

**What.** Industry has outgrown the shape it can be reasoned about in. Measured 2026-08-16:

| | Lines | Note |
|---|---|---|
| `static/industry.js` | **4,705** | one file. The manifesto's "3.6k-line frontend file" is stale — it has grown ~30% since |
| `app/industry/` | **11,954** over 22 modules | `schedule.py` 2,056, `blueprints.py` 1,517, `graph.py` 1,383, `assets.py` 1,004, `orders.py` 1,003 |

**Why it's open.** This is honest-gap #3 in [docs/manifesto.md](docs/manifesto.md), stated there as
a risk and never given an item. The frontend is the sharp end: the backend split into 22 modules
and `industry.js` did not follow, so the one file spans onboarding, plan rendering, the notice
stack, the marginal strip, the queue, the build page, sourcing, install, progress, quoting and
shares — and the only guard over it is a `no-undef` lint. Every rule this repo has about reading
before editing (`scripts/symbols.sh`, index-then-partial-read) exists because files this size cost
a fortune to work in, and it is the file most likely to be edited next, since Industry is the
service still moving.

**This is a refactor, and the bar is that nothing changes.** No feature work rides along, no flag —
rule 2 is about NEW features and this ships none. Behaviour-preserving means the same DOM, the same
endpoints in the same order, and the same plan out of the same request.

**First concrete step** (do these in order, one commit each, verified between):

1. **Measure before cutting.** `scripts/symbols.sh static/industry.js` for the real function map,
   then group functions by the step of the workflow they serve
   ([docs/industry-workflow.md](docs/industry-workflow.md) is already that taxonomy — steps 0-9).
   Write the proposed split down before moving a line.
2. **Split the frontend along the workflow, not along size.** Likely seams: setup/onboarding, plan
   render, queue + build page, sourcing/install/progress, quote/share. Load order and the globals
   the lint scrapes are the risk — `scripts/lint_js.mjs` must stay clean, and every `onclick=`
   handler in `index.html` must still resolve.
3. **Then the backend's three biggest.** `schedule.py` is already I/O-free and is the cleanest to
   cut; `blueprints.py` and `graph.py` carry the evidence layer that Reactions imports
   (`formula_print_floor`), so any move has to keep the import graph acyclic — that constraint is
   documented in [docs/reactions.md](docs/reactions.md) and is a real one, not a preference.
4. **Simplify while in there, but log it.** Dead paths and reuse-by-conditional are fair game (rule
   4); anything that changes behaviour comes out of this item and gets its own.

**How it's verified.** `test_industry*.py` before and after each commit, plus a live plan for the
same product/quantity before and after, compared field by field — a refactor that changes a number
has failed. `python3 -c "print(open('static/industry.js','rb').read().count(b'\x00'))"` on every
touched JS file (the NUL-byte trap).

**Adjacent, deliberately not in scope.** `app/reactions/jobs.py` is **3,920 lines** — it was split
out at ~1,500 and has more than doubled — and `static/reactions.js` is 3,936. Same shape, same
argument, different service. Do Industry first; if the seams generalise, open a sibling item rather
than widening this one.

## Nothing else open

The rest of the backlog is empty as of 2026-08-16. §18b (config export/import) and §19 (URL routing,
including the deep links that carry an id) both closed that day — §19's last piece, deep-linking a
colony, was closed as **won't build** rather than shipped, and the reasoning is in the archive.

**Closed, do not reopen:** a browser/E2E test (§2e-residual) is **won't build** — user decision,
2026-08-16 ("the browser test is not something I want us to do"). Routing is pinned by `test_routing_client.js` (runs the router for real) plus
source-level checks; live-browser bugs stay the user's to catch. Don't propose a headless-browser
suite again.

## Shipped and closed

Moved to [TODO-archive.md](TODO-archive.md) — the one-line shipped list and the
closed-with-reasoning verdicts. Read it before reopening anything.
