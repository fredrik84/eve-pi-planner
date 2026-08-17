"""Tests for TODO §37: alerts that check before they nag, and nag less each time.

Two behaviours, both in app/notifications.py, both behind `alert_rescan_backoff`:

  1. **Backoff.** A repeat of an alert nobody has acted on waits twice as long as the last one,
     capped. The FIRST send is never delayed — that property is the whole reason the change is
     safe, so it is asserted directly rather than assumed.
  2. **Rescan before sending.** The one colony an alert is about is re-read from ESI first, so a
     problem the user already fixed in game is never reported. A colony that cannot be re-read is
     held back rather than reported off stale data.

No ESI and no network: the scan function is injected, so every branch (success, failure, dead
token, over budget, still-fresh data) is exercised deterministically. Run inside the container:

    docker compose cp test_alert_cadence.py web:/srv/app/
    docker compose exec -T web python3 /srv/app/test_alert_cadence.py
"""
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from app.sde import get_connection                                       # noqa: E402
from app.notifications import (                                          # noqa: E402
    ensure_notification_tables, _consecutive_cooldown_h, _recently_notified,
    _rescan_targets, _rescan_for_due_alerts, _drop_unverified,
    _BACKOFF_CAP_H, _SCAN_BUDGET_PER_TICK, _SCAN_RETRY_AFTER_H,
)

FAKE_CTX = 777301
FAKE_CID = 990301
FAKE_CID2 = 990302
PLANET_A = 40100001
PLANET_B = 40100002

_failures = []


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures.append(msg)
    return bool(cond)


def _cleanup():
    con = get_connection()
    for cid in (FAKE_CID, FAKE_CID2):
        con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_notification_settings WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_notification_prefs WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()


def _seed_character(cid=FAKE_CID, refresh_token="live-token", is_dummy=0, scan_failed_at=None):
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, context_id, refresh_token, "
            "is_dummy, scan_failed_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id, "
            "refresh_token=excluded.refresh_token, is_dummy=excluded.is_dummy, "
            "scan_failed_at=excluded.scan_failed_at",
        (cid, f"Toon {cid}", FAKE_CTX, refresh_token, is_dummy, scan_failed_at),
        )
        con.commit()
    finally:
        con.close()


def _seed_planet(cid, pid, esi_expires=None):
    con = get_connection()
    con.execute("DELETE FROM pp_char_planets WHERE character_id=? AND planet_id=?", (cid, pid))
    con.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, planet_num, "
        "is_extractor, esi_expires) VALUES (?,?,?,?,?,?)",
        (cid, pid, "Barren", 1, 1, esi_expires),
    )
    con.commit()
    con.close()


def _log_send(kind, key, hours_ago, channels=("discord",)):
    """One SEND writes one row per enabled channel, microseconds apart — the shape that inflated
    the backoff rung until it was deduped."""
    con = get_connection()
    base = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    for i, ch in enumerate(channels):
        stamp = (base + timedelta(microseconds=i * 900)).isoformat()
        con.execute(
            "INSERT INTO pp_notification_log (context_id, channel, event, character, planet_id, "
            "sent_at, status) VALUES (?,?,?,?,?,?,?)",
            (FAKE_CTX, ch, kind, "Toon", key, stamp, "ok"),
        )
    con.commit()
    con.close()


def _alert(cid, pid, kind="expired"):
    return {"kind": kind, "severity": "high", "character_id": cid, "planet_id": pid,
            "character_name": "Toon"}


# ── 1. Backoff ────────────────────────────────────────────────────────────────────────────────

