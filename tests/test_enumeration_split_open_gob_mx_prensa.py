"""Enumeration split of crawler/sites/custom/gob-mx-prensa.py (18 gob-mx-* variants).

Hand-written minimal HTML only (no copied page content).
"""
import unittest

from crawler.enumeration.http import PermanentFailure
from crawler.sites import CRAWLERS

SITES = [
    "gob-mx-agricultura", "gob-mx-bienestar", "gob-mx-buengobierno", "gob-mx-defensa",
    "gob-mx-mujeres", "gob-mx-salud", "gob-mx-se", "gob-mx-sectur", "gob-mx-sedatu",
    "gob-mx-segob", "gob-mx-semar", "gob-mx-semarnat", "gob-mx-sep", "gob-mx-shcp",
    "gob-mx-sict", "gob-mx-sre", "gob-mx-sspc", "gob-mx-stps",
]

LIST_HTML = """<html><head><title>Prensa | Archivo | Secretaria de Prueba | gob.mx</title></head>
<body><div class="list">
<article><a href="/salud/prensa/101-primer-comunicado">Primer comunicado</a>
 <a href="/salud/prensa/101-primer-comunicado"><img src="x.png"></a></article>
<article><a href="https://www.gob.mx/salud/prensa/aviso-de-prueba-438701">Aviso</a></article>
<a href="/salud/documentos/no-es-prensa">doc</a>
<a href="/se/prensa/otra-dependencia">otra</a>
</div></body></html>"""

EMPTY_HTML = """<html><head><title>Prensa | Archivo | Secretaria de Prueba | gob.mx</title></head>
<body><div class="list"></div></body></html>"""

OTHER_HTML = "<html><head><title>Inicio | gob.mx</title></head><body></body></html>"

DETAIL_HTML = """<html><head><title>x</title>
<meta property="og:title" content="Comunicado de prueba sobre salud publica">
<meta property="og:description" content="Resumen corto.">
</head><body><main>
<h1>Comunicado de prueba sobre salud publica</h1>
<section class="small"><p>
  Secretaria de Prueba |
  24 de octubre de 2026
  <!-- dt></dt> -->
</p></section>
<article><p>Texto del comunicado de prueba, mas largo que el resumen.</p></article>
<div class='date-marque'><small>2026-09-09 14:58:00 -0600</small></div>
</main></body></html>"""

SHORT_TITLE_HTML = """<html><head><meta property="og:title" content="Corto"></head>
<body><h1>Corto</h1></body></html>"""


class GobMxPrensaSplit(unittest.TestCase):
    def crawler(self, site="gob-mx-salud"):
        return CRAWLERS[site](None)

    def test_all_variants_have_split_methods(self):
        for site in SITES:
            cls = CRAWLERS[site]
            self.assertTrue(callable(getattr(cls, "_enumeration_items", None)), site)
            self.assertTrue(callable(getattr(cls, "_enumeration_detail", None)), site)

    def test_items_match_crawl_extraction(self):
        c = self.crawler()
        rows = c._enumeration_items(LIST_HTML)
        self.assertEqual([r["path"] for r in rows], c._article_paths(LIST_HTML))
        self.assertEqual(
            [r["external_id"] for r in rows],
            ["101-primer-comunicado", "aviso-de-prueba-438701"],
        )
        self.assertEqual(
            rows[0]["url"], "https://www.gob.mx/salud/prensa/101-primer-comunicado?idiom=es"
        )

    def test_agency_scoped(self):
        rows = self.crawler("gob-mx-se")._enumeration_items(LIST_HTML)
        self.assertEqual([r["external_id"] for r in rows], ["otra-dependencia"])

    def test_empty_archive_page_and_foreign_page(self):
        c = self.crawler()
        self.assertEqual(c._enumeration_items(EMPTY_HTML), [])
        with self.assertRaises(PermanentFailure):
            c._enumeration_items(OTHER_HTML)

    def test_detail_paper(self):
        c = self.crawler()
        item = c._enumeration_items(LIST_HTML)[0]
        paper = c._enumeration_detail(DETAIL_HTML, item)
        self.assertEqual(paper["external_id"], "101-primer-comunicado")
        self.assertEqual(paper["url"], item["url"])
        self.assertEqual(paper["site_id"], "gob-mx-salud")
        self.assertEqual(paper["title"], "Comunicado de prueba sobre salud publica")
        self.assertEqual(paper["published_date"], "2026-10-24")
        self.assertIn("Texto del comunicado", paper["abstract"])
        self.assertEqual(paper["pdf_url"], "")

    def test_short_title_skipped(self):
        c = self.crawler()
        item = c._enumeration_items(LIST_HTML)[0]
        self.assertIsNone(c._enumeration_detail(SHORT_TITLE_HTML, item))


if __name__ == "__main__":
    unittest.main()
