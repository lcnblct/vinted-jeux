import argparse
import contextlib
import io
import os
import unittest
from unittest.mock import patch

import monitor


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.con = monitor.init_db(':memory:')
        self.cfg = {'queries': [{'name': 'Cascadia', 'url': 'https://www.vinted.fr/catalog?search_text=cascadia', 'price_max': 22}],
                    'settings': {'llm_filter': {'enabled': False}}, 'filters': {}}
        self.args = argparse.Namespace(once=True, limit=None, force_notify=False, once_no_notify=False, no_llm=False, verbose=False)
        self.item = {'id': 42, 'title': 'Cascadia', 'price': 12, 'url': 'https://www.vinted.fr/items/42'}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '123'}, clear=True))
        self.stack.enter_context(patch.object(monitor.time, 'sleep'))
        self.history = self.stack.enter_context(patch.object(monitor, 'append_history'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        # No live request may escape an offline test.
        self.stack.enter_context(patch.object(monitor.requests, 'post', side_effect=AssertionError('unexpected network')))
        monitor.set_meta(self.con, 'last_watchlist_date', monitor._paris_today_iso())

    def tearDown(self):
        self.stack.close()
        self.con.close()

    def test_failure_retries_without_item_in_search_and_audits(self):
        with patch.object(monitor, 'fetch_items', return_value=[self.item]), patch.object(monitor, '_send_outbox_payload', return_value=False) as send:
            with self.assertRaises(monitor.ScanFetchError):
                monitor.check_once(self.cfg, self.con, self.args)
            self.assertEqual(send.call_count, 1)
            self.assertFalse(monitor.is_seen(self.con, '42'))
        with patch.object(monitor, 'fetch_items', return_value=[]), patch.object(monitor, '_send_outbox_payload', return_value=True) as send:
            monitor.check_once(self.cfg, self.con, self.args)
            self.assertEqual(send.call_count, 1)
        self.assertTrue(monitor.is_seen(self.con, '42'))
        self.assertTrue(monitor.get_meta(self.con, 'last_successful_scan_at'))
        self.assertEqual(self.history.call_count, 1)

    def test_partial_delivery_only_retries_failed_recipient(self):
        with patch.dict(os.environ, {'TELEGRAM_CHAT_ID': '123,456'}):
            with patch.object(monitor, 'fetch_items', return_value=[self.item]), patch.object(monitor, '_send_outbox_payload', side_effect=[True, False]):
                with self.assertRaises(monitor.ScanFetchError):
                    monitor.check_once(self.cfg, self.con, self.args)
            with patch.object(monitor, 'fetch_items', return_value=[]), patch.object(monitor, '_send_outbox_payload', return_value=True) as send:
                monitor.check_once(self.cfg, self.con, self.args)
                self.assertEqual(send.call_count, 1)

    def test_watchlist_partial_delivery_retries_once_without_duplicate_alert(self):
        monitor.set_meta(self.con, "last_watchlist_date", "2000-01-01")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "123,456"}):
            with patch.object(monitor, "fetch_items", return_value=[self.item]), patch.object(monitor, "_send_outbox_payload", side_effect=[True, False, True, True]) as send:
                with self.assertRaises(monitor.ScanFetchError):
                    monitor.check_once(self.cfg, self.con, self.args)
                self.assertEqual(send.call_count, 4)
            with patch.object(monitor, "fetch_items", return_value=[]), patch.object(monitor, "_send_outbox_payload", return_value=True) as send:
                monitor.check_once(self.cfg, self.con, self.args)
                self.assertEqual(send.call_count, 1)
        self.assertEqual(monitor.get_meta(self.con, "last_watchlist_date"), monitor._paris_today_iso())
        self.assertEqual(self.history.call_count, 2)

    def test_dry_run_never_retries_or_sends(self):
        monitor._outbox_enqueue(self.con, 'old', {'discord': {'kind': 'discord', 'text': 'pending'}})
        self.args.limit = 5
        with patch.object(monitor, 'fetch_items', return_value=[self.item]), patch.object(monitor, '_send_outbox_payload') as send:
            monitor.check_once(self.cfg, self.con, self.args)
            send.assert_not_called()
        self.assertFalse(monitor.is_seen(self.con, '42'))
        self.assertIsNone(monitor.get_meta(self.con, 'last_successful_scan_at'))

    def test_total_fetch_failure_visible(self):
        with patch.object(monitor, 'fetch_items', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(monitor.ScanFetchError):
                monitor.check_once(self.cfg, self.con, self.args)
        self.assertIsNone(monitor.get_meta(self.con, 'last_successful_scan_at'))

    def test_rejected_variant_does_not_block_base_game(self):
        variant = dict(self.cfg['queries'][0], name='Rolling Hills', url='https://www.vinted.fr/catalog?search_text=rolling')
        self.cfg['queries'].insert(0, variant)
        self.cfg['settings']['llm_filter']['enabled'] = True
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test'}), patch.object(monitor, 'fetch_items', return_value=[self.item]), patch.object(monitor, 'enrich_item_description', return_value=''), patch.object(monitor.llm_filter, 'is_true_positive', side_effect=[(False, 'base game', .9, {}), (True, 'correct', .9, {})]) as classify, patch.object(monitor, '_send_outbox_payload', return_value=True):
            monitor.check_once(self.cfg, self.con, self.args)
            self.assertEqual(classify.call_count, 2)
        self.assertTrue(monitor.is_seen(self.con, '42'))

    def test_budget_preserves_query_cursor(self):
        self.cfg['queries'].append(dict(self.cfg['queries'][0], name='Aqua', url='https://www.vinted.fr/catalog?search_text=aqua'))
        with patch.object(monitor.time, 'monotonic', side_effect=[0, 0, 200]), patch.object(monitor, 'fetch_items', return_value=[]) as fetch:
            with self.assertRaises(monitor.ScanBudgetExceeded):
                monitor.check_once(self.cfg, self.con, self.args)
            self.assertEqual(fetch.call_count, 1)
        self.assertTrue(monitor.get_meta(self.con, 'scan_resume').endswith('|1'))
        with patch.object(monitor, 'fetch_items', return_value=[]) as fetch:
            monitor.check_once(self.cfg, self.con, self.args)
            self.assertIn('aqua', fetch.call_args_list[0].args[0])


if __name__ == '__main__':
    unittest.main()
