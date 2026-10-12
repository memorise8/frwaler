"""Regression tests for the code review (C1 C2 H1 H3 H4 H5 M1 M2 M4 M5 M7 G1 G2 G3).

Each scenario reproduces a reviewer probe or acceptance criterion against
fake HTTP sources only.
"""
from __future__ import annotations

import glob
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402

from crawler import db_libertree  # noqa: E402
from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler.enumeration import legacy  # noqa: E402
from crawler.enumeration.adapters import Adapter, AdapterUngatedNetwork  # noqa: E402
from crawler.enumeration.http import Fetcher, Response  # noqa: E402
from crawler.enumeration.parsing import Item  # noqa: E402
from crawler.enumeration.plan import PlanError, site_plan  # noqa: E402
from crawler.enumeration.report import site_report  # noqa: E402
from crawler.enumeration.run import main  # noqa: E402
from crawler.enumeration.urls import build_url  # noqa: E402
from crawler.generic_crawler import GenericCrawler  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan, html, wp_handler  # noqa: E402

LIST_ONLY = {"type": "list_only", "id_regex": r"id=(\d+)"}


def generic_crawler_for(env, site_id="fake-site", base_url="https://board.test", id_regex=r"id=(\d+)"):
    """A registered-style GenericCrawler (config file) pointing at the fake board."""
    path = env.root / f"{site_id}.json"
    path.write_text(json.dumps({
        "site_id": site_id, "site_name": site_id, "base_url": base_url,
        "options": {"delay": 0.001, **({"id_regex": id_regex} if id_regex else {})},
        "list_page": {"url": f"{base_url}/list", "pagination": {"type": "query_param", "param": "page", "start": 1},
                      "selectors": {"item_container": "tbody tr", "item_link": "a[href]", "date": "td.date"}},
        "detail_page": {}}))
    return GenericCrawler(str(path), env.conn)


def crawler_plan(**over):
    site = board_plan(listing={"parser": {"type": "adapter"}})
    site["adapter"] = {"type": "crawler"}
    site.update(over)
    return site


def reference_db(rows, site_id="fake-site"):
    tmp = tempfile.mkdtemp()
    conn = db_libertree.open_db(Path(tmp) / "reference.db")
    db_libertree.init_db(conn)
    db_libertree.upsert_site(conn, site_id, site_id, "https://board.test")
    for post_number, meta_url in rows:
        db_libertree.insert_document(conn, {"site_id": site_id, "post_number": post_number,
                                            "meta_url": meta_url, "title": "t"})
    return conn


