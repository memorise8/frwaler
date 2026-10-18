import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "bmlv-gv-at-wissen-forschung"
BASE = "https://www.bmlv.gv.at/wissen-forschung/publikationen"
LIST = BASE + "/doktyp.php?id=14"
PAD = "x" * 200


def pub(pid, title, beitrag=None, extra=""):
    link = f'<a href="beitrag.php?id={beitrag}">Beitrag</a>' if beitrag else ""
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<meta property="books:release_date" content="2024-05-01"></head><body><div id="content">'
            f'<h2>{title}</h2><p>Untertitel der Publikation {pid} mit genug Text fuer eine Beschreibung.</p>'
            f'<div class="floatbox">Dokumenttyp: Bericht Erscheinungsdatum: 05.2024 Herausgeber: BMLV '
            f'Seiten: 120 Autor: <a href="person.php?id=1">Max Muster</a></div>{link}'
            f'<a href="/pdf_pool/publikationen/p{pid}.pdf">PDF</a>{extra}</div><!-- {PAD} --></body></html>').encode("latin-1")


BODIES = {
    LIST: (f'<html><body><div id="content"><a href="publikation.php?id=411">A</a>'
           f'<a href="publikation.php?id=400">B</a><a href="publikation.php?id=411">A</a>'
           f'<!-- <a href="publikation.php?id=999">hidden</a> --> {PAD}</div></body></html>').encode(),
    BASE + "/publikation.php?id=411": pub(411, "Sicherheitspolitische Jahresvorschau", beitrag=77),
    BASE + "/publikation.php?id=400": pub(400, "Strategie und Sicherheit"),
    BASE + "/beitrag.php?id=77": (f'<html><body><div id="content"><div class="floatbox">'
                                  f'<a href="thema.php?id=1">Sicherheit</a><a href="region.php?id=2">Europa</a></div>'
                                  f'<a href="schlagwortsuche.php?sw=NATO">NATO</a> {PAD}</div></body></html>').encode(),
}


class BmlvSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?page=")[0].split("&page=")[0] == LIST:
            return BODIES[LIST]
        return BODIES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(2, len(expected))
        self.assertIn("Themen: Sicherheit", expected[0]["abstract"])  # from the beitrag page
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])),
                         doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
