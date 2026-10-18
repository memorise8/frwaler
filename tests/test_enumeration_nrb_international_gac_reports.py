"""international-canada-ca-en: index + report pages; web-archive rows are not listed."""
import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

BASE = "https://international.canada.ca/en/global-affairs/corporate/reports/access-information-privacy-reports-global-affairs"
GC = "https://www.international.gc.ca/transparency-transparence/information-privacy-information-protection/reports-rapport/index.aspx?lang=eng"
TEXT = ("This report describes how Global Affairs Canada administered the Access to Information Act during "
        "the fiscal year, including statistics on requests received and completed.")
INDEX = f"""<html><head><link rel="canonical" href="{BASE}"></head><body><main>
<h2>Access to Information Act reports</h2><ul>
<li><a href="{BASE}/2024-2025-annual-report-access-information">2024-25 report to Parliament</a></li>
<li><a href="{BASE}/2023-short">2023-24 report</a></li>
<li><a href="https://webarchiveweb.wayback.bac-lac.canada.ca/web/2025/https:/www.international.gc.ca/r.aspx">2022-23 report</a> <span class="label-warning">Archived</span></li></ul>
<h2>Privacy Act reports</h2><ul><li><a href="{BASE}/2024-2025-privacy">2024-25 privacy report</a></li></ul>
</main></body></html>"""


def detail(slug, body):
    return (f'<html><head><link rel="canonical" href="{BASE}/{slug}">'
            f'<meta name="dcterms.issued" content="2025-06-01"><meta name="dcterms.title" content="Report {slug}">'
            f'</head><body><main><h1>Report {slug}</h1><p>{body}</p>'
            f'<a href="/files/{slug}.pdf">PDF</a></main></body></html>')


CHALLENGE = "<html><head><title>Session Verification</title></head><body>challenge</body></html>"
PAGES = {"https://webarchiveweb.wayback.bac-lac.canada.ca/web/2025/https:/www.international.gc.ca/r.aspx": CHALLENGE,
BASE + "/2024-2025-annual-report-access-information": detail("2024-2025-annual-report-access-information", TEXT),
         BASE + "/2023-short": detail("2023-short", "short"),
         BASE + "/2024-2025-privacy": detail("2024-2025-privacy", TEXT)}


class GacReports(unittest.TestCase):
    def serve(self, url):
        if url.split("&page=")[0] == GC or url.split("?page=")[0] == BASE:
            return INDEX
        return PAGES.get(url)

    def check(self, site):
        expected = native(site, self.serve, net_methods=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(site, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(site, self.serve, net_methods=["_curl_get"])), doc_identities(docs))

    def test_canada_ca(self):
        self.check("international-canada-ca-en")


if __name__ == "__main__":
    unittest.main()
