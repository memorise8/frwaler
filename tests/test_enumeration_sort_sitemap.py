"""oldest_first_sort presets and sitemap enumeration (no network)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response  # noqa: E402
from tests.enumeration_fakes import Env, FakeTransport, gz, wp_handler  # noqa: E402


def wp_plan(**over):
    site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
            "listing": {"preset": "wordpress", "url": "https://wp.test/wp-json/wp/v2/posts"}}
    site.update(over)
    return site


class OldestFirstSortTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_wordpress_ascending_with_total_header(self):
        handler = wp_handler(250)
        transport = FakeTransport(handler)
        result = self.env.run(wp_plan(), None, transport=transport)
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "exhausted_end_status_400"))
        ids = [int(d["post_number"]) for d in self.env.docs()]
        self.assertEqual(ids, list(range(1, 251)))
        lists = [u for u in transport.urls if "wp-json" in u]
        # 3 pages, the 400 end page, its confirming re-read and the next page (also 400)
        self.assertEqual(len(lists), 6)

    def test_wordpress_total_mismatch(self):
        handler = wp_handler(120)
        handler.total = 130
        result = self.env.run(wp_plan(), None, transport=FakeTransport(handler))
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_with_mismatch", "reported_total_mismatch"))

    def test_empty_first_page_is_not_completion(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            return Response(400, {"Content-Type": "application/json"}, b'{"code":"rest_invalid_param"}')
        result = self.env.run(wp_plan(), handler)
        # A 400 that is not WordPress's "invalid page number" is an error, never an end.
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("incomplete", "listing_failed:unverified_error_status_400"))

    def test_ckan_offset_paging(self):
        datasets = [{"id": f"id-{i}", "name": f"ds-{i}", "title": f"Dataset {i}",
                     "metadata_created": f"2015-{1 + i // 28:02d}-{(i % 28) + 1:02d}T00:00:00"} for i in range(1, 231)]

        def handler(url):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            assert q["sort"] == "metadata_created asc", q
            start, rows = int(q["start"]), int(q["rows"])
            body = {"success": True, "result": {"count": len(datasets), "results": datasets[start:start + rows]}}
            return Response(200, {"Content-Type": "application/json"}, json.dumps(body))

        transport = FakeTransport(handler)
        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "ckan", "url": "https://ckan.test/api/3/action/package_search"}}
        result = self.env.run(site, None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        docs = self.env.docs()
        self.assertEqual([d["post_number"] for d in docs], [f"id-{i}" for i in range(1, 231)])
        self.assertEqual(docs[0]["meta_url"], "https://ckan.test/dataset/ds-1")
        starts = [parse_qs(urlsplit(u).query)["start"][0] for u in transport.urls if "package_search" in u]
        self.assertEqual(starts, ["0", "100", "200", "300", "300", "400"])  # end re-read + neighbour

    def test_dspace7_discover_objects(self):
        objs = [{"_embedded": {"indexableObject": {
            "uuid": f"u{i}", "handle": f"123/{i}", "name": f"Item {i}",
            "metadata": {"dc.date.accessioned": [{"value": f"2012-{1 + i // 28:02d}-{(i % 28) + 1:02d}T10:00:00Z"}]}}}}
            for i in range(1, 46)]

        def handler(url):
            parts = urlsplit(url)
            if parts.path == "/robots.txt":
                return Response(404, {}, b"")
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            assert q["sort"] == "dc.date.accessioned,ASC"
            page, size = int(q["page"]), int(q["size"])
            body = {"_embedded": {"searchResult": {
                "_embedded": {"objects": objs[page * size:(page + 1) * size]},
                "page": {"totalElements": len(objs), "number": page, "size": size}}}}
            return Response(200, {"Content-Type": "application/json"}, json.dumps(body))

        site = {"strategy": "oldest_first_sort", "request_interval_s": 0, "adapter": {"type": "list_only"},
                "listing": {"preset": "dspace7", "url": "https://repo.test/server/api/discover/search/objects",
                            "params": {"size": 20}}}
        result = self.env.run(site, handler)
        self.assertEqual(result["status"], "complete", result)
        docs = self.env.docs()
        self.assertEqual(len(docs), 45)
        self.assertEqual(docs[0]["meta_url"], "https://repo.test/handle/123/1")


URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>"""
INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>"""


def urlset(urls):
    # lastmod deliberately runs opposite to the publication order.
    return URLSET.format("".join(f"<url><loc>{u}</loc><lastmod>2026-0{1 + (n % 9)}-01</lastmod></url>"
                                 for n, u in enumerate(urls))).encode()


class SitemapTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def site_handler(self, broken_child=False):
        reports1 = [f"https://cd.test/publications/report-{i}/" for i in (30, 2, 11)]
        reports2 = [f"https://cd.test/publications/report-{i}/" for i in (5, 41)]
        pages = ["https://cd.test/about/", "https://cd.test/contact/"]

        def handler(url):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /admin/\n"
                                                                     b"Sitemap: https://cd.test/sitemap_index.xml\n")
            if path == "/sitemap_index.xml":
                locs = "".join(f"<sitemap><loc>https://cd.test/{n}</loc></sitemap>"
                               for n in ("report-sitemap.xml", "report-sitemap2.xml.gz", "page-sitemap.xml"))
                return Response(200, {"Content-Type": "application/xml"}, INDEX.format(locs).encode())
            if path == "/report-sitemap.xml":
                return Response(200, {"Content-Type": "application/xml"}, urlset(reports1 + pages[:1]))
            if path == "/report-sitemap2.xml.gz":
                if broken_child:
                    return Response(200, {"Content-Type": "application/xml"}, b"<urlset><url><loc>broken")
                return Response(200, {"Content-Type": "application/x-gzip"}, gz(urlset(reports2)))
            if path == "/page-sitemap.xml":
                return Response(200, {"Content-Type": "application/xml"}, urlset(pages))
            if path.startswith("/publications/"):
                return Response(200, {"Content-Type": "text/html"},
                                f"<html><h1>Report {path.split('-')[-1].strip('/')}</h1></html>".encode())
            return Response(404, {}, b"")
        return handler

    def plan(self, **sm):
        cfg = {"sitemap_url": "https://cd.test/sitemap_index.xml", "index_include": [r"report-sitemap\d*\.xml"],
               "include": ["/publications/"], "order_by_url_number": r"report-(\d+)"}
        cfg.update(sm)
        # Sitemap entries carry no title: the detail page is required.
        return {"strategy": "sitemap", "request_interval_s": 0, "base_url": "https://cd.test",
                "adapter": {"type": "selectors", "selectors": {"title": "h1"}}, "sitemap": cfg}

    def test_index_children_filters_and_gzip(self):
        transport = FakeTransport(self.site_handler())
        result = self.env.run(self.plan(), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        urls = [d["meta_url"] for d in self.env.docs()]
        # Sorted by the number in the URL inside each child, never by lastmod.
        self.assertEqual(urls, [f"https://cd.test/publications/report-{i}/" for i in (2, 11, 30, 5, 41)])
        self.assertFalse([u for u in transport.urls if "page-sitemap" in u])

    def test_sitemap_from_robots(self):
        result = self.env.run(self.plan(sitemap_url=None, from_robots=True), self.site_handler())
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 5)

    def test_broken_child_keeps_site_incomplete(self):
        result = self.env.run(self.plan(), self.site_handler(broken_child=True))
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "sitemaps_failed:1"))
        self.assertEqual(len(self.env.docs()), 3)
        self.assertTrue(self.env.store.events(self.env.site_id, "sitemap_failed"))


if __name__ == "__main__":
    unittest.main()
