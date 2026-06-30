from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from bs4 import BeautifulSoup
import requests

from .db import connect_db, init_schema, upsert_attachment, upsert_document
from .fetch import download_attachment, fetch_page_request, new_session
from .models import AttachmentLink, Target, TargetKind
from .parsers import extract_links_for_target, page_body, page_title
from .sources import TARGETS
from .target_pages import PageRequest, direct_page_request, page_request_for_target


DEFAULT_DB_PATH = Path("data/fino_acct.db")
DEFAULT_DOWNLOAD_DIR = Path("data/fino_acct_docs")


@dataclass(frozen=True, slots=True)
class CliArgs:
    db_path: Path
    download_dir: Path
    max_pages: int
    delay_seconds: float
    no_download: bool
    priorities: str


@dataclass(slots=True)
class MutableCliArgs:
    db_path: Path = DEFAULT_DB_PATH
    download_dir: Path = DEFAULT_DOWNLOAD_DIR
    max_pages: int = 1
    delay_seconds: float = 0.5
    no_download: bool = False
    priorities: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler.fino_acct.collect")
    _ = parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    _ = parser.add_argument("--max-pages", type=int, default=1)
    _ = parser.add_argument("--delay-seconds", type=float, default=0.5)
    _ = parser.add_argument("--no-download", action="store_true")
    _ = parser.add_argument("--priorities", default="")
    return parser


def parse_cli_args() -> CliArgs:
    namespace = MutableCliArgs()
    parsed = build_parser().parse_args(namespace=namespace)
    return CliArgs(
        db_path=parsed.db_path,
        download_dir=parsed.download_dir,
        max_pages=parsed.max_pages,
        delay_seconds=parsed.delay_seconds,
        no_download=parsed.no_download,
        priorities=parsed.priorities,
    )


def collect_all(
    *,
    db_path: Path,
    download_dir: Path,
    max_pages: int,
    delay_seconds: float,
    download: bool,
    priorities: set[int],
) -> tuple[int, int]:
    session = new_session()
    with connect_db(db_path) as conn:
        init_schema(conn)
        total_documents = 0
        total_attachments = 0
        for target in TARGETS:
            if priorities and target.priority not in priorities:
                continue
            docs, attachments = collect_target(
                conn=conn,
                session=session,
                target=target,
                download_dir=download_dir,
                max_pages=max_pages,
                delay_seconds=delay_seconds,
                download=download,
            )
            total_documents += docs
            total_attachments += attachments
            print(f"[{target.priority}] {target.target_name}: documents={docs} attachments={attachments}", flush=True)
    return total_documents, total_attachments


def collect_target(
    *,
    conn: sqlite3.Connection,
    session: requests.Session,
    target: Target,
    download_dir: Path,
    max_pages: int,
    delay_seconds: float,
    download: bool,
) -> tuple[int, int]:
    match target.kind:
        case TargetKind.DETAIL | TargetKind.META:
            return collect_page(
                conn=conn,
                session=session,
                target=target,
                request=direct_page_request(target.url, target.url),
                download_dir=download_dir,
                delay_seconds=delay_seconds,
                download=download,
                follow_details=False,
            )
        case TargetKind.LIST:
            documents = 0
            attachments = 0
            for page in range(1, max_pages + 1):
                docs, files = collect_page(
                    conn=conn,
                    session=session,
                    target=target,
                    request=page_request_for_target(target, page),
                    download_dir=download_dir,
                    delay_seconds=delay_seconds,
                    download=download,
                    follow_details=True,
                )
                documents += docs
                attachments += files
            return documents, attachments


def collect_page(
    *,
    conn: sqlite3.Connection,
    session: requests.Session,
    target: Target,
    request: PageRequest,
    download_dir: Path,
    delay_seconds: float,
    download: bool,
    follow_details: bool,
) -> tuple[int, int]:
    result = fetch_page_request(session, request, delay_seconds)
    if result.status_code >= 400:
        return 0, 0
    soup = BeautifulSoup(result.content, "html.parser")
    title = page_title(soup, target.target_name)
    document_id = upsert_document(
        conn,
        source_priority=target.priority,
        agency=target.agency,
        target_name=target.target_name,
        source_url=target.url,
        source_type=target.source_type,
        source_subtype=target.source_subtype,
        index_name=target.index_name,
        external_id=request.external_id,
        title=title,
        detail_url=result.url,
        published_date="",
        body_text=page_body(soup),
    )
    links = extract_links_for_target(target, result.url, soup)
    attachment_count = store_attachments(
        conn=conn,
        session=session,
        document_id=document_id,
        links=links.attachments,
        download_dir=download_dir / f"{target.priority:02d}",
        prefix=str(document_id),
        delay_seconds=delay_seconds,
        download=download,
    )
    documents = 1
    if follow_details:
        for detail_url in links.details[:10]:
            docs, files = collect_page(
                conn=conn,
                session=session,
                target=target,
                request=direct_page_request(detail_url, detail_url),
                download_dir=download_dir,
                delay_seconds=delay_seconds,
                download=download,
                follow_details=False,
            )
            documents += docs
            attachment_count += files
    return documents, attachment_count


def store_attachments(
    *,
    conn: sqlite3.Connection,
    session: requests.Session,
    document_id: int,
    links: tuple[AttachmentLink, ...],
    download_dir: Path,
    prefix: str,
    delay_seconds: float,
    download: bool,
) -> int:
    count = 0
    for index, link in enumerate(links, start=1):
        if download:
            result = download_attachment(session, link, download_dir, f"{prefix}_{index}", delay_seconds)
            local_path = "" if result.local_path is None else str(result.local_path)
            status = result.status
            content_type = result.content_type
            size_bytes = result.size_bytes
        else:
            local_path = ""
            status = "pending"
            content_type = ""
            size_bytes = 0
        upsert_attachment(
            conn,
            document_id=document_id,
            url=link.url,
            filename=link.filename,
            local_path=local_path,
            content_type=content_type,
            size_bytes=size_bytes,
            status=status,
        )
        count += 1
    return count


def parse_priorities(value: str) -> set[int]:
    if not value.strip():
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def main() -> int:
    args = parse_cli_args()
    documents, attachments = collect_all(
        db_path=args.db_path,
        download_dir=args.download_dir,
        max_pages=args.max_pages,
        delay_seconds=args.delay_seconds,
        download=not args.no_download,
        priorities=parse_priorities(args.priorities),
    )
    print(f"done documents={documents} attachments={attachments}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
