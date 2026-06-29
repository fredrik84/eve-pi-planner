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

## Statelessness / Kubernetes readiness

**Goal:** Make the app runnable as multiple identical pods behind a load balancer.
Not needed today (single container on a VPS), but the architecture should not
actively resist it. Document what needs to change and in what order.

---

### Current statefulness inventory

Three categories of shared mutable state, ordered by blast radius:

**1. `_sessions` dict (esi.py) — BREAKING in multi-instance**

`_sessions: dict[str, tuple[int, int]]` is a module-level in-memory cache of
`token → (character_id, context_id)`. It is lazily loaded once per process
(`_sessions_loaded` flag) and then kept in sync by `_save_session` / `_delete_session`.

In a two-pod setup: pod A creates a session → pod B never learns about it → 50% of
requests get 401 randomly. This is the single most important thing to fix.

**Fix:** Remove the in-memory cache entirely. Replace every `_sessions.get(token)` with
a direct DB query `SELECT character_id, context_id FROM pp_sessions WHERE token=?`.
With an index on `token` (add one — currently missing), this is a single-row lookup
and fast enough for this app's traffic. No Redis needed.

`_save_session` / `_delete_session` / `_invalidate_context_sessions` become pure DB
operations. `_sessions_loaded` flag disappears.

**2. `_pending` dict (esi.py) — BREAKING in multi-instance**

`_pending: dict[str, str]` stores the OAuth `state` parameter between `/auth/login`
and `/auth/callback`. If login hits pod A and callback hits pod B, the state lookup
fails → 403 / broken OAuth flow.

**Fix:** Store the pending state in the DB instead of memory. New table:
```sql
CREATE TABLE pp_oauth_pending (
    state      TEXT PRIMARY KEY,
    context_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```
`/auth/login` inserts a row; `/auth/callback` reads and deletes it. Add a cleanup
job (or inline expiry check) for states older than 10 minutes.

**3. `_cache` dict (market.py) — DEGRADED but not breaking**

In-memory price cache with a TTL. In multi-pod, each pod has its own cold cache →
extra Fuzzwork requests on startup. Not broken (requests are idempotent) but
wasteful. Leave as-is for now; revisit if traffic warrants it. If the DB move
happens first, a `pp_price_cache` table with a `fetched_at` column would solve it.

---

### SQLite → PostgreSQL migration plan

SQLite is a single-writer, single-file DB. It cannot be shared across pods. The SDE
data (planet types, schematics, etc.) is read-only and could stay as SQLite on a
shared volume, but the user tables (`pp_*`) must move to Postgres.

**Scope:** ~108 `get_connection()` call sites across 10 app files. Not a one-day job
but not a rewrite either — the SQL is simple and the patterns are consistent.

**SQLite → Postgres translation table:**

| SQLite | Postgres |
|--------|----------|
| `datetime('now')` | `NOW()` |
| `datetime('now', '-90 days')` | `NOW() - INTERVAL '90 days'` |
| `CURRENT_TIMESTAMP` | `NOW()` |
| `INTEGER PRIMARY KEY` (autoincrement) | `SERIAL` or `BIGSERIAL` |
| `INSERT OR REPLACE INTO` | `INSERT INTO … ON CONFLICT … DO UPDATE SET` |
| `INSERT OR IGNORE INTO` | `INSERT INTO … ON CONFLICT DO NOTHING` |
| `PRAGMA table_info(t)` | `SELECT column_name FROM information_schema.columns WHERE table_name='t'` |
| `PRAGMA journal_mode=WAL` | not needed (Postgres handles concurrency natively) |
| `?` parameter placeholder | `%s` (psycopg2) |
| `con.row_factory = sqlite3.Row` | `psycopg2.extras.RealDictCursor` |

**Suggested migration phases:**

**Phase 0 (now → any time): fix in-memory state** (see above). Stateless-safe and
useful independent of the DB move.

**Phase 1: abstract the connection layer.** Replace `sqlite3.connect(...)` in
`app/sde.py:get_connection()` with a thin adapter that can be backed by either
SQLite or Postgres via an env var `DATABASE_URL`. Keep SQLite as the default so
nothing breaks. This is the enabler for all subsequent work — no call-site changes.

