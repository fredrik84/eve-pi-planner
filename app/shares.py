"""Inventory shares for the Find-Buildables analyzer (`app/analyzer.py`) — the "Copy link"
button, which stores a pasted inventory behind a short id.

NOTE this is the ORIGINAL share mechanism and is unrelated to the plan shares in `pp_shares`
that `/s/{id}` serves. Similar names, different features.

These used to live in their own SQLite file (`data/shares.db`) opened directly, which was fine
on one box and quietly broken in production: the file sits in the container filesystem with no
volume behind it, so with two replicas a link saved on one pod 404'd on the other (measured: a
clean 404/200 alternation), and every deploy threw all of them away. It now uses the shared
`get_connection()` like every other table, so a share is a share whichever pod answers.
"""
import secrets

from app.sde import get_connection, ensure_once


@ensure_once
def ensure_inventory_shares_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_inventory_shares (
                id          TEXT PRIMARY KEY,
                inventory   TEXT NOT NULL,
                created_at  TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


def save_share(inventory: str) -> str:
    ensure_inventory_shares_table()
    share_id = secrets.token_urlsafe(6)  # 8-char URL-safe string
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_inventory_shares (id, inventory, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (share_id, inventory),
        )
        con.commit()
    finally:
        con.close()
    return share_id


def load_share(share_id: str) -> str | None:
    ensure_inventory_shares_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT inventory FROM pp_inventory_shares WHERE id = ?", (share_id,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None
