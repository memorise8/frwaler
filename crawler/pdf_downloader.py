# -*- coding: utf-8 -*-
"""libertree PDF downloader (Phase A — new infra).

Downloads a single PDF for a documents row, saves it through
``crawler.blob_storage`` (12-digit zero-padded layout under the configured
blob root), and updates the row's ``pdf_downloaded`` / ``pdf_size_bytes`` /
``pdf_sha256`` columns via :mod:`crawler.db_libertree`.

Synchronous on purpose — orchestration concerns (concurrency, scheduling)
belong elsewhere. Failure is reported via the return dict, never raised
out.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

import requests

from . import blob_storage as _blobs
from . import db_libertree as _db

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_SIZE = 200 * 1024 * 1024  # 200 MB
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def download_pdf_for(
    conn: sqlite3.Connection,
    seq_id: int,
    pdf_url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_size: int = DEFAULT_MAX_SIZE,
    blob_root: Path | str | None = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """Download PDF for ``seq_id``, save to blob, update DB.

    Returns
    -------
    dict
        ``{"success": bool, "size_bytes": int, "sha256": str|None,
           "path": str|None, "error": str|None}``
    """
    result = {
        "success": False,
        "size_bytes": 0,
        "sha256": None,
        "path": None,
        "error": None,
    }

    if not pdf_url:
        result["error"] = "empty pdf_url"
        return result

    sess = session or requests.Session()
    if "User-Agent" not in sess.headers:
        sess.headers.update({"User-Agent": USER_AGENT})

    started = time.time()
    try:
        with sess.get(pdf_url, stream=True, timeout=timeout, allow_redirects=True) as resp:
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                result["error"] = f"http {resp.status_code}: {e}"
                _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
                return result

            # Optional Content-Length precheck.
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > max_size:
                        result["error"] = f"content-length {cl} exceeds max_size {max_size}"
                        _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
                        return result
                except ValueError:
                    pass

            chunks: list[bytes] = []
            received = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_size:
                    result["error"] = f"download exceeded max_size {max_size}"
                    _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
                    return result
                chunks.append(chunk)

            content = b"".join(chunks)
    except requests.RequestException as e:
        result["error"] = f"request error: {e}"
        _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
        return result

    if not content:
        result["error"] = "empty body"
        _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
        return result

    try:
        path, size, sha = _blobs.save_pdf(seq_id, content, root=blob_root)
    except (TypeError, ValueError, OSError) as e:
        result["error"] = f"save_pdf failed: {e}"
        _db.update_document_pdf(conn, seq_id, downloaded=False, size_bytes=0, sha256="")
        return result

    _db.update_document_pdf(conn, seq_id, downloaded=True, size_bytes=size, sha256=sha)

    result.update(
        success=True,
        size_bytes=size,
        sha256=sha,
        path=str(path),
        error=None,
        elapsed_s=round(time.time() - started, 3),
    )
    return result
