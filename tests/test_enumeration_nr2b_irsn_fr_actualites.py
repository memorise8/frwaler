import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "irsn-fr-actualites"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]


def card(slug, title):
    return f"""<div class="irsn-related-card"><a class="irsn-related-card__link" href="/actualites/{slug}">
<div class="irsn-related-card__title">{title}</div></a><p class="fr-card__date">12/03/2025</p>
<p class="fr-tag">Communiqué</p></div>"""


PAGE0 = ("<html><body><div class='views-element-container'>" + card("note-a", "Note A") + card("note-b", "Note B")
         + "</div></body></html>")
EMPTY = "<html><body><div class='views-element-container'><p>Aucun résultat</p></div></body></html>"


def detail(body):
    return f"""<html><head><meta name="description" content="Résumé"></head><body>
<div class="content-container__date">12/03/2025</div><div class="irsn-richtext__content"><p>{body}</p></div>
<a href="/sites/default/files/2025-03/note.pdf">PDF</a><article data-history-node-id="42"></article></body></html>"""


DETAILS = {"note-a": detail("L'IRSN publie son bilan annuel sur la radioprotection des travailleurs. " * 3),
           "note-b": detail("court")}


class IrsnSplit(unittest.TestCase):
    def serve(self, url):
        if "/actualites/communiques-presse" in url:
            return PAGE0 if query(url).get("page", "0") == "0" else EMPTY
        if "/actualites/" in url:
            return DETAILS.get(url.rstrip("/").rsplit("/", 1)[-1])
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
