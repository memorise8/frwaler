import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from ..auth import verify_license
import sqlite3
from ..settings import pro_settings
from ..schemas import RegisterRequest, RegisterResponse, LicenseInfo, AdminActionResponse

router = APIRouter(prefix="/pro/api", tags=["license"])

DAILY_REGISTRATION_LIMIT = 5


def _verify_admin(x_admin_password: str = Header(...)):
    if x_admin_password != pro_settings.admin_password:
        raise HTTPException(status_code=403, detail="Invalid admin password")
    return True


@router.post("/register", response_model=RegisterResponse)
async def register_license(req: RegisterRequest, request: Request):
    """Self-service license key registration. No auth required.
    Rate-limited to 5 registrations per IP per day.
    Keys start as inactive (active=0) pending admin approval."""
    client_ip = request.client.host if request.client else "unknown"
    today = date.today().isoformat()

    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE ip = ? AND created_at >= ?",
            (client_ip, today),
        ).fetchone()[0]

        if count >= DAILY_REGISTRATION_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Registration limit reached: max {DAILY_REGISTRATION_LIMIT} keys per IP per day",
            )

        key = f"usr-{uuid.uuid4()}"
        conn.execute(
            "INSERT INTO licenses (key, owner, plan, active, email, ip) VALUES (?, ?, 'basic', 0, ?, ?)",
            (key, req.name, req.email, client_ip),
        )
        conn.commit()
    finally:
        conn.close()

    return RegisterResponse(
        key=key,
        plan="basic",
        message="신청이 완료되었습니다. 관리자 승인 후 사용 가능합니다.",
    )


@router.post("/verify-key")
async def verify_key(license_info: dict = Depends(verify_license)):
    return {"valid": True, "plan": license_info["plan"]}


@router.get("/usage")
async def get_usage(license_info: dict = Depends(verify_license)):
    conn = sqlite3.connect(pro_settings.license_db_path)

    from datetime import date
    today = date.today().isoformat()
    daily = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM usage_log WHERE license_key = ? AND timestamp >= ?",
        (license_info["key"], today)
    ).fetchone()

    month_start = date.today().replace(day=1).isoformat()
    monthly = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM usage_log WHERE license_key = ? AND timestamp >= ?",
        (license_info["key"], month_start)
    ).fetchone()

    conn.close()

    return {
        "plan": license_info["plan"],
        "daily": {"requests": daily[0], "tokens": daily[1], "limit": pro_settings.max_requests_per_day},
        "monthly": {"requests": monthly[0], "tokens": monthly[1], "limit": pro_settings.max_requests_per_month},
    }


# ─── Admin Endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/pending")
async def list_pending(_: bool = Depends(_verify_admin)):
    """List all pending (active=0) license requests."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        rows = conn.execute(
            "SELECT key, owner, email, ip, created_at, active FROM licenses WHERE active = 0"
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "key": r[0],
            "owner": r[1],
            "email": r[2],
            "ip": r[3],
            "created_at": r[4],
            "active": r[5],
        }
        for r in rows
    ]


@router.get("/admin/all")
async def list_all(_: bool = Depends(_verify_admin)):
    """List all licenses with usage stats."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        rows = conn.execute(
            "SELECT key, owner, email, plan, active, created_at, ip FROM licenses"
        ).fetchall()

        today = date.today().isoformat()
        month_start = date.today().replace(day=1).isoformat()

        result = []
        for r in rows:
            key = r[0]
            daily = conn.execute(
                "SELECT COUNT(*) FROM usage_log WHERE license_key = ? AND timestamp >= ?",
                (key, today)
            ).fetchone()[0]
            monthly = conn.execute(
                "SELECT COUNT(*) FROM usage_log WHERE license_key = ? AND timestamp >= ?",
                (key, month_start)
            ).fetchone()[0]
            result.append({
                "key": key,
                "owner": r[1],
                "email": r[2],
                "plan": r[3],
                "active": r[4],
                "created_at": r[5],
                "ip": r[6],
                "daily_usage": daily,
                "monthly_usage": monthly,
            })
    finally:
        conn.close()

    return result


@router.post("/admin/approve/{key}", response_model=AdminActionResponse)
async def approve_license(key: str, _: bool = Depends(_verify_admin)):
    """Set active=1 for the given key."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        row = conn.execute("SELECT key FROM licenses WHERE key = ?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="License key not found")
        conn.execute("UPDATE licenses SET active = 1, plan = 'pro' WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()

    return AdminActionResponse(key=key, status="approved", message=f"License {key} approved.")


@router.post("/admin/reject/{key}", response_model=AdminActionResponse)
async def reject_license(key: str, _: bool = Depends(_verify_admin)):
    """Delete the pending license."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        row = conn.execute("SELECT key FROM licenses WHERE key = ? AND active = 0", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pending license key not found")
        conn.execute("DELETE FROM licenses WHERE key = ? AND active = 0", (key,))
        conn.commit()
    finally:
        conn.close()

    return AdminActionResponse(key=key, status="rejected", message=f"License {key} rejected and deleted.")


@router.post("/admin/deactivate/{key}", response_model=AdminActionResponse)
async def deactivate_license(key: str, _: bool = Depends(_verify_admin)):
    """Set active=0 for an existing active key."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    try:
        row = conn.execute("SELECT key FROM licenses WHERE key = ?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="License key not found")
        conn.execute("UPDATE licenses SET active = 0 WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()

    return AdminActionResponse(key=key, status="deactivated", message=f"License {key} deactivated.")
