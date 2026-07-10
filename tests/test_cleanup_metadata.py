# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cleanup_metadata import clean_text, clean_keywords, normalize_date
from scripts.cleanup_metadata import fix_pdf_url, fix_meta_url


class TestCleanText(unittest.TestCase):
    def test_entity_decode(self):
        # esteri-it 실사례: &#8211; = en-dash, &#8217; = right quote
        self.assertEqual(
            clean_text("Nota di inquadramento &#8211; Conferenza"),
            "Nota di inquadramento – Conferenza",
        )
        self.assertEqual(clean_text("Blood Diseases &amp; Disorders"),
                         "Blood Diseases & Disorders")

    def test_double_encoded_entity(self):
        self.assertEqual(clean_text("A &amp;amp; B"), "A & B")

    def test_tag_strip(self):
        self.assertEqual(
            clean_text('intro <div class="x">body</div> <br/> end'),
            "intro body end",
        )

    def test_math_lt_preserved(self):
        # 태그가 아닌 부등호는 보존 (< 뒤가 영문자/슬래시일 때만 태그)
        self.assertEqual(clean_text("p<0.05, q>1"), "p<0.05, q>1")

    def test_newline_and_spaces(self):
        self.assertEqual(clean_text("Line one\n  Line two\t x"),
                         "Line one Line two x")

    def test_cdata_removed(self):
        self.assertEqual(clean_text("<![CDATA[Communiqué]]>"), "Communiqué")

    def test_none_and_empty_passthrough(self):
        self.assertIsNone(clean_text(None))
        self.assertEqual(clean_text(""), "")

    def test_clean_value_unchanged_identity(self):
        s = "Perfectly normal title 2024"
        self.assertEqual(clean_text(s), s)


class TestCleanKeywords(unittest.TestCase):
    def test_cdata_and_dupes(self):
        # presse-economie 실사례: CDATA 잔재 + 중복 토큰
        raw = ("Communiqué de presse, Bruno Le Maire, "
               "<![CDATA[Communiqué de presse]]>, <![CDATA[Bruno Le Maire]]>")
        self.assertEqual(clean_keywords(raw),
                         "Communiqué de presse, Bruno Le Maire")

    def test_sub_tag_stripped(self):
        self.assertEqual(clean_keywords("CO<sub>2</sub>, Carbon capture"),
                         "CO2, Carbon capture")

    def test_case_insensitive_dedupe_keeps_first(self):
        self.assertEqual(clean_keywords("Energy, energy, ENERGY, wind"),
                         "Energy, wind")


class TestNormalizeDate(unittest.TestCase):
    def test_already_iso_returns_none(self):
        # 호출부는 비ISO만 넘기지만 방어적으로: 동일값이면 변경 불필요 표시(None 아님)
        self.assertEqual(normalize_date("2025-08-01"), "2025-08-01")

    def test_french_month(self):
        self.assertEqual(normalize_date("01 août 2025"), "2025-08-01")
        self.assertEqual(normalize_date("01 avril 2021"), "2021-04-01")

    def test_dotted_korean_style(self):
        self.assertEqual(normalize_date("2014.10.24"), "2014-10-24")
        self.assertEqual(normalize_date("2025.09.15."), "2025-09-15")

    def test_rfc822(self):
        self.assertEqual(normalize_date("Fri, 01 Aug 2025 09:36:29 +0000"),
                         "2025-08-01")

    def test_short_year_english(self):
        self.assertEqual(normalize_date("1 Feb 24"), "2024-02-01")
        self.assertEqual(normalize_date("19 Nov 24"), "2024-11-19")

    def test_slash_us_default(self):
        self.assertEqual(normalize_date("09/30/2021"), "2021-09-30")

    def test_slash_dayfirst(self):
        self.assertEqual(normalize_date("06/02/2017", dayfirst=True), "2017-02-06")

    def test_slash_ymd(self):
        self.assertEqual(normalize_date("2025/11/24"), "2025-11-24")

    def test_long_english(self):
        self.assertEqual(normalize_date("March 17, 2026"), "2026-03-17")
        self.assertEqual(normalize_date("18 March 2026"), "2026-03-18")

    def test_unparseable_returns_none(self):
        self.assertIsNone(normalize_date("2026 - 12??"))
        self.assertIsNone(normalize_date(":"))
        self.assertIsNone(normalize_date(""))
        self.assertIsNone(normalize_date(None))

    def test_out_of_range_rejected(self):
        self.assertIsNone(normalize_date("0020-01-01"))
        self.assertIsNone(normalize_date("-001-11-30"))

    def test_month_year_only_rejected(self):
        self.assertIsNone(normalize_date("01/2005"))
        self.assertIsNone(normalize_date("2005/01"))
        self.assertIsNone(normalize_date("August 2025"))

    def test_capitalized_french_month(self):
        self.assertEqual(normalize_date("01 Février 2025"), "2025-02-01")

    def test_idempotent_on_all_parseable(self):
        cases = ["2025-08-01", "01 août 2025", "2014.10.24", "2025.09.15.",
                  "Fri, 01 Aug 2025 09:36:29 +0000", "1 Feb 24", "09/30/2021",
                  "2025/11/24", "March 17, 2026", "18 March 2026"]
        for c in cases:
            r1 = normalize_date(c)
            self.assertIsNotNone(r1, c)
            self.assertEqual(normalize_date(r1), r1, c)


class TestUrlRules(unittest.TestCase):
    def test_http_ok(self):
        self.assertEqual(fix_pdf_url("https://x.org/a.pdf", "https://x.org/p"),
                         ("https://x.org/a.pdf", "ok"))

    def test_relative_absolutized(self):
        self.assertEqual(
            fix_pdf_url("/globalassets/n.pdf", "https://www.sgu.se/en/page"),
            ("https://www.sgu.se/globalassets/n.pdf", "absolutized"),
        )

    def test_citation_nulled(self):
        self.assertEqual(fix_pdf_url("Brouwer2024", "https://nin.nl/p"),
                         (None, "nulled"))
        self.assertEqual(fix_pdf_url("DOI: 10.14207/ejsd.2019", "https://toi.no/p"),
                         (None, "nulled"))

    def test_ftp_kept(self):
        self.assertEqual(fix_pdf_url("ftp://ftp.asc-csa.gc.ca/a.pdf", "https://x/p"),
                         ("ftp://ftp.asc-csa.gc.ca/a.pdf", "kept_ftp"))

    def test_meta_error_nulled(self):
        self.assertEqual(fix_meta_url("ERROR"), (None, "nulled"))

    def test_meta_citation_kept(self):
        val = "Bączek-Kwinta, R. (2006). Reakcja..."
        self.assertEqual(fix_meta_url(val), (val, "kept"))


if __name__ == "__main__":
    unittest.main()
