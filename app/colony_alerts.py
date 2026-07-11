"""Shared colony-warning engine.

Computes one flat list of individual alert instances (kind, severity, character, planet,
message) from the same underlying colony data. Both the Dashboard (app/planner.py's
`dashboard()`, for display) and the notification scheduler (app/notifications.py, for pushes)
call `compute_colony_alerts()` — a single source of truth so a push notification and what's
shown on screen can never drift apart, and both automatically respect each account's configured
thresholds and muted kinds (app/alert_settings.py) without re-implementing that logic.

Eleven kinds (app.alert_settings.ALERT_KINDS): four threshold-based (expired, expiring,
storage_full, factory_refill), four correctness-based, stored per-scan by
app.esi._detect_colony_issues (ext_unrouted, fac_unfed, fac_output, p0_mismatch) — always
"high" severity — schedule_sync (an extractor running a different program length than the
fleet's norm), always "warn" severity, computed fleet-wide via _extractor_program_lengths() —
and two Reactions-specific kinds (reaction_not_running, reaction_low_stock, see
_reaction_alerts() below), unrelated to PI colonies entirely but folded into the same flat list
so they get the same mute/severity/dashboard/push plumbing for free. The threshold-based kinds
compute their own severity from the account's configured thresholds (see inline comments below).
"""
import json as _json
import time as _time

from app.sde import get_connection, load_pi_data
from app.alert_settings import get_alert_settings

# Correctness-based kinds are always "high" — matches the severity dashboard() has always used
# for these (a misrouted pin or wasted extraction is never merely a "warn").
_CORRECTNESS_SEVERITY = {
    "ext_unrouted": "high",
    "fac_unfed": "high",
    "fac_output": "high",
    "p0_mismatch": "high",
}


def _reaction_alerts(context_id: int, muted: set, now: float) -> list[dict]:
    """Reactions-specific alerts — entirely separate data (pp_reaction_assignments,
    pp_moon_goo_prices) from the PI colony rows the rest of this module works from, so this is
    a self-contained block rather than woven into the per-row loop below. Cheaply short-circuits
    to nothing for the vast majority of accounts (non-B0SS, or B0SS with nothing pending), since
    this runs for every account on every 15-minute notification tick.

    - reaction_not_running: a suggestion the player clicked "Assign" on but hasn't actually
      installed in-game yet (no matching ESI job has shown up) for longer than expiring_hours —
      reuses that existing threshold rather than adding a dedicated field, same convention
      factory_refill already follows.
    - reaction_low_stock: the shared alliance goo stock can't actually cover what's currently
      assigned across (potentially) several players — not fixable by any one player, but still
      useful to surface. Uses `planet_id` to carry the moon-goo type_id (not a real planet) so
      the notification scheduler's cooldown/dedup keying (which is keyed on `planet_id`) still
      works — see notifications.py's _recently_notified.
    """
    if "reaction_not_running" in muted and "reaction_low_stock" in muted:
        return []
    con = get_connection()
    try:
        assignments = con.execute(
            "SELECT a.id, a.character_id, a.type_id, a.name, a.runs, a.created_at, c.character_name "
            "FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id=?", (context_id,),
        ).fetchall()
    except Exception:
        return []  # table may not exist yet on a freshly-deployed environment — never let that break PI alerts
    finally:
        con.close()
    if not assignments:
        return []

    alerts: list[dict] = []
    expiring_hours = get_alert_settings(context_id)["expiring_hours"]

    if "reaction_not_running" not in muted:
        for a in assignments:
            age_hours = (now - a["created_at"]) / 3600.0
            if age_hours >= expiring_hours:
                alerts.append({
                    "kind": "reaction_not_running",
                    "severity": "high" if age_hours >= expiring_hours * 2 else "warn",
                    "character_id": a["character_id"], "character_name": a["character_name"],
                    "planet_id": a["id"], "location": a["name"], "hours_left": None,
                })

    if "reaction_low_stock" not in muted:
        try:
            from app.reactions import _load_goo_and_reached, _explode_shopping_list
            loaded = _load_goo_and_reached(context_id)
        except Exception:
            loaded = None
        if loaded:
            goo, reached, *_ = loaded
            types = load_pi_data()["types"]
            needed: dict[int, float] = {}
            for a in assignments:
                node = reached.get(a["type_id"])
                if node and node["via"]:
                    _explode_shopping_list(a["type_id"], a["runs"] * node["via"]["output_qty"], reached, needed)
            for tid, qty in needed.items():
                g = goo.get(tid)
                if g and g["stock"] < qty:
                    alerts.append({
                        "kind": "reaction_low_stock", "severity": "warn",
                        "character_id": None, "character_name": None,
                        "planet_id": tid, "location": types.get(tid, {}).get("name", str(tid)),
                        "hours_left": None,
                        # Bare material name alone gave no way to judge severity (a near-miss vs.
                        # zero stock look identical) — carry the actual numbers so the dashboard/
                        # push message can say "need 12,450, 0 in stock" instead of just a name.
                        "needed": round(qty, 1), "available": g["stock"],
                    })

    return alerts


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


