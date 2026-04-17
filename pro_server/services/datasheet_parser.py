# -*- coding: utf-8 -*-
"""LLM-based structured parameter extraction from BJT datasheet PDFs or raw text.

Primary: Google Gemini 2.5 Flash (free tier, ~500 req/day)
Fallback: OpenAI gpt-4o-mini (paid, used if Gemini key missing or call fails)
"""

import json
import logging
import os
import tempfile
from typing import Optional

_LOGGER = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"
_OPENAI_MODEL = "gpt-4o-mini"

_SYSTEM_PROMPT = (
    "You are a semiconductor datasheet parser. Extract BJT (Bipolar Junction Transistor) "
    "parameters and return STRICT JSON only. Do not invent values. Use null for missing."
)

_USER_TEMPLATE = """\
Extract from this datasheet text. Return a JSON object with exactly these keys:
vceo, vcbo, vebo, ic_max, hfe_min, hfe_max, icbo, ft, pd, tj_max,
polarity, package, extracted_bjt_type_confirmed (bool), extraction_notes (string).

Rules:
- For numeric values include the unit in the string (e.g. "40 V", "100 nA", "250 MHz").
- For polarity use "NPN" or "PNP".
- For package use the standard name (e.g. "TO-92", "SOT-23").
- Return null (not empty string) for any value you cannot find.
- Do NOT add extra keys.

MPN hint: {mpn_hint}

Text:
{text}
"""

_STRICT_USER_TEMPLATE = """\
You MUST return valid JSON only — no prose, no markdown, no code fences.
Keys: vceo, vcbo, vebo, ic_max, hfe_min, hfe_max, icbo, ft, pd, tj_max,
polarity, package, extracted_bjt_type_confirmed (bool), extraction_notes (string).
Unknown values → null.

MPN hint: {mpn_hint}

Text:
{text}
"""

_BJT_FIELD_COUNT = 12  # number of BjtParameters fields used for confidence

_MOSFET_SYSTEM_PROMPT = (
    "You are a semiconductor datasheet parser. Extract MOSFET (Metal-Oxide-Semiconductor "
    "Field-Effect Transistor) parameters and return STRICT JSON only. Do not invent values. "
    "Use null for missing."
)

_MOSFET_USER_TEMPLATE = """\
Extract from this datasheet text. Return a JSON object with exactly these keys:
bvdss, vgs_th, rds_on, id_max, idss, qg, pd, tj_max,
gate_oxide, polarity, package, extracted_mosfet_type_confirmed (bool), extraction_notes (string).

Rules:
- For numeric values include the unit in the string (e.g. "100 V", "1 mΩ", "50 nC").
- For polarity use "N-channel" or "P-channel".
- For gate_oxide use "thick", "standard", or "thin" if determinable, else null.
- For package use the standard name (e.g. "TO-254AA", "TO-257AA", "SMD-1").
- Return null (not empty string) for any value you cannot find.
- Do NOT add extra keys.

MPN hint: {mpn_hint}

Text:
{text}
"""

_MOSFET_STRICT_USER_TEMPLATE = """\
You MUST return valid JSON only — no prose, no markdown, no code fences.
Keys: bvdss, vgs_th, rds_on, id_max, idss, qg, pd, tj_max,
gate_oxide, polarity, package, extracted_mosfet_type_confirmed (bool), extraction_notes (string).
Unknown values → null.

MPN hint: {mpn_hint}

Text:
{text}
"""

_MOSFET_FIELD_COUNT = 11  # number of MosfetParameters fields used for confidence


# ---------------------------------------------------------------------------
# Gemini (primary)
# ---------------------------------------------------------------------------

def _make_gemini_client():
    """Return (client, model_name) or (None, None) if unavailable."""
    try:
        from google import genai
    except ImportError:
        return None, None

    from pro_server.settings import pro_settings

    key = pro_settings.gemini_api_key or os.environ.get("GEMINI_KEY", "")
    if not key:
        return None, None

    try:
        client = genai.Client(api_key=key)
        return client, _GEMINI_MODEL
    except Exception:
        _LOGGER.exception("Failed to init Gemini client")
        return None, None


