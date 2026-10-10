"""Actual ministry lists must reach native discovery with usable attachment URLs."""
import importlib.util
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures/ninth_source_lists"


def load(site):
    spec = importlib.util.spec_from_file_location(
        site.replace("-", "_"), ROOT / "crawler/sites/custom" / (site + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MotirPublishedDetailLinks(unittest.TestCase):
    def test_retained_native_and_delivered_boards_discover_all20_ids(self):
        module = load("motir-go-kr-kor")
        native = object.__new__(module.MotirGoKrKorCrawler)
        for filename in ["motir-native.html", "motir-publications.html"]:
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_text()
                expected = []
                for row in BeautifulSoup(raw, "lxml").select("tbody tr"):
                    a = row.select_one(".board-link a")
                    source_id = re.search(r"article.view\('(\d+)'\)", a["onclick"])[1]
                    date = next(td.get_text(strip=True) for td in row.select("td")
                                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", td.get_text(strip=True)))
                    expected.append((source_id, a.get_text(strip=True), date))
                actual = native._parse_list_page(raw)
                self.assertEqual(len(actual), 10)
                self.assertEqual([(x["bbs_seq_n"], x["title"], x["listed_date"])
                                  for x in actual], expected)

    def test_legacy_javascript_link_keeps_existing_id(self):
        module = load("motir-go-kr-kor")
        native = object.__new__(module.MotirGoKrKorCrawler)
        raw = '''<table id="mytable"><tbody><tr>
          <td><a href="javascript:article.view('321');">Legacy</a></td>
          <td>2024-07-08</td></tr></tbody></table>'''
        self.assertEqual(native._parse_list_page(raw)[0]["bbs_seq_n"], "321")

    def test_attachment_navigation_and_non_numeric_paths_are_not_records(self):
        module = load("motir-go-kr-kor")
        native = object.__new__(module.MotirGoKrKorCrawler)
        raw = '''<table id="mytable"><tbody>
          <tr><td><a href="/attach/down/321/file">Attachment</a></td></tr>
          <tr><td><a href="/kor/article/BOARD/nonnumeric/view">Invalid</a></td></tr>
          <tr><td><a href="?pageIndex=2">Next</a></td></tr>
          </tbody></table>'''
        self.assertEqual(native._parse_list_page(raw), [])


class NECAttachmentURLs(unittest.TestCase):
    def test_retained30_rows_keep_ids_dates_filenames_and_resolve_downloads(self):
        module = load("nec-go-kr-site")
        for filename in ["nec-press.html", "nec-research.html"]:
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_text()
                actual = module._parse_list_page(raw)
                rows = [a.find_parent("li") for a in
                        BeautifulSoup(raw, "lxml").select("a.btn_bbsDetail")]
                self.assertEqual(len(actual), 15)
                for item, row in zip(actual, rows):
                    a = row.select_one("a.btn_bbsDetail")
                    file = row.select_one("span.fileDown a[href*='Download.do']")
                    self.assertEqual(item["bc_idx"], a["data-info1"])
                    self.assertEqual(item["title"], a.get_text(strip=True))
                    self.assertEqual(item["listed_date"], row.select_one("span.date").get_text(strip=True))
                    if file:
                        self.assertEqual(item["pdf_url"], urljoin(module._LIST_URL, file["href"]))
                        self.assertEqual(parse_qs(urlsplit(item["pdf_url"]).query),
                                         parse_qs(urlsplit(file["href"]).query))
                        self.assertEqual(item["original_filename"],
                                         file.get("title", "").replace(" 파일다운로드", "").strip())

    def test_detail_fallback_resolves_relative_and_preserves_absolute_download(self):
        module = load("nec-go-kr-site")
        for href in ["/common/board/Download.do?bcIdx=12&streFileNm=report.pdf",
                     "https://img.nec.go.kr/Download.do?bcIdx=12&streFileNm=report.pdf"]:
            with self.subTest(href=href):
                raw = f'<a class="fileLink" href="{href}" title="보고서.pdf 파일다운로드">보고서.pdf</a>'
                result = module._parse_detail_page(raw)
                self.assertEqual(result["pdf_url"], urljoin(module._LIST_URL, href))
                self.assertEqual(result["original_filename"], "보고서.pdf")


if __name__ == "__main__":
    unittest.main()
