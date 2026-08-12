"""Small deployment auth boundary for the single-customer delivery."""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def require_operator(x_delivery_token: str | None = Header(default=None)) -> str:
    expected=os.environ.get("DELIVERY_API_TOKEN")
    if not expected:
        return "local-operator"
    if not x_delivery_token or not hmac.compare_digest(x_delivery_token,expected):
        raise HTTPException(status_code=401,detail="operator authentication required")
    return "customer-operator"
