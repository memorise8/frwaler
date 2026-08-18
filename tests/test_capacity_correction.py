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

    def test_ignores_hal_solr_rows_from_coverage_report(self):
        # coverage_report.csv 의 hal_solr 행은 개별 사이트가 아니라 HAL 플랫폼
        # 전체 총계를 담는다 (서로 다른 두 기관이 동일한 4,628,498 을 보고).
        _write_csv(self.audit / "coverage_report.csv",
                   ["site_id", "source_total", "method", "status"],
                   [["site-h", "4628498", "hal_solr", "partial"],
                    ["site-i", "123", "wordpress_posts", "ok"]])
        got = collect_measurements(self.audit)
        self.assertNotIn("site-h", got)
        self.assertEqual(got["site-i"], [(123, "coverage_report.csv")])


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
