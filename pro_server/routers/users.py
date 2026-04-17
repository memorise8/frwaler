"""User account endpoints: signup, login, profile, reports."""

import uuid
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from ..settings import pro_settings
from ..jwt_auth import hash_password, verify_password, create_token, get_current_user
from ..schemas import (
    SignupRequest,
    LoginRequest,
    AuthResponse,
    UserProfile,
    ReportListItem,
    ReportListResponse,
)

router = APIRouter(prefix="/pro/api", tags=["users"])


def _license_db():
    return sqlite3.connect(pro_settings.license_db_path)


def _screening_db():
    return sqlite3.connect(pro_settings.screening_db_path)


@router.post("/signup", response_model=AuthResponse)
async def signup(req: SignupRequest):
    """Create a new user account with an auto-generated license key."""
    user_id = str(uuid.uuid4())
    license_key = f"usr-{uuid.uuid4()}"
    password_hash, salt = hash_password(req.password)

    conn = _license_db()
    try:
        # Check if email already exists
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (req.email,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")

        # Create license key first (foreign key target)
        conn.execute(
            "INSERT INTO licenses (key, owner, plan, active, email) VALUES (?, ?, 'basic', 1, ?)",
            (license_key, req.name, req.email),
        )

        # Create user
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, password_salt, license_key) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, req.email, req.name, password_hash, salt, license_key),
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Registration failed: {exc}")
    finally:
        conn.close()

    token, expires_in = create_token(user_id, req.email)
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=req.email,
        name=req.name,
        license_key=license_key,
        expires_in=expires_in,
    )


@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    """Authenticate with email + password, receive a JWT token."""
    conn = _license_db()
    try:
        row = conn.execute(
            "SELECT id, email, name, password_hash, password_salt, license_key FROM users WHERE email = ?",
            (req.email.strip().lower(),),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user_id, email, name, pw_hash, pw_salt, license_key = row

    if not verify_password(req.password, pw_hash, pw_salt):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token, expires_in = create_token(user_id, email)
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        license_key=license_key or "",
        expires_in=expires_in,
    )


@router.get("/me", response_model=UserProfile)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return the current user's profile."""
    conn = _license_db()
    try:
        row = conn.execute(
            "SELECT id, email, name, license_key, created_at FROM users WHERE id = ?",
            (current_user["user_id"],),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfile(
        user_id=row[0],
        email=row[1],
        name=row[2],
        license_key=row[3],
        created_at=row[4],
    )


@router.get("/me/reports", response_model=ReportListResponse)
async def get_my_reports(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """Return paginated screening reports for the current user's license key."""
    # Look up the user's license key
    lconn = _license_db()
    try:
        row = lconn.execute(
            "SELECT license_key FROM users WHERE id = ?",
            (current_user["user_id"],),
        ).fetchone()
    finally:
        lconn.close()

    if not row or not row[0]:
        return ReportListResponse(reports=[], page=page, per_page=per_page, total=0)

    license_key = row[0]

    conn = _screening_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM screening_results WHERE license_key = ?",
            (license_key,),
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(
            "SELECT id, input_mpn, input_source, overall_score, status, confidence, created_at "
            "FROM screening_results WHERE license_key = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (license_key, per_page, offset),
        ).fetchall()
    finally:
        conn.close()

    reports = [
        ReportListItem(
            id=r[0],
            input_mpn=r[1],
            input_source=r[2],
            overall_score=r[3],
            status=r[4],
            confidence=r[5],
            created_at=r[6],
        )
        for r in rows
    ]

    return ReportListResponse(
        reports=reports,
        page=page,
        per_page=per_page,
        total=total,
    )
