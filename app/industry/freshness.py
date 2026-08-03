"""Keep the Industry tab's caches current without asking the user to remember.

Three caches feed this tab — running jobs, owned blueprints, and asset stock — and each one used to
be refreshed by pressing a button. That makes staleness the user's job, and the failure mode is the
bad one: forget, and the plan is quietly wrong. A stale job cache under-reports what's running (so
free slots are overstated), stale assets tell you to buy what's already in your hangar, and stale
blueprints cost you your real ME/TE. None of that announces itself.

So the tab refreshes what has gone stale when you open it, and the buttons stay for "do it now".

**Each cache gets its own threshold, because the calls are not alike.** Jobs change hour to hour and
are cheap to read. A full asset list is a heavy, paginated call per character — worth doing on the
timescale someone hauls materials around, not on every visit. Blueprints barely change at all.

**Never more than one attempt per `_MIN_GAP`, whatever the outcome.** Age only moves when a fetch
SUCCEEDS, so a character that will always fail (revoked token, missing scope) would otherwise be
retried on every single tab open forever. The attempt stamp is what stops that, and it is why this
records the attempt rather than the result.

This adds no polling: nothing here runs unless the tab is opened.
"""

from __future__ import annotations

import time

from fastapi import Depends

from app.db import get_connection
from app.esi import require_context
from app.industry._router import router

# Seconds before a cache is considered stale. Set per cache from what it costs and how fast the
# underlying truth moves — not one global number, which would be wrong for all three.
_THRESHOLDS = {
    "jobs": 15 * 60,          # cheap, and what's running changes through the day
    "assets": 60 * 60,        # heavy paginated call; matches the pace of actually hauling things
    "blueprints": 24 * 3600,  # research and ownership barely move
}

# Floor between automatic attempts for one account, regardless of what happened last time.
_MIN_GAP = 10 * 60


def _min_age(sql: str, ctx: int) -> float | None:
    """Seconds since the OLDEST row for this account (the account is only as fresh as its stalest
    character), or None when there's nothing cached yet."""
    con = get_connection()
    try:
        row = con.execute(sql, (ctx,)).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    t = row and row[0]
    return None if not t else max(0.0, time.time() - float(t))


def cache_ages(ctx: int) -> dict[str, float | None]:
    """Age in seconds of each cache. None = never fetched, which is NOT treated as stale: an
    account that has never connected a character shouldn't have a refresh attempted on its behalf
    every time it opens the tab."""
    return {
        "jobs": _min_age(
            "SELECT MIN(j.fetched_at) FROM pp_char_manufacturing_jobs j "
            "JOIN pp_characters c ON c.character_id = j.character_id WHERE c.context_id = ?", ctx),
        "blueprints": _min_age(
            "SELECT MIN(b.fetched_at) FROM pp_char_blueprints b "
            "JOIN pp_characters c ON c.character_id = b.character_id WHERE c.context_id = ?", ctx),
        "assets": _min_age(
            "SELECT MAX(updated_at) FROM pp_asset_sources WHERE context_id = ?", ctx),
    }


def stale_kinds(ctx: int) -> list[str]:
    ages = cache_ages(ctx)
    return [k for k, age in ages.items() if age is not None and age > _THRESHOLDS[k]]


def _last_attempt(ctx: int) -> float:
    from app.industry.settings import get_settings
    return float(get_settings(ctx).get("auto_refreshed_at") or 0.0)


def _stamp_attempt(ctx: int) -> None:
    from app.industry.settings import ensure_industry_settings_table
    ensure_industry_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, auto_refreshed_at) VALUES (?,?) "
            "ON CONFLICT(context_id) DO UPDATE SET auto_refreshed_at=excluded.auto_refreshed_at",
            (ctx, time.time()))
        con.commit()
    finally:
        con.close()


def refresh_stale(ctx: int) -> dict:
    """Refresh only the caches past their own threshold. Safe to call on every tab open."""
    if time.time() - _last_attempt(ctx) < _MIN_GAP:
        return {"refreshed": [], "throttled": True, "ages": cache_ages(ctx)}
    kinds = stale_kinds(ctx)
    if not kinds:
        return {"refreshed": [], "throttled": False, "ages": cache_ages(ctx)}

    _stamp_attempt(ctx)      # stamped BEFORE the work: a failing fetch must not be retried on loop
    done: list[str] = []
    for kind in kinds:
        try:
            if kind == "jobs":
                from app.industry.jobs import refresh_manufacturing_jobs
                refresh_manufacturing_jobs(ctx)
            elif kind == "blueprints":
                from app.industry.blueprints import refresh_blueprints
                refresh_blueprints(ctx)
            elif kind == "assets":
                from app.industry.assets import refresh_assets
                refresh_assets(ctx)
            done.append(kind)
        except Exception:
            continue          # one cache failing must never stop the others
    return {"refreshed": done, "throttled": False, "ages": cache_ages(ctx)}


@router.post("/api/industry/refresh-stale")
def industry_refresh_stale(ctx: int = Depends(require_context)):
    """Bring whatever has gone stale up to date. Called when the tab opens; does nothing when
    everything is current, when it ran within the last few minutes, or when there's nothing cached
    to refresh in the first place."""
    return refresh_stale(ctx)
