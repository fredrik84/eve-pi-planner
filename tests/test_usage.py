"""PI usage ingestion, access control, retention, and whole-period account counts."""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db, esi, usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.set_up_database()
        usage.ensure_usage_table.__wrapped__()
        self.stack.enter_context(patch.object(usage, "ensure_usage_table"))
        self.enabled = self.stack.enter_context(patch.object(usage, "feature_enabled", return_value=True))
        self.visible = self.stack.enter_context(patch.object(usage, "feature_enabled_for", return_value=True))
        self.app = FastAPI()
        self.app.include_router(usage.router)
        self.client = self.stack.enter_context(TestClient(self.app))
        self.today = datetime.now(timezone.utc).date()

    def set_up_database(self):
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "_IS_POSTGRES", False))
        self.stack.enter_context(patch.object(db, "_SQLITE_PATH", Path(folder) / "usage.db"))
        class ClosingConnection(db._TimedSQLiteConnection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        self.stack.enter_context(patch.object(db, "_TimedSQLiteConnection", ClosingConnection))

    def login(self, ctx=111):
        self.app.dependency_overrides[usage.require_context] = lambda: ctx

    def admin(self):
        self.app.dependency_overrides[usage.require_admin] = lambda: 999

    def post(self, event="wizard_started", **kwargs):
        return self.client.post("/api/telemetry/usage", json={"event": event}, **kwargs)

    def rows(self):
        with db.get_connection() as con:
            return [dict(r) for r in con.execute("SELECT * FROM pp_usage_daily").fetchall()]

    def seed(self, ctx, event, days_ago=0, actions=1):
        with db.get_connection() as con:
            con.execute("INSERT INTO pp_usage_daily VALUES (?, ?, ?, ?)",
                        ((self.today - timedelta(days=days_ago)).isoformat(), ctx, event, actions))
            con.commit()

    def test_authentication_and_admin_report_gate(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.client.get("/api/admin/usage").status_code, 403)
        self.login()
        self.assertEqual(self.client.get("/api/admin/usage").status_code, 403)
        self.admin()
        self.visible.return_value = False
        self.assertEqual(self.client.get("/api/admin/usage").json(), {"visible": False})
        self.assertEqual(self.rows(), [])

    def test_only_fixed_events_are_accepted_and_no_extra_data_is_stored(self):
        self.login()
        self.assertEqual(self.post("private-character-name").status_code, 422)
        self.assertEqual(self.client.post("/api/telemetry/usage", json={
            "event": "wizard_started", "system": "secret"}).status_code, 422)
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(self.post().status_code, 204)
        self.assertEqual(self.rows(), [{"day": self.today.isoformat(), "context_id": 111,
                                       "event": "wizard_started", "actions": 2}])

    def test_disabled_collection_privacy_controls_and_cross_origin(self):
        self.login()
        self.enabled.return_value = False
        self.assertEqual(self.post().status_code, 204)
        self.enabled.return_value = True
        for headers in ({"DNT": "1"}, {"Sec-GPC": "1"}, {"Purpose": "prefetch"},
                        {"Sec-Purpose": "prefetch"}):
            self.assertEqual(self.post(headers=headers).status_code, 204)
        for headers in ({"Origin": "https://foreign.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.post(headers=headers).status_code, 403)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.post(headers={"Origin": "http://testserver"}).status_code, 204)

    def test_retention_and_daily_bound(self):
        self.seed(111, "wizard_started", actions=1000)
        self.seed(111, "setup_imported", days_ago=89)
        self.seed(111, "wizard_started", days_ago=90)
        self.login()
        self.assertEqual(self.post().status_code, 204)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(r for r in rows if r["event"] == "wizard_started")["actions"], 1000)

    def test_scheduled_retention_still_runs_with_collection_disabled(self):
        self.seed(111, "wizard_started", days_ago=90)
        self.seed(222, "setup_imported", days_ago=89)
        self.enabled.return_value = False
        usage.prune_usage()
        self.assertEqual([r["context_id"] for r in self.rows()], [222])

    def test_whole_period_deduplication_segments_and_date_boundaries(self):
        self.seed(1, "wizard_started", actions=3)
        self.seed(1, "wizard_started", days_ago=6, actions=2)
        self.seed(1, "setup_imported", days_ago=7)
        self.seed(2, "setup_imported", days_ago=6)
        self.seed(3, "wizard_started", days_ago=29)
        self.seed(4, "wizard_started", days_ago=30)
        self.seed(5, "colonies_rescanned")
        self.admin()
        result = self.client.get("/api/admin/usage").json()
        week, month = result["periods"]["7"], result["periods"]["30"]
        row = lambda period, event: next(r for r in period["rows"] if r["event"] == event)
        self.assertEqual((row(week, "wizard_started")["actions"], row(week, "wizard_started")["accounts"]), (5, 1))
        self.assertEqual((row(month, "wizard_started")["actions"], row(month, "wizard_started")["accounts"]), (6, 2))
        self.assertEqual(week["adoption"], {"wizard_only": 1, "import_only": 1, "both": 0, "neither": 1})
        self.assertEqual(month["adoption"], {"wizard_only": 1, "import_only": 1, "both": 1, "neither": 1})
        self.assertNotIn("context_id", str(result))
        self.assertNotIn("day", str(result))
        self.assertEqual(row(week, "wizard_completed")["accounts"], 0)
        self.enabled.return_value = False
        self.assertFalse(self.client.get("/api/admin/usage").json()["enabled"])

    def test_account_deletion_removes_usage_and_preserves_other_accounts(self):
        self.seed(111, "wizard_started")
        self.seed(222, "setup_imported")
        with db.get_connection() as con:
            con.execute("CREATE TABLE pp_characters (character_id INTEGER, context_id INTEGER)")
            con.execute("CREATE TABLE pp_user_contexts (id INTEGER)")
            con.execute("CREATE TABLE pp_sessions (context_id INTEGER)")
            con.execute("CREATE TABLE pp_baskets (id INTEGER, context_id INTEGER)")
            con.execute("INSERT INTO pp_user_contexts VALUES (111)")
            con.commit()
        # Account deletion tolerates optional feature tables that do not exist yet.
        with patch.object(esi, "ensure_char_tables"), patch("app.audit.record"):
            esi.delete_account(111)
        self.assertEqual([r["context_id"] for r in self.rows()], [222])


if __name__ == "__main__":
    unittest.main()
