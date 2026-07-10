"""Per-account thresholds for the Dashboard's colony alerts (extractor expiry window,
storage-fill warning/severity). Defaults match what planner.py's dashboard() hardcoded before
this existed, so nothing changes in behavior until a user customizes their own thresholds.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.esi import require_context

router = APIRouter()

DEFAULTS = {
    "expiring_hours": 3.0,          # extraction cycles ending within this window count as "expiring soon"
    "storage_warn_pct": 80.0,       # launchpad fill % that starts surfacing a "storage filling up" warning
    "storage_high_pct": 95.0,       # fill % that escalates a pad to "high" severity
    "storage_high_ttf_hours": 2.0,  # time-to-full that escalates a pad to "high" severity
    "storage_urgent_hours": 3.0,    # time-to-full counted in the "(N within Xh)" header
}


@ensure_once
def ensure_alert_settings_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_alert_settings (
            context_id             INTEGER PRIMARY KEY,
            expiring_hours         REAL DEFAULT 3,
            storage_warn_pct       REAL DEFAULT 80,
            storage_high_pct       REAL DEFAULT 95,
            storage_high_ttf_hours REAL DEFAULT 2,
            storage_urgent_hours   REAL DEFAULT 3
        )
    """)
    con.commit()
    con.close()


def get_alert_settings(context_id: int | None) -> dict:
    """Effective thresholds for this account — the saved row if customized, else the defaults
    above. Used both by dashboard()'s alert computation and the settings GET endpoint, so the
    two can never drift."""
    if not context_id:
        return dict(DEFAULTS)
    ensure_alert_settings_table()
    con = get_connection()
    row = con.execute(
        "SELECT expiring_hours, storage_warn_pct, storage_high_pct, storage_high_ttf_hours, "
        "storage_urgent_hours FROM pp_alert_settings WHERE context_id=?",
        (context_id,),
    ).fetchone()
    con.close()
    if not row:
        return dict(DEFAULTS)
    return {
        "expiring_hours": row["expiring_hours"],
        "storage_warn_pct": row["storage_warn_pct"],
        "storage_high_pct": row["storage_high_pct"],
        "storage_high_ttf_hours": row["storage_high_ttf_hours"],
        "storage_urgent_hours": row["storage_urgent_hours"],
    }


class AlertSettingsUpdate(BaseModel):
    expiring_hours: float
    storage_warn_pct: float
    storage_high_pct: float
    storage_high_ttf_hours: float
    storage_urgent_hours: float


@router.get("/api/alert-settings")
def api_get_alert_settings(ctx: int = Depends(require_context)):
    return get_alert_settings(ctx)


@router.put("/api/alert-settings")
def api_update_alert_settings(req: AlertSettingsUpdate, ctx: int = Depends(require_context)):
    if not (0 < req.expiring_hours <= 24):
        raise HTTPException(status_code=400, detail="expiring_hours must be between 0 and 24")
    if not (0 < req.storage_warn_pct <= 100):
        raise HTTPException(status_code=400, detail="storage_warn_pct must be between 0 and 100")
    if not (0 < req.storage_high_pct <= 100):
        raise HTTPException(status_code=400, detail="storage_high_pct must be between 0 and 100")
    if req.storage_high_pct < req.storage_warn_pct:
        raise HTTPException(status_code=400, detail="storage_high_pct must be >= storage_warn_pct")
    if not (0 < req.storage_high_ttf_hours <= 48):
        raise HTTPException(status_code=400, detail="storage_high_ttf_hours must be between 0 and 48")
    if not (0 < req.storage_urgent_hours <= 48):
        raise HTTPException(status_code=400, detail="storage_urgent_hours must be between 0 and 48")
    ensure_alert_settings_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_alert_settings (context_id, expiring_hours, storage_warn_pct, "
        "storage_high_pct, storage_high_ttf_hours, storage_urgent_hours) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(context_id) DO UPDATE SET "
        "expiring_hours=excluded.expiring_hours, storage_warn_pct=excluded.storage_warn_pct, "
        "storage_high_pct=excluded.storage_high_pct, "
        "storage_high_ttf_hours=excluded.storage_high_ttf_hours, "
        "storage_urgent_hours=excluded.storage_urgent_hours",
        (ctx, req.expiring_hours, req.storage_warn_pct, req.storage_high_pct,
         req.storage_high_ttf_hours, req.storage_urgent_hours),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.post("/api/alert-settings/reset")
def api_reset_alert_settings(ctx: int = Depends(require_context)):
    ensure_alert_settings_table()
    con = get_connection()
    con.execute("DELETE FROM pp_alert_settings WHERE context_id=?", (ctx,))
    con.commit()
    con.close()
    return dict(DEFAULTS)