# ---------------------------------------------------------------------------
# C1 identity with existing rows
# ---------------------------------------------------------------------------
class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_registered_site_must_reuse_its_crawler(self):
        plan = {"sites": {"fake-site": board_plan(adapter=LIST_ONLY)}}
        with self.assertRaises(PlanError) as ctx:
            site_plan(plan, "fake-site", min_interval=0, registered={"fake-site"})
        self.assertIn("registered_crawler_must_be_reused", str(ctx.exception))
        with self.assertRaises(PlanError):  # crawler adapter but a framework list parser
            site_plan({"sites": {"fake-site": dict(board_plan(), adapter={"type": "crawler"})}}, "fake-site",
                      min_interval=0, registered={"fake-site"})
        site_plan({"sites": {"fake-site": crawler_plan()}}, "fake-site", min_interval=0, registered={"fake-site"})

    def test_probe4_list_only_identity_is_not_the_full_url_for_legacy_rules(self):
        crawler = generic_crawler_for(self.env)
        adapter = Adapter("fake-site", {"type": "crawler"}, self.env.conn, crawler=crawler)
        url = "https://board.test/view?id=17"
        paper = adapter.materialize(Item(key=url, url=url, title="t"), None)
        self.assertEqual(paper["external_id"], "17")
        adapter.close()

    def test_registered_generic_list_uses_legacy_make_absolute(self):
        board = FakeBoard(5)

        def handler(url):
            if urlsplit(url).path == "/board/list":
                rows = "".join(f'<tr><td class="date">2000-01-0{i}</td><td><a href="view?id={i}">T{i}</a></td></tr>'
                               for i in range(5, 0, -1))
                return Response(200, {"Content-Type": "text/html"}, html(f"<table><tbody>{rows}</tbody></table>"))
            if urlsplit(url).path == "/board/list2":
                return Response(200, {"Content-Type": "text/html"}, html("<table><tbody></tbody></table>"))
            return board(url)

        crawler = generic_crawler_for(self.env)
        site = crawler_plan(listing={"url": "https://board.test/board/list", "parser": {"type": "adapter"}},
                            last_page={"check_unparameterized_first_page": False})
        self.env.run(site, handler, crawler=crawler, transport=FakeTransport(handler))
        urls = {d["meta_url"] for d in self.env.docs()}
        # legacy GenericCrawler._make_absolute: base_url + "/" + href (not urljoin)
        self.assertEqual(urls, {f"https://board.test/view?id={i}" for i in range(1, 6)})

    def test_identity_precheck_refuses_mismatching_reference_rows(self):
        board = FakeBoard(25)
        ref = reference_db([(f"post-{i}", f"https://board.test/view?id={i}") for i in range(20, 26)])
        transport = FakeTransport(board)
        result = self.env.run(crawler_plan(), board, crawler=generic_crawler_for(self.env), transport=transport,
                              reference_conn=ref, identity_required=True)
        self.assertEqual((result["status"], result["stop_reason"]), ("config_error", "identity_mismatch"))
        self.assertEqual(result["identity"]["match_rate"], 0.0)
        self.assertEqual(self.env.docs(), [])
        ref.close()

    def test_identity_precheck_passes_on_matching_reference_rows(self):
        board = FakeBoard(25)
        ref = reference_db([(str(i), f"https://board.test/view?id={i}") for i in range(20, 26)])
        result = self.env.run(crawler_plan(), board, crawler=generic_crawler_for(self.env), reference_conn=ref,
                              identity_required=True)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["identity"]["match_rate"], 1.0)
        self.assertGreaterEqual(result["identity"]["compared"], 5)
        ref.close()

    def test_identity_precheck_required_for_registered_site(self):
        result = self.env.run(crawler_plan(), FakeBoard(5), crawler=generic_crawler_for(self.env),
                              identity_required=True)
        self.assertEqual((result["status"], result["stop_reason"]), ("config_error", "identity_reference_required"))


# ---------------------------------------------------------------------------
# C2 end detection
# ---------------------------------------------------------------------------
class EndDetectionTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def assert_never_complete_with_gap(self, result, n):
        ids = {int(d["post_number"]) for d in self.env.docs()}
        if result["status"] == "complete":
            self.assertEqual(ids, set(range(1, n + 1)))

    def test_probe2a_mod_n_wrap_stops_early(self):
        class ModBoard(FakeBoard):
            def list_page(self, page):
                n = self.pages()
                if page > n:
                    page = ((page - 1) % n) + 1
                return super().list_page(page)
        result = self.env.run(board_plan(adapter=LIST_ONLY), ModBoard(100, show_total=False))
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "repeated_page_content"))
        self.assertLess(result["requests"], 20)

    def test_probe3_transient_404_during_probe_is_not_an_end(self):
        for show_total in (False, True):
            env = Env()
            try:
                class Flaky(FakeBoard):
                    hit = set()

                    def list_page(self, page):
                        if page == 33 and page not in self.hit:
                            self.hit.add(page)
                            return Response(404, {}, b"")
                        return super().list_page(page)
                result = env.run(board_plan(adapter=LIST_ONLY), Flaky(600, show_total=show_total))
                ids = {int(d["post_number"]) for d in env.docs()}
                # The transient 404 is re-checked, the search repeated, and nothing is lost.
                self.assertEqual(result["status"], "complete", result)
                self.assertEqual(ids, set(range(1, 601)))
            finally:
                env.close()

    def test_persistent_hole_inside_range_is_incomplete(self):
        class Hole(FakeBoard):
            def list_page(self, page):
                if page == 3:
                    return Response(200, {"Content-Type": "text/html"}, html("<table><tbody></tbody></table>"))
                return super().list_page(page)
        result = self.env.run(board_plan(adapter=LIST_ONLY), Hole(100, show_total=False))
        # The hole makes page 2 look like the end; the neighbour check (page 4 has
        # items) refuses that end, so the run never claims completion.
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "last_page_unconfirmed"))
        self.assertTrue(self.env.store.events(self.env.site_id, "last_page_unconfirmed"))

    def test_probe2c_transient_soft_empty_page_is_reread(self):
        class Soft(FakeBoard):
            n = 0

            def list_page(self, page):
                if page == 5:
                    Soft.n += 1
                    if Soft.n == 3:
                        return Response(200, {"Content-Type": "text/html"}, html("<table><tbody></tbody></table>"))
                return super().list_page(page)
        result = self.env.run(board_plan(adapter=LIST_ONLY), Soft(100, show_total=False))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 100)

    def test_other_4xx_on_listing_is_not_an_end(self):
        class Bad(FakeBoard):
            def list_page(self, page):
                if page == 9:
                    return Response(400, {"Content-Type": "text/html"}, html("bad request"))
                return super().list_page(page)
        result = self.env.run(board_plan(adapter=LIST_ONLY), Bad(100, show_total=False))
        self.assertEqual(result["status"], "incomplete", result)

    def test_overlap_walk_limit_forbids_completion(self):
        board = FakeBoard(60)
        state = {"done": False}

        def hook(b, page):
            if b.list_requests == 14 and not state["done"]:
                b.insert(45)
                state["done"] = True
        board.list_hooks.append(hook)
        site = board_plan(adapter=LIST_ONLY, last_page={"max_overlap_walk": 2})
        result = self.env.run(site, board)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "overlap_walk_limit"))

    def test_forward_transient_empty_page_is_reread(self):
        handler = wp_handler(250)
        calls = {"n": 0}

        def flaky(url):
            q = parse_qs(urlsplit(url).query)
            if q.get("page") == ["2"]:
                calls["n"] += 1
                if calls["n"] == 1:
                    return Response(200, {"Content-Type": "application/json", "X-WP-Total": "250"}, b"[]")
            return handler(url)
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
        result = self.env.run(site, flaky)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 250)


