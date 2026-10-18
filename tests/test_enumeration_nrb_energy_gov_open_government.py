import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "energy-gov-open-government"
B = "https://www.energy.gov"
LONG = ("The Department of Energy customer service plan describes how the department will improve services "
        "for the public and measure customer satisfaction across programs.")
PAGE = f"""<html><body><main>
<h2>Open Government Plan</h2>
<p><a href="/downloads/open-government-plan-40">Open Government Plan 4.0</a></p>
<p><a href="/sites/default/files/plan-short.pdf">Short PDF</a></p>
<h2>Customer Service</h2>
<p>{LONG} <a href="/sites/default/files/DOE_Customer_Service_Plan.pdf">Customer Service Plan</a></p>
</main></body></html>"""
BODY = ("<html><body><main><p>The Open Government Plan 4.0 sets out the Department's commitments to "
        "transparency, participation and collaboration, with milestones for open data and FOIA.</p></main></body></html>")


class OpenGovSplit(unittest.TestCase):
    def serve(self, url):
        u = url.split("?")[0]
        if u == B + "/open-government":
            return PAGE
        if u == B + "/downloads/open-government-plan-40":
            return BODY
        if u.endswith(".pdf"):
            return b"%PDF-1.4 x"
        return None

    def test_plan_stores_what_crawl_saves(self):
        def fetch(url, timeout=30):
            body = self.serve(url)
            return (200, body) if isinstance(body, str) else (0, "")

        def native_init(c):
            # crawl() reads pages through the module-level _curl_fetch(url) -> (status, html)
            import sys
            sys.modules[type(c).__module__]._curl_fetch = fetch

        papers = native(SITE, self.serve, init=native_init)
        self.assertEqual(2, len(papers))
        result, docs = pipeline(SITE, self.serve, types=lambda u: "application/pdf" if u.endswith(".pdf") else None)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=native_init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
