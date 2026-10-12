"""Failure ledger, access stops, crawler adapters, file pass and report (no network)."""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.enumeration.files import FilePass  # noqa: E402
from crawler.enumeration.http import Response, TransportError  # noqa: E402
from crawler.enumeration.report import site_report, write_report  # noqa: E402
from tests.enumeration_fakes import PDF, Env, FakeBoard, FakeTransport, board_plan, html  # noqa: E402


class FakeCustomCrawler(BaseCrawler):
    """Stands in for a sites/custom crawler: its own parsers, its own curl fetches."""
    site_id = "fake-site"
    site_name = "Fake custom"
    base_url = "https://board.test"
    fail_save_ids = ()

    def crawl(self, limit=None):
        raise NotImplementedError

    def _parse_list_page(self, raw):
        return [{"detail_url": f"/view?id={m}", "post_number": m, "title": f"Title {m}"}
                for m in re.findall(r'href="/view\?id=(\d+)"', raw)]

    def _parse_detail(self, raw, item):
        if item["post_number"] == "5":
            raise ValueError("unexpected layout")
        title = re.search(r'<h1 class="title">(.*?)</h1>', raw)
        return {"external_id": item["post_number"], "title": title.group(1) if title else "",
                "url": f"https://board.test/view?id={item['post_number']}", "pdf_url": "",
                "authors": "[]", "keywords": "[]", "metadata": "{}"}

    def _fetch_detail_with_curl(self, raw, item):
        subprocess.run(["curl", "https://board.test/"], check=False)

    def _save_paper(self, paper):
        if str(paper.get("external_id")) in self.fail_save_ids:
            raise RuntimeError("constraint violated")
        return super()._save_paper(paper)


def custom_plan(**adapter):
    site = board_plan(listing={"parser": {"type": "adapter", "key_field": "post_number"}})
    site["adapter"] = {"type": "crawler", "list_method": "_parse_list_page", "list_args": ["html"],
                       "detail_method": "_parse_detail", "detail_args": ["html", "item"], **adapter}
    return site


class FailureLedgerTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def ledger(self):
        return {r["item_key"]: r for r in self.env.store._all("SELECT * FROM enum_item WHERE site_id=?",
                                                               (self.env.site_id,))}

    def test_missing_items_are_ledgered_and_run_continues(self):
        board = FakeBoard(25)
        board.detail_status = {3: 404, 7: 410, 12: 400}
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 22)
        ledger = self.ledger()
        self.assertEqual({k: ledger[k]["reason"] for k in ("3", "7", "12")},
                         {"3": "http_404", "7": "http_410", "12": "http_4xx"})
        _, failed = self.env.store.item_counts(self.env.site_id)
        self.assertEqual(failed, {"failed_permanent:http_404": 1, "failed_permanent:http_410": 1,
                                  "failed_permanent:http_4xx": 1})

    def test_transient_failure_retried_next_run(self):
        board = FakeBoard(15)
        board.detail_status = {4: [503, 503, 503], 9: [TransportError("ConnectionError")] * 3}
        first = self.env.run(board_plan(), board)
        self.assertEqual(first["status"], "complete", first)
        ledger = self.ledger()
        self.assertEqual(ledger["4"]["status"], "failed_transient")
        self.assertEqual(ledger["4"]["reason"], "transient:http_503")
        self.assertEqual(ledger["9"]["reason"], "transient:transport:ConnectionError")
        self.assertEqual(len(self.env.docs()), 13)
        second = self.env.run(board_plan(), board)  # completed site: retry pass only
        self.assertEqual(second["stop_reason"], "already_complete_retry_only")
        self.assertEqual(second["stats"]["retried"], 2)
        self.assertEqual(self.ledger()["4"]["status"], "saved")
        self.assertEqual(len(self.env.docs()), 15)

    def test_transient_retries_are_bounded(self):
        board = FakeBoard(5)
        board.detail_status = {2: 502}
        for _ in range(4):
            self.env.run(board_plan(item_max_attempts=3), board)
        row = self.ledger()["2"]
        self.assertEqual((row["status"], row["attempts"]), ("failed_retries_exhausted", 3))

    def test_access_refusal_stops_site_without_retry(self):
        board = FakeBoard(25)
        board.detail_status = {6: 403}
        transport = FakeTransport(board)
        result = self.env.run(board_plan(), board, transport=transport)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "access_refused"))
        self.assertTrue(transport.urls[-1].endswith("/view?id=6"))
        self.assertEqual(sum(u.endswith("id=6") for u in transport.urls), 1)  # never retried
        self.assertEqual(len(self.env.docs()), 5)  # items 1..5 saved before the refusal are kept
        self.assertIsNone(self.env.store.site(self.env.site_id)["completed_at"])
        # The refusal is held: a later run makes no request until reviewed (--refresh).
        again = FakeTransport(board)
        held = self.env.run(board_plan(), board, transport=again)
        self.assertEqual((held["status"], held["stop_reason"]), ("access_blocked", "access_hold_requires_review"))
        self.assertEqual(again.urls, [])

    def test_challenge_page_is_access_blocked(self):
        board = FakeBoard(25)
        board.detail_override = {2: html("<div>Just a moment...</div>", title="Just a moment...")}
        result = self.env.run(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("access_blocked", "challenge:browser_security_challenge"))

    def test_login_form_listing_is_access_blocked(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            return Response(200, {"Content-Type": "text/html"},
                            html('<form><input name="user"><input type="password" name="pw"></form>'))
        result = self.env.run(board_plan(), handler)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "login_form"))

    def test_robots_disallow_is_not_bypassed(self):
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /view\n")
        transport = FakeTransport(board)
        result = self.env.run(board_plan(), board, transport=transport)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "robots_disallowed"))
        self.assertFalse([u for u in transport.urls if "/view" in u])

    def test_custom_crawler_parsers_reused_and_parse_error_ledgered(self):
        board = FakeBoard(12)
        crawler = FakeCustomCrawler(self.env.conn)
        result = self.env.run(custom_plan(), board, crawler=crawler)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.ledger()["5"]["reason"], "parse_error:ValueError")
        docs = self.env.docs()
        self.assertEqual([int(d["post_number"]) for d in docs], [i for i in range(1, 13) if i != 5])
        self.assertEqual(crawler.delivery_mode, "backfill")

    def test_single_save_error_does_not_block(self):
        board = FakeBoard(12)
        crawler = FakeCustomCrawler(self.env.conn)
        crawler.fail_save_ids = ("3",)
        result = self.env.run(custom_plan(), board, crawler=crawler)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.ledger()["3"]["reason"], "save_error:RuntimeError")
        self.assertEqual(len(self.env.docs()), 10)  # 12 minus parse error (5) minus save error (3)

    def test_consecutive_save_errors_stop_site(self):
        board = FakeBoard(12)
        crawler = FakeCustomCrawler(self.env.conn)
        crawler.fail_save_ids = tuple(str(i) for i in range(1, 13))
        result = self.env.run(custom_plan(), board, crawler=crawler)
        self.assertEqual(result["status"], "config_error")
        self.assertTrue(result["stop_reason"].startswith("save_failures_consecutive"))

    def test_ungated_network_in_reused_method_is_refused(self):
        board = FakeBoard(3)
        crawler = FakeCustomCrawler(self.env.conn)
        result = self.env.run(custom_plan(detail_method="_fetch_detail_with_curl"), board, crawler=crawler)
        self.assertEqual(result["status"], "config_error")
        self.assertTrue(result["stop_reason"].startswith("adapter_ungated_network:subprocess"))
        self.assertIs(sys.modules[__name__].subprocess, subprocess)  # restored after the run


class FilePassTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_file_ledger_downloads_verifies_and_retries(self):
        board = FakeBoard(6)
        self.env.run(board_plan(), board)
        files = self.env.store.files(self.env.site_id)
        self.assertEqual(len(files), 6)
        self.assertTrue(all(f["status"] == "pending" for f in files))
        attempts = {"/files/3.pdf": [Response(503, {}, b"")] * 3}

        def handler(url):
            for suffix, queue in attempts.items():
                if url.endswith(suffix) and queue:
                    return queue.pop(0)
            if url.endswith("/files/2.pdf"):
                return Response(200, {"Content-Type": "application/pdf"}, html("<form>login</form>").encode())
            if url.endswith("/files/4.pdf"):
                return Response(404, {}, b"")
            return board(url)

        plan = self.env.plan(board_plan())
        blob = self.env.root / "blobs"
        fp = FilePass(self.env.store, self.env.fetcher(plan, FakeTransport(handler)), blob_root=blob,
                      doc_conn=self.env.conn, max_attempts=3)
        stats = fp.run(self.env.site_id)
        self.assertEqual((stats["done"], stats["failed"], stats["failed_transient"]), (3, 2, 1))
        rows = {f["url"].rsplit("/", 1)[1]: f for f in self.env.store.files(self.env.site_id)}
        self.assertEqual(rows["2.pdf"]["reason"], "html_instead_of_pdf")
        self.assertEqual(rows["4.pdf"]["reason"], "http_404")
        self.assertEqual(rows["1.pdf"]["pages"], 2)
        self.assertEqual(rows["1.pdf"]["size"], len(PDF))
        doc = self.env.conn.execute("SELECT pdf_downloaded, pdf_sha256 FROM documents WHERE post_number='1'").fetchone()
        self.assertEqual((doc[0], doc[1]), (1, rows["1.pdf"]["sha256"]))
        self.assertTrue(os.path.exists(rows["1.pdf"]["path"]))
        stats = fp.run(self.env.site_id)  # second pass retries only the transient one
        self.assertEqual(stats["done"], 1)
        self.assertEqual(self.env.store.file_counts(self.env.site_id), {"done": 4, "failed": 2})


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_report_compares_with_independent_evidence(self):
        board = FakeBoard(25)
        board.detail_status = {3: 404, 8: 404, 9: [503, 503, 503]}
        self.env.run(board_plan(), board)
        row = site_report(self.env.store, self.env.site_id)
        self.assertEqual(row["status"], "complete")
        self.assertEqual((row["listed_unique"], row["saved"], row["failed_total"]), (25, 22, 3))
        self.assertEqual(row["failed_by_reason"], {"failed_permanent:http_404": 2,
                                                   "failed_transient:transient:http_503": 1})
        self.assertEqual((row["independent_total"], row["gap"]), (25, 0))
        self.assertEqual(row["independent_evidence"], ["source_reported_total"])
        self.assertEqual(row["oldest_item_date"], "2000-01-02")
        out = self.env.root / "report"
        write_report(self.env.store, [self.env.site_id, "never-run"], out)
        data = json.loads((out / "completeness.json").read_text())
        self.assertEqual([s["status"] for s in data["sites"]], ["complete", "not_started"])
        with (out / "completeness.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["failed_by_reason"],
                         "failed_permanent:http_404=2;failed_transient:transient:http_503=1")


if __name__ == "__main__":
    unittest.main()
