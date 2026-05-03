# -*- coding: utf-8 -*-
"""SQLite database manager for crawler-poc."""

import sqlite3
import os

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'papers.db')


def get_db(db_path=None):
    """Return a connection to the SQLite database at db_path."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn):
    """Create tables if they do not exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            base_url    TEXT NOT NULL,
            last_crawled TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS papers (
            id              TEXT PRIMARY KEY,
            site_id         TEXT NOT NULL REFERENCES sites(id),
            external_id     TEXT,
            title           TEXT,
            authors         TEXT,       -- JSON array
            abstract        TEXT,
            category        TEXT,
            keywords        TEXT,       -- JSON array
            published_date  TEXT,
            url             TEXT,
            pdf_url         TEXT,
            doi             TEXT,
            department      TEXT,
            metadata        TEXT,       -- JSON object
            crawled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(site_id, external_id)
        );

        CREATE TABLE IF NOT EXISTS doc_index (
            site_id         TEXT NOT NULL,
            doc_id          TEXT NOT NULL,
            doc_number      TEXT,
            title           TEXT,
            doc_type        TEXT,
            category        TEXT,
            published_date  TEXT,
            last_altered_at TEXT,
            extra           TEXT,       -- JSON object for any additional list-API fields
            first_seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at      TIMESTAMP,
            PRIMARY KEY (site_id, doc_id)
        );

        CREATE INDEX IF NOT EXISTS idx_doc_index_site ON doc_index(site_id);
        CREATE INDEX IF NOT EXISTS idx_doc_index_last_seen ON doc_index(last_seen_at);

        -- livertree: 사이트 무관 글로벌 시퀀스 PK + 12자리 zero-pad 파일 매핑.
        -- 자세한 설명은 docs/livertree.md, crawler/storage.py 참고.
        CREATE TABLE IF NOT EXISTS documents (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            crawled_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            site_id           TEXT NOT NULL REFERENCES sites(id),
            external_id       TEXT,
            meta_url          TEXT,
            title             TEXT,
            published_date    TEXT,
            posted_date       TEXT,
            authors           TEXT,                  -- ; separated
            publisher         TEXT,                  -- ; separated
            journal           TEXT,
            pdf_url           TEXT,
            keywords          TEXT,                  -- , separated
            abstract          TEXT,
            original_filename TEXT,
            pdf_path          TEXT,
            txt_path          TEXT,
            download_status   TEXT,
            summary           TEXT,
            metadata          TEXT,                  -- JSON: site-specific raw fields
            UNIQUE(site_id, external_id)
        );

        CREATE INDEX IF NOT EXISTS idx_documents_site         ON documents(site_id);
        CREATE INDEX IF NOT EXISTS idx_documents_site_extid   ON documents(site_id, external_id);
        CREATE INDEX IF NOT EXISTS idx_documents_crawled_at   ON documents(crawled_at);
        CREATE INDEX IF NOT EXISTS idx_documents_pubdate      ON documents(published_date);
    """)
    conn.commit()
    for col in ["summary TEXT", "download_status TEXT", "download_path TEXT"]:
        try:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col}")
            conn.commit()
        except:
            pass


def upsert_paper(conn, paper_dict):
    """Insert or replace a paper record."""
    conn.execute("""
        INSERT OR REPLACE INTO papers
            (id, site_id, external_id, title, authors, abstract, category,
             keywords, published_date, url, pdf_url, doi, department, metadata)
        VALUES
            (:id, :site_id, :external_id, :title, :authors, :abstract, :category,
             :keywords, :published_date, :url, :pdf_url, :doi, :department, :metadata)
    """, paper_dict)
    conn.commit()


def register_site(conn, site_id, name, base_url):
    """Register a site; update name/base_url if it already exists."""
    conn.execute("""
        INSERT INTO sites (id, name, base_url)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url
    """, (site_id, name, base_url))
    conn.commit()


def update_summary(conn, paper_id, summary):
    """Update the summary field for a paper."""
    conn.execute("UPDATE papers SET summary = ? WHERE id = ?", (summary, paper_id))
    conn.commit()


def get_papers_without_summary(conn, site_id=None, limit=None):
    """Return papers that have no summary yet, optionally filtered by site."""
    query = "SELECT * FROM papers WHERE (summary IS NULL OR summary = '')"
    params = []
    if site_id is not None:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY crawled_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    cursor = conn.execute(query, params)
    return cursor.fetchall()


def update_download_status(conn, paper_id, status, path=None):
    """Update download_status and download_path for a paper."""
    conn.execute(
        "UPDATE papers SET download_status = ?, download_path = ? WHERE id = ?",
        (status, path, paper_id),
    )
    conn.commit()


def get_stats(conn):
    """Return a list of (site_id, site_name, paper_count) rows.

    Counts come from the livertree ``documents`` table — ``papers`` is no
    longer written to. The result-tuple field name is kept as ``paper_count``
    for backward compatibility with ``cmd_stats`` formatting.
    """
    cursor = conn.execute("""
        SELECT s.id, s.name, COUNT(d.id) AS paper_count
        FROM sites s
        LEFT JOIN documents d ON d.site_id = s.id
        GROUP BY s.id, s.name
        ORDER BY s.id
    """)
    return cursor.fetchall()


def upsert_doc_index(conn, site_id, doc_id, *, doc_number=None, title=None,
                     doc_type=None, category=None, published_date=None,
                     last_altered_at=None, extra=None):
    """Insert or update a doc_index entry. Updates ``last_seen_at`` on every call
    and clears ``deleted_at`` so re-appearing docs are revived. ``first_seen_at``
    is preserved on update.
    """
    conn.execute("""
        INSERT INTO doc_index
            (site_id, doc_id, doc_number, title, doc_type, category,
             published_date, last_altered_at, extra,
             first_seen_at, last_seen_at, deleted_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?,
             CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
        ON CONFLICT(site_id, doc_id) DO UPDATE SET
            doc_number       = COALESCE(excluded.doc_number, doc_number),
            title            = COALESCE(excluded.title, title),
            doc_type         = COALESCE(excluded.doc_type, doc_type),
            category         = COALESCE(excluded.category, category),
            published_date   = COALESCE(excluded.published_date, published_date),
            last_altered_at  = COALESCE(excluded.last_altered_at, last_altered_at),
            extra            = COALESCE(excluded.extra, extra),
            last_seen_at     = CURRENT_TIMESTAMP,
            deleted_at       = NULL
    """, (site_id, doc_id, doc_number, title, doc_type, category,
          published_date, last_altered_at, extra))


def mark_doc_index_deleted(conn, site_id, scan_started_at):
    """Mark entries whose ``last_seen_at`` is older than ``scan_started_at``
    as deleted (i.e., not observed in the most recent scan).
    Returns the number of rows marked.
    """
    cur = conn.execute("""
        UPDATE doc_index
           SET deleted_at = CURRENT_TIMESTAMP
         WHERE site_id = ?
           AND last_seen_at < ?
           AND deleted_at IS NULL
    """, (site_id, scan_started_at))
    conn.commit()
    return cur.rowcount


def get_doc_index_stats(conn, site_id=None):
    """Return per-site counts: total, active (not deleted), deleted."""
    query = """
        SELECT site_id,
               COUNT(*) AS total,
               SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted
          FROM doc_index
    """
    params = []
    if site_id:
        query += " WHERE site_id = ?"
        params.append(site_id)
    query += " GROUP BY site_id ORDER BY site_id"
    return conn.execute(query, params).fetchall()


# =====================================================================
# livertree: documents 테이블 (글로벌 INTEGER PK + 12자리 파일 매핑)
# =====================================================================

DOCUMENT_FIELDS = (
    "site_id", "external_id", "meta_url",
    "title", "published_date", "posted_date",
    "authors", "publisher", "journal",
    "pdf_url", "keywords", "abstract",
    "original_filename", "summary", "metadata",
)


def upsert_document(conn, doc_dict):
    """Insert or update a document row.

    Returns the integer ``id`` of the row.

    - Required key: ``site_id``.
    - If ``(site_id, external_id)`` already exists, the existing row is
      updated in place and its existing id is returned (preserves the
      filesystem layout the id maps to).
    - Otherwise a new row is INSERTed and the new ``lastrowid`` is returned.

    Filesystem-derived columns (``pdf_path``, ``txt_path``,
    ``download_status``) are NOT touched here — the caller should set
    them after the id is known. ``crawled_at`` defaults to CURRENT_TIMESTAMP
    on insert and is left alone on update.
    """
    if not doc_dict.get("site_id"):
        raise ValueError("upsert_document: site_id is required")

    site_id = doc_dict["site_id"]
    external_id = doc_dict.get("external_id")

    existing_id = None
    existing_pdf_url = None
    if external_id is not None:
        row = conn.execute(
            "SELECT id, pdf_url FROM documents WHERE site_id = ? AND external_id = ?",
            (site_id, external_id),
        ).fetchone()
        if row is not None:
            if hasattr(row, "keys"):
                existing_id = row["id"]
                existing_pdf_url = row["pdf_url"]
            else:
                existing_id = row[0]
                existing_pdf_url = row[1]

    payload = {k: doc_dict.get(k) for k in DOCUMENT_FIELDS}

    if existing_id is None:
        cur = conn.execute(
            f"""
            INSERT INTO documents
                ({", ".join(DOCUMENT_FIELDS)})
            VALUES
                ({", ".join(":" + f for f in DOCUMENT_FIELDS)})
            """,
            payload,
        )
        conn.commit()
        return cur.lastrowid

    # On UPDATE, never overwrite ``summary`` with NULL — LLM summaries are
    # expensive to regenerate and most crawlers don't carry them in
    # ``paper_dict``, so a routine recrawl would otherwise wipe them. Same
    # for ``metadata``: if the new payload doesn't supply it, keep what was
    # there (e.g. NTS metadata enrichment that ran in a separate step).
    _PRESERVE_IF_NULL = {"summary", "metadata"}
    set_parts = []
    for f in DOCUMENT_FIELDS:
        if f in _PRESERVE_IF_NULL:
            set_parts.append(f"{f} = COALESCE(:{f}, {f})")
        else:
            set_parts.append(f"{f} = :{f}")

    # If the attachment URL changed, the previously downloaded file no longer
    # corresponds to the row's content. Clear pdf_path/txt_path/download_status
    # so the next ``cmd_download``/``convert_site_files`` cycle re-fetches
    # and re-converts. ``original_filename`` is also cleared because it tracks
    # the source filename of whatever pdf_url is current. We also delete the
    # on-disk artifacts so the file-exists shortcut in cmd_download/convert
    # can't silently reuse the stale content.
    new_pdf_url = doc_dict.get("pdf_url")
    url_changed = (existing_pdf_url or "") != (new_pdf_url or "")
    if url_changed:
        for f in ("pdf_path", "txt_path", "download_status", "original_filename"):
            set_parts.append(f"{f} = NULL")
        try:
            from . import storage as _storage
            _storage.cleanup_doc_files(existing_id)
        except Exception:
            # Filesystem cleanup is best-effort; never block the DB update.
            pass

    set_clause = ", ".join(set_parts)
    payload["id"] = existing_id
    conn.execute(
        f"UPDATE documents SET {set_clause} WHERE id = :id",
        payload,
    )
    conn.commit()
    return existing_id


def update_document_paths(conn, doc_id, pdf_path=None, txt_path=None,
                          original_filename=None, download_status=None):
    """Update filesystem-derived columns on a document row."""
    sets = []
    params = []
    if pdf_path is not None:
        sets.append("pdf_path = ?"); params.append(pdf_path)
    if txt_path is not None:
        sets.append("txt_path = ?"); params.append(txt_path)
    if original_filename is not None:
        sets.append("original_filename = ?"); params.append(original_filename)
    if download_status is not None:
        sets.append("download_status = ?"); params.append(download_status)
    if not sets:
        return
    params.append(doc_id)
    conn.execute(
        f"UPDATE documents SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()


def update_document_summary(conn, doc_id, summary):
    conn.execute("UPDATE documents SET summary = ? WHERE id = ?", (summary, doc_id))
    conn.commit()


def get_documents_pending_download(conn, site_id=None, limit=None):
    """Return documents that have a pdf_url but have not been downloaded yet."""
    query = """
        SELECT * FROM documents
         WHERE pdf_url IS NOT NULL AND pdf_url != ''
           AND (download_status IS NULL OR download_status = 'pending' OR download_status = 'failed')
    """
    params = []
    if site_id is not None:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def get_documents_pending_convert(conn, site_id=None, limit=None):
    """Return downloaded documents whose txt_path is missing."""
    query = """
        SELECT * FROM documents
         WHERE download_status = 'downloaded'
           AND (txt_path IS NULL OR txt_path = '')
    """
    params = []
    if site_id is not None:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def get_download_stats(conn, site_id=None):
    """Return download status breakdown per site (sourced from ``documents``).

    ``pending`` counts both NULL and the explicit ``'pending'`` state, since
    ``cmd_download`` treats them equivalently.
    """
    query = """
        SELECT site_id,
               COUNT(*) AS total,
               SUM(CASE WHEN pdf_url IS NULL OR pdf_url = '' THEN 1 ELSE 0 END) AS no_file,
               SUM(CASE WHEN download_status = 'downloaded' THEN 1 ELSE 0 END) AS downloaded,
               SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN (pdf_url IS NOT NULL AND pdf_url != '')
                              AND (download_status IS NULL OR download_status = 'pending')
                         THEN 1 ELSE 0 END) AS pending
        FROM documents
    """
    params = []
    if site_id:
        query += " WHERE site_id = ?"
        params.append(site_id)
    query += " GROUP BY site_id ORDER BY site_id"
    return conn.execute(query, params).fetchall()


def get_documents_without_summary(conn, site_id=None, limit=None):
    """Return documents that have no summary yet, optionally filtered by site."""
    query = "SELECT * FROM documents WHERE (summary IS NULL OR summary = '')"
    params = []
    if site_id is not None:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()