def test_backoff_doubles_per_consecutive_send() -> bool:
    print(f"\n{'='*60}\n  a repeat nobody acted on waits twice as long as the last\n{'='*60}")
    ok = True
    con = get_connection()
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()

    ok &= check(_consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0) == 2.0,
                "with nothing ever sent, the interval is the kind's own base (2h)")

    # One send, two hours ago → next repeat is still the base interval.
    _log_send("expired", PLANET_A, hours_ago=2.0)
    ok &= check(_consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0) == 2.0,
                "after ONE send the interval has not moved — backoff is about repeats")

    # A chain: sends at -14h, -12h (gap 2 = the base), -8h (gap 4), -0h (gap 8). Three consecutive
    # repeats, so the next wait is 2 * 2^3 = 16 → capped.
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    for h in (14.0, 12.0, 8.0, 0.0):
        _log_send("expired", PLANET_A, hours_ago=h)
    got = _consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0)
    ok &= check(got == _BACKOFF_CAP_H,
                f"four sends in an unbroken chain reach the cap ({_BACKOFF_CAP_H}h, got {got})")

    # Two consecutive sends only: base 2h, gap 2h → next is 4h.
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    _log_send("expired", PLANET_A, hours_ago=2.0)
    _log_send("expired", PLANET_A, hours_ago=0.0)
    got = _consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0)
    ok &= check(got == 4.0, f"two consecutive sends → the next waits 4h (got {got})")
    con.close()
    return ok


def test_a_resolved_alert_starts_over() -> bool:
    print(f"\n{'='*60}\n  an alert that cleared and came back is not on the slow rung\n{'='*60}")
    ok = True
    con = get_connection()
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()

    # A long chain that reached the cap, then a 40-hour silence — far longer than the 12h interval
    # then in force, so the alert MUST have stopped being true — then one fresh send.
    for h in (100.0, 98.0, 94.0, 86.0, 74.0, 62.0):
        _log_send("storage_full", PLANET_A, hours_ago=h)
    _log_send("storage_full", PLANET_A, hours_ago=0.0)
    got = _consecutive_cooldown_h(con, FAKE_CTX, "storage_full", PLANET_A, 2.0)
    ok &= check(got == 2.0,
                f"a gap longer than the interval resets the chain to the base (got {got}h)")

    # The guard this protects: a problem that genuinely recurs every week must never be quietly
    # demoted to one ping a day. Same shape, weekly.
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    for h in (24.0 * 21, 24.0 * 14, 24.0 * 7, 0.0):
        _log_send("storage_full", PLANET_B, hours_ago=h)
    got = _consecutive_cooldown_h(con, FAKE_CTX, "storage_full", PLANET_B, 2.0)
    ok &= check(got == 2.0, f"a weekly recurrence stays on the base interval (got {got}h)")
    con.close()
    return ok


def test_the_first_alert_is_never_delayed() -> bool:
    print(f"\n{'='*60}\n  backoff changes repeats and nothing else\n{'='*60}")
    ok = True
    con = get_connection()
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    # THE property that makes a 12h cap safe: the cooldown gate only suppresses a send when one
    # already went out inside the window. Nothing sent → nothing suppressed, at any interval.
    ok &= check(not _recently_notified(con, FAKE_CTX, "expired", PLANET_A, _BACKOFF_CAP_H),
                "a never-before-sent alert is not suppressed even at the 12h cap")
    ok &= check(not _recently_notified(con, FAKE_CTX, "expired", PLANET_A, 24.0),
                "...nor at any interval, because the gate reads sends and there are none")
    # And once sent, it IS suppressed inside the window — the other half of the same gate.
    _log_send("expired", PLANET_A, hours_ago=1.0)
    ok &= check(_recently_notified(con, FAKE_CTX, "expired", PLANET_A, 2.0),
                "a send one hour ago suppresses a repeat on a 2h interval")
    ok &= check(not _recently_notified(con, FAKE_CTX, "expired", PLANET_A, 0.5),
                "...and does not, once the interval has elapsed")
    con.close()
    return ok


# ── 2. Rescan before sending ──────────────────────────────────────────────────────────────────

