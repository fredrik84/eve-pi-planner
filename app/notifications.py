"""Notification support: background scheduler + channel settings + event logic.

Supported channels: Pushover, ntfy.sh, Discord webhook (see notifiers.py).
Events: extractor_expiry, factory_refill.

The scheduler runs check_and_send_notifications() every 15 minutes. It uses
only data already in the DB (no ESI calls), so it works between rescans and
adds no EVE API load.
"""
import json as _json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.esi import require_context, session_context_id
from app.notifiers import make_notifier, CHANNEL_LABELS

log = logging.getLogger(__name__)
router = APIRouter()

# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_notification_tables():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_notification_settings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id INTEGER NOT NULL,
            channel    TEXT    NOT NULL,
            config     TEXT    NOT NULL,
            enabled    INTEGER DEFAULT 1,
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_notification_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id INTEGER NOT NULL,
            channel    TEXT    NOT NULL,
            event      TEXT    NOT NULL,
            character  TEXT,
            planet_id  INTEGER,
            sent_at    TEXT    DEFAULT CURRENT_TIMESTAMP,
            status     TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_notification_prefs (
            context_id        INTEGER PRIMARY KEY,
            lead_hours        INTEGER DEFAULT 4,
            notify_extractors INTEGER DEFAULT 1,
            notify_factories  INTEGER DEFAULT 1,
            notify_submissions INTEGER DEFAULT 0,
            notify_bugs        INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.commit()
    con.close()


def _get_prefs(con, context_id: int) -> dict:
    row = con.execute(
        "SELECT lead_hours, notify_extractors, notify_factories "
        "FROM pp_notification_prefs WHERE context_id=?",
        (context_id,),
    ).fetchone()
    if row:
        return {
            "lead_hours": row["lead_hours"] or 4,
            "notify_extractors": bool(row["notify_extractors"]),
            "notify_factories": bool(row["notify_factories"]),
        }
    return {
        "lead_hours": 4,
        "notify_extractors": True,
        "notify_factories": True,
    }


def _recently_notified(con, context_id: int, event: str, planet_id: int, cooldown_h: float) -> bool:
    cutoff = time.time() - cooldown_h * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    row = con.execute(
        "SELECT 1 FROM pp_notification_log "
        "WHERE context_id=? AND event=? AND planet_id=? AND sent_at > ? AND status='ok'",
        (context_id, event, planet_id, cutoff_iso),
    ).fetchone()
    return row is not None


def _log_send(con, context_id: int, channel: str, event: str, character: str | None,
              planet_id: int | None, status: str):
    con.execute(
        "INSERT INTO pp_notification_log (context_id, channel, event, character, planet_id, sent_at, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (context_id, channel, event, character, planet_id,
         datetime.now(timezone.utc).isoformat(), status),
    )


# ── Event detection ───────────────────────────────────────────────────────────

def _extractor_events(con, context_id: int, lead_hours: float) -> list[dict]:
    """Return extractor planets expiring within lead_hours from now."""
    now = time.time()
    deadline = now + lead_hours * 3600
    rows = con.execute(
        """
        SELECT cy.character_id, cy.planet_id,
               cy.install_ts, cy.prog_days,
               ch.character_name,
               COALESCE(ss.name, '') AS system_name
        FROM pp_colony_yield cy
        JOIN pp_characters ch ON ch.character_id = cy.character_id
        LEFT JOIN pp_char_planets cp
               ON cp.character_id = cy.character_id AND cp.planet_id = cy.planet_id
        LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
        WHERE ch.context_id = ?
          AND cy.install_ts = (
              SELECT MAX(cy2.install_ts) FROM pp_colony_yield cy2
              WHERE cy2.character_id = cy.character_id AND cy2.planet_id = cy.planet_id
          )
          AND cy.prog_days IS NOT NULL
        """,
        (context_id,),
    ).fetchall()
    events = []
    for r in rows:
        expiry = r["install_ts"] + r["prog_days"] * 86400
        if now < expiry <= deadline:
            hours_left = (expiry - now) / 3600
            events.append({
                "character_id": r["character_id"],
                "character_name": r["character_name"],
                "planet_id": r["planet_id"],
                "system_name": r["system_name"],
                "hours_left": hours_left,
            })
    return events


