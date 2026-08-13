"""Small deployment auth boundary for the single-customer delivery."""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def validate_auth_config() -> None:
    mode = os.environ.get("DELIVERY_AUTH_MODE", "token")
    if mode == "disabled":
        return
    if mode != "token":
        raise RuntimeError("invalid DELIVERY_AUTH_MODE")
    token = os.environ.get("DELIVERY_API_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("DELIVERY_API_TOKEN must contain at least 32 characters")


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
