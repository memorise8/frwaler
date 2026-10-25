"""A run that stopped at the page cap reads the listing again when the cap changes, instead of resuming
the capped cursor (S cases sastind, kna-forest, forest: a raised max_pages never reached older pages).
Caps stay out of plan_fp so a cap change does not restart every site. No network."""
import unittest

from tests.enumeration_fakes import Env, FakeBoard, board_plan


class PageCapReopenTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.board = FakeBoard(50)  # 5 pages of 10

    def run_plan(self, **over):
        return self.env.run(board_plan(**over), self.board)

    def reopen_events(self):
        return [e["detail"] for e in self.env.store.events("fake-site", "page_cap_reopened")]

    def test_raised_cap_reaches_the_older_pages(self):
        first = self.run_plan(max_pages=2)
        self.assertEqual("page_cap_reached", first["stop_reason"], first)
        capped = len(self.env.docs())
        self.assertLess(capped, 50)
        second = self.run_plan(max_pages=10)
        self.assertEqual("complete", second["status"], second)
        self.assertEqual(50, len(self.env.docs()))
        self.assertEqual([{"from": {"max_pages": 2}, "to": {"max_pages": 10}}], self.reopen_events())

    def test_same_cap_resumes_the_cursor(self):
        self.run_plan(max_pages=2)
        again = self.run_plan(max_pages=2)
        self.assertEqual("page_cap_reached", again["stop_reason"], again)
        self.assertEqual([], self.reopen_events())

    def test_cap_is_not_part_of_the_plan_fingerprint(self):
        a = self.env.plan(board_plan(max_pages=2)).fingerprint
        b = self.env.plan(board_plan(max_pages=10)).fingerprint
        self.assertEqual(a, b)

    def test_raised_cap_after_a_different_stop_still_reopens(self):
        # The capped cursor is what matters, not the reason the last run stopped (review H1).
        self.run_plan(max_pages=2)
        self.env.store.finish_site("fake-site", "incomplete", "time_budget")
        second = self.run_plan(max_pages=10)
        self.assertEqual("complete", second["status"], second)
        self.assertEqual(50, len(self.env.docs()))

    def test_stop_before_limits_were_recorded_reopens_once(self):
        self.run_plan(max_pages=2)
        site = self.env.store.site("fake-site")
        state = {k: v for k, v in site["state"].items() if k != "limits"}
        self.env.store.save_state("fake-site", state)  # a run from before this change
        second = self.run_plan(max_pages=10)
        self.assertEqual("complete", second["status"], second)
        self.assertEqual(1, len(self.reopen_events()))


if __name__ == "__main__":
    unittest.main()
