import copy
import unittest

from tests.nrb_helpers import fixture, native, pipeline, identities, doc_identities, query

SITE = "wbf-admin-ch-en"


class WbfSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture(SITE)

    def serve(self, url):
        if url.startswith("https://d-nsbc-p.admin.ch/v1/search"):
            return self.fx["listing"] if query(url).get("offset", "0") == "0" else self.fx["empty"]
        return None

    def init(self, crawler):
        crawler._fetch_list_page = lambda offset, limit: copy.deepcopy(
            self.fx["listing"] if offset == 0 else self.fx["empty"])

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertEqual(2, len(expected))  # third row: abstract too short
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(identities(expected), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
