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
from app.esi import require_context, session_context_id, ADMIN_CHARACTERS
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
    # Migrate existing rows that lack the admin columns
    for col in ("notify_submissions", "notify_bugs"):
        try:
            con.execute(f"ALTER TABLE pp_notification_prefs ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass
    con.commit()
    con.close()


def _get_prefs(con, context_id: int) -> dict:
    row = con.execute(
        "SELECT lead_hours, notify_extractors, notify_factories, "
        "notify_submissions, notify_bugs "
        "FROM pp_notification_prefs WHERE context_id=?",
        (context_id,),
    ).fetchone()
    if row:
        return {
            "lead_hours": row["lead_hours"] or 4,
            "notify_extractors": bool(row["notify_extractors"]),
            "notify_factories": bool(row["notify_factories"]),
            "notify_submissions": bool(row["notify_submissions"]),
            "notify_bugs": bool(row["notify_bugs"]),
        }
    return {
        "lead_hours": 4,
        "notify_extractors": True,
        "notify_factories": True,
        "notify_submissions": False,
        "notify_bugs": False,
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


def _is_admin_context(con, context_id: int) -> bool:
    """True if any character in this context is an admin (bootstrap or DB)."""
    try:
        db_admins = {
            r["character_name"].lower()
            for r in con.execute("SELECT character_name FROM pp_admins").fetchall()
        }
    except Exception:
        db_admins = set()
    all_admins = {n.lower() for n in ADMIN_CHARACTERS} | db_admins
    rows = con.execute(
        "SELECT character_name FROM pp_characters WHERE context_id=?", (context_id,)
    ).fetchall()
    return any(r["character_name"].lower() in all_admins for r in rows)


def _submission_events(con) -> list[dict]:
    """New pending planet submissions created in the last 20 minutes."""
    rows = con.execute(
        "SELECT id, submitted_at FROM pp_planet_submissions "
        "WHERE status='pending' AND submitted_at > datetime('now', '-20 minutes')"
    ).fetchall()
    return [{"item_id": r["id"], "event": "submission_new",
             "cooldown_h": 168.0, "info": f"submission #{r['id']}"} for r in rows]


def _bug_events(con) -> list[dict]:
    """New open bug reports created in the last 20 minutes."""
    rows = con.execute(
        "SELECT id, created_at, title FROM pp_bugs "
        "WHERE status='open' AND created_at > datetime('now', '-20 minutes')"
    ).fetchall()
    return [{"item_id": r["id"], "event": "bug_new",
             "cooldown_h": 168.0, "info": r["title"] or f"bug #{r['id']}"} for r in rows]


def _recently_notified_item(con, context_id: int, event: str, item_id: int) -> bool:
    row = con.execute(
        "SELECT 1 FROM pp_notification_log "
        "WHERE context_id=? AND event=? AND planet_id=? AND status='ok'",
        (context_id, event, item_id),
    ).fetchone()
    return row is not None


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

    events: list[dict] = []
    if prefs["notify_extractors"]:
        for ev in _extractor_events(con, context_id, lead):
            ev["event"] = "extractor_expiry"
            ev["cooldown_h"] = 2.0
            events.append(ev)
    if prefs["notify_factories"]:
        for ev in _factory_events(con, context_id, lead):
            ev["event"] = "factory_refill"
            ev["cooldown_h"] = 4.0
            events.append(ev)

    for ev in events:
        if _recently_notified(con, context_id, ev["event"], ev["planet_id"], ev["cooldown_h"]):
            continue
        title, body = _format_event(ev)
        for setting in settings:
            try:
                cfg = _json.loads(setting["config"])
                notifier = make_notifier(setting["channel"], cfg)
                notifier.send(title, body)
                _log_send(con, context_id, setting["channel"], ev["event"],
                          ev["character_name"], ev["planet_id"], "ok")
                log.info("Notification sent: %s → %s / %s", setting["channel"], ev["event"], ev.get("system_name"))
            except Exception as exc:
                _log_send(con, context_id, setting["channel"], ev["event"],
                          ev["character_name"], ev["planet_id"], f"error: {exc}")
                log.warning("Notification send failed (%s/%s): %s", setting["channel"], ev["event"], exc)

    # Admin-only events (planet submissions, bug reports)
    if not _is_admin_context(con, context_id):
        return
    admin_events: list[dict] = []
    if prefs.get("notify_submissions"):
        admin_events.extend(_submission_events(con))
    if prefs.get("notify_bugs"):
        admin_events.extend(_bug_events(con))

    for ev in admin_events:
        if _recently_notified_item(con, context_id, ev["event"], ev["item_id"]):
            continue
        title, body = _format_admin_event(ev)
        for setting in settings:
            try:
                cfg = _json.loads(setting["config"])
                notifier = make_notifier(setting["channel"], cfg)
                notifier.send(title, body)
                _log_send(con, context_id, setting["channel"], ev["event"],
                          None, ev["item_id"], "ok")
                log.info("Admin notification sent: %s → %s #%s", setting["channel"], ev["event"], ev["item_id"])
            except Exception as exc:
                _log_send(con, context_id, setting["channel"], ev["event"],
                          None, ev["item_id"], f"error: {exc}")
                log.warning("Admin notification failed (%s/%s): %s", setting["channel"], ev["event"], exc)


def _format_admin_event(ev: dict) -> tuple[str, str]:
    if ev["event"] == "submission_new":
        return "New planet submission", f"A pilot submitted planet data for review — {ev['info']}"
    return "New bug report", f"A new bug was filed — {ev['info']}"


def _format_event(ev: dict) -> tuple[str, str]:
    hours = round(ev["hours_left"], 1)
    name = ev.get("character_name") or "unknown"
    sys = ev.get("system_name") or "unknown system"
    if ev["event"] == "extractor_expiry":
        title = "Extractor expiring"
        body = f"Extractor expires in ~{hours}h — {name} · {sys}"
    else:
        title = "Factory refill due"
        body = f"Factory pads due for refill in ~{hours}h — {name} · {sys} (estimate based on last scan)"
    return title, body


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
    notify_submissions: bool = False
    notify_bugs: bool = False


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
        "(context_id, lead_hours, notify_extractors, notify_factories, notify_submissions, notify_bugs) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
        "lead_hours=excluded.lead_hours, notify_extractors=excluded.notify_extractors, "
        "notify_factories=excluded.notify_factories, "
        "notify_submissions=excluded.notify_submissions, notify_bugs=excluded.notify_bugs",
        (ctx, req.lead_hours,
         1 if req.notify_extractors else 0, 1 if req.notify_factories else 0,
         1 if req.notify_submissions else 0, 1 if req.notify_bugs else 0),
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
