"""Per-character ESI snapshot caching.

Several industry features work the same way: for every character on the account that granted a
particular scope, pull a list from ESI and stash it as JSON in a one-row-per-character table, so
the planner can read it without touching ESI on every request (blueprints, manufacturing jobs).

The two refresh endpoints that do this were line-for-line identical apart from the scope, the
fetcher, and which table/column to write — enough duplication that a fix to the skip accounting or
the upsert had to be made twice. This is that loop, once.
"""
from app.sde import get_connection
from app.esi import _get_valid_token

import json as _json
import time as _time


def refresh_character_cache(context_id: int, *, scope: str, table: str, column: str, fetch) -> dict:
    """Re-read `fetch(character_id, token)` for every scoped character and cache the result.

    Best-effort per character: a missing scope, an unusable token or a failed fetch increments
    `skipped` and moves on, so one bad character never blocks the rest of the account. `fetch`
    returning None means "the call failed" — distinct from an empty list, which is a valid answer
    (a character genuinely owning no blueprints) and IS cached.

    Returns {"refreshed": n, "skipped": n}.
    """
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchall()
        refreshed = skipped = 0
        for c in chars:
            if scope not in (c["scopes"] or ""):
                skipped += 1
                continue
            tok = _get_valid_token(c["character_id"])
            if not tok:
                skipped += 1
                continue
            data = fetch(c["character_id"], tok)
            if data is None:
                skipped += 1
                continue
            con.execute(
                f"INSERT INTO {table} (character_id, {column}, fetched_at) "
                f"VALUES (?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
                f"{column}=excluded.{column}, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(data), _time.time()),
            )
            refreshed += 1
        con.commit()
    finally:
        con.close()
    return {"refreshed": refreshed, "skipped": skipped}
