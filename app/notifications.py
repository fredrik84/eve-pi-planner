"""Notification support: background scheduler + channel settings + event logic.

Supported channels: Pushover, ntfy.sh, Discord webhook (see notifiers.py).
Events: the same 12 alert kinds the Dashboard shows (app.alert_settings.ALERT_KINDS) — expired,
expiring, storage_full, factory_refill, ext_unrouted, fac_unfed, fac_output, p0_mismatch,
schedule_sync, reaction_finishing_soon, reaction_completed, reaction_stage_ready. Event
detection itself lives in
app.alerts.compute_alerts(); this
module is purely a consumer (kind/severity filtering, cooldown, batching, sending) — it does not
implement its own detection, so a push notification and what's shown on the Dashboard can never
drift apart.

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

from app.sde import get_connection, ensure_once
from app.esi import require_context, session_context_id
from app.notifiers import make_notifier, CHANNEL_LABELS
from app.alert_settings import ALERT_KINDS
from app.alerts import compute_alerts
from app.features import feature_enabled_for

log = logging.getLogger(__name__)
router = APIRouter()

_VALID_ALERT_KINDS = {k["key"] for k in ALERT_KINDS}

# ── DB helpers ────────────────────────────────────────────────────────────────

@ensure_once
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
    for col, ddl in (("notify_kinds", "TEXT"), ("min_severity", "TEXT")):
        try:
            con.execute(f"ALTER TABLE pp_notification_prefs ADD COLUMN {col} {ddl}")
            con.commit()
        except Exception:
            pass
    # One-time migration for pre-existing rows (notify_kinds still NULL): derive from the old
    # notify_extractors/notify_factories booleans, so an account that had already muted e.g.
    # factory refills doesn't suddenly get pinged for it. Deliberately does NOT auto-enable the
    # new kinds this unification adds (storage_full, ext_unrouted, ...) for already-configured
    # accounts — least-surprise, don't silently expand what an already-tuned account gets pinged
    # about. Brand new contexts (no row at all) get "everything on" via _get_prefs's default,
    # matching the old out-of-the-box behavior (notify_extractors=1, notify_factories=1).
    rows = con.execute(
        "SELECT context_id, notify_extractors, notify_factories "
        "FROM pp_notification_prefs WHERE notify_kinds IS NULL"
    ).fetchall()
    for r in rows:
        kinds = []
        if r["notify_extractors"]:
            kinds += ["expired", "expiring"]
        if r["notify_factories"]:
            kinds += ["factory_refill"]
        con.execute(
            "UPDATE pp_notification_prefs SET notify_kinds=?, "
            "min_severity=COALESCE(min_severity,'warn') WHERE context_id=?",
            (_json.dumps(kinds), r["context_id"]),
        )
    con.commit()
    con.close()


def _get_prefs(con, context_id: int) -> dict:
    row = con.execute(
        "SELECT notify_kinds, min_severity FROM pp_notification_prefs WHERE context_id=?",
        (context_id,),
    ).fetchone()
    if row and row["notify_kinds"] is not None:
        try:
            kinds = [k for k in _json.loads(row["notify_kinds"]) if k in _VALID_ALERT_KINDS]
        except Exception:
            kinds = []
        return {
            "notify_kinds": kinds,
            "min_severity": row["min_severity"] if row["min_severity"] in ("warn", "high") else "warn",
        }
    # No row at all — brand new context, default to everything on (matches the old
    # notify_extractors=1/notify_factories=1 out-of-the-box default).
    return {"notify_kinds": [k["key"] for k in ALERT_KINDS], "min_severity": "warn"}


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


# ── Background job ────────────────────────────────────────────────────────────

# Next in the sequence after build_sde.py (918_273_645) / populate_geo.py (918_273_646).
_NOTIFY_ADVISORY_LOCK_KEY = 918_273_647

# Correctness-based kinds (and schedule_sync, which is just as persistent — a drifted extractor
# stays drifted until the player manually reseats its program) are structural problems, not
# time-decaying like an expiring extraction, so they get a much longer cooldown to avoid nagging
# about the same unfixed issue every 15-minute scheduler tick.
_COOLDOWN_HOURS = {
    "expired": 2.0, "expiring": 2.0, "storage_full": 2.0, "factory_refill": 4.0,
    "ext_unrouted": 24.0, "fac_unfed": 24.0, "fac_output": 24.0, "p0_mismatch": 24.0,
    "schedule_sync": 24.0,
    # Time-decaying like expiring/storage_full (gets more urgent as the job's end_date
    # approaches, and resolves itself once refilled) — same short cooldown, not the 24h
    # "persistent structural problem" category.
    "reaction_finishing_soon": 2.0,
    # A finished-but-uncollected reaction stays "done" until you collect + restart it, so re-nag
    # every 4h (like factory_refill) — long enough not to spam, short enough to remind you to reseat
    # the slot so it isn't sitting idle.
    "reaction_completed": 4.0,
    # A ready stage stays ready until the player installs it, so this is a "persistent state"
    # kind rather than a decaying one — nag rarely. Its dedupe_id is per (character, chain,
    # stage), so a SECOND chain reaching its own stage 2 still pings inside the window.
    "reaction_stage_ready": 12.0,
}

# ── Backing off on an alert nobody is acting on ───────────────────────────────────────────────
#
# The cooldowns above are the FIRST repeat's interval, not every repeat's. An alert that has been
# sent five times and not acted on is not made more useful by a sixth: five pings did not move the
# user, and the sixth costs the credibility of every other alert. So the interval doubles per
# consecutive send, capped.
#
# Why the cap is 12h: the user's own framing — an alert that fires while they are asleep or AFK is
# read whenever they next look, so the value of a repeat is in it being there at all, not in it
# being prompt. Two a day is enough to keep a real problem in view.
#
# **The first send is never delayed by any of this** — `_recently_notified` only suppresses a send
# when one already went out inside the window, so a newly-detected problem always fires at once.
# Backing off changes repeats and nothing else.
_BACKOFF_CAP_H = 12.0
# The scheduler ticks every 15 min and sends on the first tick AFTER the cooldown elapses, so a gap
# legitimately runs up to one tick long. Anything beyond this slack means no tick found the alert
# present — i.e. it had resolved and later came back, which starts a fresh chain.
_BACKOFF_SLACK_H = 0.5
_BACKOFF_MAX_ROWS = 24        # far past the cap; bounds the per-alert query


def _consecutive_cooldown_h(con, context_id: int, event: str, planet_id, base_h: float) -> float:
    """How long to wait before repeating THIS alert, given how many times it has already repeated.

    Derived from `pp_notification_log` rather than stored: the log already records every send, and a
    counter column would be a second source of truth that drifts the first time a send fails or a
    row is pruned. The chain is read oldest-to-newest, and a gap longer than the interval then in
    force means the alert stopped being true in between — so it resets, and a problem that genuinely
    recurs is never quietly demoted to the slowest rung.
    """
    rows = con.execute(
        "SELECT sent_at FROM pp_notification_log "
        "WHERE context_id=? AND event=? AND planet_id=? AND status='ok' "
        "ORDER BY sent_at DESC LIMIT ?",
        (context_id, event, planet_id, _BACKOFF_MAX_ROWS),
    ).fetchall()
    stamps = []
    for r in reversed(rows):                       # oldest first
        try:
            stamps.append(datetime.fromisoformat(r["sent_at"]).timestamp())
        except Exception:
            continue                               # an unparseable row breaks no chain it isn't in
    rung = 0
    for i in range(1, len(stamps)):
        expected = min(base_h * (2 ** rung), _BACKOFF_CAP_H)
        gap_h = (stamps[i] - stamps[i - 1]) / 3600.0
        rung = 0 if gap_h > expected + _BACKOFF_SLACK_H else rung + 1
    return min(base_h * (2 ** rung), _BACKOFF_CAP_H)


def check_and_send_notifications():
    """Run every 15 minutes. Pure DB math — no ESI calls.

    Guarded by a Postgres advisory lock (same pattern as scripts/build_sde.py /
    populate_geo.py). Every uvicorn worker in every pod replica runs its own APScheduler on its
    own 15-minute timer, and prod alone is 2 replicas x 3 workers = 6 independent processes
    sharing this DB (plus the dev environment, which shares the same Postgres AND the same
    discord-webhook secret) — with no lock, several of those timers landing close together could
    all pass the same _recently_notified() check before any of them committed their own send,
    producing real duplicate pings (confirmed live: a user got 3 identical extractor-expiry
    notifications). The lock serializes runs; a process that blocks waiting for it sees the
    winner's already-committed log rows once it acquires the lock, so _recently_notified()
    correctly skips instead of racing.
    """
    from app.db import _IS_POSTGRES
    con = get_connection()
    try:
        if _IS_POSTGRES:
            con.execute("SELECT pg_advisory_lock(?)", (_NOTIFY_ADVISORY_LOCK_KEY,))
        try:
            ensure_notification_tables()
            # Contexts with at least one enabled notification setting.
            ctx_rows = con.execute(
                "SELECT DISTINCT context_id FROM pp_notification_settings WHERE enabled=1"
            ).fetchall()
            for ctx_row in ctx_rows:
                ctx = ctx_row["context_id"]
                try:
                    _process_context(con, ctx)
                except Exception:
                    log.exception("Notification error for context %s", ctx)
            con.commit()
        finally:
            if _IS_POSTGRES:
                con.execute("SELECT pg_advisory_unlock(?)", (_NOTIFY_ADVISORY_LOCK_KEY,))
                con.commit()
    except Exception:
        log.exception("check_and_send_notifications failed")
    finally:
        con.close()


def _due_alerts(con, context_id: int, notify_kinds: set, min_severity: str,
                smart: bool) -> dict[str, list[dict]]:
    """The alerts that are true right now AND out of cooldown, grouped by kind.

    Run twice per tick when the rescan is on — once to decide what is worth an ESI call, once
    against the refreshed data to decide what is worth sending.
    """
    by_kind: dict[str, list[dict]] = {}
    for a in compute_alerts(context_id):
        if a["kind"] not in notify_kinds:
            continue
        if min_severity == "high" and a["severity"] != "high":
            continue
        base_h = _COOLDOWN_HOURS.get(a["kind"], 2.0)
        # What identifies THIS occurrence for cooldown purposes. A colony alert is about a planet;
        # a reactions alert is not, and used to fall through with no key at all — so every kind
        # with `planet_id: None` re-sent on all six schedulers' 15-minute ticks, forever. An alert
        # may now name its own key (`dedupe_id`), and only a genuinely keyless alert skips the
        # check.
        key = a.get("dedupe_id", a.get("planet_id"))
        cooldown_h = (_consecutive_cooldown_h(con, context_id, a["kind"], key, base_h)
                      if (smart and key is not None) else base_h)
        if key is not None and _recently_notified(con, context_id, a["kind"], key, cooldown_h):
            continue
        a["_dedupe_key"] = key
        by_kind.setdefault(a["kind"], []).append(a)
    return by_kind


class _ScanOutcome:
    """What the rescan pass did, so the second alert pass knows what it may trust.

    `suppressed` holds (character_id, planet_id) pairs whose data could NOT be refreshed. Those
    alerts are held back rather than sent: the user's call, and the right one — a colony we failed
    to read is a colony we have nothing current to say about, and the character page already shows
    a dead token as the thing to fix.
    """
    __slots__ = ("scanned", "suppressed")

    def __init__(self):
        self.scanned: set = set()
        self.suppressed: set = set()


# One ESI call per colony, and only for a colony an alert is actually about. The blanket
# alternative — scan every character on a timer — costs ~96 scans per character per day; this costs
# one per alert SEND, and sends back off geometrically, so an ignored problem is scanned about four
# times on day one and twice a day after. The budget below is the guarantee that does not depend on
# that estimate being right.
_SCAN_BUDGET_PER_TICK = 20
# A transient ESI failure (timeout, 5xx) leaves the token in place and tells us nothing, so retrying
# it every 15 minutes is the one way this could still hammer the API for no result.
_SCAN_RETRY_AFTER_H = 1.0


def _rescan_targets(con, context_id: int, by_kind: dict) -> list[tuple[int, int]]:
    """(character_id, planet_id) worth an ESI read this tick, in a stable order.

    Filters, in the order they cost least: alerts that name no colony, then colonies whose data is
    already as fresh as ESI will give (`esi_expires` in the future — a read before it is refused by
    the rule in CLAUDE.md and would tell us nothing anyway), then characters with a dead refresh
    token (`token_ok` false — a known-dead token is never worth a request), then characters whose
    last automatic scan failed recently.
    """
    wanted: list[tuple[int, int]] = []
    seen = set()
    for evs in by_kind.values():
        for a in evs:
            cid, pid = a.get("character_id"), a.get("planet_id")
            # Reaction kinds carry no planet — they read industry jobs, a different ESI path, and
            # are deliberately not in this first cut.
            if not cid or not pid or (cid, pid) in seen:
                continue
            seen.add((cid, pid))
            wanted.append((cid, pid))
    if not wanted:
        return []

    now = time.time()
    fresh_by_planet = {
        (r["character_id"], r["planet_id"]): r["esi_expires"]
        for r in con.execute(
            "SELECT character_id, planet_id, esi_expires FROM pp_char_planets "
            "WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)",
            (context_id,),
        )
    }
    chars = {
        r["character_id"]: r
        for r in con.execute(
            "SELECT character_id, refresh_token, is_dummy, scan_failed_at "
            "FROM pp_characters WHERE context_id=?",
            (context_id,),
        )
    }
    out = []
    for cid, pid in wanted:
        exp = fresh_by_planet.get((cid, pid))
        if exp and exp > now:
            continue                       # ESI has nothing newer; not stale, so not our problem
        c = chars.get(cid)
        if not c or c["is_dummy"] or not c["refresh_token"]:
            continue                       # placeholder, or a token the user must re-add first
        failed_at = c["scan_failed_at"] or 0
        if failed_at and now - failed_at < _SCAN_RETRY_AFTER_H * 3600:
            continue
        out.append((cid, pid))
    return out


def _default_scan(character_id: int, planet_id: int) -> bool:
    """Refresh one colony. True if the read succeeded and the DB now holds current data."""
    from app.esi import _get_valid_token, _fetch_planets
    token = _get_valid_token(character_id)
    if not token:
        return False
    _fetch_planets(character_id, token, only_planet_id=planet_id)
    return True


def _rescan_for_due_alerts(con, context_id: int, by_kind: dict, scan=None) -> _ScanOutcome:
    """Refresh the colonies the due alerts are about, so the alert is judged on current data.

    `scan` is injectable so the tests can exercise every branch of this without ESI.
    """
    scan = scan or _default_scan
    outcome = _ScanOutcome()
    targets = _rescan_targets(con, context_id, by_kind)
    for cid, pid in targets[:_SCAN_BUDGET_PER_TICK]:
        try:
            ok = scan(cid, pid)
        except Exception:
            log.warning("Alert rescan failed for character %s planet %s", cid, pid, exc_info=True)
            ok = False
        if ok:
            outcome.scanned.add((cid, pid))
            con.execute("UPDATE pp_characters SET scan_failed_at=NULL WHERE character_id=?", (cid,))
        else:
            outcome.suppressed.add((cid, pid))
            con.execute("UPDATE pp_characters SET scan_failed_at=? WHERE character_id=?",
                        (time.time(), cid))
    # Over budget is not a failure, but it IS unverified — hold those back and let the next tick
    # scan them. At 20 a tick that is ~1,900 colonies a day, so this is a brake, not a queue.
    for cid, pid in targets[_SCAN_BUDGET_PER_TICK:]:
        outcome.suppressed.add((cid, pid))
    con.commit()
    return outcome


def _drop_unverified(by_kind: dict, suppressed: set) -> dict:
    """Remove alerts about a colony we could not read. A kind left with no events disappears."""
    if not suppressed:
        return by_kind
    out = {}
    for kind, evs in by_kind.items():
        keep = [a for a in evs
                if (a.get("character_id"), a.get("planet_id")) not in suppressed]
        if keep:
            out[kind] = keep
    return out


def _process_context(con, context_id: int):
    prefs = _get_prefs(con, context_id)
    notify_kinds = set(prefs["notify_kinds"])
    if not notify_kinds:
        return
    min_severity = prefs["min_severity"]   # "warn" = everything, "high" = high only

    settings = con.execute(
        "SELECT id, channel, config FROM pp_notification_settings "
        "WHERE context_id=? AND enabled=1",
        (context_id,),
    ).fetchall()
    if not settings:
        return

    smart = feature_enabled_for("alert_rescan_backoff", context_id)
    by_kind = _due_alerts(con, context_id, notify_kinds, min_severity, smart)

    if smart and by_kind:
        # Everything above is a judgement about data the user may already have made stale by fixing
        # the problem in-game. Refresh what the due alerts are about, then ask again — an alert that
        # does not survive a fresh read was never worth sending.
        outcome = _rescan_for_due_alerts(con, context_id, by_kind)
        if outcome.scanned or outcome.suppressed:
            by_kind = _due_alerts(con, context_id, notify_kinds, min_severity, smart)
            by_kind = _drop_unverified(by_kind, outcome.suppressed)

    for kind, evs in by_kind.items():
        title, body = _format_batch(kind, evs)
        if kind == "reaction_completed":
            body += _reaction_completed_sale_hint(context_id, evs)
        if kind == "reaction_stage_ready":
            body = _stage_ready_body(evs)
        for setting in settings:
            status = "ok"
            try:
                cfg = _json.loads(setting["config"])
                notifier = make_notifier(setting["channel"], cfg)
                notifier.send(title, body, description=body)
                log.info("Notification sent: %s → %s (%d events)", setting["channel"], kind, len(evs))
            except Exception as exc:
                status = f"error: {exc}"
                log.warning("Notification send failed (%s/%s): %s", setting["channel"], kind, exc)
            for ev in evs:
                # Log under the SAME key the cooldown reads back (`_dedupe_key`), or the check
                # above would never find what was just sent.
                _log_send(con, context_id, setting["channel"], kind,
                          ev["character_name"], ev.get("_dedupe_key", ev["planet_id"]), status)


def _collapse_line(evs: list[dict], singular: str, plural: str) -> str:
    """'N extractors — Char ×6, Char2 ×6, ...' — the same tally-and-collapse style as the
    dashboard's issue cards. These events come in fleets (many planets sharing one schedule or
    one structural problem), so a per-planet itemization is the wrong shape here — a
    per-character count, sorted busiest-first, is what's actually actionable."""
    tally: dict[str, int] = {}
    for e in evs:
        name = e.get("character_name") or "?"
        tally[name] = tally.get(name, 0) + 1
    total = sum(tally.values())
    parts = ", ".join(f"{c} ×{n}" for c, n in sorted(tally.items(), key=lambda x: -x[1]))
    noun = singular if total == 1 else plural
    return f"{total} {noun} — {parts}"


