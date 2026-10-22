import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from tests.collector_split_helpers import load

SITE = "cwf-ca-publications"
REPORT = "https://cwf.ca/research/publications/report-confederation-of-tomorrow-2019-survey-of-canadians/"
PDF = "https://cwf.ca/wp-content/uploads/2019/03/Confederation-of-Tomorrow-Survey-2019-Report-1.pdf"


def links(*pairs):
    return BeautifulSoup("<div>" + "".join(f'<a href="{h}">{t}</a>' for h, t in pairs) + "</div>", "html.parser")


class CwfPdfUrl(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def test_pdf_text_links_to_pages_are_not_pdfs(self):
        root = links(("https://cwf.ca", "Read the report (PDF)"), (REPORT, "Download PDF"), (PDF, "Read the report"))
        self.assertEqual([PDF], self.crawler._extract_pdf_urls(root))

    def test_download_button_outside_wysiwyg(self):
        html = ('<html><head><title>Policy Levers Toolkit</title></head><body><article><div class="article-body">'
                '<div class="wysiwyg"><p>The toolkit reviews policy levers that help cleantech firms grow.</p></div>'
                f'<div class="button-container"><a href="{PDF}">Download PDF</a></div></div></article></body></html>')
        with patch.object(self.crawler, "_extract_raw_date", return_value=None), \
                patch.object(self.crawler, "_extract_authors", return_value=[]), \
                patch.object(self.crawler, "_extract_abstract", return_value="x" * 120):
            paper = self.crawler._parse_detail_page(html, REPORT, {"url": REPORT, "title": "Policy Levers Toolkit"})
        self.assertEqual(PDF, paper["pdf_url"])

    def test_uploads_link_without_pdf_suffix_kept_by_text(self):
        upload = "https://cwf.ca/wp-content/uploads/2019/03/report-1/?download=1"
        self.assertEqual([upload], self.crawler._extract_pdf_urls(links((upload, "Download PDF"))))


if __name__ == "__main__":
    unittest.main()
