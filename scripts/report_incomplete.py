#!/usr/bin/env python3
"""Report incomplete/defective papers to docs/fino-incomplete-report.md.

Detection criteria:
  - shallow_abstract: abstract length < MIN_ABS_LEN
  - missing_html:    metadata.rawHtmlPath empty/null
  - missing_laws:    metadata.relatedLaws empty
  - missing_doc_no:  metadata.documentNumber empty
  - empty_abstract:  abstract is null/empty
  - missing_meta:    metadata is null/empty/{}

Usage:
    python scripts/report_incomplete.py
    python scripts/report_incomplete.py --min-abs 200 --site nts-taxlaw-pd
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
REPORT_MD = ROOT / "docs" / "fino-incomplete-report.md"

DEFAULT_MIN_ABS = 200


def find_issues(conn: sqlite3.Connection, site_filter: str | None,
                min_abs: int) -> dict:
    """Return dict mapping site_id → list of (issue_type, paper_dict) tuples."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    where_parts = ["site_id LIKE 'nts-taxlaw%'"]
    if site_filter:
        where_parts = [f"site_id = '{site_filter}'"]
    where_sql = " AND ".join(where_parts)

    rows = cur.execute(f"""
        SELECT external_id, site_id, title, abstract, metadata,
               published_date,
               json_extract(metadata, '$.category') AS category,
               crawled_at
        FROM documents WHERE {where_sql}
    """).fetchall()

    by_site: dict = defaultdict(list)

    for row in rows:
        meta = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except json.JSONDecodeError:
                pass

        issues: list[str] = []
        abstract = row["abstract"] or ""
        if not abstract.strip():
            issues.append("empty_abstract")
        elif len(abstract) < min_abs:
            issues.append("shallow_abstract")
        if not meta:
            issues.append("missing_meta")
        else:
            if not (meta.get("documentNumber") or "").strip():
                issues.append("missing_doc_no")
            if not (meta.get("rawHtmlPath") or "").strip():
                issues.append("missing_html")
            if not (meta.get("relatedLaws") or []):
                issues.append("missing_laws")

        if issues:
            by_site[row["site_id"]].append({
                "external_id": row["external_id"],
                "doc_number": meta.get("documentNumber", ""),
                "title": row["title"] or "",
                "abstract_len": len(abstract),
                "category": row["category"] or "",
                "published_date": row["published_date"] or "",
                "crawled_at": row["crawled_at"] or "",
                "issues": issues,
            })
    return by_site


def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def render_report(by_site: dict, min_abs: int) -> str:
    lines: list[str] = []
    lines.append("# Fino 크롤링 — 불완전 수집 리포트")
    lines.append("")
    lines.append("자동 점검: 본문이 짧거나, 원본 HTML이 없거나, 메타가 누락된 건 추적용 리포트.")
    lines.append("**데이터 손실은 아님** — 모두 DB에 저장되어 있으나 일부 필드가 비었음.")
    lines.append("")
    lines.append(f"- 점검 기준 (abstract 짧음): **{min_abs}자 미만**")
    lines.append("")

    # 전체 요약
    lines.append("## 사이트별 요약")
    lines.append("")
    lines.append("| 사이트 | 불완전 건수 | empty_abstract | shallow_abstract | missing_html | missing_laws | missing_meta | missing_doc_no |")
    lines.append("|--------|-------------|----------------|------------------|--------------|--------------|--------------|----------------|")
    for site, papers in sorted(by_site.items()):
        counts = defaultdict(int)
        for p in papers:
            for issue in p["issues"]:
                counts[issue] += 1
        lines.append(
            f"| {site} | **{len(papers)}** "
            f"| {counts['empty_abstract']} "
            f"| {counts['shallow_abstract']} "
            f"| {counts['missing_html']} "
            f"| {counts['missing_laws']} "
            f"| {counts['missing_meta']} "
            f"| {counts['missing_doc_no']} |"
        )
    lines.append("")

    # 사이트별 상세 리스트
    for site, papers in sorted(by_site.items()):
        lines.append(f"## {site} — 불완전 {len(papers)}건")
        lines.append("")
        # 이슈 종류별 그룹화
        by_issue: dict = defaultdict(list)
        for p in papers:
            for issue in p["issues"]:
                by_issue[issue].append(p)

        for issue in ["empty_abstract", "missing_meta", "missing_doc_no",
                      "missing_html", "missing_laws", "shallow_abstract"]:
            items = by_issue.get(issue, [])
            if not items:
                continue
            lines.append(f"### {issue} ({len(items)}건)")
            lines.append("")
            lines.append("| 문서번호 | 제목 | 분류 | 생산일 | 본문길이 | DOC_ID |")
            lines.append("|----------|------|------|--------|----------|--------|")
            for p in sorted(items, key=lambda x: x["abstract_len"])[:50]:
                lines.append(
                    f"| {md_escape(p['doc_number'])} "
                    f"| {md_escape(p['title'][:60])} "
                    f"| {md_escape(p['category'])} "
                    f"| {p['published_date']} "
                    f"| {p['abstract_len']} "
                    f"| {p['external_id']} |"
                )
            if len(items) > 50:
                lines.append("")
                lines.append(f"... ({len(items) - 50}건 더 있음 — 상위 50건만 표시)")
            lines.append("")

    if not by_site:
        lines.append("## ✅ 불완전 항목 없음")
        lines.append("")
        lines.append("모든 문서가 본문/메타데이터/관련법령 정상 수집됨.")

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-abs", type=int, default=DEFAULT_MIN_ABS,
                    help="abstract 짧음 기준 (기본 200자)")
    ap.add_argument("--site", default=None,
                    help="특정 site_id만 점검 (예: nts-taxlaw-pd)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    by_site = find_issues(conn, args.site, args.min_abs)
    conn.close()

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(render_report(by_site, args.min_abs), encoding="utf-8")

    total = sum(len(p) for p in by_site.values())
    print(f"불완전 {total}건 → {REPORT_MD}")
    for site, papers in sorted(by_site.items()):
        print(f"  {site}: {len(papers)}건")


if __name__ == "__main__":
    main()
