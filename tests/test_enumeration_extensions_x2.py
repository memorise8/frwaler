"""Framework extensions (X2): two-step detail keeping the first page, PDF text, nested POST page,
dynamic tokens, multiple listings, next link, dynamic GET ref, auxiliary detail request. No network."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.plan import PlanError  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, board_plan, html  # noqa: E402
from tests.test_enumeration_extensions import RecordingTransport, api_items, api_plan, json_resp  # noqa: E402


def make_crawler(env, detail):
    class Board(BaseCrawler):
        site_id = "fake-site"
        site_name = "fake-site"
        base_url = "https://board.test"

        def crawl(self, limit=None):
            pass

    c = Board(env.conn, 0)
    c.detail = detail
    return c


def crawler_plan(adapter):
    site = board_plan()
    site["adapter"] = {"type": "crawler", "detail_method": "detail", **adapter}
    return site


def make_pdf(text):
    """A one-page PDF whose text layer is ``text`` (ASCII), with a correct xref."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


STEP = {"use": "detail_page", "selector": "a.next"}


class KeepFirstTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def handler(self, board, link=True):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/view":
                i = int(parse_qs(parts.query)["id"][0])
                a = f'<a class="next" href="/record/{i}">full</a>' if link else ""
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<span class="date">2001-02-{i:02d}</span>{a}'))
            if parts.path.startswith("/record/"):
                i = int(parts.path.rsplit("/", 1)[1])
                return Response(200, {"Content-Type": "text/html"}, html(f'<h1>Title {i}</h1>'))
            return board(url)
        return handle

    @staticmethod
    def detail(second, first, url):
        from bs4 import BeautifulSoup
        url = url["url"] if isinstance(url, dict) else url
        return {"external_id": url.split("=")[1], "url": url, "title": BeautifulSoup(second, "html.parser").h1.text,
                "published_date": BeautifulSoup(first, "html.parser").select_one("span.date").text}

    def check(self, adapter, link=True):
        board = FakeBoard(4)
        result = self.env.run(crawler_plan(adapter), None, crawler=make_crawler(self.env, self.detail),
                              transport=RecordingTransport(self.handler(board, link)))
        return result, self.env.docs()

    def test_legacy_args_get_both_pages(self):
        result, docs = self.check({"detail_args": ["html", "first_html", "first_url"], "second_step": STEP})
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted((d["title"], d["published_date"]) for d in docs),
                         [(f"Title {i}", f"2001-02-{i:02d}") for i in range(1, 5)])
        self.assertTrue(all("/view?id=" in d["meta_url"] for d in docs))

    def test_contract_args_get_both_pages(self):
        def listing(page_html):
            from bs4 import BeautifulSoup
            return [{"url": a["href"], "external_id": a["href"].split("=")[1], "title": a.get_text()}
                    for a in BeautifulSoup(page_html, "html.parser").select("tbody a")]
        site = board_plan(listing={"parser": {"type": "adapter", "listing_marker": "tbody"}})
        site["adapter"] = {"type": "crawler", "contract_version": 1, "list_method": "listing",
                           "detail_method": "detail", "second_step": STEP,
                           "detail_args": ["html", "first_html", "item"]}
        c = make_crawler(self.env, self.detail)
        c.listing = listing
        result = self.env.run(site, None, crawler=c, transport=RecordingTransport(self.handler(FakeBoard(4))))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(d["published_date"] for d in self.env.docs()),
                         [f"2001-02-{i:02d}" for i in range(1, 5)])
        from crawler.enumeration.contracts import ContractError, validate_contract
        with self.assertRaises(ContractError):
            validate_contract({"contract_version": 1, "type": "crawler", "list_method": "listing",
                               "list_args": ["first_html"], "detail_method": "detail"})

    def test_optional_link_missing_first_is_the_page_itself(self):
        def detail(second, first, url):
            assert second == first
            return {"external_id": url.split("=")[1], "url": url, "title": "t"}
        board = FakeBoard(3)
        site = crawler_plan({"detail_args": ["html", "first_html", "item"], "second_step": dict(STEP, required=False)})
        site["adapter"]["detail_args"] = ["html", "first_html", "first_url"]
        result = self.env.run(site, None, crawler=make_crawler(self.env, detail),
                              transport=RecordingTransport(self.handler(board, link=False)))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 3)

    def test_first_tokens_need_a_second_detail_page(self):
        with self.assertRaises(PlanError):
            self.env.plan(crawler_plan({"detail_args": ["html", "first_html"]}))
        with self.assertRaises(PlanError):
            self.env.plan(crawler_plan({"detail_args": ["first_html"],
                                        "second_step": {"use": "pdf_url", "selector": "a"}}))
        result = self.env.run(crawler_plan({"list_method": "x", "list_args": ["first_html"], "second_step": STEP,
                                            "detail_args": ["html", "item"]}),
                              FakeBoard(1), crawler=make_crawler(self.env, self.detail))
        self.assertEqual(result["status"], "config_error", result)


@unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
class PdfTextTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def handler(self, board, bodies):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/view":
                i = int(parse_qs(parts.query)["id"][0])
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<h1>Title {i}</h1><a class="next" href="/files/{i}.pdf">pdf</a>'))
            if parts.path.startswith("/files/"):
                i = int(parts.path.rsplit("/", 1)[1].split(".")[0])
                return Response(200, {"Content-Type": "application/pdf"}, bodies.get(i, make_pdf(f"Abstract {i}")))
            return board(url)
        return handle

    def site(self, **pdf_text):
        return crawler_plan({"detail_args": ["pdf_text", "first_html", "first_url"], "second_step": STEP,
                             "pdf_text": pdf_text})

    def run_with(self, detail, site, bodies=None):
        board = FakeBoard(3)
        return self.env.run(site, None, crawler=make_crawler(self.env, detail),
                            transport=RecordingTransport(self.handler(board, bodies or {})))

    def test_text_reaches_the_method_and_subprocess_stays_outside_the_guard(self):
        seen = []

        def detail(text, first, url):
            from crawler.enumeration import guard
            self.assertTrue(guard.active())  # the method itself still runs under the guard
            seen.append(text)
            return {"external_id": url.split("=")[1], "url": url, "title": "t", "abstract": text.strip()}
        result = self.run_with(detail, self.site(args=["-layout"]))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(t.strip() for t in seen), [f"Abstract {i}" for i in range(1, 4)])

    def test_unreadable_or_short_body_gives_none(self):
        seen = []

        def detail(text, first, url):
            seen.append(text)
            return None if text is None else {"external_id": url.split("=")[1], "url": url, "title": "t"}
        self.run_with(detail, self.site(min_bytes=100), bodies={1: b"%PDF-short"})
        self.assertIn(None, seen)
        self.assertEqual(len(self.env.docs()), 2)

    def test_validation(self):
        with self.assertRaises(PlanError):  # option outside the allowed set
            self.env.plan(self.site(args=["-opw", "x"]))
        with self.assertRaises(PlanError):
            self.env.plan(self.site(args=["-enc", "Latin1"]))
        with self.assertRaises(PlanError):
            self.env.plan(self.site(timeout_s=600))
        with self.assertRaises(PlanError):  # config without the argument
            self.env.plan(crawler_plan({"detail_args": ["html"], "pdf_text": {}}))
        with self.assertRaises(PlanError):  # argument without the config
            self.env.plan(crawler_plan({"detail_args": ["pdf_text"]}))
        self.env.plan(self.site(args=["-f", "1", "-l", "5", "-enc", "UTF-8"], timeout_s=60))


NONCE, TOKEN = "n0nce-abc", "csrf-secret-123"


class NestedBodyDynamicTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def handler(self, ids, *, header_name="X-Csrf", check_dyn=True, page_size=3):
        self.dyn_requests = 0

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path == "/start":
                self.dyn_requests += 1
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<script>var FWP = {{"nonce": "{NONCE}"}};</script>'))
            if parts.path == "/csrf":
                self.dyn_requests += 1
                return json_resp({"headerName": header_name, "token": TOKEN})
            if parts.path == "/search" and method == "POST":
                body = json.loads(data)
                if check_dyn and body.get("nonce") != NONCE:
                    return Response(403, {}, b"bad nonce")
                page = body["search"]["page"]
                assert isinstance(page, int), body
                chunk = ids[(page - 1) * page_size: page * page_size]
                return json_resp({"items": api_items(chunk), "total": len(ids)})
            return Response(404, {}, b"")
        return handle

    def site(self, dynamic=None, **listing):
        out = {"method": "POST", "idempotent": True, "body_type": "json",
               "body": {"search": {"page": "{page}", "sort": "date asc"}, "filters": []},
               "total": {"type": "json", "path": "total"}, **listing}
        if dynamic is not None:
            out["dynamic"] = dynamic
        return api_plan(listing=out)

    def bad(self, site):
        with self.assertRaises(PlanError):
            self.env.plan(site)

    def test_page_value_goes_into_the_nested_body(self):
        transport = RecordingTransport(self.handler(list(range(1, 8)), check_dyn=False))
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual([int(d["post_number"]) for d in self.env.docs()], list(range(1, 8)))
        bodies = [json.loads(c[3]) for c in transport.calls if c[0] == "POST"]
        self.assertEqual(bodies[0], {"search": {"page": 1, "sort": "date asc"}, "filters": []})

    def test_declared_media_type(self):
        transport = RecordingTransport(self.handler([1, 2], check_dyn=False))
        self.env.run(self.site(content_type="versus/callback; charset=UTF-8"), None, transport=transport)
        posts = [c for c in transport.calls if c[0] == "POST"]
        self.assertTrue(posts)
        self.assertTrue(all(c[2]["Content-Type"] == "versus/callback; charset=UTF-8" for c in posts))

    def test_dynamic_values_from_a_public_page_and_header(self):
        listing_body = {"nonce": "{dyn:nonce}", "search": {"page": "{page}"}}
        site = self.site({"url": "https://api.test/start", "values": {"nonce": {"regex": r'"nonce":\s*"([^"]+)"'}}},
                         body=listing_body)
        transport = RecordingTransport(self.handler(list(range(1, 8))))
        result = self.env.run(site, None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 7)
        self.assertEqual(self.dyn_requests, 1)  # read once per run
        # The value never reaches the state store (events, cursors, ledger).
        self.env.store.conn.commit()
        dump = "\n".join(self.env.store.conn.iterdump())
        self.assertNotIn(NONCE, dump)

        csrf = self.site({"url": "https://api.test/csrf", "values": {"h": {"json_path": "headerName"},
                                                                     "t": {"json_path": "token"}},
                          "header": {"name": "{dyn:h}", "value": "{dyn:t}"}})
        env2 = Env()
        self.addCleanup(env2.close)
        transport = RecordingTransport(self.handler(list(range(1, 4)), check_dyn=False))
        result = env2.run(csrf, None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        posts = [c for c in transport.calls if c[0] == "POST"]
        self.assertTrue(posts and all(c[2].get("X-Csrf") == TOKEN for c in posts))
        self.assertFalse([c for c in transport.calls if c[0] != "POST" and "X-Csrf" in c[2]])

    def test_missing_value_or_forbidden_header_stops_with_the_name_only(self):
        site = self.site({"url": "https://api.test/start", "values": {"nonce": {"regex": r'"other":"([^"]+)"'}}},
                         body={"nonce": "{dyn:nonce}", "search": {"page": "{page}"}})
        result = self.env.run(site, None, transport=RecordingTransport(self.handler([1])))
        self.assertEqual(result["status"], "config_error", result)
        self.assertIn("dynamic_value_missing:nonce", result["stop_reason"])

        env2 = Env()
        self.addCleanup(env2.close)
        csrf = self.site({"url": "https://api.test/csrf", "values": {"h": {"json_path": "headerName"},
                                                                     "t": {"json_path": "token"}},
                          "header": {"name": "{dyn:h}", "value": "{dyn:t}"}})
        result = env2.run(csrf, None, transport=RecordingTransport(self.handler([1], header_name="Cookie",
                                                                               check_dyn=False)))
        self.assertEqual(result["status"], "config_error", result)
        self.assertNotIn(TOKEN, result["stop_reason"])

    def test_validation(self):
        two = {"a": "{page}", "b": "{page}"}
        self.bad(self.site(body=two))
        self.bad(self.site(body={"a": "p{page}"}))
        self.bad(self.site(body={"a": 1}))  # no page value
        self.bad(self.site(params={"q": 1}))  # body and params together
        self.bad(self.site(content_type="application/x-www-form-urlencoded"))
        self.bad(self.site(body={"n": "{dyn:x}", "p": "{page}"}))  # undeclared dynamic value
        start = {"url": "https://api.test/start", "values": {"x": {"regex": "(a)(b)"}}}
        self.bad(self.site(start, body={"n": "{dyn:x}", "p": "{page}"}))  # two groups
        other = {"url": "https://other.test/start", "values": {"x": {"regex": "(a)"}}}
        self.bad(self.site(other, body={"n": "{dyn:x}", "p": "{page}"}))  # other host
        lit = {"url": "https://api.test/start", "values": {"x": {"regex": "(a)"}},
               "header": {"name": "Cookie", "value": "{dyn:x}"}}
        self.bad(self.site(lit))
        self.bad(api_plan(listing={"body": {"p": "{page}"}}))  # GET listing


class MultiListingTest(unittest.TestCase):
    """ext:multi_listing: segments found on a hub page, or explicit segments with their own URLs."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    COLS = {"3698": ("2025", [1, 2, 3, 4]), "1570": ("1999", [5, 6])}

    def handler(self, cols, page_size=3):
        def handle(method, url, data):
            parts = urlsplit(url)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path == "/hub":
                links = "".join(f'<div class="column_list"><a href="/col/col{c}/index.html">{y}</a></div>'
                                for c, (y, _) in cols.items())
                return Response(200, {"Content-Type": "text/html"}, html(links + '<a href="/about">About</a>'))
            if parts.path == "/search":
                ids = cols[q["columnid"]][1]
                page = int(q["page"])
                return json_resp({"items": api_items(ids[(page - 1) * page_size: page * page_size]),
                                  "total": len(ids)})
            return Response(404, {}, b"")
        return handle

    def site(self, **seg):
        site = api_plan(strategy="date_range_split", listing={"total": {"type": "json", "path": "total"}})
        site["segments"] = {"mode": "discover", "hub_url": "https://api.test/hub", "selector": "div.column_list a",
                            "key_regex": r"/col/col(\d+)/", "params": {"columnid": "{key}"}, **seg}
        return site

    def test_discovered_segments_are_read_with_their_label(self):
        seen = []

        def paper(item, segment):
            seen.append((item["id"], segment["key"], segment["label"]))
            return {"external_id": str(item["id"]), "url": item["url"], "title": item["title"],
                    "category": f"Annual Report {segment['label']}"}
        site = self.site()
        site["adapter"] = {"type": "crawler", "contract_version": 1, "list_source": "json", "list_result": ["items"],
                           "paper_method": "paper", "paper_args": ["item", "segment"]}
        site["listing"]["parser"] = {"type": "adapter"}
        c = make_crawler(self.env, None)
        c.paper = paper
        result = self.env.run(site, None, crawler=c, transport=RecordingTransport(self.handler(self.COLS)))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(seen), [(i, "3698", "2025") for i in (1, 2, 3, 4)] + [(i, "1570", "1999") for i in (5, 6)])

    def test_a_segment_gone_from_the_hub_is_still_read_and_reported(self):
        result = self.env.run(self.site(), None, transport=RecordingTransport(self.handler(self.COLS)))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 6)
        transport = RecordingTransport(self.handler(self.COLS))
        orig = transport.handler

        def handle(method, url, data):  # the hub no longer links 1570; its listing still answers
            if urlsplit(url).path == "/hub":
                return Response(200, {"Content-Type": "text/html"},
                                html('<div class="column_list"><a href="/col/col3698/index.html">2025</a></div>'))
            return orig(method, url, data)
        transport.handler = handle
        result = self.env.run(self.site(), None, transport=transport, refresh=True)
        self.assertEqual(result["status"], "complete_unverified", result)
        self.assertIn("segments_not_rediscovered", result["stop_reason"])
        self.assertTrue([u for u in transport.urls if "columnid=1570" in u])

    def test_after_a_plan_change_a_ledger_segment_missing_from_the_hub_is_reported(self):
        self.env.run(self.site(), None, transport=RecordingTransport(self.handler(self.COLS)))
        result = self.env.run(self.site(label=None), None,  # another plan: no remembered segments
                              transport=RecordingTransport(self.handler({"3698": self.COLS["3698"]})))
        self.assertEqual(result["status"], "complete_unverified", result)
        self.assertIn("segments_not_rediscovered", result["stop_reason"])

    def test_too_few_segments_found_is_incomplete(self):
        result = self.env.run(self.site(min_segments=3), None, transport=RecordingTransport(self.handler(self.COLS)))
        self.assertEqual(result["status"], "incomplete", result)
        self.assertIn("segments_discovered_below_min:2", result["stop_reason"])

    def test_explicit_segments_with_their_own_listing_urls(self):
        pages = {("bachelorthesis", 1): [1, 2], ("bachelorthesis", 2): [3], ("phdthesis", 1): [4],
                 ("search", 1): [5, 6], ("search", 2): [7]}

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            bits = parts.path.strip("/").split("/")
            if bits[0] == "types":
                kind, page = bits[1], int(bits[3]) if len(bits) > 3 else 1
            elif bits[0] in ("page", ""):
                kind, page = "search", int(bits[1]) if bits[0] == "page" else 1
                assert parse_qs(parts.query)["s"] == ["pdf"], url
            else:
                return Response(404, {}, b"")
            ids = pages.get((kind, page), [])
            rows = "".join(f'<tr><td class="date">2000-01-0{i}</td><td><a href="/view?id={i}">T{i}</a></td></tr>'
                           for i in ids)
            return Response(200, {"Content-Type": "text/html"}, html(f"<table><tbody>{rows}</tbody></table>"))
        site = board_plan(strategy="date_range_split", adapter={"type": "list_only"})
        site["adapter"] = {"type": "list_only", "id_regex": r"id=(\d+)"}
        site["listing"] = {"url": "https://board.test/types/{segment}/page/{page}/",
                           "first_page_url": "https://board.test/types/{segment}/", "start_page": 1,
                           "parser": dict(site["listing"]["parser"], listing_marker="tbody")}
        site["segments"] = {"mode": "explicit", "items": [
            {"key": "bachelorthesis", "label": "bachelorthesis"}, {"key": "phdthesis"},
            {"key": "search", "url": "https://board.test/page/{page}/?s=pdf",
             "first_page_url": "https://board.test/?s=pdf"}]}
        transport = RecordingTransport(handle)
        result = self.env.run(site, None, transport=transport)
        self.assertIn(result["status"], ("complete", "complete_unverified"), result)
        self.assertEqual(sorted(int(d["post_number"]) for d in self.env.docs()), list(range(1, 8)))
        self.assertIn("https://board.test/types/bachelorthesis/", transport.urls)
        self.assertIn("https://board.test/?s=pdf", transport.urls)
        self.assertIn("https://board.test/page/2/?s=pdf", transport.urls)

    def test_validation(self):
        def bad(site):
            with self.assertRaises(PlanError):
                self.env.plan(site)
        bad(self.site(hub_url="https://other.test/hub"))
        bad(self.site(key_regex="col(\\d+)(x)"))
        bad(self.site(params={}))  # nothing tells the segments apart
        bad(self.site(label="script"))
        bad(self.site(min_segments=0))
        bad(self.site(**{"from": "2020"}))  # range keys do not apply
        site = api_plan(strategy="date_range_split")
        site["segments"] = {"mode": "explicit", "items": [{"key": "a", "url": "https://other.test/x"}]}
        bad(site)
        site["segments"] = {"mode": "explicit", "items": [{"key": "a"}, {"key": "a"}]}
        bad(site)
        site = self.site()
        site["adapter"] = {"type": "crawler", "contract_version": 1, "list_method": "rows", "list_args": ["segment"],
                           "list_returns_paper": True}
        bad(site)


class SmallExtensionsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def bad(self, site):
        with self.assertRaises(PlanError):
            self.env.plan(site)

    # -- ext:next_link -----------------------------------------------------
    def link_handler(self, slices, other_host=False):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path in ("/search", "/load"):
                n = int(parse_qs(parts.query).get("slice", ["0"])[0])
                host = "https://evil.test" if other_host else ""
                nxt = f"{host}/load?slice={n + 1}&cHash=sig{n + 1}" if n + 1 < len(slices) else None
                return json_resp({"items": api_items(slices[n]), "next": nxt})
            return Response(404, {}, b"")
        return handle

    def link_site(self):
        return api_plan(listing={"next_link": {"json_path": "next"}})

    def test_next_link_is_followed_to_the_end(self):
        transport = RecordingTransport(self.link_handler([[1, 2], [3, 4], [5]]))
        result = self.env.run(self.link_site(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(int(d["post_number"]) for d in self.env.docs()), [1, 2, 3, 4, 5])
        self.assertIn("https://api.test/load?slice=2&cHash=sig2", transport.urls)

    def test_next_link_to_another_host_stops(self):
        result = self.env.run(self.link_site(), None, transport=RecordingTransport(self.link_handler([[1], [2]], True)))
        self.assertEqual(result["status"], "config_error", result)
        self.assertIn("next_link_other_host", result["stop_reason"])

    def test_next_link_from_html(self):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            n = int(parse_qs(parts.query).get("slice", ["0"])[0])
            more = f'<a class="more" href="/list?slice={n + 1}">more</a>' if n < 2 else ""
            rows = f'<tr><td class="date">2000-01-0{n + 1}</td><td><a href="/view?id={n + 1}">T</a></td></tr>'
            return Response(200, {"Content-Type": "text/html"}, html(f"<table><tbody>{rows}</tbody></table>{more}"))
        site = board_plan(strategy="oldest_first_sort", adapter={"type": "list_only"})
        site["adapter"] = {"type": "list_only", "id_regex": r"id=(\d+)"}
        site["listing"] = {"url": "https://board.test/list", "parser": dict(site["listing"]["parser"]),
                           "next_link": {"selector": "a.more"}}
        result = self.env.run(site, None, transport=RecordingTransport(handle))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 3)

    def test_next_link_validation(self):
        self.bad(api_plan(strategy="last_page_backward", listing={"next_link": {"json_path": "next"}}))
        self.bad(api_plan(listing={"next_link": {"json_path": "next"}, "next_cursor": {"path": "c", "param": "c"}}))
        self.bad(api_plan(listing={"next_link": {"selector": "a", "json_path": "x"}}))

    # -- ext:dynamic_ref -------------------------------------------------------
    def test_dynamic_value_in_get_params(self):
        def handle(method, url, data):
            parts = urlsplit(url)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path == "/api/v2":
                return json_resp({"refs": {"master": "REF42"}})
            if parts.path == "/search":
                if q.get("ref") != "REF42":
                    return Response(400, {}, b"bad ref")
                page = int(q["page"])
                return json_resp({"items": api_items([1, 2, 3][(page - 1) * 2: page * 2])})
            return Response(404, {}, b"")
        site = api_plan(listing={"params": {"ref": "{dyn:ref}"},
                                 "dynamic": {"url": "https://api.test/api/v2", "values": {"ref": {"json_path": "refs.master"}}}})
        result = self.env.run(site, None, transport=RecordingTransport(handle))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 3)
        self.bad(api_plan(listing={"params": {"ref": "{dyn:ref}"}, "dynamic": {
            "url": "https://api.test/api/v2", "values": {"ref": {"json_path": "refs.master"}},
            "header": {"name": "X-Ref", "value": "{dyn:ref}"}}}))  # headers: POST only

    # -- ext:detail_aux_request ------------------------------------------------
    def test_second_request_built_from_item_fields(self):
        def listing(page_html):
            from bs4 import BeautifulSoup
            return [{"url": a["href"], "external_id": a["href"].split("=")[1], "page_id": "p/" + a["href"].split("=")[1]}
                    for a in BeautifulSoup(page_html, "html.parser").select("tbody a")]

        def detail(aux, first, item):
            return {"external_id": item["external_id"], "url": item["url"], "title": "t",
                    "metadata": json.dumps({"related": aux["links"], "h1": "Title" in first})}
        seen = []

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/aux":
                seen.append(parts.query)
                return json_resp({"links": [parse_qs(parts.query)["pageId"][0]]})
            return FakeBoard(3)(url)
        site = board_plan(listing={"parser": {"type": "adapter", "listing_marker": "tbody"}})
        site["adapter"] = {"type": "crawler", "contract_version": 1, "list_method": "listing", "detail_method": "detail",
                           "detail_args": ["json", "first_html", "item"],
                           "second_step": {"use": "detail_page", "url_template":
                                           "https://board.test/aux?pageType=article&pageId={item:page_id}"}}
        c = make_crawler(self.env, detail)
        c.listing = listing
        result = self.env.run(site, None, crawler=c, transport=RecordingTransport(handle))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 3)
        self.assertIn("pageType=article&pageId=p%2F1", seen)  # value URL-quoted
        bad = dict(site, adapter=dict(site["adapter"], second_step={"use": "detail_page", "url_template": "/aux?x={item:a}"}))
        self.bad(bad)
        bad = dict(site, adapter=dict(site["adapter"], second_step={"use": "detail_page",
                                                                   "url_template": "https://board.test/aux?x={a}"}))
        self.bad(bad)


if __name__ == "__main__":
    unittest.main()