# (title, noun-singular, noun-plural) per kind — mirrors the labels in app.alert_settings.ALERT_KINDS.
_KIND_LABELS = {
    "expired":        ("Extractions expired", "extractor", "extractors"),
    "expiring":       ("Extractions expiring soon", "extractor", "extractors"),
    "storage_full":   ("Storage filling up", "launchpad", "launchpads"),
    "factory_refill": ("Factories due for refill", "factory", "factories"),
    "ext_unrouted":   ("Extractor not routed", "extractor", "extractors"),
    "fac_unfed":      ("Factory has no input route", "factory", "factories"),
    "fac_output":     ("Factory output not routed", "factory", "factories"),
    "p0_mismatch":    ("Extracting something unused", "colony", "colonies"),
    "schedule_sync":  ("Extractor schedule out of sync", "extractor", "extractors"),
    "reaction_finishing_soon": ("Reactions finishing soon", "character", "characters"),
    "reaction_completed": ("Reactions completed", "character", "characters"),
    "reaction_stage_ready": ("Reaction stage ready to start", "stage", "stages"),
}


def _format_batch(kind: str, evs: list[dict]) -> tuple[str, str]:
    """Return (title, body) for a batch of same-kind events, matching the dashboard's
    aggregate style."""
    title, singular, plural = _KIND_LABELS.get(kind, (kind, "item", "items"))
    body = _collapse_line(evs, singular, plural)
    return title, body


