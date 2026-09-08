import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_bot_description.py"
SPEC = importlib.util.spec_from_file_location("update_bot_description", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise MODULE.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class BotDescriptionTests(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertTrue(callable(MODULE.main))

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:token"}, clear=False)
    @patch.object(MODULE.requests, "post")
    def test_env_token_and_api_calls_are_mocked(self, post):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("queries:\n  - name: Koi\n    price_max: 20\n")
            cfg.flush()
            post.return_value = FakeResponse({"ok": True, "result": {}})
            with patch.object(MODULE, "CONFIG_PATH", Path(cfg.name)):
                self.assertEqual(MODULE.main(), 0)
                self.assertEqual(post.call_count, 2)

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:token"}, clear=False)
    @patch.object(MODULE.requests, "post", return_value=FakeResponse({"ok": False}))
    def test_api_failure_returns_error(self, post):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as cfg:
            cfg.write("queries: []")
            cfg.flush()
            with patch.object(MODULE, "CONFIG_PATH", Path(cfg.name)):
                self.assertEqual(MODULE.main(), 1)


if __name__ == "__main__":
    unittest.main()
