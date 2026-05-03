# -*- coding: utf-8 -*-
"""LLM-based summarization for livertree documents.

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


OPENAI_MODEL = "gpt-5.4-mini"
GEMINI_MODEL = "gemini-2.5-flash"

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
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(text, title)},
            ],
            temperature=0.2,
            max_completion_tokens=400,
        )
        return _clean(response.choices[0].message.content)
    except Exception as e:  # network, auth, rate-limit, etc.
        print(f"[summarizer:openai] error: {e}")
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
