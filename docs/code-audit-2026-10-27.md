# Code cleanup audit — 2026-10-27

Base: `3b7c337` (`main`). Scope: **232 source files plus 8 build/dependency manifests**, about 99,000 source lines before cleanup.

This is a repository-wide structural/reference review with targeted semantic inspection and executable validation of the changed paths. The inventory below records a disposition for every source file; syntax/reference review is not a claim that every possible runtime branch has been exercised.

## What this PR changes

- Remove unused backend facades, obsolete constants and the abandoned second blueprint-paste UI, including its unreachable handlers and discarded batch rendering.
- Reuse one parser for saved type-id lists instead of maintaining identical order/account implementations.
- Retire the nonfunctional `reactions_pack_hosts` rollout switch. Both production pacing modes already call `_hosts_for_parallel_jobs`; the removed predicate had no caller. This removes an ineffective Admin control, not the packing behavior.
- Make SDE helper re-exports explicit so static tools cannot mistake router-support imports for dead code.
- Remove unused local computations and imports, and enforce unused-local checks in the existing JavaScript lint job.
- Update stale tests that still demanded removed UI controls/footnotes or a cleared cadence with no weekly default.

No endpoints, authentication gates, database columns or stored-data migrations are removed. The generated OpenAPI document is identical: **187 paths**, including unchanged request/response schemas. No production dependencies are added.

## Evidence and validation

- Python AST and targeted Ruff checks cover all Python source; JavaScript AST/reference checks include script-to-script calls and inline HTML/template handlers. CSS and HTML parse checks and shell syntax checks cover the other source types.
- A conservative frontend reference graph reaches all **919 remaining top-level functions** from **166 entry-point references**. A textual single-use search alone was not used to delete event handlers.
- The complete synthetic Industry suite passes **1,130 checks**. All **10 JavaScript suites** pass, including the new manual-library/permission regression; that new regression fails against unchanged `main` because the obsolete renderer still reads/builds discarded batch data.
- **21 of 22 distinct Python suites** pass in the expanded run. This includes all backend CI contracts, Reactions unit tests, required skills, shared cadence, manual blueprints, completion/job summaries, reaction pricing/clock, refill rates, factory drain and settings memoization.
- `test_ui_notes.py` and the setup-page source checks pass. The updated cadence suite now passes; unchanged `main` fails its old wizard onchange assertion and its two pre-weekly-default expectations.
- The broader `test_blueprint_paste.py` integration suite fails with **`KeyError: 33359` on both unchanged main and this branch** using the existing local SDE fixture. The parser/store implementation is unchanged in this PR. This is recorded as a baseline fixture/test issue, not counted as a passing test.
- Integration runs use disposable copies of the local development SQLite data with networking disabled; the source volume is read-only. No production fixtures or user settings are changed.

## Code deliberately retained

- Router-registration imports and package re-exports: unused-looking imports can register endpoints. The schema comparison protects this boundary.
- Legacy JSON/source-key readers and additive database migrations: old stored records still need them. Removing a reader requires a migration/retention decision, not an unused-symbol guess.
- Distinct Manufacturing/Reactions economics, scheduling and settings gates: superficially similar code has different pricing directions, slot ownership and failure semantics.
- Test cache reset/status utilities (`bpc.reset_caches`, notification outcome classification) that are actually called by regression fixtures.
- The original Find Buildables analyzer/optimizer and inventory sharing: they remain wired to the planner UI and HTTP routes. Lack of traffic measurements does not establish that these features are unused.
- Fixture, benchmark, SDE-population and recovery scripts: explicit operational entry points are not expected to have Python callers.
- Dynamic CSS/HTML hooks: only the old footnote family had sufficient ownership evidence for deletion. Generated classes were not deleted from a text-only selector search.

## Follow-ups worth a separate review

1. **Reactions module boundaries.** `app/reactions/jobs.py` and `static/reactions.js` still mix job evidence, allocation, recurring lifecycle, settings and rendering. A focused split should map dependencies and preserve import/handler contracts first, as required by TODO §34-residual. This PR removes abandoned paths without moving the live allocator wholesale.
2. **Long planner functions.** The dashboard aggregator, per-order scheduler, extractor fetch and reaction leveller have high branch counts because they combine multiple domain steps. Pure input/output helpers are the next useful seam; arbitrary line-count splitting would conceal those contracts.
3. **Original analyzer retirement.** If Find Buildables is no longer wanted, retiring its UI/API together could remove the analyzer/optimizer/inventory-share stack. That is a product decision and needs usage evidence, rather than being smuggled into dead-code deletion.
4. **Lease duration.** The shared job runner has a fixed TTL and no heartbeat. Its unused renewal method did not provide protection. Choose renewal/cancellation behavior separately if jobs need to outlive that TTL; contract scans already renew their own region lease.
5. **Integration-fixture repair.** Isolate the blueprint-paste suite from assumptions in the current SDE/ownership fixture; reproduce `33359` before changing production parsing.

