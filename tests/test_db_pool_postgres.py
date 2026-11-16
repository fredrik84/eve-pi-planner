"""Pool ownership and recovery against an explicitly supplied, disposable PostgreSQL DB."""
import gc
import os
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

sys.path.insert(0, ".")

import psycopg2
from psycopg2 import sql
from app import db


@unittest.skipUnless(os.environ.get("DB_POOL_TEST_DSN"), "requires disposable DB_POOL_TEST_DSN")
class PostgresPoolTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg2.connect(os.environ["DB_POOL_TEST_DSN"])
        self.admin.autocommit = True
        self.addCleanup(self.admin.close)
        self.schema = "pool_test_" + uuid.uuid4().hex
        with self.admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            cursor.execute(sql.SQL("CREATE TABLE {}.writes (value integer)").format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.patches = [patch.object(db, "DATABASE_URL", os.environ["DB_POOL_TEST_DSN"]),
                        patch.object(db, "_IS_POSTGRES", True),
                        patch.object(db, "_PG_POOL", None), patch.object(db, "_PG_POOL_SIZE", 2),
                        patch.object(db, "_PG_POOL_WAIT_SECONDS", 0.1)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.close_pool)

    def drop_schema(self):
        with self.admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def close_pool(self):
        if db._PG_POOL is not None:
            while not db._PG_POOL.empty():
                connection, _ = db._PG_POOL.get_nowait()
                db._pg_discard(connection)

    def test_abandoned_transaction_is_rolled_back_and_capacity_recovers(self):
        connection = db.get_connection()
        connection.execute(f"INSERT INTO {self.schema}.writes VALUES (7)")
        del connection
        gc.collect()
        self.assertEqual(db._pg_pool().qsize(), 2)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute(f"SELECT count(*) FROM {self.schema}.writes").fetchone()[0], 0)
        self.assertEqual(db._pg_pool().qsize(), 2)

    def test_terminated_backend_does_not_reduce_capacity(self):
        connection = db.get_connection()
        pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        with self.admin.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
        with self.assertRaises(psycopg2.Error):
            with connection:
                connection.execute("SELECT 1")
        self.assertEqual(db._pg_pool().qsize(), 2)
        for _ in range(4):
            with db.get_connection() as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(db._pg_pool().qsize(), 2)

    def test_duplicate_returns_never_share_a_session_between_borrowers(self):
        connection = db.get_connection()
        connection.close()
        connection.close()
        connection.__exit__(None, None, None)
        self.assertEqual(db._pg_pool().qsize(), 2)
        together = Barrier(2)

        def borrow():
            with db.get_connection() as connection:
                pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                together.wait(timeout=5)
                return pid

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(len(set(executor.map(lambda _: borrow(), range(2)))), 2)
        self.assertEqual(db._pg_pool().qsize(), 2)

    def test_real_exhaustion_waits_then_recovers_without_overbooking(self):
        held = [db.get_connection(), db.get_connection()]
        try:
            with self.assertRaisesRegex(RuntimeError, "pool exhausted"):
                db.get_connection()
            self.assertEqual(db._pg_pool().qsize(), 0)
        finally:
            for connection in held:
                connection.close()
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(db._pg_pool().qsize(), 2)


if __name__ == "__main__":
    unittest.main()
