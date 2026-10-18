import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "digital-gob-es-comunicacion"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
P = "/comunicacion/notas-prensa/secretaria-digitalizacion/2026/10/"


def card(slug, title):
    return f"""<div class="dnt-card"><div class="dnt-title"><a href="{P}{slug}">{title}</a></div>
<div class="dnt-date">14/10/2026</div><div class="dnt-text">Teaser de {title}.</div></div>"""


LIST1 = ("<div class='dnt-load-content'>" + card("nota-a", "Nota A") + card("nota-b", "Nota B") + "</div>")
EMPTY = "<div class='dnt-load-content'><div class='dnt-no-results'><p>No se ha encontrado ninguna noticia</p></div></div>"
MODELS = {
    "nota-a": {"title": "Nota A", "id": "m1", ":items": {"t": {":type": "portal/components/common/text",
               "text": "<p>" + "El Ministerio presenta nuevas medidas digitales para la ciudadanía. " * 3 + "</p>"},
               "d": {"datepicker": "2026-10-14"}}},
    "nota-b": {"title": "Nota B", ":items": {}},
}


class DigitalSplit(unittest.TestCase):
    def serve(self, url):
        if url.endswith(".model.json"):
            m = MODELS.get(url[:-len(".model.json")].rsplit("/", 1)[-1])
            return None if m is None else json.dumps(m)
        if P in url:
            return "<html><body><p>short</p></body></html>"  # crawl()'s HTML fallback
        if "content_display_copy.list.html" in url:
            return LIST1
        if "content_display_copy.list.p" in url:
            return EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_text"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_text"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
