"""Retained MFDS records keep their own registration date and source identity."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/mfds_list_dates"


def crawler():
    spec = importlib.util.spec_from_file_location(
        "mfds_dates", ROOT / "crawler/sites/custom/mfds-go-kr-brd.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return object.__new__(module.MFDSBoardCrawler)


class MFDSListDates(unittest.TestCase):
    def test_retained_first_second_last_pages_keep27_ids_titles_and_dates(self):
        native = crawler()
        provenance = json.loads((FIXTURES / "provenance.json").read_text())
        for source in provenance["sources"]:
            with self.subTest(page=source["fixture"]):
                actual = native._parse_list_page((FIXTURES / source["fixture"]).read_text())
                self.assertEqual(actual, [tuple(row) for row in source["expected"]])

    def test_missing_date_never_borrows_neighbor_or_attachment_date(self):
        html = '''<ul>
          <li><a class="title" href="view.do?seq=11">No date</a>
            <ul><li>attachment-2025-01-02.pdf</li></ul></li>
          <li><a class="title" href="view.do?seq=12">Dated</a>
            <div class="right_column">2026-10-08</div></li>
          <li><a class="title" href="view.do?seq=13">Also missing</a></li>
        </ul>'''
        self.assertEqual(crawler()._parse_list_page(html),
                         [("11", "No date", ""), ("12", "Dated", "2026-10-08"),
                          ("13", "Also missing", "")])

    def test_legacy_row_date_containers_remain_supported(self):
        for tag in ["li", "tr"]:
            for date_class in ["winfo", "date"]:
                with self.subTest(tag=tag, date_class=date_class):
                    html = (f'<{tag}><td><a class="title" href="view.do?seq=14">Legacy</a>'
                            f'<span class="{date_class}">등록일 2024-03-01</span></td></{tag}>')
                    if tag == "tr":
                        html = "<table>" + html + "</table>"
                    self.assertEqual(crawler()._parse_list_page(html),
                                     [("14", "Legacy", "2024-03-01")])


if __name__ == "__main__":
    unittest.main()
