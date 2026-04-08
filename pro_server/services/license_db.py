"""License database helpers - thin wrappers around auth.py for external use."""
import sqlite3
from ..settings import pro_settings


def create_license(key: str, owner: str, plan: str = "basic", expires_at: str = None):
    """Create a new license key."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    conn.execute(
        "INSERT INTO licenses (key, owner, plan, expires_at) VALUES (?, ?, ?, ?)",
        (key, owner, plan, expires_at)
    )
    conn.commit()
    conn.close()


def deactivate_license(key: str):
    """Deactivate a license key."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    conn.execute("UPDATE licenses SET active = 0 WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def list_licenses():
    """List all licenses."""
    conn = sqlite3.connect(pro_settings.license_db_path)
    rows = conn.execute("SELECT key, owner, plan, active, expires_at, created_at FROM licenses").fetchall()
    conn.close()
    return [
        {"key": r[0], "owner": r[1], "plan": r[2], "active": bool(r[3]), "expires_at": r[4], "created_at": r[5]}
        for r in rows
    ]
