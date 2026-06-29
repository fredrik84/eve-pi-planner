# Planned Features

Backlog of features planned but not yet implemented. Each section has enough
detail to be picked up in a fresh session. Mark items `[x]` when shipped.

---

## Admin: System Stats page

**Goal:** Give the admin a live read-only dashboard of the system health / usage
without having to query the DB manually.

**New Admin sub-page:** "System stats" (add button to `#adminSubnav`,
`data-page="stats"`).

**New endpoint:** `GET /api/admin/stats` (admin-gated).

Returns a single JSON object with:

```json
{
  "users": {
    "contexts": 12,
    "active_7d": 4,
    "active_30d": 8,
    "characters": 128,
    "dummy_characters": 3
  },
  "planet_db": {
    "planet_rows": 5141,
    "systems_covered": 420,
    "constellations_covered": 38,
    "pending_submissions": 0
  },
  "usage": {
    "profiles": 5,
    "shares": 37,
    "shares_7d": 6,
    "shares_30d": 12,
    "bugs_open": 1,
    "bugs_total": 1
  },
  "data": {
    "char_planet_scans": 326,
    "colony_yield_rows": 1338,
    "sessions": 100
  }
}
```

"Active" = has a session row with `created_at > now - N days`
(sessions are created on login; serves as a proxy for recent activity).

**Frontend:** simple stat tiles, same `.an-stats` grid style as the rest of the app.
No names, no per-user breakdown — aggregate only.

---

## Admin: DB Cleanup tool

**Goal:** Safe, admin-supervised cleanup of stale/orphaned data. Never auto-runs.
Preview first, then confirm.

**New Admin sub-page:** "Cleanup" (`data-page="cleanup"`).

**New endpoints:**

- `GET /api/admin/cleanup/preview` — returns counts per category, no writes
- `POST /api/admin/cleanup` — body `{categories: [...]}`, deletes selected, returns counts deleted

**Categories (each independently selectable via checkboxes):**

| key | What | Condition |
|-----|------|-----------|
| `old_sessions` | Auth session tokens | `created_at < now - 90 days` |
| `old_shares` | Shared plan links | `last_accessed < now - 90 days` (see shares note below) |
| `empty_contexts` | User contexts with zero characters | `NOT EXISTS (SELECT 1 FROM pp_characters WHERE context_id=...)` |
| `orphaned_plan_config` | Per-character config rows for deleted characters | `character_id NOT IN pp_characters` |
| `orphaned_colony_yield` | Yield history for deleted characters | `character_id NOT IN pp_characters` |

**What is never touched by cleanup:**
- `pp_planets` — global, no expiry
- `pp_char_planets` — user scan data; deleted only via account deletion (see below)
- `pp_profiles` — user's saved plans
- `pp_characters` with any data attached

**UI flow:**
1. Admin opens Cleanup sub-page → auto-loads preview (counts per category)
2. Each category shows its count and a checkbox (pre-checked if count > 0)
3. "Run cleanup" button → confirm dialog listing what will be deleted → POST → show
   counts deleted per category → reload preview (should all be 0)

---

## Shares: last_accessed tracking

**Goal:** Track when a share link was last loaded so cleanup can target truly
unused links rather than just old ones.

**Migration:** `ALTER TABLE pp_shares ADD COLUMN last_accessed TEXT` (ISO timestamp,
nullable — NULL means never accessed after migration, treat as created_at for
cleanup purposes).

