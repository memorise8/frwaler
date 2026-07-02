from __future__ import annotations

import re
from typing import Any

from .models import ArticleRecord, DocumentRecord

_NUM = re.compile(r"^\s*(\d+(?:-\d+)+)\s+(.*\S)")


def exec_citation_url(ntst_bsc_id: str) -> str:
    return f"https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntst_bsc_id}"


def parse_exec(data: dict, *, name: str, ntst_bsc_id: str, bodies: dict[str, str],
               pdf_text: str = "") -> DocumentRecord:
    rows: list[dict[str, Any]] = data.get("data", {}).get("ASISTE001MR02", {}).get("exeBaseDVOList", [])
    base = exec_citation_url(ntst_bsc_id)
    # 오염 필터용: 세목 PDF 텍스트의 공백 제거본. taxlaw 목록에 타 세목 조문이
    # 섞여 오는 경우(예: 국세기본법 목록의 주류면허 조문), 그 조문 제목은 이 세목
    # PDF에 없다 → 본문 없고 제목도 PDF에 없으면 외래 조문으로 보고 제외.
    pdf_norm = re.sub(r"\s+", "", pdf_text) if pdf_text else ""
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
        body = bodies.get(num, "")
        if not body and pdf_norm:
            title_norm = re.sub(r"\s+", "", title)
            if title_norm and title_norm not in pdf_norm:
                continue  # 세목 PDF에 없는 외래(오염) 조문 → 제외
        seq += 1
        articles.append(
            ArticleRecord(
                article_no=num,
                article_title=title,
                body_text=body,
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
