# -*- coding: utf-8 -*-
"""25개 대형 사이트 크롤러의 커서 패턴 준수 — 정적 소스 검사.

하이픈 파일명이라 import 할 수 없으므로 소스 텍스트로 확인한다.
여기 실패하면: 해당 파일이 delivery_cursor 를 읽지 않거나(재개 불가),
_advance_cursor 를 부르지 않거나(전진 보고 없음 → 재큐잉 불가),
DELIVERY_ORDER 선언이 없다(증분 전략 미지정).
"""
import os
import unittest
from pathlib import Path
from unittest import mock

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
# Task 5(oldest_first HAL 6개) + Task 6(newest_first DSpace/XMLUI/Invenio 7개) +
# Task 7(나머지 newest_first 3개 + arbitrary 9개)까지 세 웨이브가 모두 구현되어
# 이제 TARGETS 전체를 활성화한다.
ACTIVE = TARGETS


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


class _NetworkAttempt(BaseException):
    """fetch 시도 감지용 센티널 -- BaseException 이라 크롤러의 except Exception 이 못 삼킨다."""


class ResumeEntersLoopTest(unittest.TestCase):
    """거대 커서로 재개해도 모든 대상이 최소 1회 fetch 를 시도해야 한다.

    고정 절대 캡이 커서 변수에 걸려 있으면 재개가 0페이지를 걷고 조용히
    끝난다(실제로 9/12 파일에서 발생) -- 그 회귀를 행위로 잡는다. 네트워크
    진입점 전부를 센티널로 막으므로 실제 요청은 원리적으로 불가능하다.
    """

    def test_huge_cursor_resume_still_attempts_a_fetch(self):
        import subprocess
        import urllib.request

        import requests

        from crawler.sites import CRAWLERS

        def _boom(*a, **k):
            raise _NetworkAttempt()

        with mock.patch.object(subprocess, "run", _boom), \
                mock.patch.object(subprocess, "check_output", _boom), \
                mock.patch.object(subprocess, "Popen", _boom), \
                mock.patch.object(urllib.request, "urlopen", _boom), \
                mock.patch.object(requests.Session, "request", _boom), \
                mock.patch.object(requests, "get", _boom), \
                mock.patch.object(requests, "post", _boom), \
                mock.patch.dict(os.environ, {"LIBERTREE_DB_BACKEND": "postgres"}):
            for site_id, (order, key) in TARGETS.items():
                with self.subTest(site=site_id):
                    cls = CRAWLERS[site_id]
                    inst = cls(db_conn=None)
                    inst.delivery_mode = "backfill"
                    inst.delivery_cursor = {key: 10**9}
                    with self.assertRaises(_NetworkAttempt):
                        inst.crawl(limit=None)