def _isk(n: float) -> str:
    n = float(n)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.0f}k"
    return f"{n:.0f}"


def _stage_ready_body(evs: list[dict]) -> str:
    """"Stage 2 is ready to start on Alt — install Reinforced Carbon Fiber."

    Names the products, because the whole point of the ping is that the player can act on it
    without opening the site to find out what it was about.
    """
    lines = []
    for e in sorted(evs, key=lambda x: (x.get("character_name") or "", x.get("stage") or 0)):
        what = ", ".join(e.get("names") or []) or f"{e.get('runs', 0)} job(s)"
        lines.append(f"Stage {e.get('stage')} is ready on {e.get('character_name')} — "
                     f"everything it waits on has finished. Install {what}.")
    return "\n".join(lines)


def _reaction_completed_sale_hint(context_id: int, evs: list[dict]) -> str:
    """Extra body text for a 'reactions completed' push: for each finished product, if one of the
    account's followed local/alliance markets has a buy order that BEATS hauling the output to Jita
    (Jita buy price minus the jump-freight cost to get it there), tell the player how much they can
    sell there and the ISK they'd gain by not hauling. Read-only, opt-in (local_sell_hint flag), and
    a no-op for anyone with no followed markets — so it never fires for the common Jita-only user.
    Best-effort: any lookup failure just yields no hint rather than blocking the completion push."""
    try:
        from app.features import feature_enabled
        if not feature_enabled("local_sell_hint"):
            return ""
        from app.markets import effective_markets, best_local_buy
        if not effective_markets(context_id):
            return ""
        # Finished output units per product across the whole batch.
        runs_by_type: dict[int, int] = {}
        for ev in evs:
            for p in ev.get("products") or []:
                tid = p.get("type_id")
                if tid:
                    runs_by_type[tid] = runs_by_type.get(tid, 0) + (p.get("runs") or 0)
        if not runs_by_type:
            return ""
        tids = list(runs_by_type)

        from app.market import fetch_market_data
        from app.reactions.settings import effective_reaction_settings
        from app.sde import load_pi_data
        con = get_connection()
        try:
            out_qty = {r["output_type_id"]: r["output_qty"] for r in con.execute(
                "SELECT output_type_id, output_qty FROM reactions WHERE output_type_id IN (%s)"
                % ",".join("?" * len(tids)), tids)}
        finally:
            con.close()
        local = best_local_buy(context_id, tids)
        jita = fetch_market_data(tids)
        export_isk_per_m3 = effective_reaction_settings(context_id).get("export_isk_per_m3", 0.0) or 0.0
        types = load_pi_data()["types"]

        lines = []
        for tid, runs in runs_by_type.items():
            loc = local.get(tid)
            if not loc or not loc.get("buy_price"):
                continue
            units = runs * (out_qty.get(tid, 0.0) or 0.0)
            if units < 1:
                continue
            vol_per_unit = (types.get(tid, {}) or {}).get("volume", 0.0) or 0.0
            jita_buy = (jita.get(tid) or {}).get("buy_price", 0.0) or 0.0
            jita_net = jita_buy - vol_per_unit * export_isk_per_m3   # what Jita nets AFTER freight
            edge = loc["buy_price"] - jita_net
            if edge <= 0:
                continue
            sellable = min(units, loc.get("buy_volume", 0.0) or 0.0)
            if sellable < 1:
                continue
            extra = sellable * edge
            name = (types.get(tid, {}) or {}).get("name", str(tid))
            lines.append(f"  {int(sellable):,} {name} → {loc['market']} @ {_isk(loc['buy_price'])} "
                         f"(+{_isk(extra)} vs hauling to Jita)")
        if not lines:
            return ""
        return "\n\nSell locally instead of hauling to Jita:\n" + "\n".join(lines)
    except Exception:
        log.warning("local sell hint failed for context %s", context_id, exc_info=True)
        return ""


