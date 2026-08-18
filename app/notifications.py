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

The scheduler runs check_and_send_notifications() every 15 minutes. With `alert_rescan_backoff`
off it uses only data already in the DB and adds no EVE API load; with it on, a due alert first
triggers a re-read of the one colony it is about, bounded by a whole-tick budget — see
docs/platform.md, "Check before nagging".
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
    # AFTER the commit above, never in the same transaction as the CREATE TABLEs. `CREATE INDEX IF
    # NOT EXISTS` has a known duplicate-key race when several workers start together, and the
    # Postgres cursor wrapper rolls the connection back on a failed statement — sharing the
    # transaction would let that race undo the table creation on a fresh DB while `@ensure_once`
    # recorded the module as initialised.
    #
    # Why the index: `_recently_notified`, `_consecutive_cooldown_h` and `_log_outcome`'s dedupe
    # each look up (context, event, key) ordered by time, once per due alert per 15-minute tick,
    # against a table nothing used to prune. That was a sequential scan on every one of them.
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_notif_log_lookup "
                    "ON pp_notification_log (context_id, event, planet_id, sent_at)")
        con.commit()
    except Exception:
        pass
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


# ── Recording the alerts that never became a send ─────────────────────────────
#
# `_log_send` runs inside the channel loop, so it only ever fires on a real send. That made the two
# outcomes the rescan exists to produce completely invisible: an alert it PREVENTED (re-read the
# colony, found the problem already fixed, sent nothing) and an alert it SUPPRESSED because nothing
# could be verified. The first is the whole benefit, the second is the whole cost, and neither is
# recoverable after the fact — unlike the backoff rung, which `_consecutive_cooldown_h` derives from
# the timestamps of sends that did happen.
#
# They go in `pp_notification_log` rather than a table of their own because every column already
# fits and the readers all filter on `status='ok'` (`_recently_notified`, `_consecutive_cooldown_h`,
# `resend-last`), so a new status value is invisible to them by construction. `/api/notifications/log`
# was the one reader without such a filter and now has one — the user's log is a list of things that
# were sent, and must stay that.
_OUTCOME_CHANNEL = "-"        # no channel was involved; the column is NOT NULL
_OUTCOME_STATUSES = ("prevented",)          # exact matches
_OUTCOME_PREFIXES = ("suppressed:",)        # `suppressed:<reason>`
# A dead token keeps its alert due on all 96 ticks of the day, so an unguarded suppressed row is 96
# rows a day per alert, forever. One row per window says the same thing. `prevented` needs no such
# guard: an alert that was found fixed does not come back on the next tick, and if it genuinely
# recurs later that IS a second occurrence worth its own row.
_OUTCOME_LOG_WINDOW_H = 12.0
# These rows are instrumentation, not history: nothing in the app reads them back and the user's
# log never shows them, so they are answered by "what happened last month", not "what happened in
# 2026". Sends are NOT pruned — `_consecutive_cooldown_h` reads them and they are the user's own
# record. Without this the outcome rows are the only part of this table that grows forever.
_OUTCOME_RETENTION_DAYS = 45


_last_prune_at = 0.0


def _prune_outcome_rows():
    """Delete instrumentation rows past the retention window. OWN CONNECTION, deliberately.

    Everything the tick sent is uncommitted until the single commit at the end of
    `check_and_send_notifications` — `_process_context` never commits — and the Postgres cursor
    wrapper rolls the connection back on any statement failure. Sharing the connection would mean
    a timeout on this DELETE (its predicate is on `sent_at` alone, which no index serves, so it is
    the likeliest statement in the tick to hit one) discarding the whole tick's send log AFTER the
    pushes had already gone out — and the next tick, finding nothing in the log, would send every
    alert for every account again. A pruning convenience must not be able to cause a duplicate
    notification storm.
    """
    # At most twice a day, not on all 96 ticks: a scan that deletes nothing 95 times out of 96 is
    # pure cost. Set only after the DELETE succeeds, so a failure is retried rather than hidden.
    global _last_prune_at
    if time.time() - _last_prune_at < 12 * 3600:
        return
    cutoff = datetime.fromtimestamp(
        time.time() - _OUTCOME_RETENTION_DAYS * 86400, tz=timezone.utc).isoformat()
    con = get_connection()
    try:
        con.execute(f"DELETE FROM pp_notification_log WHERE sent_at < ? AND NOT ({_SENDS_ONLY_SQL})",
                    (cutoff,))
        con.commit()
        _last_prune_at = time.time()
    except Exception:
        log.warning("Pruning notification outcome rows failed", exc_info=True)
    finally:
        con.close()


