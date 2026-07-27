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
# `group` clusters related features in the Admin UI (collapsible sections — see admin.js
# loadAdminFeatures) so the list stays scannable as it grows; pick the app area the feature's
# `description` names first (most descriptions already lead with "On the X tab:" / "On X:").
# GROUP_ORDER below controls display order; a group missing from it sorts last.
FEATURE_REGISTRY = [
    {"key": "timeline", "label": "PI process timeline", "group": "Dashboard",
     "description": "Account-level “you are here” timeline on the Dashboard: "
                    "extractors started → haul P1 → refill factories.",
     "default": False},
    {"key": "split_extraction", "label": "Split extraction", "group": "Planner",
     "description": "Plan two P0s on one planet type (2 ECUs share the 10 heads) and "
                    "reinvest the freed planets into more factories.",
     "default": True},
    {"key": "baskets", "label": "Custom production baskets", "group": "Planner",
     "description": "Pick a custom multi-product basket as a planning target in the product picker.",
     "default": True},
    {"key": "skill_roi", "label": "Skill-ROI advisor", "group": "Setup Analysis",
     "description": "On Setup Analysis: which character skills (Interplanetary Consolidation, "
                    "Command Center Upgrades) to train for more output, ranked by ISK/day.",
     "default": False},
    {"key": "move_character", "label": "Move a character to another account", "group": "Setup Analysis",
     "description": "On Setup Analysis: the 1:1 colony-swap tool for moving a character's PI to a "
                    "character on another account.",
     "default": False},
    {"key": "schedule_sync", "label": "Extractor schedule sync warning", "group": "Dashboard",
     "description": "On the Dashboard: warn when an extractor runs a different program length than "
                    "the rest of the fleet (drifts off your batch restart). Mutable per character.",
     "default": False},
    {"key": "pad_fill", "label": "Fill-factories meter", "group": "Dashboard",
     "description": "On the Dashboard: how far the P1 in your extractor pads would go toward filling "
                    "every factory's 30,000 m³ buffer — a binding-material % + per-material breakdown.",
     "default": False},
    {"key": "dummy_characters", "label": "Placeholder characters", "group": "Characters",
     "description": "On the Characters tab: add placeholder toons (no ESI login) that contribute "
                    "planet slots + CCU level to plans without logging the alt in.",
     "default": False},
    {"key": "factory_layout", "label": "Factory Layout tab", "group": "Planner",
     "description": "Show the Factory Layout tab — generates importable EVE PI templates for any "
                    "P1–P4 product.",
     "default": False},
    {"key": "planet_db", "label": "Planet DB tab", "group": "Planet DB",
     "description": "Show the Planet DB tab — the shared planet density database the planner "
                    "uses; also lets users submit and browse planet data.",
     "default": False},
    {"key": "notifications", "label": "Notifications", "group": "Notifications & Alerts",
     "description": "Let users configure Pushover / ntfy.sh / Discord alerts for extractor "
                    "expiry and factory refill reminders.",
     "default": False},
    {"key": "esi_cache_skip", "label": "ESI cache-aware rescan", "group": "Characters",
     "description": "Skip re-fetching a colony/skills from ESI while its cache (Expires header) "
                    "hasn't lapsed yet — faster rescans — and show a “no new data until” hint "
                    "in the UI.",
     "default": False},
    {"key": "measured_yield", "label": "Measured yield in Planet DB", "group": "Planet DB",
     "description": "On the Planet DB tab: show a real measured average extraction yield "
                    "(pooled across all users' actual colonies) alongside a planet's static "
                    "richness value, where enough samples exist.",
     "default": False},
    {"key": "hybrid_colonies", "label": "Hybrid colony analysis", "group": "Setup Analysis",
     "description": "Track hand-built colonies that run extraction + a P1→P2+ factory chain "
                    "on one planet: surfaces their real demand in Setup Analysis and suggests "
                    "reseats to close their own shortfall (never a redeploy).",
     "default": False},
    {"key": "measured_yield_blend", "label": "Measured yield in planning weights", "group": "Planner",
     "description": "Nudge the planner's extractor placement toward planets with real pooled "
                    "yield data, confidence-weighted by sample count — never overrides the "
                    "static richness value, and does nothing for the ~99% of planets with no "
                    "measured data yet. Separate from the Planet DB display flag since this "
                    "changes real plan output, not just a badge.",
     "default": False},
    {"key": "alert_settings", "label": "Configurable Dashboard alerts", "group": "Notifications & Alerts",
     "description": "Settings → Alerts: customize the extractor-expiry warning window and "
                    "storage-fill warning/severity thresholds used by the Dashboard's colony "
                    "warnings, instead of the fixed defaults.",
     "default": False},
    {"key": "extraction_targets", "label": "Extraction targets reference", "group": "Setup Analysis",
     "description": "On Setup Analysis: each material row shows its P0 source name and a comfortable "
                    "P0/hr-per-planet target at a glance — a reference while reseating extractor heads.",
     "default": False},
    {"key": "redeploy_proximity", "label": "Overlapping extraction ranges", "group": "Setup Analysis",
     "description": "On Setup Analysis: flag when two colonies on the same planet have overlapping "
                    "reachable extraction areas for the same P0 — they compete for the same hotspots "
                    "and reseating can't escape it, so it suggests redeploying one command centre to "
                    "a clear area. Overlap % cutoff is set in Settings → General. Needs a rescan to "
                    "capture extractor positions.",
     "default": False},
    {"key": "redeploy_depletion", "label": "Depleting-deposit redeploy advice", "group": "Setup Analysis",
     "description": "On Setup Analysis: flag an extractor colony whose measured install-yield has "
                    "trended down across the last several programs — the deposit is exhausting, so a "
                    "reseat only chases a sinking ceiling and the fix is to redeploy the CC to a fresh "
                    "planet.",
     "default": False},
    {"key": "reactions", "label": "Reactions tracking", "group": "Reactions",
     "description": "Adds a Reactions tab: ranks the most profitable moon-goo reaction chains, "
                    "priced from your alliance's own price sheet if it has one (see Admin → "
                    "Groups) or live market prices otherwise, to ship and sell. This only "
                    "controls whether the nav tab shows — the tool itself is open to any "
                    "logged-in user regardless of this flag's state, so rolling it to 'public' "
                    "just reveals the tab to everyone.",
     "default": False},
    {"key": "reaction_orders", "label": "Reactions: customer orders", "group": "Reactions",
     "description": "On the Reactions tab: track a fixed-unit order for another player — runs "
                    "needed for the whole chain, materials to import, a cost breakdown (no "
                    "markup applied — you decide what to charge), and a rough time estimate. "
                    "Committing to an order occupies real reaction slots the same way the "
                    "suggestion/manual-assign flow does.",
     "default": False},
    {"key": "local_market", "label": "Reactions: local / alliance market pricing", "group": "Reactions",
     "description": "On the Reactions tab: follow one or more markets (a player structure market "
                    "and/or a public region market) in a priority order and price reactions "
                    "against them, falling back to Jita for anything not listed locally. Adds a "
                    "market & freight setup card and a per-line price-source badge.",
     "default": False},
    {"key": "local_sell_hint", "label": "Reactions: local buy-order sell hint", "group": "Reactions",
     "description": "In the 'reactions completed' alert: if one of your followed local/alliance "
                    "markets has a buy order that beats hauling the output to Jita (after jump-freight "
                    "cost), tell you how much you can sell there and the ISK you'd gain. Read-only — "
                    "no auto-sell. Requires local / alliance market pricing to be set up.",
     "default": False},
    {"key": "industry", "label": "Industry / manufacturing planner", "group": "Industry",
     "description": "Adds an Industry tab: pick a buildable (module up to a capital) and a "
                    "quantity, and it decides build-vs-buy for every component, prices a shopping "
                    "list against your followed markets (local → Jita), and reports what it costs "
                    "and how long it takes. Spans manufacturing AND reactions for deep builds. "
                    "Admin-preview while it's built out.",
     "default": False},
    {"key": "industry_share", "label": "Industry: customer build-status links", "group": "Industry",
     "description": "Share a queued build with the person who ordered it: a login-free link showing "
                    "what's being built, which stage it's on, a progress bar and an ETA. Deliberately "
                    "carries no character names, systems or ISK — only the build's own progress. "
                    "Revocable at any time.",
     "default": False},
]
GROUP_ORDER = ["Dashboard", "Setup Analysis", "Planner", "Planet DB", "Characters",
               "Notifications & Alerts", "Reactions", "Industry"]
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
         "group": f.get("group") or "Other",
         "state": db_state.get(f["key"]) or _default_state(f)}
        for f in FEATURE_REGISTRY
    ]
    from app.main import GIT_COMMIT
    _admin, _tester = admin_and_tester_status(pp_session)
    return {"features": feats, "group_order": GROUP_ORDER, "is_admin": _admin, "is_tester": _tester,
            "git_commit": GIT_COMMIT}


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