# ---------------------------------------------------------------------------
# H1 login widget, M2 429 cooldown
# ---------------------------------------------------------------------------
SIDEBAR = b"""<!doctype html><html><head><title>Board</title></head><body>
<div id="side"><form action="/login.do"><input name="id"><input type="password" name="pw"></form></div>
<div class="board"><ul><li><a href="view.do?no=1">Post 1</a></li></ul></div></body></html>"""
LOGIN = b"""<!doctype html><html><head><title>Sign in</title></head><body>
<div class="login"><h2>Sign in</h2><form action="/login.do" method="post"><input name="id">
<input type="password" name="pw"><button>Login</button></form></div></body></html>"""


class AccessHeuristicsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_probe1_sidebar_login_widget_is_not_blocked(self):
        f = Fetcher("x", FakeTransport(lambda u: Response(200, {"Content-Type": "text/html"}, SIDEBAR, u)),
                    self.env.root / "g", interval=0, respect_robots=False, sleep=lambda s: None)
        resp = f.get("https://ex.kr/board/list.do")
        self.assertEqual(resp.status, 200)
        self.assertFalse(resp.login_suspect)

    def test_login_page_listing_is_review_pending_not_held(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            return Response(200, {"Content-Type": "text/html"}, LOGIN)
        result = self.env.run(board_plan(), handler)
        self.assertEqual((result["status"], result["stop_reason"]), ("review_pending", "login_form"))
        self.assertTrue(self.env.store.events(self.env.site_id, "login_form_review"))
        again = FakeTransport(handler)
        self.env.run(board_plan(), handler, transport=again)
        self.assertTrue(again.urls)  # not a permanent hold

    def test_login_page_detail_is_an_item_failure(self):
        board = FakeBoard(15)
        board.detail_override = {4: LOGIN.decode()}
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        row = self.env.store.item(self.env.site_id, "4")
        self.assertEqual((row["status"], row["reason"]), ("failed_permanent", "login_required_suspect"))

    def test_repeated_429_stops_site_and_records_cooldown(self):
        board = FakeBoard(15)

        def handler(url):
            if "/view" in url:
                return Response(429, {"Retry-After": "30"}, b"")
            return board(url)
        transport = FakeTransport(handler)
        result = self.env.run(board_plan(max_retry_wait_s=60), handler, transport=transport)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "rate_limited_cooldown"))
        self.assertEqual(sum("/view" in u for u in transport.urls), 3)
        state = self.env.store.site(self.env.site_id)["state"]
        self.assertIn("cooldown_until", state)
        again = FakeTransport(handler)
        held = self.env.run(board_plan(), handler, transport=again)
        self.assertEqual((held["status"], held["stop_reason"]), ("incomplete", "cooling_down"))
        self.assertEqual(again.urls, [])


