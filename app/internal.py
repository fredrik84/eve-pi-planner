"""Internal API endpoints — gated by X-Internal-Token, not public."""
import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Header, HTTPException

router = APIRouter()

_INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")


def _check_token(x_internal_token: str) -> None:
    if not _INTERNAL_TOKEN or x_internal_token != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/api/internal/recent-donations")
def recent_donations(
    since_hours: float = 1.1,
    x_internal_token: str = Header(default=""),
):
    """Return corp wallet donations from the last `since_hours` hours.

    Finds the first context with a wallet-scoped character.
    Returns {connected, donations:[{date, amount, donor, reason}]}.
    """
    _check_token(x_internal_token)

    from app.db import get_connection
    from app.esi import WALLET_SCOPE
    from app.esi_data import corp_wallet_summary

    con = get_connection()
    row = con.execute(
        "SELECT context_id FROM pp_characters WHERE scopes LIKE ? LIMIT 1",
        (f"%{WALLET_SCOPE}%",),
    ).fetchone()
    con.close()

    if not row:
        return {"connected": False, "donations": []}

    summary = corp_wallet_summary(row["context_id"])
    if not summary.get("connected") or summary.get("error"):
        return {"connected": False, "donations": [], "error": summary.get("error")}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    recent = []
    for d in summary.get("donations", []):
        try:
            dt = datetime.fromisoformat(d["date"].replace("Z", "+00:00"))
            if dt >= cutoff:
                recent.append(d)
        except Exception:
            pass

    return {
        "connected": True,
        "corp_name": summary.get("corp_name"),
        "donations": recent,
    }
