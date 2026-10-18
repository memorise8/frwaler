import json
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "federalregister-gov-documents"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
AGENCIES = [{"name": "Health and Human Services Department", "slug": "health-and-human-services-department",
             "parent_id": None},
            {"name": "National Institutes of Health", "slug": "national-institutes-of-health", "parent_id": 221}]


def row(num, day, abstract, title="Notice"):
    return {"title": f"{title} {num}", "type": "Notice", "abstract": abstract, "excerpts": None,
            "document_number": num,
            "html_url": f"https://www.federalregister.gov/documents/{day.replace('-', '/')}/{num}/x",
            "pdf_url": f"https://www.govinfo.gov/content/pkg/FR-{day}/pdf/{num}.pdf",
            "public_inspection_pdf_url": None, "publication_date": day, "agencies": AGENCIES,
            "topics": ["Health"]}


ROWS = [row("2025-00003", "2025-03-02", "A sufficiently long abstract for the third notice, over fifty chars."),
        *[row(f"2025-0001{i}", "2025-02-01", "short") for i in range(7)],  # many skipped in a row
        row("2025-00001", "2025-01-05", "Another long abstract that is well beyond one hundred characters in length, "
                                         "so no supplement is appended to it at all.")]


class FederalRegisterSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        rows = ROWS
        lo, hi = q.get("conditions[publication_date][gte]"), q.get("conditions[publication_date][lte]")
        if lo:
            rows = [r for r in ROWS if lo <= r["publication_date"] <= hi]
        page = int(q.get("page", "1"))
        results = rows if page == 1 else []
        return {"count": len(rows), "total_pages": 1, "results": results}

    def serve_native(self, url, params=None, **kw):
        if params:
            url = url + "?" + urlencode(params)
        body = self.serve(url)

        class Resp:
            def json(self_inner):
                return body
        return Resp()

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve_native, net_methods=["_request"])
        self.assertEqual(2, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve_native, net_methods=["_request"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