# ---------------------------------------------------------------------------
# H3 unverified empty segments
# ---------------------------------------------------------------------------
class EmptySegmentTest(unittest.TestCase):
    def test_empty_segment_without_total_is_unverified(self):
        from tests.test_enumeration_date_range import FakeSearch, make_items, plan
        env = Env()
        try:
            site = plan(listing={"total": None}, segments={"from": "2017", "to": "2020"})
            result = env.run(site, FakeSearch(make_items()))
            self.assertEqual(result["status"], "complete_unverified", result)
            site_row = env.store.site(env.site_id)
            segs = {s["segment_key"]: s["status"] for s in env.store.segments(env.site_id, site_row["plan_fp"])}
            self.assertEqual(segs["2017"], "unverified")
            report = site_report(env.store, env.site_id)
            self.assertEqual(report["segments_unverified"], 1)
            self.assertIsNone(site_row["completed_at"])
        finally:
            env.close()


# ---------------------------------------------------------------------------
# H5 legacy encoding behind a flag
# ---------------------------------------------------------------------------
def legacy_request_url(config, decode):
    params = dict(config["list_page"].get("params", {}))
    if decode:
        from crawler.enumeration.urls import decode_preencoded
        params = decode_preencoded(params)
    pagination = config["list_page"].get("pagination", {})
    if pagination.get("type") == "query_param":
        params[pagination.get("param", "page")] = pagination.get("start", 1)
    return requests.Request("GET", config["list_page"]["url"], params=params).prepare().url


class LegacyEncodingTest(unittest.TestCase):
    def configs(self):
        root = Path(__file__).resolve().parents[1] / "crawler" / "sites" / "configs"
        out = []
        for path in sorted(glob.glob(str(root / "*.json"))):
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(cfg.get("list_page"), dict) and cfg["list_page"].get("url"):
                out.append((Path(path).stem, cfg))
        return out

    def crawl_url(self, cfg, flag):
        seen = []

        class Session:
            headers = {}
            verify = True

            def request(self, method, url, params=None, **kw):
                seen.append(requests.Request(method, url, params=params).prepare().url)
                return types.SimpleNamespace(raise_for_status=lambda: None, text="<html></html>",
                                             encoding=None, status_code=200)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads(json.dumps(cfg))
            cfg.setdefault("options", {})["delay"] = 0.001
            cfg["options"].pop("fetch_method", None)
            if flag:
                cfg["options"]["decode_preencoded_params"] = True
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(cfg))
            crawler = GenericCrawler(str(path), None)
            crawler._session = Session()
            crawler._crawl_html()
        return seen[0] if seen else None

    def test_default_behaviour_is_unchanged_and_flag_fixes_double_encoding(self):
        changed = []
        for name, cfg in self.configs():
            if cfg.get("crawl_type", "html") not in ("html",):
                continue
            before = legacy_request_url(cfg, decode=False)
            self.assertEqual(self.crawl_url(cfg, flag=False), before, name)
            after = self.crawl_url(cfg, flag=True)
            self.assertEqual(after, legacy_request_url(cfg, decode=True), name)
            if after != before:
                changed.append(name)
                self.assertNotIn("%25", after, name)
        for name in ("datos-gob-cl", "helios-eie-gr", "nkvts-no"):
            self.assertIn(name, changed)
        self.assertGreaterEqual(len(changed), 40)

    def test_plus_in_encoded_value_means_space(self):
        self.assertEqual(build_url("https://d.cl/api", {"q": "a+OR+res_name%3Aa"}),
                         "https://d.cl/api?q=a+OR+res_name%3Aa")

    def test_flag_is_set_through_legacy_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"site_id": "g", "site_name": "g", "base_url": "https://g.test",
                                        "list_page": {"url": "https://g.test/l"}}))
            crawler = GenericCrawler(str(path), None)
            legacy.apply_overrides(crawler, {"decode_preencoded_params": True})
            self.assertTrue(crawler._config["options"]["decode_preencoded_params"])


