"""Framework extensions (X3): blank-body end, second-step link params/rewrite, list position,
multi-level discovery, shared post numbers, pdf_text retry, response size cap, rule skips. No network."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.plan import PlanError  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, board_plan, html  # noqa: E402
from tests.test_enumeration_extensions import RecordingTransport, api_items, api_plan, json_resp  # noqa: E402
from tests.test_enumeration_extensions_x2 import make_crawler, make_pdf  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def bad(self, site):
        with self.assertRaises(PlanError):
            self.env.plan(site)


def contract_site(**adapter):
    site = board_plan(listing={"parser": {"type": "adapter", "listing_marker": "tbody"}})
    site["adapter"] = {"type": "crawler", "contract_version": 1, "list_method": "listing", "detail_method": "detail",
                       **adapter}
    return site


def listing(page_html):
    from bs4 import BeautifulSoup
    return [{"url": a["href"], "external_id": a["href"].split("=")[1], "title": a.get_text()}
            for a in BeautifulSoup(page_html, "html.parser").select("tbody a")]


def crawler(env, detail):
    c = make_crawler(env, detail)
    c.listing = listing
    return c


class BlankBodyEndTest(Base):
    def handler(self, ids, page_size=3):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            page = int(parse_qs(parts.query)["page"][0])
            chunk = ids[(page - 1) * page_size: page * page_size]
            if not chunk:
                return Response(200, {"Content-Type": "application/json"}, b"  \n")
            return json_resp({"items": api_items(chunk)})
        return handle

    def test_blank_200_is_the_end_only_when_declared(self):
        result = self.env.run(api_plan(listing={"end_on_blank_body": True}), None,
                              transport=RecordingTransport(self.handler(list(range(1, 8)))))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 7)
        env2 = Env()
        self.addCleanup(env2.close)
        result = env2.run(api_plan(), None, transport=RecordingTransport(self.handler(list(range(1, 8)))))
        self.assertNotEqual(result["status"], "complete", result)  # undeclared: a parse error, not an end
        self.bad(api_plan(listing={"end_on_blank_body": "yes"}))


class SecondStepLinkTest(Base):
    def handler(self, seen):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/view":
                i = parse_qs(parts.query)["id"][0]
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<a class="cta" href="http://board.test/record/{i}">record</a>'
                                     f'<link type="application/json" href="/wp-json/wp/v2/posts/{i}">'))
            if parts.path.startswith(("/record/", "/wp-json/")):
                seen.append(url)
                return json_resp({"id": parts.path.rsplit("/", 1)[1]})
            return FakeBoard(3)(url)
        return handle

    @staticmethod
    def detail(data, item):
        return {"external_id": item["external_id"], "url": item["url"], "title": "t" + str(data["id"])}

    def test_fixed_parameters_are_added(self):
        seen = []
        site = contract_site(detail_args=["json", "item"], second_step={
            "use": "detail_page", "selector": "link[type='application/json']", "add_params": {"_embed": "1"}})
        result = self.env.run(site, None, crawler=crawler(self.env, self.detail),
                              transport=RecordingTransport(self.handler(seen)))
        self.assertEqual(result["status"], "complete", result)
        self.assertIn("https://board.test/wp-json/wp/v2/posts/1?_embed=1", seen)

    def test_link_is_rewritten_into_the_export_address(self):
        seen = []
        site = contract_site(detail_args=["json", "item"], second_step={
            "use": "detail_page", "selector": "a.cta",
            "rewrite": {"regex": r"/record/(\d+)$", "template": "https://board.test/record/{1}?of=xm"}})
        result = self.env.run(site, None, crawler=crawler(self.env, self.detail),
                              transport=RecordingTransport(self.handler(seen)))
        self.assertEqual(result["status"], "complete", result)
        self.assertIn("https://board.test/record/2?of=xm", seen)
        self.assertFalse([u for u in seen if u.startswith("http://")])

    def test_validation(self):
        step = {"use": "detail_page", "selector": "a"}
        self.bad(contract_site(second_step=dict(step, add_params={"a b": "1"})))
        self.bad(contract_site(second_step=dict(step, rewrite={"regex": "(x)", "template": "https://h/{2}"})))
        self.bad(contract_site(second_step=dict(step, rewrite={"regex": "(x)", "template": "/rel/{1}"})))
        self.bad(contract_site(second_step=dict(step, rewrite={"regex": "(x)", "template": "https://h/{1}{x}"})))


class ListPositionTest(Base):
    def test_page_and_position_reach_the_method_and_survive_a_retry(self):
        seen = []

        def detail(html_text, pos, item):
            i = item["external_id"]
            seen.append((int(i), pos["page"], pos["position"]))
            return {"external_id": i, "url": item["url"], "title": "t"}
        board = FakeBoard(12, page_size=5)
        board.detail_status = {2: [503, 503, 503, 503, 503, 503]}
        site = contract_site(detail_args=["html", "list_position", "item"])
        result = self.env.run(site, board, crawler=crawler(self.env, detail))
        self.assertIn(result["status"], ("complete", "incomplete"), result)
        by_id = {i: (p, n) for i, p, n in seen}
        # Newest first, 5 per page: id 12 is page 1 position 1, id 7 page 2 position 1, id 1 page 3 position 2.
        self.assertEqual(by_id[12], (1, 1))
        self.assertEqual(by_id[7], (2, 1))
        self.assertEqual(by_id[1], (3, 2))
        self.assertNotIn(2, by_id)  # still failing transiently
        board.detail_status = {}
        self.env.run(site, board, crawler=crawler(self.env, detail), retry_only=True)
        self.assertEqual({i: (p, n) for i, p, n in seen}[2], (3, 1))  # restored from the ledger

    def test_not_for_the_listing_method(self):
        self.bad(contract_site(list_args=["list_position"]))


class MultiLevelDiscoverTest(Base):
    def handler(self):
        pages = {"/archive": '<a class="j" href="/journal/oil">Oil Shale</a><a class="j" href="/journal/est">Estonian</a>',
                 "/journal/oil": '<a class="y" href="/journal/oil/2022">2022</a><a class="issue" href="/issue/1129">No 1</a>',
                 "/journal/oil/2022": '<a class="issue" href="/issue/1130">No 2</a>',
                 "/journal/est": '<a class="issue" href="/issue/900">No 1</a>'}

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path in pages:
                return Response(200, {"Content-Type": "text/html"}, html(pages[parts.path]))
            if parts.path == "/search":
                q = parse_qs(parts.query)
                issue, page = int(q["issue"][0]), int(q["page"][0])
                return json_resp({"items": api_items([issue % 100 + 1]) if page == 1 else []})
            return Response(404, {}, b"")
        return handle

    def site(self, **follow):
        site = api_plan(strategy="date_range_split")
        site["segments"] = {"mode": "discover", "hub_url": "https://api.test/archive", "selector": "a.issue",
                            "key_regex": r"/issue/(\d+)", "params": {"issue": "{key}"},
                            "follow": {"selector": "a.j, a.y", "max_depth": 2, **follow}}
        return site

    def test_segments_below_sub_hubs_are_found_with_their_path(self):
        result = self.env.run(self.site(), None, transport=RecordingTransport(self.handler()))
        self.assertIn(result["status"], ("complete", "complete_unverified"), result)
        state = self.env.store.site(self.env.site_id)["state"]["discovered_segments"]
        self.assertEqual(sorted(state), ["1129", "1130", "900"])
        self.assertEqual(state["1130"]["path"], ["Oil Shale", "2022"])

    def test_page_cap_is_incomplete(self):
        result = self.env.run(self.site(max_pages=2), None, transport=RecordingTransport(self.handler()))
        self.assertEqual(result["status"], "incomplete", result)
        self.assertIn("hub_pages_cap", result["stop_reason"])

    def test_validation(self):
        self.bad(self.site(max_depth=9))
        self.bad(self.site(other=1))


class SharedPostNumberTest(Base):
    def test_language_editions_share_a_post_number_when_declared(self):
        def detail(html_text, item):
            i = int(item["external_id"])
            return {"external_id": f"RS-{(i + 1) // 2}", "url": f"https://board.test/files/{i}.pdf", "title": "t"}
        site = contract_site(allow_shared_post_number=True)
        result = self.env.run(site, FakeBoard(4), crawler=crawler(self.env, detail))
        # Saved like the crawler did, but the buyer's duplicate check counts them: a person decides.
        self.assertEqual(result["status"], "complete_with_review", result)
        self.assertIn("shared_post_number_rows:2", result["stop_reason"])
        self.assertEqual(sorted(d["post_number"] for d in self.env.docs()), ["RS-1", "RS-1", "RS-2", "RS-2"])
        env2 = Env()
        self.addCleanup(env2.close)
        env2.run(contract_site(), FakeBoard(4), crawler=crawler(env2, detail))
        self.assertEqual(len(env2.docs()), 2)  # undeclared: the second edition is an identity collision
        self.bad(contract_site(allow_shared_post_number="yes"))


class RuleSkipTest(Base):
    @staticmethod
    def detail(html_text, item):
        if int(item["external_id"]) % 2:
            return None  # the crawler's rule, e.g. abstract under 50 characters
        return {"external_id": item["external_id"], "url": item["url"], "title": "t"}

    def test_rule_skips_are_not_failures(self):
        result = self.env.run(contract_site(none_is_rule_skip=True), FakeBoard(6), crawler=crawler(self.env, self.detail))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["stats"]["skipped_by_rule"], 3)
        env2 = Env()
        self.addCleanup(env2.close)
        result = env2.run(contract_site(), FakeBoard(6), crawler=crawler(env2, self.detail))
        self.assertEqual(result["status"], "complete_with_review", result)  # undeclared: contract failures


@unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
class PdfRetryAndSizeTest(Base):
    def test_retry_args_used_when_first_gives_no_text(self):
        from crawler.enumeration.pdftext import extract, validate
        pdf = make_pdf("Hello retry")
        spec = {"args": ["-f", "5", "-l", "5"], "retry_args": ["-raw"]}  # page 5 does not exist -> no text
        validate(spec)
        self.assertIn("Hello retry", extract(pdf, spec))
        self.assertIsNone(extract(pdf, {"args": ["-f", "5", "-l", "5"]}) or None)

    def test_response_size_cap(self):
        from crawler.enumeration.run import make_fetcher
        plan = self.env.plan(api_plan(max_response_bytes=80 << 20))
        fetcher = make_fetcher(plan, self.env.root)
        self.assertEqual(fetcher.transport.max_bytes, 80 << 20)
        self.bad(api_plan(max_response_bytes=10))
        self.bad(api_plan(max_response_bytes=900 << 20))


if __name__ == "__main__":
    unittest.main()
