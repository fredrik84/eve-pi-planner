"""A database replacement must not let readiness checks permanently drain the pool."""
import queue
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

from fastapi.testclient import TestClient
from app import db
from app.main import app


class Connection:
    def __init__(self, dead=False):
        self.closed = 0
        self.dead = dead
        self.autocommit = False

    def cursor(self):
        return self

    def execute(self, *args):
        if self.dead:
            raise RuntimeError("server closed the connection unexpectedly")

    def rollback(self):
        if self.dead:
            raise RuntimeError("connection already closed")

    def commit(self):
        self.rollback()

    def close(self):
        self.closed = 1


class ReadinessRecoveryTests(unittest.TestCase):
    def test_database_restart_during_active_probes_preserves_every_pool_slot(self):
        # Recently used sockets skip the idle ping. PostgreSQL is then replaced, so
        # SELECT 1 fails inside the endpoint rather than during pool acquisition.
        pool = queue.Queue()
        for _ in range(db._PG_POOL_SIZE):
            pool.put((Connection(dead=True), time.monotonic()))
        client = TestClient(app)  # No lifespan: no scheduler, migrations or real database.
        self.addCleanup(client.close)
        with patch.object(db, "_IS_POSTGRES", True), patch.object(db, "_pg_pool", return_value=pool), \
                patch("psycopg2.connect", side_effect=lambda *a, **kw: Connection()):
            for _ in range(db._PG_POOL_SIZE):
                response = client.get("/healthz")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["status"], "error")
                self.assertEqual(pool.qsize(), db._PG_POOL_SIZE,
                                 "a failed readiness probe leaked a pool slot")
            # Every original socket is dead; the same process must now heal itself.
            for _ in range(db._PG_POOL_SIZE * 2):
                response = client.get("/healthz")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok"})
                self.assertEqual(pool.qsize(), db._PG_POOL_SIZE)

    def test_failed_connection_setup_preserves_its_slot(self):
        class SetupFailure(Connection):
            @property
            def autocommit(self):
                return False

            @autocommit.setter
            def autocommit(self, value):
                if getattr(self, "fail_setup", False):
                    raise RuntimeError("connection failed during setup")

        connection = SetupFailure()
        connection.fail_setup = True
        pool = queue.Queue()
        pool.put((connection, time.monotonic()))
        with patch.object(db, "_IS_POSTGRES", True), patch.object(db, "_pg_pool", return_value=pool), \
                patch("psycopg2.connect", return_value=Connection()):
            with self.assertRaisesRegex(RuntimeError, "failed during setup"):
                db.get_connection()
            self.assertEqual(pool.qsize(), 1)
            self.assertTrue(connection.closed)
            with db.get_connection() as recovered:
                recovered.execute("SELECT 1")
            self.assertEqual(pool.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
