"""Submission outcomes survive offline reviews and are visible/dismissible only by their owner.

Run in the application container. All writes use a temporary SQLite database.
"""
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db, planetary


class SubmissionReviewsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.set_up_database()
        self.ensure_tables = planetary.ensure_tables.__wrapped__
        self.ensure_tables()
        self.stack.enter_context(patch.object(planetary, "ensure_tables"))
        self.gate = self.stack.enter_context(
            patch.object(planetary, "feature_enabled_for", return_value=True))
        self.app = FastAPI()
        self.app.include_router(planetary.router)
        self.app.dependency_overrides[planetary.require_context] = lambda: 111
        self.client = self.stack.enter_context(TestClient(self.app))

    def set_up_database(self):
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(db, "_IS_POSTGRES", False))
        self.stack.enter_context(patch.object(db, "_SQLITE_PATH", Path(folder) / "test.db"))
        # Production's Postgres context manager releases its connection. SQLite's
        # commits but leaves it open, so close those test connections on context exit.
        class ClosingConnection(db._TimedSQLiteConnection):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    self.close()
        self.stack.enter_context(patch.object(db, "_TimedSQLiteConnection", ClosingConnection))

    def seed(self, status="pending", owner=111):
        with db.get_connection() as con:
            return con.execute(
                "INSERT INTO pp_planet_submissions "
                "(context_id, raw_text, planet_count, status, reviewed_at) "
                "VALUES (?, 'private paste', 3, ?, ?) RETURNING id",
                (owner, status, None if status == "pending" else "2026-10-20 12:00:00"),
            ).fetchone()["id"]

    def reviews(self):
        response = self.client.get("/api/planet-submissions/reviews")
        self.assertEqual(response.status_code, 200)
        return response.json()["reviews"]

    def ack(self, sub_id):
        return self.client.post(f"/api/planet-submissions/{sub_id}/acknowledge")

    def test_review_outcomes_remain_until_owner_acknowledges(self):
        self.app.dependency_overrides[planetary.require_admin] = lambda: 999
        row = {"system": "Test", "planet_num": 1, "planet_type": "Barren", "constellation": ""}
        for outcome in ("approved", "rejected"):
            with self.subTest(outcome=outcome):
                sub_id = self.seed()
                self.assertEqual(self.reviews(), [])
                action = "approve" if outcome == "approved" else "reject"
                with patch.object(planetary, "_parse_planet_rows", return_value=([row], 0, [])), \
                        patch.object(planetary, "_invalidate_planetdb_cache"):
                    response = self.client.post(f"/api/planet-submissions/{sub_id}/{action}")
                self.assertEqual(response.status_code, 200, response.text)
                first = self.reviews()
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["id"], sub_id)
                self.assertEqual(first[0]["status"], outcome)
                self.assertIsNotNone(first[0]["reviewed_at"])
                self.assertEqual(self.reviews(), first, "reading is not acknowledgment")
                self.assertEqual(self.ack(sub_id).status_code, 200)
                self.assertEqual(self.reviews(), [])
                self.assertEqual(self.ack(sub_id).status_code, 200, "dismissal is idempotent")
                with db.get_connection() as con:
                    stored = con.execute("SELECT status, review_seen_at FROM pp_planet_submissions "
                                         "WHERE id=?", (sub_id,)).fetchone()
                self.assertEqual(stored["status"], outcome)
                self.assertIsNotNone(stored["review_seen_at"])

    def test_only_owner_sees_or_acknowledges_reviews(self):
        own = self.seed("approved")
        other = self.seed("rejected", owner=222)
        pending = self.seed()
        reviews = self.reviews()
        self.assertEqual([r["id"] for r in reviews], [own])
        self.assertNotIn("raw_text", reviews[0])
        self.assertNotIn("context_id", reviews[0])
        for sub_id in (other, pending, 999999):
            self.assertEqual(self.ack(sub_id).status_code, 404)
        with db.get_connection() as con:
            self.assertIsNone(con.execute("SELECT review_seen_at FROM pp_planet_submissions "
                                          "WHERE id=?", (other,)).fetchone()["review_seen_at"])

    def test_requires_login_and_admin_review_remains_protected(self):
        sub_id = self.seed("approved")
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/api/planet-submissions/reviews").status_code, 401)
        self.assertEqual(self.ack(sub_id).status_code, 401)
        self.assertEqual(self.client.get("/api/planet-submissions").status_code, 403)
        self.assertEqual(self.client.post(f"/api/planet-submissions/{sub_id}/approve").status_code, 403)

    def test_zero_filled_paste_survives_submission_preview_and_approval(self):
        names = sorted({name for resources in planetary.PLANET_P0_MAP.values() for name in resources})
        values = ["42" if name in planetary.PLANET_P0_MAP["Storm"] else "0" for name in names]
        text = "\t".join(["System", "Planet", *names]) + "\n" + "\t".join(["Test", "1", *values])
        with db.get_connection() as con:
            con.execute("CREATE TABLE system_geo (system TEXT, constellation TEXT)")
            con.execute("INSERT INTO system_geo VALUES ('Test', 'Fixture')")
        with patch.object(planetary, "is_admin", return_value=False), \
                patch.object(planetary, "_submitter_name", return_value="Fixture"), \
                patch.object(planetary, "notify_admin_discord"):
            response = self.client.post("/api/planets/import", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"queued": True, "submitted": 1, "skipped": 0, "errors": []})
        with db.get_connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM pp_planets").fetchone()[0], 0)
        self.app.dependency_overrides[planetary.require_admin] = lambda: 999
        response = self.client.get("/api/planet-submissions")
        self.assertEqual(response.status_code, 200, response.text)
        submission = response.json()["submissions"][0]
        self.assertEqual(submission["planets"][0]["planet_type"], "Storm")
        with patch.object(planetary, "_invalidate_planetdb_cache"):
            response = self.client.post(f"/api/planet-submissions/{submission['id']}/approve")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"approved": True, "imported": 1, "skipped": 0, "errors": []})
        with db.get_connection() as con:
            row = con.execute("SELECT * FROM pp_planets").fetchone()
            self.assertEqual(row["planet_type"], "Storm")
            self.assertEqual(row["suspended_plasma"], 42)
            self.assertEqual(row["reactive_gas"], 0)

    def test_disabled_feature_preserves_unread_results(self):
        sub_id = self.seed("approved")
        self.gate.return_value = False
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.ack(sub_id).status_code, 404)
        self.gate.return_value = True
        self.assertEqual([r["id"] for r in self.reviews()], [sub_id])

    def test_repeated_dismissal_preserves_first_acknowledgment(self):
        sub_id = self.seed("approved")
        self.assertEqual(self.ack(sub_id).status_code, 200)
        first_seen = "2026-10-20T12:34:56"
        with db.get_connection() as con:
            con.execute("UPDATE pp_planet_submissions SET review_seen_at=? WHERE id=?", (first_seen, sub_id))
        self.assertEqual(self.ack(sub_id).status_code, 200)
        with db.get_connection() as con:
            stored = con.execute("SELECT review_seen_at FROM pp_planet_submissions WHERE id=?", (sub_id,)).fetchone()
        self.assertEqual(stored["review_seen_at"], first_seen)
        self.assertEqual(self.reviews(), [])

    def test_bounded_results_do_not_lose_older_reviews(self):
        ids = [self.seed("approved") for _ in range(21)]
        self.assertEqual([r["id"] for r in self.reviews()], list(reversed(ids[1:])))
        self.ack(ids[-1])
        self.assertIn(ids[0], [r["id"] for r in self.reviews()])

    def test_migration_preserves_existing_reviews_and_dismissals(self):
        sub_id = self.seed("rejected")
        with db.get_connection() as con:
            con.execute("DROP INDEX idx_pp_planet_submission_reviews")
            con.execute("ALTER TABLE pp_planet_submissions DROP COLUMN review_seen_at")
        self.ensure_tables()
        self.assertEqual([r["id"] for r in self.reviews()], [sub_id])
        self.ack(sub_id)
        self.ensure_tables()
        self.assertEqual(self.reviews(), [], "repeated migrations preserve acknowledgment")


if __name__ == "__main__":
    unittest.main()