# ---------------------------------------------------------------------------
# M1 file ledger ordering, G1 duplicates, G2 dates
# ---------------------------------------------------------------------------
class SaveOrderingTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_file_is_enqueued_before_item_is_marked_saved(self):
        board = FakeBoard(3)
        original = self.env.store.record_item

        def crash(site_id, key, status, **kw):
            if status == "saved":
                raise KeyboardInterrupt("process killed")
            return original(site_id, key, status, **kw)
        self.env.store.record_item = crash
        with self.assertRaises(KeyboardInterrupt):
            self.env.run(board_plan(), board)
        self.env.store.record_item = original
        self.assertEqual(len(self.env.docs()), 1)
        self.assertEqual(len(self.env.store.files(self.env.site_id)), 1)

    def test_existing_post_number_is_not_inserted_again(self):
        db_libertree.insert_document(self.env.conn, {"site_id": "fake-site", "post_number": "3",
                                                     "meta_url": "https://board.test/old/3", "title": "old"})
        result = self.env.run(board_plan(), FakeBoard(5))
        self.assertEqual(result["status"], "complete", result)
        rows = self.env.conn.execute("SELECT COUNT(*) FROM documents WHERE post_number='3'").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertTrue(self.env.store.events(self.env.site_id, "duplicate_post_number"))
        self.assertEqual(result["duplicates"]["new"], 0)

    def test_new_duplicates_fail_the_site(self):
        class Dup(BaseCrawler):
            site_id = "fake-site"
            site_name = "d"
            base_url = "https://board.test"

            def crawl(self, limit=None):
                return 0

            def _parse_list_page(self, raw):
                import re
                return [{"detail_url": f"/view?id={m}", "post_number": m}
                        for m in re.findall(r'href="/view\?id=(\d+)"', raw)]

            def _paper(self, item):
                return {"external_id": item["post_number"], "title": "x",
                        "url": f"https://board.test/view?id={item['post_number']}"}

            def _save_paper(self, paper):
                seq = super()._save_paper(paper)
                self._conn.execute("INSERT INTO documents(site_id,post_number,meta_url,title) VALUES(?,?,?,?)",
                                   ("fake-site", paper["external_id"], paper["url"] + "#dup", "dup"))
                self._conn.commit()
                return seq
        site = board_plan(listing={"parser": {"type": "adapter", "key_field": "post_number"}})
        site["adapter"] = {"type": "crawler", "list_method": "_parse_list_page", "list_args": ["html"],
                           "list_returns_paper": False, "paper_method": "_paper"}
        result = self.env.run(site, FakeBoard(3), crawler=Dup(self.env.conn))
        self.assertEqual(result["status"], "failed_duplicates", result)
        self.assertEqual(result["duplicates"]["new"], 3)

    def test_dates_are_normalized_by_save_path_and_never_invented(self):
        board = FakeBoard(4)
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete")
        row = self.env.conn.execute("SELECT published_date, published_date_raw, published_date_status "
                                    "FROM documents WHERE post_number='2'").fetchone()
        self.assertEqual(tuple(row), ("2000-01-03", "2000-01-03", "normalized"))
        report = site_report(self.env.store, self.env.site_id, doc_conn=self.env.conn)
        self.assertEqual(report["date_fill"]["published_date"], 1.0)

    def test_sitemap_lastmod_is_not_a_publication_date(self):
        from tests.test_enumeration_sort_sitemap import SitemapTest
        t = SitemapTest()
        t.env = self.env
        result = self.env.run(t.plan(), t.site_handler())
        self.assertEqual(result["status"], "complete")
        values = [r[0] for r in self.env.conn.execute("SELECT published_date FROM documents")]
        self.assertTrue(values and all(not v for v in values), values)
        report = site_report(self.env.store, self.env.site_id, doc_conn=self.env.conn)
        self.assertEqual(report["date_fill"]["published_date"], 0.0)
        # M5: a sitemap URL count is not independent evidence for itself.
        self.assertIsNone(report["gap"])
        self.assertIn("sitemap_url_count_not_independent", report["independent_evidence"])

    def test_json_raw_date_reaches_save_path(self):
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
        self.env.run(site, wp_handler(3))
        raw = self.env.conn.execute("SELECT published_date_raw FROM documents ORDER BY seq_id").fetchone()[0]
        self.assertEqual(raw, "2010-01-02T00:00:00")


