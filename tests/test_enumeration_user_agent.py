"""Every outgoing request says it is our collector (user decision 2026-10-19). No network
except a loopback server for the wire check."""
import http.server
import tempfile
import threading
import unittest
from unittest.mock import patch

from crawler.base_crawler import BaseCrawler
from crawler.enumeration.guard import AdapterUngatedNetwork, guarded
from crawler.enumeration.http import DEFAULT_USER_AGENT, RequestsTransport, Response
from crawler.enumeration.plan import PlanError
from crawler.enumeration.run import make_fetcher
from tests.enumeration_fakes import Env, FakeBoard
from tests.test_enumeration_adapter_runner import crawler, good, site

ROBOTS_OK = Response(200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")


class Recording:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((url, dict(headers or {})))
        return self.handler(url)


def agents(transport):
    return [[v for k, v in h.items() if k.lower() == "user-agent"] for _, h in transport.calls]


class UserAgentTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_collector_user_agent(self):
        self.assertTrue(DEFAULT_USER_AGENT.startswith("LibertreeCollector/"))
        self.assertEqual(BaseCrawler.USER_AGENT, DEFAULT_USER_AGENT)

    def test_adapter_site_requests_carry_only_our_user_agent(self):
        board = FakeBoard(25, page_size=10)
        transport = Recording(board)
        result = self.env.run(site(), None, transport=transport, crawler=crawler(self.env, good))
        self.assertEqual("complete", result["status"], result.get("stop_reason"))
        self.assertGreater(len(transport.calls), 3)
        self.assertEqual([[DEFAULT_USER_AGENT]] * len(transport.calls), agents(transport))

    def test_plan_cannot_set_user_agent(self):
        with self.assertRaises(PlanError):
            self.env.plan(dict(site(), user_agent="Mozilla/5.0 (Windows NT 10.0)"))
        with self.assertRaises(PlanError):
            self.env.plan(dict(site(), request_headers={"user-agent": "Mozilla/5.0"}))
        self.env.plan(dict(site(), user_agent=DEFAULT_USER_AGENT))  # the default itself is fine

    def test_fetcher_overrides_user_agent_in_extra_and_request_headers(self):
        plan = self.env.plan(site())
        transport = Recording(lambda url: ROBOTS_OK if url.endswith("/robots.txt") else Response(200, {}, "ok"))
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = make_fetcher(plan, tmp, transport)
            fetcher.extra_headers, fetcher.header_hosts = {"USER-AGENT": "Mozilla/5.0"}, {"board.test"}
            fetcher.sleep = lambda s: None
            fetcher.get("https://board.test/a")
            fetcher.post("https://board.test/b", {"q": "1"}, headers={"User-agent": "Chrome/131"})
        self.assertEqual([[DEFAULT_USER_AGENT]] * len(transport.calls), agents(transport))

    def test_redirect_hops_and_robots_carry_our_user_agent(self):
        def handle(url):
            if url.endswith("/robots.txt"):
                return ROBOTS_OK
            if url.endswith("/old"):
                return Response(302, {"Location": "https://board.test/new"}, "")
            return Response(200, {}, "ok")
        plan = self.env.plan(site())
        transport = Recording(handle)
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = make_fetcher(plan, tmp, transport)
            fetcher.sleep = lambda s: None
            fetcher.get("https://board.test/old")
        urls = [u for u, _ in transport.calls]
        self.assertEqual(["https://board.test/robots.txt", "https://board.test/old", "https://board.test/new"], urls)
        self.assertEqual([[DEFAULT_USER_AGENT]] * 3, agents(transport))

    def test_stealth_fetch_stops_inside_adapter(self):
        from crawler.stealth_fetcher import StealthSession

        def detail(html, item):
            StealthSession().fetch_html(item["url"])
        transport = Recording(FakeBoard(5, page_size=10))
        result = self.env.run(site(), None, transport=transport, crawler=crawler(self.env, detail))
        self.assertNotEqual("complete", result["status"])
        self.assertIn("adapter_ungated_network:stealth_fetcher", result["stop_reason"])
        self.assertTrue(all(url.startswith("https://board.test/") for url, _ in transport.calls))

    def test_curl_cffi_stops_in_guard(self):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            self.skipTest("curl_cffi not installed")
        with guarded():
            with self.assertRaises(AdapterUngatedNetwork):
                curl_requests.get("https://board.test/x")
            with self.assertRaises(AdapterUngatedNetwork):
                curl_requests.Session().request("GET", "https://board.test/x")
            from curl_cffi import Curl
            with self.assertRaises(AdapterUngatedNetwork):
                Curl().perform()


class WireTest(unittest.TestCase):
    """RequestsTransport puts exactly the Fetcher's User-Agent on the wire (loopback server)."""

    def test_wire_user_agent(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get_all("User-Agent"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # The destination check refuses loopback; it is lifted for this test's server only.
        with patch("crawler.enumeration.http.forbidden_address", return_value=False):
            transport = RequestsTransport(resolver=lambda host: ["127.0.0.1"])
            transport.request("GET", f"http://127.0.0.1:{server.server_port}/",
                              headers={"User-Agent": DEFAULT_USER_AGENT})
        self.assertEqual([[DEFAULT_USER_AGENT]], seen)


if __name__ == "__main__":
    unittest.main()
