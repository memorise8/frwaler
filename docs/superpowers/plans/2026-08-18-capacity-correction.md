# 용량 데이터 교정 (Capacity Correction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `capacity_final.csv`(804개 사이트 문서 수 추정)가 최소 4배 과소평가돼 있음이 확인됐다 — 디스크에 이미 있는 다른 감사 CSV들을 병합해 보정된 `capacity_corrected.csv`를 만들고, 납품 문서의 잘못된 숫자(610만 건/17.7 TB)를 고친다. **네트워크 접근·크롤 없음** — 전 과정이 오프라인 파일 병합이다.

**Architecture:** 새 stdlib 전용 스크립트 `scripts/audit/correct_capacity.py`가 `capacity_final.csv`를 기준으로 `scripts/audit/*.csv` 전부에서 사이트별 독립 측정값을 수집해 `corrected = max(기재값, 모든 독립 측정값)`을 계산하고, 출처·확증 수·신뢰도 컬럼을 붙여 `capacity_corrected.csv`/`.md`를 쓴다. 기존 `consolidate_capacity.py`는 존재하지 않는 경로(`/data_raid/...`)와 sqlite에 묶여 있어 재실행 불가 — 건드리지 않는다(입력 자료의 계보 기록으로 보존).

**Tech Stack:** Python 3 stdlib (`csv`, `pathlib`, `argparse`)만. pandas 금지(컨테이너·CI에 없음). 테스트는 `unittest` + `tempfile` 픽스처 — DB 불필요.

**Spec:** `scripts/audit/CAPACITY_RELIABILITY_20260818.md` (Task 1에서 커밋 — 2026-08-18 신뢰도 분석 보고서. 원본: 세션 스크래치패드 `capacity-reliability-analysis.md`). 핵심 수치: 보정 총계 **24,260,462**, 804/804 사이트 값 확보, 387개 사이트가 2배 이상 어긋남, doaj-org-search 13,373,055.

## Global Constraints

- **네트워크 요청·크롤 절대 금지.** 이 계획의 어떤 단계도 제3자 사이트에 접근하지 않는다. 검증은 로컬 CSV 대조뿐이다.
- **`git add -A` / `git add .` 금지** — 다른 세션이 같은 브랜치에 파이썬을 커밋한다. 파일명 명시 스테이징만.
- `capacity_final.csv`·`consolidate_capacity.py` 등 기존 감사 파일은 **수정 금지** (계보 보존). 새 파일만 만든다.
- CSV 읽기는 전부 `encoding="utf-8-sig"` — `crawler_health_final.csv` 헤더에 BOM이 실제로 있다(`﻿site_id`).
- 커밋 트레일러: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- 테스트 실행: 리포지토리 루트에서 `python3 -m unittest tests.test_capacity_correction -v` (DB·컨테이너 불필요).

---

### Task 1: 교정 스크립트 + 테스트

**Files:**
- Create: `scripts/audit/correct_capacity.py`
- Create: `tests/test_capacity_correction.py`
- Create: `scripts/audit/CAPACITY_RELIABILITY_20260818.md` (분석 보고서 사본 — 컨트롤러가 디스패치에 원본 경로를 준다)

**Interfaces:**
- Produces: `collect_measurements(audit_dir) -> dict[str, list[tuple[int, str]]]` (site_id → [(값, 파일명)]), `correct_rows(base_rows, measurements) -> list[dict]`, CLI `python3 scripts/audit/correct_capacity.py [--audit-dir scripts/audit]`
- 출력 CSV 컬럼(기존 11개 + 신규 5개): `site_id, sheet, collected, max_to_collect, source, exact_or_lowerbound, gap, health, observed_pdf_rate, projected_pdf_count, projected_storage_gb, corrected_max, correction_source, n_corroborating, confidence, corrected_storage_gb`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_capacity_correction.py`:

```python
# -*- coding: utf-8 -*-
"""correct_capacity.py 의 병합·보정 로직 테스트. 실데이터·DB·네트워크 불필요."""
import csv
import tempfile
import unittest
from pathlib import Path