def test_only_colonies_worth_a_call_are_scanned() -> bool:
    print(f"\n{'='*60}\n  every filter that keeps an ESI call from being made\n{'='*60}")
    ok = True
    _seed_character(FAKE_CID, refresh_token="live-token")
    _seed_planet(FAKE_CID, PLANET_A, esi_expires=None)
    con = get_connection()

    targets, unver = _rescan_targets(con, FAKE_CTX, {"expired": [_alert(FAKE_CID, PLANET_A)]})
    ok &= check(targets == [(FAKE_CID, PLANET_A)], "a stale colony with a live token is scanned")

    # Two alerts about the SAME colony are one call, not two.
    targets, unver = _rescan_targets(con, FAKE_CTX, {
        "expired": [_alert(FAKE_CID, PLANET_A)],
        "storage_full": [_alert(FAKE_CID, PLANET_A, "storage_full")],
    })
    ok &= check(targets == [(FAKE_CID, PLANET_A)],
                "two alerts about one colony are deduped into a single read")

    # Data ESI will not regenerate yet — reading before `Expires` is the banned move, and would
    # return the same bytes anyway.
    _seed_planet(FAKE_CID, PLANET_A, esi_expires=time.time() + 600)
    targets, unver = _rescan_targets(con, FAKE_CTX, {"expired": [_alert(FAKE_CID, PLANET_A)]})
    ok &= check(targets == [] and unver == [],
                "a colony inside its ESI Expires window is never re-read — and IS still reported, "
                "because data ESI will not regenerate yet is not stale")

    # Dead token: known-dead, so never worth a request.
    _seed_planet(FAKE_CID, PLANET_A, esi_expires=None)
    # A dead token is recorded as '' — the column is NOT NULL, which is why writing NULL there
    # silently failed for as long as it did (see _refresh_token in app/esi.py).
    _seed_character(FAKE_CID, refresh_token="")
    targets, unver = _rescan_targets(con, FAKE_CTX, {"expired": [_alert(FAKE_CID, PLANET_A)]})
    ok &= check(targets == [], "a character whose refresh token is dead is never called for")
    ok &= check(unver == [((FAKE_CID, PLANET_A), "no_token")],
                "...and its alert is held back rather than sent off data we could not check, "
                "with the reason recorded so the log can tell a dead token from a busy tick")

    # A recent unattended failure backs off instead of retrying every tick.
    _seed_character(FAKE_CID, refresh_token="live-token", scan_failed_at=time.time() - 60)
    targets, unver = _rescan_targets(con, FAKE_CTX, {"expired": [_alert(FAKE_CID, PLANET_A)]})
    ok &= check(targets == [] and unver == [((FAKE_CID, PLANET_A), "retry_brake")],
                "a scan that just failed is not retried on the next tick, and is not reported either")
    _seed_character(FAKE_CID, refresh_token="live-token",
                    scan_failed_at=time.time() - (_SCAN_RETRY_AFTER_H * 3600 + 60))
    targets, unver = _rescan_targets(con, FAKE_CTX, {"expired": [_alert(FAKE_CID, PLANET_A)]})
    ok &= check(targets == [(FAKE_CID, PLANET_A)],
                "...and is retried once the backoff window has passed")

    # An alert that names no colony (the reaction kinds) is not a scan target.
    targets, unver = _rescan_targets(con, FAKE_CTX, {
        "reaction_completed": [{"kind": "reaction_completed", "character_id": FAKE_CID,
                                "planet_id": None, "character_name": "Toon"}]})
    ok &= check(targets == [], "an alert with no planet_id is not a scan target")
    con.close()
    return ok


def test_a_dead_token_can_actually_be_recorded_as_dead() -> bool:
    """The whole "never call ESI for a known-dead token" filter rests on the token being MARKED
    dead, and that write was broken: `_refresh_token` wrote NULL into a `TEXT NOT NULL` column, the
    IntegrityError was swallowed by its own outer except, and the character stayed green forever.
    This asserts the column will take what the code writes — the cheapest possible guard, and the
    one that would have caught it."""
    print(f"\n{'='*60}\n  a token that dies is recorded as dead\n{'='*60}")
    ok = True
    _seed_character(FAKE_CID, refresh_token="tok")
    con = get_connection()
    try:
        con.execute("UPDATE pp_characters SET refresh_token='' WHERE character_id=?", (FAKE_CID,))
        con.commit()
        wrote = True
    except Exception as exc:
        print(f"    (write raised {type(exc).__name__}: {exc})")
        wrote = False
    ok &= check(wrote, "the dead-token write the SSO 400 path performs is accepted by the column")
    row = con.execute("SELECT refresh_token FROM pp_characters WHERE character_id=?",
                      (FAKE_CID,)).fetchone()
    ok &= check(not row["refresh_token"], "and reads back falsy, which is what token_ok tests")
    con.close()
    return ok


