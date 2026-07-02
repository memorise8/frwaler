from __future__ import annotations

import re
from typing import Any

from .models import ArticleRecord, DocumentRecord

_NUM = re.compile(r"^\s*(\d+(?:-\d+)+)\s+(.*\S)")


def exec_citation_url(ntst_bsc_id: str) -> str:
    return f"https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntst_bsc_id}"


def parse_exec(data: dict, *, name: str, ntst_bsc_id: str, bodies: dict[str, str]) -> DocumentRecord:
    rows: list[dict[str, Any]] = data.get("data", {}).get("ASISTE001MR02", {}).get("exeBaseDVOList", [])
    base = exec_citation_url(ntst_bsc_id)
    seen: set[str] = set()
    articles: list[ArticleRecord] = []
    seq = 0
    for row in rows:
        m = _NUM.match(str(row.get("ntstTextNm", "")))
        if not m:
            continue
        num, title = m.group(1), m.group(2).strip()
        if num in seen:
            continue
        seen.add(num)
        seq += 1
        articles.append(
            ArticleRecord(
                article_no=num,
                article_title=title,
                body_text=bodies.get(num, ""),
                clause_json="[]",
                seq=seq,
                source_url=f"{base}#{num}",
            )
        )
    return DocumentRecord(
        source_kind="exec_standard",
        external_id=ntst_bsc_id,
        title=name,
        category="집행기준",
        org="국세청",
        promulgated_at="",
        effective_at=str(rows[0].get("rgtYr", "")) if rows else "",
        version_code="현행",
        source_url=base,
        articles=tuple(articles),
    )
