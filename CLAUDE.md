# eve-pi-planner — Developer Notes

This file is the always-loaded core: the rules that must be known *before* you know you need them.
Everything else is a pointer. Keep it that way — new long-form detail goes in the `docs/` file it
belongs to, never here.

## Before you read a file, index it

The big modules here are 2,000-4,000 lines. Reading windows of one to find a function costs ~1k
tokens a look and usually takes three looks.

```
scripts/symbols.sh app/reactions/jobs.py     # ~1.2k tokens for a full map of 2,430 lines
scripts/symbols.sh app static                # a whole tree
```

**For any file over ~500 lines, run this first and read the one range you need.** Same for docs:
`grep -n '^## ' docs/<file>` then a partial read. Never load a whole service doc by reflex.

## Reference docs — read on demand, not up front

| File | Read it when |
| --- | --- |
| [docs/manifesto.md](docs/manifesto.md) | **what each service is FOR** — purpose, target state, honest gap, and the questions a feature is scored against. Read before proposing or removing one |
| [docs/workflow.md](docs/workflow.md) | **testing, deploying, releasing, debugging prod** — the mechanics behind every rule below |
| [docs/code-layout.md](docs/code-layout.md) | adding a route or module, or finding where something lives |
| [docs/pi.md](docs/pi.md) | the PI planner: planning algorithm, colony simulation, Setup Analysis advice, alerts, fuel blocks |
| [docs/reactions.md](docs/reactions.md) | the Reactions tool: the advisor, and how reaction goods must be priced |
| [docs/reactions-repair-2026-08.md](docs/reactions-repair-2026-08.md) | **the 2026-08-14 audit and its repair spec** — the pricing rule's violations, the two clocks, the collapsed cadence ceiling, and the three-agent work split. Read before touching Reactions |
| [docs/industry-planning.md](docs/industry-planning.md) | Industry up to the moment a build starts: make-or-buy, blueprints, scheduling, quoting |
| [docs/industry-running.md](docs/industry-running.md) | Industry after it starts: progress, sourcing, corp hangars, customer status links |
| [docs/industry-workflow.md](docs/industry-workflow.md) | the whole Industry flow end to end: the nine steps a builder walks, and the module/endpoint/table map behind each |
| [docs/industry-workflow-user.md](docs/industry-workflow-user.md) | the same flow written for the user — what the tab does and the order to use it in |
| [docs/industry-audit-2026-08.md](docs/industry-audit-2026-08.md) | the Industry feature set scored against the manifesto — verdicts, removal candidates, live flag states |
| [docs/config-shape-2026-08.md](docs/config-shape-2026-08.md) | **is the config stored the hard way?** — the 2026-08-14 measurements behind closing §18: 13 settings rows across 10 tables, and the one read that was repeated six times a request |
| [docs/test-protocol-2026-08.md](docs/test-protocol-2026-08.md) | what to test and where, for the 2026-08-05/06 round |
| [docs/industry-planner-spec.md](docs/industry-planner-spec.md) | the Industry planner's original spec |
| [docs/platform.md](docs/platform.md) | admin, accounts, notifications, market pricing, mobile |
| [TODO.md](TODO.md) | **source of truth for open work** — read before proposing anything. Closed verdicts are in [TODO-archive.md](TODO-archive.md) |

Each `docs/` file opens with a Contents list of its own sections and a one-line hint for each.

---

## Project Goal

Optimize a player's EVE Online Planetary Industry (PI) setup across multiple characters with the
least effort for distributing and delivering materials. The planner assigns extractor planets
(where P0 raw materials are harvested) and factory planets (where P0→P1→…→P4 processing happens)
across all characters to hit a user-specified overproduction target.

### The Industry planner's goal, and the constraint around it

**Lowest net cost, fastest delivery.** Those are the two things being optimised.

**Effort is not a third goal — it is the constraint the other two have to fit inside.** The builder
this is for already has a working method: a spreadsheet and habit. A tool that produces a better
plan but costs more clicks to operate than the spreadsheet did brings nothing to the table, however
good its numbers are. So: **automate everything that can be automated, and open a knob only where
the judgement is genuinely the user's.** A plan should be right the moment it appears, with no
fine-tuning required to get there; fine-tuning is for the person who wants it, never a step on the
way in.

Today that means two knobs — **margin** and the **low-savings threshold** — plus per-item overrules
for the handful of decisions worth arguing with. Everything else the plan works out for itself.

The tension those two knobs sit in: a builder sells against competitors, and **net cost sets the
floor under the price they can quote**. Buying a component instead of building it is a good trade
for effort and delivery time — that is what the marginal-saving threshold and the speed cap are for
— but every ISK of that convenience is passed to the customer and makes the quote harder to win.
Which is why those shortcuts are judgements the user can overrule (the "worth building instead?"
strip, `force_build_ids`) rather than settled policy, and why the plan reports what each one cost
(`marginal_saving`) instead of quietly taking it. Neither answer is right for every builder; what
is wrong is deciding for them without showing the price.

