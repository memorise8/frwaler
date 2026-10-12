"""Second review round: crash between save and file queue, identity collisions,
sort check with pinned items, reference sampling, empty references, login
details, resume after deletions."""
from __future__ import annotations

import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler import db_libertree  # noqa: E402
from crawler.enumeration.http import Response  # noqa: E402
from crawler.enumeration.report import site_report, write_report  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, board_plan, wp_handler  # noqa: E402
from tests.test_enumeration_review import (LOGIN, crawler_plan, generic_crawler_for,  # noqa: E402
                                           reference_db)


class CrashBetweenSaveAndFileQueueTest(unittest.TestCase):
    def test_pdf_is_queued_after_crash_right_after_document_commit(self):
        env = Env()
        try:
            board = FakeBoard(3)
            original = env.store.enqueue_file
            calls = {"n": 0}

            def crash(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt("killed after the document commit")
                return original(*args, **kwargs)
            env.store.enqueue_file = crash
            with self.assertRaises(KeyboardInterrupt):
                env.run(board_plan(), board)
            env.store.enqueue_file = original
            self.assertEqual(len(env.docs()), 1)          # document committed
            self.assertEqual(env.store.files(env.site_id), [])  # PDF not queued yet
            result = env.run(board_plan(), board)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(len(env.docs()), 3)
            urls = sorted(f["url"].rsplit("/", 1)[1] for f in env.store.files(env.site_id))
            self.assertEqual(urls, ["1.pdf", "2.pdf", "3.pdf"])
            row = env.store.item(env.site_id, "1")
            self.assertEqual((row["status"], row["reason"]), ("saved", "existing_document"))
        finally:
            env.close()


class IdentityCollisionTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_same_post_number_other_document_is_a_collision(self):
        db_libertree.insert_document(self.env.conn, {"site_id": "fake-site", "post_number": "3",
                                                     "meta_url": "https://board.test/old/3", "title": "old"})
        result = self.env.run(board_plan(), FakeBoard(5))
        old = self.env.conn.execute("SELECT seq_id, meta_url, title FROM documents WHERE post_number='3'").fetchall()
        self.assertEqual([tuple(r)[1:] for r in old], [("https://board.test/old/3", "old")])  # untouched
        row = self.env.store.item(self.env.site_id, "3")
        self.assertEqual((row["status"], row["reason"], row["seq_id"]),
                         ("failed_permanent", "identity_collision", None))
        self.assertTrue(self.env.store.events(self.env.site_id, "identity_collision"))
        self.assertEqual(result["duplicates"]["new"], 0)
        report = site_report(self.env.store, self.env.site_id, doc_conn=self.env.conn)
        self.assertEqual((report["saved"], report["identity_collisions"], report["skipped_existing_post_number"]),
                         (4, 1, 0))
        self.assertEqual(report["date_fill"]["documents"], 4)

    def test_same_document_reprocessed_is_counted_not_collided(self):
        db_libertree.insert_document(self.env.conn, {"site_id": "fake-site", "post_number": "3",
                                                     "meta_url": "https://board.test/view?id=3", "title": "Title 3"})
        result = self.env.run(board_plan(), FakeBoard(5))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.env.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
        report = site_report(self.env.store, self.env.site_id, doc_conn=self.env.conn)
        self.assertEqual((report["saved"], report["identity_collisions"], report["skipped_existing_post_number"]),
                         (5, 0, 1))
        out = self.env.root / "rep"
        write_report(self.env.store, [self.env.site_id], out, doc_conn=self.env.conn)
        header = (out / "completeness.csv").read_text().splitlines()[0]
        self.assertIn("skipped_existing_post_number", header)
        self.assertIn("identity_collisions", header)

    def test_identity_review_exception_is_loud_and_first(self):
        env = Env("krivet-re-kr")
        try:
            # Two stored rows for one pstNo: the KRIVET rule demands review.
            for q in ("pstNo=7&a=1", "a=2&pstNo=7"):  # raw inserts: the legacy defect being guarded
                env.conn.execute("INSERT INTO documents(site_id, post_number, meta_url, title) VALUES(?,?,?,?)",
                                 ("krivet-re-kr", "7", f"https://www.krivet.re.kr/v?{q}", "x"))
            env.conn.commit()

            def handler(url):
                parts = urlsplit(url)
                if parts.path == "/robots.txt":
                    return Response(404, {}, b"")
                q = parse_qs(parts.query)
                if parts.path == "/list" and q.get("page", ["1"]) == ["1"]:
                    body = '<table><tbody><tr><td><a href="/v?pstNo=7&a=3">T7</a></td></tr></tbody></table>'
                else:
                    body = "<table><tbody></tbody></table>"
                return Response(200, {"Content-Type": "text/html"}, f"<html><body>{body}</body></html>")
            site = board_plan(base_url="https://www.krivet.re.kr",
                              listing={"url": "https://www.krivet.re.kr/list", "total": None,
                                       "parser": {"type": "html", "item_container": "tbody tr",
                                                  "item_link": "a[href]", "id_regex": r"pstNo=(\d+)"}},
                              adapter={"type": "list_only", "id_regex": r"pstNo=(\d+)"})
            env.run(site, handler)
            row = env.store.item(env.site_id, "7")
            self.assertEqual((row["status"], row["reason"]),
                             ("failed_permanent", "identity_review:krivet_identity_collision_requires_review"))
            self.assertTrue(env.store.events(env.site_id, "identity_review_required"))
            self.assertEqual(env.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
        finally:
            env.close()


class SortCheckTest(unittest.TestCase):
    def pinned_handler(self, sticky_flag):
        base = wp_handler(250)

        def handler(url):
            resp = base(url)
            q = parse_qs(urlsplit(url).query)
            if resp.status == 200 and "wp-json" in url and q.get("page") == ["1"]:
                data = json.loads(resp.body)
                pinned = {"id": 9999, "link": "https://wp.test/?p=9999", "date": "2026-10-01T00:00:00",
                          "title": {"rendered": "Pinned notice"}}
                if sticky_flag:
                    pinned["sticky"] = True
                resp = Response(200, resp.headers, json.dumps([pinned] + data))
            return resp
        return handler

    def site(self, **extra):
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
        site.update(extra)
        return site

    def test_one_pinned_item_counts_once(self):
        env = Env()
        try:
            result = env.run(self.site(oldest_sort={"max_order_violations": 1}), self.pinned_handler(False))
            self.assertNotEqual(result["stop_reason"], "sort_order_not_ascending", result)
            self.assertEqual(len(env.docs()), 251)
        finally:
            env.close()

    def test_pinned_items_can_be_excluded(self):
        env = Env()
        try:
            site = self.site()
            site["listing"]["parser"] = {"exclude_if_true": "sticky"}
            result = env.run(site, self.pinned_handler(True))
            self.assertNotEqual(result["stop_reason"], "sort_order_not_ascending", result)
            self.assertEqual(len(env.docs()), 250)
        finally:
            env.close()


class ReferenceSamplingTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_sample_comes_from_reference_rows_even_for_oldest_first_listing(self):
        # Oldest-first board: the first listing page holds the oldest items, which
        # the legacy crawler (newest-first, 200 pages) never saved.
        board = FakeBoard(60)
        board.ids = list(range(1, 61))  # ascending: page 1 = oldest
        ref = reference_db([(str(i), f"https://board.test/view?id={i}") for i in range(50, 61)])
        result = self.env.run(crawler_plan(strategy="oldest_first_sort", last_page=None), board,
                              crawler=generic_crawler_for(self.env), reference_conn=ref, identity_required=True)
        self.assertNotEqual(result["status"], "config_error", result)
        self.assertEqual(result["identity"]["match_rate"], 1.0)
        self.assertEqual(result["identity"]["compared"], 5)
        ref.close()

    def test_krivet_style_legacy_urls_compare_by_dedup_rule(self):
        env = Env("krivet-re-kr")
        try:
            def handler(url):
                parts = urlsplit(url)
                if parts.path == "/robots.txt":
                    return Response(404, {}, b"")
                return Response(200, {"Content-Type": "text/html"}, "<html><body><table><tbody>"
                                "</tbody></table></body></html>")
            ref = reference_db([(str(i), f"https://www.krivet.re.kr/v?menu=9&pstNo={i}") for i in range(1, 7)],
                               site_id="krivet-re-kr")
            crawler = generic_crawler_for(env, site_id="krivet-re-kr", base_url="https://www.krivet.re.kr",
                                          id_regex=None)
            site = crawler_plan(base_url="https://www.krivet.re.kr",
                                listing={"url": "https://www.krivet.re.kr/list", "parser": {"type": "adapter"}})
            result = env.run(site, handler, crawler=crawler, reference_conn=ref, identity_required=True)
            self.assertEqual(result["identity"]["match_rate"], 1.0, result)
            ref.close()
        finally:
            env.close()

    def test_empty_reference_needs_explicit_flag(self):
        ref = reference_db([], site_id="other-site")
        result = self.env.run(crawler_plan(), FakeBoard(5), crawler=generic_crawler_for(self.env),
                              reference_conn=ref, identity_required=True)
        self.assertEqual((result["status"], result["stop_reason"]), ("config_error", "identity_reference_empty"))
        allowed = self.env.run(crawler_plan(), FakeBoard(5), crawler=generic_crawler_for(self.env),
                               reference_conn=ref, identity_required=True, allow_empty_reference=True)
        self.assertEqual(allowed["status"], "complete", allowed)
        self.assertEqual(allowed["identity"]["mode"], "no_reference_rows_allowed")
        report = site_report(self.env.store, self.env.site_id)
        self.assertIn("identity_reference_empty_allowed", report["warnings"])
        ref.close()


class LoginDetailsTest(unittest.TestCase):
    def test_consecutive_login_details_stop_for_review(self):
        env = Env()
        try:
            board = FakeBoard(20)
            board.detail_override = {i: LOGIN.decode() for i in range(1, 21)}
            transport_result = env.run(board_plan(), board)
            self.assertEqual((transport_result["status"], transport_result["stop_reason"]),
                             ("review_pending", "login_required_details"))
            failed = env.store._all("SELECT COUNT(*) AS n FROM enum_item WHERE reason='login_required_suspect'")
            self.assertEqual(failed[0]["n"], 5)
        finally:
            env.close()


class ResumeAfterDeletionTest(unittest.TestCase):
    def test_last_pages_vanishing_between_runs_are_re_probed(self):
        env = Env()
        try:
            board = FakeBoard(95, show_total=False)
            first = env.run(board_plan(adapter={"type": "list_only", "id_regex": r"id=(\d+)"}), board,
                            max_requests=20)
            self.assertEqual(first["status"], "incomplete", first)
            saved_before = {int(d["post_number"]) for d in env.docs()}
            # The oldest 30 posts are deleted: the stored next page no longer exists.
            board.ids = [i for i in board.ids if i > 30]
            result = env.run(board_plan(adapter={"type": "list_only", "id_regex": r"id=(\d+)"}), board)
            self.assertEqual(result["status"], "complete", result)
            saved = {int(d["post_number"]) for d in env.docs()}
            self.assertEqual(saved, saved_before | set(range(31, 96)))
            self.assertTrue(env.store.events(env.site_id, "last_page_reprobed"))
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
