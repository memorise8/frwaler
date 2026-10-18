import unittest

from tests.nrb_helpers import fixture, native, native_docs, pipeline, identities, doc_identities, query

SITE = "bj-admin-ch-bj"


class BjSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture(SITE)

    def serve(self, url):
        if url.startswith("https://d-nsbc-p.admin.ch/v1/search"):
            return self.fx["listing"] if query(url).get("offset", "0") == "0" else self.fx["empty"]
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(2, len(expected))  # the 'x' row is skipped by crawl() (short abstract)
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