def test_a_colony_we_could_not_read_is_not_reported() -> bool:
    print(f"\n{'='*60}\n  a failed read holds the alert back rather than guessing\n{'='*60}")
    ok = True
    _seed_character(FAKE_CID, refresh_token="live-token")
    _seed_planet(FAKE_CID, PLANET_A, esi_expires=None)
    con = get_connection()
    by_kind = {"expired": [_alert(FAKE_CID, PLANET_A)]}

    calls = []
    out = _rescan_for_due_alerts(con, FAKE_CTX, by_kind,
                                 scan=lambda c, p: (calls.append((c, p)), True)[1])
    ok &= check(calls == [(FAKE_CID, PLANET_A)], "the due alert's colony is read")
    ok &= check(out.scanned == {(FAKE_CID, PLANET_A)} and not out.suppressed,
                "a successful read suppresses nothing")
    row = con.execute("SELECT scan_failed_at FROM pp_characters WHERE character_id=?",
                      (FAKE_CID,)).fetchone()
    ok &= check(row["scan_failed_at"] is None, "success clears any previous failure marker")

    # A read that raises: the alert is held back and the failure is remembered.
    _seed_character(FAKE_CID, refresh_token="live-token")
    def _boom(cid, pid):
        raise RuntimeError("ESI 503")
    out = _rescan_for_due_alerts(con, FAKE_CTX, by_kind, scan=_boom)
    ok &= check(out.suppressed == {(FAKE_CID, PLANET_A)} and not out.scanned,
                "a read that throws suppresses the alert instead of sending it")
    row = con.execute("SELECT scan_failed_at FROM pp_characters WHERE character_id=?",
                      (FAKE_CID,)).fetchone()
    ok &= check(row["scan_failed_at"] is not None, "and the failure is recorded for the backoff")

    # A read that reports failure (no valid token at call time) does the same.
    _seed_character(FAKE_CID, refresh_token="live-token")
    out = _rescan_for_due_alerts(con, FAKE_CTX, by_kind, scan=lambda c, p: False)
    ok &= check(out.suppressed == {(FAKE_CID, PLANET_A)},
                "a read that reports failure suppresses the alert too")

    # And the drop actually removes it from what would be sent.
    dropped = _drop_unverified({"expired": [_alert(FAKE_CID, PLANET_A)]},
                               {(FAKE_CID, PLANET_A)})
    ok &= check(dropped == {}, "a kind left with no verified events disappears entirely")
    kept = _drop_unverified(
        {"expired": [_alert(FAKE_CID, PLANET_A), _alert(FAKE_CID2, PLANET_B)]},
        {(FAKE_CID, PLANET_A)})
    ok &= check(len(kept.get("expired", [])) == 1
                and kept["expired"][0]["character_id"] == FAKE_CID2,
                "one unreadable colony does not silence another that read fine")
    con.close()
    return ok


def test_the_budget_is_a_brake_not_a_queue() -> bool:
    print(f"\n{'='*60}\n  a burst cannot turn into a burst of ESI calls\n{'='*60}")
    ok = True
    import app.notifications as N
    N._scan_budget_left = _SCAN_BUDGET_PER_TICK      # the real job resets this once per tick
    con = get_connection()
    _seed_character(FAKE_CID, refresh_token="live-token")
    over = _SCAN_BUDGET_PER_TICK + 5
    alerts = []
    for i in range(over):
        pid = PLANET_A + 100 + i
        _seed_planet(FAKE_CID, pid, esi_expires=None)
        alerts.append(_alert(FAKE_CID, pid))

    calls = []
    out = _rescan_for_due_alerts(con, FAKE_CTX, {"expired": alerts},
                                 scan=lambda c, p: (calls.append(p), True)[1])
    ok &= check(len(calls) == _SCAN_BUDGET_PER_TICK,
                f"at most {_SCAN_BUDGET_PER_TICK} colonies are read in one tick (got {len(calls)})")
    ok &= check(len(out.suppressed) == over - _SCAN_BUDGET_PER_TICK,
                "the ones over budget are held back for the next tick, not sent unverified")
    ok &= check(not (out.scanned & out.suppressed),
                "no colony is both read and held back")
    con.close()
    return ok


