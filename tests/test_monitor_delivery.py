import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = monitor.init_db(Path(self.tmp.name) / "seen.db")
        self.old_history = monitor.SENT_HISTORY
        monitor.SENT_HISTORY = Path(self.tmp.name) / "history.log"

    def tearDown(self):
        self.con.close()
        monitor.SENT_HISTORY = self.old_history
        self.tmp.cleanup()

    def test_failed_delivery_stays_in_outbox_and_retries(self):
        payload = {"kind": "discord", "text": "hello",
                   "item_title": "Game", "item_price": "10 EUR", "item_url": "u"}
        monitor._outbox_enqueue(self.con, "42", {"discord": payload})
        with patch.object(monitor, "_send_outbox_payload", side_effect=[False, True]):
            self.assertNotIn("42", monitor.process_outbox(self.con))
            self.assertIn("42", monitor.process_outbox(self.con))
        self.assertTrue(monitor.is_seen(self.con, "42"))

    def test_sent_row_finalizes_after_interruption_without_resending(self):
        monitor._outbox_enqueue(self.con, "crash", {"discord": {"kind": "discord", "item_title": "Game"}})
        self.con.execute("UPDATE delivery_outbox SET status='sent' WHERE item_id='crash'")
        self.con.commit()
        with patch.object(monitor, "_send_outbox_payload") as send:
            self.assertIn("crash", monitor.process_outbox(self.con))
            self.assertNotIn("crash", monitor.process_outbox(self.con))
            send.assert_not_called()
        self.assertTrue(monitor.is_seen(self.con, "crash"))

    def test_telegram_application_failure_is_not_success(self):
        response = type("Response", (), {"status_code": 200, "json": lambda self: {"ok": False}})()
        with patch.object(monitor.requests, "post", return_value=response) as post:
            self.assertFalse(monitor._notify_telegram_one("token", "123", "hello"))
        self.assertEqual(post.call_count, 1)

    def test_whatsapp_application_failure_is_not_success(self):
        response = type("Response", (), {"status_code": 200, "text": "ERROR: invalid key"})()
        with patch.object(monitor.requests, "get", return_value=response):
            self.assertFalse(monitor.notify_whatsapp("phone", "key", "hello"))

    def test_outbox_does_not_store_credentials(self):
        deliveries = monitor._configured_deliveries(
            "42", "Game", "10 EUR", "url", "", "Name", "md", "plain", "wa",
            "BOT_SECRET", "123", "DISCORD_SECRET", "PHONE", "API_SECRET", "NTFY_SECRET")
        monitor._outbox_enqueue(self.con, "42", deliveries)
        values = " ".join(row[0] for row in self.con.execute("SELECT payload FROM delivery_outbox"))
        for secret in ("BOT_SECRET", "DISCORD_SECRET", "API_SECRET", "NTFY_SECRET"):
            self.assertNotIn(secret, values)

    def test_llm_rejection_is_scoped_to_query_and_version(self):
        q1 = {"name": "Cascadia", "url": "https://vinted/search?a"}
        q2 = {"name": "Cascadia Hills", "url": "https://vinted/search?b"}
        monitor.mark_llm_rejected(self.con, "42", q1, "v1", "wrong edition", 0.9)
        self.assertTrue(monitor.is_llm_rejected(self.con, "42", q1, "v1"))
        self.assertFalse(monitor.is_llm_rejected(self.con, "42", q2, "v1"))
        self.assertFalse(monitor.is_llm_rejected(self.con, "42", q1, "v2"))
        self.assertFalse(monitor.is_seen(self.con, "42"))

    def test_telegram_falls_back_to_plain_text(self):
        response = type("Response", (), {"status_code": 400, "text": "bad markdown", "json": lambda self: {"ok": False}})()
        ok = type("Response", (), {"status_code": 200, "text": "ok", "json": lambda self: {"ok": True}})()
        with patch.object(monitor.requests, "post", side_effect=[response, ok]) as post:
            self.assertTrue(monitor._notify_telegram_one("token", "123", "*broken [title]"))
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("parse_mode", post.call_args_list[-1].kwargs.get("data", {}))

    def test_telegram_does_not_fallback_on_rate_limit(self):
        response = type("Response", (), {"status_code": 429, "text": "slow", "json": lambda self: {"ok": False}})()
        with patch.object(monitor.requests, "post", return_value=response) as post:
            self.assertFalse(monitor._notify_telegram_one("token", "123", "hello"))
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