## File-by-file source inventory

Areas: `app` 95, `browser-tests` 10, `scripts` 13, `static` 32, `tests` 82. “Retain” means the current role is still wired/needed or the evidence does not justify deleting it; it does not certify all behavior in that file.

| File | Role and disposition |
| --- | --- |
| [app/__init__.py](../app/__init__.py) | Retain: Python package marker. |
| [app/admin.py](../app/admin.py) | Retain: Admin features: custom production baskets (global, admin-managed) and admin-user |
| [app/alert_settings.py](../app/alert_settings.py) | Retain: Per-account thresholds for the Dashboard's colony alerts (extractor expiry window, |
| [app/alerts.py](../app/alerts.py) | Retain: Shared alert engine. |
| [app/analyzer.py](../app/analyzer.py) | Retain: The original "Find Buildables" analyzer — the app's first feature, kept as its own router. |
| [app/audit.py](../app/audit.py) | Retain: A record of who destroyed what. |
| [app/bugs.py](../app/bugs.py) | Retain: Bug reporting: any logged-in pilot can submit a report; admins (see |
| [app/cache.py](../app/cache.py) | Retain: Optional Redis cache for read-heavy, write-rare data (currently: the /api/characters payload, |
| [app/completions.py](../app/completions.py) | Retain: Shared machinery for the forward-only "finished job" ledgers. |
| [app/config_io.py](../app/config_io.py) | Retain: Export and import an account's build configuration as one portable JSON document. |
| [app/db.py](../app/db.py) | Retain: Database abstraction layer. Supports SQLite (default) and PostgreSQL (when DATABASE_URL is set). |
| [app/esi.py](../app/esi.py) | Remove unused character-only auth dependency and obsolete jobs-scope alias; context auth and unified scope flows stay. |
| [app/esi_data.py](../app/esi_data.py) | Remove unused access-token timestamp calculations; refresh-token health behavior stays. |
| [app/esi_http.py](../app/esi_http.py) | Retain: One place every ESI request goes through. |
| [app/features.py](../app/features.py) | Retire reactions_pack_hosts: its predicate had no callers and placement already uses mandatory host packing. |
| [app/fuelblock_planner.py](../app/fuelblock_planner.py) | Retain: Fuel-block basket planner — the multi-product analogue of the single-product |
| [app/fuelblocks.py](../app/fuelblocks.py) | Retain: Fuel Block bill-of-materials helpers. |
| [app/geography.py](../app/geography.py) | Retain: Small geography predicates shared by PI import and recommendation code. |
| [app/groups.py](../app/groups.py) | Retain: Generic alliance-scoped group management: a group maps to one EVE alliance, can have delegated |
| [app/industry/__init__.py](../app/industry/__init__.py) | Retain: Industry / manufacturing make-or-buy planner (see docs/industry-planner-spec.md). |
| [app/industry/_router.py](../app/industry/_router.py) | Retain: The single APIRouter shared by every industry submodule. |
| [app/industry/assets.py](../app/industry/assets.py) | Remove unused recursive hangar helper and uncalled single-source query; keep the reservation-aware multi-source path. |
| [app/industry/blueprints/__init__.py](../app/industry/blueprints/__init__.py) | Retain: Blueprint evidence: what the account owns, what it declared, and what its jobs prove. |
| [app/industry/blueprints/esi.py](../app/industry/blueprints/esi.py) | Retain: Blueprints read from ESI, how a copy ranks against another, and batch identity. |
| [app/industry/blueprints/manual.py](../app/industry/blueprints/manual.py) | Retain: Blueprints and formulas DECLARED BY HAND, and the owned-blueprint map the cost resolver |
| [app/industry/blueprints/observed.py](../app/industry/blueprints/observed.py) | Retain: Formulas observed in real industry jobs — the evidence floor Reactions reads |
| [app/industry/blueprints/paste.py](../app/industry/blueprints/paste.py) | Retain: The industry window, pasted: the parser, and the batches it replaces. |
| [app/industry/blueprints/routes.py](../app/industry/blueprints/routes.py) | Retain: The hand-declared blueprint endpoints. |
| [app/industry/bpc.py](../app/industry/bpc.py) | Retain: Blueprint (BPC/BPO) prices from public contracts, with history. |
| [app/industry/build_setup.py](../app/industry/build_setup.py) | Retain: One read/write surface for everything that shapes a build. |
| [app/industry/categories.py](../app/industry/categories.py) | Retain: Reaction families, as a taxonomy in their own right. |
| [app/industry/char_cache.py](../app/industry/char_cache.py) | Retain: Per-character ESI snapshot caching. |
| [app/industry/freshness.py](../app/industry/freshness.py) | Retain: Keep the Industry tab's caches current without asking the user to remember. |
| [app/industry/graph/__init__.py](../app/industry/graph/__init__.py) | Retain: Industry (manufacturing) make-or-buy engine — Phase 1. |
| [app/industry/graph/costs.py](../app/industry/graph/costs.py) | Retain: Make-or-buy: unit costs resolved bottom-up, the reaction-policy report, and `build_plan` — |
| [app/industry/graph/options.py](../app/industry/graph/options.py) | Retain: What a caller may ask for (`BuildOptions`, `IndustryPlanRequest`) and what the ACCOUNT |
| [app/industry/graph/params.py](../app/industry/graph/params.py) | Retain: `BuildParams` — every knob a plan is resolved against — plus the two blueprint helpers that |
| [app/industry/graph/resolve.py](../app/industry/graph/resolve.py) | Retain: `prepare_plan_inputs` — the ONE resolver every plan path goes through — with the account |
| [app/industry/graph/routes.py](../app/industry/graph/routes.py) | Retain: The endpoints: product search, a single-product plan, and the marginal sweep behind the |
| [app/industry/graph/sde.py](../app/industry/graph/sde.py) | Retain: The SDE recipe graphs and their cache, the EVE material formula, and reachability. |
| [app/industry/jobs.py](../app/industry/jobs.py) | Retain: Industry planner — live ESI manufacturing-job tracking + free-slot math. |
| [app/industry/orders.py](../app/industry/orders.py) | Reuse the settings type-id JSON parser for saved order overrides; preserve the order import name and parsing behavior. |
| [app/industry/progress.py](../app/industry/progress.py) | Retain: Queue progress from real ESI industry jobs. |
| [app/industry/reservations.py](../app/industry/reservations.py) | Retain: What the account has already PROMISED, so two plans cannot spend the same units. |
| [app/industry/routing.py](../app/industry/routing.py) | Retain: Which of the account's structures each job is installed in. |
| [app/industry/schedule/__init__.py](../app/industry/schedule/__init__.py) | Retain: Industry planner — Phase 2: demand aggregation + the parallel-slot scheduler. |
| [app/industry/schedule/demand.py](../app/industry/schedule/demand.py) | Retain: Demand aggregation — the MRP explosion. |
| [app/industry/schedule/per_order.py](../app/industry/schedule/per_order.py) | Retain: Planning each order apart (behind `industry_per_order_plans`), and rolling the results back |
| [app/industry/schedule/plan.py](../app/industry/schedule/plan.py) | Retain: The orchestrator: demand -> tasks -> schedule -> metrics, plus character assignment and the |
| [app/industry/schedule/scheduler.py](../app/industry/schedule/scheduler.py) | Retain: The resource-constrained list scheduler: dependencies, critical-path priority, and the |
| [app/industry/schedule/splitting.py](../app/industry/schedule/splitting.py) | Retain: How a type's runs become JOBS: blueprint copy limits, print limits, packing and cohort |
| [app/industry/schedule/tasks.py](../app/industry/schedule/tasks.py) | Retain: `build_tasks` — the aggregated demand turned into the concrete job list. |
| [app/industry/settings.py](../app/industry/settings.py) | Own the one shared type-id parser for account lists and order overrides. |
| [app/industry/shares.py](../app/industry/shares.py) | Retain: Customer-facing build status links. |
| [app/industry/skills.py](../app/industry/skills.py) | Remove unused gap-only facade and max-level constant; exercise the actual analyzer from tests. |
| [app/industry/slots.py](../app/industry/slots.py) | Retain: Industry planner — per-character manufacturing (and reaction) slot pools. |
| [app/industry/sourcing.py](../app/industry/sourcing.py) | Remove unused single-source facade; preserve legacy binding behavior through enable_bound_sources. |
| [app/industry/status_cache.py](../app/industry/status_cache.py) | Retain: Short-lived cache for the expensive Manufacturing status-plan response. |
| [app/industry/structures.py](../app/industry/structures.py) | Retain: Industry structures — the ME/TE (and job-cost) bonuses a player's build structure gives, and |
| [app/industry_cost.py](../app/industry_cost.py) | Retain: ESI's public industry cost-index and adjusted-price feeds, used to estimate real reaction job |
| [app/internal.py](../app/internal.py) | Retain: Internal API endpoints — gated by X-Internal-Token, not public. |
| [app/jobs.py](../app/jobs.py) | Remove unused renew API; document fixed-TTL behavior accurately. Lease claim/release and run records stay. |
| [app/latency.py](../app/latency.py) | Retain: Opt-in, request-scoped diagnostics. Never records SQL, keys, URLs or user data. |
| [app/layout.py](../app/layout.py) | Remove unused P2_FACTORY_CAP archetype constant; actual CPU/PG sizing and headroom stay. |
| [app/main.py](../app/main.py) | Retain: Application composition, lifecycle and public page entry points. |
| [app/market.py](../app/market.py) | Retain: Shared cached Jita price, order depth and history reads. |
| [app/markets.py](../app/markets.py) | Retain: Followed-market configuration + local/structure market pricing overlay for the Reactions tab. |
| [app/moon_goo.py](../app/moon_goo.py) | Retain: Group-managed price list for a moon-goo-for-sale deal (see app.groups) — the input side of |
| [app/notifications.py](../app/notifications.py) | Retain: Notification support: background scheduler + channel settings + event logic. |
| [app/notifiers.py](../app/notifiers.py) | Retain: Notification channel implementations. |
| [app/optimizer.py](../app/optimizer.py) | Retain: LP-based production optimizer. |
| [app/pi.py](../app/pi.py) | Retain: PI production chain calculator. |
| [app/pi_sim.py](../app/pi_sim.py) | Retain: Forward-simulate a PI colony from the ESI checkpoint to "now". |
| [app/planetary.py](../app/planetary.py) | Retain: Planetary Planning — planet database and allocation. |
| [app/planner.py](../app/planner.py) | Remove unused single-P0 wrapper; keep batched planet lookup and yield blending. |
| [app/planner_advisor.py](../app/planner_advisor.py) | Retain: Planner advisor — the "what should I change?" endpoints. |
| [app/planner_algo.py](../app/planner_algo.py) | Retain: Planning algorithm — extractor/factory assignment across characters. |
| [app/planner_dashboard.py](../app/planner_dashboard.py) | Retain: Dashboard endpoint — the logged-in overview. |
| [app/planner_models.py](../app/planner_models.py) | Retain: Pydantic request/response models for the planner endpoints (app/planner.py). |
| [app/planner_recommendations.py](../app/planner_recommendations.py) | Retain: Planet-DB lookups for the planner: which planets grow a given P0, and |
| [app/planner_serialization.py](../app/planner_serialization.py) | Retain: Pure serialization/formatting helpers for the planner endpoints (app/planner.py): |
| [app/planner_store.py](../app/planner_store.py) | Retain: Persistence for everything the PI planner stores on a user's behalf — the CRUD half of what |
| [app/production.py](../app/production.py) | Retain: Shared contracts for production tools; domain planning stays in its owning package. |
| [app/reactions/__init__.py](../app/reactions/__init__.py) | Retain: Moon-goo reaction profitability + job-tracking tool (see app/moon_goo.py for the group-scoped |
| [app/reactions/_flags.py](../app/reactions/_flags.py) | Update the examples after deleting the obsolete predicate; keep lazy, failure-safe feature lookup. |
| [app/reactions/_router.py](../app/reactions/_router.py) | Retain: The single APIRouter shared by every reactions submodule. |
| [app/reactions/advisor.py](../app/reactions/advisor.py) | Retain: The Reactions wizard suggestion engine — "keep me busy at a good ISK/day". |
| [app/reactions/graph.py](../app/reactions/graph.py) | Retain: Reaction graph, pricing, and the profit/opportunity + shopping-list computations — the middle |
| [app/reactions/jobs.py](../app/reactions/jobs.py) | Remove unused slot-capacity and host-packing predicates; actual slot/formula constraints and allocator stay. |
| [app/reactions/library.py](../app/reactions/library.py) | Remove references to a dead helper; preserve pasted-library evidence and missing-formula rules. |
| [app/reactions/orders.py](../app/reactions/orders.py) | Retain: Fixed-unit customer orders — the top layer of the reactions package. A persistent, trackable |
| [app/reactions/settings.py](../app/reactions/settings.py) | Retain: Group + per-account reaction pricing settings (shipping/collateral rates, reaction system for |
| [app/sde.py](../app/sde.py) | Make ensure_once/add_columns re-exports explicit; preserve this import contract for every consumer. |
| [app/shares.py](../app/shares.py) | Retain: Inventory shares for the Find-Buildables analyzer (`app/analyzer.py`) — the "Copy link" |
| [app/yield_stats.py](../app/yield_stats.py) | Retain: Pooled measured-extraction-yield signal. |
| [browser-tests/playwright.config.js](../browser-tests/playwright.config.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/browse-guard.js](../browser-tests/tests/browse-guard.js) | Retain: Test-runner protection, NOT a server-side read-only permission or authentication bypass. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/helpers.js](../browser-tests/tests/helpers.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/latency.spec.js](../browser-tests/tests/latency.spec.js) | Retain: Explicit opt-in project; ordinary smoke/protocol runs never run a benchmark. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/protocol-coverage.spec.js](../browser-tests/tests/protocol-coverage.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/protocol-phase1.spec.js](../browser-tests/tests/protocol-phase1.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/protocol-phase2.spec.js](../browser-tests/tests/protocol-phase2.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/protocol-phases3-5.spec.js](../browser-tests/tests/protocol-phases3-5.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/recurring-recovery.spec.js](../browser-tests/tests/recurring-recovery.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/tests/smoke.spec.js](../browser-tests/tests/smoke.spec.js) | Retain: Browser test/configuration entry point. Configuration/source review; no authenticated browser protocol run. |
| [scripts/build_sde.py](../scripts/build_sde.py) | Retain: Download the CCP SDE and populate PI schematic data into the app's database (Postgres in |
| [scripts/lint_js.mjs](../scripts/lint_js.mjs) | Check unused function-local bindings as well as undefined names; preserve shared globals and positional callback arguments. |
| [scripts/populate_geo.py](../scripts/populate_geo.py) | Retain: Populate geography tables from Fuzzwork's small SDE CSV exports (no full SDE download) into the |
| [scripts/populate_planet_radius.py](../scripts/populate_planet_radius.py) | Retain: Populate pp_planets.diameter (km) from Fuzzwork's SDE celestials dump (mapDenormalize.csv, under |
| [scripts/run_authenticated_benchmark.py](../scripts/run_authenticated_benchmark.py) | Retain: Privileged, opt-in production browsing with an existing, owner-authorized session. |
| [scripts/run_browser_protocol.sh](../scripts/run_browser_protocol.sh) | Retain: Operational CLI entry point; shell syntax checked. |
| [scripts/run_job.py](../scripts/run_job.py) | Retain: Entrypoint for Kubernetes CronJobs. |
| [scripts/run_latency_benchmark.sh](../scripts/run_latency_benchmark.sh) | Retain: Operational CLI entry point; shell syntax checked. |
| [scripts/seed_browser_fixture.py](../scripts/seed_browser_fixture.py) | Retain: Create the isolated tester account used by the Playwright acceptance protocol. |
| [scripts/seed_hybrid_fixture.py](../scripts/seed_hybrid_fixture.py) | Retain: Seed STABLE test contexts for verifying /api/my-setup-plan's "Current setup" derivation — |
| [scripts/seed_redeploy_fixture.py](../scripts/seed_redeploy_fixture.py) | Retain: Seed STABLE test contexts for /api/redeploy-candidates — the two Setup Analysis extraction |
| [scripts/seed_test_fixture.py](../scripts/seed_test_fixture.py) | Retain: Seed a STABLE test context for developing/verifying the dashboard + Setup Analysis features |
| [scripts/symbols.sh](../scripts/symbols.sh) | Retain: Operational CLI entry point; shell syntax checked. |
| [static/404.html](../static/404.html) | Retain: HTML page/template entry point and event-handler wiring. |
| [static/admin.js](../static/admin.js) | Remove unused response binding while preserving POST/DELETE mutations, reload and checkbox failure recovery. |
| [static/analysis.js](../static/analysis.js) | Retain: Setup Analysis tab (supply-vs-plan, skill-ROI, reseat/redeploy advice) |
| [static/app.js](../static/app.js) | Retain: JavaScript regression or tooling entry point. |
| [static/build.html](../static/build.html) | Retain: HTML page/template entry point and event-handler wiring. |
| [static/configio.js](../static/configio.js) | Retain:  |
| [static/dashboard.js](../static/dashboard.js) | Retain: Dashboard tab — split out of planetary.js (2026-06-23) to keep feature files small. |
| [static/help/industry.html](../static/help/industry.html) | Retain: HTML page/template entry point and event-handler wiring. |
| [static/index.html](../static/index.html) | Retain: HTML page/template entry point and event-handler wiring. |
| [static/industry-blueprints.js](../static/industry-blueprints.js) | Remove the abandoned second bulk-paste UI, its handler chain and discarded batch rendering; preserve precise overrides and shared-library contents. |
| [static/industry-plan.js](../static/industry-plan.js) | Retain: Product search and picker, quantity, the run itself, the metric tiles, the three controls |
| [static/industry-queue.js](../static/industry-queue.js) | Retain: Adding a plan to the queue, the what-if simulator, the order list and its editing, and the |
| [static/industry-render.js](../static/industry-render.js) | Retain: _indRenderPlan (the modal) and _indRenderPlanBody (the build page) are the composition |
| [static/industry-rules.js](../static/industry-rules.js) | Retain: The single surface over the account's build configuration (Settings) and one order's |
| [static/industry-running.js](../static/industry-running.js) | Retain: The install checklist and who is eligible for it, per-order material sourcing, and the |
| [static/industry-setup.js](../static/industry-setup.js) | Remove the help toggler for the abandoned paste form; retain the active stock-source paste controls. |
| [static/industry-shopping.js](../static/industry-shopping.js) | Remove an unused item counter; preserve staged shopping and defensive unplaced-item rows. |
| [static/industry-steps.js](../static/industry-steps.js) | Retain: The build tree, ME/TE and owned-copy chips, job chips, the done state a step cycles |
| [static/industry.js](../static/industry.js) | Retain: Tab open and the load order, the planner modal, where a container IS (the source pickers |
| [static/layout.js](../static/layout.js) | Retain: Factory Layout tab — split out of planetary.js (2026-06-23). Loaded as a separate |
| [static/planetary.js](../static/planetary.js) | Remove unused JSON serialization; keep the actual Choose systems action unchanged. |
| [static/planetdb.js](../static/planetdb.js) | Retain: Planet DB tab — split out of planetary.js (2026-06-23) to keep feature files small. |
| [static/reactions.js](../static/reactions.js) | Remove three uncalled handlers and unused local calculations; keep saved schedule, one-off deadline sizing and current order reports. |
| [static/refill.js](../static/refill.js) | Retain: Refill tool (PI Planner tab) — split out of planetary.js (2026-06-23) to keep feature files small. |
| [static/style-analysis-dashboard.css](../static/style-analysis-dashboard.css) | Retain: Styles for analysis dashboard. Dynamic class ownership retained. |
| [static/style-base.css](../static/style-base.css) | Retain: Styles for base. Dynamic class ownership retained. |
| [static/style-components.css](../static/style-components.css) | Retain: Styles for components. Dynamic class ownership retained. |
| [static/style-contribute.css](../static/style-contribute.css) | Retain: Styles for contribute. Dynamic class ownership retained. |
| [static/style-layout-admin.css](../static/style-layout-admin.css) | Remove only proven-unused stat-footnote styles after removing the last renderer; keep actionable warning and order-marker styles. |
| [static/style-misc-responsive.css](../static/style-misc-responsive.css) | Retain: Styles for misc responsive. Dynamic class ownership retained. |
| [static/style-wizard.css](../static/style-wizard.css) | Retain: Styles for wizard. Dynamic class ownership retained. |
| [static/utils.js](../static/utils.js) | Retain: Shared HTTP + formatting helpers, loaded first so every feature file can rely on these |
| [tests/__init__.py](../tests/__init__.py) | Retain: Standalone application test scripts. Source/reference review; this suite was not executed in the isolated run. |
| [tests/_bootstrap.py](../tests/_bootstrap.py) | Retain: Shared import-path setup for standalone test scripts moved below the repository root. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_alert_cadence.py](../tests/test_alert_cadence.py) | Retain: Tests for TODO §37 and §37b: alerts that check before they nag, and nag less each time. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_alerts.py](../tests/test_alerts.py) | Retain: Correctness tests for the shared alert engine (app/alerts.py) and its two Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_analysis_overlap.js](../tests/test_analysis_overlap.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_audit.py](../tests/test_audit.py) | Retain: Who pressed the dangerous button, and who went looking for one. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_blueprint_paste.py](../tests/test_blueprint_paste.py) | Retain: The industry window, pasted (`parse_blueprint_paste` / `replace_blueprint_batch`). Validation: baseline fixture failure reproduced on main. |
| [tests/test_browse_guard.js](../tests/test_browse_guard.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_character_health.js](../tests/test_character_health.js) | Retain: Run the real header/health refresh code without a browser: node tests/test_character_health.js Validation: passed. |
| [tests/test_clear_all_orders.py](../tests/test_clear_all_orders.py) | Retain: Clear all gives a customer order its runs back, instead of stranding it. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_cold_load.py](../tests/test_cold_load.py) | Retain: Cold market reads overlap without changing pricing precedence or cache isolation. Validation: passed. |
| [tests/test_config_io.py](../tests/test_config_io.py) | Retain: Config export/import (TODO §18b) — the file is portable, and a bad one changes nothing. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_cost_basis.py](../tests/test_cost_basis.py) | Retain: A missing system cost index is visible, and the two engines are honest about differing. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_db_pool.py](../tests/test_db_pool.py) | Retain: Postgres connection-pool recovery tests. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_delete_account.py](../tests/test_delete_account.py) | Retain: Correctness tests for account deletion (DELETE /api/me). Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_disconnect_character.py](../tests/test_disconnect_character.py) | Retain: Correctness tests for disconnecting a character (DELETE /api/characters/{id}). Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_distribution.py](../tests/test_distribution.py) | Retain: Distribution test suite for the PI planner. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_epoch_precision.py](../tests/test_epoch_precision.py) | Retain: Epoch columns keep sub-second precision on Postgres. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_expired_reminders.py](../tests/test_expired_reminders.py) | Retain: Expired programs notify once and back off to weekly reminders, independent of rollout. Validation: passed. |
| [tests/test_factory_drain.py](../tests/test_factory_drain.py) | Retain: When does a factory colony run its imported inputs dry? (TODO §21b) Remove only unused bindings/imports; preserve exercised behavior. Validation: passed. |
| [tests/test_features.py](../tests/test_features.py) | Retain: Smoke tests for the feature-flag system and the Skill-ROI advisor endpoint. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_formula_jobs.py](../tests/test_formula_jobs.py) | Retain: Formulas OBSERVED in real industry jobs are a third evidence source for the concurrency cap. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_formula_stock.py](../tests/test_formula_stock.py) | Retain: Reaction formulas found in ENABLED stock raise the concurrency cap — and change nothing else. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_fresh_db_tables.py](../tests/test_fresh_db_tables.py) | Retain: Every table this app needs must exist after a boot against an EMPTY database. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_frontend_cleanup.js](../tests/test_frontend_cleanup.js) | New executable UI regression: overrides do not build discarded batch markup; library contents, gates and permission failure recovery remain functional. Validation: passed. |
| [tests/test_group_structures.py](../tests/test_group_structures.py) | Retain: Alliance-shared build structures: suggestions, adoption, and the scoping that keeps one Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_hybrid_setup.py](../tests/test_hybrid_setup.py) | Retain: Regression tests for /api/my-setup-plan's "Current setup" demand derivation: hybrid ("BYOS" — Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_industry.py](../tests/test_industry.py) | Exercise live multi-source helpers; remove unused fixture setup and imports; all 1,130 checks pass. Validation: passed. |
| [tests/test_industry_stage_depth.js](../tests/test_industry_stage_depth.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_industry_status_cache.py](../tests/test_industry_status_cache.py) | Retain: Manufacturing's short-lived status cache: isolation, copies and write invalidation wiring. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_job_summary.py](../tests/test_job_summary.py) | Retain: job_summary() must report a job's real last run regardless of how chatty its neighbours are. Validation: passed. |
| [tests/test_jspace_pi.py](../tests/test_jspace_pi.py) | Retain: J-space PI contract: import safely, plan normally, never invent static proximity. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_latency.py](../tests/test_latency.py) | Retain: Isolated diagnostic contracts: no real DB, Redis, server or secrets required. Validation: passed. |
| [tests/test_level_runs.py](../tests/test_level_runs.py) | Retain: One run count per product, across every character (`reactions_level_runs`). Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_manual_blueprints.py](../tests/test_manual_blueprints.py) | Retain: Blueprints and reaction formulas DECLARED BY HAND (`pp_industry_blueprints`). Validation: passed. |
| [tests/test_manual_structures.py](../tests/test_manual_structures.py) | Retain: A build structure described BY HAND — no ESI character, no structure-search scopes. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_market_cache_contract.py](../tests/test_market_cache_contract.py) | Retain: Market cache hits, including upstream failures, must make zero upstream calls. Validation: passed. |
| [tests/test_min_cc.py](../tests/test_min_cc.py) | Retain: Minimum-command-centre tests: the level a layout actually NEEDS, vs the level we assume. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_missing_formulas.py](../tests/test_missing_formulas.py) | Retain: Once you have PASTED your industry window, a formula you never declared is one you don't own. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_nav_gating.py](../tests/test_nav_gating.py) | Retain: The logged-out sidebar shows only what a stranger can actually use. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_optimizer.py](../tests/test_optimizer.py) | Retain: Correctness tests for app/optimizer.py's LP solve. Validation: passed. |
| [tests/test_order_cadence.py](../tests/test_order_cadence.py) | Retain: The order a customer is quoted is the order the dashboard draws. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_order_output_container.py](../tests/test_order_output_container.py) | Retain: A build's OUTPUT container is a property of the plan, and it says where it came from. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_page_access.py](../tests/test_page_access.py) | Retain: Per-group PAGE access control (app/groups.py `require_page`). Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_parallel_stages.py](../tests/test_parallel_stages.py) | Retain: A chain's stages reuse one reactor, and idle reactors get put to work. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_pi_lifetime.py](../tests/test_pi_lifetime.py) | Retain: Durable PI accounting: rescans dedupe and bounded trend pruning cannot shrink totals. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_pi_product_prices.py](../tests/test_pi_product_prices.py) | Retain: Optional PI valuation prices are public market data and use the shared price cache. Validation: passed. |
| [tests/test_placeholder_slots.py](../tests/test_placeholder_slots.py) | Retain: Placeholder characters contribute the Industry job slots their owner DECLARED — and nothing else. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_planet_submission_reviews.js](../tests/test_planet_submission_reviews.js) | Retain: Browser-free checks of the actual global submission review banner and its refresh hooks. Validation: passed. |
| [tests/test_planet_submission_reviews.py](../tests/test_planet_submission_reviews.py) | Retain: Submission outcomes survive offline reviews and are visible/dismissible only by their owner. Validation: passed. |
| [tests/test_planetdb_guard.py](../tests/test_planetdb_guard.py) | Retain: The planet database is GLOBAL, so wiping it is not an ordinary user's button. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_print_locking.py](../tests/test_print_locking.py) | Retain: A blueprint is an item: jobs running off one print never overlap in time. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_production_allocation.py](../tests/test_production_allocation.py) | Retain: Slot reuse and order allocation regressions; run in the local application container. Validation: passed. |
| [tests/test_progress_running.py](../tests/test_progress_running.py) | Retain: The hand-set middle state: not started → running → done, and the rule that keeps it honest. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_reaction_job_fees.py](../tests/test_reaction_job_fees.py) | Retain: Job install fees are quoted for an account that never typed a system in. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_reaction_progressive.js](../tests/test_reaction_progressive.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_reaction_stock.py](../tests/test_reaction_stock.py) | Retain: Intermediates you already hold are not reacted again. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_reactions.py](../tests/test_reactions.py) | Retain: Correctness tests for the fixed-unit "customer order" mode in Reactions (app/reactions.py) — Remove only unused bindings/imports; preserve exercised behavior. Validation: passed. |
| [tests/test_reactions_deadline.js](../tests/test_reactions_deadline.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_reactions_profit_clock.py](../tests/test_reactions_profit_clock.py) | Retain: Reactions: profit truth and the clock (WS1 of docs/reactions-repair-2026-08.md). Validation: passed. |
| [tests/test_reactions_standalone.py](../tests/test_reactions_standalone.py) | Check the current General-settings schedule editor and shared weekly clear/default, rather than removed wizard onchange behavior. Validation: passed. |
| [tests/test_recurring_job_identity.py](../tests/test_recurring_job_identity.py) | Retain: Recurring progress must follow ESI job identity, never reuse completed batch rows. Validation: passed. |
| [tests/test_redeploy_candidates.py](../tests/test_redeploy_candidates.py) | Retain: Tests for /api/redeploy-candidates — the two Setup Analysis extraction signals: Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_refill_deadline.js](../tests/test_refill_deadline.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_refill_rates.py](../tests/test_refill_rates.py) | Retain: Per-factory refill rates: the numbers "refill to a deadline" (TODO §21) is computed from. Validation: passed. |
| [tests/test_required_skills.py](../tests/test_required_skills.py) | Exercise the production skill analyzer directly instead of an unused gap-only facade. Validation: passed. |
| [tests/test_reseat_events.py](../tests/test_reseat_events.py) | Retain: Pure unit tests for planner.reseat_redeploy_events — dating the last reseat and last redeploy from a Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_routing.py](../tests/test_routing.py) | Retain: A page is a URL, and three lists have to agree about which. Remove only unused bindings/imports; preserve exercised behavior. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_routing_client.js](../tests/test_routing_client.js) | Retain: JavaScript regression or tooling entry point. Validation: passed. |
| [tests/test_running_shopping.py](../tests/test_running_shopping.py) | Retain: Running reaction jobs have consumed their materials and must leave the shopping list. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_sde_migration.py](../tests/test_sde_migration.py) | Retain: Regression tests for the SQLite (data/sde.db) -> Postgres SDE migration. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_service_refactor.py](../tests/test_service_refactor.py) | Add malformed/empty/stored type-id coverage for account rules and order overrides. Validation: passed. |
| [tests/test_settings_memo.py](../tests/test_settings_memo.py) | Retain: The Industry settings row is read once per request, and a write is never read back stale. Validation: passed. |
| [tests/test_setup_page.py](../tests/test_setup_page.py) | Retain: The PI planner's setup page: the sections a user must be able to find. Remove only unused bindings/imports; preserve exercised behavior. Validation: passed. |
| [tests/test_shopping_roots.py](../tests/test_shopping_roots.py) | Retain: The shopping list buys a chain's materials ONCE. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_skill_enough.py](../tests/test_skill_enough.py) | Retain: "Already enough skill" half of /api/skill-roi. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_skill_time_mults.py](../tests/test_skill_time_mults.py) | Retain: Job-time multipliers use the account's REAL skills wherever we can prove we have them. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_stock_reservations.py](../tests/test_stock_reservations.py) | Retain: Materials assigned to a slot are not offered to the next plan as well. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_storage_projection.py](../tests/test_storage_projection.py) | Retain: Stopped extraction must not refill launchpads in the dashboard or alert engine. Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_system_search.py](../tests/test_system_search.py) | Retain: Solar-system typeahead (`GET /api/systems/search`, app/planetary.py `search_systems`). Source/reference review; this suite was not executed in the isolated run. |
| [tests/test_ui_notes.py](../tests/test_ui_notes.py) | Replace obsolete footnote assertions with current actionable warning, order-price and navigation contracts. Validation: passed. |
| [tests/test_upgrade_policy.py](../tests/test_upgrade_policy.py) | Retain: Automatic dependency PRs must be tested without publishing or deploying them. Validation: passed. |
| [Dockerfile](../Dockerfile) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. |
| [docker-compose.yml](../docker-compose.yml) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. |
| [requirements.txt](../requirements.txt) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. |
| [renovate.json](../renovate.json) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. |
| [.github/workflows/build.yml](../.github/workflows/build.yml) | Run the new frontend contract in pull-request CI; branch does not publish images or deploy. |
| [browser-tests/Dockerfile](../browser-tests/Dockerfile) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/package.json](../browser-tests/package.json) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. Configuration/source review; no authenticated browser protocol run. |
| [browser-tests/package-lock.json](../browser-tests/package-lock.json) | Retain: Build, service, CI or dependency manifest; retain supported runtime and automation configuration. Configuration/source review; no authenticated browser protocol run. |
