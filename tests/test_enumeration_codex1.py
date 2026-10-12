"""Cross-review (Codex) batch 1: S0, H01-H09, H11 regression scenarios.

Every scenario uses fake transports or a loopback server; no external network.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler import db_libertree  # noqa: E402
from crawler.enumeration.http import AccessBlocked, Fetcher, Response  # noqa: E402
from crawler.enumeration.plan import PlanError, site_plan  # noqa: E402
from crawler.enumeration.runner import duplicate_rows  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan, html, wp_handler  # noqa: E402
from tests.test_enumeration_legacy_cli import LoopbackBoardServer  # noqa: E402

LIST_ONLY = {"type": "list_only", "id_regex": r"id=(\d+)"}


# ---------------------------------------------------------------------------
# S0 empty isolated database
# ---------------------------------------------------------------------------
class EmptyDatabaseCliTest(unittest.TestCase):
    def test_cli_starts_from_a_missing_database_file(self):
        from crawler.enumeration.run import main
        board = FakeBoard(5, page_size=10, end_mode="empty")
        with LoopbackBoardServer(board) as srv, tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "enum_fresh.db"  # does not exist: no schema, no sites row
            plan = tmp / "plan.json"
            plan.write_text(json.dumps({"version": 1, "sites": {"fresh-site": {
                "strategy": "last_page_backward", "base_url": f"http://127.0.0.1:{srv.port}",
                "listing": {"url": f"http://127.0.0.1:{srv.port}/list", "page_param": "page", "start_page": 1,
                            "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                                       "id_regex": r"id=(\d+)"}},
                "adapter": LIST_ONLY}}}))
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--plan", str(plan), "--site", "fresh-site", "--state-dir", str(tmp / "s"),
                             "--db", str(db)])
            result = json.loads(out.getvalue().strip().splitlines()[-2])[0]
            self.assertEqual((code, result["status"]), (0, "complete"), result)
            conn = db_libertree.open_db(db)
            self.assertEqual(conn.execute("SELECT site_id, site_url FROM sites").fetchall()[0][0], "fresh-site")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
            conn.close()

    def test_registered_crawler_name_and_url_are_used_for_the_site_row(self):
        from crawler.enumeration.run import ensure_documents_db
        from tests.test_enumeration_ledgers import FakeCustomCrawler
        with tempfile.TemporaryDirectory() as tmp:
            conn = db_libertree.open_db(Path(tmp) / "enum_x.db")
            ensure_documents_db(conn, "fake-site", FakeCustomCrawler.site_name, FakeCustomCrawler.base_url,
                                db_name="enum_x.db")
            ensure_documents_db(conn, "fake-site", FakeCustomCrawler.site_name, FakeCustomCrawler.base_url,
                                db_name="enum_x.db")  # idempotent
            rows = conn.execute("SELECT site_id, site_name, site_url FROM sites").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("fake-site", "Fake custom", "https://board.test")])
            conn.close()

    def test_non_isolated_database_is_never_initialised(self):
        from crawler.enumeration.run import ensure_documents_db
        with tempfile.TemporaryDirectory() as tmp:
            conn = db_libertree.open_db(Path(tmp) / "production.db")
            with self.assertRaises(SystemExit):
                ensure_documents_db(conn, "s", "s", "https://s.test", db_name="production.db")
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name='documents'").fetchall(), [])
            conn.close()


# ---------------------------------------------------------------------------
# H08 redirects, H09 robots/plan policy
# ---------------------------------------------------------------------------
class RedirectAndRobotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler, **kw):
        kw.setdefault("interval", 0)
        kw.setdefault("sleep", lambda s: None)
        t = FakeTransport(handler)
        return Fetcher("s", t, Path(self.tmp.name) / "g", **kw), t

    def test_redirect_hops_are_explicit_and_login_location_is_not_followed(self):
        def handler(url):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(404, {}, b"")
            if path == "/a":
                return Response(302, {"Location": "https://b.test/b"}, b"")
            if path == "/b":
                return Response(302, {"Location": "/user/login?next=/b"}, b"")
            return Response(200, {"Content-Type": "text/html"}, html("<p>login</p>"))
        f, t = self.fetcher(handler)
        with self.assertRaises(AccessBlocked) as ctx:
            f.get("https://a.test/a")
        self.assertEqual(ctx.exception.reason, "login_redirect")
        pages = [u for u in t.urls if not u.endswith("robots.txt")]
        self.assertEqual(pages, ["https://a.test/a", "https://b.test/b"])  # login URL never requested
        self.assertEqual(f.requests_made, len(t.urls))  # every hop is budgeted
        self.assertIn("https://b.test/robots.txt", t.urls)  # destination robots checked before the hop

    def test_redirect_into_robots_disallowed_path_is_refused_before_request(self):
        def handler(url):
            if url == "https://a.test/robots.txt":
                return Response(200, {}, b"User-agent: *\nDisallow: /private\n")
            if url == "https://a.test/x":
                return Response(301, {"Location": "/private/1"}, b"")
            return Response(200, {"Content-Type": "text/html"}, html("ok"))
        f, t = self.fetcher(handler)
        with self.assertRaises(AccessBlocked) as ctx:
            f.get("https://a.test/x")
        self.assertEqual(ctx.exception.reason, "robots_disallowed")
        self.assertNotIn("https://a.test/private/1", t.urls)

    def test_redirect_hops_keep_the_gap(self):
        stamps = []

        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            stamps.append(time.monotonic())
            if url.endswith("/a"):
                return Response(302, {"Location": "/b"}, b"")
            return Response(200, {"Content-Type": "text/html"}, html("ok"))
        f, _ = self.fetcher(handler, interval=0.25, sleep=time.sleep)
        f.get("https://a.test/a")
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.24)

    def test_robots_forbidden_or_challenge_is_a_hold(self):
        for robots in (Response(403, {}, b"denied"), Response(401, {}, b""),
                       Response(200, {"Content-Type": "text/html"}, b"<html><title>Just a moment...</title></html>")):
            f, t = self.fetcher(lambda u, r=robots: r if u.endswith("robots.txt") else Response(200, {}, b"ok"))
            with self.assertRaises(AccessBlocked) as ctx:
                f.get("https://a.test/page")
            self.assertTrue(ctx.exception.reason.startswith("robots_"), ctx.exception.reason)
            self.assertEqual(t.urls, ["https://a.test/robots.txt"])

    def test_crawl_delay_widens_the_gap(self):
        stamps = []

        def handler(url):
            if url.endswith("robots.txt"):
                return Response(200, {}, b"User-agent: *\nCrawl-delay: 0.4\n")
            stamps.append(time.monotonic())
            return Response(200, {"Content-Type": "text/html"}, html("ok"))
        f, _ = self.fetcher(handler, interval=0, sleep=time.sleep)
        f.get("https://a.test/1")
        f.get("https://a.test/2")
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.38)

    def test_refusal_statuses_cannot_be_accepted(self):
        f, _ = self.fetcher(lambda u: Response(403, {}, b"") if not u.endswith("robots.txt") else Response(404, {}, b""))
        with self.assertRaises(AccessBlocked):
            f.get("https://a.test/x", accept_statuses=(403,))

    def test_plan_rejects_policy_relaxation(self):
        base = {"strategy": "last_page_backward", "listing": {"url": "https://x.test/l"}}
        for bad in ({"respect_robots": False},
                    {"listing": {"url": "https://x.test/l", "end_statuses": [403]}},
                    {"listing": {"url": "https://x.test/l", "end_statuses": [429]}},
                    {"listing": {"url": "https://x.test/l", "end_statuses": [400]}}):
            with self.assertRaises(PlanError, msg=bad):
                site_plan({"sites": {"s": {**base, **bad}}}, "s")
        ok = {"listing": {"url": "https://x.test/l",
                          "end_statuses": [{"status": 400, "json_code": "rest_post_invalid_page_number"}, 404]}}
        site_plan({"sites": {"s": {**base, **ok}}}, "s")


# ---------------------------------------------------------------------------
# H07 holds across passes, sites and Fetchers
# ---------------------------------------------------------------------------
class PersistentHoldTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_retry_only_block_is_persisted(self):
        board = FakeBoard(6)
        board.detail_status = {2: [503, 503, 503]}
        first = self.env.run(board_plan(), board)
        self.assertEqual(first["status"], "complete")
        board.detail_status = {2: 403}
        second = self.env.run(board_plan(), board)  # retry-only path
        self.assertEqual(second["status"], "access_blocked", second)
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "access_blocked")
        again = FakeTransport(board)
        third = self.env.run(board_plan(), board, transport=again)
        self.assertEqual(third["stop_reason"], "access_hold_requires_review")
        self.assertEqual(again.urls, [])

    def test_retry_only_rate_limit_records_cooldown(self):
        board = FakeBoard(6)
        board.detail_status = {2: [503, 503, 503]}
        self.env.run(board_plan(), board)

        def handler(url):
            return Response(429, {"Retry-After": "600"}, b"") if "/view" in url else board(url)
        second = self.env.run(board_plan(), handler)
        self.assertEqual((second["status"], second["stop_reason"]), ("incomplete", "rate_limited_cooldown"))
        self.assertIn("cooldown_until", self.env.store.site(self.env.site_id)["state"])

    def test_file_pass_respects_site_hold_and_records_new_blocks(self):
        from crawler.enumeration.files import FilePass
        board = FakeBoard(4)
        self.env.run(board_plan(), board)
        plan = self.env.plan(board_plan())

        def blocked(url):
            return Response(403, {}, b"") if "/files/" in url else board(url)
        t = FakeTransport(blocked)
        stats = FilePass(self.env.store, self.env.fetcher(plan, t), blob_root=self.env.root / "b",
                         doc_conn=self.env.conn).run(self.env.site_id)
        self.assertTrue(stats["stopped"].startswith("access_blocked"))
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "access_blocked")
        self.assertTrue(self.env.store.host_hold("board.test"))
        t2 = FakeTransport(board)
        stats = FilePass(self.env.store, self.env.fetcher(plan, t2), blob_root=self.env.root / "b",
                         doc_conn=self.env.conn).run(self.env.site_id)
        self.assertEqual(t2.urls, [])
        self.assertTrue(stats["stopped"].startswith("site_hold"))

    def test_block_on_one_site_holds_the_host_for_other_site_ids(self):
        board = FakeBoard(6)
        board.detail_status = {3: 403}
        first = self.env.run(board_plan(), board)
        self.assertEqual(first["status"], "access_blocked")
        db_libertree.upsert_site(self.env.conn, "other-site", "o", "https://board.test")
        t = FakeTransport(FakeBoard(6))
        other = self.env.run(board_plan(), None, transport=t, site_id="other-site")
        self.assertEqual(other["status"], "access_blocked", other)
        self.assertTrue(other["stop_reason"].startswith("host_access_hold"), other)
        self.assertEqual(t.urls, [])

    def test_429_cooldown_is_shared_through_the_host_gate(self):
        board = FakeBoard(6)

        def limited(url):
            return Response(429, {"Retry-After": "600"}, b"") if "/view" in url else board(url)
        first = self.env.run(board_plan(), limited)
        self.assertEqual(first["stop_reason"], "rate_limited_cooldown")
        db_libertree.upsert_site(self.env.conn, "other-site", "o", "https://board.test")
        t = FakeTransport(FakeBoard(6))
        other = self.env.run(board_plan(), None, transport=t, site_id="other-site")
        self.assertEqual((other["status"], other["stop_reason"]), ("incomplete", "host_cooldown"), other)
        self.assertEqual(t.urls, [])


# ---------------------------------------------------------------------------
# H01 listing identity conflicts, H02 leases and NULL ids
# ---------------------------------------------------------------------------
class ConflictBoard(FakeBoard):
    """Board whose rows carry an explicit post id that can repeat across URLs."""

    def __init__(self, rows_by_page, **kw):
        super().__init__(sum(len(r) for r in rows_by_page.values()), **kw)
        self.rows_by_page = rows_by_page

    def list_page(self, page):
        self.list_requests += 1
        rows = self.rows_by_page.get(page, [])
        body = "".join(f'<tr><td class="date">2000-01-0{1 + n % 9}</td><td>'
                       f'<a href="{url}" data-id="{pid}">T{pid}</a></td></tr>' for n, (pid, url) in enumerate(rows))
        return Response(200, {"Content-Type": "text/html"}, html(f"<table><tbody>{body}</tbody></table>"))

    def __call__(self, url):
        parts = urlsplit(url)
        if parts.path in ("/a", "/b", "/c", "/d", "/e"):
            return Response(200, {"Content-Type": "text/html"}, html(f'<h1 class="title">Doc {parts.path}</h1>'))
        return super().__call__(url)


def conflict_plan():
    site = board_plan(listing={"total": None,
                               "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                                          "item_id_attr": "data-id"}},
                      last_page={"check_unparameterized_first_page": False})
    site["adapter"] = {"type": "selectors", "selectors": {"title": "h1.title"}}
    return site


class ListingConflictTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def conflicts(self):
        return self.env.store._all("SELECT item_key, url FROM enum_item WHERE reason='listing_identity_conflict' "
                                   "ORDER BY url")

    def test_same_page_same_id_different_urls_are_both_kept(self):
        board = ConflictBoard({1: [(7, "/a"), (7, "/b"), (8, "/c")]})
        result = self.env.run(conflict_plan(), board)
        self.assertEqual(result["status"], "complete_with_review", result)
        conflict = [r["url"] for r in self.conflicts()]
        saved = {d["meta_url"] for d in self.env.docs()}
        self.assertEqual(len(conflict), 1)
        self.assertEqual(set(conflict) | (saved & {"https://board.test/a", "https://board.test/b"}),
                         {"https://board.test/a", "https://board.test/b"})  # neither candidate is lost
        self.assertEqual(len(saved), 2)

    def test_different_pages_same_id_is_a_conflict(self):
        board = ConflictBoard({1: [(7, "/a"), (8, "/c")], 2: [(7, "/b"), (9, "/d")]})
        result = self.env.run(conflict_plan(), board)
        self.assertEqual(result["status"], "complete_with_review", result)
        self.assertEqual(len(self.conflicts()), 1)

    def test_rerun_with_new_url_for_saved_id_is_a_conflict(self):
        self.env.run(conflict_plan(), ConflictBoard({1: [(7, "/a")]}))
        result = self.env.run(conflict_plan(), ConflictBoard({1: [(7, "/e")]}), refresh=True)
        self.assertEqual(result["status"], "complete_with_review", result)
        self.assertEqual([r["url"] for r in self.conflicts()], ["https://board.test/e"])

    def test_null_post_number_is_held_for_review(self):
        from tests.test_enumeration_ledgers import FakeCustomCrawler

        class NullId(FakeCustomCrawler):
            def _parse_detail(self, raw, item):
                paper = super()._parse_detail(raw, item)
                if item["post_number"] == "2":
                    paper["external_id"] = None
                return paper
        from tests.test_enumeration_ledgers import custom_plan
        result = self.env.run(custom_plan(), FakeBoard(4), crawler=NullId(self.env.conn))
        row = self.env.store.item(self.env.site_id, "2")
        self.assertEqual((row["status"], row["reason"]), ("failed_permanent", "identity_missing"))
        self.assertEqual(result["status"], "complete_with_review")

    def test_null_post_numbers_count_as_one_group(self):
        for n in range(3):
            self.env.conn.execute("INSERT INTO documents(site_id, post_number, meta_url, title) VALUES(?,?,?,?)",
                                  ("fake-site", None, f"https://board.test/n{n}", "t"))
        self.env.conn.commit()
        self.assertEqual(duplicate_rows(self.env.conn, "fake-site"), 2)

    def test_site_lease_blocks_a_concurrent_run(self):
        from crawler.enumeration.run import SiteLease
        with SiteLease(self.env.conn, self.env.root / "libertree.db", self.env.site_id):
            with self.assertRaises(RuntimeError):
                with SiteLease(self.env.conn, self.env.root / "libertree.db", self.env.site_id):
                    pass
        with SiteLease(self.env.conn, self.env.root / "libertree.db", self.env.site_id):
            pass  # released

    def test_duplicate_site_arguments_are_refused(self):
        from crawler.enumeration.run import main
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"version": 1, "sites": {"x": {
                "strategy": "sitemap", "sitemap": {"sitemap_url": "https://x.test/s.xml"}}}}))
            with self.assertRaises(SystemExit):
                main(["--plan", str(plan), "--site", "x", "--site", "x", "--db", str(Path(tmp) / "enum_x.db"),
                      "--state-dir", tmp])


# ---------------------------------------------------------------------------
# H03 false exhaustion
# ---------------------------------------------------------------------------
class FalseExhaustionTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def json_site(self):
        return {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"url": "https://api.test/items", "page_param": "page", "start_page": 1,
                            "parser": {"type": "json", "items_path": "items", "key": "id", "url": "link",
                                       "date": "date", "title": "title"}}}

    def json_handler(self, broken_from):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            if page >= broken_from:
                return Response(200, {"Content-Type": "application/json"}, b'{"items": [ {"id": 1,')
            items = [{"id": i, "link": f"https://api.test/i/{i}", "date": f"2001-01-{i:02d}", "title": f"I{i}"}
                     for i in range((page - 1) * 5 + 1, page * 5 + 1)]
            return Response(200, {"Content-Type": "application/json"}, json.dumps({"items": items}))
        return handler

    def test_broken_json_is_not_exhaustion(self):
        result = self.env.run(self.json_site(), self.json_handler(broken_from=2))
        self.assertEqual(result["status"], "incomplete", result)

    def test_error_html_page_is_not_exhaustion(self):
        board = FakeBoard(25)
        original = board.list_page

        def list_page(page):
            if page == 2:
                return Response(200, {"Content-Type": "text/html"}, html("<h1>Service temporarily unavailable</h1>"))
            return original(page)
        board.list_page = list_page
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": LIST_ONLY,
                "listing": board_plan()["listing"]}
        board.ids = list(range(1, 26))
        result = self.env.run(site, board)
        self.assertEqual(result["status"], "incomplete", result)

    def test_wordpress_400_other_than_invalid_page_is_an_error(self):
        handler = wp_handler(250)

        def bad(url):
            if parse_qs(urlsplit(url).query).get("page") == ["2"]:
                return Response(400, {"Content-Type": "application/json"}, b'{"code":"rest_invalid_param"}')
            return handler(url)
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
        result = self.env.run(site, bad)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(len(self.env.docs()), 100)

    def test_repeated_page_followed_by_new_items_is_not_the_end(self):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            ids = {1: [1, 2], 2: [1, 2], 3: [5, 6]}.get(page, [])
            items = [{"id": i, "link": f"https://api.test/i/{i}", "date": f"2001-01-0{i}", "title": f"I{i}"} for i in ids]
            return Response(200, {"Content-Type": "application/json"}, json.dumps({"items": items}))
        result = self.env.run(self.json_site(), handler)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertTrue(result["stop_reason"].startswith("repeated_page_inside_range"), result)


# ---------------------------------------------------------------------------
# H04 manifest crash, H05 sitemap change, H06 ineffective date filter
# ---------------------------------------------------------------------------
class ManifestAndSitemapTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_crash_while_creating_segments_does_not_lose_ranges(self):
        from tests.test_enumeration_date_range import FakeSearch, make_items, plan
        items = make_items({2020: 3, 2021: 4})
        original = self.env.store.ensure_segment
        calls = {"n": 0}

        def crash(*args, **kwargs):
            calls["n"] += 1
            result = original(*args, **kwargs)
            if calls["n"] == 1:
                raise KeyboardInterrupt("killed after the first segment commit")
            return result
        self.env.store.ensure_segment = crash
        site = plan(segments={"from": "2020", "to": "2021"})
        with self.assertRaises(KeyboardInterrupt):
            self.env.run(site, FakeSearch(items))
        self.env.store.ensure_segment = original
        result = self.env.run(site, FakeSearch(items))
        fp = self.env.store.site(self.env.site_id)["plan_fp"]
        self.assertEqual(sorted(s["segment_key"] for s in self.env.store.segments(self.env.site_id, fp)),
                         ["2020", "2021"])
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 7)

    def test_sitemap_changed_between_runs_is_re_enumerated(self):
        urls = [f"https://sm.test/doc/{i}" for i in range(1, 203)]
        state = {"urls": urls}

        def handler(url):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(404, {}, b"")
            if path == "/sitemap.xml":
                body = "".join(f"<url><loc>{u}</loc></url>" for u in state["urls"])
                return Response(200, {"Content-Type": "application/xml"},
                                f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                                f'sitemap/0.9">{body}</urlset>')
            return Response(200, {"Content-Type": "text/html"}, html(f"<h1>{path}</h1>"))
        site = {"strategy": "sitemap", "request_interval_s": 0, "base_url": "https://sm.test",
                "adapter": {"type": "selectors", "selectors": {"title": "h1"}},
                "sitemap": {"sitemap_url": "https://sm.test/sitemap.xml"}}
        first = self.env.run(site, handler, max_requests=203)  # stops inside the 2nd chunk
        self.assertEqual(first["status"], "incomplete", first)
        state["urls"] = ["https://sm.test/doc/new"] + urls
        result = self.env.run(site, handler)
        self.assertEqual(result["status"], "complete", result)
        saved = {d["meta_url"] for d in self.env.docs()}
        self.assertIn("https://sm.test/doc/new", saved)
        self.assertEqual(len(saved), 203)
        self.assertTrue(self.env.store.events(self.env.site_id, "sitemap_changed_restart"))

    def test_ineffective_date_filter_is_not_completion(self):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            body = '<ul><li><a href="/doc/1">Doc 1</a><time datetime="2020-05-05">2020-05-05</time></li></ul>'
            return Response(200, {"Content-Type": "text/html"}, html(body + "<p>Total 1 results</p>"))
        from tests.test_enumeration_date_range import plan
        result = self.env.run(plan(segments={"from": "2020", "to": "2021"}), handler)
        self.assertNotEqual(result["status"], "complete", result)
        self.assertTrue(self.env.store.events(self.env.site_id, "segment_overlap") or
                        self.env.store.events(self.env.site_id, "filter_not_effective"))


# ---------------------------------------------------------------------------
# H11 guard follows child threads
# ---------------------------------------------------------------------------
class GuardThreadTest(unittest.TestCase):
    def test_child_thread_started_under_guard_is_guarded(self):
        from crawler.enumeration import guard
        from crawler.enumeration.adapters import AdapterUngatedNetwork
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        outcome = {}

        def child():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                outcome["child"] = "connected"
            except AdapterUngatedNetwork:
                outcome["child"] = "blocked"
        try:
            with guard.guarded():
                t = threading.Thread(target=child)
                t.start()
                t.join()
            self.assertEqual(outcome, {"child": "blocked"})
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
