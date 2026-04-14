# -*- coding: utf-8 -*-
"""SQLite database manager for crawler-poc."""

import hashlib
import sqlite3
import os

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'papers.db')


def paper_exists(conn, site_id, url=None, title_hash=None):
    """Check if a paper already exists by URL or title hash."""
    if url:
        row = conn.execute(
            "SELECT id FROM papers WHERE site_id = ? AND url = ?",
            (site_id, url)
        ).fetchone()
        if row:
            return True
    if title_hash:
        row = conn.execute(
            "SELECT id FROM papers WHERE site_id = ? AND external_id = ?",
            (site_id, title_hash)
        ).fetchone()
        if row:
            return True
    return False


def compute_content_hash(title, url=""):
    """Compute a hash for deduplication."""
    content = f"{title.strip().lower()}|{url.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def get_db(db_path=None):
    """Return a connection to the SQLite database at db_path."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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

        CREATE TABLE IF NOT EXISTS products (
            id              TEXT PRIMARY KEY,
            site_id         TEXT NOT NULL REFERENCES sites(id),
            external_id     TEXT,
            name            TEXT,
            price           TEXT,
            price_value     REAL,
            currency        TEXT,
            brand           TEXT,
            category        TEXT,
            description     TEXT,
            image_url       TEXT,
            image_urls      TEXT,
            specs           TEXT,
            rating          REAL,
            review_count    INTEGER,
            availability    TEXT,
            url             TEXT,
            html_path       TEXT,
            metadata        TEXT,
            crawled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(site_id, external_id)
        );
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


def product_exists(conn, site_id, url=None, external_id=None):
    """Check if a product already exists by URL or external_id."""
    if external_id:
        row = conn.execute(
            "SELECT id FROM products WHERE site_id = ? AND external_id = ?",
            (site_id, external_id)
        ).fetchone()
        if row:
            return True
    if url:
        row = conn.execute(
            "SELECT id FROM products WHERE site_id = ? AND url = ?",
            (site_id, url)
        ).fetchone()
        if row:
            return True
    return False


def upsert_product(conn, product_dict):
    """Insert or replace a product record."""
    conn.execute("""
        INSERT OR REPLACE INTO products
            (id, site_id, external_id, name, price, price_value, currency,
             brand, category, description, image_url, image_urls, specs,
             rating, review_count, availability, url, html_path, metadata)
        VALUES
            (:id, :site_id, :external_id, :name, :price, :price_value, :currency,
             :brand, :category, :description, :image_url, :image_urls, :specs,
             :rating, :review_count, :availability, :url, :html_path, :metadata)
    """, product_dict)
    conn.commit()


def get_product_stats(conn, site_id=None):
    """Return product count per site."""
    query = """
        SELECT site_id, COUNT(*) AS total,
               SUM(CASE WHEN html_path IS NOT NULL AND html_path != '' THEN 1 ELSE 0 END) AS html_saved
        FROM products
    """
    params = []
    if site_id:
        query += " WHERE site_id = ?"
        params.append(site_id)
    query += " GROUP BY site_id ORDER BY site_id"
    return conn.execute(query, params).fetchall()
