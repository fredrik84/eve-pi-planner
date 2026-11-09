"""Run usage contracts in disposable PostgreSQL schemas using the shared test DSN."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
import test_usage
import test_planet_submission_reviews_postgres as postgres


@unittest.skipUnless(os.environ.get("PLANET_REVIEWS_TEST_DSN"), "requires a disposable PostgreSQL database")
class PostgresUsageTests(test_usage.UsageTests):
    def set_up_database(self):
        postgres.PostgresSubmissionReviewsTests.set_up_database(self)
        # Production table probes target public; this test owns an isolated schema.
        translate = test_usage.db._pg_translate
        self.stack.enter_context(patch.object(test_usage.db, "_pg_translate", side_effect=lambda sql:
            translate(sql).replace("table_schema='public'", "table_schema=current_schema()")))


if __name__ == "__main__":
    unittest.main()
