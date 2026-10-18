"""A page in place of robots.txt (challenge, home page after a redirect) is never read as
"no rules" (collect-sites report 2026-10-19, ens.hal.science Anubis page). No network."""
import unittest
from urllib.parse import urlsplit

from crawler.enumeration.http import Response
from tests.enumeration_fakes import Env, FakeBoard, board_plan
from tests.test_enumeration_extensions import RecordingTransport

ANUBIS = (b'<!doctype html><html lang="en"><head><title>Making sure you&#39;re not a bot!</title>'
          b'<script src="/.within.website/x/cmd/anubis/static/js/main.mjs"></script></head>'
          b'<body><p>Loading...</p></body></html>')
HOME = b"<!DOCTYPE html>\n<html><head><title>Home</title></head><body><a href='/list'>news</a></body></html>"


class RobotsHtmlTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def run_with(self, robots):
        board = FakeBoard(5)

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == "/robots.txt":
                return robots
            if path == "/":
                return Response(200, {"Content-Type": "text/html"}, HOME)
            return board(url)
        transport = RecordingTransport(handle)
        return self.env.run(board_plan(), None, transport=transport), transport

    def assert_stopped_before_listing(self, robots):
        result, transport = self.run_with(robots)
        self.assertNotIn(result["status"], ("complete", "complete_unverified"), result)
        self.assertFalse([u for u in transport.urls if urlsplit(u).path != "/robots.txt" and urlsplit(u).path != "/"],
                         transport.urls)
        self.assertEqual([], self.env.docs())
        return result

    def test_challenge_page_stops(self):
        self.assert_stopped_before_listing(Response(200, {"Content-Type": "text/html; charset=utf-8"}, ANUBIS))

    def test_challenge_page_with_text_plain_type_stops(self):
        self.assert_stopped_before_listing(Response(200, {"Content-Type": "text/plain"}, ANUBIS))

    def test_redirect_to_home_page_stops(self):
        self.assert_stopped_before_listing(Response(301, {"Location": "https://board.test/"}, b""))

    def test_plain_rules_served_as_html_still_apply(self):
        robots = Response(200, {"Content-Type": "text/html"}, b"User-agent: *\nDisallow: /view\n")
        result, transport = self.run_with(robots)
        self.assertFalse([u for u in transport.urls if "/view" in u])
        self.assertTrue([u for u in transport.urls if "/list" in u])

    def test_missing_robots_is_still_no_rules(self):
        result, _ = self.run_with(Response(404, {"Content-Type": "text/html"}, HOME))
        self.assertEqual("complete", result["status"], result)


if __name__ == "__main__":
    unittest.main()
