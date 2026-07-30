"""Per-account thresholds for the Dashboard's colony alerts (extractor expiry window,
storage-fill warning/severity), plus per-kind muting for every alert the Dashboard can show —
including the correctness-based ones (unrouted pins, extracting an unused P0) that have no
numeric threshold to tune, only an on/off. Defaults match what planner.py's dashboard()
hardcoded before this existed (nothing muted), so nothing changes in behavior until a user
customizes their own settings.
"""
import json as _json

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
    "reaction_refill_hours": 24.0,  # a character's soonest-finishing running reaction job within
                                     # this window counts as "needs a refill/restart soon"
    "muted_kinds": [],
}

# Every kind of colony warning the Dashboard AND the notification system can raise, muteable/
# routable regardless of whether it has a tunable threshold. Keys match the "kind" strings
# app.alerts.compute_alerts() emits (the single shared engine both consume):
# ext_unrouted/fac_unfed/fac_output/p0_mismatch come straight from
# app.esi._detect_colony_issues; expired/expiring/storage_full/factory_refill are the shared
# engine's own labels for the four threshold-based kinds; schedule_sync (an extractor running a
# different program length than the fleet's norm) has no numeric threshold, same as the
# correctness kinds — always "warn" severity (a drifted schedule isn't dangerous, just worth a
# heads-up), never "high".
ALERT_KINDS = [
    {"key": "expired", "label": "Extractions expired"},
    {"key": "expiring", "label": "Extractions expiring soon"},
    {"key": "storage_full", "label": "Storage filling up"},
    {"key": "factory_refill", "label": "Factories due for refill"},
    {"key": "ext_unrouted", "label": "Extractor not routed"},
    {"key": "fac_unfed", "label": "Factory has no input route"},
    {"key": "fac_output", "label": "Factory output not routed"},
    {"key": "p0_mismatch", "label": "Extracting something the factories don't use"},
    {"key": "schedule_sync", "label": "Extractor schedule out of sync"},
    {"key": "reaction_finishing_soon", "label": "Reactions finishing soon"},
    {"key": "reaction_completed", "label": "Reactions completed"},
]
_VALID_KINDS = {k["key"] for k in ALERT_KINDS}


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
    try:
        con.execute("ALTER TABLE pp_alert_settings ADD COLUMN muted_kinds TEXT")
        con.commit()
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE pp_alert_settings ADD COLUMN reaction_refill_hours REAL DEFAULT 24")
        con.commit()
    except Exception:
        pass
    con.close()


def get_alert_settings(context_id: int | None) -> dict:
    """Effective settings for this account — the saved row if customized, else the defaults
    above. Used both by dashboard()'s alert computation and the settings GET endpoint, so the
    two can never drift."""
    if not context_id:
        return dict(DEFAULTS)
    ensure_alert_settings_table()
    con = get_connection()
    row = con.execute(
        "SELECT expiring_hours, storage_warn_pct, storage_high_pct, storage_high_ttf_hours, "
        "storage_urgent_hours, muted_kinds, reaction_refill_hours FROM pp_alert_settings WHERE context_id=?",
        (context_id,),
    ).fetchone()
    con.close()
    if not row:
        return dict(DEFAULTS)
    try:
        muted = [k for k in _json.loads(row["muted_kinds"] or "[]") if k in _VALID_KINDS]
    except Exception:
        muted = []
    return {
        "expiring_hours": row["expiring_hours"],
        "storage_warn_pct": row["storage_warn_pct"],
        "storage_high_pct": row["storage_high_pct"],
        "storage_high_ttf_hours": row["storage_high_ttf_hours"],
        "storage_urgent_hours": row["storage_urgent_hours"],
        "reaction_refill_hours": row["reaction_refill_hours"] if row["reaction_refill_hours"] is not None else DEFAULTS["reaction_refill_hours"],
        "muted_kinds": muted,
    }


class AlertSettingsUpdate(BaseModel):
    expiring_hours: float
    storage_warn_pct: float
    storage_high_pct: float
    storage_high_ttf_hours: float
    storage_urgent_hours: float
    reaction_refill_hours: float = 24.0
    muted_kinds: list[str] = []


@router.get("/api/alert-settings")
def api_get_alert_settings(ctx: int = Depends(require_context)):
    return {**get_alert_settings(ctx), "available_kinds": ALERT_KINDS}


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
    if not (0 < req.reaction_refill_hours <= 168):
        raise HTTPException(status_code=400, detail="reaction_refill_hours must be between 0 and 168")
    unknown = [k for k in req.muted_kinds if k not in _VALID_KINDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown alert kind(s): {', '.join(unknown)}")
    ensure_alert_settings_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_alert_settings (context_id, expiring_hours, storage_warn_pct, "
        "storage_high_pct, storage_high_ttf_hours, storage_urgent_hours, reaction_refill_hours, muted_kinds) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(context_id) DO UPDATE SET "
        "expiring_hours=excluded.expiring_hours, storage_warn_pct=excluded.storage_warn_pct, "
        "storage_high_pct=excluded.storage_high_pct, "
        "storage_high_ttf_hours=excluded.storage_high_ttf_hours, "
        "storage_urgent_hours=excluded.storage_urgent_hours, reaction_refill_hours=excluded.reaction_refill_hours, "
        "muted_kinds=excluded.muted_kinds",
        (ctx, req.expiring_hours, req.storage_warn_pct, req.storage_high_pct,
         req.storage_high_ttf_hours, req.storage_urgent_hours, req.reaction_refill_hours,
         _json.dumps(sorted(set(req.muted_kinds)))),
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
    return {**dict(DEFAULTS), "available_kinds": ALERT_KINDS}
