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
