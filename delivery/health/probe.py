"""Run a small metadata probe in a disposable process, never on the delivery DB.

This checks fetching/parsing only. It does NOT certify DB persistence, PDF
download, completeness, or all pages of a site. The parent enforces a hard
timeout on the entire process group, including curl/browser descendants.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from .results import atomic_json, utc_now
from crawler.search_credentials import missing_credentials


class ProbeComplete(BaseException):
    pass


class UnsupportedProbe(BaseException):
    pass


class QuietOutput(io.TextIOBase):
    # Never retain crawler output: it may contain URLs, tokens or documents.
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def sample_metadata(cls, *, limit: int, delay: float, progress: Path) -> dict:
    from crawler.base_crawler import BaseCrawler
    from crawler import db_libertree, db, db_pg, blob_storage, storage, pdf_downloader
    import requests

    stats = {"sample_count": 0, "invalid_count": 0, "http_errors": 0,
             "blocked": 0, "network_errors": 0, "unsupported": 0}
    seen = set()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_libertree.init_db(conn)
    conn.execute("PRAGMA query_only=ON")

    def authorize(operation, arg1, arg2, database, trigger):
        if operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                         sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            stats["unsupported"] += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)

    def denied(*args, **kwargs):
        stats["unsupported"] += 1
        raise UnsupportedProbe()

    def capture(instance, doc):
        title = doc.get("title") if isinstance(doc, dict) else None
        url = next((doc.get(k) for k in ("meta_url", "url", "pdf_url") if doc.get(k)), "") if isinstance(doc, dict) else ""
        try:
            parsed = urlsplit(url)
            valid = isinstance(title, str) and bool(title.strip()) and parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            stats["invalid_count"] += 1
            atomic_json(progress, stats)
            return None
        key = hashlib.sha256((title.strip() + str(url)).encode()).digest()
        if key not in seen:
            seen.add(key)
            stats["sample_count"] += 1
        instance._last_save_created = True
        atomic_json(progress, stats)
        if stats["sample_count"] >= limit:
            raise ProbeComplete()
        return stats["sample_count"]

    def wrap_request(original):
        def request(*args, **kwargs):
            try:
                response = original(*args, **kwargs)
                status = getattr(response, "status_code", 0)
                if status >= 400:
                    stats["http_errors"] += 1
                if status in (401, 403, 429):
                    stats["blocked"] += 1
                return response
            except Exception:
                stats["network_errors"] += 1
                raise
            finally:
                atomic_json(progress, stats)
        return request

    original_run = subprocess.run

    def run_command(command, *args, **kwargs):
        result = original_run(command, *args, **kwargs)
        if isinstance(command, (tuple, list)) and command and Path(str(command[0])).name in ("curl", "wget"):
            if result.returncode:
                stats["network_errors"] += 1
                atomic_json(progress, stats)
        return result

    state = None
    exception_type = None
    try:
        with contextlib.ExitStack() as stack:
            for name in ("_save_paper", "_save_paper_v2", "_save_document", "_save_paper_legacy"):
                stack.enter_context(patch.object(BaseCrawler, name, capture))
            # Fail closed on paths outside the standard metadata-save contract.
            for module, names in (
                (db_libertree, ("open_db", "insert_document", "update_document_pdf", "update_document_text")),
                (db_pg, ("open_db", "insert_document", "update_document_pdf", "update_document_text")),
                (db, ("get_db", "upsert_document", "upsert_paper")),
                (blob_storage, ("save_pdf", "save_text")),
                (storage, ("cleanup_doc_files",)),
                (pdf_downloader, ("download_pdf_for",)),
            ):
                for name in names:
                    stack.enter_context(patch.object(module, name, denied))
            stack.enter_context(patch("sqlite3.connect", denied))
            stack.enter_context(patch("psycopg.connect", denied))
            stack.enter_context(patch.object(requests.Session, "request", wrap_request(requests.Session.request)))
            try:
                from curl_cffi.requests import Session
                stack.enter_context(patch.object(Session, "request", wrap_request(Session.request)))
            except ImportError:
                pass
            stack.enter_context(patch.object(subprocess, "run", run_command))
            stack.enter_context(contextlib.redirect_stdout(QuietOutput()))
            stack.enter_context(contextlib.redirect_stderr(QuietOutput()))
            instance = cls(db_conn=conn, delay=delay)
            # Scratch storage has no production dedup state. ScienceON explicitly
            # rejects full mode; its incremental path is the bounded sample API.
            instance.delivery_mode = "incremental" if str(cls.site_id).startswith('scienceon-') else "full"
            instance.crawl(limit=limit)
            state = {"fetch_failed": "network_error", "invalid_response": "invalid_data",
                     "authentication_failed": "blocked", "rate_limited": "blocked", "configuration": "configuration",
                     "item_failed": "error"}.get(getattr(instance, "delivery_source_failure", None))
    except ProbeComplete:
        pass
    except UnsupportedProbe:
        state = "unsupported"
    except Exception as exc:
        exception_type = type(exc).__name__
        state = "error"
    finally:
        conn.close()
    if stats["unsupported"]:
        state = "unsupported"
    elif stats["sample_count"]:
        state = "partial" if state or stats["http_errors"] or stats["network_errors"] or stats["invalid_count"] else "healthy"
    elif stats["blocked"]:
        state = "blocked"
    elif stats["network_errors"] or stats["http_errors"]:
        state = "network_error"
    elif stats["invalid_count"]:
        state = "invalid_data"
    return {**stats, "state": state or "empty", "exception_type": exception_type}


def child_environment(scratch: Path, timeout: float) -> dict:
    # The parent may be an admin shell. Never inherit its production DB DSN,
    # API token, cloud credentials or Python startup customisation.
    allowed = ("PATH", "LANG", "LC_ALL", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
               "SCIENCEON_KEY", "SCIENCEON_CLIENT_ID", "SCIENCEON_MAC_ADDRESS")
    from crawler.search_credentials import SEARCH_KEYS
    env = {key: os.environ[key] for key in (*allowed, *SEARCH_KEYS.values()) if key in os.environ}
    # HOME below is disposable, but Chromium was installed in the image's
    # original cache. Preserve that location rather than reporting missing
    # browsers as a site failure.
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"))
    env.update(HOME=str(scratch), TMPDIR=str(scratch), PYTHONDONTWRITEBYTECODE="1",
               # Many legacy crawlers parse this as int and subtract 30/60s
               # before starting a page. The parent, not this hint, owns the
               # probe's shorter hard timeout.
               LIBERTREE_DB_BACKEND="sqlite", LIBERTREE_MAX_WALL_S=str(max(120, math.ceil(timeout) + 60)),
               LIBERTREE_BLOB_ROOT=str(scratch / "blob"), LIBERTREE_DATA_ROOT=str(scratch / "data"),
               SCIENCEON_TOKEN_CACHE=str(scratch / "scienceon-token.json"))
    return env


def run_probe(site: dict, *, timeout: float = 60, limit: int = 3, delay: float = 1) -> dict:
    if not 1 <= limit <= 10 or not 1 <= timeout <= 600 or not 0.1 <= delay <= 10:
        raise ValueError("probe limits out of range")
    started = time.monotonic()
    result = {"site_id": site["site_id"], "state": "error", "sample_count": 0,
              "invalid_count": 0, "http_errors": 0, "source_hash": site["source_hash"],
              "scope": "metadata_sample_only"}
    if site["code_status"] != "ready":
        result["state"] = "unsupported"
    elif missing_credentials(site['site_id']):
        result["state"] = "configuration"
    else:
        with tempfile.TemporaryDirectory(prefix="crawler-probe-") as directory:
            scratch = Path(directory)
            output, progress = scratch / "result.json", scratch / "progress.json"
            # cwd is disposable; the package root is explicitly on PYTHONPATH.
            env = child_environment(scratch, timeout)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            command = [sys.executable, "-m", "delivery.health.probe", site["site_id"],
                       str(output), str(progress), str(limit), str(delay)]
            process = subprocess.Popen(command, cwd=scratch, env=env, start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                process.wait(timeout=timeout)
                if process.returncode == 0 and output.exists():
                    data = json.loads(output.read_text())
                    result.update({k: data.get(k) for k in ("state", "sample_count", "invalid_count", "http_errors", "exception_type")})
            except subprocess.TimeoutExpired:
                result["state"] = "timeout"
                if progress.exists():
                    try:
                        partial = json.loads(progress.read_text())
                        result.update({k: partial.get(k, 0) for k in ("sample_count", "invalid_count", "http_errors")})
                    except (ValueError, OSError):
                        pass
            finally:
                # Even a normally exiting crawler can leave browser processes.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    result.update(checked_at=utc_now(), elapsed_s=round(time.monotonic() - started, 2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("site")
    parser.add_argument("output", type=Path)
    parser.add_argument("progress", type=Path)
    parser.add_argument("limit", type=int)
    parser.add_argument("delay", type=float)
    args = parser.parse_args()
    from crawler.sites import CRAWLERS
    data = sample_metadata(CRAWLERS[args.site], limit=args.limit, delay=args.delay, progress=args.progress)
    atomic_json(args.output, data)


if __name__ == "__main__":
    main()
