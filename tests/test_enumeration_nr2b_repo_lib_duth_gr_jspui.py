import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "repo-lib-duth-gr-jspui"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
LIST = """<html><body><main id="content"><table class="table">
<tr><td><a href="/jspui/handle/123456789/501">Thesis A</a></td></tr>
<tr><td><a href="/jspui/handle/123456789/500">Thesis B</a></td></tr>
<tr><td><a href="/jspui/handle/123456789/501">Thesis A again</a></td></tr>
</table></main></body></html>"""
EMPTY = """<html><body><main id="content"><div class="row service-wrapper-row">
<h1>No Entries in Index</h1></div></main></body></html>"""


def detail(title, abstract, year):
    return f"""<html><head><meta name="citation_pdf_url" content="https://repo.lib.duth.gr/jspui/bitstream/x/{year}.pdf">
</head><body><table>
<tr><td class="metadataFieldLabel">dc.title</td><td class="metadataFieldValue">{title}</td></tr>
<tr><td class="metadataFieldLabel">heal.abstract</td><td class="metadataFieldValue">{abstract}</td></tr>
<tr><td class="metadataFieldLabel">heal.publicationDate</td><td class="metadataFieldValue">{year}</td></tr>
<tr><td class="metadataFieldLabel">dc.contributor.author</td><td class="metadataFieldValue">Author, A.</td></tr>
</table></body></html>"""


DETAILS = {
    "123456789/501": detail("Thesis A", "A long enough abstract about thesis A. " * 4, "2024"),
    "123456789/500": detail("Thesis B", "too short", "2023"),
}


class DuthSplit(unittest.TestCase):
    def serve(self, url):
        if "/jspui/browse" in url:
            return LIST if query(url).get("offset", "0") == "0" else EMPTY
        for handle, body in DETAILS.items():
            if f"/jspui/handle/{handle}?mode=full" in url:
                return body
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
