"""Framework extensions (X1): request headers, POST listings, path paging, JSON next-cursor and
last-page, 204 end, two-step detail. No network."""
from __future__ import annotations

import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.plan import PlanError, resolve_headers  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, board_plan, html  # noqa: E402


class RecordingTransport:
    """Like FakeTransport, but also records method, headers and body."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    @property
    def urls(self):
        return [c[1] for c in self.calls]

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, dict(headers or {}), data))
        result = self.handler(method, url, data)
        if isinstance(result, Exception):
            raise result
        return result


def json_resp(data, status=200, headers=None):
    return Response(status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(data))


def api_items(ids):
    return [{"id": i, "url": f"https://api.test/doc/{i}", "date": f"2020-01-{i:02d}", "title": f"Doc {i}"}
            for i in ids]


API_PARSER = {"type": "json", "items_path": "items", "key": "id", "url": "url", "date": "date", "title": "title"}


def api_plan(**over):
    site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
            "listing": {"url": "https://api.test/search", "page_param": "page", "start_page": 1,
                        "parser": dict(API_PARSER)}}
    for key, value in over.items():
        site[key] = {**site[key], **value} if isinstance(value, dict) and isinstance(site.get(key), dict) else value
    return site


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def go(self, site, handle, **kw):
        return self.env.run(site, None, transport=RecordingTransport(handle), **kw)

    def bad(self, site):
        with self.assertRaises(PlanError):
            self.env.plan(site)


class FingerprintTest(Base):
    def test_existing_plans_keep_their_fingerprint(self):
        # Values computed on integration-20261013 before the extensions.
        self.assertEqual(self.env.plan(board_plan()).fingerprint, "44f283c37d37a7c6")
        wp = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
              "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
        self.assertEqual(self.env.plan(wp).fingerprint, "4a0bb3b2b685e077")

    def test_headers_and_second_step_do_not_change_the_fingerprint(self):
        plain = self.env.plan(board_plan()).fingerprint
        more = self.env.plan(board_plan(request_headers={"Accept": "application/json"},
                                        adapter={"second_step": {"use": "pdf_url", "selector": "a.dl"}})).fingerprint
        self.assertEqual(plain, more)


class HeadersTest(Base):
    def test_validation(self):
        for name in ("Cookie", "Host", "User-Agent", "X-Forwarded-For", "Proxy-Authorization", "bad header"):
            self.bad(board_plan(request_headers={name: "x"}))
        self.bad(board_plan(request_headers={"X-A": "a\r\nb"}))
        self.bad(board_plan(request_headers_env={"Authorization": "lower_case"}))
        self.bad(board_plan(request_headers_env={"Authorization": {"env": "KEY", "format": "Bearer"}}))
        self.bad(board_plan(request_headers={"X-Key": "a"}, request_headers_env={"x-key": "KEY"}))
        self.env.plan(board_plan(request_headers={"Accept": "application/json"},
                                 request_headers_env={"Authorization": {"env": "API_TOKEN", "format": "Bearer {}"}}))

    def test_resolve_from_environment_only(self):
        raw = {"request_headers": {"Accept": "application/json"},
               "request_headers_env": {"Authorization": {"env": "API_TOKEN", "format": "Bearer {}"}, "X-Key": "XK"}}
        self.assertEqual(resolve_headers(raw, {"API_TOKEN": "t0k", "XK": "k"}),
                         {"Accept": "application/json", "Authorization": "Bearer t0k", "X-Key": "k"})
        with self.assertRaises(PlanError):
            resolve_headers(raw, {"API_TOKEN": "t0k"})  # missing variable: never sent empty

    def test_headers_reach_every_request_and_robots_still_applies(self):
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /view\n")
        transport = RecordingTransport(lambda m, u, d: board(u))
        site = board_plan(request_headers={"X-Api": "1"})
        plan = self.env.plan(site)
        result = self.env.run(site, None, transport=transport, extra_headers=resolve_headers(plan.raw))
        self.assertTrue(all(h.get("X-Api") == "1" for _, _, h, _ in transport.calls))
        self.assertNotIn("/view", " ".join(u for u in transport.urls if "/view" in u))
        self.assertEqual(result["status"], "access_blocked")


class PostListingTest(Base):
    def handler(self, ids, page_size=3):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            if parts.path == "/search":
                if method != "POST":
                    return Response(405, {}, b"")
                body = json.loads(data)
                page = int(body["page"])
                assert body["q"] == "report", body
                chunk = ids[(page - 1) * page_size: page * page_size]
                return json_resp({"items": api_items(chunk), "total": len(ids)})
            return Response(404, {}, b"")
        return handle

    def site(self, **listing):
        return api_plan(listing={"method": "POST", "idempotent": True, "body_type": "json", "params": {"q": "report"},
                                 "total": {"type": "json", "path": "total"}, **listing})

    def test_validation(self):
        self.bad(api_plan(listing={"method": "POST"}))  # idempotent must be declared
        self.bad(api_plan(listing={"method": "PUT", "idempotent": True}))
        self.bad(api_plan(listing={"idempotent": True}))
        self.bad(api_plan(listing={"method": "POST", "idempotent": True, "body_type": "xml"}))

    def test_post_listing_is_enumerated_with_body_and_gate(self):
        transport = RecordingTransport(self.handler(list(range(1, 8))))
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([int(d["post_number"]) for d in self.env.docs()], list(range(1, 8)))
        posts = [c for c in transport.calls if c[0] == "POST"]
        self.assertTrue(posts)
        self.assertEqual(posts[0][2]["Content-Type"], "application/json")
        self.assertEqual(transport.calls[0][1], "https://api.test/robots.txt")  # robots before the first POST

    def test_robots_disallow_stops_before_any_post(self):
        def handle(method, url, data):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /search\n")
            raise AssertionError("must not be requested")
        transport = RecordingTransport(handle)
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual(result["status"], "access_blocked")
        self.assertFalse([c for c in transport.calls if c[0] == "POST"])

    def test_post_redirect_is_not_followed(self):
        def handle(method, url, data):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            return Response(302, {"Location": "https://api.test/elsewhere"}, b"")
        transport = RecordingTransport(handle)
        result = self.env.run(self.site(), None, transport=transport)
        self.assertNotEqual(result["status"], "complete")
        self.assertNotIn("https://api.test/elsewhere", transport.urls)

    def test_host_hold_blocks_post(self):
        self.env.store.seed_hold("api.test", "seeded")
        transport = RecordingTransport(self.handler([1, 2]))
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual(result["status"], "access_blocked")
        self.assertEqual(transport.calls, [])


class PathPagingTest(Base):
    def board(self, n):
        board = FakeBoard(n)

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/news/":
                return board.list_page(1)
            if path.startswith("/news/page/"):
                return board.list_page(int(path.split("/")[3]))
            return board(url)
        return handle

    def site(self, **listing):
        return board_plan(listing={"url": "https://board.test/news/page/{page}/",
                                   "first_page_url": "https://board.test/news/", **listing})

    def test_validation(self):
        self.bad(board_plan(listing={"first_page_url": "https://board.test/news/"}))  # no {page}
        self.bad(self.site(first_page_url="/news/"))

    def test_path_paged_board_completes(self):
        transport = RecordingTransport(self.board(35))
        result = self.env.run(self.site(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(int(d["post_number"]) for d in self.env.docs()), list(range(1, 36)))
        self.assertIn("https://board.test/news/", transport.urls)
        self.assertFalse([u for u in transport.urls if "page=" in u or "/news/page/1/" in u])


class JsonPageTest(Base):
    def cursor_handler(self, ids, page_size=3, loop_at=None):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            start = int(q.get("after", "0"))
            chunk = [i for i in ids if i > start][:page_size]
            nxt = str(chunk[-1]) if chunk and chunk[-1] != ids[-1] else None
            if loop_at is not None and start >= loop_at:
                nxt = str(loop_at)
            return json_resp({"items": api_items(chunk), "meta": {"next": nxt}})
        return handle

    def test_validation(self):
        self.bad(board_plan(listing={"next_cursor": {"path": "meta.next", "param": "after"}}))  # backward strategy
        self.bad(api_plan(listing={"next_cursor": {"path": "meta.next", "param": "a b"}}))
        self.bad(api_plan(listing={"next_cursor": {"path": "meta.next", "param": "after"}}, oldest_sort={"overlap_pages": 1}))
        self.bad(api_plan(listing={"last_page_path": "meta.last"}))  # forward strategy

    def test_next_cursor_paging_to_the_end_and_resume(self):
        site = api_plan(listing={"next_cursor": {"path": "meta.next", "param": "after"}})
        result = self.go(site, self.cursor_handler(list(range(1, 11))), max_requests=3)
        self.assertEqual(result["status"], "incomplete")  # request budget, not completion
        result = self.go(site, self.cursor_handler(list(range(1, 11))))
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "exhausted_cursor_end"))
        self.assertEqual([int(d["post_number"]) for d in self.env.docs()], list(range(1, 11)))
        resumed = [u for u in result["transport"].urls if "after=" in u]
        self.assertTrue(resumed and "after=" in resumed[0])  # continued from the stored token

    def test_cursor_loop_is_not_completion(self):
        site = api_plan(listing={"next_cursor": {"path": "meta.next", "param": "after"}})
        result = self.go(site, self.cursor_handler(list(range(1, 20)), loop_at=6))
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("cursor_loop", result["stop_reason"])

    def last_page_handler(self, n, page_size=10, reported=None):
        ids = list(range(n, 0, -1))
        pages = max(1, -(-n // page_size))

        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            page = int({k: v[0] for k, v in parse_qs(parts.query).items()}.get("page", 1))
            chunk = ids[(page - 1) * page_size: page * page_size]
            return json_resp({"items": api_items(chunk) if False else
                              [{"id": i, "url": f"https://api.test/doc/{i}", "title": f"Doc {i}"} for i in chunk],
                              "meta": {"last": reported if reported is not None else pages}})
        return handle

    def site_last(self):
        return {"strategy": "last_page_backward", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"url": "https://api.test/list", "page_param": "page", "start_page": 1,
                            "last_page_path": "meta.last",
                            "parser": {"type": "json", "items_path": "items", "key": "id", "url": "url",
                                       "title": "title"}}}

    def test_reported_last_page_is_confirmed_and_used(self):
        transport = RecordingTransport(self.last_page_handler(95))
        result = self.env.run(self.site_last(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 95)
        found = [json.loads(r["detail"]) for r in self.env.store.conn.execute(
            "SELECT detail FROM enum_event WHERE kind='last_page_found'")]
        self.assertEqual(found[0]["last_page"], 10)

    def test_wrong_reported_last_page_falls_back_to_search(self):
        transport = RecordingTransport(self.last_page_handler(95, reported=4))
        result = self.env.run(self.site_last(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 95)
        kinds = [r[0] for r in self.env.store.conn.execute("SELECT kind FROM enum_event")]
        self.assertIn("reported_last_page_unconfirmed", kinds)


class Http204EndTest(Base):
    def handler(self, ids, page_size=3, body=b""):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            page = int({k: v[0] for k, v in parse_qs(parts.query).items()}.get("page", 1))
            chunk = ids[(page - 1) * page_size: page * page_size]
            if not chunk:
                return Response(204, {}, body)
            return json_resp({"items": api_items(chunk)})
        return handle

    def test_validation(self):
        self.bad(api_plan(listing={"end_on_204": "yes"}))

    def test_204_ends_the_listing_when_declared(self):
        result = self.go(api_plan(listing={"end_on_204": True}), self.handler(list(range(1, 8))))
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "exhausted_end_status_204"))
        self.assertEqual(len(self.env.docs()), 7)

    def test_204_is_not_an_end_without_the_plan_key(self):
        result = self.go(api_plan(), self.handler(list(range(1, 8))))
        self.assertNotEqual(result["status"], "complete")

    def test_204_with_a_body_is_an_error(self):
        result = self.go(api_plan(listing={"end_on_204": True}), self.handler(list(range(1, 8)), body=b"oops"))
        self.assertNotEqual(result["status"], "complete")


class TwoStepDetailTest(Base):
    def handler(self, board, second, external=False):
        def handle(method, url, data):
            parts = urlsplit(url)
            if parts.path == "/view":
                i = int(parse_qs(parts.query)["id"][0])
                host = "https://cdn.other.test" if external else ""
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<h1 class="title">Stub {i}</h1><a class="next" href="{host}/record/{i}">full</a>'))
            if parts.path.startswith("/record/"):
                i = int(parts.path.rsplit("/", 1)[1])
                second.append(i)
                return Response(200, {"Content-Type": "text/html"},
                                html(f'<h1 class="title">Title {i}</h1><div class="abstract">Abstract {i}</div>'
                                     f'<a class="pdf" href="/files/{i}.pdf">PDF</a>'))
            return board(url)
        return handle

    def test_validation(self):
        self.bad(board_plan(adapter={"second_step": {"use": "other", "selector": "a"}}))
        self.bad(board_plan(adapter={"second_step": {"use": "pdf_url"}}))
        self.bad(board_plan(adapter={"second_step": {"use": "pdf_url", "selector": "a", "allow_hosts": ["Bad Host"]}}))

    def test_second_page_is_the_detail_and_identity_stays(self):
        board, second = FakeBoard(12), []
        site = board_plan(adapter={"second_step": {"use": "detail_page", "selector": "a.next"}})
        result = self.env.run(site, None, transport=RecordingTransport(self.handler(board, second)))
        self.assertEqual(result["status"], "complete", result)
        docs = self.env.docs()
        self.assertEqual(len(docs), 12)
        self.assertEqual(sorted(second), list(range(1, 13)))
        self.assertTrue(all(d["title"].startswith("Title ") for d in docs))
        self.assertTrue(all("/view?id=" in d["meta_url"] for d in docs))
        self.assertTrue(all(d["pdf_url"] and "/files/" in d["pdf_url"] for d in docs))

    def test_other_host_link_is_never_requested(self):
        board, second = FakeBoard(3), []
        site = board_plan(adapter={"second_step": {"use": "detail_page", "selector": "a.next"}})
        transport = RecordingTransport(self.handler(board, second, external=True))
        self.env.run(site, None, transport=transport)
        self.assertFalse([u for u in transport.urls if "cdn.other.test" in u])
        self.assertEqual(self.env.docs(), [])  # link missing (not allowed) -> item failure, nothing saved

    def test_pdf_link_from_detail_page(self):
        board = FakeBoard(4)
        board.detail_override = {i: html(f'<h1 class="title">Title {i}</h1><a class="dl" href="/get?f={i}">x</a>')
                                 for i in range(1, 5)}
        site = board_plan(adapter={"selectors": {"title": "h1.title"},
                                   "second_step": {"use": "pdf_url", "selector": "a.dl"}})
        result = self.env.run(site, None, transport=RecordingTransport(lambda m, u, d: board(u)))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(d["pdf_url"] for d in self.env.docs()),
                         [f"https://board.test/get?f={i}" for i in range(1, 5)])


if __name__ == "__main__":
    unittest.main()
