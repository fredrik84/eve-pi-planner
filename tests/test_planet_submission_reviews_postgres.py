"""Run the review endpoint contracts against PostgreSQL, including timestamp typing.

Set PLANET_REVIEWS_TEST_DSN to a disposable test database. Each test uses its own
temporary schema; no application tables or existing schemas are changed.
"""
import os
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, ".")

import psycopg2
from psycopg2 import extensions, sql
from app import db
import test_planet_submission_reviews as reviews


@unittest.skipUnless(os.environ.get("PLANET_REVIEWS_TEST_DSN"), "requires a disposable PostgreSQL database")
class PostgresSubmissionReviewsTests(reviews.SubmissionReviewsTests):
    def set_up_database(self):
        admin = psycopg2.connect(os.environ["PLANET_REVIEWS_TEST_DSN"])
        admin.autocommit = True
        self.stack.callback(admin.close)
        schema = "planet_reviews_test_" + uuid.uuid4().hex
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def drop_schema():
            with admin.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

        self.stack.callback(drop_schema)
        dsn = extensions.make_dsn(os.environ["PLANET_REVIEWS_TEST_DSN"], options=f"-csearch_path={schema}")
        self.stack.enter_context(patch.object(db, "DATABASE_URL", dsn))
        self.stack.enter_context(patch.object(db, "_IS_POSTGRES", True))
        self.stack.enter_context(patch.object(db, "_PG_POOL", None))

        def close_pool():
            if db._PG_POOL is not None:
                while not db._PG_POOL.empty():
                    connection, _ = db._PG_POOL.get_nowait()
                    if connection is not None:
                        connection.close()

        self.stack.callback(close_pool)


if __name__ == "__main__":
    unittest.main()
