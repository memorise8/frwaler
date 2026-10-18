import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "geoportal-statistics-gov-uk-search"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
API = "https://geoportal.statistics.gov.uk/api/search/v1/collections/document/items"


def props(title, desc):
    return {"title": title, "description": desc, "created": 1760000000000, "modified": 1760500000000,
            "type": "PDF", "url": "https://www.arcgis.com/sharing/rest/content/items/x/data", "name": "a.pdf"}


FEATURES = [
    {"id": "aaa111", "properties": props("Doc A", "A guide to the statistical geography of the UK, " * 4)},
    {"id": "bbb222", "properties": props("Doc B", "short")},
]


class GeoportalSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(API + "/"):
            fid = url[len(API) + 1:].split("?")[0]
            f = next((x for x in FEATURES if x["id"] == fid), None)
            return None if f is None else dict(f)
        if url.startswith(API):
            start = int(query(url).get("startindex", "1"))
            return {"numberMatched": 2, "features": FEATURES if start == 1 else [], "links": []}
        return None

    def init(self, crawler):
        crawler._fetch_json = lambda url, context=None: self.serve(url)
        crawler._filename_from_head = lambda url: None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
