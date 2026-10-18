import json
import unittest

from crawler.enumeration.http import Response
from tests.nrb_helpers import native, native_docs, doc_identities, as_text

SITE = "energy-gov-management"
B = "https://www.energy.gov"
DESC = ("The DOE Comprehensive Emergency Management System provides a framework within which to address "
        "all hazards from natural technological and human caused events.")


def page(rows):
    settings = json.dumps({"datatableRows": rows})
    return (f'<html><body><main>table</main><script type="application/json" '
            f'data-drupal-selector="drupal-settings-json">{settings}</script></body></html>')


DIRECTIVES = page([{"ID": "DOE G 151.1-1B", "TYPE": "Guide", "ORG": "NNSA", "EDATE": "7/28/2022",
                    "TITLE": "Comprehensive Emergency Management System Guide", "DESC": DESC,
                    "SERIES": "100", "AREAS": "Safety; Security", "URL": "https://energy.gov/media/357603"},
                   {"ID": "DOE O 1", "TITLE": "Short", "DESC": "short", "URL": "https://energy.gov/media/1"}])
DELEGATIONS = page([{"ID": "S1-DEL-RATES-1993", "TITLE": "Delegation of authority to set power rates",
                     "DIRECTLINK": B + "/sites/default/files/2026-05/S1-DEL-RATES-1993.pdf",
                     "DELEGANTPOSITION": "Secretary of Energy", "DELEGANT": "Secretary",
                     "DELEGATEPOSITION": "Deputy Secretary", "DELEGATE": "Deputy", "EDATE": "1/1/1993",
                     "PDFname": "S1-DEL-RATES-1993.pdf"}])
FINAL = B + "/documents/comprehensive-emergency-management-system-guide.pdf"


class EnergyMgmtSplit(unittest.TestCase):
    def serve(self, url):
        u = url.split("?")[0]
        if u.endswith("/management/directives-library"):
            return DIRECTIVES
        if u.endswith("/management/delegations-library"):
            return DELEGATIONS
        return None

    def init(self, c):
        c._curl_resolve = lambda url: (FINAL, "application/pdf") if url.endswith("357603") else (url, "application/pdf")

    def pipeline(self):
        from tests.enumeration_fakes import Env
        from tests.nrb_helpers import plan_of, _instance, docs_of
        import contextlib, io
        plan = plan_of(SITE)
        plan["request_interval_s"] = 0

        def handler(url):
            if url.endswith("/robots.txt"):
                return Response(404, {}, b"")
            if url == B + "/media/357603":
                return Response(302, {"Location": FINAL}, b"", url)
            if url == FINAL or url.endswith(".pdf") or "/media/" in url:
                return Response(200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 x", url)
            body = self.serve(url)
            if body is None:
                return Response(404, {}, b"", url)
            return Response(200, {"Content-Type": "text/html"}, as_text(body).encode(), url)

        env = Env(SITE)
        try:
            crawler, _ = _instance(SITE, env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler)
            return result, docs_of(env)
        finally:
            env.close()

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"], init=self.init)
        self.assertEqual(2, len(expected))
        result, docs = self.pipeline()
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"], init=self.init)),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
