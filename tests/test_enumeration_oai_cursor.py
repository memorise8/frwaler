"""ext:next_link text + template: OAI-PMH resumptionToken paging (research-thea-ie-browse style). No network."""
import re
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.base_crawler import BaseCrawler
from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError
from tests.enumeration_fakes import Env, FakeBoard
from tests.test_enumeration_extensions import RecordingTransport

OAI = "https://board.test/oai/request"
TEMPLATE = OAI + "?verb=ListRecords&resumptionToken={token}"


def oai_page(ids, token):
    records = "".join(f"<record><header><identifier>oai:board.test:{i}</identifier></header>"
                      f"<metadata><title>Title {i}</title></metadata></record>" for i in ids)
    rt = f"<resumptionToken completeListSize=\"7\">{token}</resumptionToken>" if token else \
        "<resumptionToken completeListSize=\"7\"/>"
    body = (f'<?xml version="1.0" encoding="UTF-8"?><OAI-PMH><ListRecords>{records}{rt}</ListRecords></OAI-PMH>')
    return Response(200, {"Content-Type": "text/xml;charset=UTF-8"}, body)


class OaiBoard:
    """7 records, 3 per page. Tokens carry '/' and ':' (sent percent-encoded)."""

    def __init__(self):
        self.board = FakeBoard(7)

    def __call__(self, method, url, data):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        if parts.path != "/oai/request":
            return self.board(url)
        if q.get("verb") != "ListRecords":
            return Response(400, {}, b"badVerb")
        if "resumptionToken" in q:
            if "metadataPrefix" in q:
                return Response(400, {}, b"badArgument")  # OAI: the token replaces the other arguments
            page = {"oai_dc:/2": 2, "oai_dc:/3": 3}.get(q["resumptionToken"])
            if page is None:
                return Response(400, {}, b"badResumptionToken")
        else:
            if q.get("metadataPrefix") != "oai_dc":
                return Response(400, {}, b"badArgument")
            page = 1
        ids = list(range(1, 8))[(page - 1) * 3:page * 3]
        return oai_page(ids, f"oai_dc:/{page + 1}" if page < 3 else None)


def crawler(env):
    class Oai(BaseCrawler):
        site_id = "fake-site"
        site_name = "fake-site"
        base_url = "https://board.test"

        def crawl(self, limit=None):
            pass

        def listing(self, html):
            ids = re.findall(r"<identifier>oai:board\.test:(\d+)</identifier>", html)
            return [{"url": f"https://board.test/view?id={i}", "external_id": i, "title": f"Title {i}"} for i in ids]

        def detail(self, html, item):
            return {"external_id": item["external_id"], "url": item["url"], "title": item["title"]}
    return Oai(env.conn, 0)


def site(**link):
    return {"strategy": "oldest_first_sort", "request_interval_s": 0, "base_url": "https://board.test",
            "listing": {"url": OAI, "params": {"verb": "ListRecords", "metadataPrefix": "oai_dc"},
                        "parser": {"type": "adapter", "listing_marker": "listrecords"},
                        "next_link": {"selector": "resumptionToken", "text": True, "template": TEMPLATE, **link}},
            "adapter": {"type": "crawler", "contract_version": 1, "list_method": "listing", "detail_method": "detail"}}


class OaiCursorTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_resumption_token_pages_to_the_end(self):
        transport = RecordingTransport(OaiBoard())
        result = self.env.run(site(), None, transport=transport, crawler=crawler(self.env))
        self.assertEqual("complete", result["status"], result)
        self.assertEqual(list(range(1, 8)), sorted(int(d["post_number"]) for d in self.env.docs()))
        lists = [u for u in transport.urls if "/oai/" in u]
        self.assertEqual([OAI + "?verb=ListRecords&metadataPrefix=oai_dc",
                          OAI + "?verb=ListRecords&resumptionToken=oai_dc%3A%2F2",
                          OAI + "?verb=ListRecords&resumptionToken=oai_dc%3A%2F3"], lists)

    def test_error_page_after_first_page_is_not_an_end(self):
        board = OaiBoard()

        def handle(method, url, data):
            if "resumptionToken=oai_dc%3A%2F3" in url:  # expired token: OAI answers 200 with an error
                return Response(200, {"Content-Type": "text/xml"},
                                '<?xml version="1.0"?><OAI-PMH><error code="badResumptionToken"/></OAI-PMH>')
            return board(method, url, data)
        result = self.env.run(site(), None, transport=RecordingTransport(handle), crawler=crawler(self.env))
        self.assertNotIn(result["status"], ("complete", "complete_unverified"), result)

    def test_validation(self):
        bad = [{"template": "https://other.test/oai?resumptionToken={token}"},
               {"template": OAI + "?resumptionToken="},
               {"template": OAI + "?a={token}&b={token}"},
               {"template": "/oai/request?resumptionToken={token}"},
               {"template": "http://board.test/oai/request?resumptionToken={token}"},  # https -> http
               {"template": "https://board.test:8443/oai/request?resumptionToken={token}"},
               {"template": "https://u:p@board.test/oai/request?resumptionToken={token}"},
               {"text": "yes"}]
        for over in bad:
            with self.assertRaises(PlanError, msg=over):
                self.env.plan(site(**over))
        with self.assertRaises(PlanError):  # text needs the template
            self.env.plan(dict(site(), listing=dict(site()["listing"], next_link={"selector": "x", "text": True})))
        self.env.plan(site())


if __name__ == "__main__":
    unittest.main()
