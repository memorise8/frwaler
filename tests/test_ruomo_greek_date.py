import importlib.util
from pathlib import Path
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuomoGreekDate(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "ruomo_dates", ROOT / "crawler/sites/custom/ruomoplus-lib-uom-gr-simple-search.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.crawler = object.__new__(module.RuomoplusLibUomGrSimpleSearchCrawler)

    def test_actual_may_row_preserves_handle_url_and_date(self):
        fixture = Path(__file__).parent / "fixtures/ruomo_greek_date/may.html"
        rows = self.crawler._parse_list_page(fixture.read_text())
        self.assertEqual(rows, [{"handle": "8000/2271",
                                "url": "https://ruomoplus.lib.uom.gr/handle/8000/2271",
                                "listed_date_raw": "12-Μαΐ-2026"}])
        self.assertEqual(self.crawler._parse_greek_date(rows[0]["listed_date_raw"]), "2026-05-12")

    def test_composed_and_decomposed_months_parse_without_changing_precision(self):
        for text, expected in [("12-Μαΐ-2026", "2026-05-12"),
                               ("5-Οκτ-2026", "2026-10-05"),
                               ("21-Σεπ-2026", "2026-09-21")]:
            for form in ["NFC", "NFD"]:
                with self.subTest(text=text, form=form):
                    self.assertEqual(self.crawler._parse_greek_date(
                        unicodedata.normalize(form, text)), expected)

    def test_ambiguous_june_july_and_partial_dates_remain_unknown(self):
        for text in ["2-Ιου-2026", "Μαΐ-2026", "2026", "12-unknown-2026", None]:
            with self.subTest(text=text):
                self.assertIsNone(self.crawler._parse_greek_date(text))


if __name__ == "__main__":
    unittest.main()
