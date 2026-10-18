import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "homeaffairs-gov-au-sitesearch"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def row(doc_id, path, summary):
    cells = {"Title": f"Doc {doc_id}", "Path": path, "DocId": doc_id, "ListItemID": "7",
             "HitHighlightedSummary": summary, "Description": summary, "Write": "2025-03-01T00:00:00Z",
             "FileExtension": "pdf", "Author": "Home Affairs"}
    return {"Cells": [{"Key": k, "Value": v} for k, v in cells.items()]}


ROWS = [row("70219", "https://www.homeaffairs.gov.au/files/a.pdf",
            "The Department of Home Affairs certification of the campaign step together for community safety. " * 2),
        row("5458", "https://www.homeaffairs.gov.au/files/b.pdf", "short")]


def page(rows, total):
    return {"PrimaryQueryResult": {"RelevantResults": {"TotalRows": total, "RowCount": len(rows),
                                                       "Table": {"Rows": rows}}}}


class HomeAffairsSplit(unittest.TestCase):
    def serve(self, url):
        start = int(query(url).get("startrow", "0"))
        return page(ROWS if start == 0 else [], 2)

    def init(self, crawler):
        def fetch(start_row):
            data = self.serve(f"https://x/?startrow={start_row}")
            rows = data["PrimaryQueryResult"]["RelevantResults"]["Table"]["Rows"]
            return [{c["Key"]: c["Value"] for c in r["Cells"]} for r in rows], 2
        crawler._fetch_search_page = fetch

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, init=self.init)
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, init=self.init)), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
