import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "oig-treasury-gov-other-reports-testim"
B = "https://oig.treasury.gov"
AUDIT = f"""<html><body><table class="views-table views-view-table"><tbody>
<tr><td>IRS</td><td><time datetime="2025-09-30T12:00:00Z">Sep 30</time></td>
<td><a href="/system/files/2025-09/OIG-25-040.pdf">OIG-25-040 Audit of the Department's Financial Statements for Fiscal Years 2025 and 2024</a></td></tr>
<tr><td>Mint</td><td></td><td><a href="/system/files/2024-01/short.pdf">Short title</a></td></tr>
</tbody></table></body></html>"""
TESTI = f"""<html><body><table class="views-table views-view-table"><tbody>
<tr><td><a href="{B}/system/files/2023-05/testimony-2023.pdf">Testimony of the Inspector General before the House Financial Services Committee</a></td></tr>
<tr><td><a href="/system/files/2025-09/OIG-25-040.pdf">OIG-25-040 Audit of the Department's Financial Statements for Fiscal Years 2025 and 2024</a></td></tr>
</tbody></table></body></html>"""
EMPTY = '<html><body><div class="view-empty">No results</div></body></html>'


class OigSplit(unittest.TestCase):
    def serve(self, url):
        path = url.split("?")[0]
        page = query(url).get("page", "0")
        if path == B + "/reports/audit-and-evaluation":
            return AUDIT if page == "0" else EMPTY
        if path == B + "/reports/testimonies-other-documents":
            return TESTI if page == "0" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"])
        self.assertEqual(2, len(expected))  # short title skipped; the repeated PDF is saved once
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
