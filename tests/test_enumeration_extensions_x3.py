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

    def test_existing_parameter_or_fragment_gives_no_link(self):
        from crawler.enumeration.adapters import Adapter
        a = Adapter("fake-site", {"type": "selectors", "selectors": {"title": "h1"},
                                  "second_step": {"use": "detail_page", "selector": "a", "add_params": {"_embed": "1"}}},
                    None, base_url="https://board.test")

        class R:
            url = "https://board.test/view?id=1"

            def __init__(self, href):
                self.href = href

            def text(self):
                return html(f'<a href="{self.href}">x</a>')
        self.assertIsNone(a._step_link(R("/x?_embed=0")))
        self.assertIsNone(a._step_link(R("/#/record/5")))
        self.assertEqual(a._step_link(R("/x?y=1")), "https://board.test/x?y=1&_embed=1")
        b = Adapter("fake-site", {"type": "selectors", "selectors": {"title": "h1"}, "second_step": {
            "use": "detail_page", "selector": "a", "rewrite": {"regex": r"/rec/([^/?]+)", "template": "https://board.test/r/{1}"}}},
            None, base_url="https://board.test")
        self.assertEqual(b._step_link(R("/rec/a%20b")), "https://board.test/r/a%20b")  # not double-encoded
        js = Adapter("fake-site", {"type": "selectors", "selectors": {"title": "h1"}, "second_step": {
            "use": "detail_page", "selector": "a", "rewrite": {
                "regex": r"^javascript:downloadFile\('([A-Za-z0-9+/=]+)','(\d+)'\)",
                "template": "https://board.test/FileDownload.do?fileNo={1}&fileSeq={2}"}}},
            None, base_url="https://board.test")
        self.assertEqual(js._step_link(R("javascript:downloadFile('a+b=','2')")),
                         "https://board.test/FileDownload.do?fileNo=a%2Bb%3D&fileSeq=2")
        self.assertIsNone(js._step_link(R("javascript:alert(1)")))  # no match: no link
        self.assertEqual(js._step_link(R("javascript:downloadFile('Lw==','1')")),
                         "https://board.test/FileDownload.do?fileNo=Lw%3D%3D&fileSeq=1")
        js2 = Adapter("fake-site", {"type": "selectors", "selectors": {"title": "h1"}, "second_step": {
            "use": "detail_page", "selector": "a", "rewrite": {
                "regex": r"^javascript:f\('([^']+)'\)", "template": "https://board.test/d/{1}"}}},
            None, base_url="https://board.test")
        self.assertEqual(js2._step_link(R("javascript:f('%2F..%2F@evil.test')")),
                         "https://board.test/d/%2F..%2F%40evil.test")  # stays one path segment on the host
        self.assertIsNone(js2._step_link(R("javascript:f('..')")))  # dot segment refused
        self.assertIsNone(js._step_link(R("mailto:a@b.test")))
        self.assertIsNone(a._step_link(R("javascript:downloadFile('x','1')")))  # no rewrite: dropped as before

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

    def test_page_cap_reads_what_was_found_but_is_not_complete(self):
        result = self.env.run(self.site(max_pages=3), None, transport=RecordingTransport(self.handler()))
        self.assertEqual(result["status"], "complete_unverified", result)
        self.assertIn("hub_pages_cap", result["stop_reason"])

    def test_dead_sub_hub_and_downgrade_link(self):
        base = self.handler()
        seen = []

        def handle(method, url, data):
            seen.append(url)
            path = urlsplit(url).path
            if path == "/journal/est":
                return Response(404, {}, b"")
            if path == "/archive":
                return Response(200, {"Content-Type": "text/html"}, html(
                    '<a class="j" href="/journal/oil">Oil</a><a class="j" href="/journal/est">Est</a>'
                    '<a class="j" href="http://api.test/journal/plain">Plain</a>'))
            return base(method, url, data)
        result = self.env.run(self.site(), None, transport=RecordingTransport(handle))
        self.assertEqual(result["status"], "complete_unverified", result)
        self.assertIn("sub_hub_failed:404", result["stop_reason"])
        self.assertFalse([u for u in seen if u.startswith("http://")])
        state = self.env.store.site(self.env.site_id)["state"]["discovered_segments"]
        self.assertEqual(sorted(state), ["1129", "1130"])

    def test_path_reaches_the_method(self):
        got = []

        def paper(item, segment):
            got.append(segment.get("path"))
            return {"external_id": str(item["id"]), "url": item["url"], "title": item["title"]}
        site = self.site()
        site["adapter"] = {"type": "crawler", "contract_version": 1, "list_source": "json", "list_result": ["items"],
                           "paper_method": "paper", "paper_args": ["item", "segment"]}
        site["listing"]["parser"] = {"type": "adapter"}
        c = make_crawler(self.env, None)
        c.paper = paper
        self.env.run(site, None, crawler=c, transport=RecordingTransport(self.handler()))
        self.assertIn(["Oil Shale", "2022"], got)

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
        # A later pass that adds nothing still reports every shared row (the buyer sees all of them).
        result = self.env.run(site, FakeBoard(4), crawler=crawler(self.env, detail), refresh=True)
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
        site = contract_site(none_is_rule_skip=True, max_rule_skip_ratio=0.6)
        result = self.env.run(site, FakeBoard(6), crawler=crawler(self.env, self.detail))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["stats"]["skipped_by_rule"], 3)

    def test_too_many_rule_skips_need_review_or_stop(self):
        result = self.env.run(contract_site(none_is_rule_skip=True), FakeBoard(6), crawler=crawler(self.env, self.detail))
        self.assertEqual(result["status"], "complete_with_review", result)  # 50% > default 20%
        self.assertIn("skipped_by_rule:3", result["stop_reason"])
        env2 = Env()
        self.addCleanup(env2.close)
        result = env2.run(contract_site(none_is_rule_skip=True, max_consecutive_rule_skips=4), FakeBoard(12),
                          crawler=crawler(env2, lambda h, item: None))  # a broken parser, not a rule
        self.assertEqual(result["status"], "config_error", result)
        self.assertIn("rule_skips_consecutive", result["stop_reason"])

    def test_a_non_dict_result_is_not_a_rule_skip(self):
        site = board_plan()
        site["adapter"] = {"type": "crawler", "detail_method": "detail", "none_is_rule_skip": True,
                           "detail_args": ["html", "item"]}
        result = self.env.run(site, FakeBoard(3), crawler=make_crawler(self.env, lambda h, item: ["not", "a", "dict"]))
        self.assertNotIn("skipped_by_rule", result["stats"])
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


@unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
class MissingPdfAsNoTextTest(Base):
    def test_declared_404_on_the_pdf_reaches_the_fallback_rule(self):
        got = []

        def detail(text, item):
            got.append(text)
            abstract = text.strip() if text else "description from the listing"
            return {"external_id": item["external_id"], "url": item["url"], "title": "t", "abstract": abstract}

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/view":
                i = parse_qs(parts.query)["id"][0]
                return Response(200, {"Content-Type": "text/html"}, html(f'<a class="next" href="/files/{i}.pdf">pdf</a>'))
            if parts.path == "/files/2.pdf":
                return Response(404, {}, b"")
            if parts.path.startswith("/files/"):
                return Response(200, {"Content-Type": "application/pdf"}, make_pdf("Abstract ok"))
            return FakeBoard(3)(url)
        site = contract_site(detail_args=["pdf_text", "item"], second_step={"use": "detail_page", "selector": "a.next"},
                             pdf_text={"missing_statuses": [404]})
        result = self.env.run(site, None, crawler=crawler(self.env, detail), transport=RecordingTransport(handle))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 3)
        self.assertIn(None, got)
        env2 = Env()
        self.addCleanup(env2.close)
        site2 = contract_site(detail_args=["pdf_text", "item"], second_step={"use": "detail_page", "selector": "a.next"},
                              pdf_text={})
        env2.run(site2, None, crawler=crawler(env2, detail), transport=RecordingTransport(handle))
        self.assertEqual(len(env2.docs()), 2)  # undeclared: the item fails as before
        self.bad(contract_site(detail_args=["pdf_text"], pdf_text={"missing_statuses": [500]}))


class DiscoverLinkAsUrlTest(Base):
    def test_each_found_link_is_its_segments_listing(self):
        pages = {"/archive": '<a class="issue" href="/oil/issue/1129/">Oil 1</a><a class="issue" href="/est/issue/900/">Est 1</a>'
                             '<a class="issue" href="https://other.test/x/issue/5/">Other</a>'}
        seen = []

        def handle(method, url, data):
            parts = urlsplit(url)
            seen.append(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path in pages:
                return Response(200, {"Content-Type": "text/html"}, html(pages[parts.path]))
            if "/issue/" in parts.path:
                page = int(parse_qs(parts.query).get("page", ["1"])[0])
                n = int(parts.path.rstrip("/").rsplit("/", 1)[1])
                return json_resp({"items": api_items([n % 100 + 1]) if page == 1 else []})
            return Response(404, {}, b"")
        site = api_plan(strategy="date_range_split")
        site["segments"] = {"mode": "discover", "hub_url": "https://api.test/archive", "selector": "a.issue",
                            "key_regex": r"/issue/(\d+)/", "link_as_url": True}
        result = self.env.run(site, None, transport=RecordingTransport(handle))
        self.assertIn(result["status"], ("complete", "complete_unverified"), result)
        self.assertIn("https://api.test/oil/issue/1129/?page=1", seen)
        self.assertIn("https://api.test/est/issue/900/?page=1", seen)
        self.assertFalse([u for u in seen if "other.test" in u])
        bad = api_plan(strategy="date_range_split", listing={"url": "https://api.test/s/{page}/"})
        bad["segments"] = dict(site["segments"])
        self.bad(bad)


if __name__ == "__main__":
    unittest.main()
