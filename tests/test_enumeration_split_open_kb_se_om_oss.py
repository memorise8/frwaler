"""S-open: kb-se-om-oss enumeration split (crawl() unchanged), memory only.
Identity keeps crawl()'s rule: external_id = item nodeId, url = base_url + uri; the
list desc is the abstract fallback and < 50 chars after that is skipped."""
import json
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.split_open_helpers import adapter_for, identities, listing_items, native, pipeline

SITE = "kb-se-om-oss"
LIST_URL = "https://www.kb.se/om-oss/nyheter.html?page=1"
ADAPTER = {"type": "crawler", "contract_version": 1, "list_method": "_enumeration_items", "list_args": ["html"],
           "detail_method": "_enumeration_detail", "detail_args": ["html", "item"],
           "paper_method": "_enumeration_paper", "paper_args": ["item", "detail"]}
PARSER = {"type": "adapter", "key_field": "external_id", "url_field": "url", "date_field": "date_iso",
          "title_field": "title", "listing_marker": "body"}

LONG = "Kungliga biblioteket meddelar en testnyhet med tillräckligt lång brödtext för att passera gränsen. " * 2


def list_page(items):
    state = {"config": {"num": 8}, "params": {"page": 1}, "items": items}
    return ("<html><body><div id='news'></div><script>AppRegistry.registerInitialState('12.x', "
            + json.dumps(state, separators=(",", ":")) + ");</script></body></html>")


ITEMS = [
    {"title": "Nyhet ett", "desc": "kort", "date": {"iso": "2026-10-21", "millis": 1792540800000},
     "uri": "/om-oss/nyheter/nyhetsarkiv/2026-10-21-ett.html", "nodeId": "5.aaa1", "keywords": ["a", "b"]},
    {"title": "Nyhet två", "desc": LONG, "date": {"iso": "2026-10-13", "millis": 1791849600000},
     "uri": "/om-oss/nyheter/nyhetsarkiv/2026-10-13-tva.html", "nodeId": "5.bbb2"},
    {"title": "Nyhet tre", "desc": "kort", "date": {"iso": "2026-10-01"},
     "uri": "/om-oss/nyheter/nyhetsarkiv/2026-10-01-tre.html", "nodeId": "5.ccc3"},
    {"title": "Utan uri", "desc": LONG, "date": {"iso": "2026-09-01"}, "uri": "", "nodeId": "5.ddd4"},
]
DETAILS = {
    "https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-21-ett.html":
        "<html><body><time class='kb-article-date' datetime='2026-10-21'>21 okt</time>"
        "<div class='sv-text-portlet-content'><p>" + LONG + "</p></div>"
        "<a href='/download/rapport.pdf'>pdf</a></body></html>",
    "https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-13-tva.html":
        "<html><body><div class='sv-text-portlet-content'>kort</div></body></html>",
    "https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-01-tre.html":
        "<html><body><div class='sv-text-portlet-content'>kort</div></body></html>",
}
EMPTY = list_page([])


class KbSeSplit(unittest.TestCase):
    def setUp(self):
        self.adapter, self.crawler, _ = adapter_for(SITE, ADAPTER)
        self.page = listing_items(self.adapter, PARSER, list_page(ITEMS), LIST_URL)

    def test_items_keep_crawl_identity(self):
        self.assertEqual(["5.aaa1", "5.bbb2", "5.ccc3"], [i.key for i in self.page.items])
        self.assertEqual("https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-21-ett.html", self.page.items[0].url)
        self.assertEqual("Nyhet ett", self.page.items[0].title)

    def test_papers_match_native_crawl(self):
        def transport(url):
            if url.startswith("https://www.kb.se/om-oss/nyheter.html?page="):
                return list_page(ITEMS) if url.endswith("=1") else EMPTY
            return DETAILS[url]
        expected = native(SITE, transport)
        got = pipeline(self.adapter, self.page.items, DETAILS)
        self.assertEqual(2, len(expected))  # third item: abstract < 50 -> skipped by both
        self.assertEqual(identities(expected), identities(got))
        self.assertEqual([("5.aaa1", "https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-21-ett.html"),
                          ("5.bbb2", "https://www.kb.se/om-oss/nyheter/nyhetsarkiv/2026-10-13-tva.html")],
                         identities(got))
        self.assertEqual("https://www.kb.se/download/rapport.pdf", got[0]["pdf_url"])
        for e, g in zip(expected, got):
            for f in ("title", "post_number", "published_date", "abstract", "pdf_url", "keywords"):
                self.assertEqual(e.get(f), g.get(f), f)

    def test_empty_and_foreign_pages(self):
        page = listing_items(self.adapter, PARSER, EMPTY, LIST_URL)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get("explicit_empty"))
        with self.assertRaises(PermanentFailure):
            self.crawler._enumeration_items("<html><body>error</body></html>")


if __name__ == "__main__":
    unittest.main()
