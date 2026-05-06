# -*- coding: utf-8 -*-
"""SQLite database manager for crawler-poc."""

import sqlite3
import os

DEFAULT_DB_PATH = os.path.abspath(
    os.environ.get(
        "FINOLAW_DB_PATH",
        os.path.join(os.path.dirname(__file__), '..', 'data', 'data.db'),
    )
)


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
    """Return a list of (site_id, site_name, paper_count) rows."""
    cursor = conn.execute("""
        SELECT s.id, s.name, COUNT(p.id) AS paper_count
        FROM sites s
        LEFT JOIN papers p ON p.site_id = s.id
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


def get_download_stats(conn, site_id=None):
    """Return download status breakdown per site."""
    query = """
        SELECT site_id,
               COUNT(*) AS total,
               SUM(CASE WHEN pdf_url IS NULL OR pdf_url = '' THEN 1 ELSE 0 END) AS no_file,
               SUM(CASE WHEN download_status = 'downloaded' THEN 1 ELSE 0 END) AS downloaded,
               SUM(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN (pdf_url IS NOT NULL AND pdf_url != '') AND (download_status IS NULL) THEN 1 ELSE 0 END) AS pending
        FROM papers
    """
    params = []
    if site_id:
        query += " WHERE site_id = ?"
        params.append(site_id)
    query += " GROUP BY site_id ORDER BY site_id"
    return conn.execute(query, params).fetchall()
