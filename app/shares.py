import secrets
import sqlite3
from pathlib import Path

SHARES_DB = Path("data/shares.db")


def _conn() -> sqlite3.Connection:
    SHARES_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(SHARES_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS shares (
            id          TEXT PRIMARY KEY,
            inventory   TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    return con


def save_share(inventory: str) -> str:
    share_id = secrets.token_urlsafe(6)  # 8-char URL-safe string
    con = _conn()
    con.execute("INSERT INTO shares (id, inventory) VALUES (?, ?)", (share_id, inventory))
    con.commit()
    con.close()
    return share_id


def load_share(share_id: str) -> str | None:
    con = _conn()
    row = con.execute("SELECT inventory FROM shares WHERE id = ?", (share_id,)).fetchone()
    con.close()
    return row[0] if row else None