def _factory_events(con, context_id: int, lead_hours: float) -> list[dict]:
    """Return factory planets due for refill within lead_hours from now."""
    # Fetch factory_refill_hours from the most recent snapshot for this context.
    snap_row = con.execute(
        "SELECT snapshot FROM pp_plan_snapshots WHERE context_id=? ORDER BY created_at DESC LIMIT 1",
        (context_id,),
    ).fetchone()
    if not snap_row:
        return []
    try:
        snap = _json.loads(snap_row["snapshot"])
        refill_hours = snap.get("factory_refill_hours")
    except Exception:
        return []
    if not refill_hours or refill_hours <= 0:
        return []

    now = time.time()
    deadline = now + lead_hours * 3600
    rows = con.execute(
        """
        SELECT cp.character_id, cp.planet_id, cp.scanned_at,
               ch.character_name,
               COALESCE(ss.name, '') AS system_name
        FROM pp_char_planets cp
        JOIN pp_characters ch ON ch.character_id = cp.character_id
        LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
        WHERE ch.context_id = ?
          AND cp.is_extractor = 0
          AND cp.scanned_at IS NOT NULL
        """,
        (context_id,),
    ).fetchall()
    events = []
    for r in rows:
        due = r["scanned_at"] + refill_hours * 3600
        if now < due <= deadline:
            hours_left = (due - now) / 3600
            events.append({
                "character_id": r["character_id"],
                "character_name": r["character_name"],
                "planet_id": r["planet_id"],
                "system_name": r["system_name"],
                "hours_left": hours_left,
            })
    return events




# ── Background job ────────────────────────────────────────────────────────────

def check_and_send_notifications():
    """Run every 15 minutes. Pure DB math — no ESI calls."""
    try:
        ensure_notification_tables()
        con = get_connection()
        # Contexts with at least one enabled notification setting.
        ctx_rows = con.execute(
            "SELECT DISTINCT context_id FROM pp_notification_settings WHERE enabled=1"
        ).fetchall()
        if not ctx_rows:
            con.close()
            return

        for ctx_row in ctx_rows:
            ctx = ctx_row["context_id"]
            try:
                _process_context(con, ctx)
            except Exception:
                log.exception("Notification error for context %s", ctx)

        con.commit()
        con.close()
    except Exception:
        log.exception("check_and_send_notifications failed")


def _process_context(con, context_id: int):
    prefs = _get_prefs(con, context_id)
    lead = prefs["lead_hours"]

    settings = con.execute(
        "SELECT id, channel, config FROM pp_notification_settings "
        "WHERE context_id=? AND enabled=1",
        (context_id,),
    ).fetchall()
    if not settings:
        return

    ext_evs: list[dict] = []
    fac_evs: list[dict] = []
    if prefs["notify_extractors"]:
        for ev in _extractor_events(con, context_id, lead):
            ev["event"] = "extractor_expiry"
            ev["cooldown_h"] = 2.0
            if not _recently_notified(con, context_id, ev["event"], ev["planet_id"], ev["cooldown_h"]):
                ext_evs.append(ev)
    if prefs["notify_factories"]:
        for ev in _factory_events(con, context_id, lead):
            ev["event"] = "factory_refill"
            ev["cooldown_h"] = 4.0
            if not _recently_notified(con, context_id, ev["event"], ev["planet_id"], ev["cooldown_h"]):
                fac_evs.append(ev)

    for evs in (ext_evs, fac_evs):
        if not evs:
            continue
        title, body = _format_batch(evs)
        for setting in settings:
            status = "ok"
            try:
                cfg = _json.loads(setting["config"])
                notifier = make_notifier(setting["channel"], cfg)
                notifier.send(title, body)
                log.info("Notification sent: %s → %s (%d events)", setting["channel"], evs[0]["event"], len(evs))
            except Exception as exc:
                status = f"error: {exc}"
                log.warning("Notification send failed (%s/%s): %s", setting["channel"], evs[0]["event"], exc)
            for ev in evs:
                _log_send(con, context_id, setting["channel"], ev["event"],
                          ev["character_name"], ev["planet_id"], status)


def _format_batch(evs: list[dict]) -> tuple[str, str]:
    n = len(evs)
    evs_sorted = sorted(evs, key=lambda e: e["hours_left"])
    if evs[0]["event"] == "extractor_expiry":
        title = "Extractor expiring" if n == 1 else f"{n} extractors expiring"
        lines = [f"~{round(e['hours_left'], 1)}h — {e.get('character_name', '?')} · {e.get('system_name', '?')}" for e in evs_sorted]
    else:
        title = "Factory refill due" if n == 1 else f"{n} factories due for refill"
        lines = [f"~{round(e['hours_left'], 1)}h — {e.get('character_name', '?')} · {e.get('system_name', '?')} (estimate)" for e in evs_sorted]
    return title, "\n".join(lines)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def make_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    sched = AsyncIOScheduler()
    sched.add_job(check_and_send_notifications, "interval", minutes=15, id="notify_check")
    return sched


# ── API ────────────────────────────────────────────────────────────────────────

class NotificationSettingCreate(BaseModel):
    channel: str
    config: dict
    enabled: bool = True


class NotificationPrefsUpdate(BaseModel):
    lead_hours: int
    notify_extractors: bool
    notify_factories: bool


