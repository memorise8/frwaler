# -*- coding: utf-8 -*-
"""libertree.db — 별도 SQLite DB (data/libertree.db).

기존 ``crawler/db.py`` (papers.db) 와 독립적으로 운영된다.
스키마는 사용자 명세를 따른다:

- ``sites(site_id PK, site_name, site_url, sheet, created_at)``
- ``documents(seq_id PK AUTOINCREMENT, ...)`` (1조까지 안전한 INTEGER 시퀀스)

dedup 키: ``(site_id, post_number, meta_url)`` UNIQUE.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "libertree-app" / "data" / "libertree.db"


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and ensure-init) connection to libertree.db.

    `init_db` 는 호출자가 명시적으로 부르는 것이 원칙이지만, 안전하게
    매 open 시점에 idempotent 하게 schema 적용을 보장한다.
    """
    db_path = Path(path) if path is not None else DB_PATH
    db_path = Path(os.path.abspath(db_path))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if not present (idempotent)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sites (
            site_id     TEXT PRIMARY KEY,
            site_name   TEXT NOT NULL,
            site_url    TEXT NOT NULL,
            sheet       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS documents (
            -- 관리용
            seq_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            -- 사이트
            site_id           TEXT NOT NULL,
            post_number       TEXT,
            meta_url          TEXT NOT NULL,

            -- 문서정보
            title             TEXT NOT NULL,
            published_date    TEXT,
            listed_date       TEXT,
            authors           TEXT,
            publisher         TEXT,
            journal           TEXT,
            pdf_url           TEXT,
            keywords          TEXT,
            abstract          TEXT,

            -- 파일
            original_filename TEXT,
            pdf_downloaded    INTEGER DEFAULT 0,
            text_extracted    INTEGER DEFAULT 0,
            pdf_size_bytes    INTEGER,
            pdf_sha256        TEXT,

            -- 요약
            summary           TEXT,
            summary_model     TEXT,
            summary_at        TIMESTAMP,

            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        );

        CREATE INDEX IF NOT EXISTS idx_doc_site_post  ON documents(site_id, post_number);
        CREATE INDEX IF NOT EXISTS idx_doc_meta_url   ON documents(meta_url);
        CREATE INDEX IF NOT EXISTS idx_doc_collected  ON documents(collected_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_dedup
            ON documents(site_id, post_number, meta_url);
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# sites
# ---------------------------------------------------------------------------


def upsert_site(
    conn: sqlite3.Connection,
    site_id: str,
    site_name: str,
    site_url: str,
    sheet: Optional[str] = None,
) -> None:
    """Insert or update a site row."""
    conn.execute(
        """
        INSERT INTO sites (site_id, site_name, site_url, sheet)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(site_id) DO UPDATE SET
            site_name = excluded.site_name,
            site_url  = excluded.site_url,
            sheet     = COALESCE(excluded.sheet, sheet)
        """,
        (site_id, site_name, site_url, sheet),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

DOCUMENT_INSERT_FIELDS = (
    "site_id", "post_number", "meta_url",
    "title", "published_date", "listed_date",
    "authors", "publisher", "journal",
    "pdf_url", "keywords", "abstract",
    "original_filename",
    "pdf_downloaded", "text_extracted",
    "pdf_size_bytes", "pdf_sha256",
    "summary", "summary_model", "summary_at",
)


def insert_document(conn: sqlite3.Connection, doc: dict) -> int:
    """Insert a document row and return the assigned seq_id.

    Required keys: ``site_id``, ``meta_url``, ``title``.
    Optional keys: any of DOCUMENT_INSERT_FIELDS.
    Defaults: ``pdf_downloaded=0``, ``text_extracted=0``.

    If the (site_id, post_number, meta_url) dedup key already exists,
    the existing seq_id is returned without any UPDATE — caller can use
    :func:`find_by_dedup_key` first to avoid duplicate work.
    """
    if not doc.get("site_id"):
        raise ValueError("insert_document: site_id is required")
    if not doc.get("meta_url"):
        raise ValueError("insert_document: meta_url is required")
    if not doc.get("title"):
        raise ValueError("insert_document: title is required")

    existing = find_by_dedup_key(
        conn,
        doc["site_id"],
        doc.get("post_number"),
        doc["meta_url"],
    )
    if existing is not None:
        return existing

    payload = {k: doc.get(k) for k in DOCUMENT_INSERT_FIELDS}
    payload["pdf_downloaded"] = int(payload.get("pdf_downloaded") or 0)
    payload["text_extracted"] = int(payload.get("text_extracted") or 0)

    cur = conn.execute(
        f"""
        INSERT INTO documents
            ({", ".join(DOCUMENT_INSERT_FIELDS)})
        VALUES
            ({", ".join(":" + f for f in DOCUMENT_INSERT_FIELDS)})
        """,
        payload,
    )
    conn.commit()
    return cur.lastrowid


def update_document_pdf(
    conn: sqlite3.Connection,
    seq_id: int,
    *,
    downloaded: bool,
    size_bytes: int,
    sha256: str,
) -> None:
    """Mark PDF download outcome on the row."""
    conn.execute(
        """
        UPDATE documents
           SET pdf_downloaded = ?,
               pdf_size_bytes = ?,
               pdf_sha256     = ?
         WHERE seq_id = ?
        """,
        (1 if downloaded else 0, int(size_bytes or 0), sha256 or None, seq_id),
    )
    conn.commit()


def update_document_text(
    conn: sqlite3.Connection,
    seq_id: int,
    *,
    extracted: bool,
) -> None:
    """Mark text extraction outcome."""
    conn.execute(
        "UPDATE documents SET text_extracted = ? WHERE seq_id = ?",
        (1 if extracted else 0, seq_id),
    )
    conn.commit()


def update_document_summary(
    conn: sqlite3.Connection,
    seq_id: int,
    summary: str,
    model: str,
) -> None:
    """Persist LLM summary + the model that produced it (with timestamp)."""
    conn.execute(
        """
        UPDATE documents
           SET summary       = ?,
               summary_model = ?,
               summary_at    = CURRENT_TIMESTAMP
         WHERE seq_id = ?
        """,
        (summary, model, seq_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# query helpers
# ---------------------------------------------------------------------------


def get_max_post_number(conn: sqlite3.Connection, site_id: str) -> Optional[str]:
    """Return the max post_number observed for a site (string compare).

    Useful for incremental crawling — caller can stop scanning once it
    reaches a post_number ≤ this value.

    Note: post_number is TEXT so the result reflects lexicographic order.
    Numeric sites should pass zero-padded values to make this useful.
    """
    row = conn.execute(
        """
        SELECT post_number FROM documents
         WHERE site_id = ? AND post_number IS NOT NULL AND post_number != ''
         ORDER BY post_number DESC LIMIT 1
        """,
        (site_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row["post_number"]


def get_last_meta_url(conn: sqlite3.Connection, site_id: str) -> Optional[str]:
    """Return the most-recently-collected meta_url for a site."""
    row = conn.execute(
        """
        SELECT meta_url FROM documents
         WHERE site_id = ?
         ORDER BY collected_at DESC, seq_id DESC LIMIT 1
        """,
        (site_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row["meta_url"]


def find_by_dedup_key(
    conn: sqlite3.Connection,
    site_id: str,
    post_number: Optional[str],
    meta_url: str,
) -> Optional[int]:
    """Look up seq_id by (site_id, post_number, meta_url).

    Returns the seq_id if a row matches, else None. ``post_number`` may
    be None — in that case we only match rows where post_number IS NULL
    (SQLite UNIQUE treats NULL as distinct, so this is the practical
    semantic for "no post number known").
    """
    if post_number is None:
        row = conn.execute(
            """
            SELECT seq_id FROM documents
             WHERE site_id = ? AND post_number IS NULL AND meta_url = ?
             LIMIT 1
            """,
            (site_id, meta_url),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT seq_id FROM documents
             WHERE site_id = ? AND post_number = ? AND meta_url = ?
             LIMIT 1
            """,
            (site_id, post_number, meta_url),
        ).fetchone()
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row["seq_id"]


def get_document(conn: sqlite3.Connection, seq_id: int) -> Optional[Any]:
    """Fetch a single document row by seq_id."""
    return conn.execute(
        "SELECT * FROM documents WHERE seq_id = ?",
        (seq_id,),
    ).fetchone()
