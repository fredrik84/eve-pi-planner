# eve-pi-planner — platform & account

Everything that is not one of the three services: admin, accounts, notifications, market pricing, mobile.
Back to [CLAUDE.md](../CLAUDE.md).

Find a section: `grep -n '^## ' docs/platform.md` and read from that line — this file is meant to be read in parts.

## Contents

- **Bug reporting (`app/bugs.py`, `pp_bugs` table)** — in-app bug reports and where they land
- **Admin tab & custom baskets (`app/admin.py`)** — admin bootstrap, `pp_admins`, custom baskets
- **Admin sub-navigation** — how the admin tab is split up
- **Admin → Corp wallet (donations)** — donation tracking off the corp wallet
- **Notifications (`app/notifications.py`, `app/notifiers.py`)** — delivery channels and per-kind prefs
  - **Check before nagging (`alert_rescan_backoff`)** — the rescan an alert triggers, the four guards that keep it cheap, and why repeats back off
- **Local / alliance market pricing (`app/markets.py`, `local_market` flag)** — pricing against a local or alliance market instead of Jita
- **Disconnecting a character (`DELETE /api/characters/{id}`)** — every per-character table that must be cleared — a retained row keeps a live refresh token
- **Deleting an account (`DELETE /api/me`)** — the two owned-table lists, and what is anonymised rather than deleted
- **Mobile layout + "Add to Home Screen"** — the mobile surface
- **How-it-works poster + social banner** — the marketing assets and how they are generated

---

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

## Admin sub-navigation

The Admin tab (`#tab-admin`) is split into sub-pages via an inner nav (`adminSubPage(key)`,
remembered in `localStorage` as `adminPage`): **Planet submissions · Features · Baskets · User
management · Bug reports**. Each is a `.admin-subpage[data-page]` div toggled by the sub-nav; all
sections still load their data on `onAdminTabOpen` so the nav badges (pending submissions, open bugs)
populate. Add a sixth page = one `<button>` in `#adminSubnav` + one `.admin-subpage` div.

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

## Notifications (`app/notifications.py`, `app/notifiers.py`)

Push alerts for any of the 11 alert kinds (`app.alert_settings.ALERT_KINDS`), checked by an
APScheduler job every 15 minutes (`make_scheduler`, `check_and_send_notifications`).
Settings/prefs/log are per-`context_id` in `pp_notification_settings`, `pp_notification_prefs`,
`pp_notification_log`.

**Since `alert_rescan_backoff` (2026-08-17, TODO §37) this job is no longer pure DB math** — it
makes a bounded number of ESI reads. See "Check before nagging" below; everything above that
heading describes the path taken when the flag is off, which is still the whole behaviour for
anyone not on the rung.

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

### Check before nagging, and nag less each time (`alert_rescan_backoff`)

The reported bug: restart your extractors in game, don't rescan, and the `expired` alert fires
every two hours forever — the app nagging about a problem the user already fixed, off data that
predates the fix. Two changes, one flag.

**Rescan before sending.** Once an alert is due (true, and out of cooldown), the ONE colony it is
about is re-read from ESI, the alerts are recomputed, and only what survives is sent. A colony the
user already fixed produces no message at all.

Why this is cheap, and the four guards that keep it that way — the design constraint was explicitly
*"keep the amount of ESI requests low, because bashing them won't help me"*:

| Guard | What it does |
|---|---|
| Single-planet reads | `_fetch_planets(..., only_planet_id=)` — scoped to the one colony, not the whole character |
| `esi_expires` gate | Skip anything ESI will not have regenerated yet. This is what keeps the feature on the right side of the never-query-before-`Expires` rule in CLAUDE.md — **note the single-planet path deliberately does NOT self-check the cache** (`_cached_expiry` is only built when `only_planet_id is None`), which is correct for the button a human presses and is exactly why the check has to be explicit here |
| Dead-token filter | A character whose `refresh_token` is falsy is never called for. This is why the NOT NULL bug in `_refresh_token` had to be fixed first — until it was, no token was ever *marked* dead |
| `_SCAN_BUDGET_PER_TICK` | Hard cap for the **whole tick**, not per context — `_process_context` runs once per account, so a per-context cap would silently multiply by the number of accounts. Reset at the top of `check_and_send_notifications`; over-budget colonies are held back, not sent unverified |

