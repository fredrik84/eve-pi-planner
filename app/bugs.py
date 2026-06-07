"""Bug reporting: any logged-in pilot can submit a report; admins (see
`esi.ADMIN_CHARACTERS`) can list reports and mark them complete/ignored.
Stored in the `pp_bugs` SQLite table."""

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.esi import require_context, require_admin, _sessions, _load_sessions

router = APIRouter()

_VALID_STATUS = {"open", "complete", "ignored"}


def ensure_bugs_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_bugs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id     INTEGER,
            character_id   INTEGER,
            character_name TEXT,
            title          TEXT NOT NULL,
            description    TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'open',
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()


class BugReport(BaseModel):
    title: str
    description: str


class BugStatusUpdate(BaseModel):
    status: str


@router.post("/api/bugs")
def submit_bug(req: BugReport, pp_session: str = Cookie(default=None)):
    context_id = require_context(pp_session)   # 401 if not logged in
    _load_sessions()
    char_id = _sessions[pp_session][0]

    title = (req.title or "").strip()[:200]
    desc = (req.description or "").strip()[:5000]
    if not title or not desc:
        raise HTTPException(status_code=400, detail="Title and description are required")

    ensure_bugs_table()
    con = get_connection()
    row = con.execute(
        "SELECT character_name FROM pp_characters WHERE character_id=?", (char_id,)
    ).fetchone()
    name = row["character_name"] if row else str(char_id)
    con.execute(
        "INSERT INTO pp_bugs (context_id, character_id, character_name, title, description) "
        "VALUES (?,?,?,?,?)",
        (context_id, char_id, name, title, desc),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.get("/api/bugs")
def list_bugs(status: str | None = None, _: int = Depends(require_admin)):
    ensure_bugs_table()
    con = get_connection()
    if status in _VALID_STATUS:
        rows = con.execute(
            "SELECT * FROM pp_bugs WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        # Open first, then most recent.
        rows = con.execute(
            "SELECT * FROM pp_bugs "
            "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC"
        ).fetchall()
    counts = dict(con.execute(
        "SELECT status, COUNT(*) c FROM pp_bugs GROUP BY status"
    ).fetchall() or [])
    con.close()
    return {"bugs": [dict(r) for r in rows], "counts": counts}


@router.post("/api/bugs/{bug_id}/status")
def set_bug_status(bug_id: int, req: BugStatusUpdate, _: int = Depends(require_admin)):
    if req.status not in _VALID_STATUS:
        raise HTTPException(status_code=400, detail="Invalid status")
    ensure_bugs_table()
    con = get_connection()
    cur = con.execute(
        "UPDATE pp_bugs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req.status, bug_id),
    )
    con.commit()
    changed = cur.rowcount
    con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"ok": True}
