"""Shared alert engine.

Computes one flat list of individual alert instances (kind, severity, character, planet,
message). Most kinds come from colony data, but not all — see the Reactions kinds below, which
is why this module is named for alerts rather than colonies. Both the Dashboard
(app/planner_dashboard.py's `dashboard()`, for display) and the notification scheduler
(app/notifications.py, for pushes)
call `compute_alerts()` — a single source of truth so a push notification and what's
shown on screen can never drift apart, and both automatically respect each account's configured
thresholds and muted kinds (app/alert_settings.py) without re-implementing that logic.

Eleven kinds (app.alert_settings.ALERT_KINDS): four threshold-based (expired, expiring,
storage_full, factory_refill), four correctness-based, stored per-scan by
app.esi._detect_colony_issues (ext_unrouted, fac_unfed, fac_output, p0_mismatch) — always
"high" severity — schedule_sync (an extractor running a different program length than the
fleet's norm), always "warn" severity, computed fleet-wide via _extractor_program_lengths() —
and two Reactions-specific kinds (reaction_finishing_soon, reaction_completed — see
_reaction_alerts() below), unrelated to PI colonies entirely but folded into the same flat list
so they get the same mute/severity/dashboard/push plumbing for free. The threshold-based kinds compute their own severity
from the account's configured thresholds (see inline comments below).
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


def _reaction_alerts(context_id: int, muted: set, now: float) -> list[dict]:
    """Reactions-specific alerts — entirely separate data from the PI colony rows the rest of
    this module works from, so this is a self-contained block rather than woven into the per-row
    loop below. Cheaply short-circuits to nothing for the vast majority of accounts, since this
    runs for every account on every 15-minute notification tick.

    - reaction_finishing_soon: a character's soonest-finishing RUNNING reaction job (from the
      cached ESI job snapshot — see app.reactions.pp_char_industry_jobs) has less than the
      configured reaction_refill_hours left. ESI reports a job's end_date once at install time
      and it never changes, so a stale cache is still accurate for this — "hours left" is always
      recomputed fresh against `now`, only the job LIST itself can go stale if a new job started
      since the last refresh (same staleness the Reactions tab's own "Refresh" button already
      exists to fix).
    - reaction_completed: one or more of a character's running jobs have PASSED their end_date —
      finished and sitting idle until you collect the output and restart the slot. Detected from
      the same cached snapshot (a job's fixed end_date in the past), so no fresh ESI call is
      needed; it re-nags on a cooldown until the job is delivered and drops out of the snapshot.

    (There used to be a kind here called reaction_not_running, warning when an assigned-but-not-
    yet-installed suggestion sat too long — removed 2026-07-12: it had no natural resolution
    condition and stayed forever for anyone who simply hadn't gotten to installing it yet,
    unlike this one, which clears itself the moment a fresh job goes in. There was also, even
    earlier, a reaction_low_stock kind — removed for the alliance-sheet-stock-is-untrustworthy
    reason documented in app.reactions._resolve_reachable's docstring.)
    """
    want_soon = "reaction_finishing_soon" not in muted
    want_done = "reaction_completed" not in muted
    if not want_soon and not want_done:
        return []
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT j.character_id, j.jobs_json, c.character_name FROM pp_char_industry_jobs j "
            "JOIN pp_characters c ON c.character_id = j.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
    except Exception:
        return []  # table may not exist yet on a freshly-deployed environment — never let that break PI alerts
    finally:
        con.close()
    if not rows:
        return []

    from datetime import datetime
    threshold_hours = get_alert_settings(context_id)["reaction_refill_hours"]
    alerts: list[dict] = []

    for r in rows:
        try:
            jobs = _json.loads(r["jobs_json"] or "[]")
        except Exception:
            continue
        soonest_running = None   # least hours_left among jobs STILL running (>0h left)
        done_count = 0           # jobs whose end_date has passed — finished, awaiting collection/restart
        done_runs: dict[int, int] = {}  # finished output per product type_id — for the local-sale hint
        for j in jobs:
            if j.get("status") not in ("active", "paused", "ready"):
                continue
            end = j.get("end_date")
            if not end:
                continue
            try:
                end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            hours_left = (end_ts - now) / 3600.0
            if hours_left <= 0:
                done_count += 1
                tid = j.get("product_type_id")
                if tid:
                    done_runs[tid] = done_runs.get(tid, 0) + (j.get("runs") or 0)
            elif soonest_running is None or hours_left < soonest_running:
                soonest_running = hours_left
        # Finishing soon = a job still running with less than the threshold left (a lead-time warning).
        # Already-finished jobs no longer count here — they're the reaction_completed alert below.
        if want_soon and soonest_running is not None and soonest_running <= threshold_hours:
            alerts.append({
                "kind": "reaction_finishing_soon",
                "severity": "warn",
                "character_id": r["character_id"], "character_name": r["character_name"],
                "planet_id": None, "location": None, "hours_left": round(soonest_running, 1),
            })
        # Completed = one or more jobs finished and sitting idle — go collect the output and restart the
        # slot. `runs` carries the finished-job count so the message can say how many.
        if want_done and done_count > 0:
            alerts.append({
                "kind": "reaction_completed",
                "severity": "warn",
                "character_id": r["character_id"], "character_name": r["character_name"],
                "planet_id": None, "location": None, "hours_left": None, "runs": done_count,
                # Which products finished + their run counts — the notification layer turns this into
                # a "sell it to a local buy order for more than hauling to Jita" hint (local_sell_hint).
                "products": [{"type_id": tid, "runs": runs} for tid, runs in done_runs.items()],
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
    by compute_alerts() (the schedule_sync alert, mute-aware) and planner_dashboard.dashboard()'s
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


def compute_alerts(context_id: int, rows=None, now: float | None = None) -> list[dict]:
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
