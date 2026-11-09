"""First-party PI flow counts. Only admins can read aggregate reports."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from app.db import ensure_once, get_connection
from app.esi import require_admin, require_context
from app.features import feature_enabled, feature_enabled_for

router = APIRouter()
UsageEvent = Literal["wizard_opened", "wizard_started", "wizard_recommendations",
                     "wizard_completed", "wizard_saved", "setup_imported", "colonies_rescanned"]
EVENTS = {
    "wizard_opened": "Planner opened",
    "wizard_started": "Find Systems requested",
    "wizard_recommendations": "System recommendations shown",
    "wizard_completed": "Final wizard plan shown",
    "wizard_saved": "Wizard plan saved",
    "setup_imported": "Existing setup imported as saved plans",
    "colonies_rescanned": "Character colonies rescanned",
}


@ensure_once
def ensure_usage_table():
    with get_connection() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS pp_usage_daily (
            day TEXT NOT NULL, context_id INTEGER NOT NULL, event TEXT NOT NULL,
            actions INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (day, context_id, event)
        )""")
        con.commit()


class UsageMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: UsageEvent


def prune_usage():
    # Runs independently of collection, so turning the feature off still expires data.
    ensure_usage_table()
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=89)).isoformat()
    with get_connection() as con:
        con.execute("DELETE FROM pp_usage_daily WHERE day < ?", (cutoff,))
        con.commit()


@router.post("/api/telemetry/usage", status_code=204)
def record_usage(req: UsageMeasurement, request: Request, ctx: int = Depends(require_context)):
    if not feature_enabled("pi_usage_stats"):
        return Response(status_code=204)
    if (request.headers.get("dnt") == "1" or request.headers.get("sec-gpc") == "1"
            or "prefetch" in (request.headers.get("purpose", "")
                              + request.headers.get("sec-purpose", "")).lower()):
        return Response(status_code=204)
    origin = request.headers.get("origin")
    if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Same-origin measurements only")
    ensure_usage_table()
    today = datetime.now(timezone.utc).date()
    with get_connection() as con:
        con.execute("DELETE FROM pp_usage_daily WHERE day < ?", ((today - timedelta(days=89)).isoformat(),))
        # Bounded daily counters avoid storing individual visits or event timelines.
        con.execute("""INSERT INTO pp_usage_daily (day, context_id, event, actions) VALUES (?, ?, ?, 1)
            ON CONFLICT (day, context_id, event) DO UPDATE SET actions=pp_usage_daily.actions+1
            WHERE pp_usage_daily.actions < 1000""", (today.isoformat(), ctx, req.event))
        con.commit()
    return Response(status_code=204)


def _report(con, start: str, end: str):
    rows = con.execute("""SELECT event, SUM(actions) AS actions, COUNT(DISTINCT context_id) AS accounts
        FROM pp_usage_daily WHERE day >= ? AND day <= ? GROUP BY event""", (start, end)).fetchall()
    by_event = {r["event"]: dict(r) for r in rows if r["event"] in EVENTS}
    accounts = con.execute("""SELECT context_id,
        MAX(CASE WHEN event='wizard_started' THEN 1 ELSE 0 END) AS wizard,
        MAX(CASE WHEN event='setup_imported' THEN 1 ELSE 0 END) AS imported
        FROM pp_usage_daily WHERE day >= ? AND day <= ? GROUP BY context_id""", (start, end)).fetchall()
    adoption = {"wizard_only": 0, "import_only": 0, "both": 0, "neither": 0}
    for r in accounts:
        key = "both" if r["wizard"] and r["imported"] else "wizard_only" if r["wizard"] else "import_only" if r["imported"] else "neither"
        adoption[key] += 1
    return {"rows": [{"event": event, "label": label,
                      "actions": by_event.get(event, {}).get("actions", 0),
                      "accounts": by_event.get(event, {}).get("accounts", 0)}
                     for event, label in EVENTS.items()], "adoption": adoption}


@router.get("/api/admin/usage")
def usage_stats(ctx: int = Depends(require_admin)):
    if not feature_enabled_for("pi_usage_stats", ctx):
        return {"visible": False}
    ensure_usage_table()
    today = datetime.now(timezone.utc).date()
    with get_connection() as con:
        return {"visible": True, "enabled": feature_enabled("pi_usage_stats"),
                "periods": {str(days): _report(con, (today - timedelta(days=days - 1)).isoformat(), today.isoformat())
                            for days in (7, 30)}}
