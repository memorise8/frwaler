import json
import unittest
import urllib.error

from delivery.translation.providers import (
    OllamaProvider, OpenAICompatibleProvider, ProviderError, TranslationRequest,
)


class _Response:
    def __init__(self, body): self.body = json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self): return self.body


class TranslationProviderTest(unittest.TestCase):
    request = TranslationRequest("Budget 2025", "en", "ko-KR", "title")

    def test_external_provider_contract(self):
        seen = {}
        def opener(request, timeout):
            seen.update(headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
            return _Response({"choices": [{"message": {"content": "2025 예산"}}]})
        result = OpenAICompatibleProvider(endpoint="https://api.test/v1/chat/completions",
            model="test-model", api_key="secret", opener=opener).translate(self.request)
        self.assertEqual(result.text, "2025 예산")
        self.assertEqual(result.provider, "external")
        self.assertEqual(result.model_version, "test-model")
        self.assertNotIn("secret", json.dumps(seen["body"]))

    def test_internal_provider_contract(self):
        result = OllamaProvider(endpoint="http://model.test/api/generate", model="qwen",
            opener=lambda *_args, **_kwargs: _Response({"response": "2025 예산"})).translate(self.request)
        self.assertEqual((result.text, result.provider), ("2025 예산", "internal"))

    def test_normalized_errors(self):
        def denied(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://api.test", 401, "denied", {}, None)
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleProvider(endpoint="https://api.test", model="m", api_key=None,
                                     opener=denied).translate(self.request)
        self.assertEqual(caught.exception.code, "auth")
        self.assertFalse(caught.exception.retryable)

    def test_rejects_empty_and_invalid_response(self):
        provider = OllamaProvider(endpoint="http://model.test", model="m",
                                  opener=lambda *_a, **_k: _Response({}))
        with self.assertRaises(ProviderError) as empty:
            provider.translate(TranslationRequest(" ", "en", "ko-KR", "title"))
        self.assertEqual(empty.exception.code, "invalid_response")
        with self.assertRaises(ProviderError) as invalid:
            provider.translate(self.request)
        self.assertEqual(invalid.exception.code, "invalid_response")


if __name__ == "__main__": unittest.main()
