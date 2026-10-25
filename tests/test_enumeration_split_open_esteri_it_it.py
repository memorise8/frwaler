"""S-open: esteri-it-it enumeration split (crawl() unchanged), memory only.
Identity keeps crawl()'s rule: external_id = post_number = str(WP post id), url = post link;
abstract = longer of content/excerpt text, < 50 chars skipped."""
import json
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.split_open_helpers import adapter_for, identities, listing_items, native, pipeline

SITE = "esteri-it-it"
LIST_URL = "https://www.esteri.it/it/wp-json/wp/v2/posts?categories=1&per_page=100&page=1"
ADAPTER = {"type": "crawler", "contract_version": 1, "list_method": "_enumeration_items", "list_args": ["json"],
           "paper_method": "_enumeration_paper", "paper_args": [{"source": "item", "path": ["record"]}]}
PARSER = {"type": "adapter", "url_field": "url", "key_field": "id", "date_field": "date"}

LONG = "<p>Il Ministro ha incontrato oggi una delegazione per discutere di cooperazione internazionale.</p>"
POSTS = [
    {"id": 101, "date": "2026-10-24T10:00:00", "link": "https://www.esteri.it/it/sala_stampa/archivionotizie/comunicati/2026/10/uno/",
     "title": {"rendered": "Comunicato &amp;#8211; uno"}, "content": {"rendered": LONG}, "excerpt": {"rendered": ""}},
    {"id": 102, "date": "2026-10-20T09:00:00", "link": "https://www.esteri.it/it/sala_stampa/archivionotizie/comunicati/2026/10/due/",
     "title": {"rendered": "Comunicato due"}, "content": {"rendered": "<p>breve</p>"}, "excerpt": {"rendered": "<p>breve</p>"}},
    {"id": 103, "date": "2026-10-18T09:00:00", "link": "https://www.esteri.it/it/sala_stampa/archivionotizie/comunicati/2026/10/tre/",
     "title": {"rendered": "Comunicato tre"}, "content": {"rendered": ""}, "excerpt": {"rendered": LONG}},
]


class EsteriSplit(unittest.TestCase):
    def setUp(self):
        self.adapter, self.crawler, _ = adapter_for(SITE, ADAPTER)
        self.page = listing_items(self.adapter, PARSER, json.dumps(POSTS), LIST_URL, "application/json")

    def test_items(self):
        self.assertEqual(["101", "102", "103"], [i.key for i in self.page.items])
        self.assertEqual(POSTS[0]["link"], self.page.items[0].url)

    def test_papers_match_native_crawl(self):
        pages = {1: POSTS}
        expected = native(SITE, lambda page: pages.get(page, []), fetch_attr="_fetch_api_page")
        got = pipeline(self.adapter, self.page.items, {})
        self.assertEqual(2, len(expected))  # id 102: short abstract skipped by both
        self.assertEqual(identities(expected), identities(got))
        self.assertEqual(["101", "103"], [i[0] for i in identities(got)])
        for e, g in zip(expected, got):
            for f in ("title", "post_number", "published_date", "abstract", "url"):
                self.assertEqual(e.get(f), g.get(f), f)
        self.assertEqual("Comunicato – uno", got[0]["title"])

    def test_wrong_shape(self):
        with self.assertRaises(PermanentFailure):
            self.crawler._enumeration_items({"code": "rest_post_invalid_page_number"})


if __name__ == "__main__":
    unittest.main()
