import json
import unittest
from unittest.mock import patch

import llm_filter


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class LlmFilterTests(unittest.TestCase):
    def setUp(self):
        llm_filter._cache.clear()

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    @patch.object(llm_filter, "_fetch_image_b64", return_value=None)
    @patch.object(llm_filter, "_fetch_ref_b64", return_value=None)
    @patch.object(
        llm_filter.requests,
        "post",
        return_value=FakeResponse(
            {"choices": [{"message": {"content": json.dumps({"is_true_game": False, "reason": "faux"})}}]}
        ),
    )
    def test_missing_confidence_is_uncertain_fail_open(self, post, ref, image):
        result = llm_filter.is_true_positive("Koi", "Annonce", model="google/gemini-2.5-flash")
        self.assertEqual(result[0], True)
        self.assertEqual(result[2], 0.0)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "google/gemini-2.5-flash")

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    @patch.object(llm_filter, "_fetch_image_b64", return_value=None)
    @patch.object(llm_filter, "_fetch_ref_b64", return_value=None)
    @patch.object(
        llm_filter.requests,
        "post",
        return_value=FakeResponse(
            {"choices": [{"message": {"content": '{"is_true_game": "false", "confidence": 0.99}'}}]}
        ),
    )
    def test_non_boolean_is_uncertain_fail_open(self, post, ref, image):
        result = llm_filter.is_true_positive("Koi", "Annonce", model="model-a")
        self.assertEqual(result[0], True)
        self.assertEqual(result[2], 0.0)


if __name__ == "__main__":
    unittest.main()
