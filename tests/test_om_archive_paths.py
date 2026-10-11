"""Older source-published /nl/article/ records must survive list discovery."""
import importlib.util
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/om_archive_paths"


class OMArchivePaths(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "om_paths", ROOT / "crawler/sites/custom/om-mp-be-nl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.native = object.__new__(module.OmMpBeNlCrawler)

    def test_actual_first10_and_oldest2_keep_published_paths_titles_and_dates(self):
        for filename, count in [("first10.html", 10), ("last2.html", 2)]:
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_text()
                articles = BeautifulSoup(raw, "lxml").select("article")
                expected = [(next(a["href"] for a in article.select("a[href]")
                                  if a.get_text(" ", strip=True).startswith("Lees meer")),
                             article.find("h2").get_text(" ", strip=True))
                            for article in articles]
                result = self.native._parse_listing_page(raw)
                self.assertEqual(len(result), count)
                self.assertEqual([(x["detail_path"], x["title"]) for x in result], expected)
                self.assertTrue(all(x["listed_date"] for x in result))
        self.assertEqual([x["detail_path"] for x in self.native._parse_listing_page(
            (FIXTURES / "last2.html").read_text())],
            ["/nl/article/persbericht-211", "/nl/article/persbericht-212"])

    def test_navigation_link_is_not_mistaken_for_an_article(self):
        html = '<article><h2>Navigation</h2><a href="/nl/onze-publicaties">Publications</a></article>'
        self.assertEqual(self.native._parse_listing_page(html), [])


if __name__ == "__main__":
    unittest.main()
