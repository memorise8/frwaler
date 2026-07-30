#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Export the local preview:
#      uv run ecip-library/scripts/export_catalog.py
# 3. Run the deterministic URL-validation self-check:
#      uv run ecip-library/scripts/export_catalog.py --self-check
# ──────────────────

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final
from urllib.parse import SplitResult, urlsplit

MAX_RECORDS: Final = 24
PREVIEW_CHARACTER_LIMIT: Final = 1_200
PUBLIC_ID_SALT: Final = "ecip-library-local-preview-v1"
ROOT_DIRECTORY: Final = Path(__file__).resolve().parents[2]
DATABASE_PATH: Final = ROOT_DIRECTORY / "data" / "libertree.db"
OUTPUT_PATH: Final = ROOT_DIRECTORY / "ecip-library" / "data" / "catalog.json"


@dataclass(frozen=True, slots=True)
class CatalogueRecord:
    publicId: str
    title: str
    authors: str | None
    publisher: str | None
    journal: str | None
    publishedDate: str | None
    keywords: str | None
    introductionLabel: str
    introduction: str
    sourceName: str
    sourceHost: str
    originalUrl: str
    pdfUrl: str | None


@dataclass(frozen=True, slots=True)
class AvailabilityReport:
    totalDocuments: int
    summaryAvailable: int
    abstractFallbackAvailable: int
    introductionUnavailable: int
    sourceUrlPresent: int
    pdfUrlPresent: int
    selectedRecords: int
    selectedSummary: int
    selectedAbstractFallback: int
    selectedOriginalUrl: int
    selectedPdfUrl: int
    sqliteTotalChanges: int
    sqliteQueryOnly: int


@dataclass(frozen=True, slots=True)
class ExportInvariantError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def compact_text(value: str | None) -> str | None:
    if value is None:
        return None
    compacted = " ".join(value.split())
    return compacted if compacted else None


def preview_text(value: str) -> str:
    if len(value) <= PREVIEW_CHARACTER_LIMIT:
        return value
    return f"{value[:PREVIEW_CHARACTER_LIMIT].rstrip()}…"


def normalized_host(parsed_url: SplitResult) -> str | None:
    hostname = parsed_url.hostname
    if hostname is None:
        return None
    return hostname.lower().removeprefix("www.").rstrip(".")


def is_public_dns_name(hostname: str) -> bool:
    if hostname == "localhost" or hostname.endswith(".local") or "." not in hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return False


def vetted_outbound_url(raw_url: str | None, source_url: str | None) -> str | None:
    candidate = compact_text(raw_url)
    source = compact_text(source_url)
    if candidate is None or source is None:
        return None
    try:
        candidate_parts = urlsplit(candidate)
        source_parts = urlsplit(source)
        candidate_port = candidate_parts.port
        source_port = source_parts.port
    except ValueError:
        return None
    candidate_host = normalized_host(candidate_parts)
    source_host = normalized_host(source_parts)
    if (
        candidate_parts.scheme != "https"
        or source_parts.scheme != "https"
        or candidate_parts.username is not None
        or candidate_parts.password is not None
        or candidate_port not in (None, 443)
        or source_port not in (None, 443)
        or candidate_host is None
        or source_host is None
        or not is_public_dns_name(candidate_host)
        or candidate_host != source_host
    ):
        return None
    return candidate


def public_id(title: str, source_host: str, published_date: str | None) -> str:
    material = "\x1f".join((PUBLIC_ID_SALT, title, source_host, published_date or ""))
    return f"book_{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def source_rows(connection: sqlite3.Connection) -> sqlite3.Cursor:
    return connection.execute(
        """
        SELECT
          d.title,
          d.authors,
          d.publisher,
          d.journal,
          d.published_date,
          d.keywords,
          d.abstract,
          d.summary,
          d.meta_url,
          d.pdf_url,
          s.site_name,
          s.site_url
        FROM documents AS d
        JOIN sites AS s ON s.site_id = d.site_id
        WHERE trim(d.title) <> ''
          AND (trim(coalesce(d.summary, '')) <> '' OR trim(coalesce(d.abstract, '')) <> '')
        ORDER BY d.seq_id ASC
        """
    )


