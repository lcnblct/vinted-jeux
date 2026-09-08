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
        llm_filter._ref_b64_cache.clear()

    def test_patchwork_reference_is_loaded_from_repository(self):
        result = llm_filter._fetch_ref_b64(
            "references/patchwork-10e-anniversaire.jpg"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "image/jpeg")
        self.assertGreater(len(result[0]), 400)

    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=False)
    @patch.object(
        llm_filter,
        "_fetch_image_b64",
        return_value=("bGlzdGluZw==", "image/jpeg"),
    )
    @patch.object(
        llm_filter.requests,
        "post",
        return_value=FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"is_true_game": true, "reason": "ok", "confidence": 0.9}'
                        }
                    }
                ]
            }
        ),
    )
    def test_patchwork_reference_and_listing_photo_are_sent(self, post, image):
        result = llm_filter.is_true_positive(
            "Patchwork 10e Anniversaire",
            "Patchwork",
            image_urls=["https://vinted.example/photo.jpg"],
            max_images=1,
        )
        self.assertTrue(result[0])
        content = post.call_args.kwargs["json"]["messages"][0]["content"]
        image_parts = [part for part in content if part.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 2)
        self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(image_parts[1]["image_url"]["url"], "data:image/jpeg;base64,bGlzdGluZw==")

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
