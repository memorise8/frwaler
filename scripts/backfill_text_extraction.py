# -*- coding: utf-8 -*-
"""One-off text-extraction backfill script.

Targets documents where pdf_downloaded=1 AND text_extracted=0, re-running
extraction via crawler.converter.extract_text_for(conn, seq_id).

Usage:
    python scripts/backfill_text_extraction.py --site-ids SID1,SID2 [--limit N] [--max-workers N] [--dry-run]
    python scripts/backfill_text_extraction.py --all [--limit N] [--max-workers N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Project root on sys.path so crawler.* imports work from scripts/
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.converter import extract_text_for  # noqa: E402


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"text_backfill_{ts}.log"

    fmt = "%(asctime)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    logger = logging.getLogger("text_backfill")
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
# Per-site stats (thread-safe)
# ---------------------------------------------------------------------------


class SiteStats:
    __slots__ = ("total", "success", "fail", "_fail_reasons", "_lock")

    def __init__(self) -> None:
        self.total: int = 0
        self.success: int = 0
        self.fail: int = 0
        self._fail_reasons: Counter = Counter()
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self.total += 1
            self.success += 1

    def record_fail(self, reason: str) -> None:
        with self._lock:
            self.total += 1
            self.fail += 1
            self._fail_reasons[reason] += 1

    def top_fail_reasons(self, n: int = 3) -> List[tuple]:
        with self._lock:
            return self._fail_reasons.most_common(n)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def process_row(
    seq_id: int,
    site_id: str,
    conn: sqlite3.Connection,
    db_lock: threading.Lock,
    stats: SiteStats,
    logger: logging.Logger,
    dry_run: bool,
) -> None:
    if dry_run:
        logger.info("[%s] %s DRY-RUN (would extract)", site_id, seq_id)
        stats.record_success()
        return

    try:
        with db_lock:
            result = extract_text_for(conn, seq_id)
    except Exception as exc:
        reason = f"unhandled: {exc}"
        stats.record_fail(reason)
        logger.warning("[%s] %s FAIL %s", site_id, seq_id, reason)
        return

    if result.get("success"):
        chars = result.get("chars", 0)
        stats.record_success()
        logger.info("[%s] %s OK chars=%d", site_id, seq_id, chars)
    else:
        reason = result.get("error") or "unknown"
        stats.record_fail(reason)
        logger.warning("[%s] %s FAIL %s", site_id, seq_id, reason)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def maybe_log_progress(
    site_id: str,
    stats: SiteStats,
    total_for_site: int,
    logger: logging.Logger,
    interval: int = 100,
) -> None:
    done = stats.success + stats.fail
    if done > 0 and done % interval == 0:
        rate = int(100 * stats.success / done) if done else 0
        logger.info(
            "[%s] progress %d/%d success=%d fail=%d (rate %d%%)",
            site_id,
            done,
            total_for_site,
            stats.success,
            stats.fail,
            rate,
        )


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def print_summary_table(
    site_stats: Dict[str, SiteStats],
    logger: logging.Logger,
) -> None:
    header = f"{'site_id':<50} {'total':>6} {'success':>7} {'fail':>5}  top_fail_reasons"
    sep = "-" * 100
    logger.info("")
    logger.info("=== FINAL SUMMARY ===")
    logger.info(header)
    logger.info(sep)

    grand_total = grand_ok = grand_fail = 0
    for sid, st in sorted(site_stats.items()):
        top = "; ".join(f"{r}×{c}" for r, c in st.top_fail_reasons(3)) or "-"
        logger.info(
            f"{sid:<50} {st.total:>6} {st.success:>7} {st.fail:>5}  {top}"
        )
        grand_total += st.total
        grand_ok += st.success
        grand_fail += st.fail

    logger.info(sep)
    logger.info(f"{'TOTAL':<50} {grand_total:>6} {grand_ok:>7} {grand_fail:>5}")
    logger.info("")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill text extraction for documents where pdf_downloaded=1 AND text_extracted=0."
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Process every eligible row regardless of site.",
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
        help="Print rows that would be processed without extracting.",
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to libertree.db (default: data/libertree.db).",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()

    # Logging
    log_dir = PROJECT_ROOT / "data" / "audit" / "logs"
    logger = setup_logging(log_dir)

    # DB
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "data" / "libertree.db"
    logger.info("DB: %s", db_path)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    db_lock = threading.Lock()

    # Build query
    base_query = (
        "SELECT seq_id, site_id FROM documents "
        "WHERE pdf_downloaded=1 AND text_extracted=0"
    )
    params: list = []

    if not args.all:
        site_ids = [s.strip() for s in args.site_ids.split(",") if s.strip()]
        placeholders = ",".join("?" * len(site_ids))
        base_query += f" AND site_id IN ({placeholders})"
        params.extend(site_ids)

    base_query += " ORDER BY site_id, seq_id"

    rows = conn.execute(base_query, params).fetchall()
    rows = [dict(r) for r in rows]

    if args.limit is not None:
        rows = rows[: args.limit]

    logger.info(
        "Found %d rows to process (limit=%s, dry_run=%s, workers=%d)",
        len(rows),
        args.limit,
        args.dry_run,
        args.max_workers,
    )

    if not rows:
        logger.info("Nothing to do.")
        conn.close()
        return

    # Per-site stats + counts
    site_stats: Dict[str, SiteStats] = {}
    site_counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        sid = r["site_id"]
        site_counts[sid] += 1
        if sid not in site_stats:
            site_stats[sid] = SiteStats()

    # Execute
    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(
                    process_row,
                    r["seq_id"],
                    r["site_id"],
                    conn,
                    db_lock,
                    site_stats[r["site_id"]],
                    logger,
                    args.dry_run,
                ): r
                for r in rows
            }
            for fut in as_completed(futures):
                row = futures[fut]
                sid = row["site_id"]
                try:
                    fut.result()
                except Exception as exc:
                    site_stats[sid].record_fail(f"future: {exc}")
                    logger.error(
                        "[%s] %s FAIL unhandled future: %s",
                        sid,
                        row["seq_id"],
                        exc,
                    )
                maybe_log_progress(sid, site_stats[sid], site_counts[sid], logger)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user — flushing progress...")

    print_summary_table(site_stats, logger)
    conn.close()


if __name__ == "__main__":
    main()
