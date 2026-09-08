import unittest
from unittest.mock import patch

import monitor


class FakeScraper:
    def __init__(self, items=None):
        self.items = items or []
        self.search_calls = 0

    def search(self, params):
        self.search_calls += 1
        return self.items


class FetchTests(unittest.TestCase):
    def setUp(self):
        monitor._scraper_cache.clear()
        monitor._french_scraper = None

    def tearDown(self):
        monitor._scraper_cache.clear()
        monitor._french_scraper = None

    def test_reuses_one_session_for_watchlist_queries(self):
        scraper = FakeScraper([{"id": "1"}])
        with patch("vinted_scraper.VintedScraper", return_value=scraper) as constructor:
            first = monitor.fetch_items("https://www.vinted.fr/catalog?search_text=one")
            second = monitor.fetch_items("https://www.vinted.fr/catalog?search_text=two")

        self.assertEqual(first, [{"id": "1"}])
        self.assertEqual(second, [{"id": "1"}])
        constructor.assert_called_once_with("https://www.vinted.fr")
        self.assertEqual(scraper.search_calls, 2)

    def test_retries_transient_session_cookie_failure(self):
        scraper = FakeScraper([{"id": "1"}])
        with patch(
            "vinted_scraper.VintedScraper",
            side_effect=[RuntimeError("status code: 406"), scraper],
        ) as constructor, patch.object(monitor.time, "sleep") as sleep:
            result = monitor.fetch_items(
                "https://www.vinted.fr/catalog?search_text=one"
            )

        self.assertEqual(result, [{"id": "1"}])
        self.assertEqual(constructor.call_count, 2)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
