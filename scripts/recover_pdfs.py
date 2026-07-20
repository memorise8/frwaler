# -*- coding: utf-8 -*-
"""One-off PDF recovery script for green-tier government sites.

Targets documents where pdf_downloaded=0 but a pdf_url exists, using
browser UA + Referer + verify=False + GET + retry — fixes gaps left by
the production pdf_downloader for sites that require these techniques.

Usage:
    python scripts/recover_pdfs.py --green-only [--limit N] [--dry-run]
    python scripts/recover_pdfs.py --site-ids anses-fr-fr,klri-re-kr-kor [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import urllib3

# ---------------------------------------------------------------------------
# Project root on sys.path so crawler.* imports work from scripts/
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.blob_storage import save_pdf  # noqa: E402
from crawler.db_libertree import update_document_pdf  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GREEN_SITES: List[str] = [
    "anses-fr-fr",
    "defense-gouv-fr-salle-de-presse",
    "krihs-re-kr-boardes",
    "klri-re-kr-kor",
    "mivau-gob-es-el-ministerio",
    "khs-go-kr-newsbbz",
    "ntrs-nasa-gov-search",
    "transparency-gov-au-publications",
    "who-int-publications",
    "si-re-kr-bbs",
    "mois-go-kr-frt",
]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MAX_CONTENT_BYTES = 100 * 1024 * 1024  # 100 MB
TIMEOUT_SECS = 30
MAX_ATTEMPTS = 3
RETRY_BACKOFFS = [5, 15]  # seconds before attempt 2, attempt 3

# HTTP statuses that trigger a retry
RETRY_STATUSES = {429, 503, 504}
# HTTP statuses counted as "no-retry fail"
NO_RETRY_STATUSES = {403, 404, 500}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"pdf_recovery_{ts}.log"

    fmt = "%(asctime)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    logger = logging.getLogger("pdf_recovery")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    fh.setLevel(logging.DEBUG)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    sh.setLevel(logging.DEBUG)

    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.info("Log file: %s", log_file)
    return logger


# ---------------------------------------------------------------------------
# Per-site counters (thread-safe via lock)
# ---------------------------------------------------------------------------


class SiteStats:
    __slots__ = (
        "total",
        "success",
        "fail",
        "skip_too_big",
        "http_403",
        "http_404",
        "http_500",
        "conn_err",
        "ssl_err",
        "_lock",
    )

    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.fail = 0
        self.skip_too_big = 0
        self.http_403 = 0
        self.http_404 = 0
        self.http_500 = 0
        self.conn_err = 0
        self.ssl_err = 0
        self._lock = threading.Lock()

    def inc(self, field: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + n)


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------


def _make_headers(referer: str) -> dict:
    return {
        "User-Agent": BROWSER_UA,
        "Referer": referer,
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    }


def _is_html_content(content_type: str, first_2kb: bytes) -> bool:
    """Return True if this looks like an HTML error page, not a real document."""
    if "text/html" in content_type.lower():
        return True
    stripped = first_2kb.lstrip()
    if stripped.startswith(b"<"):
        return True
    return False


def download_pdf(
    pdf_url: str,
    meta_url: str,
    logger: logging.Logger,
    throttle_sec: float = 0.0,
) -> Tuple[Optional[bytes], str]:
    """Attempt to download pdf_url with retries.

    Returns (content_bytes, reason).
    content_bytes is None on failure; reason is a short description.
    """
    headers = _make_headers(meta_url)
    last_reason = "unknown"

    if throttle_sec > 0:
        time.sleep(throttle_sec)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(
                pdf_url,
                allow_redirects=True,
                timeout=TIMEOUT_SECS,
                verify=False,
                stream=True,
                headers=headers,
            )
        except requests.exceptions.SSLError as exc:
            last_reason = f"ssl_err: {exc}"
            break  # SSL errors: no retry benefit
        except requests.exceptions.ConnectionError as exc:
            last_reason = f"conn_err: {exc}"
            if attempt < MAX_ATTEMPTS:
                backoff = RETRY_BACKOFFS[attempt - 1]
                logger.debug("  conn_err attempt %d/%d, retry in %ds", attempt, MAX_ATTEMPTS, backoff)
                time.sleep(backoff)
                continue
            break
        except requests.exceptions.Timeout as exc:
            last_reason = f"timeout: {exc}"
            if attempt < MAX_ATTEMPTS:
                backoff = RETRY_BACKOFFS[attempt - 1]
                logger.debug("  timeout attempt %d/%d, retry in %ds", attempt, MAX_ATTEMPTS, backoff)
                time.sleep(backoff)
                continue
            break
        except Exception as exc:
            last_reason = f"unexpected: {exc}"
            break

        status = resp.status_code

        if status in RETRY_STATUSES:
            last_reason = f"http_{status}"
            if attempt < MAX_ATTEMPTS:
                backoff = RETRY_BACKOFFS[attempt - 1]
                logger.debug("  HTTP %d attempt %d/%d, retry in %ds", status, attempt, MAX_ATTEMPTS, backoff)
                time.sleep(backoff)
                continue
            break

        if status in NO_RETRY_STATUSES:
            last_reason = f"http_{status}"
            break

        if status not in {200, 206}:
            last_reason = f"http_{status}"
            break

        # Check Content-Length before streaming
        content_length = resp.headers.get("Content-Length")
        if content_length is not None:
            try:
                cl_int = int(content_length)
                if cl_int > MAX_CONTENT_BYTES:
                    last_reason = f"too_big: Content-Length={cl_int}"
                    resp.close()
                    return None, last_reason
            except ValueError:
                pass

        # Stream content, capping at 100 MB
        chunks: List[bytes] = []
        total_read = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total_read += len(chunk)
            if total_read > MAX_CONTENT_BYTES:
                last_reason = f"too_big: streamed>{MAX_CONTENT_BYTES}"
                resp.close()
                return None, last_reason
            chunks.append(chunk)

        content = b"".join(chunks)

        if not content:
            last_reason = "empty_response"
            break

        # Validate: reject HTML / empty
        first_2kb = content[:2048]
        content_type = resp.headers.get("Content-Type", "")
        if _is_html_content(content_type, first_2kb):
            last_reason = f"html_content (ct={content_type!r})"
            break

        return content, "ok"

    return None, last_reason


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def process_row(
    row: dict,
    stats: SiteStats,
    db_lock: threading.Lock,
    conn,
    logger: logging.Logger,
    dry_run: bool,
    throttle_sec: float = 0.0,
) -> None:
    seq_id: int = row["seq_id"]
    site_id: str = row["site_id"]
    meta_url: str = row["meta_url"] or ""
    pdf_url: str = row["pdf_url"] or ""

    stats.inc("total")

    if dry_run:
        logger.info("[%s] DRY-RUN seq_id=%s pdf_url=%s", site_id, seq_id, pdf_url)
        stats.inc("success")  # count as "would process"
        return

    try:
        content, reason = download_pdf(pdf_url, meta_url, logger, throttle_sec)
    except Exception as exc:
        reason = f"unexpected: {exc}"
        content = None

    if content is None:
        # Bucket the failure
        if reason.startswith("http_403"):
            stats.inc("http_403")
        elif reason.startswith("http_404"):
            stats.inc("http_404")
        elif reason.startswith("http_500"):
            stats.inc("http_500")
        elif reason.startswith("conn_err"):
            stats.inc("conn_err")
        elif reason.startswith("ssl_err"):
            stats.inc("ssl_err")
        elif reason.startswith("too_big"):
            stats.inc("skip_too_big")
        else:
            stats.inc("fail")
        logger.warning("[%s] %s FAIL: %s", site_id, seq_id, reason)
        return

    # Save blob
    try:
        path, size, sha = save_pdf(seq_id, content)
    except Exception as exc:
        stats.inc("fail")
        logger.error("[%s] %s FAIL: save_pdf error: %s", site_id, seq_id, exc)
        return

    # Update DB (serialised via lock)
    try:
        with db_lock:
            update_document_pdf(conn, seq_id, downloaded=True, size_bytes=size, sha256=sha)
    except Exception as exc:
        stats.inc("fail")
        logger.error("[%s] %s FAIL: db update error: %s", site_id, seq_id, exc)
        return

    stats.inc("success")
    logger.info("[%s] %s status=200 size=%d path=%s", site_id, seq_id, size, path)


# ---------------------------------------------------------------------------
# Progress reporter
# ---------------------------------------------------------------------------


def maybe_log_progress(
    site_id: str,
    stats: SiteStats,
    total_for_site: int,
    logger: logging.Logger,
    interval: int = 50,
) -> None:
    done = stats.success + stats.fail + stats.skip_too_big + stats.http_403 + stats.http_404 + stats.http_500 + stats.conn_err + stats.ssl_err
    if done > 0 and done % interval == 0:
        rate = int(100 * stats.success / done) if done else 0
        logger.info(
            "[%s] progress %d/%d success=%d fail=%d (rate %d%%)",
            site_id,
            done,
            total_for_site,
            stats.success,
            done - stats.success,
            rate,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recover PDFs for green-tier sites using browser UA + Referer + verify=False."
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--green-only",
        action="store_true",
        help="Use the hardcoded GREEN_SITES list.",
    )
    group.add_argument(
        "--site-ids",
        metavar="SID1,SID2,...",
        help="Comma-separated site_ids to process.",
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=8,
        metavar="N",
        help="Thread pool size (default: 8).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap total rows processed (for testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rows that would be processed without fetching.",
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to libertree.db (default: data/libertree.db).",
    )
    p.add_argument(
        "--throttle-sec",
        type=float,
        default=0.0,
        metavar="FLOAT",
        help="Seconds to sleep before each request (rate-limit mitigation).",
    )
    return p.parse_args()


def print_summary_table(
    site_stats: Dict[str, SiteStats],
    logger: logging.Logger,
) -> None:
    header = (
        f"{'site_id':<45} {'total':>6} {'success':>7} {'fail':>5} "
        f"{'too_big':>7} {'403':>5} {'404':>5} {'500':>5} {'conn':>5} {'ssl':>5}"
    )
    sep = "-" * len(header)
    logger.info("")
    logger.info("=== FINAL SUMMARY ===")
    logger.info(header)
    logger.info(sep)

    grand_total = grand_ok = grand_fail = 0
    for sid, st in sorted(site_stats.items()):
        fail_total = (
            st.fail + st.skip_too_big + st.http_403 + st.http_404
            + st.http_500 + st.conn_err + st.ssl_err
        )
        logger.info(
            f"{sid:<45} {st.total:>6} {st.success:>7} {fail_total:>5} "
            f"{st.skip_too_big:>7} {st.http_403:>5} {st.http_404:>5} "
            f"{st.http_500:>5} {st.conn_err:>5} {st.ssl_err:>5}"
        )
        grand_total += st.total
        grand_ok += st.success
        grand_fail += fail_total

    logger.info(sep)
    logger.info(
        f"{'TOTAL':<45} {grand_total:>6} {grand_ok:>7} {grand_fail:>5}"
    )
    logger.info("")


def main() -> None:
    args = parse_args()

    # Resolve site list
    if args.green_only:
        site_ids = GREEN_SITES
    elif args.site_ids:
        site_ids = [s.strip() for s in args.site_ids.split(",") if s.strip()]
    else:
        print(
            "ERROR: specify --green-only or --site-ids SID1,SID2,...",
            file=sys.stderr,
        )
        sys.exit(1)

    # Suppress urllib3 InsecureRequestWarning
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Logging
    log_dir = PROJECT_ROOT / "data" / "audit" / "logs"
    logger = setup_logging(log_dir)

    # DB — open with check_same_thread=False for multi-threaded workers
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "data" / "libertree.db"
    logger.info("DB: %s", db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    db_lock = threading.Lock()

    # Build placeholder list for SQL IN clause
    placeholders = ",".join("?" * len(site_ids))
    query = (
        "SELECT seq_id, site_id, meta_url, pdf_url "
        "FROM documents "
        "WHERE pdf_downloaded=0 "
        "  AND pdf_url IS NOT NULL "
        "  AND pdf_url != '' "
        f" AND site_id IN ({placeholders})"
        " ORDER BY site_id, seq_id"
    )
    rows = conn.execute(query, site_ids).fetchall()
    rows = [dict(r) for r in rows]

    if args.limit is not None:
        rows = rows[: args.limit]

    logger.info(
        "Found %d rows across %d sites (limit=%s, dry_run=%s, workers=%d, throttle_sec=%.1f)",
        len(rows),
        len(site_ids),
        args.limit,
        args.dry_run,
        args.max_workers,
        args.throttle_sec,
    )

    if not rows:
        logger.info("Nothing to do.")
        conn.close()
        return

    # Per-site stats objects
    site_stats: Dict[str, SiteStats] = {}
    site_counts: Dict[str, int] = {}
    for r in rows:
        sid = r["site_id"]
        site_counts[sid] = site_counts.get(sid, 0) + 1
        if sid not in site_stats:
            site_stats[sid] = SiteStats()

    # Execute
    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(
                    process_row,
                    row,
                    site_stats[row["site_id"]],
                    db_lock,
                    conn,
                    logger,
                    args.dry_run,
                    args.throttle_sec,
                ): row
                for row in rows
            }
            for fut in as_completed(futures):
                row = futures[fut]
                sid = row["site_id"]
                try:
                    fut.result()
                except Exception as exc:
                    site_stats[sid].inc("fail")
                    logger.error(
                        "[%s] %s FAIL: unhandled future exception: %s",
                        sid,
                        row["seq_id"],
                        exc,
                    )
                maybe_log_progress(
                    sid, site_stats[sid], site_counts[sid], logger
                )
    except KeyboardInterrupt:
        logger.warning("Interrupted by user — flushing progress...")

    print_summary_table(site_stats, logger)
    conn.close()


if __name__ == "__main__":
    main()
