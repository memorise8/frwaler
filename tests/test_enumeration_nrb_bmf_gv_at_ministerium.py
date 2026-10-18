import subprocess
import tempfile
import unittest

from tests.nrb_helpers import native, pipeline, identities, doc_identities
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = "bmf-gv-at-ministerium"
START = ("https://www.bmf.gv.at/ministerium/nationale-finanzbildungsstrategie/"
         "uebersicht-nationale-finanzbildungsstrategie/downloads.html")
LONG = ("Wien, Mai 2025 Nationale Finanzbildungsstrategie Fortschrittsbericht mit vielen Seiten Text "
        "ueber Massnahmen, Ziele und Ergebnisse der Strategie im Berichtsjahr fuer alle Zielgruppen.")
J1 = "/dam/jcr:11111111-2222-3333-4444-555555555555/Fortschrittsbericht%202025.pdf"
J2 = "/dam/jcr:66666666-2222-3333-4444-555555555555/Kurz.pdf"
J3 = "/dam/jcr:77777777-2222-3333-4444-555555555555/Formular.docx"
PAGE = f"""<html><body><main>
<h3>Offizielle Dokumente</h3>
<ul><li><a href="{J1}">Fortschrittsbericht 2025 (PDF, 2 MB)</a></li>
<li><a href="{J2}">Kurzinfo (PDF, 1 KB)</a></li></ul>
<h3>Formulare</h3>
<ul><li><a href="{J3}">Formular zur Einmeldung (Word, 50 KB)</a></li>
<li><a href="{J1}">Fortschrittsbericht 2025 doppelt</a></li></ul>
</main></body></html>"""
FILES = {"https://www.bmf.gv.at" + J1: make_pdf(LONG), "https://www.bmf.gv.at" + J2: make_pdf("kurz"),
         "https://www.bmf.gv.at" + J3: b"PK\x03\x04 not a pdf " + b"x" * 300}


def pdftext(body):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(body); fh.flush()
        r = subprocess.run(["pdftotext", "-q", fh.name, "-"], capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", errors="replace").strip()


class BmfSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == START:
            return PAGE  # the page ignores ?page=N (one-page listing: page 2 wraps to page 1)
        return FILES.get(url)

    def init(self, crawler):
        crawler._extract_pdf_text = lambda url: pdftext(FILES[url]) if len(FILES[url]) > 200 else ""

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get_text"], init=self.init)
        self.assertEqual(2, len(expected))  # long PDF + docx; short PDF skipped (abstract < 100)
        result, docs = pipeline(SITE, self.serve,
                                types=lambda u: "application/pdf" if u.endswith(".pdf") else None)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(identities(expected), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
