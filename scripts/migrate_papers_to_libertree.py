#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Migrate papers/documents from legacy data/papers.db → data/libertree.db.

Mapping
-------
Legacy ``papers`` (UUID PK) and/or ``documents`` (INTEGER PK) rows in
``data/papers.db`` are read and inserted as new rows in
``data/libertree.db``'s ``documents`` table. The new schema's seq_id is
auto-assigned (AUTOINCREMENT) — the legacy IDs are NOT preserved.

For ``papers``:
    site_id        → site_id
    url            → meta_url
    title          → title
    published_date → published_date
    authors (JSON) → authors  ('; ' joined)
    keywords(JSON) → keywords (', ' joined)
    abstract       → abstract
    pdf_url        → pdf_url
    department     → publisher
    external_id    → post_number  (best-effort — NULL otherwise)
    -- 신규 컬럼은 NULL/0
    pdf_downloaded = 0
    text_extracted = 0

For ``documents`` (libertree variant inside papers.db):
    site_id, external_id, meta_url, title, published_date, posted_date,
    authors, publisher, journal, pdf_url, keywords, abstract,
    original_filename
    → 동등 매핑.  posted_date → listed_date.

Sites (rows in legacy ``sites`` table) are pre-registered in libertree.db's
``sites`` table.

Usage:
    python scripts/migrate_papers_to_libertree.py [--src data/papers.db]
                                                  [--dst data/libertree.db]
                                                  [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler import db_libertree as _ldb  # noqa: E402


def _join_list_field(raw, sep: str) -> str | None:
    """Best-effort: JSON list str or plain str → joined str."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return sep.join(str(x).strip() for x in parsed if str(x).strip())
            except (ValueError, TypeError):
                return s
        return s
    if isinstance(raw, list):
        return sep.join(str(x).strip() for x in raw if str(x).strip())
    return str(raw)


def _src_has_table(src: sqlite3.Connection, name: str) -> bool:
    row = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _src_columns(src: sqlite3.Connection, table: str) -> set[str]:
    cur = src.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def _migrate_sites(src: sqlite3.Connection, dst: sqlite3.Connection, dry_run: bool) -> int:
    if not _src_has_table(src, "sites"):
        return 0
    rows = src.execute("SELECT * FROM sites").fetchall()
    n = 0
    for r in rows:
        keys = r.keys() if hasattr(r, "keys") else []
        site_id = r["id"] if "id" in keys else None
        name = r["name"] if "name" in keys else (site_id or "")
        base_url = r["base_url"] if "base_url" in keys else ""
        if not site_id:
            continue
        if not dry_run:
            _ldb.upsert_site(dst, site_id, name or site_id, base_url or "", sheet=None)
        n += 1
    return n


def _migrate_papers(src: sqlite3.Connection, dst: sqlite3.Connection, dry_run: bool) -> int:
    """Copy legacy papers (UUID PK) → libertree.documents."""
    if not _src_has_table(src, "papers"):
        return 0
    cols = _src_columns(src, "papers")
    rows = src.execute("SELECT * FROM papers").fetchall()
    n = 0
    seen_sites: set[str] = set()
    for r in rows:
        keys = r.keys() if hasattr(r, "keys") else []
        get = lambda k: r[k] if k in keys else None  # noqa: E731

        site_id = get("site_id")
        url = get("url")
        title = get("title") or "(untitled)"
        if not site_id:
            continue
        meta_url = url or get("pdf_url") or ""
        if not meta_url:
            # Fallback: synthesize a unique placeholder so dedup constraint
            # still holds (papers without any URL are rare/ignorable).
            meta_url = f"papers://{site_id}/{get('id') or 'unknown'}"

        post_number = get("external_id") if "external_id" in cols else None

        doc = {
            "site_id": site_id,
            "post_number": post_number,
            "meta_url": meta_url,
            "title": title,
            "published_date": get("published_date"),
            "listed_date": None,
            "authors": _join_list_field(get("authors"), "; "),
            "publisher": get("department"),
            "journal": None,
            "pdf_url": get("pdf_url"),
            "keywords": _join_list_field(get("keywords"), ", "),
            "abstract": get("abstract"),
            "original_filename": None,
            "pdf_downloaded": 0,
            "text_extracted": 0,
            "pdf_size_bytes": None,
            "pdf_sha256": None,
            "summary": get("summary") if "summary" in cols else None,
            "summary_model": None,
            "summary_at": None,
        }

        if not dry_run:
            # auto-register site if upstream sites table missed it
            if site_id not in seen_sites:
                _ldb.upsert_site(dst, site_id, site_id, "", sheet=None)
                seen_sites.add(site_id)
            _ldb.insert_document(dst, doc)
        n += 1
    return n


def _migrate_documents(src: sqlite3.Connection, dst: sqlite3.Connection, dry_run: bool) -> int:
    """Copy legacy libertree-style documents → new libertree.documents."""
    if not _src_has_table(src, "documents"):
        return 0
    cols = _src_columns(src, "documents")
    rows = src.execute("SELECT * FROM documents").fetchall()
    n = 0
    seen_sites: set[str] = set()
    for r in rows:
        keys = r.keys() if hasattr(r, "keys") else []
        get = lambda k: r[k] if k in keys else None  # noqa: E731

        site_id = get("site_id")
        if not site_id:
            continue
        meta_url = get("meta_url") or get("pdf_url") or ""
        if not meta_url:
            meta_url = f"papers://{site_id}/{get('id') or 'unknown'}"
        title = get("title") or "(untitled)"

        post_number = get("external_id") if "external_id" in cols else None

        doc = {
            "site_id": site_id,
            "post_number": post_number,
            "meta_url": meta_url,
            "title": title,
            "published_date": get("published_date"),
            "listed_date": get("posted_date") if "posted_date" in cols else None,
            "authors": get("authors"),
            "publisher": get("publisher"),
            "journal": get("journal") if "journal" in cols else None,
            "pdf_url": get("pdf_url"),
            "keywords": get("keywords"),
            "abstract": get("abstract"),
            "original_filename": get("original_filename") if "original_filename" in cols else None,
            "pdf_downloaded": 0,
            "text_extracted": 0,
            "pdf_size_bytes": None,
            "pdf_sha256": None,
            "summary": get("summary") if "summary" in cols else None,
            "summary_model": None,
            "summary_at": None,
        }

        if not dry_run:
            if site_id not in seen_sites:
                _ldb.upsert_site(dst, site_id, site_id, "", sheet=None)
                seen_sites.add(site_id)
            _ldb.insert_document(dst, doc)
        n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=str(ROOT / "data" / "papers.db"))
    parser.add_argument("--dst", default=str(ROOT / "data" / "libertree.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    src_path = Path(args.src).resolve()
    dst_path = Path(args.dst).resolve()
    print(f"src: {src_path}")
    print(f"dst: {dst_path}")
    print(f"dry-run: {args.dry_run}")

    if not src_path.exists():
        print("\nsrc DB does not exist — nothing to migrate (libertree.db ready, empty).")
        # Still ensure dst exists with schema.
        if not args.dry_run:
            conn = _ldb.open_db(dst_path)
            _ldb.init_db(conn)
            conn.close()
        return 0

    src = sqlite3.connect(str(src_path))
    src.row_factory = sqlite3.Row

    dst = _ldb.open_db(dst_path)
    _ldb.init_db(dst)

    # Pre-count source totals.
    src_papers = (
        src.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        if _src_has_table(src, "papers") else 0
    )
    src_documents = (
        src.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if _src_has_table(src, "documents") else 0
    )
    src_sites = (
        src.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        if _src_has_table(src, "sites") else 0
    )
    print(f"\nsrc: sites={src_sites} papers={src_papers} documents={src_documents}")

    if src_papers == 0 and src_documents == 0:
        print("\nsrc has no rows in papers/documents — nothing to copy.")
        src.close()
        dst.close()
        return 0

    n_sites = _migrate_sites(src, dst, args.dry_run)
    n_papers = _migrate_papers(src, dst, args.dry_run)
    n_documents = _migrate_documents(src, dst, args.dry_run)

    print(f"\nmigrated: sites={n_sites} papers={n_papers} documents={n_documents}")

    if not args.dry_run:
        dst_total = dst.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        dst_sites = dst.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        print(f"dst: sites={dst_sites} documents={dst_total}")

        # Sanity: row count check (papers + documents == new documents added in this run).
        # Note: dedup may collapse duplicates — flag if delta differs.
        expected_added = n_papers + n_documents
        if dst_total < expected_added:
            print(
                f"⚠ dst documents row count ({dst_total}) "
                f"< expected at least {expected_added} — dedup collapsed some rows."
            )
        else:
            print(f"✓ dst row count >= migrated input ({dst_total} >= {expected_added})")

    src.close()
    dst.close()
    print("\nmigration: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
