"""Unit tests for pro_server.services.datasheet_parser.

Mocks both OpenAI and Gemini clients so no real API key is required.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Canned response simulating an LLM returning a valid BJT JSON
_CANNED = {
    "vceo": "40 V",
    "vcbo": "75 V",
    "vebo": "6 V",
    "ic_max": "600 mA",
    "hfe_min": "75",
    "hfe_max": "300",
    "icbo": "10 nA",
    "ft": "300 MHz",
    "pd": "500 mW",
    "tj_max": "150 C",
    "polarity": "NPN",
    "package": "TO-18",
    "extracted_bjt_type_confirmed": True,
    "extraction_notes": "All parameters extracted successfully.",
}
_CANNED_JSON = json.dumps(_CANNED)


def _make_mock_openai_client():
    """Build a mock openai.OpenAI client that returns _CANNED_JSON."""
    message = SimpleNamespace(content=_CANNED_JSON)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(total_tokens=123)
    completion = SimpleNamespace(choices=[choice], usage=usage)

    mock_create = MagicMock(return_value=completion)
    mock_completions = SimpleNamespace(create=mock_create)
    mock_chat = SimpleNamespace(completions=mock_completions)
    return SimpleNamespace(chat=mock_chat)


def _make_mock_gemini_client():
    """Build a mock Gemini client that returns _CANNED_JSON."""
    usage = SimpleNamespace(prompt_token_count=80, candidates_token_count=50)
    response = SimpleNamespace(text=_CANNED_JSON, usage_metadata=usage)

    mock_gen = MagicMock(return_value=response)
    mock_models = SimpleNamespace(generate_content=mock_gen)
    return SimpleNamespace(models=mock_models)


def test_extract_gemini_primary_path(monkeypatch):
    """Gemini returns JSON → used as primary, OpenAI not called."""
    monkeypatch.setenv("GEMINI_KEY", "fake-gemini-key")

    gclient = _make_mock_gemini_client()

    with patch("pro_server.services.datasheet_parser._make_gemini_client",
               return_value=(gclient, "gemini-2.5-flash")):
        # OpenAI mock present but should not be reached
        with patch("pro_server.services.datasheet_parser._make_openai_client") as oclient_fn:
            from pro_server.services.datasheet_parser import extract_from_text
            params, confidence, tokens_used = extract_from_text(
                "2N2222A NPN ...", mpn_hint="2N2222A"
            )

    assert tokens_used == 130  # 80 + 50
    assert 0 < confidence <= 1.0
    assert abs(params["vceo_v"] - 40.0) < 1e-9
    assert abs(params["icbo_a_at_vcb"] - 10e-9) < 1e-18
    oclient_fn.assert_not_called()


def test_extract_openai_fallback_when_gemini_unavailable(monkeypatch):
    """No Gemini client → OpenAI is used."""
    monkeypatch.setenv("PRO_OPENAI_API_KEY", "fake-openai-key")

    oclient = _make_mock_openai_client()

    with patch("pro_server.services.datasheet_parser._make_gemini_client",
               return_value=(None, None)):
        with patch("pro_server.services.datasheet_parser._make_openai_client",
                   return_value=oclient):
            from pro_server.services.datasheet_parser import extract_from_text
            params, confidence, tokens_used = extract_from_text(
                "2N2222A NPN ...", mpn_hint="2N2222A"
            )

    assert tokens_used == 123
    assert abs(params["vceo_v"] - 40.0) < 1e-9


def test_extract_no_provider_raises(monkeypatch):
    """No Gemini, no OpenAI → RuntimeError."""
    monkeypatch.delenv("GEMINI_KEY", raising=False)
    monkeypatch.setenv("PRO_OPENAI_API_KEY", "")

    with patch("pro_server.services.datasheet_parser._make_gemini_client",
               return_value=(None, None)):
        with patch("pro_server.services.datasheet_parser._make_openai_client",
                   return_value=None):
            from pro_server.services.datasheet_parser import extract_from_text
            with pytest.raises(RuntimeError, match="No LLM provider"):
                extract_from_text("some text")


def test_gemini_failure_falls_back_to_openai(monkeypatch):
    """Gemini raises RuntimeError → OpenAI fallback kicks in."""
    monkeypatch.setenv("GEMINI_KEY", "fake-gemini-key")
    monkeypatch.setenv("PRO_OPENAI_API_KEY", "fake-openai-key")

    # Gemini client exists but raises on call
    gclient = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=MagicMock(side_effect=Exception("quota exceeded"))
        )
    )
    oclient = _make_mock_openai_client()

    with patch("pro_server.services.datasheet_parser._make_gemini_client",
               return_value=(gclient, "gemini-2.5-flash")):
        with patch("pro_server.services.datasheet_parser._make_openai_client",
                   return_value=oclient):
            from pro_server.services.datasheet_parser import extract_from_text
            params, _, tokens_used = extract_from_text("text", mpn_hint="X")

    assert tokens_used == 123  # OpenAI mock token count
    assert params["vceo_v"] == 40.0