def _extractor_program_lengths(rows) -> tuple[list[dict], float | None]:
    """Per-extractor {character_id, character_name, planet_id, location, prog_hours, expiry} +
    the fleet's most common program length (0.5h bins, i.e. the batch-restart cadence). Shared
    by compute_colony_alerts() (the schedule_sync alert, mute-aware) and planner.dashboard()'s
    restart-due countdown (an always-on maintenance stat, NOT mute-aware — it needs every
    extractor's program data regardless of any account's alert settings) — extracted here so
    the two can't compute a different norm."""
    ext_progs: list[dict] = []
    for r in rows:
        if not r["is_ext"]:
            continue
        ss = _json.loads(r["sim_state"] or "null")
        if isinstance(ss, dict) and ss.get("program_days"):
            ext_progs.append({
                "character_id": r["cid"], "character_name": r["ch"] or "?",
                "planet_id": r["planet_id"],
                "location": (r["system"] or "?") + (f" P{r['pn']}" if r["pn"] is not None else ""),
                "prog_hours": ss["program_days"] * 24.0, "expiry": ss.get("expiry"),
            })
    norm = None
    if ext_progs:
        counts: dict[float, int] = {}
        for e in ext_progs:
            b = round(e["prog_hours"] * 2) / 2
            counts[b] = counts.get(b, 0) + 1
        norm = max(counts, key=counts.get)
    return ext_progs, norm


def compute_colony_alerts(context_id: int, rows=None, now: float | None = None) -> list[dict]:
    """Flat list of individual alert instances for this account: one dict per affected
    planet/kind — {kind, severity, character_id, character_name, planet_id, location,
    hours_left, pct (storage_full only), prog_hours/norm_hours (schedule_sync only)}. Already
    filtered by the account's alert_settings thresholds and muted_kinds, so callers never need
    to re-check those.

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

    # schedule_sync: fleet-wide (needs every extractor's program length to find the norm), so
    # computed once here rather than per-row like the rest of this function.
    if "schedule_sync" not in muted:
        ext_progs, norm = _extractor_program_lengths(rows)
        if len(ext_progs) >= 3 and norm is not None:
            for e in ext_progs:
                if abs(e["prog_hours"] - norm) > 0.4:
                    alerts.append({
                        "kind": "schedule_sync", "severity": "warn",
                        "character_id": e["character_id"], "character_name": e["character_name"],
                        "planet_id": e["planet_id"], "location": e["location"], "hours_left": None,
                        "prog_hours": round(e["prog_hours"], 1), "norm_hours": round(norm, 1),
                    })

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

    alerts.extend(_reaction_alerts(context_id, muted, now))
    return alerts
