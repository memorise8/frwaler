import json
import unittest
from pathlib import Path

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "mivau-gob-es-el-ministerio"
PLAN = json.loads((Path(__file__).resolve().parents[1] / "docs/examples/nr2b" / (SITE + ".json"))
                  .read_text(encoding="utf-8"))["sites"][SITE]
BASE = "/el-ministerio/sala-de-prensa/noticias/"


def li(slug, title, day):
    return f"""<li class='noticia'><time datetime="{day}T10:00:00Z">{day}</time>
<a class="stretched-link" href="{BASE}{slug}"><h2>{title}</h2></a>
<div class="cuerpo_noticia"><p>Teaser {title}</p></div></li>"""


PAGE = ("<html><body><div class='container-noticias'><ul class='listado_noticias'>"
        + li("mie-14102026-1124", "Nota A", "2026-10-14") + li("mie-14102026-0814", "Nota B", "2026-10-14")
        + "</ul></div></body></html>")
EMPTY = "<html><body><div class='container-noticias no_respuesta'><h2>Sin resultados</h2></div></body></html>"


def detail(node, title, body):
    return f"""<html><head><link rel="shortlink" href="/node/{node}">
<meta property="article:published_time" content="2026-10-14T10:00:00+02:00"></head>
<body><h1>{title}</h1><div class="detalle_noticia"><div class="cuerpo_noticia"><p>{body}</p></div></div>
<a href="/recursos_mfom/comodin/recursos/nota.pdf">PDF</a></body></html>"""


DETAILS = {"mie-14102026-1124": detail("173120", "Nota A", "El Ministerio de Vivienda informa de nuevas medidas. " * 3),
           "mie-14102026-0814": detail("173117", "Nota B", "corto")}


class MivauSplit(unittest.TestCase):
    def serve(self, url):
        if BASE in url:
            return DETAILS.get(url.rstrip("/").rsplit("/", 1)[-1])
        if "/el-ministerio/sala-de-prensa" in url:
            return PAGE if query(url).get("page", "0") == "0" else EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve, plan=PLAN)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
