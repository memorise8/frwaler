"""Request-policy fixes after the first real-site runs (fake transports only, no network)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import delivery.health.request_gate as request_gate  # noqa: E402
from crawler.enumeration import http as H  # noqa: E402
from crawler.enumeration.http import Fetcher, Response  # noqa: E402
from tests.enumeration_fakes import FakeTransport  # noqa: E402


class FakeClock:
    """Wall clock + monotonic clock + sleep sharing one fake time line."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


def ok(body=b"<html><body><p>x</p></body></html>", **headers):
    return Response(200, {"Content-Type": "text/html", **headers}, body)


class CrawlDelayTest(unittest.TestCase):
    """HIGH-1: the gap between the end of one response and the next request is >= Crawl-delay."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        patches = [mock.patch.object(request_gate, "time", self.clock), mock.patch.object(H, "time", self.clock)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pages(self, delay, n=3, interval=1.5):
        starts = []

        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, f"User-agent: *\nCrawl-delay: {delay}\n".encode())
            starts.append(self.clock.t)
            self.clock.t += 0.5  # response time
            return ok()

        f = Fetcher("s", FakeTransport(handler), Path(self.tmp.name) / "gates", interval=interval,
                    clock=self.clock.monotonic, sleep=self.clock.sleep)
        for i in range(n):
            f.get(f"https://example.org/p{i}")
        # gap = next start - previous end (previous start + 0.5)
        return [round(b - (a + 0.5), 3) for a, b in zip(starts, starts[1:])]

    def test_crawl_delay_above_gate_ceiling_is_fully_respected(self):
        gaps = self.run_pages(20)
        self.assertTrue(gaps and all(g >= 20 for g in gaps), gaps)
        self.assertTrue(all(g < 21 for g in gaps), gaps)  # and not doubled

    def test_crawl_delay_under_ceiling_unchanged(self):
        gaps = self.run_pages(5)
        self.assertTrue(all(5 <= g < 5.5 for g in gaps), gaps)

    def test_fractional_crawl_delay_above_ceiling(self):
        gaps = self.run_pages(12.5)
        self.assertTrue(all(12.5 <= g < 13 for g in gaps), gaps)


UA = H.DEFAULT_USER_AGENT


class RobotsMatcherTest(unittest.TestCase):
    """HIGH-2: RFC 9309 matching (groups, longest match, Allow on ties, '*' and '$', %-normalisation)."""

    def allowed(self, robots, url, ua=UA):
        return H.parse_robots(robots).allows(url, ua)

    def test_wildcard_and_end_anchor(self):
        robots = "User-agent: *\nDisallow: /*?page=\nDisallow: /search/*.php$\n"
        self.assertFalse(self.allowed(robots, "https://x.org/list?page=2"))
        self.assertTrue(self.allowed(robots, "https://x.org/list"))
        self.assertFalse(self.allowed(robots, "https://x.org/search/a/b.php"))
        self.assertTrue(self.allowed(robots, "https://x.org/search/a/b.php?x=1"))
        self.assertTrue(self.allowed(robots, "https://x.org/search/a/b.phpx"))

    def test_longest_match_wins_regardless_of_order(self):
        for robots in ("User-agent: *\nDisallow: /\nAllow: /public\n",
                       "User-agent: *\nAllow: /public\nDisallow: /\n"):
            self.assertTrue(self.allowed(robots, "https://x.org/public/x"), robots)
            self.assertFalse(self.allowed(robots, "https://x.org/other"), robots)
        robots = "User-agent: *\nAllow: /a\nDisallow: /a/b\n"
        self.assertFalse(self.allowed(robots, "https://x.org/a/b/c"))
        self.assertTrue(self.allowed(robots, "https://x.org/a/c"))

    def test_equal_length_allow_wins_and_empty_disallow_allows(self):
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "https://x.org/page/1"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow:\n", "https://x.org/anything"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /\n", "https://x.org/"))
        self.assertFalse(self.allowed("User-agent: *\nDisallow: /\n", "https://x.org"))

    def test_group_selection(self):
        robots = ("User-agent: Googlebot\nDisallow: /\n\n"
                  "User-agent: *\nDisallow: /private\n")
        self.assertTrue(self.allowed(robots, "https://x.org/x"))
        self.assertFalse(self.allowed(robots, "https://x.org/private/1"))
        # A group naming our product token (first product of the User-Agent) replaces the '*' group.
        robots = "User-agent: *\nDisallow: /\n\nUser-agent: Mozilla/5.0\nDisallow: /c\n"
        self.assertTrue(self.allowed(robots, "https://x.org/x"))
        self.assertFalse(self.allowed(robots, "https://x.org/c"))
        # Other words of the browser User-Agent never relax a stricter '*' group.
        robots = "User-agent: *\nDisallow: /\n\nUser-agent: Chrome\nDisallow:\n"
        self.assertFalse(self.allowed(robots, "https://x.org/x"))
        # Several user-agent lines share one group; groups for the same agent merge.
        robots = ("User-agent: foo\nUser-agent: *\nDisallow: /a\n\n"
                  "user-agent: *\ndisallow: /b\n")
        self.assertFalse(self.allowed(robots, "https://x.org/a"))
        self.assertFalse(self.allowed(robots, "https://x.org/b"))
        # Rules before any user-agent line belong to no group.
        self.assertTrue(self.allowed("Disallow: /\nUser-agent: *\nDisallow: /z\n", "https://x.org/a"))
        self.assertTrue(self.allowed("", "https://x.org/a"))

    def test_percent_encoding_normalisation(self):
        robots = "User-agent: *\nDisallow: /caf%C3%A9\nDisallow: /a%7Eb\nDisallow: /é\n"
        self.assertFalse(self.allowed(robots, "https://x.org/café/1"))
        self.assertFalse(self.allowed(robots, "https://x.org/caf%c3%a9/1"))
        self.assertFalse(self.allowed(robots, "https://x.org/a~b"))
        self.assertFalse(self.allowed(robots, "https://x.org/%C3%A9"))
        # A reserved character stays encoded: %2F is not '/'.
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /a/b\n", "https://x.org/a%2Fb"))

    def test_crawl_delay_never_shortened_by_a_specific_group(self):
        robots = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: Chrome\nCrawl-delay: 1\n"
        self.assertEqual(H.crawl_delay(robots, UA), 10)
        robots = "User-agent: *\nCrawl-delay: 2\n\nUser-agent: Chrome\nCrawl-delay: 4.5\n"
        self.assertEqual(H.crawl_delay(robots, UA), 4.5)

    def test_bom_and_comments(self):
        robots = "﻿User-agent: * # all\nDisallow: /x # no\n"
        self.assertFalse(self.allowed(robots, "https://x.org/x"))


class RobotsFetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler, **kw):
        kw.setdefault("interval", 0)
        kw.setdefault("sleep", lambda s: None)
        t = FakeTransport(handler)
        return Fetcher("s", t, Path(self.tmp.name) / "gates", **kw), t

    def test_allow_after_disallow_all(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\nAllow: /public\n")
            return ok()
        f, _ = self.fetcher(handler)
        self.assertEqual(f.get("https://x.org/public/x").status, 200)
        with self.assertRaises(H.AccessBlocked) as ctx:
            f.get("https://x.org/other")
        self.assertEqual(ctx.exception.reason, "robots_disallowed")

    def test_unparseable_robots_blocks(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /x\n")
            return ok()
        f, t = self.fetcher(handler)
        with mock.patch.object(H, "parse_robots", side_effect=ValueError("boom")):
            with self.assertRaises(H.AccessBlocked) as ctx:
                f.get("https://x.org/a")
        self.assertEqual(ctx.exception.reason, "robots_unparseable")
        self.assertEqual(t.urls, ["https://x.org/robots.txt"])

    def test_redirected_robots_crawl_delay_keyed_to_origin_host(self):
        def handler(url):
            if url == "https://a.test/robots.txt":
                return Response(301, {"Location": "https://b.test/robots.txt"}, b"")
            if url == "https://b.test/robots.txt":
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nCrawl-delay: 4\n")
            return ok()
        f, _ = self.fetcher(handler)
        f.get("https://a.test/x")
        self.assertEqual(f._host_delay.get("a.test"), 4)
        self.assertNotIn("b.test", f._host_delay)


class RobotsUnavailableTest(unittest.TestCase):
    """MEDIUM-3: an unreachable robots.txt stops the site and is not re-requested for every URL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_cached_then_retried_after_ttl(self):
        def handler(url):
            return Response(503, {}, b"") if url.endswith("/robots.txt") else ok()
        t = FakeTransport(handler)
        f = Fetcher("s", t, Path(self.tmp.name) / "gates", interval=0, retries=3, sleep=lambda s: None,
                    clock=self.clock.monotonic)
        for i in range(3):
            with self.assertRaises(H.RobotsUnavailable) as ctx:
                f.get(f"https://x.org/p{i}")
            self.assertEqual(ctx.exception.reason, "robots_unavailable:http_503")
        self.assertEqual(t.urls, ["https://x.org/robots.txt"] * 3)  # one robots fetch (3 tries), no page
        self.clock.t += H.ROBOTS_RETRY_S + 1
        with self.assertRaises(H.RobotsUnavailable):
            f.get("https://x.org/p9")
        self.assertEqual(len(t.urls), 6)

    def test_runner_stops_incomplete_without_item_requests(self):
        from tests.enumeration_fakes import Env, FakeBoard, board_plan
        env = Env()
        self.addCleanup(env.close)
        board = FakeBoard(25)

        def handler(url):
            return Response(503, {}, b"") if url.endswith("/robots.txt") else board(url)
        result = env.run(board_plan(), handler)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "robots_unavailable:http_503"))
        urls = result["transport"].urls
        self.assertTrue(urls and all(u.endswith("/robots.txt") for u in urls), urls)
        self.assertEqual(len(urls), 3)
        self.assertIsNone(env.store.host_hold("board.test"))  # not a refusal: no host hold
        self.assertEqual(env.store.events(env.site_id, "robots_unavailable")[-1]["detail"]["reason"],
                         "robots_unavailable:http_503")

    def test_file_pass_stops_without_attempt(self):
        from crawler.enumeration.files import FilePass
        from tests.enumeration_fakes import Env
        env = Env()
        self.addCleanup(env.close)
        env.store.enqueue_file(env.site_id, 1, "https://cdn.test/a.pdf")
        t = FakeTransport(lambda u: Response(503, {}, b""))
        f = Fetcher(env.site_id, t, env.root / "gates", interval=0, sleep=lambda s: None, holds=env.store)
        stats = FilePass(env.store, f, blob_root=env.root / "blobs").run(env.site_id)
        self.assertEqual(stats["stopped"], "robots_unavailable:http_503")
        row = env.store.files(env.site_id)[0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))


