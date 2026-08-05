# eve-pi-planner — feature reference

Per-feature notes for the smaller subsystems. Look up only the one you are touching.
Back to [CLAUDE.md](../CLAUDE.md).

## Bug reporting (`app/bugs.py`, `pp_bugs` table)

Any logged-in pilot can file a report; only admins read/triage them. **Admin = account owns
a character whose name is in `esi.ADMIN_CHARACTERS`** (permanent bootstrap set, `{"ekaoni"}`)
**OR in the `pp_admins` table** (DB-backed, managed from the Admin tab). EVE names are globally
unique and SSO-verified, so a name match proves ownership — no separate admin auth.
`esi.is_admin(pp_session)` checks the session context's characters against the bootstrap set ∪
`pp_admins` (`_db_admin_names`); `esi.require_admin` is the 403 dependency. `is_admin` is
surfaced in `GET /api/characters` so the SPA shows the Admin tab/button. Bug endpoints: `POST
/api/bugs` (login-gated), `GET /api/bugs` (admin, `?status=`), `POST /api/bugs/{id}/status`
(admin, status ∈ `open|complete|ignored`). UI: header **Report bug** opens `#bugModal`; the
bug **list/triage moved into the Admin tab** (`loadBugs`/`renderBugs`/`filterBugs`).

## Admin tab & custom baskets (`app/admin.py`)

A new top-nav **Admin** tab (`#tab-admin`, shown only when `is_admin`; `app.js` `switchTab`
→ `onAdminTabOpen`) hosts three sections: Custom baskets, Admin users, Bug reports.

- **Admin users** (`pp_admins` table; `ensure_admin_table` in `esi.py`): `GET/POST/DELETE
  /api/admins` (admin-gated). Bootstrap names (`ADMIN_CHARACTERS`) show as "permanent" and
  can't be removed; everyone else is add/remove from the UI.
- **Custom baskets** (`pp_baskets` + `pp_basket_items`): a basket is a named set of PI
  commodities (P1–P4) + per-run quantities, planned by the **same engine as the built-in fuel
  block**. `GET /api/baskets` is **public** (the wizard lists baskets in the product picker);
  `POST/PUT/DELETE /api/baskets` are admin-gated. Each item is validated as a real PI type at
  tier ≥ 1.
- **Per-character config sentinel:** a basket's `pp_plan_config` rows are keyed by
  `BASKET_CONFIG_BASE + basket_id` (`BASKET_CONFIG_BASE = 2_000_000_000`, above real type_ids
  and the fuel-block `4312`). This sentinel is stored as the product `type_id` in profiles/
  shares, so they encode the basket with **no extra field** (`_basketIdFromTid` derives it).

**Engine generalization** (`fuelblock_planner._resolve_target_basket`): `FuelBlockPlanRequest`
gained `basket_id`. `None` → built-in fuel block (`resolve_bom`, ME applied, racial block
pricing, `BLOCKS_PER_RUN`=40). Set → custom basket (`fuelblocks.resolve_basket_components`
from the DB, **no ME**, ISK = Σ component market value/day, `run_size`/`unit_label` from the
basket). `_compute_fuelblock_budget` / `_assign_fuelblock_factories` are reused unchanged. The
plan result carries `unit_label`; `renderFinalPlan` uses it instead of hardcoded "fuel blocks".

UI (`planetary.js`): `_wiz.basketId` (null = fuel block); `_refreshBaskets` keeps the picker
options live after admin edits; the manufacturing-ME card shows only for the built-in fuel
block (`builtinFb = _wiz.fuelblock && !_wiz.basketId`).

## Disconnecting a character (`DELETE /api/characters/{id}`)

Removes one character from the calling account: a **hard delete**, not a soft unlink. A row left
behind with `context_id` cleared would still hold a live refresh token — i.e. we could still read
that character from ESI — which is exactly what a user clicking ✕ is asking us to stop doing
(rule 8). The endpoint has always behaved this way for placeholder and wallet-only characters;
what was missing was that it only cleared `pp_characters` + `pp_char_planets` and orphaned rows in
**eight** other tables.

- **`_CHAR_OWNED_TABLES`** (`app/esi_data.py`) is the delete list — per-character operational
  state, all of it re-createable by a rescan if the character is re-added. **Add a new
  `character_id`-keyed table to this list when you create one**, or a disconnect will silently
  leave its rows behind.