# ── Scheduler ─────────────────────────────────────────────────────────────────

def make_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.yield_stats import aggregate_colony_yields
    from app.reactions.jobs import log_all_reaction_completions
    from app.industry.jobs import log_all_manufacturing_completions
    from app.jobs import run_job
    sched = AsyncIOScheduler()

    # The scheduler starts in EVERY replica, so each job would otherwise fire once per pod. The
    # completion ledgers are idempotent (they upsert on job_id) but the notification check is not:
    # two pods can read the cooldown log simultaneously, both decide nothing was sent recently, and
    # both push. Wrapping each job in a database lease makes exactly one replica run it.
    def _leased(name, fn):
        return lambda: run_job(name, fn)

    sched.add_job(_leased("notify_check", check_and_send_notifications),
                  "interval", minutes=15, id="notify_check")
    sched.add_job(_leased("yield_aggregate", aggregate_colony_yields),
                  "cron", hour=3, id="yield_aggregate")
    # Forward-only reaction turnover/net-profit ledger — logs jobs that finished since the last tick.
    sched.add_job(_leased("reaction_completions", log_all_reaction_completions),
                  "interval", minutes=15, id="reaction_completions")
    # Same, for manufacturing jobs (activity 1).
    sched.add_job(_leased("manufacturing_completions", log_all_manufacturing_completions),
                  "interval", minutes=15, id="manufacturing_completions")
    return sched