from scripts.audit.correct_capacity import (
    COUNT_COLUMNS,
    collect_measurements,
    correct_rows,
)


def _write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


class CollectMeasurementsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_takes_the_max_count_column_per_row_and_records_the_file(self):
        _write_csv(self.audit / "probe.csv",
                   ["site_id", "counted", "server_total", "note"],
                   [["site-a", "100", "13373055", "ok"]])
        got = collect_measurements(self.audit)
        self.assertEqual(got["site-a"], [(13373055, "probe.csv")])

    def test_reads_bom_prefixed_headers(self):
        # crawler_health_final.csv 의 헤더가 실제로 BOM 으로 시작한다.
        (self.audit / "bom.csv").write_bytes(
            "site_id,source_total\nsite-b,42\n".encode("utf-8-sig"))
        got = collect_measurements(self.audit)
        self.assertEqual(got["site-b"], [(42, "bom.csv")])

    def test_skips_output_files_and_files_without_site_or_count_columns(self):
        _write_csv(self.audit / "capacity_final.csv",
                   ["site_id", "max_to_collect"], [["site-c", "999999"]])
        _write_csv(self.audit / "capacity_corrected.csv",
                   ["site_id", "corrected_max"], [["site-c", "888888"]])
        _write_csv(self.audit / "no_site.csv", ["name", "total"], [["x", "5"]])
        _write_csv(self.audit / "no_count.csv", ["site_id", "note"], [["site-c", "hi"]])
        self.assertEqual(collect_measurements(self.audit), {})

    def test_ignores_blank_and_non_numeric_values_but_keeps_zero(self):
        _write_csv(self.audit / "probe.csv",
                   ["site_id", "exact_total"],
                   [["site-d", ""], ["site-e", "abc"], ["site-f", "0"]])
        got = collect_measurements(self.audit)
        self.assertNotIn("site-d", got)
        self.assertNotIn("site-e", got)
        self.assertEqual(got["site-f"], [(0, "probe.csv")])


class CorrectRowsTest(unittest.TestCase):
    def _base_row(self, **over):
        row = {
            "site_id": "site-a", "sheet": "s", "collected": "10",
            "max_to_collect": "1000", "source": "count_timeout",
            "exact_or_lowerbound": "lower_bound", "gap": "990", "health": "",
            "observed_pdf_rate": "0.5", "projected_pdf_count": "500",
            "projected_storage_gb": "2.5",
        }
        row.update(over)
        return row

    def test_upgrades_to_the_largest_independent_measurement(self):
        rows = correct_rows(
            [self._base_row()],
            {"site-a": [(5000, "x.csv"), (4900, "y.csv"), (80, "z.csv")]})
        r = rows[0]
        self.assertEqual(r["corrected_max"], 5000)
        self.assertEqual(r["correction_source"], "x.csv")
        # 확증 수 = corrected 의 90% 이상을 보고한 파일 수 (x, y)
        self.assertEqual(r["n_corroborating"], 2)
        self.assertEqual(r["confidence"], "corroborated")
        # 저장 용량은 기존 행의 사이트별 비율로 스케일: 2.5GB * 5000/1000
        self.assertAlmostEqual(r["corrected_storage_gb"], 12.5)

    def test_single_source_correction_is_flagged(self):
        rows = correct_rows([self._base_row()], {"site-a": [(5000, "x.csv")]})
        self.assertEqual(rows[0]["confidence"], "single_source")

    def test_filed_value_wins_when_it_is_already_the_max(self):
        rows = correct_rows(
            [self._base_row(max_to_collect="9000")],
            {"site-a": [(5000, "x.csv")]})
        r = rows[0]
        self.assertEqual(r["corrected_max"], 9000)
        self.assertEqual(r["correction_source"], "capacity_final.csv")
        self.assertEqual(r["confidence"], "unchanged")

    def test_unmeasured_row_gets_a_value_and_estimated_storage(self):
        # 기재값이 비어 있던 145개 행의 경로. 저장 용량은 전역 평균(GB/문서)으로 추정.
        base = self._base_row(site_id="site-u", max_to_collect="",
                              projected_pdf_count="", projected_storage_gb="",
                              observed_pdf_rate="", source="", gap="",
                              exact_or_lowerbound="unmeasured")
        anchor = self._base_row()  # 전역 평균 계산용: 1000건 -> 2.5GB
        rows = correct_rows([anchor, base], {"site-u": [(200, "x.csv")]})
        r = rows[1]
        self.assertEqual(r["corrected_max"], 200)
        self.assertEqual(r["confidence"], "single_source")
        self.assertAlmostEqual(r["corrected_storage_gb"], 0.5)  # 200 * (2.5/1000)

    def test_row_with_no_measurement_anywhere_stays_blank_not_zero(self):
        base = self._base_row(site_id="site-n", max_to_collect="",
                              projected_storage_gb="", projected_pdf_count="",
                              observed_pdf_rate="", exact_or_lowerbound="unmeasured")
        rows = correct_rows([base], {})
        r = rows[0]
        self.assertEqual(r["corrected_max"], "")
        self.assertEqual(r["confidence"], "no_data")

    def test_count_columns_cover_every_known_audit_column(self):
        # 분석이 실제로 사용한 컬럼 집합에서 하나라도 빠지면 조용한 과소평가가 된다.
        self.assertTrue({"server_total", "counted", "source_total", "total",
                         "filtered_total", "exact_total", "total_text_only",
                         "html_list_하한"} <= set(COUNT_COLUMNS))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트를 돌려 실패를 확인한다**

