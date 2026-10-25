"""S-open: agri-ee-ministeerium-uudised enumeration split (crawl() unchanged), memory only.
Identity keeps crawl()'s rule: external_id = post_number = file slug (or datatable-row id),
url = START_URL#<section anchor>. The per-item HEAD is not made (headers None/empty
give the same paper as an empty HEAD answer)."""
import json
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.split_open_helpers import adapter_for, identities, listing_items, native, pipeline

SITE = "agri-ee-ministeerium-uudised"
START = "https://www.agri.ee/ministeerium-uudised-kontakt/uuringud"
ADAPTER = {"type": "crawler", "contract_version": 1, "list_method": "_enumeration_items", "list_args": ["html"],
           "paper_method": "_enumeration_paper", "paper_args": [{"source": "item", "path": ["record"]}]}
PARSER = {"type": "adapter", "key_field": "external_id", "url_field": "url", "date_field": "listed_date",
          "title_field": "title"}

ROWS = [
    ['<div class="type-file"><a href="/sites/default/files/documents/2026-08/Uuring%20A.pdf">Uuring A</a> | 1 MB | pdf</div>',
     '<time datetime="2026-08-25T12:00:00Z">25.08.2026</time>', ''],
    ['<a href="https://www.example.org/uuring-b">Uuring B</a>', '<span>06.04.2026</span>', ''],
    ['<a href="#"></a>', '<span>01.01.2026</span>', ''],
]


def page(rows):
    return ("<html><body><div role='tabpanel' id='kalandus'><div id='accordion-title--1'>Kalandus</div>"
            "<table data-json-id='datatable-1'></table></div>"
            "<script type='application/json' id='datatable-1'>" + json.dumps(rows) + "</script>"
            "<script type='application/json' id='datatable-1-options'>{\"paging\": false}</script>"
            "</body></html>")


class AgriEeSplit(unittest.TestCase):
    def setUp(self):
        self.adapter, self.crawler, _ = adapter_for(SITE, ADAPTER)
        self.page = listing_items(self.adapter, PARSER, page(ROWS), START + "?page=0")

    def test_items(self):
        self.assertEqual(["Uuring A", "uuring-b"], [i.key for i in self.page.items])

    def test_papers_match_native_crawl(self):
        expected = native(SITE, lambda url, **k: page(ROWS), fetch_attr="_curl_text",
                          extra={"detail_delay": 0, "_curl_headers": lambda url, **k: {}})
        got = pipeline(self.adapter, self.page.items, {})
        self.assertEqual(2, len(expected))
        self.assertEqual(identities(expected), identities(got))
        self.assertEqual([("Uuring A", START + "#kalandus"), ("uuring-b", START + "#kalandus")], identities(got))
        for e, g in zip(expected, got):
            for f in ("title", "post_number", "published_date", "abstract", "pdf_url", "original_filename"):
                self.assertEqual(e.get(f), g.get(f), f)
        self.assertEqual("2026-08-25", got[0]["published_date"])
        self.assertEqual("2026-04-06", got[1]["published_date"])

    def test_foreign_page(self):
        with self.assertRaises(PermanentFailure):
            self.crawler._enumeration_items("<html><body>Just a moment</body></html>")


if __name__ == "__main__":
    unittest.main()
