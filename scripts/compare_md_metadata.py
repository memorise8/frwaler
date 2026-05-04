#!/usr/bin/env python3
"""세법MD_20260417_metadata.json 과 papers.db 종합 비교.

입력:
- metadata.json: [{folder1, folder2, filename, 문서번호, 생산일자, 제목}, ...]

비교 항목:
1. 문서번호 매칭 (matched / md_only / db_only)
2. 제목 일치도 (exact / case-fold / 불일치)
3. 생산일자 일치도 ('YYYY. MM. DD.' → 'YYYY-MM-DD' 변환 후 비교)
4. folder2 ↔ DB documentTypeName 카테고리 검증
5. _N split 그룹화 분포

출력:
- docs/md_metadata_comparison_report.md
- reports/md_metadata.{summary,md_only,db_only,title_diff,date_diff}.json
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

RE_DATE = re.compile(r"^(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\s*$")


def normalise_dn(s: str) -> str:
    s = (s or "").strip()
    if s.endswith(".md"):
        s = s[:-3]
    if s.endswith("-"):
        s = s[:-1]
    return s.strip()


def normalise_date(s: str | None) -> str | None:
    """'1998. 02. 10.' → '1998-02-10'. 매칭 안 되면 원문 반환."""
    if not s:
        return None
    s = s.strip()
    m = RE_DATE.match(s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s


def normalise_title(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", "", s).strip()


def load_metadata(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_db_index(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        """
        SELECT site_id, external_id, title, published_date, category,
               json_extract(metadata, '$.documentNumber'),
               json_extract(metadata, '$.documentTypeName')
        FROM papers
        WHERE site_id LIKE 'nts-taxlaw%'
        """
    ):
        site_id, ext_id, title, pub, category, dn, dt = row
        if not dn:
            continue
        out.setdefault(normalise_dn(dn), []).append(
            {
                "site_id": site_id,
                "external_id": ext_id,
                "title": title or "",
                "published_date": pub or "",
                "category": category,
                "doc_type": dt,
                "raw_dn": dn,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="세법MD_20260417_metadata.json")
    ap.add_argument("--out", default="docs/md_metadata_comparison_report.md")
    ap.add_argument("--json-prefix", default="reports/md_metadata")
    args = ap.parse_args()

    meta_path = ROOT / args.meta
    out_md = ROOT / args.out
    json_prefix = ROOT / args.json_prefix
    out_md.parent.mkdir(parents=True, exist_ok=True)
    json_prefix.parent.mkdir(parents=True, exist_ok=True)

    print(f"loading {meta_path.name} ({meta_path.stat().st_size/1e6:.0f}MB)...")
    records = load_metadata(meta_path)
    print(f"  records: {len(records):,}")

    by_dn: dict[str, list[dict]] = defaultdict(list)
    no_dn = 0
    for r in records:
        dn = r.get("문서번호")
        if not dn:
            no_dn += 1
            continue
        by_dn[normalise_dn(dn)].append(r)

    print(f"  no 문서번호: {no_dn}")
    print(f"  unique 문서번호: {len(by_dn):,}")

    print("loading DB index...")
    conn = sqlite3.connect(DB_PATH)
    db_idx = load_db_index(conn)
    db_keys = set(db_idx.keys())
    md_keys = set(by_dn.keys())
    print(f"  unique DB docNumber: {len(db_keys):,}")

    matched = md_keys & db_keys
    md_only = md_keys - db_keys
    db_only = db_keys - md_keys
    print(f"  matched={len(matched):,} md_only={len(md_only)} db_only={len(db_only)}")

    site_dist: Counter[str] = Counter()
    for k in matched:
        for rec in db_idx[k]:
            site_dist[rec["site_id"]] += 1

    title_exact = 0
    title_normed = 0
    title_diff: list[dict] = []
    date_exact = 0
    date_diff: list[dict] = []
    date_md_missing = 0
    date_db_missing = 0
    cat_check: Counter[tuple[str, str, str]] = Counter()

    for dn in matched:
        md_records = by_dn[dn]
        db_records = db_idx[dn]
        md_titles = {(r.get("제목") or "") for r in md_records}
        md_dates_norm = {normalise_date(r.get("생산일자")) for r in md_records if r.get("생산일자")}
        md_subcats = {r.get("folder2") for r in md_records if r.get("folder2")}

        for db_rec in db_records:
            db_title = db_rec["title"]
            db_pub = db_rec["published_date"]
            db_dt = db_rec["doc_type"]

            if any(t == db_title for t in md_titles):
                title_exact += 1
            elif any(normalise_title(t) == normalise_title(db_title) for t in md_titles):
                title_normed += 1
            else:
                if len(title_diff) < 200:
                    title_diff.append(
                        {
                            "documentNumber": dn,
                            "db_title": db_title[:80],
                            "md_titles": [t[:80] for t in md_titles],
                        }
                    )

            if not db_pub:
                date_db_missing += 1
            elif not md_dates_norm:
                date_md_missing += 1
            elif db_pub in md_dates_norm:
                date_exact += 1
            else:
                if len(date_diff) < 200:
                    date_diff.append(
                        {
                            "documentNumber": dn,
                            "db_pub": db_pub,
                            "md_dates": list(md_dates_norm),
                        }
                    )

            for sc in md_subcats:
                expected = SUBCAT_TO_DOCTYPE.get(sc)
                if expected is None:
                    cat_check[(sc, db_dt or "(null)", "unmapped")] += 1
                elif expected == db_dt:
                    cat_check[(sc, db_dt, "agree")] += 1
                else:
                    cat_check[(sc, db_dt or "(null)", "disagree")] += 1

    db_only_doctype: Counter[str] = Counter()
    db_only_site: Counter[str] = Counter()
    for k in db_only:
        for rec in db_idx[k]:
            db_only_doctype[rec["doc_type"] or "(null)"] += 1
            db_only_site[rec["site_id"]] += 1

    md_only_subcat: Counter[str] = Counter()
    md_only_law: Counter[str] = Counter()
    md_only_samples: list[dict] = []
    for k in sorted(md_only):
        for r in by_dn[k]:
            md_only_subcat[r.get("folder2") or "?"] += 1
            md_only_law[r.get("folder1") or "?"] += 1
        if len(md_only_samples) < 100:
            r = by_dn[k][0]
            md_only_samples.append(
                {
                    "documentNumber": k,
                    "title": r.get("제목"),
                    "folder1": r.get("folder1"),
                    "folder2": r.get("folder2"),
                    "생산일자": r.get("생산일자"),
                }
            )

    db_only_samples: list[dict] = []
    for k in sorted(db_only)[:100]:
        rec = db_idx[k][0]
        db_only_samples.append(
            {
                "documentNumber": k,
                "title": rec["title"][:80],
                "doc_type": rec["doc_type"],
                "site_id": rec["site_id"],
                "published_date": rec["published_date"],
            }
        )

    split_dist: Counter[int] = Counter()
    for dn, files in by_dn.items():
        split_dist[len(files)] += 1

    title_total = sum(len(db_idx[k]) for k in matched)
    title_match_total = title_exact + title_normed
    date_total = title_total
    date_compared = date_exact + len(date_diff)
    L: list[str] = []
    add = L.append

    add("# md metadata.json vs papers.db 비교 리포트")
    add("")
    add(f"- 메타데이터: `{args.meta}` ({len(records):,} 레코드, unique 문서번호 {len(by_dn):,})")
    add(f"- DB: `data/papers.db` site_id LIKE `nts-taxlaw%` (unique docNum {len(db_keys):,})")
    add("")

    add("## 1. 문서번호 매칭")
    add("")
    add("| 지표 | 수 |")
    add("|---|---:|")
    add(f"| metadata records | {len(records):,} |")
    add(f"| 문서번호 누락 | {no_dn} |")
    add(f"| metadata unique 문서번호 | {len(by_dn):,} |")
    add(f"| DB unique documentNumber | {len(db_keys):,} |")
    add(f"| **매칭 (양쪽)** | **{len(matched):,}** |")
    add(f"| md only (DB에 없음) | {len(md_only)} |")
    add(f"| db only (md에 없음) | {len(db_only):,} |")
    add(f"| md 매칭률 | {len(matched)*100/max(1,len(md_keys)):.2f}% |")
    add(f"| db 매칭률 | {len(matched)*100/max(1,len(db_keys)):.2f}% |")
    add("")
    add("### 매칭된 문서의 site_id 분포")
    add("")
    add("| site_id | 수 |")
    add("|---|---:|")
    for s, n in sorted(site_dist.items(), key=lambda x: -x[1]):
        add(f"| {s} | {n:,} |")
    add("")

    add("## 2. 제목 일치도 (매칭된 문서 대상)")
    add("")
    add("- exact: 글자 그대로 일치")
    add("- whitespace-only: 공백만 다름")
    add("- diff: 다름")
    add("")
    add("| 분류 | 수 |")
    add("|---|---:|")
    add(f"| 비교 대상 (DB 레코드 수) | {title_total:,} |")
    add(f"| exact | {title_exact:,} |")
    add(f"| whitespace-only | {title_normed:,} |")
    add(f"| diff | {len(title_diff):,}+ (최대 200까지 샘플) |")
    if title_total:
        add(f"| 일치율 | {title_match_total*100/title_total:.2f}% |")
    add("")

    add("## 3. 생산일자 일치도")
    add("")
    add("md 형식 `YYYY. MM. DD.` → `YYYY-MM-DD` 변환 후 비교.")
    add("")
    add("| 분류 | 수 |")
    add("|---|---:|")
    add(f"| 비교 대상 (DB 레코드 수) | {date_total:,} |")
    add(f"| DB 빈값 | {date_db_missing:,} |")
    add(f"| md 빈값 (DB는 있음) | {date_md_missing:,} |")
    add(f"| 일치 | {date_exact:,} |")
    add(f"| 불일치 | {len(date_diff):,}+ (최대 200까지 샘플) |")
    if date_compared:
        add(f"| 일치율 (불일치 대비) | {date_exact*100/date_compared:.2f}% |")
    add("")

    add("## 4. 폴더(folder2) ↔ DB documentTypeName 검증")
    add("")
    add("매칭된 문서만. 같은 문서가 여러 folder1에 등장하는 경우 각각 카운트.")
    add("")
    add("| 폴더 | DB doctype | status | 수 |")
    add("|---|---|---|---:|")
    for sub_cat in sorted({k[0] for k in cat_check}):
        items = [(dt, st, n) for (sc, dt, st), n in cat_check.items() if sc == sub_cat]
        for dt, st, n in sorted(items, key=lambda x: -x[2]):
            add(f"| {sub_cat} | {dt} | {st} | {n:,} |")
    add("")

    add("## 5. _N split 분포 (한 문서번호의 파일 수)")
    add("")
    add("| 파일 수 | 문서번호 개수 |")
    add("|---:|---:|")
    for cnt in sorted(split_dist):
        add(f"| {cnt} | {split_dist[cnt]:,} |")
    add("")

    add("## 6. db only — DB엔 있고 md엔 없음")
    add("")
    add(f"**총 {len(db_only):,}건** — 이쪽이 진짜로 md에서 빠진 것")
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
    add("### 샘플 30개")
    add("")
    for s in db_only_samples[:30]:
        add(f"- `{s['documentNumber']}` [{s['doc_type']}] {s['title']}")
    add("")

    add("## 7. md only — md엔 있고 DB엔 없음")
    add("")
    add(f"**총 {len(md_only)}건** — 이쪽이 진짜로 DB에 빠진 것")
    add("")
    if md_only_subcat:
        add("### folder2 분포 (출현 횟수)")
        add("")
        add("| 폴더 | 출현 |")
        add("|---|---:|")
        for s, n in sorted(md_only_subcat.items(), key=lambda x: -x[1]):
            add(f"| {s} | {n:,} |")
        add("")
    if md_only_samples:
        add("### 샘플")
        add("")
        for s in md_only_samples[:50]:
            add(
                f"- `{s['documentNumber']}` [{s['folder2']}/{s['folder1']}] "
                f"{s['생산일자'] or '?'}  {(s['title'] or '')[:60]}"
            )
        add("")

    out_md.write_text("\n".join(L), encoding="utf-8")
    print(f"\nReport: {out_md.relative_to(ROOT)}")

    summary = {
        "metadata_records": len(records),
        "md_unique_docnum": len(by_dn),
        "db_unique_docnum": len(db_keys),
        "matched": len(matched),
        "md_only": len(md_only),
        "db_only": len(db_only),
        "md_match_pct": round(len(matched) * 100 / max(1, len(md_keys)), 2),
        "db_match_pct": round(len(matched) * 100 / max(1, len(db_keys)), 2),
        "title": {
            "total_db_records_compared": title_total,
            "exact": title_exact,
            "whitespace_only": title_normed,
            "diff_first_200": len(title_diff),
            "match_pct": round(title_match_total * 100 / max(1, title_total), 2),
        },
        "date": {
            "total_db_records_compared": date_total,
            "db_missing": date_db_missing,
            "md_missing": date_md_missing,
            "exact": date_exact,
            "diff_first_200": len(date_diff),
            "match_pct": round(date_exact * 100 / max(1, date_compared), 2),
        },
        "split_distribution": dict(sorted(split_dist.items())),
    }
    (json_prefix.parent / f"{json_prefix.name}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (json_prefix.parent / f"{json_prefix.name}.md_only.json").write_text(
        json.dumps(md_only_samples + [{"documentNumber": k} for k in sorted(md_only)],
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (json_prefix.parent / f"{json_prefix.name}.db_only.json").write_text(
        json.dumps([{"documentNumber": k, **db_idx[k][0]} for k in sorted(db_only)],
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (json_prefix.parent / f"{json_prefix.name}.title_diff.json").write_text(
        json.dumps(title_diff, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (json_prefix.parent / f"{json_prefix.name}.date_diff.json").write_text(
        json.dumps(date_diff, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON: {json_prefix.relative_to(ROOT)}.{{summary,md_only,db_only,title_diff,date_diff}}.json")
    conn.close()


if __name__ == "__main__":
    main()
