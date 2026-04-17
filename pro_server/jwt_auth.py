"""JWT authentication helpers for the user account system."""

import hashlib
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException


def _get_settings():
    from .settings import pro_settings
    return pro_settings


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Hash a password with SHA-256 + salt. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Verify a password against a stored hash + salt."""
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h == password_hash


def create_token(user_id: str, email: str) -> tuple[str, int]:
    """Create a JWT token. Returns (token_str, expires_in_seconds)."""
    settings = _get_settings()
    expires_in = settings.jwt_expiry_hours * 3600
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires_in


def verify_token(token: str) -> dict:
    """Decode and verify a JWT token. Returns the payload dict."""
    settings = _get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(authorization: str = Header(...)) -> dict:
    """FastAPI dependency — extracts and verifies JWT from Authorization header."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must start with 'Bearer '")
    token = authorization[7:]
    payload = verify_token(token)
    return {"user_id": payload["sub"], "email": payload["email"]}