Run: `python3 -m unittest tests.test_capacity_correction -v`
Expected: FAIL — `ModuleNotFoundError: scripts.audit.correct_capacity` (필요하면 `scripts/audit/__init__.py`·`scripts/__init__.py` 유무를 확인하고, 없으면 빈 파일로 만든다 — 이미 있으면 만들지 않는다)

- [ ] **Step 3: 구현을 작성한다**

`scripts/audit/correct_capacity.py`:

```python
# -*- coding: utf-8 -*-
"""capacity_final.csv 를 디스크의 모든 감사 CSV 와 대조해 보정한다 (오프라인 전용).

2026-08-18 신뢰도 분석(CAPACITY_RELIABILITY_20260818.md)의 결론을 실행에 옮긴다:
consolidate_capacity.py 의 우선순위 사슬은 ~20개 감사 파일을 전혀 읽지 않아
(핵심 사례: doaj-org-search 35,582 vs 서버 자체 보고 13,373,055 — 376배),
합계가 6,109,256 으로 기재됐지만 최선 증거 병합 시 24,260,462 이다.

방법: 사이트별 corrected = max(기재값, 다른 모든 감사 CSV 의 측정값).
provenance(어느 파일이 값을 줬는지)·확증 수·신뢰도를 함께 기록해,
single_source 행은 나중에 실측 재검증 대상으로 골라낼 수 있게 한다.

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
```

- [ ] **Step 4: 테스트를 돌려 통과를 확인한다**

Run: `python3 -m unittest tests.test_capacity_correction -v`
Expected: PASS (전부)

- [ ] **Step 5: 분석 보고서를 스펙으로 커밋 위치에 복사한다**

컨트롤러가 디스패치에서 준 원본 경로의 파일을 `scripts/audit/CAPACITY_RELIABILITY_20260818.md` 로 복사한다. 내용 수정 없음.

- [ ] **Step 6: 커밋**

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
git add scripts/audit/correct_capacity.py tests/test_capacity_correction.py scripts/audit/CAPACITY_RELIABILITY_20260818.md
# scripts/__init__.py 또는 scripts/audit/__init__.py 를 Step 2 에서 새로 만들었다면 그것도 이름으로 추가
git commit -m "feat(audit): merge every audit CSV into a corrected capacity table

