from pathlib import Path
import sqlite3

from .models import ParagraphRecord


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
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            std_num INTEGER NOT NULL UNIQUE,
            std_type TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS paragraphs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            para_num TEXT NOT NULL,
            section_path TEXT NOT NULL,
            body_html TEXT NOT NULL,
            body_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(document_id, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_std_documents_type ON documents(std_type);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_doc ON paragraphs(document_id);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_num ON paragraphs(para_num);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection, *, std_num: int, std_type: str, title: str, source_url: str
) -> int:
    conn.execute(
        """
        INSERT INTO documents (std_num, std_type, title, source_url)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(std_num) DO UPDATE SET
            std_type = excluded.std_type,
            title = excluded.title,
            source_url = excluded.source_url,
            collected_at = CURRENT_TIMESTAMP
        """,
        (std_num, std_type, title, source_url),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM documents WHERE std_num = ?", (std_num,)).fetchone()
    return int(row["id"])


def replace_paragraphs(
    conn: sqlite3.Connection, document_id: int, records: list[ParagraphRecord]
) -> int:
    with conn:  # 단일 트랜잭션: 삭제+삽입
        conn.execute("DELETE FROM paragraphs WHERE document_id = ?", (document_id,))
        conn.executemany(
            """
            INSERT INTO paragraphs (
                document_id, para_num, section_path, body_html, body_text, source_url, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(document_id, r.para_num, r.section_path, r.body_html, r.body_text,
              r.source_url, r.seq) for r in records],
        )
    return len(records)