class ItemRobotsTest(unittest.TestCase):
    """MEDIUM-4: robots denial of one detail/file URL is an item failure; listing denial still stops."""

    def setUp(self):
        from tests.enumeration_fakes import Env
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_detail_url_disallowed_is_item_failure(self):
        from tests.enumeration_fakes import FakeBoard, board_plan
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /view?id=5$\n")
        result = self.env.run(board_plan(), board)
        self.assertEqual(result["status"], "complete", result)
        row = self.env.store.item(self.env.site_id, "5")
        self.assertEqual((row["status"], row["reason"]), ("failed_permanent", "robots_disallowed"))
        urls = result["transport"].urls
        self.assertNotIn("https://board.test/view?id=5", urls)
        self.assertIn("https://board.test/view?id=4", urls)
        self.assertEqual(len(self.env.docs()), 24)
        self.assertIsNone(self.env.store.host_hold("board.test"))

    def test_listing_url_disallowed_still_stops_site(self):
        from tests.enumeration_fakes import FakeBoard, board_plan
        board = FakeBoard(25, robots=b"User-agent: *\nDisallow: /list\n")
        result = self.env.run(board_plan(), board)
        self.assertEqual((result["status"], result["stop_reason"]), ("access_blocked", "robots_disallowed"))

    def complete_site_with_files(self, handler, urls):
        from crawler.enumeration.files import FilePass
        st = self.env.store
        st.start_site(self.env.site_id, "fp", "last_page_backward")
        st.finish_site(self.env.site_id, "complete", "done", {})
        for i, url in enumerate(urls):
            st.enqueue_file(self.env.site_id, i + 1, url, role="attachment")
        t = FakeTransport(handler)
        f = Fetcher(self.env.site_id, t, self.env.root / "gates", interval=0, sleep=lambda s: None, holds=st)
        return FilePass(st, f, blob_root=self.env.root / "blobs").run(self.env.site_id), t

    def test_file_url_disallowed_is_file_failure_and_site_stays_complete(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n")
            return Response(200, {"Content-Type": "application/octet-stream"}, b"data")
        stats, t = self.complete_site_with_files(handler, ["https://f.test/private/a.bin", "https://f.test/b.bin"])
        files = {r["url"]: r for r in self.env.store.files(self.env.site_id)}
        self.assertEqual((files["https://f.test/private/a.bin"]["status"],
                          files["https://f.test/private/a.bin"]["reason"]), ("failed", "robots_disallowed"))
        self.assertEqual(files["https://f.test/b.bin"]["status"], "done")
        self.assertIsNone(stats["stopped"])
        self.assertEqual(self.env.store.site(self.env.site_id)["status"], "complete")

    def test_file_refusal_holds_host_without_overwriting_complete(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            return Response(403, {}, b"denied")
        stats, t = self.complete_site_with_files(handler, ["https://f.test/a.bin", "https://f.test/b.bin"])
        self.assertEqual(stats["stopped"], "access_blocked:access_refused")
        site = self.env.store.site(self.env.site_id)
        self.assertEqual(site["status"], "complete")
        self.assertIsNotNone(self.env.store.host_hold("f.test"))
        self.assertEqual([u for u in t.urls if not u.endswith("robots.txt")], ["https://f.test/a.bin"])
        self.assertTrue(self.env.store.events(self.env.site_id, "file_pass_stopped"))


class HostNormalisationTest(unittest.TestCase):
    """MEDIUM-5: IDN hosts go through the gate as IDNA; ungovernable hosts are a PermanentFailure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler):
        t = FakeTransport(handler)
        return Fetcher("s", t, Path(self.tmp.name) / "gates", interval=0, sleep=lambda s: None), t

    def test_idn_host_is_requested_as_idna(self):
        f, t = self.fetcher(lambda u: Response(404, {}, b"") if u.endswith("robots.txt") else ok())
        resp = f.get("https://café.fr:8443/x?q=1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(t.urls, ["https://xn--caf-dma.fr:8443/robots.txt", "https://xn--caf-dma.fr:8443/x?q=1"])

    def test_underscore_host_is_permanent_failure_without_request(self):
        f, t = self.fetcher(lambda u: ok())
        with self.assertRaises(H.PermanentFailure) as ctx:
            f.get("https://my_host.example.org/x")
        self.assertEqual(ctx.exception.reason, "ungovernable_host")
        self.assertEqual(t.urls, [])

    def test_redirect_to_ungovernable_host(self):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            return Response(302, {"Location": "https://bad_host.example.org/y"}, b"")
        f, t = self.fetcher(handler)
        with self.assertRaises(H.PermanentFailure):
            f.get("https://ok.example.org/x")
        self.assertFalse([u for u in t.urls if "bad_host" in u])

    def test_robots_redirect_to_ungovernable_host_is_not_permission(self):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(302, {"Location": "https://bad_host.example.org/robots.txt"}, b"")
            return ok()
        f, t = self.fetcher(handler)
        with self.assertRaises(H.RobotsUnavailable):
            f.get("https://ok.example.org/x")
        self.assertEqual(t.urls, ["https://ok.example.org/robots.txt"])

    def test_runner_does_not_crash_on_ungovernable_listing_host(self):
        from tests.enumeration_fakes import Env, FakeBoard, board_plan
        env = Env()
        self.addCleanup(env.close)
        site = board_plan(base_url="https://my_board.test",
                          listing={"url": "https://my_board.test/list"})
        result = env.run(site, FakeBoard(5))
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "listing_failed:ungovernable_host"))
        self.assertEqual(result["transport"].urls, [])


class LocationEncodingTest(unittest.TestCase):
    """MEDIUM-2: a raw UTF-8 Location (read as latin-1 by http.client) is re-decoded like requests does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def follow(self, location):
        def handler(url):
            if url.endswith("robots.txt"):
                return Response(404, {}, b"")
            if url.endswith("/r"):
                return Response(302, {"Location": location}, b"")
            return ok()
        t = FakeTransport(handler)
        f = Fetcher("s", t, Path(self.tmp.name) / "gates", interval=0, sleep=lambda s: None)
        f.get("https://x.org/r")
        return t.urls[-1]

    def test_raw_utf8_location(self):
        self.assertEqual(self.follow("/café/doc".encode("utf-8").decode("latin-1")), "https://x.org/café/doc")

    def test_latin1_or_ascii_location_unchanged(self):
        self.assertEqual(self.follow("/caf\xe9"), "https://x.org/caf\xe9")
        self.assertEqual(self.follow("/caf%C3%A9"), "https://x.org/caf%C3%A9")


class ResponseEncodingTest(unittest.TestCase):
    """MEDIUM-6: quoted charset, BOM (also for JSON), meta charset further into <head>."""

    def test_quoted_charset_header(self):
        for ctype in ('text/html; charset="iso-8859-1"', "text/html; charset='iso-8859-1'", "text/html;charset = ISO-8859-1"):
            r = Response(200, {"Content-Type": ctype}, "<p>café</p>".encode("latin-1"))
            self.assertEqual(r.text(), "<p>café</p>", ctype)

    def test_bom_wins_and_is_stripped(self):
        r = Response(200, {"Content-Type": "application/json"}, b"\xef\xbb\xbf{\"a\": \"\xc3\xa9\"}")
        self.assertEqual(r.json(), {"a": "é"})
        r = Response(200, {"Content-Type": "text/html; charset=iso-8859-1"}, b"\xef\xbb\xbf<p>caf\xc3\xa9</p>")
        self.assertEqual(r.text(), "<p>café</p>")
        r = Response(200, {}, "﻿<p>café</p>".encode("utf-16-le"))
        self.assertEqual(r.text(), "<p>café</p>")

    def test_meta_charset_after_large_head(self):
        head = "<html><head><script>" + "x" * 6000 + "</script><meta charset=\"windows-1252\"></head>"
        r = Response(200, {"Content-Type": "text/html"}, (head + "<body>café</body></html>").encode("cp1252"))
        self.assertIn("café", r.text())
        head = ("<html><head>" + "<!-- pad -->" * 600 +
                '<meta http-equiv="Content-Type" content="text/html; charset=euc-kr"></head>')
        r = Response(200, {"Content-Type": "text/html"}, (head + "<body>한글</body>").encode("euc-kr"))
        self.assertIn("한글", r.text())

    def test_meta_charset_in_body_text_is_ignored(self):
        body = ("<html><head><title>t</title></head><body><pre>&lt;meta charset=\"iso-8859-1\"&gt;</pre>"
                '<meta charset="iso-8859-1"> é</body></html>').encode("utf-8")
        self.assertIn("é", Response(200, {"Content-Type": "text/html"}, body).text())


def page(head_title, body):
    return f"<html><head><title>{head_title}</title></head><body>{body}</body></html>".encode()


class LoginPageTest(unittest.TestCase):
    """MEDIUM-7: a login modal in the page chrome is not a login page; real login pages still are."""

    NAV = "<header><nav><a href='/'>Accueil</a><a href='/pubs'>Publications</a></nav></header>"
    FOOT = "<footer><a href='/legal'>Mentions</a></footer>"

    def test_header_modal_with_content_is_not_login(self):
        html = (b"<html><head><title>Rapport annuel 2024</title></head><body><header><nav><a href='/'>Accueil</a>"
                b"</nav><div class=modal><h2>Connexion</h2><form><input type=password name=p></form></div></header>"
                b"<main><h1>Rapport annuel 2024</h1><a href='/doc.pdf'>PDF</a></main></body></html>")
        self.assertFalse(H.login_page(html))

    def test_dialog_outside_header_is_not_login(self):
        body = (self.NAV + "<main><h1>Étude 12</h1><p>Résumé</p><a href='/e12.pdf'>PDF</a></main>"
                "<div id='loginModal' class='modal fade' role='dialog'><h2>Log in</h2>"
                "<form><input type='password' name='pw'></form></div>" + self.FOOT)
        self.assertFalse(H.login_page(page("Étude 12", body)))
        body = (self.NAV + "<main><h1>Étude 12</h1><a href='/e12.pdf'>PDF</a></main>"
                "<dialog><h2>Sign in</h2><form><input type=password></form></dialog>")
        self.assertFalse(H.login_page(page("Étude 12", body)))

    def test_real_login_pages_still_detected(self):
        form = "<form><input name=u><input type=password name=p><a href='/forgot'>Mot de passe oublié</a></form>"
        cases = [
            page("Connexion | Site", self.NAV + f"<main>{form}<a href='/help'>Aide</a></main>" + self.FOOT),
            page("Site", self.NAV + f"<main><h1>Sign in</h1>{form}<a href='/help'>Help</a></main>" + self.FOOT),
            page("Site", self.NAV + f"<main><h2>Connexion</h2>{form}<a href='/help'>Aide</a></main>" + self.FOOT),
            page("Site", self.NAV + f"<main>{form}</main>" + self.FOOT),  # nothing but the form
            # A login wall rendered as a modal with no other content.
            page("Site", self.NAV + f"<div class='modal'><h2>Connexion</h2>{form}</div>" + self.FOOT),
            # Login heading in a modal and the page title also says login.
            page("Login", self.NAV + f"<div class='modal'><h2>Log in</h2>{form}</div><main><a href='/x'>x</a></main>"),
        ]
        for html in cases:
            self.assertTrue(H.login_page(html), html)

    def test_no_password_never_login(self):
        self.assertFalse(H.login_page(page("Connexion", "<h1>Connexion</h1>")))


class SitemapFixTest(unittest.TestCase):
    """MEDIUM-8: empty sitemaps are not completion evidence, broken XML is retried, gzip is bounded."""

    def setUp(self):
        from tests.enumeration_fakes import Env
        self.env = Env()

    def tearDown(self):
        self.env.close()

    @staticmethod
    def plan(**sm):
        cfg = {"sitemap_url": "https://sm.test/sitemap.xml"}
        cfg.update(sm)
        return {"strategy": "sitemap", "request_interval_s": 0, "base_url": "https://sm.test",
                "adapter": {"type": "selectors", "selectors": {"title": "h1"}}, "sitemap": cfg}

    @staticmethod
    def handler(sitemap_body, ctype="application/xml"):
        def handle(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            if url.endswith("/sitemap.xml"):
                body = sitemap_body() if callable(sitemap_body) else sitemap_body
                return Response(200, {"Content-Type": ctype}, body)
            if "/doc/" in url:
                return Response(200, {"Content-Type": "text/html"}, b"<html><h1>Doc</h1></html>")
            return Response(404, {}, b"")
        return handle

    def test_empty_urlset_is_unverified(self):
        from tests.test_enumeration_sort_sitemap import urlset
        result = self.env.run(self.plan(), self.handler(urlset([])))
        self.assertEqual(result["status"], "complete_unverified", result)
        seg = self.env.store.segments(self.env.site_id, self.env.store.site(self.env.site_id)["plan_fp"])[0]
        self.assertEqual((seg["status"], seg["evidence"]), ("unverified", "sitemap_empty_urlset"))

    def test_all_urls_filtered_out_is_unverified(self):
        from tests.test_enumeration_sort_sitemap import urlset
        result = self.env.run(self.plan(include=["/publications/"]), self.handler(urlset(["https://sm.test/a/"])))
        self.assertEqual((result["status"], result["stop_reason"]), ("complete_unverified", "sitemaps_listed_nothing"))

    def test_empty_index_is_unverified(self):
        from tests.test_enumeration_sort_sitemap import INDEX
        result = self.env.run(self.plan(), self.handler(INDEX.format("").encode()))
        self.assertEqual(result["status"], "complete_unverified", result)

    def test_maintenance_html_is_retried_next_run(self):
        from tests.test_enumeration_sort_sitemap import urlset
        maintenance = b"<html><body><h1>Maintenance</h1><p>Back soon & thanks</p></body></html>"
        answers = [maintenance, urlset(["https://sm.test/doc/1"])]
        handler = self.handler(lambda: answers[0])
        first = self.env.run(self.plan(), handler)
        self.assertEqual((first["status"], first["stop_reason"]), ("incomplete", "sitemaps_failed:1"))
        answers.pop(0)
        second = self.env.run(self.plan(), handler)
        self.assertEqual(second["status"], "complete", second)
        self.assertEqual(len(self.env.docs()), 1)

    def test_broken_sitemap_retries_are_bounded(self):
        broken = b"<html><body>Maintenance & more</body></html>"
        counts = []
        for _ in range(5):
            result = self.env.run(self.plan(), self.handler(broken))
            counts.append(sum("/sitemap.xml" in u for u in result["transport"].urls))
            self.assertEqual(result["status"], "incomplete")
        self.assertEqual(counts, [1, 1, 1, 0, 0])

    def test_gzip_expansion_is_capped(self):
        import gzip
        bomb = gzip.compress(b"<urlset>" + b" " * 200_000 + b"</urlset>")
        result = self.env.run(self.plan(max_uncompressed_bytes=10_000), self.handler(bomb, "application/x-gzip"))
        self.assertEqual(result["status"], "incomplete")
        seg = self.env.store.segments(self.env.site_id, self.env.store.site(self.env.site_id)["plan_fp"])[0]
        self.assertEqual((seg["status"], seg["evidence"]), ("failed", "sitemap_too_large"))


class DateRangeBoundaryTest(unittest.TestCase):
    """MEDIUM-9: a range filter whose end the site treats as exclusive loses boundary items while each
    segment's total still matches; the overall total check (default on) exposes it."""

    def setUp(self):
        from tests.enumeration_fakes import Env
        self.env = Env()

    def tearDown(self):
        self.env.close()

    @staticmethod
    def items():
        from datetime import date
        out, n = [], 0
        for year in (2018, 2019, 2020):
            for d in (date(year, 1, 1), date(year, 6, 1), date(year, 12, 31)):
                n += 1
                out.append({"id": n, "date": d})
        return out

    def exclusive_site(self):
        """FakeSearch whose 'to' parameter is exclusive (to=2018-12-31 drops 2018-12-31)."""
        from datetime import date, timedelta
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        from tests.test_enumeration_date_range import FakeSearch
        search = FakeSearch(self.items())

        def handler(url):
            parts = urlsplit(url)
            q = dict(parse_qsl(parts.query, keep_blank_values=True))
            if "to" in q:
                q["to"] = (date.fromisoformat(q["to"]) - timedelta(days=1)).isoformat()
            return search(urlunsplit(parts._replace(query=urlencode(q))))
        return handler

    def run_plan(self, handler, **over):
        from tests.test_enumeration_date_range import plan
        return self.env.run(plan(**over), handler)

    def test_exclusive_end_is_exposed_by_default_overall_check(self):
        result = self.run_plan(self.exclusive_site())
        self.assertEqual(result["status"], "complete_with_mismatch", result)
        self.assertIn("overall_total", result["stop_reason"])
        event = self.env.store.events(self.env.site_id, "overall_total_mismatch")[-1]["detail"]
        self.assertEqual(event, {"overall_total": 9, "listed": 6})

    def test_declared_exclusive_end_collects_boundary_items(self):
        result = self.run_plan(self.exclusive_site(), segments={"filter": {
            "type": "range", "from_param": "from", "to_param": "to", "to_inclusive": False}})
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(len(self.env.docs()), 9)

    def test_inclusive_site_still_complete(self):
        from tests.test_enumeration_date_range import FakeSearch
        result = self.run_plan(FakeSearch(self.items()))
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "all_segments_reconciled"))
        self.assertEqual(self.env.store.site(self.env.site_id)["state"]["overall_total"], 9)

    def test_disabled_overall_check_is_unverified(self):
        result = self.run_plan(self.exclusive_site(), overall_total_check=False)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_unverified", "overall_total_check_disabled"))

    def test_unavailable_overall_total_is_unverified(self):
        from tests.test_enumeration_date_range import FakeSearch
        search = FakeSearch(self.items())

        def handler(url):
            resp = search(url)
            if "from=" not in url and "/search" in url:  # the unfiltered listing shows no total
                resp = Response(resp.status, resp.headers, resp.body.replace(b"Total", b"Shown"))
            return resp
        result = self.run_plan(handler)
        self.assertEqual((result["status"], result["stop_reason"]),
                         ("complete_unverified", "overall_total_unavailable"))

    def test_parts_and_explicit_plans_unchanged(self):
        from tests.test_enumeration_date_range import FakeSearch
        result = self.run_plan(FakeSearch(self.items(), mode="parts"),
                               segments={"filter": {"type": "parts", "year_param": "year", "month_param": "month"}})
        self.assertEqual(result["status"], "complete", result)


class RequestTimingTest(unittest.TestCase):
    """MEDIUM-1: per-host request timing (gate wait, response time, Crawl-delay) reaches the state DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        for p in (mock.patch.object(request_gate, "time", self.clock), mock.patch.object(H, "time", self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetcher_summary_shows_crawl_delay_and_slow_requests(self):
        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nCrawl-delay: 10\n")
            self.clock.t += 1.0
            return ok()
        f = Fetcher("s", FakeTransport(handler), Path(self.tmp.name) / "gates", interval=1.5,
                    clock=self.clock.monotonic, sleep=self.clock.sleep)
        for i in range(3):
            f.get(f"https://slow.test/p{i}")
        summary = f.timing_summary()
        host = summary["hosts"]["slow.test"]
        self.assertEqual(host["requests"], 4)  # robots + 3 pages
        self.assertEqual(host["crawl_delay_s"], 10)
        self.assertEqual(host["effective_gap_s"], 10)
        self.assertGreaterEqual(host["gate_wait_max_s"], 9.9)
        self.assertAlmostEqual(host["response_avg_s"], 0.75, places=2)
        self.assertTrue(summary["slow_samples"])
        self.assertTrue(all(s["host"] == "slow.test" for s in summary["slow_samples"]))
        self.assertLessEqual(len(summary["slow_samples"]), H.SLOW_SAMPLES)
        self.assertEqual(f.timing_summary(reset=True)["hosts"]["slow.test"]["requests"], 4)
        self.assertEqual(f.timing_summary()["hosts"], {})

    def test_runner_records_one_timing_event_and_report_field(self):
        from crawler.enumeration.report import site_report
        from tests.enumeration_fakes import Env, FakeBoard, board_plan
        env = Env()
        self.addCleanup(env.close)
        result = env.run(board_plan(), FakeBoard(12))
        self.assertEqual(result["status"], "complete", result)
        events = env.store.events(env.site_id, "request_timing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["hosts"]["board.test"]["requests"], result["requests"])
        report = site_report(env.store, env.site_id)
        self.assertEqual(report["request_timing"]["hosts"]["board.test"]["requests"], result["requests"])
        self.assertEqual(report["request_count"], result["requests"])


class LowPriorityFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler, **kw):
        kw.setdefault("interval", 0)
        kw.setdefault("respect_robots", False)
        kw.setdefault("sleep", self.sleeps.append)
        t = FakeTransport(handler)
        return Fetcher("s", t, Path(self.tmp.name) / "gates", **kw), t

    def test_dns_failure_is_retried_with_backoff(self):
        answers = [[], ["93.184.216.34"]]
        f, t = self.fetcher(lambda u: ok(), resolver=lambda host: answers.pop(0))
        self.assertEqual(f.get("https://x.org/a").status, 200)
        self.assertEqual(len(self.sleeps), 1)
        f, t = self.fetcher(lambda u: ok(), resolver=lambda host: [], retries=3)
        with self.assertRaises(H.TransientFailure) as ctx:
            f.get("https://x.org/a")
        self.assertEqual(ctx.exception.reason, "dns_unresolved")
        self.assertEqual(t.urls, [])

    def test_503_retry_after_is_honoured_and_long_one_stops(self):
        answers = [Response(503, {"Retry-After": "30"}, b""), ok()]
        f, _ = self.fetcher(lambda u: answers.pop(0))
        self.assertEqual(f.get("https://x.org/a").status, 200)
        self.assertGreaterEqual(self.sleeps[0], 29)
        f, t = self.fetcher(lambda u: Response(503, {"Retry-After": "3600"}, b""), max_retry_wait_s=120)
        with self.assertRaises(H.RateLimited) as ctx:
            f.get("https://x.org/a")
        self.assertEqual((ctx.exception.reason, ctx.exception.status, len(t.urls)), ("unavailable_retry_after", 503, 1))
        # The cooldown is host-wide (gate state), like a 429 cooldown.
        f2, t2 = self.fetcher(lambda u: ok())
        with self.assertRaises(H.RateLimited):
            f2.get("https://x.org/b")
        self.assertEqual(t2.urls, [])

    def test_body_too_large_is_not_retried(self):
        f, t = self.fetcher(lambda u: H.BodyTooLarge("body_too_large"), retries=3)
        with self.assertRaises(H.PermanentFailure) as ctx:
            f.get("https://x.org/big.pdf")
        self.assertEqual(ctx.exception.reason, "body_too_large")
        self.assertEqual(len(t.urls), 1)

    def test_transport_refuses_declared_oversize_body_before_reading(self):
        class Raw:
            status_code, url = 200, "https://x.org/big"
            headers = {"Content-Length": str(10 ** 9)}
            read = []

            def iter_content(self, n):
                Raw.read.append(n)
                yield b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        transport = H.RequestsTransport(max_bytes=1000, resolver=lambda h: ["93.184.216.34"])
        with mock.patch.object(transport.session, "request", return_value=Raw()):
            with self.assertRaises(H.BodyTooLarge):
                transport.request("GET", "https://x.org/big")
        self.assertEqual(Raw.read, [])


if __name__ == "__main__":
    unittest.main()
