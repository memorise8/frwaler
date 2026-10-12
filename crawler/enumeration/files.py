"""Separate file (PDF/attachment) pass with its own ledger.

Body enumeration only enqueues file URLs. This pass downloads them through the
same request gate, verifies them, stores them and records size/hash/pages.
The primary PDF uses the existing blob layout and ``update_document_pdf``.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from .http import AccessBlocked, BudgetExhausted, PermanentFailure, TransientFailure

_PAGE = re.compile(rb"/Type\s*/Page(?!s)\b")


def pdf_page_count(data):
    """Approximate page count from page objects (no external dependency)."""
    count = len(_PAGE.findall(data))
    return count or None


def is_pdf(data):
    prefix = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n\x00")
    return prefix.startswith(b"%PDF-")


def _ext_for(url, content_type, data):
    if is_pdf(data):
        return "pdf"
    m = re.search(r"\.([A-Za-z0-9]{2,5})(?:$|\?)", url)
    if m:
        return m.group(1).lower()
    return (content_type.split("/")[-1].split(";")[0] or "bin").lower()[:8]


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class FilePass:
    def __init__(self, store, fetcher, *, blob_root, doc_conn=None, max_attempts=3, max_bytes=200 * 1024 * 1024):
        self.store = store
        self.fetcher = fetcher
        self.blob_root = Path(blob_root)
        self.doc_conn = doc_conn
        self.max_attempts = max_attempts
        self.max_bytes = max_bytes

    def _store_primary(self, seq_id, data):
        from crawler import blob_storage
        from crawler.db_backend import get_backend
        path, size, sha = blob_storage.save_pdf(seq_id, data, root=self.blob_root)
        if self.doc_conn is not None:
            backend = get_backend("sqlite" if isinstance(self.doc_conn, sqlite3.Connection) else "postgres")
            backend.update_document_pdf(self.doc_conn, seq_id, downloaded=True, size_bytes=size, sha256=sha)
        return path

    def run(self, site_id):
        stats = {"done": 0, "failed": 0, "failed_transient": 0, "unavailable": 0, "stopped": None}
        for row in self.store.pending_files(site_id, self.max_attempts):
            url, seq_id, role = row["url"], row["seq_id"], row["role"]
            try:
                resp = self.fetcher.get(url)
            except PermanentFailure as exc:
                self.store.update_file(site_id, seq_id, url, status="failed", reason=exc.reason)
                stats["failed"] += 1
                continue
            except TransientFailure as exc:
                attempts = row["attempts"] + 1
                status = "failed_transient" if attempts < self.max_attempts else "failed"
                reason = f"transient:{exc.reason}" if status == "failed_transient" else f"retries_exhausted:{exc.reason}"
                self.store.update_file(site_id, seq_id, url, status=status, reason=reason)
                stats[status] += 1
                continue
            except AccessBlocked as exc:
                self.store.update_file(site_id, seq_id, url, status="unavailable", reason=f"access_blocked:{exc.reason}")
                stats["unavailable"] += 1
                stats["stopped"] = f"access_blocked:{exc.reason}"
                break
            except BudgetExhausted as exc:
                stats["stopped"] = exc.reason
                break
            data = resp.body
            ctype = resp.content_type
            if not data:
                self.store.update_file(site_id, seq_id, url, status="failed", reason="empty_body")
                stats["failed"] += 1
                continue
            if len(data) > self.max_bytes:
                self.store.update_file(site_id, seq_id, url, status="failed", reason="too_large")
                stats["failed"] += 1
                continue
            expects_pdf = role == "pdf" or url.lower().split("?")[0].endswith(".pdf") or "pdf" in ctype.lower()
            if expects_pdf and not is_pdf(data):
                reason = "html_instead_of_pdf" if b"<html" in data[:2048].lower() else "invalid_pdf_header"
                self.store.update_file(site_id, seq_id, url, status="failed", reason=reason, content_type=ctype,
                                       size=len(data))
                stats["failed"] += 1
                continue
            sha = hashlib.sha256(data).hexdigest()
            pages = pdf_page_count(data) if is_pdf(data) else None
            if role == "pdf" and is_pdf(data):
                path = self._store_primary(seq_id, data)
            else:
                path = self.blob_root / "attachments" / str(seq_id) / f"{sha}.{_ext_for(url, ctype, data)}"
                _atomic_write(path, data)
            self.store.update_file(site_id, seq_id, url, status="done", size=len(data), sha256=sha, pages=pages,
                                   content_type=ctype, reason=None, path=str(path))
            stats["done"] += 1
        return stats