- **Ownership is checked FIRST** (`SELECT … WHERE character_id=? AND context_id=?`, 404 if it
  doesn't match). Every delete below it is keyed by `character_id` **alone** — without that check
  any logged-in user could wipe any character's rows by guessing an id.
- **Missing tables are skipped, not fatal** (`_table_exists`). Several of these belong to modules
  (industry, reactions, markets) whose `ensure_*` may not have run in this process yet, and on
  Postgres a statement against a missing table aborts the WHOLE transaction — rolling back the
  deletes that already succeeded. The probe is written in the `sqlite_master` form that
  `app.db._pg_translate` rewrites to `information_schema`.
- **References are cleared, not left dangling:** `pp_market_config.market_character_id` (else the
  `_market_character` fallback silently re-decides something the user chose explicitly) and the
  removed id is stripped out of every saved plan's `pp_profiles.factory_character_ids`.
- **Sessions are re-pointed, not dropped.** `pp_sessions` binds the character you logged in *with*,
  but is scoped to the account; if that's the character being removed, the session moves to any
  surviving character rather than logging the user out mid-action. Only a now-empty account ends
  the session (`logged_out: true` in the response).
- **Not deleted, deliberately:** `pp_bugs` (an admin support record, not the character's data — it
  already denormalises `character_name` so it stays readable) and
  `pp_industry_completions`/`pp_reaction_completions` (the ACCOUNT's earnings ledger; `character_id`
  is provenance, and deleting them would silently rewrite historical profit).
- **The ESI grant is revoked** best-effort after the commit (`esi.revoke_refresh_token`, 5s timeout,
  never raises). Dropping our copy already stops *us* using it, but the grant lives on at CCP until
  it expires, and "disconnect" should mean it's actually gone. The disconnect never fails on it.
- **Irreversible bit:** `pp_colony_yield` (measured yield per colony across reseats) cannot be
  re-derived by re-adding the character — ESI only reports the CURRENT extraction program. The UI
  confirm names that specifically; everything else comes back on a rescan.
- Covered by `test_disconnect_character.py` (in-process + a live HTTP layer for the
  `require_context` gate).

## Deleting an account (`DELETE /api/me`)

The account-level counterpart, in `app/esi.py`. It had the same bug in a worse place: it cleared
three per-character tables and four context tables while orphaning rows in roughly twenty others —
on the endpoint whose entire promise is "delete all my data".

- Works from **two shared lists in `app/esi.py`**: `_CHAR_OWNED_TABLES` (also used by the
  per-character disconnect, so the two can't drift) and `_CONTEXT_OWNED_TABLES`. Both are explicit
  lists, **not** a reflective "every table with a context_id column" sweep — adding a table should
  be a deliberate decision about whether an account deletion takes it, not something that starts
  happening silently. **Add new per-account tables to `_CONTEXT_OWNED_TABLES`.**
- **The opposite call to the per-character case on history:** the completions ledgers
  (`pp_industry_completions`, `pp_reaction_completions`) and every per-character work record ARE
  deleted here, because the account they belong to is itself going away.
- **`pp_bugs` is anonymised, not deleted** — `context_id`/`character_id` nulled and the name
  replaced with `(deleted account)`. The report is about the app, not the reporter, and admins
  still need open bugs triaged after someone leaves; but nothing identifying the account may
  survive.
- **`pp_markets` is keyed `(owner_kind, owner_id)`** — only `owner_kind='account'` rows are the
  user's. Group-level market lists survive; they belong to the group and are shared with other
  members. Same reasoning for `pp_reaction_settings` and the `pp_group_*` tables.
- **`pp_shares` / `pp_inventory_shares` cannot be cleaned by account** — they have no owner column
  at all, by construction: a share is an opaque id plus a payload, deliberately unattributable to
  the account that created it. There is nothing to delete by context, not an omission.
- Missing tables are skipped via the same `_table_exists` probe, for the same Postgres
  whole-transaction-abort reason described above.
- Covered by `test_delete_account.py` (seeds rows by introspecting each table's NOT NULL columns,
  so a new column can't silently turn a seed into a skipped assertion).

## Reactions suggestion engine (`app/reactions/advisor.py`)

Split out of `app/reactions/jobs.py`, which had grown to ~1,500 lines covering three unrelated
jobs (ESI job fetching, the persistent slot plan, and this). `advisor.py` holds the two-stage
wizard engine — the knapsack over WHAT to run, then bin-packing onto WHO runs it — plus
`/api/reactions/suggest`. It imports from `jobs.py` **one way only**; `_character_capacities`
deliberately stayed in `jobs.py` (it's about slots, and the customer-order allocation path needs
it too), which is what keeps the dependency acyclic. `__init__.py` imports jobs before advisor
for the same reason.

## Shared alert engine (`app/alerts.py`) + configurable thresholds (`app/alert_settings.py`)

**`compute_alerts(context_id, rows=None, now=None)`** is the single source of truth for
every colony warning in the app — both the Dashboard (`planner.py`'s `dashboard()`, for display)
and the notification scheduler (`app/notifications.py`, for pushes) call it and nothing else
re-implements detection. This was a deliberate unification (2026-07-10): notifications used to
have their own bespoke `_extractor_events`/`_factory_events` queries, duplicating (and able to
drift from) what the Dashboard showed. Returns a flat list of individual alert instances —
`{kind, severity, character_id, character_name, planet_id, location, hours_left, pct
(storage_full only)}` — across 11 kinds (`app.alert_settings.ALERT_KINDS`): four threshold-based
(`expired`, `expiring`, `storage_full`, `factory_refill`); four correctness-based, stored
per-scan by `app.esi._detect_colony_issues` (`ext_unrouted`, `fac_unfed`, `fac_output`,
`p0_mismatch` — always "high" severity, never muted-by-default); `schedule_sync` (an extractor
running a different program length than the fleet's norm, always "warn", computed fleet-wide via
`_extractor_program_lengths()`); and two Reactions kinds (`reaction_finishing_soon`,
`reaction_completed` — see `_reaction_alerts()`), which are not about PI colonies at all but are
folded into the same flat list so they inherit the mute/severity/dashboard/push plumbing. That
last pair is why the module is `app/alerts.py` and not `colony_alerts.py`. `factory_refill` has no
dedicated threshold fields of its own — deliberately reuses `expiring_hours` as "how far ahead to
flag" and `storage_high_ttf_hours` as the warn→high cutoff (same ideas as extraction expiry and
storage already use) rather than adding two more number fields for one kind. `dashboard()` passes
its own already-fetched `pp_char_planets` rows in via `rows=` to avoid a second query; the
scheduler (iterating many contexts) leaves it `None` and the function does its own fetch.
`dashboard()` re-groups the flat list into its existing display cards (per-character correctness
tallies, collapsed expired/expiring/factory-refill lines, the grouped storage card) — collapsed
cards derive their severity from whether **any** instance in the group is "high" (see
`factory_refill_high` in `dashboard()`), not a hardcoded value, so e.g. one imminent factory
refill escalates the whole "Factories due for refill" card even if others in the batch aren't
urgent yet.

**Thresholds and muting** (`app/alert_settings.py`) are per-account: `pp_alert_settings`
(context_id PK, one row per customizing account — no row = defaults, set to exactly what used to
be hardcoded, so nothing changed behavior until a user edits it): `expiring_hours` (3h),
`storage_warn_pct` (80), `storage_high_pct` (95) / `storage_high_ttf_hours` (2 — either escalates
a pad to "high"), `storage_urgent_hours` (3 — counted in the "(N within Xh)" header). `muted_kinds`
(JSON column, same row) lets ANY of the 11 kinds be turned off entirely, including the
correctness-based ones with no numeric threshold to tune. `get_alert_settings(context_id)` is the
single read path `compute_alerts()` and the settings endpoints all use, so they can't drift.
`GET/PUT /api/alert-settings` + `POST /api/alert-settings/reset`, all `require_context`-gated (own
account only). `ALERT_KINDS` is the key+label registry — `GET /api/alert-settings` echoes it back
as `available_kinds` so the frontend never hardcodes labels (the same registry is reused by
`GET /api/notifications/prefs` for the same reason). UI: Settings modal → **Alerts** section
(`settingsSecAlerts`, gated by the `alert_settings` flag like `notifications` gates its own
section) — 5 threshold number-inputs (`.settings-field-row`, label left/control right/hairline
divider — replaces an earlier ad-hoc inline layout that misaligned once there were several rows of
differing label length) + a "Muted alerts" 2-column checkbox grid (`.settings-toggle-grid`),
Save/Reset.

## Fill-factories meter (Dashboard, `pad_fill` flag)

"How far does the P1 in my extractor pads go toward filling all my factories?" Backend
`_pad_fill_meter(parsed, pi, types)` in planner.py (attached as `pad_fill` in the dashboard payload):
- **have** = P1 sitting in EXTRACTOR launchpads, per type_id, forward-projected via `pi_sim.project`
  (falls back to raw `pad_contents`).
- **need** = each factory's 30,000 m³ (3-LP) buffer split by **consumption ratio**, per material:
  `30000 × (frac/Σfrac) ÷ 0.19 m³/unit`. NOTE `_compute_p1_fracs` returns **P1-per-product recipe
  quantities, NOT fractions summing to 1** — normalise per factory (`frac/Σfrac`) or `need` blows up
  ~4000× (a real bug hit during dev). Full buffer per factory = 30000/0.19 ≈ 157,894 units.
- **fill %** = the BINDING material `min(have/need)` (you need them all); `materials[]` is the
  per-material breakdown (have/need/pct), weakest first.
Frontend (`dashboard.js`, gated by `_featureActive('pad_fill')`): a top Overview tile ("N% to fully
fill factories") + a "Fill factories from pads" card with the binding statement + per-material bars
(`.padfill-*`). Default off (admin-preview).

## Admin sub-navigation

The Admin tab (`#tab-admin`) is split into sub-pages via an inner nav (`adminSubPage(key)`,
remembered in `localStorage` as `adminPage`): **Planet submissions · Features · Baskets · User
management · Bug reports**. Each is a `.admin-subpage[data-page]` div toggled by the sub-nav; all
sections still load their data on `onAdminTabOpen` so the nav badges (pending submissions, open bugs)
populate. Add a sixth page = one `<button>` in `#adminSubnav` + one `.admin-subpage` div.

## Dashboard "Up next" agenda (`timeline` flag)

Account-level sorted list of the next maintenance tasks (Restart extractors / Haul extractor P1 /
Refill factories) with countdown + absolute clock time, on the Dashboard under Maintenance routine.
`_renderTimelineCard(t)` reuses the existing dashboard `*_due_hours` totals (no extra request).
**Deliberately NOT a single-cycle line** — extractor and factory cadences desync badly (several
extractor restarts per factory refill), so a "you are here on one timeline" viz is misleading; a
sorted agenda is honest. Gated by `timeline`; shows an "admin preview" tag only while not public.

## Skill-ROI advisor (`skill_roi` flag, Setup Analysis)

`GET /api/skill-roi` (session-scoped): per character, the output gain from the next level of the two
yield skills — **Interplanetary Consolidation** (<5 → +1 planet ≈ one colony's average value/day,
from `total_value_day / total_planets`) and **Command Center Upgrades** (<5 → extra factory units
that pack onto each FACTORY planet, via `_units_per_planet` = layout-engine `max_count` at cc vs
cc+1, × per-unit value). Gain-only (no SP/train cost — the user's spend decision). Sorted by ISK/day,
top 12. Frontend: `_fetchSkillRoi()` + `_renderSkillRoiSection()` appended in `renderAnalysis`.

**The response has TWO halves — the other one says "stop training".** `enough[]` (rendered as the
"Already enough skill" card) is where a skill is already past what the character's colonies use.
PI advice defaults to "train everything to V", which is a long haul on a rank-4 skill for a level
plenty of colonies never touch. Two sources, in order:
- **Command Center Upgrades** — preferred basis is `pp_char_planets.upgrade_level`, the level the
  colonies are ACTUALLY upgraded to in-game (`basis: "deployed"`). Observed state can't over-claim:
  a player who really did upgrade to V reports V and gets no advice. Falls back to a **modelled**
  requirement (`_required_cc_extractor` / `_required_cc_factory` = lowest level fitting as many
  basics / packing as many units) for scans predating the column — that fallback is against our
  MAXIMAL archetype, so it's the conservative answer. Taken as the **max over ALL the character's
  planets, extractors included** — characters are rarely pure-factory.
- **Interplanetary Consolidation** — planet slots trained but not deployed. Related fix: the
  gain-side IC suggestion is now suppressed while `free_slots > 0`; telling someone to train a
  rank-4 skill for a slot while one sits empty is backwards.
`_units_per_planet` returns **0** (not `max_count`'s floor of 1) when not even one unit fits the
budget — otherwise the advice reads "this level runs your P4 planet" for a level that can't host it.
Covered by `test_skill_enough.py` (seeds colonies deployed below the trained level + a character
with idle slots, via a fabricated session cookie).
**Limitations (v1):** flat per-unit factory rate (same model as `my-setup-plan`); P4 factories are
1/planet so CCU shows no gain for them; **extractor-side CCU (more basics → more P0→P1 refining) is
NOT modelled yet** — the documented follow-up. Returns nothing when all characters are IC5/CCU5
(correct — nothing to train). Planetology / Advanced Planetology affect survey only, not yield, so
they're excluded.

## Refill "empty pads" toggle

Factory launchpad contents come from the last ESI scan and are **not** simulated forward (only
extractors are), so the scanned "P1 already in the pad" goes stale and over-reports (ESI returns the
last-checkpoint contents, from before the factory drew them down — a rescan re-reads the same stale
checkpoint). The Refill tool's **"Pads emptied at drop-off"** toggle (`_refillIgnorePads`, default
ON) ignores `input_m3` and fills to a clean 30,000 m³ (3 LP), matching the usual "empty the pads when
you drop the next batch" workflow. Off = subtract the last-scan contents. m³/unit is 0.19 (verified);
the under-fill people hit was the stale `input_m3`, not the volume constant.

## How-it-works poster + social banner

`static/how-it-works.svg` (9:16 five-step infographic) is the hero on the How-it-works page, opened
in an in-page dark lightbox (`openImageLightbox`/`closeImageLightbox` — generic, reusable) instead of
the bare white file URL. The social/OG preview `static/og-image.png` (1200×630) is the 3:1 banner
(`eve_pi_banner.svg`) centred on a matching dark canvas; the og:image `?v=` is stamped automatically alongside every other asset in
`index.html` AND the `/s/{id}` OG injection in `main.py` when it changes. SVG source posters live in
`~/Claude-Workspace/` (`eve_pi_planner.svg`, `eve_pi_banner.svg`); re-render the OG with cairosvg +
Pillow.

## Mobile layout + "Add to Home Screen"

The site is usable on phones and meant to be **bookmarked to the iOS home screen** — a plain
shortcut, NOT a packaged PWA (no `manifest.webmanifest`, no service worker). `index.html` head
carries `viewport-fit=cover` + `theme-color` + the `apple-mobile-web-app-*` meta (capable=yes,
`black-translucent` status bar, title "EVE PI") so the bookmark opens full-screen; the existing
`apple-touch-icon` supplies the icon.

A single `@media (max-width: 760px)` block at the **end of `style-misc-responsive.css`** (the last
of the `style-*.css` files loaded — see below) does the rest:
- The left `.sidebar` becomes a **fixed bottom tab bar** (icon-over-label). Selectors are paired
  with `body.nav-collapsed .sidebar …` so a desktop-collapsed state can't out-specify them.
- **Only the lightweight pages show** on the bar, in this order (flex `order:` overrides, How-it-
  works and Setup-Analysis swapped vs. desktop): **Dashboard · Setup Analysis · How it works ·
  Admin** (admins only). The heavy tools — Planetary Planning, Factory Layout, Find
  Buildables/Refill (the `.nav-group`), Planet DB, Characters, and **Contribute** (no mobile value)
  — are `display:none` (and dropped from `MOBILE_TABS` in `app.js`). Dashboard stat tiles go 3-up on
  phones (`#dashboardContent .an-stats`); Setup-Analysis stats stay 2-up. Login and
  **Rescan both live in the header**, so the hidden Characters tab isn't needed on mobile. The
  header drops `#reportBugBtn` and keeps `EVE PI` on one line (`white-space:nowrap`).
- `#dashboardNavTab` is forced visible (`display:flex !important`) so the bar stays consistent when
  logged out (JS otherwise inline-hides it); its panel is the login CTA (which now points to the
  header **Login** button, not the hidden Characters tab).
- Hidden on phones in Setup Analysis: `.an-suggest-move` (rebalance "move factories" cards) and
  `.an-suggest-sep` (the manual "Move a character to another account" tool).
- `.an-stats` becomes a 2-up grid (a lone stat tile no longer stretches full-width with its value
  stranded left); `.pp-card-title` wraps so the analyze "Plan" dropdown gets its own full-width line.
- Two-column page grids (`.pp-layout`) stack; `.pp-card` gets `overflow-x:auto` so
  wide tables scroll inside the card instead of the whole page.

`app.js` DOMContentLoaded has a matching guard: `MOBILE_TABS` + `matchMedia('(max-width:760px)')`
— on a phone it never lands on a hidden tab, falling back to **Dashboard** (a `/s/<id>` share link
still opens the plan view). `app.js` also adds **pull-to-refresh** (`setupPullToRefresh`): dragging
down from `scrollTop 0` past a threshold triggers `rescanAll()` (only when the header `#rescanBtn`
exists, i.e. logged in), with a `#ptr-indicator` banner. Standalone home-screen apps have no native
pull-to-refresh, so this is ours. (No `?v=` bump needed on changes — the server stamps every asset
URL with the running build; see the asset-stamping note above.)

## Admin → Corp wallet (donations)

Admin-only view of the corp ISK balance + **player donations**, read via ESI so the owner doesn't
have to log the toon into the game (web SSO needs only a browser — handy for an alpha account that
can't run alongside other characters). Admin-gated, so no public feature flag.

**Scope handling — one app, opt-in scope.** The base `SCOPES` (skills + planets) is unchanged, so
the normal **Login** never asks the public for wallet access. `esi.WALLET_SCOPE =
esi-wallet.read_corporation_wallets.v1`; **`WALLET_SCOPES = WALLET_SCOPE`** — the connect flow
requests ONLY the wallet scope (no skills/planets/POCOs; the wallet toon is a read-only money viewer
and isn't planned over, so the callback's skill/planet fetches just fail silently). `/auth/login`
gained a `wallet: int = 0` query param — `?wallet=1` requests `WALLET_SCOPES` instead of `SCOPES`.
The EVE application (developers.eveonline.com) must **list** the wallet scope in its allowed set, but
listing ≠ requesting — it's only requested on the wallet flow. No second app needed.

**Granted scopes are stored.** `pp_characters.scopes` (TEXT, migrated via `ALTER TABLE`) holds the
JWT `scp` claim (a list, or a bare string for one scope) captured in `esi_callback` — so we can find
which character authorised wallet read. `_wallet_character(context_id)` returns the first character
in that context whose `scopes` contains `WALLET_SCOPE`.

**`esi.corp_wallet_summary(context_id)`** (called by `GET /api/corp-wallet`, `Depends(require_admin)`
which returns the admin's context id) reads, via that character's token: `/characters/{id}/` →
`corporation_id`, `/corporations/{id}/` → name, `/corporations/{id}/wallets/` → per-division balances
(403/401 → `{error:'role'}` = character lacks Accountant/Junior-Accountant/Director), and
`/corporations/{id}/wallets/1/journal/` → entries with `ref_type == 'player_donation'` (donor =
`first_party_id`, resolved via `_resolve_names`). Returns `{connected, balance (div1), total_balance,
total_donated, donations:[{date,amount,donor,reason}], corp_name, ...}`; `{connected:False}` when no
wallet character is linked; `{error:'token'|'fetch'}` otherwise. **Donations/`total_donated` cover
only the most recent journal page (~2500 rows)** — fine for a low-volume corp; the *balance* is
always current. Journal is the master division (1) only.

**Frontend:** Admin sub-page `data-page="wallet"` (`loadCorpWallet`, lazy-loaded from `adminSubPage`
only when opened, since it hits ESI). `connectCorpWallet()` mirrors `esiLogin()` but opens
`/auth/login?wallet=1`. The connected toon joins the admin's context like any character (shows in
Characters / may get PI-scanned — set `planet_limit=0` to exclude from plans if it clutters).
Gating test in `test_features.py` (`test_corp_wallet_gated` → 403 for anonymous).

## Local / alliance market pricing (`app/markets.py`, `local_market` flag)

Reactions pricing can follow one or more **markets** in a priority chain — a player-owned Upwell
**structure** market and/or a public NPC **region** market — falling back to **Jita** (Fuzzwork,
`app.market`) for anything not listed locally. Built because an alliance selling inputs below Jita
on its own structure market was invisible to the tool. Reactions-tab only for now (PI planner /
fuel blocks stay on Jita).

- **Opt-in scope** (`app/esi.py`): `MARKET_SCOPE = esi-markets.structure_markets.v1` +
  `SEARCH_STRUCT_SCOPE = esi-search.search_structures.v1` (+ reused `STRUCTURES_SCOPE` for name
  resolution). `MARKET_SCOPES` **unions** the base `SCOPES` (full PI+market char, like the reactions
  flow). Requested only via `/auth/login?market=1` (`esi_login(market=1)`), never on public Login.
  Frontend clone `connectReactionsMarket()` (`planetary.js`). **Prereq:** the two new scopes must be
  LISTED on the EVE application at developers.eveonline.com (listing ≠ requesting, same as wallet).
- **Config = per-account, group-seeded** — one table `pp_markets(owner_kind account|group, owner_id,
  kind structure|region, location_id, name, priority, active)`. `effective_markets(context_id)` =
  personal list → account's group-manager default list → `[]` (Jita only). Jita is NEVER a row (the
  implicit last fallback). CRUD `GET/POST/DELETE /api/markets`, `POST /api/markets/reorder`
  (require_context; group scope gated by `is_group_manager`). Mirrors the freight resolver in
  `app/reactions/settings.py` (`effective_reaction_settings`) — **freight was already built**, this
  reuses its account-settings UI.
- **Per-context state** `pp_market_config(context_id, market_character_id, onboarded)`. The
  **market character** (whose token reads the structure market) is user-designatable — `POST
  /api/markets/reader {character_id}` (must be a context char holding `MARKET_SCOPE`);
  `_market_character` returns the designated one if still scoped, else the first scoped char
  (back-compat default). `onboarded` is the one-time first-run flag, set by `POST
  /api/markets/complete` (requires ≥1 character in the context). `_markets_payload` also returns
  `characters` (each with `is_market`) and `market_character_id` so the UI can list them + pick the
  reader.
- **Reads** (`app/markets.py`): `fetch_structure_market(context_id, structure_id)` paginates
  `GET /markets/structures/{id}/` via `_market_character`'s token (first char in the context holding
  `MARKET_SCOPE` — clone of `esi_data._wallet_character`), aggregates the whole book per type with
  `_wavg_percentile` (volume-weighted 5th percentile, robust to a lone 1-unit order — matches
  Fuzzwork's shape so it's drop-in). Redis-cached by structure_id (book is identical whoever reads
  it). `fetch_region_market(region_id, type_ids)` is public, per-type, Redis-cached per (region,type).
  `resolve_market_data(context_id, type_ids)` walks `effective_markets` in priority order, takes the
  first market quoting each type, else Jita `fetch_market_data`; each entry carries an extra `source`
  label. **Drop-in for `fetch_market_data`** — the reactions call sites in `graph.py`/`jobs.py` were
  swapped to it (all already had `context_id` in scope). With no markets configured it returns
  exactly Jita, so behavior is unchanged for everyone until they set one up.
- **Freight applies to Jita-sourced items only.** In `_load_goo_and_reached`, `purchasable`'s import
  shipping (`import_isk_per_m3 × volume`) is added **only when `m["source"] == "Jita"`** — the haul
  from the remote hub. A material sourced from a followed local/alliance market gets NO import
  freight (the market is assumed at/near the reaction site; a user who follows a far-off market
  prices their own transport into that market's own numbers). Moon goo from the group sheet already
  had zero import cost (separate `goo` path). `_materials_report`'s `market_name` names the winning
  market per leaf.
- **Search** (`/api/markets/search?q=`): structures via `GET /characters/{id}/search/?categories=
  structure` (only ones the char can access) resolved to names via `/universe/structures/{id}/`;
  regions matched against SDE `constellations.region` names, resolved to ids via public
  `/universe/ids/`. Needs a connected market character for structure results.
- **UI** (`reactions.js`, gated by `_featureActive('local_market')`): the whole Reactions tab is
  **blocked behind an inline first-run gate** (`#rxGate`, `_rxApplyGate`/`_rxRenderGate`) until the
  user has added ≥1 character and clicked **Save & continue** (`_rxCompleteOnboarding` → `POST
  /api/markets/complete`). `onReactionsTabOpen` is async and returns early while gated (hides
  `#rxDashboard`). The gate has 3 steps — **(1) Add your characters** (required; lists context chars
  via `_rxCharListHtml` with a **market-character radio** among scope-holders → `_rxSetMarketReader`,
  plus `connectReactionsMarket`), **(2) Add local markets** (optional), **(3) Configure freighting
  costs** (optional, foldable, reuses `_rxAccountSettingsFormHtml`). `_rxApplyGate` **fails open** (no
  gate) if the feature's off or the fetch fails, so a hiccup never locks the tab. Once `onboarded`,
  the gate never shows again; `#rxMarketSetupCard` shows a one-line "Reaction pricing: A → B → Jita"
  summary and **all edits go through the Reactions ⚙ Settings modal** (`_rxOpenSettingsModal`, which
  hosts the market manager above the freight forms). The market list + search is a **reusable manager
  component** (`_rxMarketManagerHtml` / `_rxMountMarkets` / `_rxRenderMarketManager`) mounted into
  either the gate (`#rxOnboardMarkets`) or the Settings modal (`#rxSettingsMarkets`); `_rxMarketMount`
  tracks which is live so `_rxRefreshMarkets` re-renders the right one. `connectReactionsMarket`'s
  callback is `_rxAfterConnect` (refreshes gate / settings / tab depending on what's open). The leaf
  source name is threaded onto each `reached` leaf node (`market_name`) in `_load_goo_and_reached` and
  surfaced by `_materials_report`, rendered as a per-line **price-source badge** in the shopping list.
- Gating test `test_markets_gated` in `test_features.py`; `local_market` in the registry.

## Notifications (`app/notifications.py`, `app/notifiers.py`)

Push alerts for any of the 11 alert kinds (`app.alert_settings.ALERT_KINDS`), checked by an
APScheduler job every 15 minutes (`make_scheduler`, `check_and_send_notifications`) — pure DB
math, no ESI calls, so it runs freely between rescans. Settings/prefs/log are per-`context_id` in
`pp_notification_settings`, `pp_notification_prefs`, `pp_notification_log`.

- **Event detection is not this module's job.** `_process_context` calls
  `app.alerts.compute_alerts(context_id)` — the same function `dashboard()` uses —
  and only filters/batches/sends. There used to be bespoke `_extractor_events`/`_factory_events`
  queries here, duplicating what the Dashboard computed; unified 2026-07-10 so a push and what's
  shown on screen can never drift apart. If you're tempted to add a new kind of push alert, add
  the kind to `compute_alerts()` first, not here.
- **Prefs = which kinds + a severity floor.** `pp_notification_prefs.notify_kinds` (JSON array of
  `ALERT_KINDS` keys) + `min_severity` (`"warn"` = everything, `"high"` = high only). Old
  `lead_hours`/`notify_extractors`/`notify_factories` columns are left in place (harmless, unused)
  rather than dropped — this codebase's migration convention is additive `ALTER TABLE ADD COLUMN`,
  never `DROP COLUMN`. **One-time migration** (in `ensure_notification_tables()`) derives
  `notify_kinds` for pre-existing rows from those old booleans
  (`notify_extractors→[expired,expiring]`, `notify_factories→[factory_refill]`) so an account that
  had already muted e.g. factory refills doesn't suddenly get pinged for it — but does **not**
  auto-enable the new kinds this unification added (`storage_full`, `ext_unrouted`, ...) for
  already-configured accounts, since silently expanding what an already-tuned account gets pinged
  about is the wrong default. A brand-new context (no prefs row at all) gets all 11 kinds enabled,
  matching the old out-of-the-box default. `GET /api/notifications/prefs` echoes back
  `ALERT_KINDS` as `available_kinds` (same registry `/api/alert-settings` uses) so the frontend
  never hardcodes labels.
- **Channels** (`notifiers.py`, `_CHANNEL_MAP`): Pushover, ntfy.sh, Discord webhook. Each is a
  `BaseNotifier.send(title, body, url=None, fields=None)`. `fields` (a list of `{name, value,
  inline}`) is Discord-only — when present, `DiscordNotifier` sends a **rich embed** instead of
  plain text; Pushover/ntfy ignore it and use `title`/`body`. Discord content is truncated at 2000
  chars (hard API limit) as a fallback safety net — the embed path avoids hitting it in practice
  since fields don't count against the same limit.
- **Batching, not per-event spam.** `_process_context` groups same-kind alerts into ONE message
  each (`_format_batch`, `_KIND_LABELS` for the per-kind title/noun), rather than firing one
  notification per planet — an earlier per-event version produced a wall of pings when several
  things expired close together. **Cooldown is per-kind** (`_COOLDOWN_HOURS`): 2h for the
  time-decaying kinds (expiry/storage), 4h for factory refill, **24h for the correctness-based
  kinds** (`ext_unrouted` etc.) — those are persistent structural problems until the player fixes
  them, not something that resolves itself, so a short cooldown would just nag every 15 minutes
  about the same unfixed issue. Cooldown is checked **before** collecting each alert into its
  batch, so one recently-notified planet doesn't block others in the same run.
- **`POST /api/notifications/resend-last`:** replays the most recent logged batch (grouped by
  `sent_at` to the minute, since all sends in one scheduler run land within seconds of each other)
  to all enabled channels, tagged `[Replay]`. Built because testing the real formatting meant
  waiting for something to actually be due — this fires immediately from history instead. No fake
  countdown (`hours_left` isn't meaningful for a replay); just character + planet.
- **`POST /api/notifications/test`** sends a one-off "channel is working" ping when adding a new
  channel — separate from resend-last, which replays real event data.

## Setup Analysis: what the advice must do

Every number and every fix on this tab reflects **sustaining the full production cycle**, measured on
honest refined/exported throughput (`per_day` = `rate_sustained`, the P1 that actually reaches
factories) — never raw head extraction (`ext_per_day`). The headline and the per-material drilldown
must use the same basis; they once contradicted each other ("98% fed, needs fixing" over bars all
reading "+2% healthy"), which leaves the user with no idea what to fix. Raw extraction is
diagnostics only (the avg-P0/hr admin mode, and deciding heads-limited vs refining-limited).

- **Never emit "leave it / you're only mildly tight" advice.** Always steer to full sustain plus a
  +10% decay buffer (`_HEALTHY_BUFFER = 0.10`). Prefer redeploy-to-a-richer-planet (no overshoot)
  over adding a colony, but always give a path to 100%+.
- **Suggest up to the buffer, not only when short.** `_burndownSection` surfaces any material with
  less than a +10% extraction buffer. Short = urgent/red; the rest = an optional, non-alarming
  "headroom top-up". Gating on short-only forced a one-fix-per-decay-cycle daily grind.
- **Fix ladder:** reseat > lower extraction cycle > redeploy same planet > redeploy another planet.
  "Lower extraction cycle" is a **global** lever (it lifts average P0/hr everywhere), so it belongs
  in the card footer, never as a per-colony rung.
- **Refining-limited** (`extSupply >= need` but `have < need`; `extPerDay > perDay * 1.05`): the
  colony can't refine what it pulls. The fix is on-planet refining capacity — higher CC level,
  smaller planet, storage-less extractor to free PG for another Basic Industry Facility, or split
  extraction — **not** reseating or adding heads. Detect and say so explicitly, even when extraction
  looks healthy.
- **`_reseatWontHelp`** pulls two classes out of the reseat list into a "Don't reseat these —
  <real fix>" block: refining-limited colonies, and freshly redeployed ones
  (`redeploy_at` within `_REDEPLOY_FRESH_DAYS` = 3, where the "decline" is just ramp noise).
- **Redeploy stays targeted, not noisy** — a single tail line ("if still short, redeploy a tapped
  colony to a richer planet"), never a per-colony wall.
- `_P0_PER_P1 = 150`; `redeploy_at`/`reseat_at` are epoch **seconds**.

### Redeploy candidates (`redeploy_proximity`, `redeploy_depletion`)

- **Overlap is measured on reachable footprints, not head positions or shared planets.** Two of your
  characters on the same planet is normal distribution, not a problem; overlapping *reachable areas*
  are, because the whole area depletes and reseating only moves heads within reach. Footprint =
  (centre `c` = the ECU pin's lat/lon, reach = farthest head + head radius) from
  `pp_char_planets.ext_heads`; overlap when `_gc_dist(a,b) < reachA + reachB` for the same P0 across
  characters. Threshold is client-side and user-set (`localStorage.ppHotspotOverlap`, default 50,
  Settings → General); the server returns every overlap above a 1% floor.
- **Depletion** = `pp_colony_yield.peak_day` trending down: window 6, min 5 programs, ≥15% decline
  start→current, ≤1 up-step tolerated. A thin-but-flat planet is deliberately not flagged. Reseat
  verdict: per-program decline < 8% **and** total < 45% → "a reseat still buys time"; past that,
  redeploy (`_RESEAT_GIVEUP_PER_PROG` / `_RESEAT_GIVEUP_TOTAL`).
- **Precedence:** a depleting member of an overlap cluster is the mover (it needs a fresh deposit
  anyway, and moving it fixes both). Depleting rows already covered by a displayed cluster are
  filtered out — don't double-list.
- **Urgency:** an overlap only *hurts* once it's eating yield, so clusters with a depleting member
  are "do soon" and the rest collapse into "whenever you next rebuild them".
- **Lead with a same-planet relocate for every reseat-can't-fix case.** Moving the extractor to a
  clearer area of the same planet keeps the existing command centre; dismantling for another planet
  (especially another system) is far more work and is a last resort, offered only when a richer
  planet is actually free.
- Fixes are **grouped by character** ("N fixes, one login") — you play one character at a time.
- Rejected UI: persistent localStorage "done" checkboxes read as cheap and improvised; move targets
  are chips instead.
- **Gotcha:** `/api/characters` colony objects are built in `app/esi_data.py` and are **Redis-cached**
  (`charlist_key`, busted on rescan). A field selected in SQL but missing from that dict is
  `undefined` frontend-wide — that hid the per-colony flag button and degraded redeploy matching.
  Add the field to that dict, then rescan before expecting to see it.
