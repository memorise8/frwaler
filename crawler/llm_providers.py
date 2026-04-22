# -*- coding: utf-8 -*-
"""LLM provider abstraction for page analysis.

Supports:
- OpenAI (gpt-5.4-mini)
- Google Gemini (gemini-2.5-flash)

Select via `provider` arg or LLM_PROVIDER env var ('gpt' | 'gemini').
Defaults to 'gpt'.
"""

import json
import os
from typing import Optional

from bs4 import BeautifulSoup

OPENAI_MODEL = "gpt-5.4-mini"
GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "You analyze web pages to find downloadable documents. "
    "Respond in JSON only."
)


def _build_user_prompt(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, noscript, svg, path"):
        tag.decompose()
    clean_text = soup.get_text(strip=True)[:3000]
    body = soup.select_one("body")
    structure = str(body)[:2000] if body else str(soup)[:2000]
    return (
        f"Analyze this page and find document download links.\n"
        f"URL: {url}\n\n"
        f"Page text (truncated):\n{clean_text[:1500]}\n\n"
        f"HTML structure (truncated):\n{structure[:1500]}\n\n"
        f"Respond with JSON:\n"
        f'{{\n'
        f'    "has_documents": true/false,\n'
        f'    "document_links_css": "CSS selector for document links",\n'
        f'    "file_links_css": "CSS selector for direct file download links",\n'
        f'    "pagination_css": "CSS selector for next page link if any",\n'
        f'    "notes": "brief description"\n'
        f'}}'
    )


def _parse_json_response(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _analyze_openai(html: str, url: str) -> Optional[dict]:
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
                {"role": "user", "content": _build_user_prompt(html, url)},
            ],
            temperature=0,
            max_completion_tokens=300,
        )
        return _parse_json_response(response.choices[0].message.content)
    except Exception as e:
        print(f"[LLM:openai] error: {e}")
        return None


def _analyze_gemini(html: str, url: str) -> Optional[dict]:
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
                parts=[types.Part.from_text(text=_build_user_prompt(html, url))],
            )
        ]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0,
            max_output_tokens=400,
        )
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        return _parse_json_response(response.text)
    except Exception as e:
        print(f"[LLM:gemini] error: {e}")
        return None


def analyze_page_for_documents(
    html: str,
    url: str,
    provider: Optional[str] = None,
) -> Optional[dict]:
    """Analyze a web page using an LLM to identify document download links.

    Args:
        html: Raw HTML of the page.
        url: The page URL (for context).
        provider: 'gpt' or 'gemini'. If None, uses LLM_PROVIDER env var
                  (default 'gpt').

    Returns:
        Parsed JSON dict with keys (has_documents, document_links_css,
        file_links_css, pagination_css, notes), or None if unavailable /
        error / invalid response.
    """
    selected = (provider or os.environ.get("LLM_PROVIDER") or "gpt").lower()
    if selected in ("gpt", "openai"):
        return _analyze_openai(html, url)
    if selected in ("gemini", "google"):
        return _analyze_gemini(html, url)
    print(f"[LLM] unknown provider: {selected!r}, falling back to gpt")
    return _analyze_openai(html, url)
