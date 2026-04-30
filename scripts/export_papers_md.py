#!/usr/bin/env python3
"""Export papers from papers.db to markdown files.

Usage:
    python scripts/export_papers_md.py --site-id nts-taxlaw-pd --limit 10
    python scripts/export_papers_md.py --site-id nts-taxlaw-qt --limit 10

Output: data/exports/<site_id>/<safe_doc_number>.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
EXPORT_ROOT = ROOT / "data" / "exports"

UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(s: str, max_len: int = 120) -> str:
    s = UNSAFE_FS.sub("_", s or "")
    s = s.strip(" .")
    return s[:max_len] or "unnamed"


def _kv(label: str, value) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, list):
        if all(isinstance(x, str) for x in value):
            return f"- **{label}**: {', '.join(value)}"
        return f"- **{label}**: {json.dumps(value, ensure_ascii=False)}"
    return f"- **{label}**: {value}"


def render_paper(row: sqlite3.Row) -> str:
    meta = {}
    if row["metadata"]:
        try:
            meta = json.loads(row["metadata"])
        except json.JSONDecodeError:
            meta = {}
    keywords = []
    if row["keywords"]:
        try:
            keywords = json.loads(row["keywords"])
        except json.JSONDecodeError:
            pass

    lines: list[str] = []
    lines.append(f"# {row['title']}")
    lines.append("")

    # Primary metadata table
    lines.append("## 메타데이터")
    lines.append("")
    fields = [
        ("문서번호", meta.get("documentNumber")),
        ("사건번호", meta.get("caseNumber")),
        ("DOC_ID", row["external_id"]),
        ("유형", meta.get("documentTypeName")),
        ("결정유형코드", meta.get("decisionClassCd")),
        ("세법 분류", row["category"]),
        ("생산일자", row["published_date"]),
        ("귀속연도", meta.get("attrYr")),
        ("심리결과", meta.get("reviewReason")),
        ("심리결과코드", meta.get("reviewResultCd")),
        ("전원합의체 여부", meta.get("supremeCourtAllAgmt")),
        ("등록일시", meta.get("firstRegDtm")),
        ("수정일시", meta.get("lastAltDtm")),
        ("등록기관코드", meta.get("inputOrgCd")),
        ("상세 URL", row["url"]),
    ]
    for label, value in fields:
        line = _kv(label, value)
        if line:
            lines.append(line)
    lines.append("")

    if keywords:
        lines.append("## 키워드")
        lines.append("")
        lines.append(", ".join(keywords))
        lines.append("")

    related_laws = meta.get("relatedLaws") or []
    if related_laws:
        lines.append("## 관련 법령")
        lines.append("")
        for law in related_laws:
            lines.append(f"- {law}")
        lines.append("")

    related_topics = meta.get("relatedTopics") or []
    if related_topics:
        lines.append("## 관련법령 주제어")
        lines.append("")
        for t in related_topics:
            lines.append(f"- {t}")
        lines.append("")

    trial_history = meta.get("trialHistory") or []
    if trial_history:
        lines.append("## 심급 이력")
        lines.append("")
        for i, t in enumerate(trial_history, 1):
            lines.append(f"{i}. {t}")
        lines.append("")

    referenced_cases = meta.get("referencedCases") or []
    if referenced_cases:
        lines.append("## 참조 판례")
        lines.append("")
        for r in referenced_cases:
            lines.append(f"- {r}")
        lines.append("")

    cited_cases = meta.get("citedCases") or []
    if cited_cases:
        lines.append("## 인용 판례")
        lines.append("")
        for r in cited_cases:
            lines.append(f"- {r}")
        lines.append("")

    attached = meta.get("attachedFiles") or []
    if attached:
        lines.append("## 첨부파일")
        lines.append("")
        for f in attached:
            nm = f.get("name", "")
            fid = f.get("fileId", "")
            lines.append(f"- {nm} (fileId: {fid})")
        lines.append("")

    abstract = row["abstract"] or ""
    if abstract:
        lines.append("## 본문")
        lines.append("")
        lines.append(abstract)
        lines.append("")

    return "\n".join(lines)


def export(site_id: str, limit: int | None,
           updated_after: str | None = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = """SELECT external_id, title, abstract, category, keywords,
                    published_date, url, metadata, crawled_at
             FROM papers WHERE site_id = ?"""
    params: list = [site_id]
    if updated_after:
        sql += " AND crawled_at > ?"
        params.append(updated_after)
    sql += " ORDER BY crawled_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    params = tuple(params)

    out_dir = EXPORT_ROOT / site_id
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    html_count = 0
    for row in conn.execute(sql, params):
        meta = json.loads(row["metadata"] or "{}")
        doc_no = meta.get("documentNumber") or row["external_id"]
        ext_id = row["external_id"] or ""
        base = safe_filename(f"{doc_no}_{ext_id}" if doc_no != ext_id else str(ext_id))

        # MD (검색용)
        (out_dir / (base + ".md")).write_text(render_paper(row), encoding="utf-8")
        count += 1

        # HTML (위키 표시용) — 크롤러가 저장한 원본(_html/{DOC_ID}.html)을
        # 같은 베이스네임으로 symlink (디스크 절약, 단일 source-of-truth)
        raw_path = meta.get("rawHtmlPath")
        if raw_path:
            src = ROOT / raw_path
            if src.exists():
                link = out_dir / (base + ".html")
                # 기존 링크/파일 정리
                if link.is_symlink() or link.exists():
                    link.unlink()
                # out_dir 기준 상대경로로 symlink 생성
                try:
                    target = os.path.relpath(src, start=out_dir)
                    link.symlink_to(target)
                    html_count += 1
                except OSError as e:
                    # 심볼릭 링크 지원 안 되는 환경 fallback: 복사
                    link.write_text(src.read_text(encoding="utf-8"),
                                    encoding="utf-8")
                    html_count += 1
    conn.close()
    print(f"[{site_id}] MD {count}개, HTML {html_count}개 → {out_dir}")
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--updated-after",
                    help="Only export papers whose crawled_at is newer "
                         "than this ISO timestamp (e.g. '2026-04-23 14:00:00')")
    args = ap.parse_args()
    export(args.site_id, args.limit, args.updated_after)


if __name__ == "__main__":
    main()