def test_channels_do_not_inflate_the_rung() -> bool:
    """A send writes one row per channel. Counting rows instead of sends put a three-channel user
    at the 12h cap after their second alert and made the chain reset unreachable — a gap of
    microseconds is never longer than any interval."""
    print(f"\n{'='*60}\n  the rung counts sends, not notification channels\n{'='*60}")
    ok = True
    con = get_connection()
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()

    _log_send("expired", PLANET_A, hours_ago=0.0, channels=("discord", "pushover", "ntfy"))
    got = _consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0)
    ok &= check(got == 2.0, f"ONE send over three channels is one send (got {got}h, want 2.0)")

    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    _log_send("expired", PLANET_A, hours_ago=2.0, channels=("discord", "pushover"))
    _log_send("expired", PLANET_A, hours_ago=0.0, channels=("discord", "pushover"))
    got = _consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0)
    ok &= check(got == 4.0,
                f"two sends over two channels is the two-send answer (got {got}h, want 4.0)")

    # And the reset still works for a multi-channel user, which the row-counting version could
    # never do — every gap it saw was microseconds.
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    for h in (100.0, 98.0, 94.0, 86.0):
        _log_send("expired", PLANET_B, hours_ago=h, channels=("discord", "pushover", "ntfy"))
    _log_send("expired", PLANET_B, hours_ago=0.0, channels=("discord", "pushover", "ntfy"))
    got = _consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_B, 2.0)
    ok &= check(got == 2.0, f"a multi-channel chain still resets after a real gap (got {got}h)")
    con.close()
    return ok


def test_the_budget_is_for_the_whole_tick_not_each_account() -> bool:
    """`_process_context` runs once per context, so a per-context cap multiplies by the number of
    accounts. The number in the docs has to be the number the app can actually issue."""
    print(f"\n{'='*60}\n  the scan budget covers the tick, not one account\n{'='*60}")
    ok = True
    import app.notifications as N
    con = get_connection()
    _seed_character(FAKE_CID, refresh_token="live-token")
    alerts = []
    for i in range(_SCAN_BUDGET_PER_TICK):
        pid = PLANET_A + 500 + i
        _seed_planet(FAKE_CID, pid, esi_expires=None)
        alerts.append(_alert(FAKE_CID, pid))

    N._scan_budget_left = _SCAN_BUDGET_PER_TICK
    calls = []
    N._rescan_for_due_alerts(con, FAKE_CTX, {"expired": alerts},
                             scan=lambda c, p: (calls.append(p), True)[1])
    first = len(calls)
    # A SECOND context in the same tick finds the budget already spent.
    out2 = N._rescan_for_due_alerts(con, FAKE_CTX, {"expired": alerts},
                                    scan=lambda c, p: (calls.append(p), True)[1])
    ok &= check(first == _SCAN_BUDGET_PER_TICK, f"the first account spends the budget ({first})")
    ok &= check(len(calls) == _SCAN_BUDGET_PER_TICK,
                f"a second account in the same tick issues no further reads (total {len(calls)})")
    ok &= check(len(out2.suppressed) == len(alerts),
                "and its alerts are held for the next tick rather than sent unverified")
    N._scan_budget_left = _SCAN_BUDGET_PER_TICK
    con.close()
    return ok


def test_the_flag_off_path_is_the_old_behaviour() -> bool:
    """The rollback path. With the flag off there must be NO ESI call, no suppression, and the
    kind's own cooldown — otherwise turning the flag off does not undo anything."""
    print(f"\n{'='*60}\n  with the flag off, nothing about this feature happens\n{'='*60}")
    ok = True
    import app.notifications as N
    con = get_connection()
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    # Five consecutive sends: smart would be at the cap, the old path is always the base.
    for h in (14.0, 12.0, 8.0, 0.0):
        _log_send("expired", PLANET_A, hours_ago=h)

    seen = {}
    real_scan, real_notifier = N._default_scan, N.make_notifier
    N._default_scan = lambda c, p: seen.setdefault("scanned", True)
    N.make_notifier = lambda ch, cfg: (_ for _ in ()).throw(
        AssertionError("send attempted in a flag-off cadence test"))
    try:
        smart_cd = N._consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0)
        flag_off = N._due_alerts(con, FAKE_CTX, {"expired"}, "warn", smart=False)
        flag_on = N._due_alerts(con, FAKE_CTX, {"expired"}, "warn", smart=True)
    finally:
        N._default_scan, N.make_notifier = real_scan, real_notifier
    ok &= check(smart_cd == _BACKOFF_CAP_H,
                f"the chain HAS backed off, so the two paths must differ (got {smart_cd}h)")
    ok &= check("scanned" not in seen, "no scan is triggered while computing due alerts")
    ok &= check(isinstance(flag_off, dict) and isinstance(flag_on, dict),
                "both paths return the same shape, so the caller cannot tell them apart by type")
    con.close()
    return ok