**The test for anything new here:** does it add a step to the path a builder walks every time? If
it does, it has to remove more than it adds. A control that only the occasional case needs belongs
behind the common one, not in front of it.

### The PI side's principle: minimize interactions with planets

Easier PI management for as much profit as we can muster, by cutting the player's manual trips —
picking up P1, distributing it, dropping it at factories. Judge every PI feature by whether it
*reduces* clicks/trips/decisions; if it adds steps without a clear payoff, automate the math away
or drop the feature. Setup Analysis is **advice on improving the plan, not an interactive
calculator**. The real tension: long programs mean fewer restarts but lower average yield — surface
the tradeoff, default to fewer interactions. There is no ESI write API, so "automate" means doing
all the math and handing back the single number or action to take.

---

## Development guidelines (deploy & change policy)

Standing rules for ALL changes. Follow them unless the user explicitly says otherwise.
Mechanics for 1, 2, 6 and 7 are in [docs/workflow.md](docs/workflow.md).

1. **Always test.** Write proper test cases for new features and run them against the container
   before calling anything shipped. Assert *durable invariants*, not runtime state an admin can
   change. → [test suites](docs/workflow.md#test-suites)
2. **Gate new features.** Every NEW feature ships behind a feature flag (`app/features.py`
   `FEATURE_REGISTRY`, default `False` = admin-preview), rolled out from Admin → Features. We have
   no staging environment, so this IS the staging mechanism. **Hot-patches / fixes to EXISTING
   features do NOT need a flag** — fix them in place. → [feature flags](docs/workflow.md#feature-flags-appfeaturespy)
3. **User simplicity is a core design element.** Maximize automation, minimize manual config. The
   best UI is read-only: surface a computed answer rather than a knob. Add a configurable field
   only when the math genuinely can't decide for the user.
4. **Reuse code; build generic endpoints — but no reuse-by-conditional.** Extract shared helpers
   and write general endpoints. Do NOT bolt `if mode == ...` branches onto an endpoint to make it
   serve two callers. Prefer a clean shared helper called by two thin endpoints, or a parameter
   that's genuinely orthogonal (like `FuelBlockPlanRequest.basket_id`), over a flag that forks the
   body.
5. **Static data first, live data when needed.** Prefer SDE / Fuzzwork (static) for anything that
   doesn't change per-player. Use ESI for live per-character data. **Live data trumps everything —
   UNLESS the value can be reliably derived from a known, documented formula** (then compute it).
6. **Default to `main` only — `dev` is opt-in, not routine.** Pushing to both doubles the
   notification volume for one logical change. **Don't make the dev-vs-main call unilaterally
   mid-session — ask.** End commit messages with the Co-Authored-By trailer.
   → [deploying](docs/workflow.md#deploying-main-vs-dev)
7. **Commit messages ARE the release notes — be extra vigilant.** The changelog is built verbatim
   from the commit log, with no editing pass in between. Every commit message must stand on its own
   as a one-line changelog entry: single-line `feat:`/`fix:`/`chore:` description, no body, stating
   *why* the change was made, not just *what* changed. A vague commit (`fix stuff`, `wip`) becomes
   a vague, useless line in the public changelog. → [cutting a release](docs/workflow.md#cutting-a-release)
8. **Preserve user privacy.** User data is never exposed publicly. Every endpoint that returns
   character names, systems, planets, or any locatable data **must** be gated by `require_context`
   (own data only) or `require_admin`. The only exceptions: (a) the Admin → Users page, already
   admin-gated; (b) anonymous/full shares, where the user explicitly chose to publish
   (`anonymize=False`). When adding a new endpoint, default to session-scoped. Never add a publicly
   accessible endpoint that returns per-user data, even in aggregate form that could be
   re-identified.
9. **No ads, no third-party data sharing.** No analytics scripts, tracking pixels, ad networks, or
   any third-party JS in the frontend. No user data is ever sent to a third party. ESI, Fuzzwork
   and `images.evetech.net` are the only external services this app contacts, and only for game
   data — not telemetry. The Prometheus `/metrics` endpoint is infrastructure-internal
   (token-gated, default off) and contains only aggregate counts, never per-user data.

See also [CONTRIBUTING.md](CONTRIBUTING.md) for the condensed, PR-facing version.

---

## Invariants that have already bitten us

Short forms only — the full incident writeups are in
[docs/workflow.md](docs/workflow.md#epoch-timestamps-must-be-double-precision-never-real).

**Epoch timestamps must be `double precision`, never `REAL`.** Postgres `real` is float4 (~7
digits); an epoch needs 10, so it quantises to ~64-128s. New epoch columns need an entry in
`_EPOCH_COLUMNS` (`app/db.py`) or they silently round. Asking the live schema is the only proof.

**Two different "counts" — do NOT conflate.**
- **5 = P0 resources per planet *type*** → `PLANET_P0_MAP` (planetary.py). Each type's 5-set is
  unique, so a planet's resources identify its type. Used for import type-inference, the
  `planet_types` label, and extractor-template planet selection. **Not** used for extractor/factory
  *assignment* — that reads the per-planet richness columns in `pp_planets`.
- **6 = max planets per character** → `max_planets = 1 + interplanetary_consolidation` (the
  character's skill, from the DB). Unrelated to `PLANET_P0_MAP`. Don't change one thinking it's the
  other.

**Planet density is not yield.** The richness % in `pp_planets` (0-100+) says how plentiful the
resource *hotspots* are — it is **not** an output figure. A 19%-density planet can still average
the full 48,000 P0/hour if the heads are placed well. Sizing anything off density systematically
under-builds good planets with sparse hotspots. For actual output use
`pp_planet_yield_avg.measured_pct` (`app/yield_stats.py`) or the colony's own ESI extraction rate.
Density is legitimate for *ranking* candidates and hotspot-placement advice only.

**Never add an ESI force-refresh bypass.** CCP's best-practice page states that querying before
`Expires` risks an ESI ban for circumventing caching — that would take down the whole app. Only
colony detail is cache-gated (~10 min); colony list, skills and the rest are fetched every rescan,
so a rescan is never a no-op. If staleness is reported, check `next_data_at`/`esi_expires` in
`pp_char_planets` first (expect ~10 min, not hours) before suspecting a logic bug.

**Never reintroduce psycopg2's `ThreadedConnectionPool`** — it broke under concurrency; the pool is
a `queue.Queue`. Always use `with get_connection()`, never a bare connection you close by hand.

**Scope every per-user query by `context_id`.** Redis caches `/api/characters`, the Planet DB and
admin stats — invalidate on write.

**`datetime('now')` must translate to TEXT via `TO_CHAR()`** — TEXT columns can't be compared
against `timestamptz`. See `_pg_translate()` in `app/db.py`.

**After any large JS edit, check for NUL bytes.** The Edit tool can land template-literal
separators as literal NULs:
`python3 -c "print(open('static/x.js','rb').read().count(b'\x00'))"` — expect 0.

**A reaction good's sell-order price is not achievable profit** — use instant-sell (buy orders).
See [docs/reactions.md](docs/reactions.md).

**The page on screen is `currentTab()`, never `localStorage`.** A page is a URL (`TAB_SLUGS` /
`TAB_SUBPAGES` / `TAB_RECORDS` in `static/app.js`); the stored `activeTab` answers only "what should a bare visit to
`/` restore". Storage is shared across BROWSER TABS, so a guard reading it gets whichever page the
*other* tab last opened. Adding an SPA page means four lists agreeing — the panel in `index.html`,
`TAB_SLUGS`, `SPA_PAGES` in `app/main.py`, and the nav button — which `test_routing.py` checks. A
URL may also name a ROW (`/manufacturing/order/123` — `TAB_RECORDS` + `SPA_RECORDS`): **that route
must look nothing up**, so a recipient learns nothing from it answering, and a record the caller may
not open is dropped from the address bar with a REPLACE rather than explained (rule 8, TODO §19c).
**Never register the page routes as `/{page}`**: `StaticFiles` is mounted at `/`, so a wildcard
returns the SPA document for every missing asset, which a browser reports as a syntax error inside
the file rather than the 404 it is.

---

## Key constants

| Constant | Value | Meaning |
|---|---|---|
| 48,000 | P0/cycle baseline | Extraction rate for a 100-richness planet (full bar), 1-hour extractor cycle |
| `_kk` | `48_000 × 24` | Baseline P0 per day per extractor slot |
| `_cycles_per_day` | 24 | Extractor cycles per day (1-hour cycle assumed) |
| Planet value scale | 0–100+ | Raw richness from SDE; 100 = full bar; values > 100 are boosted/exceptional planets |

## Access control

The Planet DB (`pp_planets`) is a single **global, shared** table (no `context_id`). Reads
(`GET /api/planets`, `/api/constellations`) are open; **`POST /api/planets/import` requires a
login** (`Depends(require_context)`, 401s without a valid `pp_session`) because the merge path never
deletes. **`DELETE /api/planets` requires a SITE ADMIN** — it wipes the table for every user, and
being gated on a mere session is what emptied it on 2026-08-15 (TODO §20). `test_planetdb_guard.py`
fails on any unscoped global delete that is not admin-gated. Everything else (`pp_characters`, `pp_profiles`, `pp_shares`,
`pp_plan_config`) is per-`context_id` and session-gated. Only admins write to `pp_planets`
directly; everyone else goes through the
[contribution review queue](docs/workflow.md#contribution-review-queue-pp_planet_submissions).

**Profiles & shares** both persist plan inputs. A new `PlanRequest` field that a user sets must be
wired into **all three**: the `pp_profiles` column, the share payload, and the frontend
save/restore. → [details](docs/workflow.md#profiles--shares)
