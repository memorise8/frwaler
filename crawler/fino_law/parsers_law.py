from __future__ import annotations

import json
from typing import Any

from .models import ArticleRecord, DocumentRecord


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def law_citation_url(name: str) -> str:
    return f"https://www.law.go.kr/법령/{name}"


def parse_search_for_current(data: dict, name: str) -> dict | None:
    """lawSearch 응답에서 법령명 정확 일치 + 현행 1건 반환."""
    items = _as_list(data.get("LawSearch", {}).get("law"))
    for item in items:
        if item.get("법령명한글") == name and item.get("현행연혁코드") == "현행":
            return item
    return None


def parse_law_service(data: dict, *, name: str, category: str, external_id: str) -> DocumentRecord:
    """lawService 응답 → DocumentRecord(+articles). 조문만 추출."""
    root = data.get("법령", {})
    basic = root.get("기본정보", {})
    org = ""
    dept = basic.get("소관부처")
    if isinstance(dept, dict):
        org = dept.get("content", "") or dept.get("소관부처명", "")
    elif isinstance(dept, str):
        org = dept
    promulgated = str(basic.get("공포일자", "") or "")
    effective = str(basic.get("시행일자", "") or "")

    base_url = law_citation_url(name)
    articles: list[ArticleRecord] = []
    seq = 0
    for unit in _as_list(root.get("조문", {}).get("조문단위")):
        if unit.get("조문여부") != "조문":
            continue
        seq += 1
        article_no = str(unit.get("조문번호", "") or "").strip()
        # API의 조문번호는 "1" 형태 → "제N조"로 정규화
        label = article_no if article_no.startswith("제") else f"제{article_no}조"
        clauses = _as_list(unit.get("항"))
        # Build complete body_text: heading + 항내용 + 호내용 + 목내용
        body_parts: list[str] = []
        heading = str(unit.get("조문내용", "") or "").strip()
        if heading:
            body_parts.append(heading)
        for clause in clauses:
            항내용 = str(clause.get("항내용", "") or "").strip()
            if 항내용:
                body_parts.append(항내용)
            for 호 in _as_list(clause.get("호")):
                호내용 = str(호.get("호내용", "") or "").strip()
                if 호내용:
                    body_parts.append(호내용)
                for 목 in _as_list(호.get("목")):
                    목내용 = str(목.get("목내용", "") or "").strip()
                    if 목내용:
                        body_parts.append(목내용)
        body_text = "\n".join(body_parts)
        articles.append(
            ArticleRecord(
                article_no=label,
                article_title=str(unit.get("조문제목", "") or "").strip(),
                body_text=body_text,
                clause_json=json.dumps(clauses, ensure_ascii=False),
                seq=seq,
                source_url=f"{base_url}#{label}",
            )
        )

    return DocumentRecord(
        source_kind="law",
        external_id=external_id,
        title=name,
        category=category,
        org=org,
        promulgated_at=promulgated,
        effective_at=effective,
        version_code="현행",
        source_url=base_url,
        articles=tuple(articles),
    )
