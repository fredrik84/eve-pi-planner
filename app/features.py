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

from app.sde import get_connection
from app.esi import require_admin, is_admin

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
]
_DEFAULTS = {f["key"]: f for f in FEATURE_REGISTRY}


def ensure_features_table():
    con = get_connection()
    con.execute(
        """CREATE TABLE IF NOT EXISTS pp_features (
               key        TEXT PRIMARY KEY,
               enabled    INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT
           )"""
    )
    existing = {r["key"] for r in con.execute("SELECT key FROM pp_features")}
    now = datetime.now(timezone.utc).isoformat()
    for f in FEATURE_REGISTRY:
        if f["key"] not in existing:   # seed a missing flag with its registry default
            con.execute(
                "INSERT INTO pp_features (key, enabled, updated_at) VALUES (?,?,?)",
                (f["key"], 1 if f["default"] else 0, now),
            )
    con.commit()
    con.close()


def feature_enabled(key: str) -> bool:
    """Is this feature on for the public? (Ignores admin — for backend gating if ever needed.)"""
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT enabled FROM pp_features WHERE key=?", (key,)).fetchone()
    con.close()
    if row is not None:
        return bool(row["enabled"])
    return bool(_DEFAULTS.get(key, {}).get("default", False))


@router.get("/api/features")
def list_features(pp_session: str = Cookie(default=None)):
    """Public. Returns every registered feature with its current public-visibility state, plus
    whether the caller is an admin (the SPA shows a feature when enabled OR the caller is admin)."""
    ensure_features_table()
    con = get_connection()
    state = {r["key"]: bool(r["enabled"]) for r in con.execute("SELECT key, enabled FROM pp_features")}
    con.close()
    feats = [
        {"key": f["key"], "label": f["label"], "description": f["description"],
         "enabled": state.get(f["key"], f["default"])}
        for f in FEATURE_REGISTRY
    ]
    return {"features": feats, "is_admin": is_admin(pp_session)}


class FeatureToggle(BaseModel):
    enabled: bool


@router.post("/api/features/{key}")
def set_feature(key: str, req: FeatureToggle, _: int = Depends(require_admin)):
    if key not in _DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown feature")
    ensure_features_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_features (key, enabled, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (key, 1 if req.enabled else 0, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True, "key": key, "enabled": req.enabled}