**Where to update:** `GET /s/{id}` in `main.py` (the OG-meta route that serves the
SPA for a share link) and `GET /api/pp-shares/{id}` in `planner.py` (the API call
that fetches the payload). Both should `UPDATE pp_shares SET last_accessed=now WHERE id=?`
on every hit. This is a fire-and-forget update (don't let a DB error block the response).

**Cleanup condition change:** once this is live, the cleanup category `old_shares`
switches from `created_at < now - 90d` to
`COALESCE(last_accessed, created_at) < now - 90d`.

**Stats endpoint:** add `shares_accessed_never` count (last_accessed IS NULL and
created_at < now - 30d) to help assess how many links were generated but never opened.

---

## User: Account self-deletion ("Delete my data")

**Goal:** Any logged-in user can permanently delete all their data from the system.
Builds trust — users can see there's no lock-in and their data isn't kept silently.

**New endpoint:** `DELETE /api/me` (session-gated via `require_context`).

Deletes in dependency order (FK-safe, all in one transaction):

1. `pp_plan_config` where `character_id IN (chars of this context)`
2. `pp_colony_yield` where `character_id IN (chars of this context)`
3. `pp_char_planets` where `character_id IN (chars of this context)`
4. `pp_plan_snapshots` where `context_id = this context`
5. `pp_plan_baseline` where `context_id = this context`
6. `pp_profiles` where `context_id = this context`
7. `pp_baskets` where `context_id = this context` (private baskets only)
8. `pp_basket_items` for those baskets
9. `pp_characters` where `context_id = this context`
10. `pp_sessions` where `context_id = this context` (logs out all devices)
11. `pp_user_contexts` where `id = this context`

Does NOT delete:
- `pp_shares` — shares are anonymous (anon shares have no names; full shares are
  the user's choice to publish). Could add a flag later; skip for v1.
- `pp_planets` — global shared DB, not per-user.
- `pp_bugs` — filed reports (de-linked from the user, just lose the reporter context).

After deletion the response is `{deleted: true}`. The frontend should immediately
clear the session cookie and redirect to `/` (same as logout).

**UI placement:** a new "Account" section at the bottom of the **Characters tab**
(visible only when logged in). Keep it low-key — a small "Delete my account" link
that opens a confirm modal. Modal copy:

> "This will permanently delete all your characters, saved plans, profiles, and
> scan data. Shared links you created will remain (they contain no character names
> if you used the default anonymous share). This cannot be undone."

Two-step confirm: type "DELETE" into a text input before the button activates
(prevents fat-finger). On confirm, call `DELETE /api/me`, then redirect to `/`.

**Test:** add to `test_features.py` — create a context, add some data, call
`DELETE /api/me`, verify all rows gone, verify subsequent authenticated calls 401.

---

## Notes / dependencies

- Stats and Cleanup are independent of each other and of Account deletion.
- Shares `last_accessed` migration should land **before** Cleanup is shipped, so
  the 90-day window has time to populate. Or ship Cleanup first with
  `created_at`-based logic and swap the condition after.
- Account deletion endpoint must be tested before UI is wired — a misfire here
  is not recoverable.

---

## [CONSIDERING] Public API access for user data

**Status: undecided.** Owner hasn't decided whether to build this. Document trade-offs
before committing.

**Goal:** Let users pull their own plan/colony data programmatically (spreadsheet tools,
personal dashboards, third-party EVE tools).

**Trade-offs:**

| Pro | Con |
|-----|-----|
| Power users can automate workflows | Increases DB + webserver load (tools poll aggressively) |
| Builds ecosystem goodwill | Harder to revoke once published |
| Reduces manual copy-paste | Rate-limiting infrastructure needed |
| | Auth design non-trivial (tokens vs session cookies) |

**If built, non-negotiable constraints (guideline 7 + 8):**
- Must be per-user scoped — a token only accesses the issuing account's data.
- No endpoint may return data across users, even aggregate.
- Tokens are user-generated and user-revokable from the settings page.
- No third party may receive the data; the API is for the user themselves.
- Rate-limited from day one (e.g. 60 req/min per token).

**Suggested scope for v1 (read-only):**
- `GET /api/v1/characters` — own characters + skills
- `GET /api/v1/colonies` — own colony scan data
- `GET /api/v1/plans` — saved profiles

**Decision needed:** is the load increase acceptable? Could mitigate with aggressive
caching (colony data only changes on rescan anyway) or a separate read replica.
Do not start implementation without explicit go-ahead.

---

## Notification support

**Goal:** Alert users when their PI needs attention — extractors expiring, factory
pads near-empty — without requiring them to log in and check manually.

**Supported channels (v1):** Pushover, ntfy.sh, Discord webhook. Designed to add
more without touching existing channels.

---

### Data model

New table `pp_notification_settings`:
```sql
CREATE TABLE pp_notification_settings (
    id          INTEGER PRIMARY KEY,
    context_id  INTEGER NOT NULL,
    channel     TEXT NOT NULL,        -- 'pushover' | 'ntfy' | 'discord'
    config      TEXT NOT NULL,        -- JSON, channel-specific (see below)
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
```

Per-channel `config` shape:
- **Pushover:** `{"user_key": "...", "app_token": "..."}` — user supplies both; the
  app token is a single registered Pushover app key (one app key for the service,
  stored in env `PUSHOVER_APP_TOKEN`; user supplies only their user key).
- **ntfy.sh:** `{"topic": "your-secret-topic", "server": "https://ntfy.sh"}` — server
  is optional (defaults to ntfy.sh, allows self-hosted).
- **Discord:** `{"webhook_url": "https://discord.com/api/webhooks/..."}` — user
  pastes their channel webhook URL.

`config` is stored as-is (no encryption in v1 — SQLite is local, single-tenant
enough). Add a note in the UI that tokens are stored server-side.

New table `pp_notification_log`:
```sql
CREATE TABLE pp_notification_log (
    id          INTEGER PRIMARY KEY,
    context_id  INTEGER NOT NULL,
    channel     TEXT NOT NULL,
    event       TEXT NOT NULL,        -- 'extractor_expiry' | 'factory_refill'
    character   TEXT,
    sent_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    status      TEXT                  -- 'ok' | 'error: ...'
);
```
Used to suppress duplicate alerts (don't re-notify within a cooldown window) and
shown in the settings page so the user can see what was sent.

---

### Notification events

**1. Extractor program expiry** (`extractor_expiry`)
- Source: `pp_colony_yield.install_ts + prog_days * 86400` = expiry timestamp.
- Trigger: expiry is within the next N hours (user-configurable, default 4h).
- Cooldown: don't re-notify the same planet within 2h.
- Message: "3 extractors expire in ~2h — ekaoni · L7-RDZ"

**2. Factory refill due** (`factory_refill`)
- Source: `pp_char_planets` (factory planets) + `pp_plan_snapshots` (refill cadence
  `factory_refill_hours` from the most recent saved plan for this context).
- Trigger: `last_scanned_at + factory_refill_hours` is within N hours.
- Caveat: scan time is not production time; this is an estimate. State clearly in
  the notification that it's approximate.
- Cooldown: don't re-notify the same factory planet within 4h.
- Message: "Factory pads due for refill in ~3h — ekaoni · 0-U2M4"

---

### Background job

**Approach:** APScheduler embedded in FastAPI (no external queue, no extra
containers). Runs `check_and_send_notifications()` every 15 minutes.

```python
# app/notifications.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
scheduler = AsyncIOScheduler()
scheduler.add_job(check_and_send_notifications, 'interval', minutes=15)
```

Started in `main.py` `startup` event, shut down in `shutdown` event.

`check_and_send_notifications()`:
1. Load all contexts with at least one enabled notification setting.
2. For each context, compute upcoming events (no ESI calls — pure DB math).
3. For each event not already logged within its cooldown, send via the configured
   channel(s) and write to `pp_notification_log`.

**No rescanning required.** Extractor expiry is computed from `install_ts + prog_days`
already in `pp_colony_yield`. Factory timing uses the last scan timestamp as a
proxy. This means notifications go slightly stale between rescans, but avoids
hammering ESI on a 15-minute loop.

---

### Channel abstraction

```python
# app/notifiers.py
class BaseNotifier:
    def send(self, title: str, body: str, url: str | None = None) -> None:
        raise NotImplementedError

class PushoverNotifier(BaseNotifier): ...
class NtfyNotifier(BaseNotifier): ...
class DiscordNotifier(BaseNotifier): ...

def make_notifier(channel: str, config: dict) -> BaseNotifier:
    return {"pushover": PushoverNotifier, "ntfy": NtfyNotifier,
            "discord": DiscordNotifier}[channel](config)
```

Adding a new channel = one new `BaseNotifier` subclass + one entry in `make_notifier`.
No changes to the scheduler or event logic.

---

### Settings UI

New **Settings** tab (or section within Characters tab — decide at build time).
Sections:
- **Notification channels** — add/test/remove channels; per-channel config form;
  "Send test notification" button (`POST /api/notifications/test`).
- **Notification preferences** — per-event toggles + lead-time input (how many
  hours ahead to warn).
- **Notification log** — last 20 sent notifications (event, channel, time, status).

Endpoints:
- `GET/POST/DELETE /api/notifications/settings` (session-gated)
- `POST /api/notifications/test` — send a test message to a channel immediately
- `GET /api/notifications/log` — recent send history for this context

---

### Dependencies

- `apscheduler` (pip) for the background scheduler.
- Each channel uses only stdlib `urllib` (no `requests` dependency) to keep the
  image small.
- No new env vars required for ntfy/Discord. Pushover optionally uses
  `PUSHOVER_APP_TOKEN` if the operator registers a shared app token; otherwise
  the user supplies their own app token in the config JSON.

---

### Build order

1. `app/notifiers.py` + channel implementations (testable in isolation)
2. DB tables + `app/notifications.py` scheduler + event logic
3. `POST /api/notifications/test` endpoint (validates channels before wiring the scheduler)
4. Scheduler wired into `main.py` startup
5. Settings UI (channels + preferences + log)
6. Feature-flagged (`notifications`, default off)
