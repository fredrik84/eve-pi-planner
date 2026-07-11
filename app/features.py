"""Feature flags: gate features behind an admin-controlled on/off switch.

We have no separate staging environment, so new (or risky) features ship to production
hidden from the public and visible only to admins. An admin flips a flag from the Admin tab
to roll a feature out to everyone. The flag state lives in `pp_features`; the set of known
features is the code-defined `FEATURE_REGISTRY` (a feature missing from the registry can't be
toggled). Admins always see every feature regardless of its flag (so they can preview); the
public sees a feature only when its flag is enabled. Mirrors the auth/table pattern in admin.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.esi import require_admin, admin_and_tester_status

router = APIRouter()

# key, label, one-line description, and the default public-visibility state. New/experimental
# features default False (admin-only until rolled out); already-shipped features default True
# (visible now, but retrofitted with a flag so an admin can pull them if they misbehave).
FEATURE_REGISTRY = [
    {"key": "timeline", "label": "PI process timeline",
     "description": "Account-level “you are here” timeline on the Dashboard: "
                    "extractors started → haul P1 → refill factories.",
     "default": False},
    {"key": "split_extraction", "label": "Split extraction",
     "description": "Plan two P0s on one planet type (2 ECUs share the 10 heads) and "
                    "reinvest the freed planets into more factories.",
     "default": True},
    {"key": "baskets", "label": "Custom production baskets",
     "description": "Pick a custom multi-product basket as a planning target in the product picker.",
     "default": True},
    {"key": "skill_roi", "label": "Skill-ROI advisor",
     "description": "On Setup Analysis: which character skills (Interplanetary Consolidation, "
                    "Command Center Upgrades) to train for more output, ranked by ISK/day.",
     "default": False},
    {"key": "move_character", "label": "Move a character to another account",
     "description": "On Setup Analysis: the 1:1 colony-swap tool for moving a character's PI to a "
                    "character on another account.",
     "default": False},
    {"key": "schedule_sync", "label": "Extractor schedule sync warning",
     "description": "On the Dashboard: warn when an extractor runs a different program length than "
                    "the rest of the fleet (drifts off your batch restart). Mutable per character.",
     "default": False},
    {"key": "pad_fill", "label": "Fill-factories meter",
     "description": "On the Dashboard: how far the P1 in your extractor pads would go toward filling "
                    "every factory's 30,000 m³ buffer — a binding-material % + per-material breakdown.",
     "default": False},
    {"key": "dummy_characters", "label": "Placeholder characters",
     "description": "On the Characters tab: add placeholder toons (no ESI login) that contribute "
                    "planet slots + CCU level to plans without logging the alt in.",
     "default": False},
    {"key": "factory_layout", "label": "Factory Layout tab",
     "description": "Show the Factory Layout tab — generates importable EVE PI templates for any "
                    "P1–P4 product.",
     "default": False},
    {"key": "planet_db", "label": "Planet DB tab",
     "description": "Show the Planet DB tab — the shared planet density database the planner "
                    "uses; also lets users submit and browse planet data.",
     "default": False},
    {"key": "notifications", "label": "Notifications",
     "description": "Let users configure Pushover / ntfy.sh / Discord alerts for extractor "
                    "expiry and factory refill reminders.",
     "default": False},
    {"key": "esi_cache_skip", "label": "ESI cache-aware rescan",
     "description": "Skip re-fetching a colony/skills from ESI while its cache (Expires header) "
                    "hasn't lapsed yet — faster rescans — and show a “no new data until” hint "
                    "in the UI.",
     "default": False},
    {"key": "measured_yield", "label": "Measured yield in Planet DB",
     "description": "On the Planet DB tab: show a real measured average extraction yield "
                    "(pooled across all users' actual colonies) alongside a planet's static "
                    "richness value, where enough samples exist.",
     "default": False},
    {"key": "hybrid_colonies", "label": "Hybrid colony analysis",
     "description": "Track hand-built colonies that run extraction + a P1→P2+ factory chain "
                    "on one planet: surfaces their real demand in Setup Analysis and suggests "
                    "reseats to close their own shortfall (never a redeploy).",
     "default": False},
    {"key": "measured_yield_blend", "label": "Measured yield in planning weights",
     "description": "Nudge the planner's extractor placement toward planets with real pooled "
                    "yield data, confidence-weighted by sample count — never overrides the "
                    "static richness value, and does nothing for the ~99% of planets with no "
                    "measured data yet. Separate from the Planet DB display flag since this "
                    "changes real plan output, not just a badge.",
     "default": False},
    {"key": "alert_settings", "label": "Configurable Dashboard alerts",
     "description": "Settings → Alerts: customize the extractor-expiry warning window and "
                    "storage-fill warning/severity thresholds used by the Dashboard's colony "
                    "warnings, instead of the fixed defaults.",
     "default": False},
    {"key": "extraction_targets", "label": "Extraction targets reference",
     "description": "On Setup Analysis: a per-resource P0/hr lookup table (min + comfortable-buffer "
                    "target) for the selected plan — a reference while reseating extractor heads.",
     "default": False},
]
_DEFAULTS = {f["key"]: f for f in FEATURE_REGISTRY}
VALID_STATES = {"hidden", "admin", "testers", "public"}


def _default_state(f: dict) -> str:
    return "public" if f["default"] else "admin"


@ensure_once
def ensure_features_table():
    con = get_connection()
    con.execute(
        """CREATE TABLE IF NOT EXISTS pp_features (
               key        TEXT PRIMARY KEY,
               enabled    INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT
           )"""
    )
    con.commit()
    # Add state column and migrate from boolean enabled if needed
    try:
        con.execute("ALTER TABLE pp_features ADD COLUMN state TEXT")
    except Exception:
        pass
    con.execute(
        "UPDATE pp_features SET state = CASE WHEN enabled=1 THEN 'public' ELSE 'admin' END "
        "WHERE state IS NULL"
    )
    existing = {r["key"] for r in con.execute("SELECT key FROM pp_features")}
    now = datetime.now(timezone.utc).isoformat()
    for f in FEATURE_REGISTRY:
        if f["key"] not in existing:
            con.execute(
                "INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,?)",
                (f["key"], 1 if f["default"] else 0, _default_state(f), now),
            )
    con.commit()
    con.close()


def feature_enabled(key: str) -> bool:
    """Is this feature public-visible? (For backend gating — ignores admin/tester roles.)"""
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT state FROM pp_features WHERE key=?", (key,)).fetchone()
    con.close()
    if row is not None:
        return row["state"] == "public"
    return bool(_DEFAULTS.get(key, {}).get("default", False))


@router.get("/api/features")
def list_features(pp_session: str = Cookie(default=None)):
    """Public. Returns every registered feature with its state, plus caller's admin/tester role."""
    ensure_features_table()
    con = get_connection()
    db_state = {r["key"]: r["state"] for r in con.execute("SELECT key, state FROM pp_features")}
    con.close()
    feats = [
        {"key": f["key"], "label": f["label"], "description": f["description"],
         "state": db_state.get(f["key"]) or _default_state(f)}
        for f in FEATURE_REGISTRY
    ]
    from app.main import GIT_COMMIT
    _admin, _tester = admin_and_tester_status(pp_session)
    return {"features": feats, "is_admin": _admin, "is_tester": _tester, "git_commit": GIT_COMMIT}


class FeatureStateUpdate(BaseModel):
    state: str


@router.post("/api/features/{key}")
def set_feature(key: str, req: FeatureStateUpdate, _: int = Depends(require_admin)):
    if key not in _DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown feature")
    if req.state not in VALID_STATES:
        raise HTTPException(status_code=400, detail=f"Invalid state — must be one of: {', '.join(sorted(VALID_STATES))}")
    ensure_features_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, state=excluded.state, updated_at=excluded.updated_at",
        (key, 1 if req.state == "public" else 0, req.state, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True, "key": key, "state": req.state}