# The WHERE fragment that keeps sends and drops instrumentation, derived from the two tuples above
# so a new outcome status cannot be added without the user's log excluding it. Literal-safe: every
# value is a constant in this file, never anything from a request.
_SENDS_ONLY_SQL = " AND ".join(
    [f"COALESCE(status,'') <> '{s}'" for s in _OUTCOME_STATUSES]
    + [f"COALESCE(status,'') NOT LIKE '{p}%'" for p in _OUTCOME_PREFIXES]
)


def _is_outcome_status(status: str | None) -> bool:
    return bool(status) and (status in _OUTCOME_STATUSES
                             or status.startswith(_OUTCOME_PREFIXES))


def _log_outcome(con, context_id: int, event: str, character: str | None, key,
                 status: str, dedupe_h: float = 0.0):
    if dedupe_h and key is not None:
        cutoff = datetime.fromtimestamp(time.time() - dedupe_h * 3600, tz=timezone.utc).isoformat()
        seen = con.execute(
            "SELECT 1 FROM pp_notification_log "
            "WHERE context_id=? AND event=? AND planet_id=? AND status=? AND sent_at > ?",
            (context_id, event, key, status, cutoff),
        ).fetchone()
        if seen:
            return
    _log_send(con, context_id, _OUTCOME_CHANNEL, event, character, key, status)


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
_BACKOFF_MAX_ROWS = 72        # far past the cap even at several channels; bounds the query
# One send writes one log row PER ENABLED CHANNEL (`_log_send` is inside the channel loop), and
# those rows land microseconds apart. Counted naively, a user with three channels reached the 12h
# cap after their SECOND send and could never reset the chain, because a gap of microseconds is
# never longer than any interval. Rows this close together are one send, by construction — nothing
# real repeats inside a minute when the shortest base interval is two hours. (`resend-last` groups
# by the minute for the same reason.)
_SAME_SEND_WINDOW_S = 60


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
            ts = datetime.fromisoformat(r["sent_at"]).timestamp()
        except Exception:
            continue                               # an unparseable row breaks no chain it isn't in
        if stamps and ts - stamps[-1] < _SAME_SEND_WINDOW_S:
            continue                               # same send, another channel
        stamps.append(ts)
    rung = 0
    for i in range(1, len(stamps)):
        expected = min(base_h * (2 ** rung), _BACKOFF_CAP_H)
        gap_h = (stamps[i] - stamps[i - 1]) / 3600.0
        rung = 0 if gap_h > expected + _BACKOFF_SLACK_H else rung + 1
    return min(base_h * (2 ** rung), _BACKOFF_CAP_H)


def check_and_send_notifications():
    """Run every 15 minutes.

    NOT pure DB math since `alert_rescan_backoff`: a due alert triggers a bounded re-read of the one
    colony it is about (see `_rescan_for_due_alerts`). With the flag off it makes no ESI calls at
    all, which is what it always did.

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
            global _scan_budget_left
            _scan_budget_left = _SCAN_BUDGET_PER_TICK
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
            _prune_outcome_rows()      # after the commit, and on its own connection — see there
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

    `reasons` maps the same pairs to WHY, for the log. Four suppressions with four different causes
    read as one number otherwise, and "we are over budget every tick" and "this account's token has
    been dead for a week" call for opposite responses.
    """
    __slots__ = ("scanned", "suppressed", "reasons")

    def __init__(self):
        self.scanned: set = set()
        self.suppressed: set = set()
        self.reasons: dict = {}

    def suppress(self, pair, reason: str):
        self.suppressed.add(pair)
        self.reasons.setdefault(pair, reason)


