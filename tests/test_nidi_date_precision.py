"""Year-only source citations must not acquire an invented publication day."""
import importlib.util
import json
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/nidi_date_precision/first100.html"


def load():
    spec = importlib.util.spec_from_file_location(
        "nidi_dates", ROOT / "crawler/sites/custom/nidi-nl-en.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NidiDatePrecision(unittest.TestCase):
    def test_actual100_citations_preserve_year_ids_files_without_january_first(self):
        module = load()
        native = object.__new__(module.NidiNlEnCrawler)
        items = native._parse_list_soup(BeautifulSoup(FIXTURE.read_text(), "lxml"), 1)
        self.assertEqual(len(items), 100)
        for item in items:
            with self.subTest(record=item["external_id"]):
                self.assertRegex(item["year_raw"], r"^\d{4}$")
                self.assertIsNone(item["listed_date"])
                paper = native._paper_from_item(
                    item, abstract=item["reference"], metadata=native._base_metadata(item))
                for field in ["published_date", "listed_date", "posted_date"]:
                    self.assertIsNone(paper[field])
                self.assertEqual(json.loads(paper["metadata"])["posted_date"], item["year_raw"])
                for field in ["external_id", "post_number", "original_filename", "pdf_url"]:
                    self.assertEqual(paper[field], item[field])

    def test_day_precision_dates_still_parse(self):
        module = load()
        for text in ["2026-10-08", "8 October 2026", "08/10/2026"]:
            with self.subTest(text=text):
                self.assertEqual(module._parse_date(text), "2026-10-08")

    def test_year_or_month_precision_remains_unknown_day(self):
        module = load()
        for text in ["2026", "October 2026", "Published in 2026", "", None]:
            with self.subTest(text=text):
                self.assertIsNone(module._parse_date(text))


if __name__ == "__main__":
    unittest.main()
