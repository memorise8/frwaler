# -*- coding: utf-8 -*-
"""Promote crawler products with space-grade qual levels into heritage_parts."""

import json
import sqlite3

# Qualification levels that qualify a part as space heritage
QUAL_WHITELIST = frozenset({
    "JANS",
    "JANSR",
    "JANTX",
    "JANTXV",
    "SMD-5962",
    "ESCC",
    "MIL-PRF-19500",
    "COTS-upscreened",
})


def _get_normalizer():
    """Import normalize_bjt_params lazily to avoid circular imports."""
    try:
        from pro_server.services.normalizer import normalize_bjt_params
        return normalize_bjt_params
    except ImportError:
        try:
            from .normalizer import normalize_bjt_params
            return normalize_bjt_params
        except ImportError:
            return None


def promote_heritage(papers_db_path: str, screening_db_path: str) -> int:
    """Scan the crawler products table and promote qualifying rows to heritage_parts.

    Reads products from the crawler DB at ``papers_db_path`` where
    ``metadata.qual_level`` is in QUAL_WHITELIST, then inserts them into the
    ``heritage_parts`` table in the screening DB at ``screening_db_path``.

    Parameters
    ----------
    papers_db_path:
        Path to the SQLite DB used by the crawler (contains ``products`` table).
    screening_db_path:
        Path to the SQLite DB used by pro_server (contains ``heritage_parts``).

    Returns
    -------
    int
        Number of rows promoted (new insertions; duplicates are ignored).
    """
    normalize = _get_normalizer()

    src = sqlite3.connect(papers_db_path)
    src.row_factory = sqlite3.Row

    dst = sqlite3.connect(screening_db_path)

    # Ensure heritage_parts table exists in destination DB
    dst.execute("""
        CREATE TABLE IF NOT EXISTS heritage_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mpn TEXT NOT NULL,
            manufacturer TEXT NOT NULL,
            part_type TEXT,
            qual_level TEXT,
            parameters TEXT,
            heritage_notes TEXT,
            source_url TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(mpn, manufacturer)
        )
    """)
    dst.commit()

    promoted = 0
    skipped_no_qual = 0
    skipped_dup = 0

    try:
        rows = src.execute(
            "SELECT external_id, name, brand, specs, metadata, url, site_id "
            "FROM products WHERE metadata IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[heritage_importer] Could not read products table: {exc}")
        src.close()
        dst.close()
        return 0

    for row in rows:
        # Parse metadata JSON
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}

        qual_level = meta.get("qual_level") or ""
        # Normalize qual_level for whitelist check (case-insensitive prefix match)
        matched_qual = _match_qual(qual_level)
        if not matched_qual:
            skipped_no_qual += 1
            continue

        mpn = (row["external_id"] or row["name"] or "").strip()
        manufacturer = (row["brand"] or "").strip()
        if not mpn:
            continue

        # Parse specs JSON
        try:
            raw_specs = json.loads(row["specs"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw_specs = {}

        # Normalize BJT parameters if normalizer is available
        if normalize and raw_specs:
            try:
                parameters = normalize(raw_specs)
            except Exception as exc:
                print(f"[heritage_importer] normalize_bjt_params error for {mpn}: {exc}")
                parameters = raw_specs
        else:
            parameters = raw_specs

        datasheet_url = meta.get("datasheet_url") or ""
        source_url = row["url"] or datasheet_url or ""
        site_id = row["site_id"] or ""
        heritage_notes = f"Imported from crawler site={site_id}"

        try:
            dst.execute(
                """INSERT OR IGNORE INTO heritage_parts
                   (mpn, manufacturer, part_type, qual_level, parameters,
                    heritage_notes, source_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mpn,
                    manufacturer,
                    "bjt",
                    matched_qual,
                    json.dumps(parameters),
                    heritage_notes,
                    source_url,
                ),
            )
            if dst.execute(
                "SELECT changes()"
            ).fetchone()[0] > 0:
                promoted += 1
            else:
                skipped_dup += 1
        except sqlite3.Error as exc:
            print(f"[heritage_importer] Insert error for {mpn}: {exc}")

    dst.commit()
    src.close()
    dst.close()

    print(
        f"[heritage_importer] Done: {promoted} promoted, "
        f"{skipped_dup} duplicates ignored, "
        f"{skipped_no_qual} skipped (no qualifying level)."
    )
    return promoted


def _match_qual(qual_level: str) -> str:
    """Return the canonical whitelist entry if qual_level matches, else ''."""
    if not qual_level:
        return ""
    upper = qual_level.strip().upper()
    for entry in QUAL_WHITELIST:
        if upper == entry.upper() or upper.startswith(entry.upper()):
            return entry
    # Partial substring match (e.g. 'JANTXV/883B' → 'JANTXV')
    for entry in sorted(QUAL_WHITELIST, key=len, reverse=True):
        if entry.upper() in upper:
            return entry
    return ""
