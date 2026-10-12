"""Legacy crawler overrides, GenericCrawler filter encoding, and the CLI end to end
against a loopback HTTP server (no external network)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402

from crawler import db_libertree  # noqa: E402
from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.enumeration import legacy  # noqa: E402
from crawler.enumeration.plan import PlanError, load_plan, site_plan  # noqa: E402
from crawler.enumeration.run import main  # noqa: E402
from crawler.generic_crawler import GenericCrawler  # noqa: E402
from tests.enumeration_fakes import FakeBoard  # noqa: E402


class Capped(BaseCrawler):
    site_id = "capped"
    site_name = "Capped"
    base_url = "https://capped.test"
    MAX_PAGES = 200

    def crawl(self, limit=None):
        return 0


def generic_config(tmp, **pagination):
    path = Path(tmp) / "cfg.json"
    path.write_text(json.dumps({
        "site_id": "gen-site", "site_name": "Gen", "base_url": "https://gen.test",
        "options": {"delay": 0.001},
        "list_page": {"url": "https://gen.test/list?f%5B0%5D=lang%3Aen",
                      "params": {"type%5B170%5D": "170"},
                      "pagination": {"type": "query_param", "param": "page", "start": 1, **pagination},
                      "selectors": {"item_container": "li"}}}))
    return str(path)


class LegacyOverrideTest(unittest.TestCase):
    def test_defaults_are_untouched(self):
        crawler = Capped(None)
        self.assertEqual(legacy.apply_overrides(crawler, None), {})
        self.assertEqual(legacy.apply_overrides_from_env(crawler, environ={}), {})
        self.assertEqual(crawler.MAX_PAGES, 200)

    def test_page_cap_instance_attribute(self):
        crawler, other = Capped(None), Capped(None)
        changed = legacy.apply_overrides(crawler, {"page_cap": 5000})
        self.assertEqual(changed, {"MAX_PAGES": (200, 5000)})
        self.assertEqual((crawler.MAX_PAGES, other.MAX_PAGES, Capped.MAX_PAGES), (5000, 200, 200))
        legacy.apply_overrides(other, {"page_cap": None})
        self.assertEqual(other.MAX_PAGES, legacy.UNLIMITED)
        with self.assertRaises(ValueError):
            legacy.apply_overrides(other, {"page_cap": 0})

    def test_page_cap_module_constant(self):
        module = types.ModuleType("fake_capped_module")
        module.MAX_PAGES = 200
        exec("from crawler.base_crawler import BaseCrawler\n"
             "class ModCrawler(BaseCrawler):\n"
             "    site_id='m'; site_name='m'; base_url='https://m.test'\n"
             "    def crawl(self, limit=None):\n        return MAX_PAGES\n", module.__dict__)
        sys.modules[module.__name__] = module
        try:
            crawler = module.ModCrawler(None)
            self.assertEqual(legacy.apply_overrides(crawler, {"page_cap": 750}), {"module.MAX_PAGES": (200, 750)})
            self.assertEqual(crawler.crawl(), 750)
        finally:
            del sys.modules[module.__name__]

    def test_start_page_override_for_generic_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            crawler = GenericCrawler(generic_config(tmp), None)
            changed = legacy.apply_overrides(crawler, {"start_page": 0})
            self.assertEqual(changed, {"pagination.start": (1, 0)})
            self.assertEqual(crawler._config["list_page"]["pagination"]["start"], 0)

    def test_worker_hook_reads_plan_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"version": 1, "sites": {"capped": {"strategy": "sitemap",
                                                                          "legacy": {"page_cap": 1200}}}}))
            crawler = Capped(None)
            legacy.apply_overrides_from_env(crawler, environ={legacy.ENV_PLAN: str(plan)})
            self.assertEqual(crawler.MAX_PAGES, 1200)

    def test_generic_crawler_does_not_double_encode_filters(self):
        seen = []

        class Session:
            headers = {}

            def request(self, method, url, params=None, **kw):
                seen.append(requests.Request(method, url, params=params).prepare().url)
                return types.SimpleNamespace(raise_for_status=lambda: None, text="<html></html>",
                                             encoding=None, status_code=200)

        with tempfile.TemporaryDirectory() as tmp:
            crawler = GenericCrawler(generic_config(tmp), None)
            crawler._session = Session()
            crawler.crawl()
        self.assertEqual(seen, ["https://gen.test/list?f%5B0%5D=lang%3Aen&type%5B170%5D=170&page=1"])


class PlanValidationTest(unittest.TestCase):
    def test_interval_minimum_and_strategy(self):
        base = {"strategy": "last_page_backward", "listing": {"url": "https://x.test/l"}}
        with self.assertRaises(PlanError):
            site_plan({"sites": {"s": dict(base, request_interval_s=0.5)}}, "s")
        with self.assertRaises(PlanError):
            site_plan({"sites": {"s": dict(base, strategy="newest_200_pages")}}, "s")
        plan = site_plan({"sites": {"s": base}}, "s")
        self.assertEqual((plan.get("request_interval_s"), plan.max_pages), (1.5, None))

    def test_fingerprint_ignores_budgets_but_not_listing(self):
        base = {"strategy": "last_page_backward", "listing": {"url": "https://x.test/l"}}
        a = site_plan({"sites": {"s": base}}, "s").fingerprint
        b = site_plan({"sites": {"s": dict(base, max_requests=5)}}, "s").fingerprint
        c = site_plan({"sites": {"s": dict(base, listing={"url": "https://x.test/l2"})}}, "s").fingerprint
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_plan_change_restarts_segments_but_keeps_ledger(self):
        from tests.enumeration_fakes import Env, board_plan
        env = Env()
        try:
            board = FakeBoard(15)
            env.run(board_plan(), board, max_requests=10)
            saved = len(env.docs())
            self.assertGreater(saved, 0)
            changed = board_plan(listing={"params": {"sort": "new"}})
            result = env.run(changed, board)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(len(env.docs()), 15)
            self.assertTrue(env.store.events(env.site_id, "plan_changed"))
            self.assertEqual(result["stats"]["saved_new"], 15 - saved)
        finally:
            env.close()


class LoopbackBoardServer:
    def __init__(self, board):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                resp = board(f"http://127.0.0.1:{outer.port}{self.path}")
                self.send_response(resp.status)
                for name, value in resp.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                self.wfile.write(resp.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class CliEndToEndTest(unittest.TestCase):
    def test_cli_run_and_report_over_loopback(self):
        board = FakeBoard(6, page_size=4, end_mode="empty")
        with LoopbackBoardServer(board) as srv, tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "libertree.db"
            conn = db_libertree.open_db(db)
            db_libertree.init_db(conn)
            db_libertree.upsert_site(conn, "cli-site", "cli", f"http://127.0.0.1:{srv.port}")
            conn.close()
            plan = tmp / "plan.json"
            plan.write_text(json.dumps({"version": 1, "defaults": {"request_interval_s": 1.5}, "sites": {"cli-site": {
                "strategy": "last_page_backward",
                "listing": {"url": f"http://127.0.0.1:{srv.port}/list", "page_param": "page", "start_page": 1,
                            "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                                       "date": "td.date", "id_regex": r"id=(\d+)"},
                            "total": {"type": "regex", "pattern": r"총\s*(\d+)\s*건"}},
                "adapter": {"type": "list_only", "id_regex": r"id=(\d+)"}}}}))
            self.assertEqual(load_plan(plan)["version"], 1)
            out = io.StringIO()
            began = __import__("time").monotonic()
            with redirect_stdout(out):
                code = main(["--plan", str(plan), "--site", "cli-site", "--state-dir", str(tmp / "state"),
                             "--db", str(db)])
            elapsed = __import__("time").monotonic() - began
            self.assertEqual(code, 0)
            result = json.loads(out.getvalue().strip().splitlines()[-1])[0]
            self.assertEqual(result["status"], "complete", result)
            # robots, page 1, bare page, page 2 probe, page 2..1 backward with overlap: >= 6 gated requests.
            self.assertGreaterEqual(result["requests"], 6)
            self.assertGreaterEqual(elapsed, 1.5 * (result["requests"] - 1) - 0.1)
            conn = db_libertree.open_db(db)
            ids = [r[0] for r in conn.execute("SELECT post_number FROM documents ORDER BY seq_id")]
            conn.close()
            self.assertEqual(ids, ["1", "2", "3", "4", "5", "6"])
            report = json.loads((tmp / "state" / "reports" / "completeness.json").read_text())["sites"][0]
            self.assertEqual((report["listed_unique"], report["independent_total"], report["gap"]), (6, 6, 0))
            self.assertTrue((tmp / "state" / "reports" / "completeness.csv").exists())


if __name__ == "__main__":
    unittest.main()