class NotificationTestRequest(BaseModel):
    channel: str
    config: dict


@router.get("/api/notifications/settings")
def get_notification_settings(ctx: int = Depends(require_context)):
    ensure_notification_tables()
    con = get_connection()
    rows = con.execute(
        "SELECT id, channel, config, enabled, created_at "
        "FROM pp_notification_settings WHERE context_id=? ORDER BY created_at",
        (ctx,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            cfg = _json.loads(r["config"])
        except Exception:
            cfg = {}
        # Mask sensitive values before returning
        out.append({
            "id": r["id"],
            "channel": r["channel"],
            "channel_label": CHANNEL_LABELS.get(r["channel"], r["channel"]),
            "config_preview": _config_preview(r["channel"], cfg),
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
        })
    return {"settings": out}


def _config_preview(channel: str, cfg: dict) -> str:
    """Human-readable summary of a channel config, no secrets."""
    if channel == "pushover":
        key = cfg.get("user_key", "")
        return f"user …{key[-4:]}" if len(key) > 4 else "configured"
    if channel == "ntfy":
        topic = cfg.get("topic", "")
        server = cfg.get("server", "ntfy.sh")
        return f"{server}/{topic}" if topic else "configured"
    if channel == "discord":
        url = cfg.get("webhook_url", "")
        return f"webhook …{url[-12:]}" if len(url) > 12 else "configured"
    return "configured"


@router.post("/api/notifications/settings")
def create_notification_setting(req: NotificationSettingCreate, ctx: int = Depends(require_context)):
    if req.channel not in CHANNEL_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown channel: {req.channel}")
    ensure_notification_tables()
    con = get_connection()
    new_id = con.execute(
        "INSERT INTO pp_notification_settings (context_id, channel, config, enabled) VALUES (?,?,?,?) RETURNING id",
        (ctx, req.channel, _json.dumps(req.config), 1 if req.enabled else 0),
    ).fetchone()[0]
    con.commit()
    con.close()
    return {"ok": True, "id": new_id}


@router.patch("/api/notifications/settings/{setting_id}")
def toggle_notification_setting(setting_id: int, ctx: int = Depends(require_context)):
    ensure_notification_tables()
    con = get_connection()
    row = con.execute(
        "SELECT enabled FROM pp_notification_settings WHERE id=? AND context_id=?",
        (setting_id, ctx),
    ).fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404)
    new_val = 0 if row["enabled"] else 1
    con.execute(
        "UPDATE pp_notification_settings SET enabled=? WHERE id=? AND context_id=?",
        (new_val, setting_id, ctx),
    )
    con.commit()
    con.close()
    return {"ok": True, "enabled": bool(new_val)}


@router.delete("/api/notifications/settings/{setting_id}")
def delete_notification_setting(setting_id: int, ctx: int = Depends(require_context)):
    ensure_notification_tables()
    con = get_connection()
    con.execute(
        "DELETE FROM pp_notification_settings WHERE id=? AND context_id=?",
        (setting_id, ctx),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.get("/api/notifications/prefs")
def get_notification_prefs(ctx: int = Depends(require_context)):
    ensure_notification_tables()
    con = get_connection()
    prefs = _get_prefs(con, ctx)
    con.close()
    return prefs


@router.put("/api/notifications/prefs")
def update_notification_prefs(req: NotificationPrefsUpdate, ctx: int = Depends(require_context)):
    if req.lead_hours < 1 or req.lead_hours > 72:
        raise HTTPException(status_code=400, detail="lead_hours must be 1–72")
    ensure_notification_tables()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_notification_prefs "
        "(context_id, lead_hours, notify_extractors, notify_factories) "
        "VALUES (?,?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
        "lead_hours=excluded.lead_hours, notify_extractors=excluded.notify_extractors, "
        "notify_factories=excluded.notify_factories",
        (ctx, req.lead_hours,
         1 if req.notify_extractors else 0, 1 if req.notify_factories else 0),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.post("/api/notifications/test")
def test_notification(req: NotificationTestRequest, ctx: int = Depends(require_context)):
    if req.channel not in CHANNEL_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown channel: {req.channel}")
    try:
        notifier = make_notifier(req.channel, req.config)
        notifier.send("EVE PI Planner test", "Your notification channel is working.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.get("/api/notifications/log")
def get_notification_log(ctx: int = Depends(require_context)):
    ensure_notification_tables()
    con = get_connection()
    rows = con.execute(
        "SELECT channel, event, character, planet_id, sent_at, status "
        "FROM pp_notification_log WHERE context_id=? ORDER BY sent_at DESC LIMIT 40",
        (ctx,),
    ).fetchall()
    con.close()
    return {"log": [dict(r) for r in rows]}