# One ESI call per colony, and only for a colony an alert is actually about. The blanket
# alternative — scan every character on a timer — costs ~96 scans per character per day; this costs
# one per alert SEND, and sends back off geometrically, so an ignored problem is scanned about four
# times on day one and twice a day after. The budget below is the guarantee that does not depend on
# that estimate being right.
# Whole-TICK, not per context: `_process_context` runs once per context in a loop, so a per-context
# cap would silently multiply by the number of accounts with notifications on.
# `check_and_send_notifications` resets this at the top of each run.
#
# Honest about the scope: this is a module global, so the ceiling is per PROCESS, and prod runs 6
# (2 replicas x 3 workers). The advisory lock serializes them and `_recently_notified` empties the
# second runner's alert list before it reaches a scan, so the real spend stays near 20 — but the
# guarantee this constant makes on its own is 20 per process, not 20 per app.
_SCAN_BUDGET_PER_TICK = 20
_scan_budget_left = _SCAN_BUDGET_PER_TICK
# A transient ESI failure (timeout, 5xx) leaves the token in place and tells us nothing, so retrying
# it every 15 minutes is the one way this could still hammer the API for no result.
_SCAN_RETRY_AFTER_H = 1.0


def _rescan_targets(con, context_id: int, by_kind: dict,
                    with_reactions: bool = False) -> tuple[list, list]:
    """(character_id, planet_id) worth an ESI read this tick, in a stable order.

    Returns `(targets, unverifiable)`, where each unverifiable entry is `(pair, reason)`.

    Filters, in the order they cost least: alerts that name nothing readable, then data that is
    already as fresh as ESI will give (`esi_expires` in the future — a read before it is refused by
    the rule in CLAUDE.md and would tell us nothing anyway), then characters with a dead refresh
    token (a known-dead token is never worth a request), then characters whose last automatic scan
    failed recently.

    A `planet_id` of None is a REACTION target: the `reaction_*` kinds are about a character's
    industry jobs, not a colony, so the thing to re-read is that character's job list. Gated by
    `with_reactions` — a different ESI endpoint and a second flag's worth of new traffic.
    """
    wanted: list[tuple[int, int | None]] = []
    seen = set()
    for kind, evs in by_kind.items():
        for a in evs:
            cid, pid = a.get("character_id"), a.get("planet_id")
            if not cid:
                continue
            if pid is None:
                # Keyed on the KIND, not on the missing planet: other keyless kinds may exist and
                # a jobs read would tell us nothing about them.
                if not with_reactions or not kind.startswith("reaction_"):
                    continue
            elif not pid:
                continue           # falsy-but-not-None was skipped before this; keep it skipped
            if (cid, pid) in seen:
                continue
            seen.add((cid, pid))
            wanted.append((cid, pid))
    if not wanted:
        return [], []

    now = time.time()
    fresh_by_planet = {
        (r["character_id"], r["planet_id"]): r["esi_expires"]
        for r in con.execute(
            "SELECT character_id, planet_id, esi_expires FROM pp_char_planets "
            "WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)",
            (context_id,),
        )
    }
    # The jobs endpoint's own cache window, so the reaction targets get the same "ESI has nothing
    # newer" free pass the colonies get from `esi_expires`. Read once, and only if it can matter.
    jobs_fetched_at: dict[int, float] = {}
    jobs_cache_ttl = 0.0            # 0 = no free pass, i.e. read it — the safe way to be wrong
    if any(pid is None for _c, pid in wanted):
        try:
            from app.reactions.jobs import _JOBS_CACHE_TTL
            jobs_cache_ttl = _JOBS_CACHE_TTL
            jobs_fetched_at = {r["character_id"]: (r["fetched_at"] or 0) for r in con.execute(
                "SELECT character_id, fetched_at FROM pp_char_industry_jobs "
                "WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)",
                (context_id,),
            )}
        except Exception:
            jobs_fetched_at = {}    # table may not exist yet — never break the colony alerts
    chars = {
        r["character_id"]: r
        for r in con.execute(
            "SELECT character_id, refresh_token, is_dummy, scan_failed_at, scopes "
            "FROM pp_characters WHERE context_id=?",
            (context_id,),
        )
    }
    out, unverifiable = [], []
    for cid, pid in wanted:
        if pid is None:
            fetched = jobs_fetched_at.get(cid, 0)
            if fetched and now - fetched < jobs_cache_ttl:
                continue        # inside ESI's own cache window — same free pass as `esi_expires`
        else:
            exp = fresh_by_planet.get((cid, pid))
            if exp and exp > now:
                # Not stale: ESI has nothing newer to give, so the data behind this alert is
                # already as current as it can be. Verified WITHOUT a request — the cheapest good
                # outcome.
                continue
        c = chars.get(cid)
        if not c or c["is_dummy"]:
            continue                       # a placeholder character has no ESI data to refresh
        if pid is None and "read_character_jobs" not in (c["scopes"] or ""):
            # The jobs scope is opt-in (`?reactions=1` login) and this character either never
            # granted it or dropped it by re-authorising through the normal login. No request can
            # check this alert either way; what differs is whether the user has been told.
            #
            # A character we hold a jobs SNAPSHOT for is one that WAS tracked, so the Characters
            # card and the Reactions tab both now say "job tracking disconnected" and offer the
            # re-authorise (`reactions_scope_lost`). Held back, like a dead token: the page names
            # the fix, and every alert about that snapshot is computed from data frozen at whatever
            # the last successful read saw.
            #
            # No snapshot means nothing was ever tracked — nothing on any page would explain the
            # silence, so it is SKIPPED and delivered exactly as it is today. (`jobs_fetched_at`
            # empties on a read failure, which lands here too: the safe direction.)
            if cid in jobs_fetched_at:
                unverifiable.append(((cid, pid), "no_jobs_scope"))
            continue
        if not c["refresh_token"]:
            # A token the user must re-add. We cannot check, so we do not tell them about it —
            # the red dot on the character page is the thing to act on, not a colony report we
            # have no current evidence for.
            unverifiable.append(((cid, pid), "no_token"))
            continue
        failed_at = c["scan_failed_at"] or 0
        if failed_at and now - failed_at < _SCAN_RETRY_AFTER_H * 3600:
            # Inside the retry brake after a transient failure — still unchecked, so still not
            # something to report. Sending here would be reporting off exactly the stale data the
            # feature exists to stop trusting.
            unverifiable.append(((cid, pid), "retry_brake"))
            continue
        out.append((cid, pid))
    return out, unverifiable