def _call_gemini(client, model_name: str, user_content: str,
                 max_tokens: int = 1024) -> tuple[dict, int]:
    """Call Gemini with JSON mode. Returns (parsed_dict, tokens_used)."""
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=f"{_SYSTEM_PROMPT}\n\n{user_content}",
            config={
                "response_mime_type": "application/json",
                "max_output_tokens": max_tokens,
            },
        )
    except Exception as exc:
        _LOGGER.exception("Gemini call failed")
        raise RuntimeError("Gemini extraction failed — see server logs") from exc

    usage = response.usage_metadata
    tokens = (usage.prompt_token_count or 0) + (usage.candidates_token_count or 0)
    raw_text = (response.text or "").strip()
    parsed = json.loads(raw_text)  # may raise — caller handles retry
    return parsed, tokens


# ---------------------------------------------------------------------------
# OpenAI (fallback)
# ---------------------------------------------------------------------------

def _make_openai_client():
    """Return OpenAI client or None if unavailable/misconfigured."""
    try:
        from openai import OpenAI
    except ImportError:
        return None

    from pro_server.settings import pro_settings

    key = pro_settings.openai_api_key
    if not key:
        return None

    try:
        return OpenAI(api_key=key)
    except Exception:
        _LOGGER.exception("Failed to init OpenAI client")
        return None


