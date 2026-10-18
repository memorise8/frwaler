import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "rcaanc-cirnac-gc-ca-eng"
B = "https://www.rcaanc-cirnac.gc.ca"
START = B + "/eng/1575131323136/1575131374706"
TEXT = ("This annual statutory report describes the activities of the department in the fiscal year, "
        "including progress on reconciliation and the implementation of agreements with partners.")
LIST = f"""<html><body><main><h2>Annual reports</h2><ul>
<li><a href="/eng/1787169911022/1787169970928">Annual Statutory Report 2025-26</a></li>
<li><a href="/eng/1756126440869/1756126476029">Sixth Annual Report</a></li>
<li><a href="#x">anchor</a></li><li><a href="https://example.org/eng/1/2">ext</a></li></ul>
<h2>Audits</h2><ul><li><a href="/eng/1700000000000/1700000000001">Audit short</a></li></ul></main></body></html>"""


def detail(title, body):
    return (f'<html><head><meta name="dcterms.issued" content="2025-08-25"><meta name="dcterms.creator" '
            f'content="Crown-Indigenous Relations"></head><body><main><h1>{title}</h1><p>{body}</p>'
            f'<a href="/DAM/x/report.pdf">PDF version</a></main></body></html>')


PAGES = {B + "/eng/1787169911022/1787169970928": detail("Annual Statutory Report 2025-26", TEXT),
         B + "/eng/1756126440869/1756126476029": detail("Sixth Annual Report", TEXT),
         B + "/eng/1700000000000/1700000000001": detail("Audit short", "too short")}


class RcaancSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == START:
            return LIST
        return PAGES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
