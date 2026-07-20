#!/usr/bin/env python3
"""libertree documents_fts migration — idempotent FTS5 over the documents table.

Mirrors the existing ``papers_fts`` setup (`scripts/migrate_indexes_fts.py`)
but indexes the new libertree ``documents`` table:

- title, abstract, summary, keywords
- tokenize='trigram' (correct for Korean/CJK; unicode61 splits Hangul)
- AFTER INSERT / UPDATE / DELETE triggers keep the index in sync
- Initial backfill picks up any rows that pre-date the FTS table
- ``--no-backup`` skips the ``data/papers.db.bak-YYYYMMDD-HHMM`` snapshot

Safe to re-run: each step probes ``sqlite_master`` first.

Usage:
    python -m scripts.migrate_documents_fts [--db PATH] [--no-backup]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "papers.db"

EXPECTED_TOKENIZER = "trigram"


def _fmt(ms: float) -> str:
    return f"{ms:.0f}ms"


def _step(label: str, fn, *args, **kw):
    t0 = time.time()
    out = fn(*args, **kw)
    print(f"  [{_fmt((time.time() - t0) * 1000):>8}] {label}")
    return out


def _get_fts_tokenizer(conn) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()
    if not row or not row[0]:
        return ""
    sql = row[0].lower()
    if "trigram" in sql:
        return "trigram"
    if "unicode61" in sql:
        return "unicode61"
    return "unknown"


def _drop_fts_and_triggers(conn):
    for trig in ("documents_ai", "documents_ad", "documents_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("DROP TABLE IF EXISTS documents_fts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[error] DB not found: {db_path}", file=sys.stderr)
        return 1

    t_total = time.time()

    # 1. Backup
    if not args.no_backup:
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        bak = db_path.with_name(db_path.name + f".bak-{ts}")
        print(f"[backup] {db_path}\n         -> {bak}")
        t0 = time.time()
        shutil.copy2(db_path, bak)
        print(f"  [{_fmt((time.time() - t0) * 1000):>8}] backup done")
    else:
        print("[backup] skipped (--no-backup)")

    # 2. Connect
    print(f"\n[connect] {db_path} (timeout=60s)")
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    try:
        # documents table must exist (libertree migration prerequisite)
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone():
            print(
                "[error] 'documents' table missing. Run "
                "`python -m scripts.migrate_libertree` first.",
                file=sys.stderr,
            )
            return 1

        print("\n[migrate] BEGIN IMMEDIATE")
        conn.execute("BEGIN IMMEDIATE")

        # ── FTS5 virtual table ───────────────────────────────────────────────
        print("\n[fts5]")
        existing_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        fts_exists = "documents_fts" in existing_tables
        if fts_exists:
            current_tok = _get_fts_tokenizer(conn)
            if current_tok != EXPECTED_TOKENIZER:
                print(
                    f"  [rebuild] documents_fts uses tokenizer={current_tok!r}, "
                    f"need {EXPECTED_TOKENIZER!r} — dropping and recreating"
                )
                _step("DROP old documents_fts + triggers", _drop_fts_and_triggers, conn)
                fts_exists = False
            else:
                print(f"  [skip] documents_fts exists with tokenizer={current_tok!r}")

        if not fts_exists:
            _step(
                f"CREATE VIRTUAL TABLE documents_fts (tokenize={EXPECTED_TOKENIZER})",
                conn.execute,
                f"""CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    title, abstract, summary, keywords,
                    content='documents', content_rowid='rowid',
                    tokenize='{EXPECTED_TOKENIZER}'
                )""",
            )

        # ── Triggers ─────────────────────────────────────────────────────────
        print("\n[triggers]")
        existing_triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }

        trigger_defs = [
            (
                "documents_ai",
                """CREATE TRIGGER IF NOT EXISTS documents_ai
                   AFTER INSERT ON documents BEGIN
                     INSERT INTO documents_fts(rowid, title, abstract, summary, keywords)
                       VALUES (new.rowid, new.title, new.abstract, new.summary, new.keywords);
                   END""",
            ),
            (
                "documents_ad",
                """CREATE TRIGGER IF NOT EXISTS documents_ad
                   AFTER DELETE ON documents BEGIN
                     INSERT INTO documents_fts(documents_fts, rowid, title, abstract, summary, keywords)
                       VALUES ('delete', old.rowid, old.title, old.abstract, old.summary, old.keywords);
                   END""",
            ),
            (
                "documents_au",
                """CREATE TRIGGER IF NOT EXISTS documents_au
                   AFTER UPDATE ON documents BEGIN
                     INSERT INTO documents_fts(documents_fts, rowid, title, abstract, summary, keywords)
                       VALUES ('delete', old.rowid, old.title, old.abstract, old.summary, old.keywords);
                     INSERT INTO documents_fts(rowid, title, abstract, summary, keywords)
                       VALUES (new.rowid, new.title, new.abstract, new.summary, new.keywords);
                   END""",
            ),
        ]
        for name, ddl in trigger_defs:
            if name in existing_triggers:
                print(f"  [skip] trigger {name} already exists")
            else:
                _step(f"CREATE TRIGGER {name}", conn.execute, ddl)

        # ── Backfill ────────────────────────────────────────────────────────
        # External-content FTS5 needs the special ``rebuild`` command to
        # actually populate the index from the source table — a plain
        # INSERT INTO fts(rowid, col, …) SELECT … is treated as a "tell me
        # what's at this rowid" notification, not an indexing operation,
        # so MATCH would silently return no rows. Always run ``rebuild``;
        # it's idempotent and proportional to row count.
        print("\n[backfill]")
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"  documents={doc_count:,} → running FTS rebuild")
        _step(
            "INSERT INTO documents_fts(documents_fts) VALUES('rebuild')",
            conn.execute,
            "INSERT INTO documents_fts(documents_fts) VALUES('rebuild')",
        )
        fts_count = conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
        print(f"  documents_fts={fts_count:,} (after rebuild)")

        print("\n[analyze]")
        _step("ANALYZE", conn.execute, "ANALYZE")

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
        return 1

    conn.close()
    print(f"\n{'=' * 60}")
    print(f"documents_fts migration complete in {(time.time() - t_total):.1f}s")
    print(f"Tokenizer: {EXPECTED_TOKENIZER}")
    print(f"Columns:   title, abstract, summary, keywords")
    print(f"Triggers:  documents_ai, documents_ad, documents_au")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