def _default_scan(character_id: int, planet_id: int | None) -> bool:
    """Refresh what one alert is about. True only if it was actually re-read.

    `planet_id is None` means a `reaction_*` alert, which is about the character's industry jobs
    rather than a colony — a different endpoint, so a different read, but the same contract: the
    return value must be honest about whether anything was verified.

    For a colony, `fetched` counts planets ATTEMPTED, not planets that came back — it is
    incremented before the detail request — so it cannot stand in for success on its own. `failed`
    is the honest signal, and treating a failed read as a success is what would let the retry brake
    never engage.
    """
    if planet_id is None:
        from app.reactions.jobs import refresh_character_jobs
        return refresh_character_jobs(character_id)
    from app.esi import _get_valid_token, _fetch_planets
    token = _get_valid_token(character_id)
    if not token:
        return False
    res = _fetch_planets(character_id, token, only_planet_id=planet_id) or {}
    return res.get("failed", 0) == 0 and (res.get("fetched", 0) + res.get("skipped", 0)) >= 1


def _rescan_for_due_alerts(con, context_id: int, by_kind: dict, scan=None,
                           with_reactions: bool = False) -> _ScanOutcome:
    """Refresh the colonies the due alerts are about, so the alert is judged on current data.

    `scan` is injectable so the tests can exercise every branch of this without ESI.
    """
    global _scan_budget_left
    scan = scan or _default_scan
    outcome = _ScanOutcome()
    targets, unverifiable = _rescan_targets(con, context_id, by_kind, with_reactions)
    for pair, reason in unverifiable:
        outcome.suppress(pair, reason)
    budget = max(0, _scan_budget_left)
    for cid, pid in targets[:budget]:
        try:
            ok = scan(cid, pid)
        except Exception:
            log.warning("Alert rescan failed for character %s planet %s", cid, pid, exc_info=True)
            ok = False
        if ok:
            outcome.scanned.add((cid, pid))
            con.execute("UPDATE pp_characters SET scan_failed_at=NULL WHERE character_id=?", (cid,))
        else:
            outcome.suppress((cid, pid), "scan_failed")
            con.execute("UPDATE pp_characters SET scan_failed_at=? WHERE character_id=?",
                        (time.time(), cid))
            # The character list is Redis-cached for 5 minutes and now carries this field. Without
            # the invalidation the amber dot lags behind the silence it exists to explain.
            try:
                from app.cache import cache_invalidate
                from app.esi_data import charlist_key
                cache_invalidate(charlist_key(context_id))
            except Exception:
                pass
    # Over budget is not a failure, but it IS unverified — hold those back and let the next tick
    # scan them. 20 a tick is ~1,900 colony reads a day at the per-process ceiling, and
    # nothing like that in practice, because a read only happens when an alert is actually sent.
    _scan_budget_left = budget - len(targets[:budget])
    for cid, pid in targets[budget:]:
        outcome.suppress((cid, pid), "over_budget")
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


