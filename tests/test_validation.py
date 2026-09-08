import copy
import unittest
from types import SimpleNamespace

import monitor


class ValidationTests(unittest.TestCase):
    def config(self):
        return {"queries": [{"name": "Cascadia", "url": "https://www.vinted.fr/catalog?search_text=cascadia", "price_max": 22}]}

    def test_price_forms(self):
        for raw in ("12,50", "12.50 €", {"amount": "12.50", "currency_code": "EUR"}):
            self.assertEqual(monitor._parse_price_float({"price": raw}), 12.5)
        self.assertEqual(monitor._parse_price_float(SimpleNamespace(price="12.50", currency="EUR")), 12.5)

    def test_unknown_nonfinite_negative_and_foreign_prices_deferred(self):
        for raw in (None, True, "NaN", "inf", "-1", "unknown", {"amount": 10, "currency_code": "USD"}):
            item = {"title": "Cascadia", "price": raw}
            self.assertEqual(monitor.apply_filters([item], {}, {"price_max": 22}), [])

    def test_sold_and_overpriced_excluded(self):
        for item in ({"title": "Cascadia", "price": {"amount": "999", "currency_code": "EUR"}},
                     {"title": "Cascadia", "price": 10, "is_sold": True}):
            self.assertEqual(monitor.apply_filters([item], {"exclude_sold": True}, {"price_max": 22}), [])

    def test_zero_threshold_preserved(self):
        self.assertEqual(monitor.apply_filters([{"title": "Game", "price": 1}], {}, {"price_max": 0}), [])

    def test_valid_config(self):
        cfg = self.config()
        self.assertIs(monitor.validate_config(cfg), cfg)

    def test_bad_config_fails_before_scan(self):
        cases = [None, {}, {"queries": []}]
        for patch in ({"price_max": -1}, {"price_max": "22"}, {"must_contain": "cascadia"}, {"url": "https://example.com"}):
            cfg = self.config()
            cfg["queries"][0].update(patch)
            cases.append(cfg)
        for settings in ({"per_page": 2.5}, {"poll_interval": 0}, {"llm_filter": {"confidence_threshold": 2}}):
            cfg = self.config()
            cfg["settings"] = settings
            cases.append(cfg)
        duplicate = self.config()
        duplicate["queries"].append(copy.deepcopy(duplicate["queries"][0]))
        cases.append(duplicate)
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                monitor.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
