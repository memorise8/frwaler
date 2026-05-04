#!/usr/bin/env python3
"""md 파일 리스트와 papers.db documentNumber 비교.

입력: 상대 경로 형식 md 파일 리스트 (find . -type f -name '*.md' 결과).
   ./세법MD_20260417/{법령카테고리}/{문서종류폴더}/{파일명}.md

규칙:
- 파일명에서 .md / 끝의 - 제거 → DB의 metadata.documentNumber 와 정확 비교
- '_N.md' 접미사: 분할 문서 → suffix 제거 후 매칭 (별도 카운트)
- '- ' 시작 파일: documentNumber 없음 → 매칭 제외 (별도 카운트)
- 같은 파일이 여러 법령카테고리에 중복: 정상 (한 문서 다중 법령 적용)

출력:
- docs/md_comparison_report.md  (사람용 마크다운 리포트)
- reports/md_compare.summary.json  (요약)
- reports/md_compare.md_only.json  (md에만 있는 docNumber 리스트)
- reports/md_compare.db_only.json  (DB에만 있는 docNumber 리스트)

Usage:
    .venv/bin/python scripts/compare_md.py --list md_list.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"

RE_PATH = re.compile(r"^\./세법MD_\d+/([^/]+)/([^/]+)/(.+)$")
RE_EMPTY = re.compile(r"^- ")
RE_SUFFIX = re.compile(r"_(\d+)\.md$")

SUBCAT_TO_DOCTYPE = {
    "1. 사전답변": "사전",
    "2. 질의회신": "질의",
    "3. 과세기준자문": "기준",
    "4. 고시서면질의": "고시",
    "5. 과세적부": "적부",
    "6. 이의신청": "이의",
    "7. 심사청구": "심사",
    "8. 심판청구": "심판",
    "9. 판례": "판례",
    "9. 핀례": "판례",
    "10. 헌재": "헌재",
}


def normalise(s: str) -> str:
    s = (s or "").strip()
    if s.endswith(".md"):
        s = s[:-3]
    if s.endswith("-"):
        s = s[:-1]
    return s.strip()


def load_md_list(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            m = RE_PATH.match(line)
            if not m:
                continue
            rows.append(m.groups())
    return rows


def load_db_doc_index(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT site_id, external_id, title,
               json_extract(metadata, '$.documentNumber'),
               json_extract(metadata, '$.documentTypeName')
        FROM papers
        WHERE site_id LIKE 'nts-taxlaw%'
        """
    ):
        site_id, ext_id, title, dn, dt = row
        if not dn:
            continue
        out.setdefault(normalise(dn), []).append(
            {
                "site_id": site_id,
                "external_id": ext_id,
                "doc_type": dt,
                "title": (title or "")[:60],
                "raw_dn": dn,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default="md_list.txt")
    ap.add_argument("--out", default="docs/md_comparison_report.md")
    ap.add_argument("--json-prefix", default="reports/md_compare")
    args = ap.parse_args()

    md_path = ROOT / args.list
    out_md = ROOT / args.out
    json_prefix = ROOT / args.json_prefix
    out_md.parent.mkdir(parents=True, exist_ok=True)
    json_prefix.parent.mkdir(parents=True, exist_ok=True)

    print(f"loading {md_path.relative_to(ROOT)}...")
    rows = load_md_list(md_path)
    print(f"  total: {len(rows):,}")

    empty_count = 0
    suffix_count = 0
    no_md_ext = 0
    by_norm: dict[str, list[dict]] = defaultdict(list)

    for law_cat, sub_cat, fname in rows:
        if RE_EMPTY.match(fname):
            empty_count += 1
            continue
        if not fname.endswith(".md"):
            no_md_ext += 1
            continue
        m_suffix = RE_SUFFIX.search(fname)
        if m_suffix:
            suffix_count += 1
            base = fname[: m_suffix.start()]
        else:
            base = fname[:-3]
        norm = normalise(base)
        if not norm:
            continue
        by_norm[norm].append(
            {
                "law_cat": law_cat,
                "sub_cat": sub_cat,
                "fname": fname,
                "has_suffix": bool(m_suffix),
            }
        )

    print(f"  '- ' empty: {empty_count:,}")
    print(f"  '_N.md' suffix: {suffix_count:,}")
    print(f"  no .md ext: {no_md_ext:,}")
    print(f"  unique normalised: {len(by_norm):,}")

    print("loading DB index...")
    conn = sqlite3.connect(DB_PATH)
    db_idx = load_db_doc_index(conn)
    db_keys = set(db_idx.keys())
    print(f"  unique DB docNumber: {len(db_keys):,}")

    md_keys = set(by_norm.keys())
    matched = md_keys & db_keys
    md_only = md_keys - db_keys
    db_only = db_keys - md_keys

    site_dist: Counter[str] = Counter()
    for k in matched:
        for rec in db_idx[k]:
            site_dist[rec["site_id"]] += 1

    db_only_doctype: Counter[str] = Counter()
    db_only_site: Counter[str] = Counter()
    for k in db_only:
        for rec in db_idx[k]:
            db_only_doctype[rec["doc_type"] or "(null)"] += 1
            db_only_site[rec["site_id"]] += 1

    md_only_subcat: Counter[str] = Counter()
    for k in md_only:
        for entry in by_norm[k]:
            md_only_subcat[entry["sub_cat"]] += 1

    cat_match: Counter[tuple[str, str, str]] = Counter()
    for norm in matched:
        for entry in by_norm[norm]:
            sub_cat = entry["sub_cat"]
            expected_dt = SUBCAT_TO_DOCTYPE.get(sub_cat)
            seen = {rec["doc_type"] for rec in db_idx[norm]}
            for actual_dt in seen:
                if expected_dt is None:
                    cat_match[(sub_cat, actual_dt or "(null)", "unmapped")] += 1
                elif expected_dt == actual_dt:
                    cat_match[(sub_cat, actual_dt, "agree")] += 1
                else:
                    cat_match[(sub_cat, actual_dt or "(null)", "disagree")] += 1

    md_only_sample = sorted(md_only)[:50]
    db_only_sample = sorted(db_only)[:50]

    L: list[str] = []
    add = L.append
    add("# md 파일 vs papers.db 비교 리포트")
    add("")
    add(f"- 입력: `{args.list}`")
    add(f"- DB: `data/papers.db` (site_id LIKE `nts-taxlaw%`)")
    add("")
    add("## 1. 요약")
    add("")
    add("| 지표 | 수 |")
    add("|---|---:|")
    add(f"| 전체 md 파일 라인 | {len(rows):,} |")
    add(f"| `- ` 시작 (doc번호 없음) | {empty_count:,} |")
    add(f"| `_N.md` 접미사 (분할 문서) | {suffix_count:,} |")
    add(f"| `.md` 확장자 아님 | {no_md_ext:,} |")
    add(f"| 정규화 후 unique md | {len(by_norm):,} |")
    add(f"| DB documentNumber unique | {len(db_keys):,} |")
    add(f"| **매칭 (양쪽)** | **{len(matched):,}** |")
    add(f"| md only (DB에 없음) | {len(md_only):,} |")
    add(f"| db only (md에 없음) | {len(db_only):,} |")
    if md_keys:
        add(f"| md 매칭률 | {len(matched)*100/len(md_keys):.2f}% |")
    if db_keys:
        add(f"| db 매칭률 | {len(matched)*100/len(db_keys):.2f}% |")
    add("")

    add("## 2. 매칭된 문서의 site_id 분포")
    add("")
    add("| site_id | 수 |")
    add("|---|---:|")
    for s, n in sorted(site_dist.items(), key=lambda x: -x[1]):
        add(f"| {s} | {n:,} |")
    add("")

    add("## 3. DB only — DB엔 있고 md엔 없음")
    add("")
    add(f"**총 {len(db_only):,}건**")
    add("")
    add("### documentTypeName 분포")
    add("")
    add("| documentTypeName | 수 |")
    add("|---|---:|")
    for dt, n in sorted(db_only_doctype.items(), key=lambda x: -x[1]):
        add(f"| {dt} | {n:,} |")
    add("")
    add("### site_id 분포")
    add("")
    add("| site_id | 수 |")
    add("|---|---:|")
    for s, n in sorted(db_only_site.items(), key=lambda x: -x[1]):
        add(f"| {s} | {n:,} |")
    add("")
    add("### 샘플 50개")
    add("")
    for k in db_only_sample:
        rec = db_idx[k][0]
        add(f"- `{k}` [{rec['doc_type']}] {rec['title'][:50]}")
    add("")

    add("## 4. md only — md엔 있고 DB엔 없음")
    add("")
    add(f"**총 {len(md_only):,}건**")
    add("")
    add("### 서브카테고리 분포 (한 doc이 여러 법령에 중복 가능)")
    add("")
    add("| 폴더 | 출현 |")
    add("|---|---:|")
    for s, n in sorted(md_only_subcat.items(), key=lambda x: -x[1]):
        add(f"| {s} | {n:,} |")
    add("")
    add("### 샘플 50개")
    add("")
    for k in md_only_sample:
        entries = by_norm[k]
        e = entries[0]
        add(f"- `{k}` [{e['sub_cat']}] (×{len(entries)} 법령)")
    add("")

    add("## 5. 폴더(서브카테고리) ↔ DB documentTypeName 일치 검증")
    add("")
    add("매칭된 문서만 대상. agree=폴더와 DB일치, disagree=불일치, unmapped=폴더-DB 매핑 미정의.")
    add("")
    add("| 폴더 | DB doctype | status | 수 |")
    add("|---|---|---|---:|")
    for sub_cat in sorted({k[0] for k in cat_match}):
        items = [(dt, st, n) for (sc, dt, st), n in cat_match.items() if sc == sub_cat]
        for dt, st, n in sorted(items, key=lambda x: -x[2]):
            add(f"| {sub_cat} | {dt} | {st} | {n:,} |")
    add("")

    out_md.write_text("\n".join(L), encoding="utf-8")
    print(f"\nReport: {out_md.relative_to(ROOT)}")

    summary = {
        "total_md_lines": len(rows),
        "empty_doc": empty_count,
        "suffix_count": suffix_count,
        "no_md_ext": no_md_ext,
        "unique_md": len(by_norm),
        "unique_db": len(db_keys),
        "matched": len(matched),
        "md_only": len(md_only),
        "db_only": len(db_only),
        "md_match_pct": round(len(matched) * 100 / max(1, len(md_keys)), 2),
        "db_match_pct": round(len(matched) * 100 / max(1, len(db_keys)), 2),
        "matched_by_site": dict(site_dist),
        "db_only_by_doctype": dict(db_only_doctype),
        "md_only_by_subcat": dict(md_only_subcat),
    }
    (json_prefix.parent / f"{json_prefix.name}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (json_prefix.parent / f"{json_prefix.name}.md_only.json").write_text(
        json.dumps(sorted(md_only), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (json_prefix.parent / f"{json_prefix.name}.db_only.json").write_text(
        json.dumps(sorted(db_only), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON: {json_prefix.relative_to(ROOT)}.{{summary,md_only,db_only}}.json")

    conn.close()


if __name__ == "__main__":
    main()