# ---------------------------------------------------------------------------
# G3 isolated database, M4 sort order, M7 overrides cleanup, H4 network guard
# ---------------------------------------------------------------------------
class IsolationTest(unittest.TestCase):
    def test_cli_refuses_non_isolated_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"version": 1, "sites": {"s": {
                "strategy": "sitemap", "sitemap": {"sitemap_url": "https://x.test/s.xml"}}}}))
            for args in ([], ["--db", str(Path(tmp) / "libertree.db")]):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--plan", str(plan), "--state-dir", tmp, *args])
                self.assertIn("isolated", str(ctx.exception.code))
            self.assertFalse((Path(tmp) / "libertree.db").exists())


class SortOrderTest(unittest.TestCase):
    def test_ignored_sort_parameter_is_detected(self):
        env = Env()
        try:
            handler = wp_handler(250)

            def descending(url):
                resp = handler(url)
                if resp.status == 200 and "wp-json" in url:
                    data = json.loads(resp.body)
                    for row in data:
                        row["date"] = f"20{30 - row['id'] % 20:02d}-01-01T00:00:00"
                    resp = Response(200, resp.headers, json.dumps(data))
                return resp
            site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                    "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
            result = env.run(site, descending)
            self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "sort_order_not_ascending"))
        finally:
            env.close()


class OverrideCleanupTest(unittest.TestCase):
    def test_restore_all_undoes_module_constants(self):
        module = types.ModuleType("fake_capped_module_m7")
        module.MAX_PAGES = 200
        exec("from crawler.base_crawler import BaseCrawler\n"
             "class C(BaseCrawler):\n    site_id='m'; site_name='m'; base_url='https://m.test'\n"
             "    def crawl(self, limit=None):\n        return MAX_PAGES\n", module.__dict__)
        sys.modules[module.__name__] = module
        try:
            legacy.apply_overrides(module.C(None), {"page_cap": 9})
            self.assertEqual(module.MAX_PAGES, 9)
            legacy.restore_all()
            self.assertEqual(module.MAX_PAGES, 200)
        finally:
            del sys.modules[module.__name__]

    def test_worker_restores_overrides_between_jobs(self):
        source = (Path(__file__).resolve().parents[1] / "delivery" / "worker" / "worker.py").read_text()
        self.assertIn("restore_legacy_overrides()", source)


class NetworkGuardTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def run_with_method(self, body):
        ns = {}
        exec("from crawler.base_crawler import BaseCrawler\n"
             "class Net(BaseCrawler):\n    site_id='fake-site'; site_name='n'; base_url='https://board.test'\n"
             "    def crawl(self, limit=None):\n        return 0\n"
             "    def _parse_list_page(self, raw):\n        import re\n"
             "        return [{'detail_url': '/view?id='+m, 'post_number': m} "
             "for m in re.findall(r'href=\"/view\\?id=(\\d+)\"', raw)]\n"
             f"    def _detail(self, raw, item):\n{body}\n", ns)
        site = board_plan(listing={"parser": {"type": "adapter", "key_field": "post_number"}})
        site["adapter"] = {"type": "crawler", "list_method": "_parse_list_page", "list_args": ["html"],
                           "detail_method": "_detail", "detail_args": ["html", "item"]}
        return self.env.run(site, FakeBoard(2), crawler=ns["Net"](self.env.conn))

    def test_from_import_subprocess_is_blocked(self):
        result = self.run_with_method("        from subprocess import run\n        run(['true'])")
        self.assertEqual(result["status"], "config_error")
        self.assertTrue(result["stop_reason"].startswith("adapter_ungated_network"), result)

    def test_raw_socket_and_urllib_are_blocked(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            result = self.run_with_method(
                f"        import urllib.request\n        urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=1)")
            self.assertEqual(result["status"], "config_error")
            self.assertTrue(result["stop_reason"].startswith("adapter_ungated_network"), result)
            result = self.run_with_method(
                f"        import socket\n        socket.create_connection(('127.0.0.1', {port}), timeout=1)")
            self.assertTrue(result["stop_reason"].startswith("adapter_ungated_network"), result)
        finally:
            server.close()

    def test_guard_is_thread_local(self):
        from crawler.enumeration import guard
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        outcome = {}

        def other():
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.close()
            outcome["other"] = "connected"
        try:
            with guard.guarded():
                t = threading.Thread(target=other)
                t.start()
                t.join()
                with self.assertRaises(AdapterUngatedNetwork):
                    socket.create_connection(("127.0.0.1", port), timeout=1)
            self.assertEqual(outcome, {"other": "connected"})
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
