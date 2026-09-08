import importlib.util
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "merge_state.py"
spec = importlib.util.spec_from_file_location("merge_state", SCRIPT)
merge_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_state)


class StateMergeTests(unittest.TestCase):
    def test_merges_all_tables_and_keeps_latest_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = root / "first.db", root / "second.db", root / "out.db"
            with closing(sqlite3.connect(first)) as con, con:
                con.executescript(
                    """
                    CREATE TABLE seen (id TEXT PRIMARY KEY, title TEXT);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE outbox (id TEXT PRIMARY KEY, payload TEXT);
                    INSERT INTO seen VALUES ('a', 'A');
                    INSERT INTO meta VALUES ('last_watchlist_date', '2026-09-07');
                    INSERT INTO outbox VALUES ('msg-a', 'one');
                    """
                )
            with closing(sqlite3.connect(second)) as con, con:
                con.executescript(
                    """
                    CREATE TABLE seen (id TEXT PRIMARY KEY, title TEXT);
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE rejections (id TEXT PRIMARY KEY, reason TEXT);
                    INSERT INTO seen VALUES ('b', 'B');
                    INSERT INTO meta VALUES ('last_watchlist_date', '2026-09-08');
                    INSERT INTO rejections VALUES ('item-b', 'not a game');
                    """
                )
            merge_state._merge_db(output, [first, second])
            with closing(sqlite3.connect(output)) as con, con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT value FROM meta WHERE key='last_watchlist_date'").fetchone()[0], "2026-09-08")
                self.assertEqual(con.execute("SELECT payload FROM outbox").fetchone()[0], "one")
                self.assertEqual(con.execute("SELECT reason FROM rejections").fetchone()[0], "not a game")

    def test_sent_delivery_wins_in_both_merge_orders(self):
        import monitor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending, sent, output = root / "pending.db", root / "sent.db", root / "merged.db"
            for db, status in ((pending, "pending"), (sent, "sent")):
                with closing(monitor.init_db(db)) as con:
                    monitor._outbox_enqueue(con, "42", {"discord": {"kind": "discord"}})
                    con.execute("UPDATE delivery_outbox SET status=?", (status,))
                    con.commit()
            for inputs in ([pending, sent], [sent, pending]):
                merge_state._merge_db(output, inputs)
                with closing(sqlite3.connect(output)) as con:
                    self.assertEqual(con.execute("SELECT status FROM delivery_outbox").fetchone()[0], "sent")

    def test_history_is_union_sorted_with_one_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two, output = root / "one.log", root / "two.log", root / "merged.log"
            one.write_text("# audit\n2026-09-08 | item-b\n2026-09-07 | item-a\n", encoding="utf-8")
            two.write_text("# another header\n2026-09-08 | item-b\n2026-09-09 | item-c\n", encoding="utf-8")
            merge_state.merge_history(output, [one, two])
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "# audit\n2026-09-07 | item-a\n2026-09-08 | item-b\n2026-09-09 | item-c\n",
            )


if __name__ == "__main__":
    unittest.main()
