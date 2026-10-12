"""--seed-holds: access-restricted hosts are held before the first request.

Fake transports and a loopback server only; no external network.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.store import Store  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan  # noqa: E402
from tests.test_enumeration_legacy_cli import LoopbackBoardServer  # noqa: E402

LIST_ONLY = {"type": "list_only", "id_regex": r"id=(\d+)"}


class SeedHoldStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "enumeration.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_seeded_hold_is_recorded_with_seeded_reason(self):
        self.assertTrue(self.store.seed_hold("Example.ORG", "ACCESS_REFUSED"))
        hold = self.store.host_hold("example.org")
        self.assertEqual(hold["reason"], "seeded:ACCESS_REFUSED")
        self.assertIsNone(self.store.host_hold("www.example.org"))  # exact host unless wildcard

    def test_wildcard_matches_subdomains_only(self):
        self.store.seed_hold("*.hal.science", "access_refused")
        self.assertTrue(self.store.host_hold("theses.hal.science"))
        self.assertTrue(self.store.host_hold("a.b.hal.science"))
        self.assertIsNone(self.store.host_hold("hal.science"))
        self.assertIsNone(self.store.host_hold("nothal.science"))

    def test_existing_hold_is_not_overwritten(self):
        self.store.hold_host("example.org", "http_403")
        self.store.seed_hold("example.org", "ACCESS_REFUSED")
        self.assertEqual(self.store.host_hold("example.org")["reason"], "http_403")

    def test_released_host_is_not_seeded_again(self):
        self.store.seed_hold("example.org", "ACCESS_REFUSED")
        self.store.release_host("example.org")
        self.store.event("*", "host_hold_released", {"host": "example.org"})
        self.assertFalse(self.store.seed_hold("example.org", "ACCESS_REFUSED"))
        self.assertIsNone(self.store.host_hold("example.org"))


class SeedHoldRunnerTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_held_site_ends_access_blocked_without_requests(self):
        self.env.store.seed_hold("board.test", "ROBOTS_REFUSED")
        t = FakeTransport(FakeBoard(6))
        # A registered site without a reference DB would otherwise stop as config_error first.
        result = self.env.run(board_plan(), None, transport=t, identity_required=True)
        self.assertEqual(result["status"], "access_blocked", result)
        self.assertEqual(result["stop_reason"], "host_access_hold:seeded:ROBOTS_REFUSED")
        self.assertEqual(t.urls, [])
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "access_blocked")

    def test_wildcard_hold_covers_a_subdomain_listing(self):
        self.env.store.seed_hold("*.board.test", "AUTH_REQUIRED")
        t = FakeTransport(FakeBoard(6))
        plan = board_plan(base_url="https://www.board.test", listing={"url": "https://www.board.test/list"})
        result = self.env.run(plan, None, transport=t)
        self.assertEqual(result["status"], "access_blocked", result)
        self.assertEqual(t.urls, [])


class SeedHoldCliTest(unittest.TestCase):
    def _plan(self, tmp, port):
        plan = tmp / "plan.json"
        plan.write_text(json.dumps({"version": 1, "sites": {"seed-site": {
            "strategy": "last_page_backward", "base_url": f"http://127.0.0.1:{port}",
            "listing": {"url": f"http://127.0.0.1:{port}/list", "page_param": "page", "start_page": 1,
                        "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                                   "id_regex": r"id=(\d+)"}},
            "adapter": LIST_ONLY}}}))
        return plan

    def _main(self, argv):
        from crawler.enumeration.run import main
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, json.loads(out.getvalue().strip().splitlines()[-2])[0]

    def test_cli_seed_holds_blocks_before_any_request_and_release_still_works(self):
        board = FakeBoard(5, page_size=10, end_mode="empty")
        calls = []

        def counted(url):
            calls.append(url)
            return board(url)
        with LoopbackBoardServer(counted) as srv, tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            seeds = tmp / "seed-holds.csv"
            seeds.write_text("host,reason\n127.0.0.1,ACCESS_REFUSED\n", encoding="utf-8")
            args = ["--plan", str(self._plan(tmp, srv.port)), "--state-dir", str(tmp / "s"),
                    "--db", str(tmp / "enum_seed.db")]
            code, result = self._main(args + ["--seed-holds", str(seeds)])
            self.assertEqual((code, result["status"]), (2, "access_blocked"), result)
            self.assertEqual(result["stop_reason"], "host_access_hold:seeded:ACCESS_REFUSED")
            self.assertEqual(calls, [])
            # A person releases host and site; re-passing the same seed file does not re-hold it.
            code, result = self._main(args + ["--seed-holds", str(seeds), "--release-host", "127.0.0.1",
                                              "--refresh"])
            self.assertEqual((code, result["status"]), (0, "complete"), result)
            self.assertTrue(calls)

    def test_cli_rejects_a_seed_file_without_host_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            seeds = tmp / "bad.csv"
            seeds.write_text("domain,reason\nexample.org,x\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                self._main(["--plan", str(self._plan(tmp, 1)), "--state-dir", str(tmp / "s"),
                            "--db", str(tmp / "enum_seed.db"), "--seed-holds", str(seeds)])


if __name__ == "__main__":
    unittest.main()
