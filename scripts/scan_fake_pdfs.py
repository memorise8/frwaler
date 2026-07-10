#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Scan for fake PDFs (HTML files saved as .pdf) in libertree.db.

This script identifies blobs marked pdf_downloaded=1 but are actually HTML files,
which cannot be text-extracted. It optionally resets the DB rows and deletes
the fake blob files to enable future recovery passes to retry.
"""

import argparse
import logging
import sqlite3
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.db_libertree import open_db, init_db


# ============================================================================
# Constants
# ============================================================================

BLOB_ROOT = Path("libertree")
FAKE_SMALL_BYTES = 10240  # 10KB 미만 unknown은 가짜로 판정


# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(report_file: Optional[Path] = None) -> Tuple[logging.Logger, Path]:
    """Set up logging to both stdout and file."""
    logger = logging.getLogger("scan_fake_pdfs")
    logger.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "%(levelname)-8s | %(message)s"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler
    if report_file is None:
        log_dir = Path("data/audit/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = log_dir / f"fake_pdf_scan_{ts}.log"

    file_handler = logging.FileHandler(report_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger, report_file


# ============================================================================
# Blob Classification
# ============================================================================

def classify_blob(blob_path: Path) -> str:
    """Classify a blob by reading first 16 bytes.

    Returns one of:
    - "ok_pdf": Starts with %PDF-
    - "fake_html": Starts with <!, <html, <HTML, <?xml (case-insensitive)
    - "valid_binary": ZIP (PK\x03\x04) or Office (\xd0\xcf\x11\xe0)
    - "missing": File doesn't exist
    - "empty": File is 0 bytes
    - "unknown": None of the above
    """
    if not blob_path.exists():
        return "missing"

    try:
        size = blob_path.stat().st_size
        if size == 0:
            return "empty"

        # Read first 16 bytes
        with open(blob_path, "rb") as f:
            magic = f.read(16)

        if not magic:
            return "empty"

        # Check for PDF signature
        if magic.startswith(b"%PDF-"):
            return "ok_pdf"

        # Check for HTML signatures
        magic_str = magic.decode("utf-8", errors="ignore").lower()
        if any(
            magic_str.startswith(prefix)
            for prefix in ["<!", "<html", "<?xml"]
        ):
            return "fake_html"

        # Also check raw bytes for uppercase <HTML (not yet lowercased)
        magic_raw = magic.decode("utf-8", errors="ignore")
        if magic_raw.startswith("<HTML"):
            return "fake_html"

        # Check for ZIP (Office, etc.)
        if magic.startswith(b"PK\x03\x04"):
            return "valid_binary"

        # Check for Office OLE2
        if magic.startswith(b"\xd0\xcf\x11\xe0"):
            return "valid_binary"

        return "unknown"

    except Exception as e:
        return f"error_{type(e).__name__}"


def is_fake(classification: str, size_bytes: int) -> bool:
    """fake 판정: HTML이거나, 10KB 미만의 unknown(에러 텍스트/리다이렉트 등)."""
    if classification == "fake_html":
        return True
    if classification == "unknown" and size_bytes < FAKE_SMALL_BYTES:
        return True
    return False


def blob_path_for(seq_id: int) -> Path:
    """Derive blob path from seq_id.

    seq_id is formatted as 12 digits: first 4 digits, then next 4 digits, then full ID.
    Example: seq_id=123456 -> "000000123456" -> "libertree/0000/0012/000000123456.pdf"
    """
    s = f"{seq_id:012d}"
    return BLOB_ROOT / s[:4] / s[4:8] / f"{s}.pdf"


# ============================================================================
# Database and File Operations
# ============================================================================

def reset_pdf_row(conn: sqlite3.Connection, seq_id: int) -> None:
    """Reset pdf_downloaded, pdf_size_bytes, pdf_sha256 for a row."""
    conn.execute(
        """
        UPDATE documents
           SET pdf_downloaded = 0,
               pdf_size_bytes = 0,
               pdf_sha256 = '',
               text_extracted = 0
         WHERE seq_id = ?
        """,
        (seq_id,),
    )
    conn.commit()


def delete_blob_file(blob_path: Path) -> None:
    """Delete blob file if it exists."""
    blob_path.unlink(missing_ok=True)


# ============================================================================
# Main Scanning Logic
# ============================================================================

class Scanner:
    """Thread-safe scanner for fake PDFs."""

    def __init__(
        self,
        db_path: Path,
        reset: bool = False,
        delete_blob: bool = False,
        limit: Optional[int] = None,
        workers: int = 8,
    ):
        self.db_path = db_path
        self.reset = reset
        self.delete_blob = delete_blob
        self.limit = limit
        self.workers = workers

        self.conn = None
        self.db_lock = threading.Lock()

        # sqlite3 connections are only usable from the thread that created
        # them (check_same_thread defaults to True). Worker threads get
        # their own lazily-opened connection instead of sharing self.conn.
        self._thread_local = threading.local()
        self._thread_conns = []
        self._thread_conns_lock = threading.Lock()

        # Statistics
        self.stats = defaultdict(int)
        self.site_fake_counts = defaultdict(int)
        self.stats_lock = threading.Lock()

    def open(self):
        """Open database connection."""
        self.conn = open_db(self.db_path)
        init_db(self.conn)

    def _get_write_conn(self) -> sqlite3.Connection:
        """Return a connection usable by the *current* thread for writes."""
        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = open_db(self.db_path)
            self._thread_local.conn = conn
            with self._thread_conns_lock:
                self._thread_conns.append(conn)
        return conn

    def close(self):
        """Close database connection(s).

        Thread-local connections were opened by worker threads and cannot be
        closed here (sqlite3 enforces same-thread usage); they are released
        when the process exits. We only attempt a best-effort close and
        never let a cross-thread close error abort the run after work
        already completed successfully.
        """
        if self.conn:
            self.conn.close()
        for conn in self._thread_conns:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                pass

    def fetch_rows(self) -> list:
        """Fetch all rows where pdf_downloaded=1."""
        rows = self.conn.execute(
            """
            SELECT seq_id, site_id
              FROM documents
             WHERE pdf_downloaded = 1
             ORDER BY seq_id ASC
            """,
        ).fetchall()

        if self.limit:
            rows = rows[:self.limit]

        return rows

    def process_row(self, row) -> None:
        """Process a single row (read, classify, optionally reset/delete)."""
        seq_id = row[0]
        site_id = row[1]

        blob_path = blob_path_for(seq_id)
        classification = classify_blob(blob_path)
        size = blob_path.stat().st_size if blob_path.exists() else 0
        fake = is_fake(classification, size) or classification == "missing"

        # Update stats
        with self.stats_lock:
            self.stats["total_checked"] += 1
            self.stats[f"count_{classification}"] += 1

            if fake:
                self.stats["count_fake_total"] += 1
                self.site_fake_counts[site_id] += 1

        # Handle actions
        if fake:
            if self.reset or self.delete_blob:
                # For logging: only log if action is taken
                action_parts = []
                if self.reset:
                    action_parts.append("reset")
                if self.delete_blob:
                    action_parts.append("delete")
                action_str = "+".join(action_parts)

                if self.reset:
                    with self.db_lock:
                        reset_pdf_row(self._get_write_conn(), seq_id)

                if self.delete_blob:
                    delete_blob_file(blob_path)
                    delete_blob_file(blob_path.with_suffix(".txt"))

    def run(self) -> None:
        """Run the full scan."""
        self.open()
        try:
            rows = self.fetch_rows()

            if not rows:
                print("No rows to scan (pdf_downloaded=1).")
                return

            print(f"Scanning {len(rows)} documents...")

            # Process with ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [
                    executor.submit(self.process_row, row)
                    for row in rows
                ]

                # Wait for all to complete
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        print(f"Error processing row: {e}")

            # Print summary
            self.print_summary()

        finally:
            self.close()

    def print_summary(self) -> None:
        """Print summary statistics."""
        print("\n" + "=" * 70)
        print("SCAN SUMMARY")
        print("=" * 70)

        total = self.stats.get("total_checked", 0)
        print(f"\nTotal blobs checked: {total}")

        print("\nCategory breakdown:")
        for category in [
            "ok_pdf", "fake_html", "valid_binary",
            "missing", "empty", "unknown"
        ]:
            count = self.stats.get(f"count_{category}", 0)
            pct = (count / total * 100) if total > 0 else 0
            print(f"  {category:20s}: {count:6d} ({pct:5.1f}%)")

        fake_total = self.stats.get("count_fake_total", 0)
        pct = (fake_total / total * 100) if total > 0 else 0
        print(f"\nTotal fake (fake_html + missing + small unknown): {fake_total:6d} ({pct:5.1f}%)")

        # Error categories
        error_categories = [k for k in self.stats.keys() if k.startswith("count_error_")]
        if error_categories:
            print("\nError categories:")
            for cat in error_categories:
                count = self.stats[cat]
                print(f"  {cat:20s}: {count:6d}")

        # Top 20 sites with fake blobs
        if self.site_fake_counts:
            print("\nTop 20 sites with fake blobs:")
            sorted_sites = sorted(
                self.site_fake_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:20]
            for site_id, count in sorted_sites:
                print(f"  {site_id:40s}: {count:6d}")

        print("\n" + "=" * 70)

        # Summary of actions
        if self.reset or self.delete_blob:
            action_parts = []
            if self.reset:
                action_parts.append("reset")
            if self.delete_blob:
                action_parts.append("delete-blob")
            print(f"Actions applied: {', '.join(action_parts)}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Scan for fake PDFs (HTML files saved as .pdf) in libertree.db."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset pdf_downloaded=0, pdf_size_bytes=0, pdf_sha256='' for fake/missing blobs.",
    )
    parser.add_argument(
        "--delete-blob",
        action="store_true",
        help="Also delete the fake blob file from disk.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit scan to N rows (for testing).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of worker threads (default 8).",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Path to write log file (default: data/audit/logs/fake_pdf_scan_{TS}.log).",
    )

    args = parser.parse_args()

    # Setup logging
    logger, log_file = setup_logging(args.report_file)

    logger.info("=" * 70)
    logger.info("scan_fake_pdfs.py")
    logger.info("=" * 70)
    logger.info(f"Reset: {args.reset}")
    logger.info(f"Delete blob: {args.delete_blob}")
    logger.info(f"Limit: {args.limit}")
    logger.info(f"Workers: {args.workers}")
    logger.info(f"Log file: {log_file}")
    logger.info("")

    # Run scanner
    try:
        db_path = Path("data/libertree.db")
        scanner = Scanner(
            db_path=db_path,
            reset=args.reset,
            delete_blob=args.delete_blob,
            limit=args.limit,
            workers=args.workers,
        )
        scanner.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
