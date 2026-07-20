# -*- coding: utf-8 -*-
"""Adapter: legacy ``paper_dict`` (UUID/JSON-array shape) → libertree ``document_dict``.

기존 사이트 크롤러는 다음 형태의 dict 를 반환한다::

    {
        "id": "<uuid>",                 # ignored — DB 가 자동 생성
        "site_id": "ntrs",
        "external_id": "...",
        "title": "...",
        "authors": '["Alice", "Bob"]',  # JSON array string
        "abstract": "...",
        "category": "...",              # dropped
        "keywords": '["tax", "law"]',   # JSON array string
        "published_date": "...",
        "url": "...",                   # → meta_url
        "pdf_url": "...",
        "doi": "...",                   # dropped
        "department": "...",            # → publisher
        "metadata": '{"...": "..."}',   # JSON object — best-effort posted_date/journal/original_filename 추출
    }

이 어댑터를 통과한 결과 dict 는 :func:`crawler.db.upsert_document`/
:meth:`BaseCrawler._save_document` 에 그대로 넣을 수 있다.

기존 크롤러 코드는 단 한 줄만 바꾸면 된다::

    self._save_paper(paper_dict)
    # →
    self._save_document(paper_to_document(paper_dict))
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable

# documents 테이블이 받는 keys (id/timestamp/path 컬럼은 제외) — db.DOCUMENT_FIELDS 와 동일.
_KEYS = (
    "site_id", "external_id", "meta_url",
    "title", "published_date", "posted_date",
    "authors", "publisher", "journal",
    "pdf_url", "keywords", "abstract",
    "original_filename", "summary", "metadata",
)

# legacy.metadata JSON 안에서 잘 쓰는 키 후보들. 사이트마다 다르므로 best-effort.
_POSTED_DATE_KEYS = ("posted_date", "listDate", "list_date", "regDate", "firstRegDtm", "registeredAt")
_JOURNAL_KEYS = ("journal", "journalName", "publication", "venue", "publishedIn")
_ORIGINAL_FILENAME_KEYS = ("original_filename", "originalFilename", "fileName", "attachedFileName", "pdfFileName")


def _join_authors(value: Any) -> str | None:
    """Convert author field (str / list / JSON-encoded list) to ``"; "`` joined string."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                items = parsed if isinstance(parsed, list) else [s]
            except (ValueError, TypeError):
                items = [s]
        else:
            # 이미 ; 또는 , 로 구분됐을 수도 있다 — 통일.
            if ";" in s:
                items = [p.strip() for p in s.split(";")]
            elif "," in s and len(s) < 200:
                items = [p.strip() for p in s.split(",")]
            else:
                items = [s]
    else:
        items = [str(value)]
    items = [str(x).strip() for x in items if str(x).strip()]
    return "; ".join(items) if items else None


def _join_keywords(value: Any) -> str | None:
    """Convert keyword field to ``", "`` joined string."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                items = parsed if isinstance(parsed, list) else [s]
            except (ValueError, TypeError):
                items = [s]
        else:
            if "," in s:
                items = [p.strip() for p in s.split(",")]
            elif ";" in s:
                items = [p.strip() for p in s.split(";")]
            else:
                items = [s]
    else:
        items = [str(value)]
    items = [str(x).strip() for x in items if str(x).strip()]
    return ", ".join(items) if items else None


def _join_publisher(value: Any) -> str | None:
    """Convert department/publisher to ``"; "`` joined string (multi-org support)."""
    return _join_authors(value)


def _pick(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _filename_from_pdf_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def paper_to_document(paper: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a legacy ``paper_dict`` to a ``document_dict`` for the documents table.

    - ``id`` (UUID) 는 무시한다 — documents 테이블은 INTEGER AUTOINCREMENT 를 사용한다.
    - ``url`` → ``meta_url``
    - ``department`` → ``publisher``
    - ``authors`` / ``keywords`` JSON 배열 → 구분자 문자열
    - ``metadata`` (JSON) 에서 ``posted_date``, ``journal``, ``original_filename`` 을 best-effort 추출
    - ``metadata`` 자체는 ``doc["metadata"]`` 에 JSON 문자열로 보존되며, 사이트별 필드
      (``category``, ``doi``, ``documentTypeName``, ``documentNumber`` 등) 가 거기 들어간다.
    """
    metadata: Dict[str, Any] = {}
    raw_meta = paper.get("metadata")
    if isinstance(raw_meta, str) and raw_meta.strip():
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict):
                metadata = parsed
        except (ValueError, TypeError):
            metadata = {}
    elif isinstance(raw_meta, dict):
        metadata = raw_meta

    # site-specific top-level fields (category, doi) — promote into the metadata
    # JSON so the finolaw UI can read them back via parseMetadata().
    enriched_meta = dict(metadata) if metadata else {}
    for k in ("category", "doi"):
        if paper.get(k) and k not in enriched_meta:
            enriched_meta[k] = paper[k]

    posted_date = paper.get("posted_date") or _pick(metadata, _POSTED_DATE_KEYS)
    journal = paper.get("journal") or _pick(metadata, _JOURNAL_KEYS)
    original_filename = (
        paper.get("original_filename")
        or _pick(metadata, _ORIGINAL_FILENAME_KEYS)
        or _filename_from_pdf_url(paper.get("pdf_url"))
    )

    metadata_json: str | None = None
    if enriched_meta:
        try:
            metadata_json = json.dumps(enriched_meta, ensure_ascii=False)
        except (TypeError, ValueError):
            metadata_json = None

    doc: Dict[str, Any] = {
        "site_id": paper.get("site_id"),
        "external_id": paper.get("external_id"),
        "meta_url": paper.get("meta_url") or paper.get("url"),
        "title": paper.get("title"),
        "published_date": paper.get("published_date"),
        "posted_date": posted_date,
        "authors": _join_authors(paper.get("authors")),
        "publisher": _join_publisher(paper.get("publisher") or paper.get("department")),
        "journal": journal,
        "pdf_url": paper.get("pdf_url"),
        "keywords": _join_keywords(paper.get("keywords")),
        "abstract": paper.get("abstract"),
        "original_filename": original_filename,
        "summary": paper.get("summary"),
        "metadata": metadata_json,
    }
    return doc
