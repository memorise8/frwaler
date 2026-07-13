from __future__ import annotations

from pathlib import Path
import sqlite3


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nts_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seq INTEGER, tax_category TEXT, summary TEXT,
            revision_reason TEXT, registered_at TEXT,
            source_file TEXT, loaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS nts_revision_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER REFERENCES nts_revisions(id) ON DELETE CASCADE,
            role TEXT, case_number TEXT, case_date TEXT, matched_doc_id TEXT
        );
        CREATE TABLE IF NOT EXISTS nts_excluded_docs (
            external_id TEXT PRIMARY KEY,
            revision_id INTEGER REFERENCES nts_revisions(id) ON DELETE CASCADE,
            doc_number TEXT, title TEXT, revision_reason TEXT, registered_at TEXT,
            excluded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_rev_source ON nts_revisions(source_file);
        CREATE INDEX IF NOT EXISTS idx_case_rev ON nts_revision_cases(revision_id);
        """
    )
    conn.commit()


def delete_by_source(conn: sqlite3.Connection, source_file: str) -> None:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM nts_revisions WHERE source_file = ?", (source_file,))]
    if ids:
        qs = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM nts_excluded_docs WHERE revision_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM nts_revision_cases WHERE revision_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM nts_revisions WHERE id IN ({qs})", ids)
    conn.commit()


def insert_revision(conn: sqlite3.Connection, *, seq: int, tax_category: str, summary: str,
                    revision_reason: str, registered_at: str, source_file: str) -> int:
    cur = conn.execute(
        """INSERT INTO nts_revisions (seq, tax_category, summary, revision_reason,
             registered_at, source_file) VALUES (?, ?, ?, ?, ?, ?)""",
        (seq, tax_category, summary, revision_reason, registered_at, source_file),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_case(conn: sqlite3.Connection, *, revision_id: int, role: str,
                case_number: str, case_date: str, matched_doc_id: str | None) -> None:
    conn.execute(
        """INSERT INTO nts_revision_cases (revision_id, role, case_number, case_date, matched_doc_id)
           VALUES (?, ?, ?, ?, ?)""",
        (revision_id, role, case_number, case_date, matched_doc_id),
    )
    conn.commit()


def upsert_excluded(conn: sqlite3.Connection, *, external_id: str, revision_id: int,
                    doc_number: str, title: str, revision_reason: str, registered_at: str) -> None:
    conn.execute(
        """INSERT INTO nts_excluded_docs (external_id, revision_id, doc_number, title,
             revision_reason, registered_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(external_id) DO UPDATE SET revision_id=excluded.revision_id,
             doc_number=excluded.doc_number, title=excluded.title,
             revision_reason=excluded.revision_reason, registered_at=excluded.registered_at,
             excluded_at=datetime('now','localtime')""",
        (external_id, revision_id, doc_number, title, revision_reason, registered_at),
    )
    conn.commit()


def iter_excluded(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT external_id, doc_number, title, revision_reason, registered_at "
        "FROM nts_excluded_docs ORDER BY external_id").fetchall()


def iter_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM nts_revisions ORDER BY seq DESC").fetchall()


def count_excluded(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM nts_excluded_docs").fetchone()[0])
