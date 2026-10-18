import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "state-gov-press-releases"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def post(i, body):
    return {"id": i, "slug": f"r{i}", "title": {"rendered": f"Release {i}"}, "date": "2026-10-16T10:00:00",
            "modified": "2026-10-16T11:00:00", "link": f"https://www.state.gov/releases/r{i}/",
            "content": {"rendered": f"<p>{body}</p>"}, "excerpt": {"rendered": ""},
            "acf": {"document_date": "2026-10-16"},
            "class_list": ["state_bureaus-office-of-the-spokesman", "state_document_type-readout"]}


POSTS = [post(2, "Secretary met the foreign minister to discuss regional cooperation and trade. " * 2), post(1, "short")]
PAST = {"code": "rest_post_invalid_page_number", "message": "x", "data": {"status": 400}}


class StatePressSplit(unittest.TestCase):
    def serve(self, url):
        return POSTS if query(url).get("page", "1") == "1" else None

    def init(self, crawler):
        crawler._fetch_page = lambda page: (POSTS, 1) if page == 1 else ([], 0)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
