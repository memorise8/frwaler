"""last_page_backward against fake boards (no network)."""
from __future__ import annotations

import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan  # noqa: E402


def post_numbers(env):
    return [int(d["post_number"]) for d in env.docs()]


class LastPageBackwardTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def last_page_event(self):
        return self.env.store.events(self.env.site_id, "last_page_found")[-1]["detail"]

    def assert_full_oldest_first(self, n):
        self.assertEqual(post_numbers(self.env), list(range(1, n + 1)))

    def test_empty_page_end_start_one(self):
        board = FakeBoard(45, end_mode="empty")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event(), {"last_page": 5, "end_kind": "empty",
                                                  "probe_requests": self.last_page_event()["probe_requests"]})
        self.assert_full_oldest_first(45)

    def test_clamp_end(self):
        board = FakeBoard(45, end_mode="clamp")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event()["last_page"], 5)
        self.assertEqual(self.last_page_event()["end_kind"], "clamp")
        self.assert_full_oldest_first(45)

    def test_clamp_end_exact_multiple(self):
        board = FakeBoard(40, end_mode="clamp")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual((self.last_page_event()["last_page"], self.last_page_event()["end_kind"]), (4, "clamp"))
        self.assert_full_oldest_first(40)

    def test_wrap_to_first_page_end(self):
        board = FakeBoard(45, end_mode="wrap")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual((self.last_page_event()["last_page"], self.last_page_event()["end_kind"]), (5, "wrap"))
        self.assert_full_oldest_first(45)

    def test_not_found_end(self):
        board = FakeBoard(45, end_mode="404")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event()["end_kind"], "not_found")
        self.assert_full_oldest_first(45)

    def test_start_page_zero(self):
        board = FakeBoard(33, start_page=0, end_mode="clamp")
        result = self.env.run(board_plan(listing={"start_page": 0}), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event()["last_page"], 3)  # pages 0..3
        self.assert_full_oldest_first(33)

    def test_wrong_start_page_is_detected_not_skipped(self):
        # Site's first page is 0; a plan reading from 1 would silently skip page 0.
        board = FakeBoard(33, start_page=0)
        result = self.env.run(board_plan(listing={"start_page": 1}), board)
        self.assertEqual((result["status"], result["stop_reason"]), ("config_error", "start_page_mismatch"))
        self.assertEqual(self.env.docs(), [])

    def test_single_page_site(self):
        board = FakeBoard(7, end_mode="clamp")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event()["last_page"], 1)
        self.assert_full_oldest_first(7)

    def test_large_board_probe_is_logarithmic(self):
        board = FakeBoard(1000, page_size=10, end_mode="empty", show_total=False)
        site = board_plan(adapter={"type": "list_only", "id_regex": r"id=(\d+)"})
        result = self.env.run(site, board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.last_page_event()["last_page"], 100)
        self.assertLess(self.last_page_event()["probe_requests"], 25)
        self.assert_full_oldest_first(1000)

    def test_new_posts_during_crawl_are_not_missed(self):
        board = FakeBoard(95, end_mode="empty")
        inserted = []

        def hook(b, page):
            # After the probe, three new posts arrive before every list request.
            if b.list_requests > 12 and len(inserted) < 40:
                b.insert(3)
                inserted.extend([b.next_id - 1])

        board.list_hooks.append(hook)
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        saved = post_numbers(self.env)
        self.assertEqual(len(saved), len(set(saved)))
        self.assertTrue(set(range(1, 96)) <= set(saved), sorted(set(range(1, 96)) - set(saved)))
        # The original backlog is saved oldest first.
        original = [p for p in saved if p <= 95]
        self.assertEqual(original, sorted(original))

    def test_burst_larger_than_a_page_is_walked(self):
        board = FakeBoard(60, end_mode="empty")
        state = {"done": False}

        def hook(b, page):
            if b.list_requests == 14 and not state["done"]:
                b.insert(25)  # more than two pages at once
                state["done"] = True

        board.list_hooks.append(hook)
        result = self.env.run(board_plan(adapter={"type": "list_only", "id_regex": r"id=(\d+)"}), board)
        self.assertEqual(result["status"], "complete", result)
        saved = post_numbers(self.env)
        self.assertEqual(len(saved), len(set(saved)))
        self.assertTrue(set(range(1, 61)) <= set(saved), sorted(set(range(1, 61)) - set(saved)))

    def test_resume_after_budget_skips_finished_pages(self):
        board = FakeBoard(45, end_mode="empty")
        first = self.env.run(board_plan(), board, max_requests=25)
        self.assertEqual((first["status"], first["stop_reason"]), ("incomplete", "request_budget"))
        partial = post_numbers(self.env)
        self.assertTrue(0 < len(partial) < 45)
        self.assertEqual(partial, list(range(1, len(partial) + 1)))  # oldest first, no gaps
        site = self.env.store.site(self.env.site_id)
        self.assertIsNone(site["completed_at"])
        transport = FakeTransport(board)
        second = self.env.run(board_plan(), board, transport=transport)
        self.assertEqual(second["status"], "complete", second)
        self.assert_full_oldest_first(45)
        details = [u for u in transport.urls if "/view" in u]
        # Already saved items are not fetched again.
        self.assertEqual(len(details), 45 - len(partial))
        self.assertEqual(len(set(details)), len(details))

    def test_page_cap_is_not_completion(self):
        board = FakeBoard(45, end_mode="empty")
        result = self.env.run(board_plan(max_pages=3), board)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "page_cap_reached"))
        self.assertEqual(sorted(post_numbers(self.env)), list(range(16, 46)))
        self.assertIsNone(self.env.store.site(self.env.site_id)["completed_at"])

    def test_reported_total_mismatch_is_recorded(self):
        board = FakeBoard(45, end_mode="empty")
        board.total_override = 50
        result = self.env.run(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_with_mismatch", "reported_total_mismatch"))
        self.assertTrue(self.env.store.events(self.env.site_id, "total_mismatch"))

    def test_page_urls_use_configured_parameter(self):
        board = FakeBoard(15, page_param="pg")
        transport = FakeTransport(board)
        site = board_plan(listing={"page_param": "pg", "params": {"f%5B0%5D": "type:1"}})
        result = self.env.run(site, board, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        lists = [u for u in transport.urls if "/list" in u]
        self.assertTrue(all("f%5B0%5D=type%3A1" in u and "%25" not in u for u in lists), lists)
        self.assertTrue(any(parse_qs(urlsplit(u).query).get("pg") == ["2"] for u in lists))


if __name__ == "__main__":
    unittest.main()
