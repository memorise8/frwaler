"""Idiap year/month citations must not acquire a fabricated publication day."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/idiap_date_precision"


def load():
    spec = importlib.util.spec_from_file_location(
        "idiap_dates", ROOT / "crawler/sites/custom/publications-idiap-ch-publications.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdiapDatePrecision(unittest.TestCase):
    def test_actual_year_only_list_and_detail_preserve_identity_and_raw_year(self):
        module = load()
        crawler = object.__new__(module.PublicationsIdiapChPublicationsCrawler)
        items, _ = crawler._parse_list_page((FIXTURES / "journals.html").read_text())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["external_id"], "5817")
        for item in items:
            self.assertEqual(item["listed_date_raw"], "2026")
            self.assertIsNone(item["listed_date"])
        paper = crawler._parse_detail((FIXTURES / "5817.html").read_text(), items[0])
        self.assertEqual(paper["external_id"], "5817")
        self.assertEqual(paper["post_number"], "5817")
        self.assertEqual(paper["url"], items[0]["url"])
        self.assertTrue(paper["pdf_url"].endswith("Li_IJRR_2026.pdf"))
        for field in ["published_date", "listed_date", "posted_date"]:
            self.assertIsNone(paper[field])
        metadata = json.loads(paper["metadata"])
        self.assertEqual(metadata["year"], "2026")
        self.assertEqual(metadata["posted_date"], "2026")
        self.assertEqual(metadata["list_record"]["listed_date_raw"], "2026")

    def test_partial_or_invalid_dates_never_gain_a_day(self):
        module = load()
        for year, month in [("2026", None), ("2026", "May"), ("2026", "5"),
                            ("2026", "unknown"), (None, None), ("2026-05", None),
                            ("2026-02-30", None)]:
            with self.subTest(year=year, month=month):
                self.assertIsNone(module._iso_date_from_parts(year, month))

    def test_explicit_valid_day_retains_source_precision(self):
        module = load()
        self.assertEqual(module._iso_date_from_parts("2026-10-08"), "2026-10-08")
        self.assertEqual(module._iso_date_from_parts("2024-02-29"), "2024-02-29")


if __name__ == "__main__":
    unittest.main()
