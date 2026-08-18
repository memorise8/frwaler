# -*- coding: utf-8 -*-
"""25개 대형 사이트 크롤러의 커서 패턴 준수 — 정적 소스 검사.

하이픈 파일명이라 import 할 수 없으므로 소스 텍스트로 확인한다.
여기 실패하면: 해당 파일이 delivery_cursor 를 읽지 않거나(재개 불가),
_advance_cursor 를 부르지 않거나(전진 보고 없음 → 재큐잉 불가),
DELIVERY_ORDER 선언이 없다(증분 전략 미지정).
"""
import unittest
from pathlib import Path

CUSTOM = Path(__file__).resolve().parent.parent / "crawler" / "sites" / "custom"

# site_id -> (기대 DELIVERY_ORDER, 커서 키)
TARGETS = {
    "inserm-hal-science-search": ("oldest_first", "offset"),
    "anr-hal-science-search": ("oldest_first", "offset"),
    "amu-hal-science-search": ("oldest_first", "offset"),
    "cea-hal-science-cnrgh": ("oldest_first", "offset"),
    "ehess-hal-science-search": ("oldest_first", "offset"),
    "ens-lyon-hal-science-search": ("oldest_first", "offset"),
    "openresearch-repository-anu-edu-au-search": ("newest_first", "page"),
    "research-collection-ethz-ch-search": ("newest_first", "page"),
    "dspace-ut-ee-search": ("newest_first", "page"),
    "ostrnrcan-dostrncan-canada-ca-search": ("newest_first", "page"),
    "repositorio-uchile-cl-discover": ("newest_first", "page"),
    "sonar-ch-global": ("newest_first", "page"),
    "cds-cern-ch-collection": ("newest_first", "page"),
    "doaj-org-search": ("newest_first", "page"),
    "pergamos-lib-uoa-gr-search": ("newest_first", "page"),
    "etera-ee-browse": ("newest_first", "offset"),
    "data-gv-at-datasets": ("arbitrary", "page"),
    "data-gov-au-data": ("arbitrary", "offset"),
    "e-stat-go-jp-stat-search": ("arbitrary", "page"),
    "ots-at-pressemappe": ("arbitrary", "page"),
    "ntrs-nasa-gov-search": ("arbitrary", "offset"),
    "prism-go-kr-homepage": ("arbitrary", "page"),
    "etis-ee-portal": ("arbitrary", "page"),
    "datacatalogue-adruk-org-browser": ("arbitrary", "page"),
    "mof-go-kr-doc": ("arbitrary", "page"),
}
# 이번 태스크(HAL 6)만 먼저 활성화하고, Task 6·7 이 나머지 키를 살린다.
ACTIVE = {k: v for k, v in TARGETS.items() if v[0] == "oldest_first"}


class CursorConformanceTest(unittest.TestCase):
    def _source(self, site_id):
        return (CUSTOM / f"{site_id}.py").read_text(encoding="utf-8")

    def test_active_targets_follow_the_cursor_pattern(self):
        for site_id, (order, key) in ACTIVE.items():
            src = self._source(site_id)
            with self.subTest(site=site_id):
                self.assertIn("delivery_cursor", src)
                self.assertIn("_advance_cursor", src)
                self.assertIn(f'DELIVERY_ORDER = "{order}"', src)
                self.assertIn(f'"{key}"', src)

    def test_hal_family_uses_stable_docid_sort(self):
        for site_id in ("anr-hal-science-search", "amu-hal-science-search",
                        "ehess-hal-science-search", "ens-lyon-hal-science-search",
                        "inserm-hal-science-search", "cea-hal-science-cnrgh"):
            with self.subTest(site=site_id):
                self.assertIn("docid asc", self._source(site_id))
