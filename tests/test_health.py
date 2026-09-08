import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.check_health import check_health


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.db"
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    def tearDown(self):
        self.tmp.cleanup()

    def write_timestamp(self, value):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_successful_scan_at', ?)", (value,))

    def test_missing_metadata_fails(self):
        with self.assertRaisesRegex(RuntimeError, "missing"):
            check_health(self.db, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_recent_scan_passes_with_injected_clock(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.write_timestamp("2026-01-01T11:30:00Z")
        check_health(self.db, now=now)

    def test_stale_scan_fails(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.write_timestamp("2026-01-01T11:14:59+00:00")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            check_health(self.db, now=now)

    def test_future_scan_beyond_tolerance_fails(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.write_timestamp("2026-01-01T12:05:01Z")
        with self.assertRaisesRegex(RuntimeError, "future"):
            check_health(self.db, now=now)

    def test_small_clock_skew_is_allowed(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.write_timestamp("2026-01-01T12:05:00Z")
        check_health(self.db, now=now)

    def test_malformed_timestamp_fails(self):
        self.write_timestamp("not-a-timestamp")
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            check_health(self.db, now=datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
