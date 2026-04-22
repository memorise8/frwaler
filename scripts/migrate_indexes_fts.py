#!/usr/bin/env python3
"""
Idempotent SQLite migration: adds indexes + FTS5 virtual table to papers.db.
Safe to run multiple times — skips anything already present.

Fixes vs v1:
- Added composite index (site_id, json_extract(metadata,'$.documentTypeName'))
  which is what the GROUP BY dashboard query actually needs.
- Switched FTS tokenizer to 'trigram' (SQLite 3.34+) which correctly handles
  Korean/CJK characters (unicode61 treats them as separators, not tokens).
- Detects stale FTS built with wrong tokenizer and rebuilds it.
"""

import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "papers.db")
DB_PATH = os.path.abspath(DB_PATH)

# Expected tokenizer — if existing table used a different one we rebuild.
EXPECTED_TOKENIZER = "trigram"


def fmt(ms: float) -> str:
    return f"{ms:.0f}ms"


def step(label: str, fn, *args):
    t0 = time.time()
    result = fn(*args)
    elapsed = (time.time() - t0) * 1000
    print(f"  [{fmt(elapsed):>8}] {label}")
    return result


def get_fts_tokenizer(conn) -> str:
    """Return the tokenizer string from papers_fts schema, or '' if not found."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    if not row or not row[0]:
        return ""
    sql = row[0].lower()
    if "trigram" in sql:
        return "trigram"
    if "unicode61" in sql:
        return "unicode61"
    return "unknown"


def drop_fts_and_triggers(conn):
    """Drop FTS virtual table and its sync triggers so we can recreate them."""
    for trig in ("papers_ai", "papers_ad", "papers_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("DROP TABLE IF EXISTS papers_fts")


def main():
    t_total = time.time()

    # 1. Backup
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    bak = f"{DB_PATH}.bak-{ts}"
    print(f"[backup] Copying {DB_PATH}")
    print(f"         -> {bak}")
    t0 = time.time()
    shutil.copy2(DB_PATH, bak)
    print(f"  [{fmt((time.time()-t0)*1000):>8}] backup done")

    # 2. Connect with long timeout so we wait for crawlers to yield the write lock
    print(f"\n[connect] Opening DB with timeout=60s ...")
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        # 3. Single transaction — BEGIN IMMEDIATE to grab write lock up front
        print("\n[migrate] BEGIN IMMEDIATE ...")
        conn.execute("BEGIN IMMEDIATE")

        # ── Indexes ──────────────────────────────────────────────────────────

        indexes = [
            (
                "idx_papers_doc_type",
                "CREATE INDEX IF NOT EXISTS idx_papers_doc_type "
                "ON papers(json_extract(metadata, '$.documentTypeName'))",
            ),
            (
                # Composite: covers the dashboard GROUP BY query
                # SELECT json_extract(metadata,'$.documentTypeName'), COUNT(*)
                # FROM papers WHERE site_id IN (...) GROUP BY 1
                "idx_papers_site_doctype",
                "CREATE INDEX IF NOT EXISTS idx_papers_site_doctype "
                "ON papers(site_id, json_extract(metadata, '$.documentTypeName'))",
            ),
            (
                "idx_papers_crawled_at",
                "CREATE INDEX IF NOT EXISTS idx_papers_crawled_at "
                "ON papers(crawled_at)",
            ),
            (
                "idx_papers_category",
                "CREATE INDEX IF NOT EXISTS idx_papers_category "
                "ON papers(category)",
            ),
            (
                "idx_papers_pub_date",
                "CREATE INDEX IF NOT EXISTS idx_papers_pub_date "
                "ON papers(published_date)",
            ),
            (
                "idx_papers_site_pubdate",
                "CREATE INDEX IF NOT EXISTS idx_papers_site_pubdate "
                "ON papers(site_id, published_date DESC)",
            ),
        ]

        print("\n[indexes]")
        existing_indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

        for name, ddl in indexes:
            if name in existing_indexes:
                print(f"  [skip] {name} already exists")
            else:
                step(f"CREATE {name}", conn.execute, ddl)

        # ── FTS5 virtual table ────────────────────────────────────────────────

        print("\n[fts5]")
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        fts_exists = "papers_fts" in existing_tables
        if fts_exists:
            current_tok = get_fts_tokenizer(conn)
            if current_tok != EXPECTED_TOKENIZER:
                print(
                    f"  [rebuild] papers_fts exists but uses tokenizer={current_tok!r},"
                    f" need {EXPECTED_TOKENIZER!r} for Korean CJK — dropping and recreating"
                )
                step("DROP old papers_fts + triggers", drop_fts_and_triggers, conn)
                fts_exists = False
            else:
                print(f"  [skip] papers_fts already exists with tokenizer={current_tok!r}")

        if not fts_exists:
            step(
                f"CREATE VIRTUAL TABLE papers_fts (tokenize={EXPECTED_TOKENIZER})",
                conn.execute,
                f"""CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
                    title, abstract, metadata,
                    content='papers', content_rowid='rowid',
                    tokenize='{EXPECTED_TOKENIZER}'
                )""",
            )

        # ── Triggers ─────────────────────────────────────────────────────────

        print("\n[triggers]")
        existing_triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }

        trigger_defs = [
            (
                "papers_ai",
                """CREATE TRIGGER IF NOT EXISTS papers_ai
                   AFTER INSERT ON papers BEGIN
                     INSERT INTO papers_fts(rowid, title, abstract, metadata)
                       VALUES (new.rowid, new.title, new.abstract, new.metadata);
                   END""",
            ),
            (
                "papers_ad",
                """CREATE TRIGGER IF NOT EXISTS papers_ad
                   AFTER DELETE ON papers BEGIN
                     INSERT INTO papers_fts(papers_fts, rowid, title, abstract, metadata)
                       VALUES ('delete', old.rowid, old.title, old.abstract, old.metadata);
                   END""",
            ),
            (
                "papers_au",
                """CREATE TRIGGER IF NOT EXISTS papers_au
                   AFTER UPDATE ON papers BEGIN
                     INSERT INTO papers_fts(papers_fts, rowid, title, abstract, metadata)
                       VALUES ('delete', old.rowid, old.title, old.abstract, old.metadata);
                     INSERT INTO papers_fts(rowid, title, abstract, metadata)
                       VALUES (new.rowid, new.title, new.abstract, new.metadata);
                   END""",
            ),
        ]

        for name, ddl in trigger_defs:
            if name in existing_triggers:
                print(f"  [skip] trigger {name} already exists")
            else:
                step(f"CREATE TRIGGER {name}", conn.execute, ddl)

        # ── FTS backfill ──────────────────────────────────────────────────────

        print("\n[backfill]")
        fts_count = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
        papers_count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        print(f"  papers={papers_count:,}  papers_fts={fts_count:,}")

        if fts_count >= papers_count:
            print("  [skip] FTS already fully populated")
        else:
            missing = papers_count - fts_count
            print(f"  Backfilling ~{missing:,} rows ...")
            step(
                f"INSERT INTO papers_fts ... ({missing:,} rows)",
                conn.execute,
                """INSERT INTO papers_fts(rowid, title, abstract, metadata)
                   SELECT rowid, title, abstract, metadata FROM papers
                   WHERE rowid NOT IN (SELECT rowid FROM papers_fts)""",
            )

        # ── ANALYZE ───────────────────────────────────────────────────────────

        print("\n[analyze]")
        step("ANALYZE", conn.execute, "ANALYZE")

        # ── COMMIT ────────────────────────────────────────────────────────────

        conn.execute("COMMIT")
        print("\n[commit] Transaction committed successfully.")

    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        try:
            conn.execute("ROLLBACK")
            print("[rollback] Transaction rolled back.", file=sys.stderr)
        except Exception:
            pass
        conn.close()
        sys.exit(1)

    conn.close()

    total_ms = (time.time() - t_total) * 1000
    index_names = [n for n, _ in indexes]
    print(f"\n{'='*60}")
    print(f"Migration complete in {total_ms/1000:.1f}s")
    print(f"Backup: {bak}")
    print(f"Indexes: {index_names}")
    print(f"FTS5 table: papers_fts (tokenize={EXPECTED_TOKENIZER})")
    print(f"Triggers: papers_ai, papers_ad, papers_au")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
