"""Provider-neutral translation clients with normalized failures."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

ERROR_CODES = {"auth", "quota", "rate_limit", "timeout", "invalid_response", "configuration", "internal"}


@dataclass(frozen=True)
class TranslationRequest:
    text: str
    source_lang: str
    target_locale: str
    source_field: str


@dataclass(frozen=True)
class SummaryRequest:
    title: str
    text: str
    source_lang: str
    target_locale: str = "ko-KR"


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider: str
    model_version: str
    prompt_version: str
    input_chars: int
    output_chars: int
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class SummaryResult:
    summary_text: str
    key_points: tuple[str, ...]
    institutions: tuple[str, ...]
    provider: str
    model_version: str
    prompt_version: str
    input_chars: int
    output_chars: int
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


class TranslationProvider(Protocol):
    name: str
    model: str
    prompt_version: str
    max_chars: int

    def translate(self, request: TranslationRequest) -> TranslationResult: ...
    def summarize(self, request: SummaryRequest) -> SummaryResult: ...


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown provider error code: {code}")
        super().__init__(message[:500])
        self.code = code
        self.retryable = retryable


def _prompt(request: TranslationRequest) -> str:
    return (
        f"Translate the following {request.source_field} from {request.source_lang or 'unknown'} "
        f"to {request.target_locale}. Return only the translation. Preserve names, numbers, dates, "
        f"and URLs exactly.\n\n{request.text}"
    )


def _summary_prompt(request: SummaryRequest) -> str:
    return (
        f"Summarize the following {request.source_lang or 'unknown'} document in Korean. "
        "Return one JSON object only with keys summary_ko, key_points, institutions. "
        "summary_ko must be 3 to 5 concise Korean sentences written in Hangul, even when the source "
        "is Chinese or Japanese. key_points must contain up to 5 Korean strings written in Hangul. "
        "institutions must contain only organization names explicitly present "
        "in the source; keep their original spelling. Do not invent facts or include markdown.\n\n"
        f"Title: {request.title}\n\nText: {request.text}"
    )


def _summary_data(output: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    cleaned = output.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
        summary = data["summary_ko"]
        points = data.get("key_points", [])
        institutions = data.get("institutions", [])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("invalid_response", "invalid structured summary", retryable=True) from exc
    if not isinstance(summary, str) or not summary.strip():
        raise ProviderError("invalid_response", "empty structured summary", retryable=True)
    if not isinstance(points, list) or not isinstance(institutions, list):
        raise ProviderError("invalid_response", "invalid structured summary lists", retryable=True)
    if any(not isinstance(item, str) for item in (*points, *institutions)):
        raise ProviderError("invalid_response", "invalid structured summary item", retryable=True)
    if not re.search(r"[가-힣]", summary) or any(item.strip() and not re.search(r"[가-힣]", item) for item in points):
        raise ProviderError("invalid_response", "summary is not Korean", retryable=True)
    return summary.strip(), tuple(item.strip() for item in points[:5] if item.strip()), tuple(
        item.strip() for item in institutions[:10] if item.strip())


def _http_error(exc: Exception) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return ProviderError("auth", f"provider HTTP {exc.code}", retryable=False)
        if exc.code == 429:
            return ProviderError("rate_limit", "provider rate limit", retryable=True)
        if exc.code in (402,):
            return ProviderError("quota", "provider quota exhausted", retryable=False)
        return ProviderError("internal", f"provider HTTP {exc.code}", retryable=exc.code >= 500)
    if isinstance(exc, (TimeoutError, urllib.error.URLError)):
        return ProviderError("timeout", "provider request timed out", retryable=True)
    return ProviderError("internal", type(exc).__name__, retryable=True)


class OpenAICompatibleProvider:
    name = "external"

    def __init__(self, *, endpoint: str, model: str, api_key: str | None,
                 prompt_version: str = "translate-ko-v1", timeout: float = 45,
                 max_chars: int = 8000, opener: Callable = urllib.request.urlopen):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("translation endpoint must be HTTP(S)")
        self.endpoint, self.model, self.api_key = endpoint, model, api_key
        self.prompt_version, self.timeout, self.max_chars = prompt_version, timeout, max_chars
        self._opener = opener

    def translate(self, request: TranslationRequest) -> TranslationResult:
        if not request.text.strip():
            raise ProviderError("invalid_response", "source text is empty", retryable=False)
        text = request.text[:self.max_chars]
        payload = json.dumps({"model": self.model, "messages": [{"role": "user", "content": _prompt(
            TranslationRequest(text, request.source_lang, request.target_locale, request.source_field)
        )}], "temperature": 0.2}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        try:
            with self._opener(urllib.request.Request(self.endpoint, data=payload, headers=headers),
                              timeout=self.timeout) as response:
                body = json.loads(response.read())
                output = body["choices"][0]["message"]["content"].strip()
        except ProviderError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_response", "invalid provider response", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        if not output:
            raise ProviderError("invalid_response", "empty provider response", retryable=True)
        usage = body.get("usage", {})
        return TranslationResult(output, self.name, self.model, self.prompt_version, len(text),
                                 len(output), round((time.monotonic() - started) * 1000),
                                 usage.get("prompt_tokens"), usage.get("completion_tokens"),
                                 body["choices"][0].get("finish_reason"))

    def summarize(self, request: SummaryRequest) -> SummaryResult:
        text = request.text[:self.max_chars]
        if not text.strip():
            raise ProviderError("invalid_response", "source text is empty", retryable=False)
        prompt = _summary_prompt(SummaryRequest(request.title, text, request.source_lang, request.target_locale))
        payload = json.dumps({"model": self.model, "messages": [{"role": "user", "content": prompt}],
                              "temperature": 0.1, "max_tokens": 1200,
                              "response_format": {"type": "json_object"}}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        try:
            with self._opener(urllib.request.Request(self.endpoint, data=payload, headers=headers),
                              timeout=self.timeout) as response:
                body = json.loads(response.read())
                output = body["choices"][0]["message"]["content"].strip()
            summary, points, institutions = _summary_data(output)
        except ProviderError: raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_response", "invalid provider response", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        usage = body.get("usage", {})
        return SummaryResult(summary, points, institutions, self.name, self.model, self.prompt_version,
                             len(request.title) + len(text), len(output),
                             round((time.monotonic() - started) * 1000),
                             usage.get("prompt_tokens"), usage.get("completion_tokens"),
                             body["choices"][0].get("finish_reason"))


class OllamaProvider:
    name = "internal"

    def __init__(self, *, endpoint: str, model: str, prompt_version: str = "translate-ko-v1",
                 timeout: float = 300, max_chars: int = 8000,
                 opener: Callable = urllib.request.urlopen):
        self.endpoint, self.model = endpoint, model
        self.prompt_version, self.timeout, self.max_chars = prompt_version, timeout, max_chars
        self._opener = opener

    def translate(self, request: TranslationRequest) -> TranslationResult:
        if not request.text.strip():
            raise ProviderError("invalid_response", "source text is empty", retryable=False)
        text = request.text[:self.max_chars]
        payload = json.dumps({"model": self.model, "prompt": _prompt(
            TranslationRequest(text, request.source_lang, request.target_locale, request.source_field)
        ), "stream": False, "think": False}).encode()
        started = time.monotonic()
        try:
            with self._opener(urllib.request.Request(self.endpoint, data=payload,
                              headers={"Content-Type": "application/json"}), timeout=self.timeout) as response:
                body = json.loads(response.read())
                output = body.get("response", "").strip()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_response", "invalid provider response", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        if not output:
            raise ProviderError("invalid_response", "empty provider response", retryable=True)
        return TranslationResult(output, self.name, self.model, self.prompt_version, len(text),
                                 len(output), round((time.monotonic() - started) * 1000),
                                 body.get("prompt_eval_count"), body.get("eval_count"), body.get("done_reason"))

    def summarize(self, request: SummaryRequest) -> SummaryResult:
        text = request.text[:self.max_chars]
        if not text.strip():
            raise ProviderError("invalid_response", "source text is empty", retryable=False)
        payload = json.dumps({"model": self.model, "prompt": _summary_prompt(
            SummaryRequest(request.title, text, request.source_lang, request.target_locale)),
            "stream": False, "think": False, "format": "json"}).encode()
        started = time.monotonic()
        try:
            with self._opener(urllib.request.Request(self.endpoint, data=payload,
                              headers={"Content-Type": "application/json"}), timeout=self.timeout) as response:
                body = json.loads(response.read())
                output = body.get("response", "").strip()
            summary, points, institutions = _summary_data(output)
        except ProviderError: raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_response", "invalid provider response", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        return SummaryResult(summary, points, institutions, self.name, self.model, self.prompt_version,
                             len(request.title) + len(text), len(output),
                             round((time.monotonic() - started) * 1000),
                             body.get("prompt_eval_count"), body.get("eval_count"), body.get("done_reason"))


def provider_from_env(name: str, *, model_version: str, prompt_version: str) -> TranslationProvider:
    if name == "external":
        endpoint = os.environ.get("TRANSLATION_EXTERNAL_ENDPOINT")
        model = os.environ.get("TRANSLATION_EXTERNAL_MODEL")
        if not endpoint or not model:
            raise ProviderError("configuration", "external translation provider is not configured", retryable=False)
        if model_version != model:
            raise ProviderError("configuration", "queued model does not match configured external model", retryable=False)
        return OpenAICompatibleProvider(endpoint=endpoint, model=model,
                                        api_key=os.environ.get("TRANSLATION_EXTERNAL_API_KEY"),
                                        prompt_version=prompt_version)
    if name == "internal":
        endpoint = os.environ.get("TRANSLATION_INTERNAL_ENDPOINT")
        model = os.environ.get("TRANSLATION_INTERNAL_MODEL")
        if not endpoint or not model:
            raise ProviderError("configuration", "internal translation provider is not configured", retryable=False)
        if model_version != model:
            raise ProviderError("configuration", "queued model does not match configured internal model", retryable=False)
        return OllamaProvider(endpoint=endpoint, model=model, prompt_version=prompt_version)
    raise ProviderError("configuration", f"unknown translation provider: {name}", retryable=False)
