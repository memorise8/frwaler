import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "data-gv-at-datasets"
LIST = "https://www.data.gv.at/api/hub/search/search"
DET = "https://www.data.gv.at/api/hub/search/datasets/"
U1 = "cef1c710-3564-4c4a-adce-39896ba4bed8"


def rec(i, desc, dist_title="Bericht.pdf"):
    return {"id": i, "title": {"de": f"Datensatz {i}"}, "description": {"de": desc},
            "publisher": {"name": "Land Kaernten"}, "issued": "2016-10-31T00:00:00Z", "modified": "2026-01-01",
            "categories": [{"label": {"de": "Umwelt"}}], "keywords": [{"label": "Plan"}],
            "distributions": [{"id": "d1", "format": {"id": "PDF"}, "title": {"de": dist_title},
                               "access_url": [f"https://www.data.gv.at/files/{i}.pdf"]}]}


LONG = "Eine ausfuehrliche Beschreibung des Datensatzes mit mehr als hundert Zeichen, damit die Mindestlaenge erreicht ist."
LISTING = {"result": {"count": 3, "results": [rec(U1, "kurz"), rec("ankoe-foia-1486", LONG), rec("x-2", "")]}}
EMPTY = {"result": {"count": 3, "results": []}}
DETAILS = {U1: {"result": rec(U1, LONG)}, "ankoe-foia-1486": {"result": {}}, "x-2": {"result": rec("x-2", "")}}


class DataGvAtSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(LIST):
            return LISTING if query(url).get("page", "0") == "0" else EMPTY
        if url.startswith(DET):
            return DETAILS.get(url[len(DET):])
        return None

    def test_plan_stores_what_crawl_saves(self):
        def init(c):
            c.delivery_cursor = None
            c._advance_cursor = lambda *a, **k: None
            c._mark_exhausted = lambda *a, **k: None
        expected = native(SITE, self.serve, net_methods=["_curl_get"], init=init)
        self.assertGreaterEqual(len(expected), 2)
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
