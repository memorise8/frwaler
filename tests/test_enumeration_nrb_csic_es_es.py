import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "csic-es-es"
B = "https://www.csic.es"
START = B + "/es/el-csic/informacion-corporativa/el-csic-en-cifras"
STATIC = ('<h2>DATOS MÁS DESTACADOS DEL CSIC (2004-2010)</h2><h3>2010</h3>'
          '<p><a href="/sites/default/files/2024-03/Datos_2010.pdf">Datos 2010</a></p>')


def page(rows, nxt):
    v = "".join(f'<div class="views-row"><a href="/sites/default/files/{r}.pdf">Institutos y Centros datos {r[-4:]}</a></div>'
                for r in rows)
    pager = '<a rel="next" href="?page=1">Siguiente</a>' if nxt else ""
    return f"<html><body><main>{v}{pager}{STATIC}</main></body></html>"


PAGES = {0: page(["datos-2025", "datos-2024"], True), 1: page(["datos-2013"], False)}


class CsicSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == START:
            p = int(query(url).get("page", "0"))
            return PAGES.get(p, page([], False))
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"])
        self.assertGreaterEqual(len(expected), 3)
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
