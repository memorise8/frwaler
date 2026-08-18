"""Small deployment auth boundary for the single-customer delivery."""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

# The value delivery/.env.example used to ship for DELIVERY_API_TOKEN. It is
# 48 characters, so the length check alone accepted it, and a customer who
# copied the example and filled in only POSTGRES_PASSWORD would have run the
# whole console behind a secret published in this repository. The example now
# ships the field empty, but anyone who already copied the old one is still
# holding this string -- so refuse it by name rather than by length.
PLACEHOLDER_TOKENS = frozenset({"replace-with-a-random-secret-at-least-32-characters"})


def validate_auth_config() -> None:
    mode = os.environ.get("DELIVERY_AUTH_MODE", "token")
    if mode == "disabled":
        return
    if mode != "token":
        raise RuntimeError("invalid DELIVERY_AUTH_MODE")
    token = os.environ.get("DELIVERY_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("DELIVERY_API_TOKEN must contain at least 32 characters")
    if token in PLACEHOLDER_TOKENS:
        raise RuntimeError(
            "DELIVERY_API_TOKEN is still the example placeholder, which is public. "
            "Generate one with: openssl rand -hex 32"
        )


def require_operator(x_delivery_token: str | None = Header(default=None)) -> str:
    mode = os.environ.get("DELIVERY_AUTH_MODE", "token")
    if mode == "disabled":
        return "local-operator"
    expected=os.environ.get("DELIVERY_API_TOKEN")
    if mode != "token" or not expected or len(expected) < 32:
        raise HTTPException(status_code=503,detail="operator authentication unavailable")
    if not x_delivery_token or not hmac.compare_digest(x_delivery_token,expected):
        raise HTTPException(status_code=401,detail="operator authentication required")
    return "customer-operator"