# ── API ────────────────────────────────────────────────────────────────────────

class NotificationSettingCreate(BaseModel):
    channel: str
    config: dict
    enabled: bool = True


class NotificationPrefsUpdate(BaseModel):
    notify_kinds: list[str]
    min_severity: str


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
    return {**prefs, "available_kinds": ALERT_KINDS}


@router.put("/api/notifications/prefs")
def update_notification_prefs(req: NotificationPrefsUpdate, ctx: int = Depends(require_context)):
    unknown = [k for k in req.notify_kinds if k not in _VALID_ALERT_KINDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown alert kind(s): {', '.join(unknown)}")
    if req.min_severity not in ("warn", "high"):
        raise HTTPException(status_code=400, detail="min_severity must be 'warn' or 'high'")
    ensure_notification_tables()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_notification_prefs (context_id, notify_kinds, min_severity) VALUES (?,?,?) "
        "ON CONFLICT(context_id) DO UPDATE SET "
        "notify_kinds=excluded.notify_kinds, min_severity=excluded.min_severity",
        (ctx, _json.dumps(sorted(set(req.notify_kinds))), req.min_severity),
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


@router.post("/api/notifications/resend-last")
def resend_last_notification(ctx: int = Depends(require_context)):
    """Resend the most recent notification batch from the log to all saved channels."""
    ensure_notification_tables()
    con = get_connection()

    settings = con.execute(
        "SELECT channel, config FROM pp_notification_settings WHERE context_id=? AND enabled=1",
        (ctx,),
    ).fetchall()
    if not settings:
        con.close()
        raise HTTPException(status_code=400, detail="No enabled notification channels configured.")

    latest_row = con.execute(
        "SELECT MAX(sent_at) AS ts FROM pp_notification_log WHERE context_id=? AND status='ok'",
        (ctx,),
    ).fetchone()
    if not latest_row or not latest_row["ts"]:
        con.close()
        raise HTTPException(status_code=404, detail="No previous notifications found in log.")

    latest_ts = latest_row["ts"]
    # Match to the minute — all events in the same scheduler run land within seconds of each other.
    minute_prefix = latest_ts[:16]
    rows = con.execute(
        "SELECT DISTINCT event, character, planet_id FROM pp_notification_log "
        "WHERE context_id=? AND status='ok' AND sent_at >= ? ORDER BY event",
        (ctx, minute_prefix),
    ).fetchall()

    by_event: dict[str, list[dict]] = {}
    for r in rows:
        cp_row = con.execute(
            "SELECT ss.name AS system_name, cp.planet_num, cp.planet_type "
            "FROM pp_char_planets cp "
            "JOIN pp_characters ch ON ch.character_id = cp.character_id "
            "LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id "
            "WHERE ch.context_id=? AND cp.planet_id=? LIMIT 1",
            (ctx, r["planet_id"]),
        ).fetchone()
        by_event.setdefault(r["event"], []).append({
            "event": r["event"],
            "character_name": r["character"] or "?",
            "planet_id": r["planet_id"],
            "system_name": (cp_row["system_name"] if cp_row else "") or "?",
            "planet_num": cp_row["planet_num"] if cp_row else None,
            "planet_type": cp_row["planet_type"] if cp_row else "",
            "hours_left": 0.0,
        })
    con.close()

    if not by_event:
        raise HTTPException(status_code=404, detail="No previous notifications found.")

    sent = []
    errors = []
    for event_type, evs in by_event.items():
        n = len(evs)
        title, body = _format_batch(event_type, evs)
        title = f"[Replay] {title}"
        for setting in settings:
            try:
                cfg = _json.loads(setting["config"])
                notifier = make_notifier(setting["channel"], cfg)
                notifier.send(title, body, description=body)
            except Exception as exc:
                errors.append(f"{setting['channel']}: {exc}")
        if not errors:
            sent.append({"event": event_type, "count": n, "title": title})

    return {"sent": sent, "errors": errors}


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