def records_from_connection(connection: sqlite3.Connection) -> list[CatalogueRecord]:
    records: list[CatalogueRecord] = []
    for row in source_rows(connection):
        title = compact_text(row[0])
        source_name = compact_text(row[10])
        original_url = vetted_outbound_url(row[8], row[11])
        source_parts = urlsplit(row[11]) if row[11] is not None else None
        source_host = normalized_host(source_parts) if source_parts is not None else None
        if title is None or source_name is None or original_url is None or source_host is None:
            continue
        summary = compact_text(row[7])
        abstract = compact_text(row[6])
        if summary is not None:
            introduction_label = "요약"
            introduction = preview_text(summary)
        elif abstract is not None:
            introduction_label = "도서 소개"
            introduction = preview_text(abstract)
        else:
            continue
        records.append(
            CatalogueRecord(
                publicId=public_id(title, source_host, compact_text(row[4])),
                title=title,
                authors=compact_text(row[1]),
                publisher=compact_text(row[2]),
                journal=compact_text(row[3]),
                publishedDate=compact_text(row[4]),
                keywords=compact_text(row[5]),
                introductionLabel=introduction_label,
                introduction=introduction,
                sourceName=source_name,
                sourceHost=source_host,
                originalUrl=original_url,
                pdfUrl=vetted_outbound_url(row[9], row[11]),
            )
        )
        if len(records) == MAX_RECORDS:
            break
    return records


def report_from_connection(
    connection: sqlite3.Connection, records: list[CatalogueRecord]
) -> AvailabilityReport:
    row = connection.execute(
        """
        SELECT
          count(*),
          sum(trim(coalesce(summary, '')) <> ''),
          sum(trim(coalesce(summary, '')) = '' AND trim(coalesce(abstract, '')) <> ''),
          sum(trim(coalesce(summary, '')) = '' AND trim(coalesce(abstract, '')) = ''),
          sum(trim(coalesce(meta_url, '')) <> ''),
          sum(trim(coalesce(pdf_url, '')) <> '')
        FROM documents
        """
    ).fetchone()
    if row is None:
        raise ExportInvariantError("documents availability query returned no aggregate row")
    return AvailabilityReport(
        totalDocuments=int(row[0]),
        summaryAvailable=int(row[1]),
        abstractFallbackAvailable=int(row[2]),
        introductionUnavailable=int(row[3]),
        sourceUrlPresent=int(row[4]),
        pdfUrlPresent=int(row[5]),
        selectedRecords=len(records),
        selectedSummary=sum(record.introductionLabel == "요약" for record in records),
        selectedAbstractFallback=sum(
            record.introductionLabel == "도서 소개" for record in records
        ),
        selectedOriginalUrl=sum(record.originalUrl is not None for record in records),
        selectedPdfUrl=sum(record.pdfUrl is not None for record in records),
        sqliteTotalChanges=connection.total_changes,
        sqliteQueryOnly=int(connection.execute("PRAGMA query_only").fetchone()[0]),
    )


def export_catalogue() -> AvailabilityReport:
    database_uri = f"{DATABASE_PATH.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        records = records_from_connection(connection)
        if not records:
            raise ExportInvariantError("no vetted local-preview catalogue records were available")
        report = report_from_connection(connection, records)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return report


def run_self_check() -> None:
    trusted_source = "https://catalogue.example.org/records"
    valid = vetted_outbound_url("https://catalogue.example.org/record/1", trusted_source)
    malformed = vetted_outbound_url("https://catalogue.example.org:invalid/record/1", trusted_source)
    insecure = vetted_outbound_url("http://catalogue.example.org/record/1", trusted_source)
    foreign = vetted_outbound_url("https://elsewhere.example.org/record/1", trusted_source)
    if valid is None or malformed is not None or insecure is not None or foreign is not None:
        raise ExportInvariantError("outbound URL validation self-check failed")
    print("self-check: valid HTTPS retained; malformed, HTTP, and foreign-host URLs omitted")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the local ECIP catalogue sample")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_check:
        run_self_check()
        return
    report = export_catalogue()
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