def _seed_due_expired_alert():
    """A real `expired` alert for the fake context, plus an enabled notification channel — so
    `_process_context` has something to actually decide about."""
    import json
    _seed_character(FAKE_CID, refresh_token="live-token")
    con = get_connection()
    con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (FAKE_CID,))
    con.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, planet_num, "
        "is_extractor, sim_state, esi_expires) VALUES (?,?,?,?,?,?,?)",
        (FAKE_CID, PLANET_A, "Barren", 1, 1, json.dumps({"expiry": time.time() - 3600}), None),
    )
    con.execute("DELETE FROM pp_notification_settings WHERE context_id=?", (FAKE_CTX,))
    con.execute(
        "INSERT INTO pp_notification_settings (context_id, channel, config, enabled) "
        "VALUES (?,?,?,1)", (FAKE_CTX, "discord", json.dumps({"webhook_url": "http://x"})),
    )
    con.execute("DELETE FROM pp_notification_prefs WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_notification_log WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()


def _run_process_context(flag_on, scan_result):
    """Run the REAL _process_context with the flag and the scan outcome forced. Returns
    (sent_titles, scan_calls)."""
    import app.notifications as N
    sent, scans = [], []

    class _FakeNotifier:
        def send(self, title, body, **kw):
            sent.append(title)

    real_flag, real_scan, real_notifier = N.feature_enabled_for, N._default_scan, N.make_notifier
    N.feature_enabled_for = lambda key, ctx: flag_on
    N._default_scan = lambda c, p: (scans.append((c, p)), scan_result)[1]
    N.make_notifier = lambda ch, cfg: _FakeNotifier()
    N._scan_budget_left = _SCAN_BUDGET_PER_TICK
    con = get_connection()
    try:
        N._process_context(con, FAKE_CTX)
        con.commit()
    finally:
        N.feature_enabled_for, N._default_scan, N.make_notifier = real_flag, real_scan, real_notifier
        con.close()
    return sent, scans


def test_the_whole_path_end_to_end() -> bool:
    """The integration point the unit tests could not see: with the feature OFF nothing changes,
    and with it ON a colony we failed to read produces NO notification. Both of these could be
    deleted from `_process_context` without any other test noticing."""
    print(f"\n{'='*60}\n  _process_context: the flag gate and the suppression, for real\n{'='*60}")
    ok = True

    _seed_due_expired_alert()
    sent, scans = _run_process_context(flag_on=False, scan_result=True)
    ok &= check(scans == [], "flag OFF: no ESI read is attempted")
    ok &= check(len(sent) == 1, f"flag OFF: the alert is sent exactly as before (sent {len(sent)})")

    _seed_due_expired_alert()
    sent, scans = _run_process_context(flag_on=True, scan_result=True)
    ok &= check(scans == [(FAKE_CID, PLANET_A)], "flag ON: the alert's colony is read first")
    ok &= check(len(sent) == 1,
                f"flag ON: an alert that survives the re-read is still sent (sent {len(sent)})")

    _seed_due_expired_alert()
    sent, scans = _run_process_context(flag_on=True, scan_result=False)
    ok &= check(scans == [(FAKE_CID, PLANET_A)], "flag ON: the read is attempted")
    ok &= check(sent == [],
                f"flag ON: a colony we could NOT read produces no notification (sent {sent})")

    con = get_connection()
    row = con.execute("SELECT COUNT(*) AS c FROM pp_notification_log "
                      "WHERE context_id=? AND status='ok'", (FAKE_CTX,)).fetchone()
    ok &= check(row["c"] == 0, "and nothing is logged as sent, so no cooldown is started for it")
    con.execute("DELETE FROM pp_notification_settings WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()
    return ok


def test_a_scan_that_read_nothing_is_not_a_success() -> bool:
    """The trap that bit twice, in two functions: `_fetch_planets` reports `fetched` as planets
    ATTEMPTED, and its outer except returns all-zeros — so `failed == 0` does NOT mean a scan
    worked. Both the alert job's success test and the hand-rescan's flag-clearing have to reject a
    scan that read nothing, or a total failure reads as a clean bill of health."""
    print(f"\n{'='*60}\n  a scan that read nothing never counts as a success\n{'='*60}")
    ok = True
    import app.esi as E
    import app.notifications as N
    from app.esi_data import _clear_scan_failure

    cases = [
        ({"fetched": 1, "skipped": 0, "failed": 0}, True,  "a colony actually re-read"),
        ({"fetched": 0, "skipped": 1, "failed": 0}, True,  "a colony ESI had nothing newer for"),
        ({"fetched": 0, "skipped": 0, "failed": 1}, False, "a detail read that failed"),
        ({"fetched": 0, "skipped": 0, "failed": 0}, False,
         "the all-zeros the outer except returns when the scan died before the loop"),
    ]
    real_token, real_fetch = E._get_valid_token, E._fetch_planets
    E._get_valid_token = lambda cid: "tok"
    try:
        for res, want, label in cases:
            E._fetch_planets = lambda c, t, only_planet_id=None, _r=res: _r
            got = N._default_scan(FAKE_CID, PLANET_A)
            ok &= check(got is want, f"_default_scan: {label} -> {want}")
    finally:
        E._get_valid_token, E._fetch_planets = real_token, real_fetch

    # And the same predicate on the hand-rescan path, which clears the amber dot.
    for res, should_clear, label in [(c[0], c[1], c[2]) for c in cases]:
        _seed_character(FAKE_CID, refresh_token="live-token", scan_failed_at=time.time() - 10)
        _clear_scan_failure(FAKE_CID, res)
        con = get_connection()
        row = con.execute("SELECT scan_failed_at FROM pp_characters WHERE character_id=?",
                          (FAKE_CID,)).fetchone()
        con.close()
        cleared = row["scan_failed_at"] is None
        ok &= check(cleared is should_clear,
                    f"_clear_scan_failure: {label} -> {'clears' if should_clear else 'keeps'} the marker")
    return ok


def _outcome_rows():
    con = get_connection()
    rows = [dict(r) for r in con.execute(
        "SELECT event, character, planet_id, status FROM pp_notification_log "
        "WHERE context_id=? AND status <> 'ok' ORDER BY sent_at", (FAKE_CTX,))]
    con.close()
    return rows


def test_the_alerts_that_never_became_a_send_are_recorded() -> bool:
    """§37a: the rescan's two outcomes were invisible, and unlike the backoff rung they cannot be
    reconstructed afterwards — `_log_send` only ever runs on a real send. An alert PREVENTED is the
    entire benefit of the feature; an alert SUPPRESSED is its entire cost. Both now leave a row."""
    print(f"\n{'='*60}\n  the rescan records what it prevented and what it suppressed\n{'='*60}")
    ok = True
    import json
    import app.notifications as N

    # PREVENTED: the scan succeeds AND finds the problem fixed — so it writes fresh colony data,
    # exactly as a real scan does, and the second _due_alerts pass comes back empty.
    def _fix_the_colony(cid, pid):
        con = get_connection()
        con.execute("UPDATE pp_char_planets SET sim_state=? WHERE character_id=? AND planet_id=?",
                    (json.dumps({"expiry": time.time() + 86400}), cid, pid))
        con.commit()
        con.close()
        return True

    _seed_due_expired_alert()
    real_flag, real_scan, real_notifier = N.feature_enabled_for, N._default_scan, N.make_notifier
    N.feature_enabled_for = lambda key, ctx: True
    N._default_scan = _fix_the_colony
    N.make_notifier = lambda ch, cfg: (_ for _ in ()).throw(
        AssertionError("a prevented alert must never reach a notifier"))
    N._scan_budget_left = _SCAN_BUDGET_PER_TICK
    con = get_connection()
    try:
        N._process_context(con, FAKE_CTX)
        con.commit()
    finally:
        N.feature_enabled_for, N._default_scan, N.make_notifier = real_flag, real_scan, real_notifier
        con.close()

    rows = _outcome_rows()
    ok &= check([r["status"] for r in rows] == ["prevented"],
                f"a problem the user had already fixed is logged as prevented (got {rows})")
    ok &= check(bool(rows) and rows[0]["event"] == "expired" and rows[0]["planet_id"] == PLANET_A,
                "...against the kind and the colony it was about, so it can be attributed")

    # SUPPRESSED: the scan fails, the alert is still true, and nothing is sent. The reason is on
    # the row — "we are over budget" and "this token has been dead a week" want opposite responses.
    _seed_due_expired_alert()
    sent, scans = _run_process_context(flag_on=True, scan_result=False)
    ok &= check(sent == [], "a colony we could not read still sends nothing")
    rows = _outcome_rows()
    ok &= check([r["status"] for r in rows] == ["suppressed:scan_failed"],
                f"...and the suppression is logged WITH its cause (got {rows})")

    # An unresolved problem keeps the alert due on all 96 ticks of the day. One row per cause per
    # window, not 96. The failed scan above has since armed the retry brake, which is a DIFFERENT
    # cause and gets its own row — after that the count must stop moving.
    before = len(_outcome_rows())
    for _ in range(3):
        _run_process_context(flag_on=True, scan_result=False)
    mid = _outcome_rows()
    ok &= check([r["status"] for r in mid[before:]] == ["suppressed:retry_brake"],
                f"the retry brake is logged once as its own cause (got {mid[before:]})")
    for _ in range(3):
        _run_process_context(flag_on=True, scan_result=False)
    ok &= check(len(_outcome_rows()) == len(mid),
                f"and further ticks add nothing at all (was {len(mid)}, "
                f"now {len(_outcome_rows())})")

    # None of this may reach the readers that treat the log as a list of sends.
    con = get_connection()
    N._log_send(con, FAKE_CTX, "discord", "expired", "Toon", PLANET_A, "ok")
    con.commit()
    ok &= check(N._consecutive_cooldown_h(con, FAKE_CTX, "expired", PLANET_A, 2.0) == 2.0,
                "the outcome rows do not count as sends in the backoff chain")
    ok &= check(N._recently_notified(con, FAKE_CTX, "expired", PLANET_A, 2.0) is True,
                "...and a real send is still found by the cooldown check")
    con.close()

    log = N.get_notification_log(ctx=FAKE_CTX)["log"]
    ok &= check(log and all(not N._is_outcome_status(r["status"]) for r in log),
                f"the user's notification log still lists only things that were sent (got {log})")
    return ok


def main() -> int:
    _cleanup()
    try:
        results = [
            test_backoff_doubles_per_consecutive_send(),
            test_a_resolved_alert_starts_over(),
            test_the_first_alert_is_never_delayed(),
            test_channels_do_not_inflate_the_rung(),
            test_the_flag_off_path_is_the_old_behaviour(),
            test_a_dead_token_can_actually_be_recorded_as_dead(),
            test_only_colonies_worth_a_call_are_scanned(),
            test_a_colony_we_could_not_read_is_not_reported(),
            test_the_budget_is_a_brake_not_a_queue(),
            test_the_budget_is_for_the_whole_tick_not_each_account(),
            test_the_whole_path_end_to_end(),
            test_a_scan_that_read_nothing_is_not_a_success(),
            test_the_alerts_that_never_became_a_send_are_recorded(),
        ]
    finally:
        _cleanup()

    print(f"\n{'='*60}")
    if all(results) and not _failures:
        print("  All alert cadence checks passed.")
        return 0
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1


if __name__ == "__main__":
    # `scan_failed_at` is an additive migration in ensure_char_tables — call it rather than
    # assuming the running container has already been through it.
    from app.esi import ensure_char_tables
    ensure_char_tables()
    ensure_notification_tables()
    sys.exit(main())
