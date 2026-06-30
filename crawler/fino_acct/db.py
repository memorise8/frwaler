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
        CREATE TABLE IF NOT EXISTS acct_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_priority INTEGER NOT NULL,
            agency TEXT NOT NULL,
            target_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_subtype TEXT NOT NULL,
            index_name TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            detail_url TEXT NOT NULL,
            published_date TEXT NOT NULL,
            body_text TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_priority, external_id)
        );

        CREATE TABLE IF NOT EXISTS acct_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES acct_documents(id) ON DELETE CASCADE,
            url TEXT NOT NULL,
            filename TEXT NOT NULL,
            local_path TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'downloaded',
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(document_id, url)
        );

        CREATE INDEX IF NOT EXISTS idx_acct_documents_source_type
            ON acct_documents(source_type, source_subtype);
        CREATE INDEX IF NOT EXISTS idx_acct_attachments_document_id
            ON acct_attachments(document_id);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source_priority: int,
    agency: str,
    target_name: str,
    source_url: str,
    source_type: str,
    source_subtype: str,
    index_name: str,
    external_id: str,
    title: str,
    detail_url: str,
    published_date: str,
    body_text: str,
) -> int:
    conn.execute(
        """
        INSERT INTO acct_documents (
            source_priority, agency, target_name, source_url, source_type,
            source_subtype, index_name, external_id, title, detail_url,
            published_date, body_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_priority, external_id) DO UPDATE SET
            agency = excluded.agency,
            target_name = excluded.target_name,
            source_url = excluded.source_url,
            source_type = excluded.source_type,
            source_subtype = excluded.source_subtype,
            index_name = excluded.index_name,
            title = excluded.title,
            detail_url = excluded.detail_url,
            published_date = excluded.published_date,
            body_text = excluded.body_text,
            fetched_at = CURRENT_TIMESTAMP
        """,
        (
            source_priority,
            agency,
            target_name,
            source_url,
            source_type,
            source_subtype,
            index_name,
            external_id,
            title,
            detail_url,
            published_date,
            body_text,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM acct_documents WHERE source_priority = ? AND external_id = ?",
        (source_priority, external_id),
    ).fetchone()
    return int(row["id"])


def upsert_attachment(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    url: str,
    filename: str,
    local_path: str,
    content_type: str,
    size_bytes: int,
    status: str = "downloaded",
) -> None:
    conn.execute(
        """
        INSERT INTO acct_attachments (
            document_id, url, filename, local_path, content_type, size_bytes, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, url) DO UPDATE SET
            filename = excluded.filename,
            local_path = excluded.local_path,
            content_type = excluded.content_type,
            size_bytes = excluded.size_bytes,
            status = excluded.status,
            fetched_at = CURRENT_TIMESTAMP
        """,
        (document_id, url, filename, local_path, content_type, size_bytes, status),
    )
    conn.commit()
