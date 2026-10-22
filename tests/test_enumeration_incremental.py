"""Incremental mode of the enumeration framework (fake boards, no network)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.adapters import Adapter  # noqa: E402
from crawler.enumeration.incremental import IncrementalPolicy, KnownKeys, support  # noqa: E402
from crawler.enumeration.plan import PlanError, site_plan  # noqa: E402
from crawler.enumeration.runner import SiteRunner  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan  # noqa: E402


def list_pages(transport):
    return [int(parse_qs(urlsplit(u).query).get("page", ["1"])[0]) for u in transport.urls
            if urlsplit(u).path == "/list"]


def detail_ids(transport):
    return [int(parse_qs(urlsplit(u).query)["id"][0]) for u in transport.urls if urlsplit(u).path == "/view"]


class IncEnv(Env):
    def incremental(self, site, handler, *, policy=None, known_keys=None, **fetch_kw):
        plan = self.plan(site)
        transport = FakeTransport(handler)
        adapter = Adapter(plan.site_id, plan.raw.get("adapter"), self.conn, base_url=plan.base_url)
        try:
            runner = SiteRunner(plan, self.store, self.fetcher(plan, transport, **fetch_kw), adapter, today="2026-10-22")
            result = runner.run_incremental(policy, known_keys)
        finally:
            adapter.close()
        result["transport"] = transport
        return result


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.env = IncEnv()

    def tearDown(self):
        self.env.close()

    def post_numbers(self):
        return sorted(int(d["post_number"]) for d in self.env.docs())

    def full_run(self, board):
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)

    def test_stops_after_known_run_and_saves_only_new(self):
        board = FakeBoard(100)
        self.full_run(board)
        board.insert(7)  # ids 101..107 on top of page 1
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=10, overlap_pages=1))
        self.assertEqual((result["status"], result["stop_reason"]), ("incremental_complete", "known_boundary_reached"))
        # page 1: 7 new + 3 known; page 2: streak 13 >= 10; page 3: the one overlap page.
        self.assertEqual(list_pages(result["transport"]), [1, 2, 3])
        self.assertEqual(sorted(detail_ids(result["transport"])), list(range(101, 108)))
        self.assertEqual(result["stats"]["saved_new"], 7)
        self.assertEqual(self.post_numbers(), list(range(1, 108)))
        # The full run's status/segments are not touched; the incremental result lives in the state.
        site = self.env.store.site(self.env.site_id)
        self.assertEqual(site["status"], "complete")
        self.assertEqual(site["state"]["incremental"]["stop_reason"], "known_boundary_reached")
        self.assertTrue(self.env.store.events(self.env.site_id, "incremental_run"))

    def test_known_run_counts_consecutive_items_only(self):
        board = FakeBoard(60)
        self.full_run(board)
        board.insert(3)
        # Without overlap the walk ends on the page where the streak reaches N.
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=20, overlap_pages=0))
        self.assertEqual(result["stop_reason"], "known_boundary_reached")
        self.assertEqual(list_pages(result["transport"]), [1, 2, 3])  # streak 7, 17, 27
        self.assertEqual(result["stats"]["saved_new"], 3)

    def test_new_item_in_overlap_resets_the_boundary(self):
        board = FakeBoard(40)
        self.full_run(board)
        board.insert(10)  # page 1 all new
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=5, overlap_pages=1))
        self.assertEqual(result["stats"]["saved_new"], 10)
        self.assertEqual(result["stop_reason"], "known_boundary_reached")

    def test_respects_max_pages(self):
        board = FakeBoard(200)  # fresh store: nothing known
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=50, max_pages=3))
        self.assertEqual((result["status"], result["stop_reason"]), ("incremental_complete", "page_cap_reached"))
        self.assertEqual(list_pages(result["transport"]), [1, 2, 3])
        self.assertEqual(self.post_numbers(), list(range(171, 201)))
        self.assertTrue(self.env.store.events(self.env.site_id, "incremental_page_cap"))
        # No full run happened: the site stays resumable as a normal full pass.
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "incomplete")
        full = self.env.run(board_plan(), board)
        self.assertEqual(full["status"], "complete", full)
        self.assertEqual(self.post_numbers(), list(range(1, 201)))

    def test_source_exhausted_on_small_board(self):
        board = FakeBoard(15)
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=50))
        self.assertEqual(result["stop_reason"], "source_exhausted")
        self.assertEqual(self.post_numbers(), list(range(1, 16)))

    def test_known_keys_seed_by_meta_url(self):
        board = FakeBoard(100)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, tmp.name)
        tmp.write("site_id,post_number,meta_url\n")
        for i in range(1, 91):
            tmp.write(f"{self.env.site_id},,https://board.test/view?id={i}\n")
        tmp.write("other-site,95,https://board.test/view?id=95\n")  # other sites' rows are ignored
        tmp.close()
        seed = KnownKeys.load(tmp.name, self.env.site_id)
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=10, overlap_pages=0),
                                      known_keys=seed)
        self.assertEqual(result["stop_reason"], "known_boundary_reached")
        self.assertEqual(sorted(detail_ids(result["transport"])), list(range(91, 101)))  # no detail for seeded
        self.assertEqual(self.post_numbers(), list(range(91, 101)))
        self.assertEqual(result["stats"]["seed_known"], 10)

    def test_known_keys_seed_by_post_number_jsonl(self):
        board = FakeBoard(30)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, tmp.name)
        for i in range(1, 26):
            tmp.write('{"post_number": "%d"}\n' % i)
        tmp.close()
        seed = KnownKeys.load(tmp.name, self.env.site_id)
        declared = board_plan(incremental={"listing_key_is_post_number": True})
        result = self.env.incremental(declared, board, policy=IncrementalPolicy(known_run=5, overlap_pages=0),
                                      known_keys=seed)
        self.assertEqual(self.post_numbers(), list(range(26, 31)))
        self.assertEqual(result["stop_reason"], "known_boundary_reached")

    def test_bare_post_numbers_never_match_listing_keys_without_declaration(self):
        # Seeded numbers belong to other documents; the board's numeric keys collide with them.
        board = FakeBoard(30)
        seed = KnownKeys(rows=[(str(i), None) for i in range(1, 31)])
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=5, overlap_pages=0),
                                      known_keys=seed)
        self.assertEqual(result["stats"]["seed_known"], 0)
        self.assertEqual(self.post_numbers(), list(range(1, 31)))  # nothing new was silently skipped

    def test_identity_match_uses_meta_url_or_exact_pair(self):
        seed = KnownKeys(rows=[("7", "https://x.test/a?id=7"), ("8", None)])
        self.assertTrue(seed.identity_match("7", "https://x.test/a?id=7"))
        self.assertTrue(seed.identity_match("99", "https://X.test/a?id=7"))  # same meta_url
        self.assertFalse(seed.identity_match("7", "https://x.test/other"))   # number alone is not identity
        self.assertFalse(seed.identity_match("8", "https://x.test/b"))
        seed.key_is_post_number = True
        self.assertTrue(seed.identity_match("8", "https://x.test/b"))

    def test_leading_pinned_rows_are_left_out_of_the_order_check(self):
        board = FakeBoard(40)
        board.ids = [3, 5] + [i for i in board.ids if i not in (3, 5)]  # two old notices pinned on top
        result = self.env.incremental(board_plan(), board, policy=IncrementalPolicy(known_run=50, max_pages=2))
        self.assertEqual(result["status"], "incremental_complete", result)
        self.assertEqual(result["stats"]["pinned_prefix"], 2)
        self.assertIn(3, self.post_numbers())

    def test_oldest_first_page_cannot_pass_as_pinned(self):
        from crawler.enumeration.incremental import pinned_prefix
        from crawler.enumeration.parsing import Item
        items = [Item(key=str(i), url=f"u{i}", date=f"2020-01-{i:02d}") for i in range(1, 11)]
        self.assertEqual(pinned_prefix(items), 0)
        self.assertEqual(pinned_prefix(list(reversed(items))), 0)

    def test_known_keys_csv_without_identity_columns_is_rejected(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, tmp.name)
        tmp.write("site_id,title\nx,y\n")
        tmp.close()
        with self.assertRaises(ValueError):
            KnownKeys.load(tmp.name, "x")

    def test_refuses_oldest_first_and_unknown_orders_before_any_request(self):
        cases = [
            ({"strategy": "oldest_first_sort"}, "listing_order_oldest_first"),
            ({"incremental": {"order": "unknown"}}, "listing_order_unknown"),
            ({"incremental": {"order": "oldest_first"}}, "listing_order_oldest_first"),
            ({"strategy": "date_range_split", "segments": {"mode": "explicit", "items": [{"key": "a", "params": {"c": "1"}}]}},
             "strategy_not_front_pageable:date_range_split"),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason):
                board = FakeBoard(20)
                result = self.env.incremental(board_plan(**over), board)
                self.assertEqual((result["status"], result["stop_reason"]),
                                 ("config_error", f"incremental_unsupported:{reason}"))
                self.assertEqual(result["transport"].urls, [])
                self.assertEqual(self.env.docs(), [])
        self.assertEqual(support(site_plan({"sites": {"s": {"strategy": "sitemap", "sitemap": {"sitemap_url": "https://x.test/s.xml"},
                                                            "request_interval_s": 1.5}}}, "s"))[1],
                         "strategy_not_front_pageable:sitemap")

    def test_listing_that_is_not_newest_first_stops_before_saving(self):
        board = FakeBoard(30)
        board.ids.reverse()  # served oldest first: dates go up
        result = self.env.incremental(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]), ("config_error", "incremental_order_not_newest_first"))
        self.assertEqual(list_pages(result["transport"]), [1])
        self.assertEqual(self.env.docs(), [])
        self.assertTrue(self.env.store.events(self.env.site_id, "incremental_order_violation"))

    def test_undated_listing_needs_a_declared_order(self):
        undated = {"listing": {"parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                                          "id_regex": r"id=(\d+)"}}}
        board = FakeBoard(30)
        result = self.env.incremental(board_plan(**undated), board)
        self.assertEqual(result["stop_reason"], "incremental_order_unverified")
        self.assertEqual(self.env.docs(), [])
        declared = dict(undated, incremental={"order": "newest_first", "known_run": 5, "overlap_pages": 0})
        result = self.env.incremental(board_plan(**declared), board)
        self.assertEqual(result["status"], "incremental_complete", result)
        self.assertEqual(self.post_numbers(), list(range(1, 31)))

    def test_plan_section_sets_policy(self):
        plan = self.env.plan(board_plan(incremental={"known_run": 7, "max_pages": 2}))
        policy = IncrementalPolicy.from_plan(plan)
        self.assertEqual((policy.known_run, policy.max_pages, policy.overlap_pages), (7, 2, 1))
        self.assertEqual(IncrementalPolicy.from_plan(plan, known_run=3).known_run, 3)
        self.assertEqual(IncrementalPolicy().known_run, 50)
        self.assertEqual(IncrementalPolicy().max_pages, 50)

    def test_plan_section_is_validated(self):
        for bad in ({"known_run": 0}, {"max_pages": 5000}, {"order": "random"}, {"stop_after": 3}, {"known_run": "50"}, []):
            with self.subTest(bad=bad), self.assertRaises(PlanError):
                self.env.plan(board_plan(incremental=bad))

    def test_host_hold_stops_before_any_request(self):
        self.env.store.seed_hold("board.test", "access_restricted")
        board = FakeBoard(20)
        result = self.env.incremental(board_plan(), board)
        self.assertEqual(result["status"], "access_blocked")
        self.assertEqual(result["transport"].urls, [])

    def test_refusal_is_persisted_as_a_hold_and_not_retried(self):
        from crawler.enumeration.http import Response
        board = FakeBoard(30)
        calls = []

        def handler(url):
            if urlsplit(url).path == "/list":
                calls.append(url)
                return Response(403, {"Content-Type": "text/html"}, "<html>forbidden</html>")
            return board(url)

        result = self.env.incremental(board_plan(), handler)
        self.assertEqual(result["status"], "access_blocked", result)
        self.assertEqual(len(calls), 1)  # no retry around a block
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "access_blocked")
        again = self.env.incremental(board_plan(), board)
        self.assertEqual(again["status"], "access_blocked")
        self.assertEqual(again["transport"].urls, [])

    def test_request_budget_is_respected(self):
        board = FakeBoard(100)
        result = self.env.incremental(board_plan(), board, max_requests=5)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertLessEqual(len(result["transport"].urls), 5)


class IncrementalCliTest(unittest.TestCase):
    def test_flags_need_incremental_and_exclude_full_modes(self):
        from crawler.enumeration import run
        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "plan.json")
            with open(plan, "w") as fh:
                fh.write('{"sites": {"s": {"strategy": "last_page_backward", "listing": {"url": "https://x.test/l"}}}}')
            base = ["--plan", plan, "--db", os.path.join(tmp, "enum_t.db"), "--state-dir", tmp]
            for extra in (["--known-keys", plan], ["--known-run", "5"], ["--incremental", "--refresh"],
                          ["--incremental", "--retry-failed"]):
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    run.main(base + extra)


if __name__ == "__main__":
    unittest.main()
