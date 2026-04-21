"""Lookup fallback into the crawler products DB when an MPN is not in heritage."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None  # fetch helper returns a clear error in that case


def find_product(db_path: str, mpn: str, device_type: Optional[str] = None) -> Optional[dict]:
    """Return a product row matching the MPN, optionally filtered by device_type.

    Matches case-insensitive on ``external_id`` or the first whitespace-separated
    token of ``name``. Picks the product whose ``metadata.datasheet_url`` is set
    over those that don't.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        clauses = ["(UPPER(external_id) = UPPER(?) OR UPPER(name) LIKE UPPER(?))"]
        params: list[object] = [mpn, f"{mpn}%"]
        if device_type:
            clauses.append("device_type = ?")
            params.append(device_type)
        rows = conn.execute(
            f"SELECT id, site_id, external_id, name, brand, category, device_type, "
            f"description, specs, metadata, url FROM products "
            f"WHERE {' AND '.join(clauses)} LIMIT 10",
            params,
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    def _score(row) -> int:
        s = 0
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except Exception:
            meta = {}
        if meta.get("datasheet_url"):
            s += 10
        if (row["external_id"] or "").upper() == mpn.upper():
            s += 5
        return s

    best = max(rows, key=_score)
    meta = {}
    try:
        meta = json.loads(best["metadata"]) if best["metadata"] else {}
    except Exception:
        pass
    specs = {}
    try:
        specs = json.loads(best["specs"]) if best["specs"] else {}
    except Exception:
        pass

    return {
        "id": best["id"],
        "site_id": best["site_id"],
        "external_id": best["external_id"],
        "name": best["name"],
        "brand": best["brand"],
        "category": best["category"],
        "device_type": best["device_type"],
        "description": best["description"],
        "url": best["url"],
        "datasheet_url": meta.get("datasheet_url"),
        "specs": specs,
    }


def fetch_datasheet_bytes(url: str, max_bytes: int = 20 * 1024 * 1024, timeout: float = 25.0) -> bytes:
    """Download a PDF datasheet into memory. Raises on failure or size overflow."""
    if httpx is None:
        raise RuntimeError("httpx is not installed; cannot auto-fetch datasheet")
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.content
    if len(data) > max_bytes:
        raise ValueError(f"Datasheet PDF exceeds {max_bytes} bytes")
    if len(data) < 200:
        raise ValueError("Datasheet PDF is too small / empty")
    return data
