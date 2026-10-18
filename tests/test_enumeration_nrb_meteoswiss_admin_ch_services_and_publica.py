import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "meteoswiss-admin-ch-services-and-publica"
API = "https://www.meteoswiss.admin.ch/api/search/public-en/publications/results.json"
BASE = "https://www.meteoswiss.admin.ch"
LEAD = "A climate bulletin describing the weather of the month in Switzerland with temperature and rain anomalies."


def doc(n, lead):
    return {"path": f"/services-and-publications/publications/p{n}.html", "title": f"Pub {n}", "lead": lead,
            "publicationDate": "2026-0%d-01T00:00:00Z" % n, "keywords": "climate", "categories": ["Bulletin"],
            "_version_": 1000 + n, "pageSubType": "publication"}


PAGES = {0: [doc(1, LEAD), doc(2, "")], 2: [doc(3, ""), doc(4, "")], 4: [doc(5, LEAD)]}
DETAIL = ('<mch-publication-page authors="Alice, Bob" edition="MeteoSwiss Report" '
          'publication-src="{&quot;href&quot;:&quot;/dam/p.pdf&quot;,&quot;label&quot;:&quot;p.pdf&quot;}">'
          '</mch-publication-page>')


class MeteoSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(API):
            start = int(query(url).get("start", "0"))
            return {"response": {"numFound": 5, "start": start, "docs": PAGES.get(start, [])}}
        if url.startswith(BASE + "/services-and-publications/publications/p"):
            return DETAIL
        return None

    def test_plan_stores_what_crawl_saves(self):
        plan = __import__("tests.nrb_helpers", fromlist=["plan_of"]).plan_of(SITE)
        plan["listing"]["page_size"] = 2  # synthetic pages of two docs
        expected = native(SITE, self.serve, init=lambda c: setattr(c, "_PAGE_SIZE", 2) or setattr(c, "_curl_get", lambda u, compressed=True: _txt(self.serve(u))))
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=plan)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=lambda c: setattr(c, "_PAGE_SIZE", 2) or setattr(c, "_curl_get", lambda u, compressed=True: _txt(self.serve(u))))),
                         doc_identities(docs))


def _txt(body):
    import json
    return None if body is None else (json.dumps(body) if isinstance(body, dict) else body)


if __name__ == "__main__":
    unittest.main()
