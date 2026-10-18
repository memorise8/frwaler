import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "transportes-gob-es-el-ministerio"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
LIST = "/ministerio/comunicacion/sala-prensa"


def row(slug, node, title):
    return f"""<div class="views-row"><article id="node-{node}-teaser"><a href="{LIST}/{slug}">{title}</a>
<time datetime="2026-10-16T12:00:00Z">16/10/2026</time>
<div class="field--name-field-summary">Resumen breve</div></article></div>"""


PAGE0 = ("<html><body><div class='view-content'>" + row("vie-16102026-1318", "901", "Nota A")
         + row("vie-16102026-1310", "900", "Nota B") + "</div></body></html>")
EMPTY = "<html><body><div class='view-empty'>Sin resultados</div></body></html>"


def detail(body):
    return f"""<html><body><div class="node__content"><p>{body}</p>
<a href="/recursos_mfom/nota.pdf">PDF</a></div></body></html>"""


DETAILS = {"vie-16102026-1318": detail("El Ministerio de Transportes informa de nuevas inversiones. " * 3),
           "vie-16102026-1310": detail("corto")}


class TransportesSplit(unittest.TestCase):
    def serve(self, url):
        if "/views/ajax" in url:
            return None
        if LIST + "/" in url:
            return DETAILS.get(url.rstrip("/").rsplit("/", 1)[-1])
        if LIST in url:
            return PAGE0 if query(url).get("page", "0") == "0" else EMPTY
        return None

    def serve_bytes(self, url, *a, **k):
        body = self.serve(url)
        return None if body is None else body.encode("utf-8")

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve_bytes, module_funcs=["_curl"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve_bytes, module_funcs=["_curl"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
