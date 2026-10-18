import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "gov-wales-publications"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def li(slug, title):
    return f"""<div class="index-list__item"><div class="index-list__title"><a href="/{slug}">{title}</a></div>
<div class="index-list__date"><time datetime="2026-10-16T12:00:00Z">16 October 2026</time></div>
<div class="index-list__type">Guidance</div></div>"""


PAGE0 = ("<html><body><div class='views-element-container'>" + li("doc-a", "Doc A") + li("doc-b", "Doc B")
         + "</div></body></html>")
EMPTY = "<html><body><div class='views-element-container'><p>No results</p></div></body></html>"


def detail(title, summary):
    return f"""<html><body><main><h1 class="page-header__title">{title}</h1>
<div class="hero-block__summary"><p>{summary}</p></div>
<div class="first-published"><span class="item">16 October 2026</span></div>
<div class="document"><span class="document__type">PDF</span><a href="/sites/default/files/a.pdf">Doc</a></div>
</main></body></html>"""


DETAILS = {"doc-a": detail("Doc A", "Guidance for local authorities on bathing water designation in Wales. " * 2),
           "doc-b": detail("Doc B", "Short.")}


class GovWalesSplit(unittest.TestCase):
    def serve(self, url):
        if "/publications" in url:
            return PAGE0 if query(url).get("page", "0") == "0" else EMPTY
        return DETAILS.get(url.rstrip("/").rsplit("/", 1)[-1])

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