capacity_final.csv's precedence chain never read ~20 audit files that were
already on disk, so doaj-org-search was filed as 35,582 while three separate
probes recorded the server itself reporting ~13.37M (376x). The corrected
table takes the max across all measurements per site, records which file it
came from and how many files corroborate it, and flags single-source values
for later spot verification. Offline only -- no network, no re-crawling.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 실데이터 실행·검증·커밋

**Files:**
- Create: `scripts/audit/capacity_corrected.csv` (스크립트 출력)
- Create: `scripts/audit/capacity_corrected.md` (스크립트 출력)

**Interfaces:**
- Consumes: Task 1 의 CLI. 실행: `cd /mnt/raid/ruci_workspace/frwaler-delivery && python3 scripts/audit/correct_capacity.py`
- Produces: 커밋된 보정 CSV — Task 3 문서가 이 파일의 숫자를 인용한다.

- [ ] **Step 1: 실행한다**

Run: `python3 scripts/audit/correct_capacity.py`
Expected: `capacity_corrected.csv`(804행 + 헤더) 와 `.md` 생성, stdout 에 filed/corrected/changed2x 요약.

- [ ] **Step 2: 분석 보고서와 대조해 검증한다**

아래를 각각 확인하고 결과 숫자를 보고서에 기록한다:

```bash
python3 - <<'EOF'
import csv
rows = list(csv.DictReader(open("scripts/audit/capacity_corrected.csv", encoding="utf-8-sig")))
assert len(rows) == 804, len(rows)
total = sum(int(r["corrected_max"]) for r in rows if r["corrected_max"] != "")
doaj = next(r for r in rows if r["site_id"] == "doaj-org-search")
print("total", total)
print("doaj", doaj["corrected_max"], doaj["correction_source"], doaj["confidence"])
print("no_data", sum(1 for r in rows if r["confidence"] == "no_data"))
print("changed2x", sum(1 for r in rows if r["corrected_max"] != "" and
      (int(float(r["max_to_collect"])) if r["max_to_collect"] else 0) * 2 < int(r["corrected_max"])))
EOF
```

기대값 (스펙 `CAPACITY_RELIABILITY_20260818.md` §6):
- total 이 **24,260,462 의 ±1% 이내** — 벗어나면 원인을 찾아 보고서에 설명하고, 스크립트 결함이면 고친다 (분석과 병합 규칙이 동일하므로 크게 다를 이유가 없다).
- doaj = **13,373,055**, confidence 는 corroborated (세 파일이 13.3M대를 보고)
- no_data = **0** (분석: 804/804 값 확보)
- changed2x ≈ **387** (±소수 허용 — 정의 차이가 있으면 보고서에 기록)

- [ ] **Step 3: 커밋**

```bash
git add scripts/audit/capacity_corrected.csv scripts/audit/capacity_corrected.md
git commit -m "data(audit): corrected per-site capacity -- 24.3M documents, not 6.1M

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

(실제 보정 합계가 24.3M 과 다르게 확정되면 커밋 메시지의 숫자를 실측값으로 바꾼다 — 커밋 메시지가 데이터와 달라선 안 된다.)

---

### Task 3: 납품 문서의 숫자 교정

**Files:**
- Modify: `docs/DELIVERY_READINESS_20260818.md` (「데이터」 절의 총 용량 관련 기술)
- Modify: `docs/HANDOFF_20260818_SCALE.md` (§2 보정 표와 추정 총계)

**Interfaces:**
- Consumes: Task 2 가 커밋한 `capacity_corrected.csv`/`.md` 의 **실측 확정 숫자** (아래 자리표시 `N` 들은 전부 Task 2 결과로 치환 — 이 태스크의 구현자는 반드시 그 파일을 먼저 읽는다)

- [ ] **Step 1: DELIVERY_READINESS 의 용량 기술을 고친다**

문서에서 `6,109,256`·`610만`·`17.7 TB`·`6.1M` 을 언급하는 모든 위치를 찾아(`grep -n "6,109,256\|610만\|17.7\|6\.1M" docs/DELIVERY_READINESS_20260818.md`) 다음 취지로 바꾼다 — 숫자는 Task 2 확정값 사용:

```markdown
- 전체 수집 가능 문서(보정): **약 N,NNN만 건** (`scripts/audit/capacity_corrected.csv`,
  2026-08-18 보정 — 종전 기재 6,109,256 은 감사 파일 ~20개를 병합하지 않은
  과소평가였다. 근거: `scripts/audit/CAPACITY_RELIABILITY_20260818.md`)