def _log_rescan_outcomes(con, context_id: int, before: dict, after: dict, outcome: _ScanOutcome):
    """Record the alerts the rescan pass stopped from being sent, and why.

    `before` is what was due off the stored data, `after` what is still due once the colonies have
    been re-read and the unverifiable ones dropped. An alert in the first and not the second either
    stopped being true (the rescan PREVENTED it) or could not be checked (SUPPRESSED). An alert that
    simply resolved between the two passes without any scan touching it is neither, and is logged as
    neither — crediting the feature for that would be the easiest way to make this measurement lie.
    """
    kept = {(kind, a.get("_dedupe_key")) for kind, evs in after.items() for a in evs}
    # `prevented` additionally requires that NOTHING about that colony (or that character's jobs)
    # survived. An alert can vanish from `before` because it ESCALATED, not because it was fixed:
    # `expiring` and `expired` are two kinds on one planet_id, and `reaction_finishing_soon` and
    # `reaction_completed` are two kinds on one character — a re-read that discovers the deadline
    # has passed swaps one for the other and still sends. Crediting that as prevented would inflate
    # the exact number the feature is judged on. Requiring the whole target to be clear undercounts
    # instead (a colony with one fixed problem and one real one credits neither), which is the
    # direction a measurement should err in.
    kept_targets = {(a.get("character_id"), a.get("planet_id"))
                    for evs in after.values() for a in evs}
    for kind, evs in before.items():
        for a in evs:
            key = a.get("_dedupe_key")
            if (kind, key) in kept:
                continue
            pair = (a.get("character_id"), a.get("planet_id"))
            reason = outcome.reasons.get(pair)
            if reason:
                _log_outcome(con, context_id, kind, a.get("character_name"), key,
                             f"suppressed:{reason}", dedupe_h=_OUTCOME_LOG_WINDOW_H)
            elif pair in outcome.scanned and pair not in kept_targets:
                _log_outcome(con, context_id, kind, a.get("character_name"), key, "prevented")


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
        outcome = _rescan_for_due_alerts(
            con, context_id, by_kind,
            with_reactions=feature_enabled_for("alert_rescan_reactions", context_id))
        if outcome.scanned or outcome.suppressed:
            before = by_kind
            by_kind = _due_alerts(con, context_id, notify_kinds, min_severity, smart)
            by_kind = _drop_unverified(by_kind, outcome.suppressed)
            _log_rescan_outcomes(con, context_id, before, by_kind, outcome)

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
        f"FROM pp_notification_log WHERE context_id=? AND {_SENDS_ONLY_SQL} "
        "ORDER BY sent_at DESC LIMIT 40",
        (ctx,),
    ).fetchall()
    con.close()
    return {"log": [dict(r) for r in rows]}
