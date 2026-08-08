# -*- coding: utf-8 -*-
"""libertree Postgres 백엔드 — crawler/db_libertree.py 의 Postgres 미러.

공개 함수 시그니처는 db_libertree 와 동일하다. 크롤러 코드는
crawler/db_backend.py 를 통해 이 모듈 또는 db_libertree 를 선택해 쓴다.
dedup 키: (site_id, post_number, meta_url) UNIQUE.
"""
from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

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


def open_db(dsn: str) -> psycopg.Connection:
    """Open a Postgres connection with dict rows (mirrors sqlite3.Row)."""
    return psycopg.connect(dsn, row_factory=dict_row)


def init_db(conn: psycopg.Connection) -> None:
    """Create tables/indexes if absent (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            site_id     TEXT PRIMARY KEY,
            site_name   TEXT NOT NULL,
            site_url    TEXT NOT NULL,
            sheet       TEXT,
            created_at  TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            seq_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            collected_at      TIMESTAMPTZ DEFAULT now(),
            site_id           TEXT NOT NULL REFERENCES sites(site_id),
            post_number       TEXT,
            meta_url          TEXT NOT NULL,
            title             TEXT NOT NULL,
            published_date    TEXT,
            listed_date       TEXT,
            authors           TEXT,
            publisher         TEXT,
            journal           TEXT,
            pdf_url           TEXT,
            keywords          TEXT,
            abstract          TEXT,
            original_filename TEXT,
            pdf_downloaded    INTEGER DEFAULT 0,
            text_extracted    INTEGER DEFAULT 0,
            pdf_size_bytes    BIGINT,
            pdf_sha256        TEXT,
            summary           TEXT,
            summary_model     TEXT,
            summary_at        TIMESTAMPTZ
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_site_post ON documents(site_id, post_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_meta_url  ON documents(meta_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_collected ON documents(collected_at)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_dedup ON documents(site_id, post_number, meta_url)"
    )
    conn.commit()


def upsert_site(conn, site_id, site_name, site_url, sheet: Optional[str] = None) -> None:
    conn.execute(
        """
        INSERT INTO sites (site_id, site_name, site_url, sheet)
        VALUES (%(site_id)s, %(site_name)s, %(site_url)s, %(sheet)s)
        ON CONFLICT (site_id) DO UPDATE SET
            site_name = EXCLUDED.site_name,
            site_url  = EXCLUDED.site_url,
            sheet     = COALESCE(EXCLUDED.sheet, sites.sheet)
        """,
        {"site_id": site_id, "site_name": site_name, "site_url": site_url, "sheet": sheet},
    )
    conn.commit()


def find_by_dedup_key(conn, site_id, post_number, meta_url) -> Optional[int]:
    if post_number is None:
        row = conn.execute(
            "SELECT seq_id FROM documents WHERE site_id=%s AND post_number IS NULL AND meta_url=%s LIMIT 1",
            (site_id, meta_url),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT seq_id FROM documents WHERE site_id=%s AND post_number=%s AND meta_url=%s LIMIT 1",
            (site_id, post_number, meta_url),
        ).fetchone()
    return None if row is None else row["seq_id"]


def insert_document(conn, doc: dict) -> int:
    if not doc.get("site_id"):
        raise ValueError("insert_document: site_id is required")
    if not doc.get("meta_url"):
        raise ValueError("insert_document: meta_url is required")
    if not doc.get("title"):
        raise ValueError("insert_document: title is required")

    existing = find_by_dedup_key(conn, doc["site_id"], doc.get("post_number"), doc["meta_url"])
    if existing is not None:
        return existing

    payload = {k: doc.get(k) for k in DOCUMENT_INSERT_FIELDS}
    payload["pdf_downloaded"] = int(payload.get("pdf_downloaded") or 0)
    payload["text_extracted"] = int(payload.get("text_extracted") or 0)

    cols = ", ".join(DOCUMENT_INSERT_FIELDS)
    vals = ", ".join("%(" + f + ")s" for f in DOCUMENT_INSERT_FIELDS)
    row = conn.execute(
        f"INSERT INTO documents ({cols}) VALUES ({vals}) RETURNING seq_id",
        payload,
    ).fetchone()
    conn.commit()
    return row["seq_id"]


def update_document_pdf(conn, seq_id, *, downloaded, size_bytes, sha256) -> None:
    conn.execute(
        "UPDATE documents SET pdf_downloaded=%s, pdf_size_bytes=%s, pdf_sha256=%s WHERE seq_id=%s",
        (1 if downloaded else 0, int(size_bytes or 0), sha256 or None, seq_id),
    )
    conn.commit()


def update_document_text(conn, seq_id, *, extracted) -> None:
    conn.execute(
        "UPDATE documents SET text_extracted=%s WHERE seq_id=%s",
        (1 if extracted else 0, seq_id),
    )
    conn.commit()


def update_document_summary(conn, seq_id, summary, model) -> None:
    conn.execute(
        "UPDATE documents SET summary=%s, summary_model=%s, summary_at=now() WHERE seq_id=%s",
        (summary, model, seq_id),
    )
    conn.commit()


def get_max_post_number(conn, site_id) -> Optional[str]:
    row = conn.execute(
        """
        SELECT post_number FROM documents
         WHERE site_id=%s AND post_number IS NOT NULL AND post_number <> ''
         ORDER BY post_number DESC LIMIT 1
        """,
        (site_id,),
    ).fetchone()
    return None if row is None else row["post_number"]


def get_last_meta_url(conn, site_id) -> Optional[str]:
    row = conn.execute(
        "SELECT meta_url FROM documents WHERE site_id=%s ORDER BY collected_at DESC, seq_id DESC LIMIT 1",
        (site_id,),
    ).fetchone()
    return None if row is None else row["meta_url"]


def get_document(conn, seq_id) -> Optional[Any]:
    return conn.execute("SELECT * FROM documents WHERE seq_id=%s", (seq_id,)).fetchone()
