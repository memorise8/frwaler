from fastapi import APIRouter, Depends
from ..auth import verify_license
import sqlite3
from ..settings import pro_settings

router = APIRouter(prefix="/pro/api", tags=["license"])


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
