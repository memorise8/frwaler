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


if __name__ == "__main__":
    unittest.main()