def _call_openai(client, user_content: str, max_tokens: int = 1024) -> tuple[dict, int]:
    """Call gpt-4o-mini with JSON mode. Returns (parsed_dict, tokens_used)."""
    try:
        response = client.chat.completions.create(
            model=_OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
    except Exception as exc:
        _LOGGER.exception("OpenAI call failed")
        raise RuntimeError("LLM extraction failed — see server logs") from exc

    tokens = response.usage.total_tokens
    raw_text = response.choices[0].message.content or ""
    parsed = json.loads(raw_text)  # may raise — caller handles retry
    return parsed, tokens


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _try_provider(call_fn, user_msg: str, strict_msg: str) -> tuple[Optional[dict], int]:
    """Try one provider with retry on JSON parse failure.
    Returns (parsed_dict | None, tokens_used). None means both attempts failed."""
    try:
        return call_fn(user_msg)
    except json.JSONDecodeError:
        pass
    except RuntimeError:
        # provider-level failure (API error, rate limit, quota); caller decides fallback
        raise

    try:
        return call_fn(strict_msg)
    except json.JSONDecodeError:
        return None, 0


def extract_from_text(
    text: str, mpn_hint: Optional[str] = None
) -> tuple[dict, float, int]:
    """Extract BJT parameters from raw datasheet text.

    Tries Gemini first (free tier). Falls back to OpenAI on any failure.

    Returns:
        (bjt_params_dict, extraction_confidence_0_to_1, tokens_used)

    Raises:
        RuntimeError: if neither provider is configured or both fail.
        ValueError:   if parsed JSON was invalid on both attempts with each provider.
    """
    from pro_server.services.normalizer import normalize_bjt_params

    truncated = text[:12000]
    hint = mpn_hint or "unknown"
    user_msg = _USER_TEMPLATE.format(mpn_hint=hint, text=truncated)
    strict_msg = _STRICT_USER_TEMPLATE.format(mpn_hint=hint, text=truncated)

    raw: Optional[dict] = None
    total_tokens = 0
    provider_used = None

    # --- Try Gemini first ---
    gclient, gmodel = _make_gemini_client()
    if gclient is not None:
        try:
            raw, total_tokens = _try_provider(
                lambda msg: _call_gemini(gclient, gmodel, msg),
                user_msg, strict_msg,
            )
            if raw is not None:
                provider_used = "gemini"
        except RuntimeError:
            _LOGGER.warning("Gemini failed; falling back to OpenAI")
            raw = None

    # --- Fallback to OpenAI ---
    if raw is None:
        oclient = _make_openai_client()
        if oclient is not None:
            try:
                raw, total_tokens = _try_provider(
                    lambda msg: _call_openai(oclient, msg),
                    user_msg, strict_msg,
                )
                if raw is not None:
                    provider_used = "openai"
            except RuntimeError:
                raise

    if raw is None:
        if provider_used is None:
            raise RuntimeError(
                "No LLM provider configured. Set GEMINI_KEY or PRO_OPENAI_API_KEY "
                "in your environment."
            )
        raise ValueError(
            "All LLM extraction attempts produced invalid JSON. "
            "Check that the input is a valid BJT datasheet."
        )

    _LOGGER.info("Datasheet extracted via %s (tokens=%d)", provider_used, total_tokens)

    # Normalize to BjtParameters field names + SI units
    normalized = normalize_bjt_params(raw)

    # Compute confidence: fraction of non-null fields
    non_null = sum(1 for v in normalized.values() if v is not None)
    confidence = non_null / _BJT_FIELD_COUNT

    # Penalise if model couldn't confirm BJT type or expressed uncertainty
    bjt_confirmed = raw.get("extracted_bjt_type_confirmed", True)
    notes = str(raw.get("extraction_notes") or "").lower()
    if mpn_hint and not bjt_confirmed:
        confidence = max(0.0, confidence - 0.1)
    if "uncertain" in notes or "could not find" in notes:
        confidence = max(0.0, confidence - 0.1)

    confidence = min(1.0, confidence)
    return normalized, confidence, total_tokens


def extract_from_pdf_bytes(
    pdf_bytes: bytes, mpn_hint: Optional[str] = None
) -> tuple[dict, float, int]:
    """Extract BJT parameters from raw PDF bytes."""
    from crawler.converter import extract_text_pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        text = extract_text_pdf(tmp_path) or ""
    finally:
        os.unlink(tmp_path)

    return extract_from_text(text, mpn_hint=mpn_hint)


def extract_from_pdf_path(
    path: str, mpn_hint: Optional[str] = None
) -> tuple[dict, float, int]:
    """Extract BJT parameters from a PDF file on disk."""
    from crawler.converter import extract_text_pdf

    text = extract_text_pdf(path) or ""
    return extract_from_text(text, mpn_hint=mpn_hint)


# ---------------------------------------------------------------------------
# MOSFET extraction
# ---------------------------------------------------------------------------

def extract_mosfet_from_text(
    text: str, mpn_hint: Optional[str] = None
) -> tuple[dict, float, int]:
    """Extract MOSFET parameters from raw datasheet text.

    Tries Gemini first (free tier). Falls back to OpenAI on any failure.

    Returns:
        (mosfet_params_dict, extraction_confidence_0_to_1, tokens_used)
    """
    from pro_server.services.normalizer import normalize_mosfet_params

    truncated = text[:12000]
    hint = mpn_hint or "unknown"
    user_msg = _MOSFET_USER_TEMPLATE.format(mpn_hint=hint, text=truncated)
    strict_msg = _MOSFET_STRICT_USER_TEMPLATE.format(mpn_hint=hint, text=truncated)

    raw: Optional[dict] = None
    total_tokens = 0
    provider_used = None

    # --- Try Gemini first ---
    gclient, gmodel = _make_gemini_client()
    if gclient is not None:
        # Temporarily swap system prompt for MOSFET
        original_system = _SYSTEM_PROMPT
        try:
            import pro_server.services.datasheet_parser as _self
            _self._SYSTEM_PROMPT_BACKUP = _SYSTEM_PROMPT

            def _gemini_call_mosfet(msg):
                resp = gclient.models.generate_content(
                    model=gmodel,
                    contents=f"{_MOSFET_SYSTEM_PROMPT}\n\n{msg}",
                    config={"response_mime_type": "application/json", "max_output_tokens": 1024},
                )
                usage = resp.usage_metadata
                tokens = (usage.prompt_token_count or 0) + (usage.candidates_token_count or 0)
                import json as _json
                return _json.loads((resp.text or "").strip()), tokens

            raw, total_tokens = _try_provider(
                _gemini_call_mosfet,
                user_msg, strict_msg,
            )
            if raw is not None:
                provider_used = "gemini"
        except RuntimeError:
            _LOGGER.warning("Gemini failed for MOSFET; falling back to OpenAI")
            raw = None

    # --- Fallback to OpenAI ---
    if raw is None:
        oclient = _make_openai_client()
        if oclient is not None:
            def _openai_call_mosfet(msg):
                import json as _json
                response = oclient.chat.completions.create(
                    model=_OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": _MOSFET_SYSTEM_PROMPT},
                        {"role": "user", "content": msg},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=1024,
                )
                tokens = response.usage.total_tokens
                return _json.loads(response.choices[0].message.content or ""), tokens

            try:
                raw, total_tokens = _try_provider(
                    _openai_call_mosfet,
                    user_msg, strict_msg,
                )
                if raw is not None:
                    provider_used = "openai"
            except RuntimeError:
                raise

    if raw is None:
        if provider_used is None:
            raise RuntimeError(
                "No LLM provider configured. Set GEMINI_KEY or PRO_OPENAI_API_KEY "
                "in your environment."
            )
        raise ValueError(
            "All LLM extraction attempts produced invalid JSON. "
            "Check that the input is a valid MOSFET datasheet."
        )

    _LOGGER.info("MOSFET datasheet extracted via %s (tokens=%d)", provider_used, total_tokens)

    normalized = normalize_mosfet_params(raw)

    non_null = sum(1 for v in normalized.values() if v is not None)
    confidence = non_null / _MOSFET_FIELD_COUNT

    mosfet_confirmed = raw.get("extracted_mosfet_type_confirmed", True)
    notes = str(raw.get("extraction_notes") or "").lower()
    if mpn_hint and not mosfet_confirmed:
        confidence = max(0.0, confidence - 0.1)
    if "uncertain" in notes or "could not find" in notes:
        confidence = max(0.0, confidence - 0.1)

    confidence = min(1.0, confidence)
    return normalized, confidence, total_tokens


def extract_mosfet_from_pdf_bytes(
    pdf_bytes: bytes, mpn_hint: Optional[str] = None
) -> tuple[dict, float, int]:
    """Extract MOSFET parameters from raw PDF bytes."""
    from crawler.converter import extract_text_pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        text = extract_text_pdf(tmp_path) or ""
    finally:
        os.unlink(tmp_path)

    return extract_mosfet_from_text(text, mpn_hint=mpn_hint)


if __name__ == "__main__":
    # Verify imports
    from pro_server.schemas import BjtParameters  # noqa: F401
    from pro_server.services.normalizer import normalize_bjt_params  # noqa: F401

    print("Imports OK: BjtParameters, normalize_bjt_params")

    gemini_key = os.environ.get("GEMINI_KEY", "")
    openai_key = os.environ.get("PRO_OPENAI_API_KEY", "")
    if not gemini_key and not openai_key:
        print("skip: no LLM API key (set GEMINI_KEY or PRO_OPENAI_API_KEY)")
    else:
        fake_text = (
            "2N2222A NPN Silicon Transistor. "
            "VCEO=40V VCBO=75V VEBO=6V IC(max)=600mA "
            "hFE min=75 max=300 ICBO=10nA FT=300MHz PD=500mW Tj=150C Package=TO-18"
        )
        params, conf, tokens = extract_from_text(fake_text, mpn_hint="2N2222A")
        print(f"tokens_used={tokens}, confidence={conf:.2f}")
        print(f"params={params}")
        assert tokens > 0
        assert any(v is not None for v in params.values())
        print("Sanity call PASSED")
