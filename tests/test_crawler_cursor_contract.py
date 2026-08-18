# -*- coding: utf-8 -*-
"""BaseCrawler 커서 계약: 추가만, 기본값에서 기존 동작 불변."""
import unittest

from crawler.base_crawler import BaseCrawler, CrawlUpToDate


class _Fake(BaseCrawler):
    site_id = "rc-fake"
    site_name = "fake"
    base_url = "http://invalid.invalid"

    def crawl(self, limit=None):
        return None


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

    def test_up_to_date_is_a_runtime_error_like_cancelled(self):
        # 워커가 CrawlCancelled 와 같은 방식으로 잡는 제어 흐름 예외다.
        self.assertTrue(issubclass(CrawlUpToDate, RuntimeError))


if __name__ == "__main__":
    unittest.main()
