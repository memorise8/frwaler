from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Final

from .export_markdown import extract_text
from .training_models import DEFAULT_WORKSPACE_ROOT, LoadEvent, MIN_TEXT_CHARS, SourceDocument

ALLOWED_GENERIC_SITE_IDS: Final[frozenset[str]] = frozenset({"better-fsc-go-kr-fsc_new"})


def strip_front_matter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown.strip()
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return markdown.strip()
    return parts[2].strip()


def normalize_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def load_public_markdown_documents(manifest_path: Path) -> tuple[list[SourceDocument], list[LoadEvent]]:
    documents: list[SourceDocument] = []
    events: list[LoadEvent] = []
    if not manifest_path.exists():
        return documents, [LoadEvent("public", "", str(manifest_path), "manifest_missing", 0)]
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        markdown_path = Path(row.get("markdown_path", ""))
        text = normalize_text(strip_front_matter(markdown_path.read_text(encoding="utf-8", errors="ignore")))
        source_key = f"public:{row.get('source_type', 'unknown')}:{row.get('source_subtype', 'unknown')}"
        title = str(row.get("filename") or row.get("source_title") or "untitled")
        events.append(LoadEvent(source_key, title, str(markdown_path), "loaded", len(text)))
        if len(text) < MIN_TEXT_CHARS:
            continue
        documents.append(
            SourceDocument(
                source_id=f"public:{row.get('attachment_id', markdown_path.stem)}",
                source_key=source_key,
                source_name=str(row.get("target_name") or row.get("agency") or "public"),
                title=title,
                url=str(row.get("detail_url") or row.get("attachment_url") or ""),
                text=text,
                access_type="public",
            )
        )
    return documents, events


def load_generic_paper_documents(db_path: Path) -> tuple[list[SourceDocument], list[LoadEvent]]:
    if not db_path.exists():
        return [], [LoadEvent("generic_db", "", str(db_path), "db_missing", 0)]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, site_id, title, abstract, url, category FROM papers ORDER BY crawled_at DESC").fetchall()
    documents: list[SourceDocument] = []
    events: list[LoadEvent] = []
    for row in rows:
        site_id = str(row["site_id"])
        if site_id not in ALLOWED_GENERIC_SITE_IDS:
            continue
        text = normalize_text(str(row["abstract"] or ""))
        source_key = f"public:{site_id}"
        title = str(row["title"] or "untitled")
        events.append(LoadEvent(source_key, title, str(db_path), "loaded", len(text)))
        if len(text) < MIN_TEXT_CHARS:
            continue
        documents.append(
            SourceDocument(
                source_id=f"paper:{row['id']}",
                source_key=source_key,
                source_name=str(row["category"] or row["site_id"]),
                title=title,
                url=str(row["url"] or ""),
                text=text,
                access_type="public",
            )
        )
    return documents, events


def load_kicpa_documents(kicpa_root: Path, *, workspace_root: Path = DEFAULT_WORKSPACE_ROOT) -> tuple[list[SourceDocument], list[LoadEvent]]:
    manifest_path = kicpa_root / "manifest.jsonl"
    if not manifest_path.exists():
        return [], [LoadEvent("kicpa", "", str(manifest_path), "manifest_missing", 0)]
    documents: list[SourceDocument] = []
    events: list[LoadEvent] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        for index, attachment in enumerate(row.get("attachments", []), start=1):
            path = _resolve_path(str(attachment.get("path", "")), workspace_root)
            text = _read_attachment_text(path)
            title = str(row.get("title") or attachment.get("label") or path.name)
            source_key = f"kicpa:{row.get('source_key', 'unknown')}"
            events.append(LoadEvent(source_key, title, str(path), "loaded" if text else "extract_failed", len(text)))
            if len(text) < MIN_TEXT_CHARS:
                continue
            documents.append(
                SourceDocument(
                    source_id=f"kicpa:{row.get('board_id')}:{row.get('bltn_no')}:{index}",
                    source_key=source_key,
                    source_name=str(row.get("source_name") or "KICPA"),
                    title=title,
                    url=str(attachment.get("url") or ""),
                    text=text,
                    access_type=str(row.get("access_type") or "kicpa_member_authenticated"),
                )
            )
    return documents, events


def _resolve_path(value: str, workspace_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return workspace_root / path


def _read_attachment_text(path: Path) -> str:
    if not path.exists():
        return ""
    if path.suffix.lower() in {".txt", ".md"}:
        return normalize_text(path.read_text(encoding="utf-8", errors="ignore"))
    return normalize_text(extract_text(path))
