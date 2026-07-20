# -*- coding: utf-8 -*-
"""LLM-based summarization for libertree documents.

Mirrors the provider-abstraction shape of ``crawler/llm_providers.py`` but
emits a plain Korean text summary instead of a JSON descriptor.

Public entry:
    summarize_text(text, *, title=None, provider=None) -> Optional[str]

Provider selection: ``provider`` arg > ``LLM_PROVIDER`` env var > ``"gpt"``.
Returns ``None`` when the provider is unavailable (no key) or the call
fails — callers should treat ``None`` as "leave the existing summary
column alone" (cf. ``upsert_document``'s COALESCE-on-summary semantics).
"""

from __future__ import annotations

import os
from typing import Optional


# Model selection — override with SUMMARIZER_MODEL env var. Default is
# gpt-5.4-mini: verified available via OpenAI API key, non-reasoning model
# (no token-budget surprises), produces clean Korean summaries.
# gpt-5.5-mini may become available later — set SUMMARIZER_MODEL=gpt-5.5-mini
# to opt in. We exclude gpt-5-mini from the fallback chain because, as a
# reasoning model, it tends to echo source text rather than condense it
# unless given a much larger token budget than 5.4-mini needs.
SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "gpt-5.4-mini")
SUMMARIZER_FALLBACK_CHAIN = ["gpt-5.4-mini", "gpt-4o-mini"]
GEMINI_MODEL = "gemini-2.5-flash"


def _is_gpt5_family(model: str) -> bool:
    """gpt-5.x rejects `temperature` (only default 1 allowed) and prefers
    `max_completion_tokens` over `max_tokens`. Detect the family by name."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or "gpt-5." in m


def _summarize_with_model(client, model: str, text: str, title: Optional[str]) -> Optional[str]:
    # gpt-5.x mini variants are reasoning models — internal reasoning_tokens
    # consume the completion budget before any visible text is emitted, so
    # 400 was too tight (e.g. 192 reasoning + 0 output → empty content).
    # Use 800 for the gpt-5.x family, keep 400 for older models.
    is_gpt5 = _is_gpt5_family(model)
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(text, title)},
        ],
        "max_completion_tokens": 800 if is_gpt5 else 400,
    }
    # gpt-5.x family rejects temperature != 1; only set for older models.
    if not is_gpt5:
        kwargs["temperature"] = 0.2
    response = client.chat.completions.create(**kwargs)
    return _clean(response.choices[0].message.content)

# Hard cap on input characters fed to the model. Korean admin documents
# routinely exceed model context windows; truncating here keeps the cost
# bounded and avoids a hard error from the API.
MAX_INPUT_CHARS = 12_000

SYSTEM_PROMPT = (
    "당신은 한국 행정·법률 문서를 한국어로 간결하게 요약하는 전문 어시스턴트입니다. "
    "출력은 한국어 평문 3~5문장이며, 마크다운/제목/접두사(\"요약:\" 등) 없이 "
    "본문만 작성합니다. 반드시 사실에 근거하고, 추정·창작은 금지합니다."
)


def _build_user_prompt(text: str, title: Optional[str]) -> str:
    text = (text or "").strip()
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS] + "\n…(이하 생략)"
    if title:
        return f"제목: {title}\n\n본문:\n{text}\n\n위 본문을 한국어로 3~5문장으로 요약하세요."
    return f"다음 본문을 한국어로 3~5문장으로 요약하세요:\n\n{text}"


def _clean(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = s.strip()
    return s or None


def _summarize_openai(text: str, title: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    # Build fallback chain starting with the configured model, then the
    # remaining chain entries in order (without duplicating the primary).
    primary = SUMMARIZER_MODEL
    chain = [primary] + [m for m in SUMMARIZER_FALLBACK_CHAIN if m != primary]

    last_err: Optional[Exception] = None
    for idx, model in enumerate(chain):
        try:
            result = _summarize_with_model(client, model, text, title)
        except Exception as e:
            msg = str(e).lower()
            # Only fall through on model-not-found / unsupported errors;
            # bail out early on auth / rate-limit / network so we don't
            # blow through quota.
            if "model" in msg and ("not found" in msg or "does not exist" in msg or "unsupported" in msg):
                if idx == len(chain) - 1:
                    print(f"[summarizer:openai] all models in fallback chain failed: {e}")
                    return None
                print(f"[summarizer:openai] {model} unavailable; falling back to {chain[idx+1]}")
                last_err = e
                continue
            print(f"[summarizer:openai] error ({model}): {e}")
            return None

        # Empty string / None — typical for reasoning models (gpt-5.x mini)
        # when the completion budget is exhausted by reasoning_tokens. Fall
        # through to the next model rather than returning None silently.
        if result:
            return result
        if idx == len(chain) - 1:
            print(f"[summarizer:openai] all models returned empty content")
            return None
        print(f"[summarizer:openai] {model} returned empty content; falling back to {chain[idx+1]}")
    return None


def _summarize_gemini(text: str, title: Optional[str]) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=_build_user_prompt(text, title))],
            )
        ]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=400,
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        return _clean(response.text)
    except Exception as e:
        print(f"[summarizer:gemini] error: {e}")
        return None


def summarize_text(
    text: str,
    *,
    title: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[str]:
    """Produce a Korean 3-5 sentence summary of ``text``.

    Returns ``None`` if the provider is misconfigured (missing API key)
    or the call fails. Callers should treat ``None`` as "skip this row"
    rather than overwriting any existing summary.
    """
    if not text or not text.strip():
        return None
    selected = (provider or os.environ.get("LLM_PROVIDER") or "gpt").lower()
    if selected in ("gpt", "openai"):
        return _summarize_openai(text, title)
    if selected in ("gemini", "google"):
        return _summarize_gemini(text, title)
    print(f"[summarizer] unknown provider: {selected!r}, falling back to gpt")
    return _summarize_openai(text, title)
