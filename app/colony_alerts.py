"""Shared colony-warning engine.

Computes one flat list of individual alert instances (kind, severity, character, planet,
message) from the same underlying colony data. Both the Dashboard (app/planner.py's
`dashboard()`, for display) and the notification scheduler (app/notifications.py, for pushes)
call `compute_colony_alerts()` — a single source of truth so a push notification and what's
shown on screen can never drift apart, and both automatically respect each account's configured
thresholds and muted kinds (app/alert_settings.py) without re-implementing that logic.

Eight kinds (app.alert_settings.ALERT_KINDS): four threshold-based (expired, expiring,
storage_full, factory_refill) and four correctness-based, stored per-scan by
app.esi._detect_colony_issues (ext_unrouted, fac_unfed, fac_output, p0_mismatch) — the
correctness ones are always "high" severity; the threshold-based ones compute their own
severity from the account's configured thresholds (see inline comments below).
"""
import json as _json
import time as _time

from app.sde import get_connection
from app.alert_settings import get_alert_settings

# Correctness-based kinds are always "high" — matches the severity dashboard() has always used
# for these (a misrouted pin or wasted extraction is never merely a "warn").
_CORRECTNESS_SEVERITY = {
    "ext_unrouted": "high",
    "fac_unfed": "high",
    "fac_output": "high",
    "p0_mismatch": "high",
}


def _fetch_colony_rows(context_id: int):
    con = get_connection()
    rows = con.execute("""
        SELECT c.character_name AS ch, c.character_id AS cid, cp.planet_id AS planet_id,
               cp.planet_num AS pn, s.name AS system,
               cp.is_extractor AS is_ext, cp.issues AS issues, cp.sim_state AS sim_state,
               cp.scanned_at AS scanned_at, cp.checkpoint_at AS checkpoint_at, cp.storage AS storage
        FROM pp_char_planets cp
        JOIN pp_characters c ON c.character_id = cp.character_id
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        WHERE c.context_id = ? AND COALESCE(c.is_dummy, 0) = 0
    """, (context_id,)).fetchall()
    con.close()
    return rows


def _fetch_factory_refill_hours(context_id: int) -> float | None:
    """Cadence from the most recent saved plan snapshot — same field the Dashboard's
    "Maintenance routine" card already uses (see planner.py's `_factory_refill_hours`)."""
    con = get_connection()
    row = con.execute(
        "SELECT snapshot FROM pp_plan_snapshots WHERE context_id=? ORDER BY created_at DESC LIMIT 1",
        (context_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    try:
        hours = _json.loads(row["snapshot"]).get("factory_refill_hours")
    except Exception:
        return None
    return hours if hours and hours > 0 else None


def compute_colony_alerts(context_id: int, rows=None, now: float | None = None) -> list[dict]:
    """Flat list of individual alert instances for this account: one dict per affected
    planet/kind — {kind, severity, character_id, character_name, planet_id, location,
    hours_left, pct (storage_full only)}. Already filtered by the account's alert_settings
    thresholds and muted_kinds, so callers never need to re-check those.

    `rows` lets a caller that already fetched pp_char_planets for its own purposes (dashboard())
    pass them in and skip a redundant query; a fresh caller (the notification scheduler,
    iterating many contexts) leaves it None and this does its own fetch.
    """
    if rows is None:
        rows = _fetch_colony_rows(context_id)
    if now is None:
        now = _time.time()
    _alert = get_alert_settings(context_id)
    muted = set(_alert.get("muted_kinds") or [])
    alerts: list[dict] = []

    expiring_window_s = _alert["expiring_hours"] * 3600.0
    refill_hours = _fetch_factory_refill_hours(context_id) if "factory_refill" not in muted else None

    for r in rows:
        cid = r["cid"]
        ch = r["ch"] or "?"
        loc = (r["system"] or "?") + (f" P{r['pn']}" if r["pn"] is not None else "")
        planet_id = r["planet_id"]

        # Correctness-based kinds, stored per-scan (app.esi._detect_colony_issues).
        for k in _json.loads(r["issues"] or "[]"):
            if k in muted:
                continue
            alerts.append({
                "kind": k, "severity": _CORRECTNESS_SEVERITY.get(k, "high"),
                "character_id": cid, "character_name": ch,
                "planet_id": planet_id, "location": loc, "hours_left": None,
            })

        if r["is_ext"]:
            ss = _json.loads(r["sim_state"] or "null")
            exp = ss.get("expiry") if isinstance(ss, dict) else None
            if exp is not None:
                hours_left = (exp - now) / 3600.0
                if exp < now and "expired" not in muted:
                    alerts.append({
                        "kind": "expired", "severity": "high",
                        "character_id": cid, "character_name": ch,
                        "planet_id": planet_id, "location": loc, "hours_left": hours_left,
                    })
                elif now <= exp < now + expiring_window_s and "expiring" not in muted:
                    alerts.append({
                        "kind": "expiring", "severity": "warn",
                        "character_id": cid, "character_name": ch,
                        "planet_id": planet_id, "location": loc, "hours_left": hours_left,
                    })

            if "storage_full" not in muted:
                st = _json.loads(r["storage"] or "null")
                if st:
                    fill_h = st.get("fill_m3_h", 0) or 0
                    anchor = r["checkpoint_at"] or r["scanned_at"]
                    el_h = max(0.0, (now - anchor) / 3600.0) if anchor else 0.0
                    cap = st.get("cap_m3") or 1
                    vol = min(cap, (st.get("vol_m3") or 0) + fill_h * el_h)
                    pct = vol / cap * 100.0
                    ttf = ((cap - vol) / fill_h) if fill_h > 0 and vol < cap else None
                    if pct >= _alert["storage_warn_pct"]:
                        sev = "high" if (pct >= _alert["storage_high_pct"]
                                          or (ttf is not None and ttf < _alert["storage_high_ttf_hours"])) else "warn"
                        alerts.append({
                            "kind": "storage_full", "severity": sev,
                            "character_id": cid, "character_name": ch,
                            "planet_id": planet_id, "location": loc, "hours_left": ttf,
                            "pct": round(pct),
                        })
        elif refill_hours and r["scanned_at"]:
            # Factory refill: no dedicated threshold field exists (or is asked for) yet, so this
            # deliberately reuses expiring_hours as "how far ahead to flag" (same "warn me about
            # things due soon" idea as extraction expiry) and storage_high_ttf_hours as the
            # warn→high cutoff (same "imminent" idea as storage) rather than adding two more
            # number fields for a single kind.
            due = r["scanned_at"] + refill_hours * 3600.0
            hours_left = (due - now) / 3600.0
            if hours_left <= _alert["expiring_hours"]:
                sev = "high" if (hours_left < 0 or hours_left < _alert["storage_high_ttf_hours"]) else "warn"
                alerts.append({
                    "kind": "factory_refill", "severity": sev,
                    "character_id": cid, "character_name": ch,
                    "planet_id": planet_id, "location": loc, "hours_left": hours_left,
                })

    return alerts
