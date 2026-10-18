"""publichealthscotland-scot-publications: the real past-the-end listing page (200, full page
chrome, empty div.search-results) is an explicit empty listing, not a parse error.
Synthetic HTML only; no network."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load

SITE = "publichealthscotland-scot-publications"
CHROME = ("<html><body><main><h1>Publications</h1><p>We release a wide range of research.</p>"
          "<div class=\"search-box\">Search publications</div>{results}"
          "<div class=\"paging\">Page 407 of 408 Page 408 of 408</div></main></body></html>")
ROW = ("<li class=\"search-item\"><a href=\"/publications/series/release-1/\"><h3>Release 1</h3></a>"
       "<ul class=\"list-inline text-muted\"><li>12 June 2025</li><li>Research</li></ul>"
       "<div class=\"search-item-description\"><p>Desc</p></div></li>")


class PastLastPage(unittest.TestCase):
    def setUp(self):
        self.crawler, _module = load(SITE)

    def test_empty_search_results_container_is_explicit_empty(self):
        html = CHROME.format(results="<div class=\"search-results\"></div>")
        self.assertEqual(self.crawler._enumeration_items(html), [])

    def test_rows_inside_search_results_are_listed(self):
        html = CHROME.format(results=f"<div class=\"search-results\"><ul>{ROW}</ul></div>")
        rows = self.crawler._enumeration_items(html)
        self.assertEqual([r["url"] for r in rows],
                         ["https://www.publichealthscotland.scot/publications/series/release-1/"])

    def test_text_without_results_container_still_fails(self):
        html = CHROME.format(results="<div class=\"maintenance\">Down for maintenance</div>")
        with self.assertRaises(PermanentFailure):
            self.crawler._enumeration_items(html)


if __name__ == "__main__":
    unittest.main()