```python
# app/sde.py
import os
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # empty = SQLite

def get_connection():
    if DATABASE_URL.startswith("postgresql"):
        import psycopg2, psycopg2.extras
        con = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return con
    else:
        con = sqlite3.connect("data/sde.db")
        con.row_factory = sqlite3.Row
        return con
```

The adapter must also handle the placeholder difference (`?` vs `%s`). Options:
- Wrapper method `con.execute(sql, params)` that translates placeholders.
- Use SQLAlchemy Core (no ORM) — handles both dialects transparently. Adds a dep
  but removes the translation burden entirely. **Recommended for this app's size.**

**Phase 2: translate SQL syntax.** With the adapter in place, run the app against a
test Postgres instance and fix each failure. The 13 `datetime('now'...)` calls and
12 `INSERT OR REPLACE/IGNORE` calls are the bulk of the work. The 4 `PRAGMA` calls
are all in `ensure_*` migration helpers — replace with `information_schema` queries.

**Phase 3: split SDE vs user DB.** The SDE tables (`pi_schematics`, `pi_schematic_inputs`,
`types`, `constellations`, `system_geo`, `system_jumps`, `solar_systems`) are
populated by `scripts/populate_*.py` and are read-only at runtime. Keep these in
SQLite on a volume (or ship them baked into the image — they're ~5 MB). Move only
`pp_*` tables to Postgres. This avoids migrating the SDE pipeline and keeps image
builds simple.

**Phase 4: Kubernetes deployment.** At this point:
- User DB: Postgres (external managed service or a StatefulSet).
- SDE DB: SQLite file baked into the image (rebuild image on SDE update).
- Sessions: stateless (DB-backed after Phase 0).
- OAuth pending: DB-backed (after Phase 0).
- Market cache: per-pod in-memory (acceptable degradation).
- APScheduler (notifications): runs in ONE pod only. Use a `pp_scheduler_lock` table
  with a heartbeat row to elect a single scheduler leader. Other pods skip scheduling
  if the lock is held by a live heartbeat. Simple and dependency-free.

---

### Build order

1. **Fix `_sessions`** — remove in-memory cache, DB-only lookup. Test: two-process
   simulation (start app, write session directly to DB from a second process, verify
   the first process honours it).
2. **Fix `_pending`** — move OAuth state to DB table.
3. **Abstract `get_connection()`** — env-var switchable, placeholder translation.
4. **Postgres SQL translation** — iterate until test suite passes against Postgres.
5. **Split SDE / user DB** — bake SDE into image, point user tables at Postgres.
6. **Helm chart / K8s manifests** — deployment, service, ingress (Traefik is already
   the reverse proxy; the Helm chart just replaces docker-compose labels).
7. **Scheduler leader election** — implement only when notifications are shipped.

Steps 1–2 are valuable TODAY (remove a class of subtle bugs) independent of Kubernetes.
Steps 3–7 are a future project. Don't start step 3 without a dedicated session for it.

---

## Redis cache layer

**Goal:** Optional cache layer in front of the DB to reduce query load and enable
sub-millisecond auth checks across pods. Not needed today, but the architecture
should slot Redis in cleanly when the time comes.

**Design principle: Redis is optional.** The app must work correctly without it —
single-container deployments should not require Redis. Every read goes through a
cache-aside helper that falls back to the DB on a miss or when Redis is unavailable.
Redis is purely a performance layer, never the source of truth.

**Enabled by:** `REDIS_URL=redis://localhost:6379` env var. Absent → cache layer is
a no-op (all reads go straight to DB, all writes are no-ops).

---

### What to cache and why

**Tier 1 — high value, implement first**

| Key pattern | Content | TTL | Invalidation |
|-------------|---------|-----|-------------|
| `session:{token}` | `{character_id, context_id}` | 30 days (matches cookie) | Delete on logout / account deletion |
| `oauth_pending:{state}` | `context_id` | 10 min | Delete on callback (consumed once) |
| `market:{type_id}` | `{buy, sell}` prices | 1 hour | TTL only (prices change slowly) |

These three replace the two broken in-memory dicts (`_sessions`, `_pending`) and the
per-pod market cache — all described in the statelessness plan. With Redis, the
statelessness plan's "fix `_pending` with a DB table" becomes "fix `_pending` with
Redis" instead, which is cleaner (natural TTL, no cleanup job needed).

**Tier 2 — moderate value, add when DB load is measurable**

| Key pattern | Content | TTL | Invalidation |
|-------------|---------|-----|-------------|
| `features` | Full feature flag dict | 60s | Delete on any flag toggle |
| `admin_stats` | All 20 stat counters | 60s | TTL only (admin page, stale is fine) |
| `pi_data` | SDE schematics + type names | 24h | Delete on SDE rebuild |
| `planet_constellations` | `GET /api/constellations` response | 10 min | Delete on planet import |

`pi_data` is the most valuable: it's loaded from SQLite on every plan run and is
large (~hundreds of rows). A 24h cache cuts a lot of redundant SQLite reads in a
busy multi-pod setup.

**Tier 3 — low value, probably never needed**

| Key | Content | Notes |
|-----|---------|-------|
| `plan:{hash}` | Full plan result | Invalidation is complex (planet DB changes, config changes). Not worth it. |
| `char_planets:{context_id}` | Colony scan data | Changes on every rescan. Cache churn would exceed benefit. |

---

### Implementation pattern

A single thin module `app/cache.py`:

```python
import os, json, functools

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None

def _get_redis():
    global _redis
    if _redis is None and REDIS_URL:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis

def cache_get(key: str):
    r = _get_redis()
    if not r:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val is not None else None
    except Exception:
        return None  # Redis down → cache miss, never crash

def cache_set(key: str, value, ttl_seconds: int):
    r = _get_redis()
    if not r:
        return
    try:
        r.setex(key, ttl_seconds, json.dumps(value))
    except Exception:
        pass

def cache_delete(key: str):
    r = _get_redis()
    if not r:
        return
    try:
        r.delete(key)
    except Exception:
        pass

def cache_delete_prefix(prefix: str):
    """Delete all keys matching prefix:* — use sparingly (SCAN, not KEYS)."""
    r = _get_redis()
    if not r:
        return
    try:
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor, match=f"{prefix}:*", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
```

All errors are swallowed — a Redis outage degrades to DB-only, never causes a 500.
`cache_delete_prefix` uses `SCAN` (not `KEYS`) so it never blocks the Redis event
loop on large keyspaces.

---

### Interaction with the statelessness plan

With Redis available, Phase 0 of the statelessness plan changes slightly:

| Without Redis | With Redis |
|---------------|------------|
| `_sessions` → DB lookup on every request | `_sessions` → `cache_get("session:{token}")`, miss → DB → `cache_set` |
| `_pending` → `pp_oauth_pending` DB table | `_pending` → `cache_set("oauth_pending:{state}", ctx, ttl=600)` |

The DB remains the source of truth for sessions. Redis is a read-through cache in
front of it. This means the DB fix (Phase 0) should land first regardless — Redis
then slots in as an optimisation on top of already-correct DB-backed behaviour.

---

### Dependencies

- `redis` (pip) — lazy import, only needed if `REDIS_URL` is set. Add to
  `requirements.txt` unconditionally (tiny package, no harm installed unused).
- No Redis server needed for local dev / single-container deployments.
- For Docker Compose multi-instance: add a `redis:alpine` service and set
  `REDIS_URL=redis://redis:6379` in `.env`.
- For Kubernetes: managed Redis (e.g. Upstash, Redis Cloud, or a simple
  `redis:alpine` Deployment with a ClusterIP service).

---

### Build order

1. Write `app/cache.py` (the module above — pure utility, no side effects).
2. Wire `session:{token}` cache into `require_context` / `_save_session` /
   `_delete_session` / `_invalidate_context_sessions` (after Phase 0 of
   statelessness plan is done — build on top of correct DB-backed behaviour).
3. Wire `oauth_pending:{state}` (replaces the `pp_oauth_pending` table approach).
4. Wire `market:{type_id}` (drop `_cache` dict in `market.py`).
5. Wire Tier 2 keys as DB load becomes measurable.

Do NOT build this before the statelessness Phase 0 fixes. Redis on top of broken
in-memory state makes debugging much harder.

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
