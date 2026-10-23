"""Atomic, bounded latest-probe records. No document content or raw errors."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATES = {"healthy", "partial", "empty", "blocked", "network_error", "timeout",
          "configuration", "error", "unsupported", "invalid_data"}
FIELDS = {"site_id", "state", "checked_at", "elapsed_s", "sample_count", "invalid_count",
          "source_hash", "http_errors", "exception_type", "scope", "reason"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_dir() -> Path:
    return Path(os.environ.get("CRAWLER_HEALTH_REPORT_DIR", "/data/health"))


def site_path(root: Path, site_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", site_id):
        raise ValueError("invalid site id")
    return root / f"{site_id}.json"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".health-")
    try:
        # Sanitized reports are shared read-only with a potentially non-root
        # backend container and are downloadable by the local operator.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_result(root: Path, site: dict, *, now: datetime | None = None,
                stale_hours: float = 48) -> dict:
    path = site_path(root, site["site_id"])
    if not path.exists():
        return {"state": "unchecked", "checked_at": None}
    try:
        # Metadata diagnostics contain up to 100 bounded events. The API still
        # returns only FIELDS, not the diagnostic payload or arbitrary extras.
        if path.stat().st_size > 256 * 1024:
            raise ValueError("oversized report")
        result = json.loads(path.read_text())
        if result["site_id"] != site["site_id"] or result["state"] not in STATES:
            raise ValueError("invalid report")
        checked = datetime.fromisoformat(result["checked_at"])
        if checked.tzinfo is None:
            raise ValueError("missing timezone")
        age = ((now or datetime.now(timezone.utc)) - checked).total_seconds()
        if age < -300:
            raise ValueError("future report")
        clean = {k: v for k, v in result.items() if k in FIELDS}
        clean["age_seconds"] = max(0, int(age))
        if result.get("source_hash") != site.get("source_hash"):
            clean.update(last_state=result["state"], state="code_changed")
        elif age > stale_hours * 3600:
            clean.update(last_state=result["state"], state="stale")
        return clean
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {"state": "report_error", "checked_at": None}
