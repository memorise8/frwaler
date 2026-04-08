import sqlite3
from datetime import datetime, date
from fastapi import Header, HTTPException

LICENSE_DB = None


def init_license_db(db_path: str):
    global LICENSE_DB
    LICENSE_DB = db_path
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            plan TEXT DEFAULT 'basic',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            timestamp TEXT DEFAULT (datetime('now')),
            tokens_used INTEGER DEFAULT 0,
            FOREIGN KEY (license_key) REFERENCES licenses(key)
        )
    """)
    conn.commit()
    conn.close()


async def verify_license(x_license_key: str = Header(...)):
    """FastAPI dependency - validates license key from header."""
    if not x_license_key:
        raise HTTPException(status_code=401, detail="License key required")

    conn = sqlite3.connect(LICENSE_DB)
    row = conn.execute(
        "SELECT key, plan, active, expires_at FROM licenses WHERE key = ?",
        (x_license_key,)
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid license key")

    if not row[2]:  # active
        conn.close()
        raise HTTPException(status_code=403, detail="License key is deactivated")

    if row[3] and row[3] < datetime.now().isoformat():  # expires_at
        conn.close()
        raise HTTPException(status_code=403, detail="License key has expired")

    # Check daily rate limit
    today = date.today().isoformat()
    count = conn.execute(
        "SELECT COUNT(*) FROM usage_log WHERE license_key = ? AND timestamp >= ?",
        (x_license_key, today)
    ).fetchone()[0]

    from .settings import pro_settings
    if count >= pro_settings.max_requests_per_day:
        conn.close()
        raise HTTPException(status_code=429, detail="Daily rate limit exceeded")

    conn.close()
    return {"key": x_license_key, "plan": row[1]}


def log_usage(license_key: str, endpoint: str, tokens_used: int = 0):
    """Log API usage for billing/analytics."""
    conn = sqlite3.connect(LICENSE_DB)
    conn.execute(
        "INSERT INTO usage_log (license_key, endpoint, tokens_used) VALUES (?, ?, ?)",
        (license_key, endpoint, tokens_used)
    )
    conn.commit()
    conn.close()
