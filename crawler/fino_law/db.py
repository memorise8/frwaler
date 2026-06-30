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
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            org TEXT NOT NULL,
            promulgated_at TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            version_code TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_kind, external_id)
        );

        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            article_no TEXT NOT NULL,
            article_title TEXT NOT NULL,
            body_text TEXT NOT NULL,
            clause_json TEXT NOT NULL DEFAULT '[]',
            source_url TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(document_id, article_no, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(source_kind);
        CREATE INDEX IF NOT EXISTS idx_articles_document_id ON articles(document_id);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    external_id: str,
    title: str,
    category: str,
    org: str,
    promulgated_at: str,
    effective_at: str,
    version_code: str,
    source_url: str,
) -> int:
    conn.execute(
        """
        INSERT INTO documents (
            source_kind, external_id, title, category, org,
            promulgated_at, effective_at, version_code, source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, external_id) DO UPDATE SET
            title = excluded.title,
            category = excluded.category,
            org = excluded.org,
            promulgated_at = excluded.promulgated_at,
            effective_at = excluded.effective_at,
            version_code = excluded.version_code,
            source_url = excluded.source_url,
            collected_at = CURRENT_TIMESTAMP
        """,
        (source_kind, external_id, title, category, org,
         promulgated_at, effective_at, version_code, source_url),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM documents WHERE source_kind = ? AND external_id = ?",
        (source_kind, external_id),
    ).fetchone()
    return int(row["id"])


def upsert_article(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    article_no: str,
    article_title: str,
    body_text: str,
    clause_json: str,
    source_url: str,
    seq: int,
) -> None:
    conn.execute(
        """
        INSERT INTO articles (
            document_id, article_no, article_title, body_text, clause_json, source_url, seq
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, article_no, seq) DO UPDATE SET
            article_title = excluded.article_title,
            body_text = excluded.body_text,
            clause_json = excluded.clause_json,
            source_url = excluded.source_url
        """,
        (document_id, article_no, article_title, body_text, clause_json, source_url, seq),
    )
    conn.commit()