- 추정 저장 용량(보정): **약 NNN TB** — 전부 수집은 비현실적이며, 무엇을 받을지
  선택하는 것이 다음 설계 과제다 (HANDOFF_20260818_SCALE.md §3-4)
- 최대 사이트: doaj-org-search **13,373,055건** (종전 기재 35,582 — OAI
  resumption 카운트의 단독 실패, 서버 자체 보고값으로 3회 교차 확인)
```

- [ ] **Step 2: HANDOFF_20260818_SCALE §2 를 갱신한다**

§2 끝에 다음을 덧붙인다 (기존 표는 역사 기록으로 유지, 숫자는 Task 2 확정값):

```markdown
**2026-08-18 후속:** 전수 대조 완료 — `scripts/audit/capacity_corrected.csv`.
보정 총계 **N,NNN,NNN건** (위 표의 20,480,139 는 5개 사이트만 반영한 중간값).
2배 이상 상향 NNN개 사이트, 값 없는 사이트 0개. single_source 로 표시된
사이트는 실측 재검증 후보다. 방법별 신뢰도와 원인 분석은
`scripts/audit/CAPACITY_RELIABILITY_20260818.md`.
```

- [ ] **Step 3: 커밋**

```bash
git add docs/DELIVERY_READINESS_20260818.md docs/HANDOFF_20260818_SCALE.md
git commit -m "docs(delivery): replace the understated capacity figures with corrected ones

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## 이 계획에 없는 것 (의도적)

- **실측 재검증 (네트워크)** — `single_source` 사이트(특히 ots-at-pressemappe 1,561,267)의 실제 프로브는 별도 승인 후 별도 작업. 이 계획은 오프라인 병합까지만.
- **재개(cursor) 설계·수집 대상 선택** — HANDOFF §4 의 2·3단계. 보정된 숫자가 이 설계들의 입력이 된다.
- **`consolidate_capacity.py` 수리** — 옛 경로·sqlite 의존이라 이 호스트에서 실행 불가. 계보 기록으로 보존.
- **고객 인계 아티팩트 갱신** — 컨트롤러가 Task 3 이후 별도로 수행 (아티팩트 도구 필요).

## Self-Review

**1. 스펙 커버리지** — 분석 §5 의 세 분류가 모두 반영되는가: 불신 방법의 과소평가(→ max 병합), 145개 unmeasured(→ 전 파일 수집으로 자동 해소), doaj 단독 실패(→ max 가 13.37M 채택). ✓ / 분석 §6 의 검증 기준 4개(total·doaj·no_data·changed2x)를 Task 2 Step 2 가 전부 검사. ✓
**2. 플레이스홀더 점검** — Task 3 의 `N` 들은 의도된 실측 치환 지시이며 구현자가 Task 2 커밋 데이터에서 읽는다고 명시. 그 외 TBD 없음. ✓
**3. 타입 일관성** — `collect_measurements` 반환 dict[str, list[tuple[int,str]]] 을 `correct_rows` 가 같은 모양으로 소비, 테스트도 동일. `COUNT_COLUMNS` 집합 테스트와 구현 튜플 일치. `corrected_max` 는 int 또는 ""(no_data), md/합계 코드가 `isinstance(..., int)` 로 걸러 일관. ✓
