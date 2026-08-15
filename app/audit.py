"""A record of who destroyed what.

On 2026-08-15 production's `pp_planets` was found empty — 5,302 planets, the shared reference data
the whole PI planner runs on, gone. Reconstructing what happened took reading eight nightly dumps to
bracket the window to a day, then git archaeology to find the change that exposed the button. The
database itself could not answer the only question that mattered: **who pressed it, and when.**

That is what this fixes. A destructive action writes one row here before it is answered as a
success. It is deliberately thin — an audit trail nobody reads is still worth writing, and one that
is expensive to write is one somebody will skip.

**What belongs here:** anything that removes data another user can see, and anything that removes a
lot of one user's data at once. Not ordinary edits, not a single row a user just created and
deleted — the signal dies in that noise.

**It never blocks the action.** A failure to record is logged and swallowed: an audit table having
a bad day must not turn a working delete into a 500. That trade is deliberate and is the reason the
write goes *after* the delete has succeeded rather than in the same transaction.
"""
from __future__ import annotations

import logging
import time

from app.db import get_connection
from app.sde import ensure_once

log = logging.getLogger(__name__)

# Kept out of the audit table itself: the actions worth recording are a short, curated list, and
# naming them in one place means a grep tells you what is covered rather than what happened to have
# been called so far.
GLOBAL_SCOPE = "global"          # affects every user of the service
ACCOUNT_SCOPE = "account"        # affects one account, but wholesale


@ensure_once
def ensure_audit_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_audit_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                at            REAL    NOT NULL,
                action        TEXT    NOT NULL,
                scope         TEXT    NOT NULL,
                context_id    INTEGER,
                character_name TEXT,
                target        TEXT,
                affected      INTEGER,
                detail        TEXT
            )
        """)
        # The page reads newest-first and nothing else, so one index is the whole access pattern.
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON pp_audit_log (at DESC)")
        con.commit()
    finally:
        con.close()


def _actor_name(context_id: int | None) -> str:
    """A name for the row, resolved once at write time.

    Deliberately DENORMALISED. The point of an audit row is to still make sense after the account it
    describes is gone — and account deletion is itself one of the things recorded here, so a join
    would erase exactly the entries most worth keeping.
    """
    if not context_id:
        return ""
    try:
        con = get_connection()
        try:
            row = con.execute(
                "SELECT character_name FROM pp_characters WHERE context_id=? "
                "ORDER BY character_id LIMIT 1", (context_id,)).fetchone()
            return (row["character_name"] if row else "") or ""
        finally:
            con.close()
    except Exception:
        return ""


def record(action: str, *, scope: str = ACCOUNT_SCOPE, context_id: int | None = None,
           target: str = "", affected: int = 0, detail: str = "") -> None:
    """Record one destructive action. Never raises."""
    try:
        ensure_audit_table()
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_audit_log (at, action, scope, context_id, character_name, "
                "target, affected, detail) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), action, scope, context_id, _actor_name(context_id),
                 target, int(affected or 0), detail))
            con.commit()
        finally:
            con.close()
    except Exception:
        log.exception("audit record failed: action=%s target=%s", action, target)
    # Logged as well as stored: the table can be lost or truncated, and the pod logs are a second,
    # independent copy of the one fact that was expensive to reconstruct without it.
    if scope == GLOBAL_SCOPE:
        log.warning("AUDIT global destructive action: %s target=%s affected=%s ctx=%s",
                    action, target, affected, context_id)


SECURITY_SCOPE = "security"      # somebody was refused something

# A scanner hitting a hundred protected paths a second must not become a hundred rows a second, and
# a bored user clicking a forbidden link twice is one event, not two. Same (action, target, actor)
# inside this window is dropped. In-process and per-replica, which is fine: the point is to stop a
# flood, not to achieve exactly-once across pods.
_DEDUPE_SECONDS = 300
_recent_denials: dict[tuple, float] = {}


def record_denied(action: str, *, context_id: int | None = None, target: str = "",
                  detail: str = "") -> None:
    """Record a refused attempt: a non-admin reaching an admin endpoint, a restricted group
    reaching a page it may not, a logged-in session probing a path that does not exist.

    **Only ever called for a request that was already refused** — this is a record of the gate
    working, not a gate itself. Anonymous traffic is deliberately excluded by the callers: the
    internet scans everything constantly, and a log of that is noise that buries the signal, which
    is a KNOWN user doing something they should not.
    """
    key = (action, target, context_id)
    now = time.time()
    last = _recent_denials.get(key, 0.0)
    if now - last < _DEDUPE_SECONDS:
        return
    _recent_denials[key] = now
    if len(_recent_denials) > 5000:                     # bounded; oldest half discarded
        for k in sorted(_recent_denials, key=_recent_denials.get)[:2500]:
            _recent_denials.pop(k, None)
    record(action, scope=SECURITY_SCOPE, context_id=context_id, target=target, detail=detail)


def recent(limit: int = 200) -> list[dict]:
    """Newest first. Read by the Admin → Audit page."""
    ensure_audit_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, at, action, scope, context_id, character_name, target, affected, detail "
            "FROM pp_audit_log ORDER BY at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
