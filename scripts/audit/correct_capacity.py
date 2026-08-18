# -*- coding: utf-8 -*-
"""capacity_final.csv 를 디스크의 모든 감사 CSV 와 대조해 보정한다 (오프라인 전용).

2026-08-18 신뢰도 분석(CAPACITY_RELIABILITY_20260818.md)의 결론을 실행에 옮긴다:
consolidate_capacity.py 의 우선순위 사슬은 ~20개 감사 파일을 전혀 읽지 않아
(핵심 사례: doaj-org-search 35,582 vs 서버 자체 보고 13,373,055 — 376배),
합계가 6,109,256 으로 기재됐지만 최선 증거 병합 시 24,260,462 이다
(분석 단계 추정치. 이 스크립트의 실제 실행 결과는 hal_solr 제외 규칙을
반영해 24,330,001 — 차이 +0.29% 는 10만 건 미만 롱테일에 국한된다).

방법: 사이트별 corrected = max(기재값, 다른 모든 감사 CSV 의 측정값).
provenance(어느 파일이 값을 줬는지)·확증 수·신뢰도를 함께 기록해,
single_source 행은 나중에 실측 재검증 대상으로 골라낼 수 있게 한다.
coverage_report.csv 의 method=hal_solr 행은 개별 사이트가 아니라 HAL 플랫폼
전체 총계가 새어 들어온 값이라 측정값 수집에서 제외한다(consolidate_capacity.py 도
같은 이유로 제외했다).

네트워크 접근 없음. 기존 파일 수정 없음. 출력은 capacity_corrected.csv/.md 뿐.

Usage:
    python3 scripts/audit/correct_capacity.py [--audit-dir scripts/audit]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

# 분석이 실측 대조에 사용한 count 성 컬럼 전부. 여기서 빠지면 그 파일의
# 측정값이 조용히 무시되어 다시 과소평가로 돌아간다 — 테스트가 집합을 고정한다.
COUNT_COLUMNS = (
    "server_total", "counted", "source_total", "total",
    "filtered_total", "exact_total", "total_text_only", "html_list_하한",
)

# 이 스크립트의 기준 입력과 출력 — 측정값 수집에서 제외한다.
# (기준값은 별도로 base 행에서 읽으므로 여기 넣으면 이중 계산은 아니고,
#  출력을 제외하지 않으면 재실행이 자기 출력을 다시 읽는다.)
EXCLUDED_FILES = frozenset({"capacity_final.csv", "capacity_corrected.csv"})

BASE_FIELDS = [
    "site_id", "sheet", "collected", "max_to_collect", "source",
    "exact_or_lowerbound", "gap", "health",
    "observed_pdf_rate", "projected_pdf_count", "projected_storage_gb",
]
OUT_FIELDS = BASE_FIELDS + [
    "corrected_max", "correction_source", "n_corroborating",
    "confidence", "corrected_storage_gb",
]

# corrected 의 90% 이상을 보고한 파일을 "확증"으로 센다. 2개 이상이면
# corroborated, 1개면 single_source(실측 재검증 후보).
CORROBORATION_RATIO = 0.9


def _to_int(value):
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def collect_measurements(audit_dir: Path) -> dict:
    """audit_dir 의 모든 CSV 에서 site_id 별 (측정값, 파일명) 목록을 모은다.

    파일별로 헤더에 site_id 와 COUNT_COLUMNS 교집합이 있어야 하며, 행마다
    존재하는 count 컬럼들의 최댓값 하나를 그 파일의 측정값으로 삼는다
    (counted 는 크롤러가 멈출 때까지 센 수, server_total 은 서버가 보고한
    전체 수 — 큰 쪽이 실체에 가깝다).
    """
    measurements: dict = {}
    for path in sorted(audit_dir.glob("*.csv")):
        if path.name in EXCLUDED_FILES:
            continue
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            if "site_id" not in fields:
                continue
            count_cols = [c for c in COUNT_COLUMNS if c in fields]
            if not count_cols:
                continue
            per_site: dict = {}
            for row in reader:
                site = (row.get("site_id") or "").strip()
                if not site:
                    continue
                # coverage_report.csv 의 hal_solr 행은 HAL 플랫폼 전체 규모가
                # 사이트 값으로 새어 들어온 것이다 (afd 와 ign-ensg 가 동일한
                # 4,628,498 을 보고 — 서로 다른 기관이 같은 값일 수 없다).
                # consolidate_capacity.py 도 같은 이유로 hal_solr 을 제외했다.
                if path.name == "coverage_report.csv" \
                        and (row.get("method") or "").strip() == "hal_solr":
                    continue
                values = [v for v in (_to_int(row.get(c)) for c in count_cols)
                          if v is not None]
                if not values:
                    continue
                best = max(values)
                if site not in per_site or best > per_site[site]:
                    per_site[site] = best
            for site, best in per_site.items():
                measurements.setdefault(site, []).append((best, path.name))
    return measurements


def _global_gb_per_doc(base_rows) -> float:
    """기재 행들의 (저장 GB / 문서 수) 전역 평균 — unmeasured 행 추정용."""
    total_docs = 0
    total_gb = 0.0
    for row in base_rows:
        docs = _to_int(row.get("max_to_collect"))
        try:
            gb = float(row.get("projected_storage_gb") or "")
        except ValueError:
            continue
        if docs and docs > 0:
            total_docs += docs
            total_gb += gb
    return (total_gb / total_docs) if total_docs else 0.0


def correct_rows(base_rows, measurements) -> list:
    """기재 행 + 측정값 → 보정 컬럼이 붙은 행 목록 (입력은 수정하지 않는다)."""
    gb_per_doc = _global_gb_per_doc(base_rows)
    out = []
    for row in base_rows:
        r = dict(row)
        site = r["site_id"]
        filed = _to_int(r.get("max_to_collect"))
        candidates = list(measurements.get(site, []))
        if filed is not None:
            candidates.append((filed, "capacity_final.csv"))

        if not candidates:
            r.update(corrected_max="", correction_source="",
                     n_corroborating=0, confidence="no_data",
                     corrected_storage_gb="")
            out.append(r)
            continue

        corrected, source_file = max(candidates, key=lambda t: t[0])
        independent = [v for v, _ in measurements.get(site, [])]
        n_corr = sum(1 for v in independent
                     if corrected > 0 and v >= corrected * CORROBORATION_RATIO)
        if filed is not None and filed >= corrected:
            corrected, source_file = filed, "capacity_final.csv"
            confidence = "unchanged"
        elif n_corr >= 2:
            confidence = "corroborated"
        else:
            confidence = "single_source"

        # 저장 용량: 사이트별 비율이 있으면 스케일, 없으면 전역 평균으로 추정.
        try:
            filed_gb = float(r.get("projected_storage_gb") or "")
        except ValueError:
            filed_gb = None
        if filed and filed > 0 and filed_gb is not None:
            corrected_gb = round(filed_gb * corrected / filed, 2)
        else:
            corrected_gb = round(corrected * gb_per_doc, 2)

        r.update(corrected_max=corrected, correction_source=source_file,
                 n_corroborating=n_corr, confidence=confidence,
                 corrected_storage_gb=corrected_gb)
        out.append(r)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="scripts/audit", type=Path)
    args = parser.parse_args()
    audit: Path = args.audit_dir

    with (audit / "capacity_final.csv").open(encoding="utf-8-sig", newline="") as f:
        base_rows = list(csv.DictReader(f))

    rows = correct_rows(base_rows, collect_measurements(audit))

    out_csv = audit / "capacity_corrected.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(rows)

    corrected_total = sum(r["corrected_max"] for r in rows
                          if isinstance(r["corrected_max"], int))
    filed_total = sum(v for v in (_to_int(r.get("max_to_collect")) for r in rows)
                      if v is not None)
    n_changed = sum(1 for r in rows
                    if isinstance(r["corrected_max"], int)
                    and (_to_int(r.get("max_to_collect")) or 0) * 2 < r["corrected_max"])
    storage_total = sum(r["corrected_storage_gb"] for r in rows
                        if isinstance(r["corrected_storage_gb"], float))
    by_conf: dict = {}
    for r in rows:
        by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
    top = sorted((r for r in rows if isinstance(r["corrected_max"], int)),
                 key=lambda r: r["corrected_max"], reverse=True)[:20]

    out_md = audit / "capacity_corrected.md"
    lines = [
        "# capacity_corrected — 보정 요약",
        "",
        f"- 기재 합계(capacity_final.csv): **{filed_total:,}**",
        f"- 보정 합계: **{corrected_total:,}**",
        f"- 2배 이상 상향된 사이트: **{n_changed}**",
        f"- 보정 저장 용량 추정: **{storage_total / 1024:.1f} TB**",
        f"- 신뢰도 분포: " + ", ".join(
            f"{k}={v}" for k, v in sorted(by_conf.items())),
        "",
        "| site_id | corrected_max | source | confidence |",
        "|---|--:|---|---|",
    ]
    lines += [f"| {r['site_id']} | {r['corrected_max']:,} | "
              f"{r['correction_source']} | {r['confidence']} |" for r in top]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Wrote {out_csv}")
    print(f"Wrote {out_md}")
    print(f"filed={filed_total:,} corrected={corrected_total:,} changed2x={n_changed}")


if __name__ == "__main__":
    main()
