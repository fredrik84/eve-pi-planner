"""Expired programs notify once and back off to weekly reminders, independent of rollout.

No network or application database: real due/send logic runs against an in-memory log.
Run in the application container: python3 tests/test_expired_reminders.py
"""
import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, ".")
from app import alerts, notifications as N

HOUR = 3600
DAY = 24 * HOUR
NOW = datetime(2026, 10, 22, 8, tzinfo=timezone.utc).timestamp()


class ExpiredReminderTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE pp_notification_log (context_id INTEGER, channel TEXT, "
                         "event TEXT, character TEXT, planet_id INTEGER, sent_at TEXT, status TEXT)")
        self.clock = self.stack.enter_context(patch.object(N.time, "time", return_value=NOW))
        self.alert = {"kind": "expired", "severity": "high", "character_id": 123,
                      "character_name": "Test pilot", "planet_id": 456,
                      "location": "Test P1", "hours_left": -1, "expiry": NOW - HOUR}
        self.compute = self.stack.enter_context(patch.object(N, "compute_alerts",
                                                           side_effect=lambda ctx: [dict(self.alert)]))

    def send_at(self, stamp, channels=("discord",), **overrides):
        row = dict(context_id=1, character=self.alert["character_name"],
                   planet_id=self.alert["planet_id"], event="expired", status="ok")
        row.update(overrides)
        for i, channel in enumerate(channels):
            iso = datetime.fromtimestamp(stamp + i * 0.001, tz=timezone.utc).isoformat()
            self.con.execute("INSERT INTO pp_notification_log VALUES (?,?,?,?,?,?,?)",
                             (row["context_id"], channel, row["event"], row["character"],
                              row["planet_id"], iso, row["status"]))

    def due(self, smart=False):
        return bool(N._due_alerts(self.con, 1, {"expired"}, "warn", smart).get("expired"))

    def test_new_program_notifies_immediately_with_or_without_flag(self):
        for smart in (False, True):
            self.assertTrue(self.due(smart), "a newly detected expiry is never delayed")

    def test_reminder_intervals_increase_from_daily_to_weekly(self):
        last = NOW
        self.send_at(last)
        for wait_days in (1, 2, 4, 7, 7, 7):
            self.clock.return_value = last + wait_days * DAY - 1
            self.assertFalse(self.due(False), f"not due before {wait_days} days, even with flag off")
            self.assertFalse(self.due(True))
            self.clock.return_value += 1
            self.assertTrue(self.due(False), f"due after {wait_days} days")
            self.assertTrue(self.due(True))
            last = self.clock.return_value
            self.send_at(last)

    def test_long_gaps_do_not_restart_the_same_programs_sequence(self):
        self.alert["expiry"] = NOW - 40 * DAY
        for days_ago in (35, 30, 20, 5):
            self.send_at(NOW - days_ago * DAY)
        self.assertFalse(self.due(), "an outage or failed scan is not a restarted extraction")
        self.clock.return_value = NOW + 2 * DAY
        self.assertTrue(self.due(), "weekly reminder is still sent when due")

    def test_restart_resets_history_without_waiting_for_weekly_reminder(self):
        self.alert["expiry"] = NOW - 40 * DAY
        for days_ago in (35, 30, 20, 5):
            self.send_at(NOW - days_ago * DAY)
        self.assertFalse(self.due())
        self.alert["expiry"] = NOW - 60
        self.assertTrue(self.due(), "a restarted program's fresh expiry notifies immediately")
        self.send_at(NOW)
        self.clock.return_value = NOW + DAY - 1
        self.assertFalse(self.due())
        self.clock.return_value += 1
        self.assertTrue(self.due(), "new program starts at the daily rung")

    def test_history_is_scoped_to_account_character_planet_and_program(self):
        for overrides in ({"context_id": 2}, {"character": "Other pilot"},
                          {"planet_id": 999}, {"event": "expiring"}):
            self.send_at(NOW - 60, **overrides)
        self.send_at(self.alert["expiry"] - 1)
        self.assertTrue(self.due(), "another occurrence cannot suppress a first alert")
        self.send_at(NOW)
        self.assertFalse(self.due())
        self.alert["character_name"] = "Another pilot on the same planet"
        self.assertTrue(self.due(), "shared planets do not conflate different characters")

    def test_failed_suppressed_or_prevented_sends_do_not_delay_first_alert(self):
        for status in ("error: offline", "suppressed:scan_failed", "prevented"):
            self.send_at(NOW, status=status)
        self.assertTrue(self.due())

    def test_multiple_channels_count_as_one_reminder(self):
        self.send_at(NOW, channels=("discord", "pushover", "ntfy"))
        self.clock.return_value = NOW + DAY
        self.assertTrue(self.due(), "three channels must not push the first reminder to four days")
        self.send_at(NOW + DAY, channels=("discord", "pushover", "ntfy"))
        self.clock.return_value = NOW + 2 * DAY
        self.assertFalse(self.due())
        self.clock.return_value = NOW + 3 * DAY
        self.assertTrue(self.due())

    def test_existing_two_hour_history_adopts_long_reminders_immediately(self):
        self.alert["expiry"] = NOW - 10 * DAY
        for hours_ago in (8, 6, 4, 2):
            self.send_at(NOW - hours_ago * HOUR)
        self.assertFalse(self.due(), "rollout must not send another round of initial alerts")
        self.clock.return_value = NOW + 7 * DAY - 2 * HOUR
        self.assertTrue(self.due())

    def test_scheduler_sends_only_expected_batches_through_three_week_break(self):
        self.con.execute("CREATE TABLE pp_notification_settings "
                         "(id INTEGER, context_id INTEGER, channel TEXT, config TEXT, enabled INTEGER)")
        self.con.execute("INSERT INTO pp_notification_settings VALUES (1, 1, 'discord', '{}', 1)")
        clock = self.clock

        class ClockDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.return_value, tz=tz)

        notifier = Mock()
        sent = []
        notifier.send.side_effect = lambda *args, **kwargs: sent.append(clock.return_value)

        def verified(*args, **kwargs):
            outcome = N._ScanOutcome()
            outcome.scanned.add((self.alert["character_id"], self.alert["planet_id"]))
            return outcome

        with patch.object(N, "datetime", ClockDatetime), \
                patch.object(N, "_get_prefs", return_value={"notify_kinds": ["expired"], "min_severity": "warn"}), \
                patch.object(N, "feature_enabled_for", return_value=False), \
                patch.object(N, "_rescan_for_due_alerts", side_effect=verified), \
                patch.object(N, "make_notifier", return_value=notifier):
            for tick in range(22 * 24 * 4):
                clock.return_value = NOW + tick * 15 * 60
                N._process_context(self.con, 1)
        self.assertEqual(sent, [NOW + d * DAY for d in (0, 1, 3, 7, 14, 21)])

    def test_alert_carries_exact_program_expiry(self):
        expiry = NOW - 12345
        row = {"cid": 123, "ch": "Pilot", "planet_id": 456, "system": "Test", "pn": 1,
               "issues": "[]", "is_ext": True, "sim_state": json.dumps({"expiry": expiry})}
        settings = {"expiring_hours": 3, "muted_kinds": ["schedule_sync", "storage_full", "factory_refill"]}
        with patch.object(alerts, "get_alert_settings", return_value=settings), \
                patch.object(alerts, "_reaction_alerts", return_value=[]):
            result = alerts.compute_alerts(1, rows=[row], now=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["expiry"], expiry)


if __name__ == "__main__":
    unittest.main()
