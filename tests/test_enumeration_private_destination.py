"""Destination address check: no request to private/loopback/link-local/reserved
addresses, for every request and every redirect hop (fake resolver, no network)."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import (Fetcher, PrivateDestination, RequestsTransport, Response,  # noqa: E402
                                      TransientFailure, forbidden_address)
from crawler.enumeration.plan import PlanError, site_plan  # noqa: E402
from crawler.enumeration.run import exit_code  # noqa: E402
from tests.enumeration_fakes import Env, FakeBoard, FakeTransport, board_plan  # noqa: E402

PUBLIC = "93.184.216.34"
DNS = {"public.test": [PUBLIC], "board.test": [PUBLIC], "intranet.test": ["10.1.2.3"],
       "mixed.test": [PUBLIC, "192.168.0.10"], "meta.test": ["169.254.169.254"], "v6.test": ["::1"]}


def resolver(host):
    if host not in DNS:
        raise socket.gaierror("no such host")
    return list(DNS[host])


def page(body=b"<html><body><table><tr><td>x</td></tr></table></body></html>", status=200, **headers):
    return Response(status, {"Content-Type": "text/html", **headers}, body)


class AddressRuleTest(unittest.TestCase):
    def test_forbidden_ranges(self):
        for ip in ("10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.1", "127.0.0.1", "127.8.8.8", "::1",
                   "169.254.1.1", "169.254.169.254", "fe80::1", "fc00::1", "fd12:3456::1", "0.0.0.0", "::",
                   "224.0.0.1", "239.255.255.250", "ff02::1", "240.0.0.1", "255.255.255.255", "100.64.0.1",
                   "::ffff:10.0.0.1", "::ffff:127.0.0.1", "192.0.2.10", "198.18.0.1"):
            self.assertTrue(forbidden_address(ip), ip)
        for ip in (PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "172.32.0.1"):
            self.assertFalse(forbidden_address(ip), ip)


class FetcherDestinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, handler):
        t = FakeTransport(handler)
        f = Fetcher("s", t, Path(self.tmp.name) / "g", interval=0, retries=1, backoff_s=0, sleep=lambda s: None,
                    respect_robots=False, resolver=resolver)
        return f, t

    def test_private_host_is_never_requested(self):
        f, t = self.fetcher(lambda url: page())
        with self.assertRaises(PrivateDestination) as cm:
            f.get("https://intranet.test/list")
        self.assertEqual(t.urls, [])
        self.assertEqual((cm.exception.host, cm.exception.address), ("intranet.test", "10.1.2.3"))
        self.assertTrue(cm.exception.reason.startswith("private_destination_blocked"))

    def test_any_private_address_among_several_blocks(self):
        f, t = self.fetcher(lambda url: page())
        with self.assertRaises(PrivateDestination):
            f.get("https://mixed.test/")
        with self.assertRaises(PrivateDestination):
            f.get("http://meta.test/latest/meta-data/")
        self.assertEqual(t.urls, [])

    def test_public_redirect_to_private_host_stops_before_the_hop(self):
        def handler(url):
            if url.startswith("https://public.test/"):
                return page(status=302, Location="http://intranet.test/admin")
            return page()
        f, t = self.fetcher(handler)
        with self.assertRaises(PrivateDestination):
            f.get("https://public.test/list")
        self.assertEqual(t.urls, ["https://public.test/list"])

    def test_redirect_to_ip_literal_is_blocked(self):
        f, t = self.fetcher(lambda url: page(status=301, Location="http://169.254.169.254/latest/")
                            if "public.test" in url else page())
        with self.assertRaises(PrivateDestination):
            f.get("https://public.test/")
        self.assertEqual(len(t.urls), 1)

    def test_ip_literal_urls_are_checked_without_dns(self):
        f, t = self.fetcher(lambda url: page())
        for url in ("http://127.0.0.1:8080/x", "http://10.0.0.7/", "http://[::1]/x", "http://[fe80::1]/",
                    "http://0.0.0.0/"):
            with self.assertRaises(PrivateDestination, msg=url):
                f.get(url)
        self.assertEqual(t.urls, [])

    def test_ipv6_loopback_by_name(self):
        f, t = self.fetcher(lambda url: page())
        with self.assertRaises(PrivateDestination):
            f.get("https://v6.test/")
        self.assertEqual(t.urls, [])

    def test_public_host_passes(self):
        f, t = self.fetcher(lambda url: page())
        self.assertEqual(f.get("https://public.test/list").status, 200)
        self.assertEqual(f.get(f"http://{PUBLIC}/x").status, 200)
        self.assertEqual(len(t.urls), 2)

    def test_resolution_failure_is_transient_and_sends_nothing(self):
        f, t = self.fetcher(lambda url: page())
        with self.assertRaises(TransientFailure) as cm:
            f.get("https://unknown.test/")
        self.assertEqual(cm.exception.reason, "dns_unresolved")
        self.assertEqual(t.urls, [])

    def test_robots_request_is_checked_too(self):
        t = FakeTransport(lambda url: page())
        f = Fetcher("s", t, Path(self.tmp.name) / "g2", interval=0, retries=1, backoff_s=0,
                    sleep=lambda s: None, respect_robots=True, resolver=resolver)
        with self.assertRaises(PrivateDestination):
            f.get("https://intranet.test/list")
        self.assertEqual(t.urls, [])


class RunnerDestinationTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_site_stops_with_its_own_status_and_no_host_hold(self):
        DNS["board.test"] = ["192.168.10.10"]
        try:
            t = FakeTransport(FakeBoard(6))
            result = self.env.run(board_plan(), None, transport=t, resolver=resolver)
        finally:
            DNS["board.test"] = [PUBLIC]
        self.assertEqual(result["status"], "private_destination_blocked", result)
        self.assertIn("board.test", result["stop_reason"])
        self.assertEqual(t.urls, [])
        self.assertIsNone(self.env.store.host_hold("board.test"))
        events = self.env.store.events(self.env.site_id, "private_destination_blocked")
        self.assertEqual(events[0]["detail"]["host"], "board.test")
        self.assertEqual(events[0]["detail"]["address"], "192.168.10.10")
        self.assertEqual(exit_code([result]), 1)

    def test_public_board_still_completes(self):
        result = self.env.run(board_plan(), FakeBoard(6), resolver=resolver)
        self.assertEqual(result["status"], "complete", result)


class PlanCannotRelaxTest(unittest.TestCase):
    def test_plan_keys_that_would_relax_the_check_are_refused(self):
        for key in ("allow_private_destinations", "private_destination_check", "destination_check", "resolver",
                    "allow_private_addresses"):
            for where in ("site", "defaults"):
                plan = {"sites": {"s": board_plan()}}
                if where == "site":
                    plan["sites"]["s"][key] = True
                else:
                    plan["defaults"] = {key: True}
                with self.assertRaises(PlanError, msg=(key, where)):
                    site_plan(plan, "s", min_interval=0)


class RequestsTransportPinningTest(unittest.TestCase):
    """The production transport connects to the address it checked (no second lookup)."""

    def test_private_resolution_never_opens_a_socket(self):
        t = RequestsTransport(resolver=lambda host: ["10.0.0.9"])
        with mock.patch("urllib3.util.connection.create_connection") as conn:
            with self.assertRaises(PrivateDestination):
                t.request("GET", "http://intranet.test/x")
        conn.assert_not_called()

    def test_connection_goes_to_the_checked_address_with_original_host(self):
        t = RequestsTransport(resolver=lambda host: [PUBLIC])
        seen = []

        def fake_connect(address, *a, **k):
            seen.append(address)
            raise ConnectionRefusedError("offline test")
        with mock.patch("urllib3.connection.connection.create_connection", side_effect=fake_connect):
            with self.assertRaises(Exception):
                t.request("GET", "http://public.test:8080/x", headers={"User-Agent": "t"})
        self.assertTrue(seen)
        self.assertEqual(seen[0], (PUBLIC, 8080))


if __name__ == "__main__":
    unittest.main()
