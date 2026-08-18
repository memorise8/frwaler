# -*- coding: utf-8 -*-
"""BaseCrawler 커서 계약: 추가만, 기본값에서 기존 동작 불변."""
import os
import unittest
from unittest import mock

from crawler.base_crawler import BaseCrawler, CrawlCancelled, CrawlControl, CrawlUpToDate


class _Fake(BaseCrawler):
    site_id = "rc-fake"
    site_name = "fake"
    base_url = "http://invalid.invalid"

    def crawl(self, limit=None):
        return None


class _StubBackend:
    """DB 없이 _save_paper_v2 의 저장 경로를 흉내내는 스텁.

    ``responses`` 는 ``find_by_dedup_key`` 가 호출 순서대로 돌려줄 값들이다:
    정수(또는 임의의 truthy 값)면 "이미 알려진 문서", ``None`` 이면 "신규".
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._next_insert_id = 1000

    def find_by_dedup_key(self, conn, site_id, post_number, meta_url):
        return self._responses.pop(0)

    def insert_document(self, conn, doc):
        self._next_insert_id += 1
        return self._next_insert_id


class CursorContractTest(unittest.TestCase):
    def _inst(self):
        return _Fake(db_conn=None)

    def test_defaults_change_nothing(self):
        inst = self._inst()
        self.assertIsNone(inst.delivery_cursor)
        self.assertIsNone(inst._pending_cursor)
        self.assertEqual(inst._cursor_items_done, 0)
        self.assertEqual(type(inst).DELIVERY_ORDER, "arbitrary")
        self.assertEqual(inst.delivery_mode, "incremental")  # 기존 기본값 유지

    def test_advance_cursor_records_in_memory_only(self):
        inst = self._inst()
        inst._advance_cursor({"page": 3}, items_done=50)
        inst._advance_cursor({"page": 4}, items_done=50)
        self.assertEqual(inst._pending_cursor, {"page": 4})
        self.assertEqual(inst._cursor_items_done, 100)

    def test_advance_cursor_copies_the_dict(self):
        inst = self._inst()
        cur = {"page": 1}
        inst._advance_cursor(cur)
        cur["page"] = 999
        self.assertEqual(inst._pending_cursor, {"page": 1})

    def test_control_exceptions_escape_except_Exception(self):
        # 733/800 크롤러가 저장 루프를 except Exception 으로 감싼다 —
        # 이 성질이 무너지면 취소와 조기 종료가 코퍼스 전체에서 무력화된다.
        for exc_type in (CrawlUpToDate, CrawlCancelled):
            self.assertTrue(issubclass(exc_type, CrawlControl))
            self.assertFalse(issubclass(exc_type, Exception))
        with self.assertRaises(CrawlUpToDate):
            try:
                raise CrawlUpToDate("boom")
            except Exception:
                self.fail("except Exception 이 제어 예외를 삼켰다")

    def test_arbitrary_order_never_raises_regardless_of_known_streak(self):
        # DELIVERY_ORDER 기본값(arbitrary)에서는 얼마나 연속으로 기보유
        # 문서를 만나도 CrawlUpToDate 가 나면 안 된다.
        inst = self._inst()
        n = inst.UP_TO_DATE_THRESHOLD * 10
        stub = _StubBackend(responses=[42] * n)
        with mock.patch.dict(os.environ, {"LIBERTREE_DB_BACKEND": "postgres"}), \
                mock.patch("crawler.db_backend.get_backend", return_value=stub):
            for _ in range(n):
                result = inst._save_paper_v2(
                    {"post_number": "fixed-1", "meta_url": "http://invalid.invalid/1", "title": "t"}
                )
                self.assertEqual(result, 42)

    def test_counter_resets_on_a_genuine_insert(self):
        inst = self._inst()
        inst.DELIVERY_ORDER = "newest_first"
        inst.UP_TO_DATE_THRESHOLD = 5
        # 4 known, 1 new (resets), 4 known (counter=4, no raise), 1 more known (5th -> raise).
        responses = [42, 42, 42, 42, None, 42, 42, 42, 42, 42]
        stub = _StubBackend(responses=list(responses))
        doc = {"post_number": "fixed-1", "meta_url": "http://invalid.invalid/1", "title": "t"}
        with mock.patch.dict(os.environ, {"LIBERTREE_DB_BACKEND": "postgres"}), \
                mock.patch("crawler.db_backend.get_backend", return_value=stub):
            for _ in range(len(responses) - 1):
                inst._save_paper_v2(dict(doc))
            with self.assertRaises(CrawlUpToDate):
                inst._save_paper_v2(dict(doc))


if __name__ == "__main__":
    unittest.main()