**Count the requests honestly.** One target is **two** ESI requests in the steady state, not one:
`characters/{cid}/planets/` (the list, always) and `characters/{cid}/planets/{pid}/` (the detail).
It was four until the two static lookups were made conditional — `universe/names/` is now skipped
when the system is already in `solar_systems`, and `universe/planets/{pid}/` when the planet's
in-system ordinal is already stored. Both answers are immutable, so this is pure caching; both also
speed up the hand rescan. **Worst case is therefore 20 targets x 2 = 40 requests per 15-minute tick
for the entire app**, whatever the number of accounts.

Because a scan happens per *send*, and sends back off (below), an ignored problem costs about four
reads on day one and two a day after — against ~96 per character per day for the blanket-timer
alternative that was rejected.

**Backoff on repeats.** `_consecutive_cooldown_h` doubles the kind's base interval per consecutive
send, capped at `_BACKOFF_CAP_H` (12h). Derived from `pp_notification_log` rather than stored: the
log already records every send, and a counter column would be a second source of truth that drifts
the first time a send fails. A gap longer than the interval then in force means the alert stopped
being true in between, so the chain resets — **which is the property that matters**, because
without it a problem that genuinely recurs weekly would be permanently demoted to the slowest rung.

**The first send is never delayed by any of this.** `_recently_notified` only suppresses a send when
one already went out inside the window, so a newly-detected problem always fires at once. That is
what makes a 12h cap safe, and `test_alert_cadence.py` asserts it directly rather than assuming it.

**A colony that could not be CHECKED is held back, not reported.** User's call, 2026-08-17: an
alert we cannot verify is not sent. That covers all four ways a check does not happen — a failed
read, a dead token, the retry brake after a transient failure, and running out of tick budget —
because in every one of them the only thing left to report from is the stale data the feature
exists to stop trusting. The one case that is NOT held back is a colony inside its `esi_expires`
window: ESI has nothing newer to give, so that data is current by definition and the alert is sent
having been verified without a request.

The transient case (timeout, 5xx — which leave the token valid) is recorded in
`pp_characters.scan_failed_at`, which both backs the retry off by `_SCAN_RETRY_AFTER_H` and drives
an **amber** dot on the character card, distinct from the red "re-add this character". Without that
third state a valid-token character whose reads keep failing would show green while its alerts were
silently paused. A successful hand rescan clears it (`_clear_scan_failure`), so the dot's own
"Rescan to check now" is true — the job alone could never clear it for a character that no longer
has a due alert, which would have left healthy characters amber forever.

**Measuring it.** `_log_send` only ever runs on a real send, which left the feature's two defining
outcomes with no trace at all — and unlike the backoff rung (recoverable from send timestamps
whenever you care to look) they cannot be reconstructed after the fact. So `_log_rescan_outcomes`
writes them into `pp_notification_log` under statuses no send ever uses:

| `status` | Meaning |
|---|---|
| `prevented` | Due, re-read, found already fixed — never sent. **The benefit.** |
| `suppressed:no_token` | Held back: the character needs re-authorising |
| `suppressed:retry_brake` | Held back: a read failed recently and is not being retried yet |
| `suppressed:scan_failed` | Held back: this tick's read failed |
| `suppressed:over_budget` | Held back: more colonies were due than one tick will read |

Same table because every column already fits and every reader filters `status='ok'`
(`_recently_notified`, `_consecutive_cooldown_h`, `resend-last`); `/api/notifications/log` was the
one that did not and now excludes them via `_SENDS_ONLY_SQL`, because the user's log is a list of
things that were sent. Rows are deduped per cause per `_OUTCOME_LOG_WINDOW_H` — an unresolved
problem is due on all 96 ticks of the day, and 96 identical rows say nothing 1 does not.
`prevented` is deliberately not deduped: a problem found fixed does not recur next tick, so a second
row is a second real occurrence. To read it:

```sql
SELECT status, COUNT(*) FROM pp_notification_log
 WHERE sent_at > '2026-08-18' AND status <> 'ok' GROUP BY status ORDER BY 2 DESC;
```

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

## How-it-works poster + social banner

`static/how-it-works.svg` (9:16 five-step infographic) is the hero on the How-it-works page, opened
in an in-page dark lightbox (`openImageLightbox`/`closeImageLightbox` — generic, reusable) instead of
the bare white file URL. The social/OG preview `static/og-image.png` (1200×630) is the 3:1 banner
(`eve_pi_banner.svg`) centred on a matching dark canvas; the og:image `?v=` is stamped automatically alongside every other asset in
`index.html` AND the `/s/{id}` OG injection in `main.py` when it changes. SVG source posters live in
`~/Claude-Workspace/` (`eve_pi_planner.svg`, `eve_pi_banner.svg`); re-render the OG with cairosvg +
Pillow.
