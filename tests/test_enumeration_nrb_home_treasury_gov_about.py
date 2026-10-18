import re
import subprocess
import tempfile
import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = "home-treasury-gov-about"
START = ("https://home.treasury.gov/about/budget-financial-reporting-planning-and-performance"
         "/good-accounting-obligation-in-government-act-gao-ig-act-reports")
F = "https://home.treasury.gov/system/files/266/"
INTRO = ("The Good Accounting Obligation in Government Act requires each agency to include in its annual budget "
         "justification a report on public recommendations.")
PAGE = f"""<html><head><meta property="og:updated_time" content="2024-01-23T10:00:00"></head><body>
<div class="field--name-field-page-body"><p>{INTRO}</p>
<ul><li><a href="{F}GAO-IG-Act-Report-for-FY-2025.pdf">GAO-IG Act Report for FY 2025</a></li>
<li><a href="{F}GAO-IG-Act-Report-Executive-Summary-for-FY-2024.pdf">GAO-IG Act Report Executive Summary for FY 2024</a></li>
</ul></div></body></html>"""
FILES = {F + "GAO-IG-Act-Report-for-FY-2025.pdf": make_pdf("Treasury GAO IG Act report fiscal year 2025 recommendations status open closed"),
         F + "GAO-IG-Act-Report-Executive-Summary-for-FY-2024.pdf": make_pdf("short")}


def first_pages(body):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(body); fh.flush()
        r = subprocess.run(["pdftotext", "-f", "1", "-l", "2", fh.name, "-"], capture_output=True, timeout=30)
        if r.returncode != 0:
            return ""
        t = r.stdout.decode("utf-8", errors="replace")
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()[:2000]


class TreasurySplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == START:
            return PAGE
        return FILES.get(url)

    def init(self, c):
        c._extract_pdf_abstract = lambda url, max_chars=2000: first_pages(FILES[url])

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"], init=self.init)
        self.assertEqual(2, len(expected))  # short PDF text falls back to the page intro
        result, docs = pipeline(SITE, self.serve, types=lambda u: "application/pdf" if u.endswith(".pdf") else None)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"], init=self.init)),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
