import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "mfat-govt-nz-en"
B = "https://www.mfat.govt.nz"
LIST = B + "/en/about-us/mfat-annual-reports"
PAD = "<!-- " + "x" * 6000 + " -->"
PAGE = f"""<html><body><main>
<div class="promo"><h2><a href="/en/about-us/mfat-annual-reports/mfat-annual-report-2025-26">MFAT Annual Report 2025-26</a></h2><p>Annual report.</p></div>
<div class="promo"><h2><a href="/en/about-us/mfat-annual-reports/mfat-annual-report-2022-23">MFAT Annual Report 2022-23</a></h2></div>
</main>{PAD}</body></html>"""
BODY = ("The Ministry's annual report presents performance against strategic intentions, financial statements "
        "and the activities of the year across foreign affairs and trade.")
DETAIL = f'<html><body><main><p>{BODY}</p><a href="/assets/report.pdf">PDF</a></main>{PAD}</body></html>'


class MfatSplit(unittest.TestCase):
    def serve(self, url):
        u = url.split("?")[0]
        if u == LIST:
            return PAGE
        if u.startswith(LIST + "/"):
            return DETAIL
        return None

    def init(self, c):
        c._wayback_fetch_timestamps = lambda *a, **k: None
        c._wayback_cdx_fetch = lambda *a, **k: None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"], init=self.init)
        self.assertGreaterEqual(len(expected), 2)
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"], init=self.init)),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
