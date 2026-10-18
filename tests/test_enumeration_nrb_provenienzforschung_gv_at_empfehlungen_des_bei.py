import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = "provenienzforschung-gv-at-empfehlungen-des-bei"
B = "https://provenienzforschung.gv.at"
LIST = B + "/empfehlungen-des-beirats/beschluesse/beschluesse-alphabetisch/"
PAGE = ("""<html><body><table class="downloads">
<tr><th>Abeles Richard, Dr. (2004-01-27)</th><td><a download href="/beiratsbeschluesse/Abeles_Richard_2004-01-27.pdf">PDF</a></td></tr>
<tr><th>Kurz (2005-01-01)</th><td><a download href="/beiratsbeschluesse/Kurz_2005-01-01.pdf">PDF</a></td></tr>
<tr><th>ohne Link</th><td></td></tr></table></body></html>""").encode()
FILES = {B + "/beiratsbeschluesse/Abeles_Richard_2004-01-27.pdf":
         make_pdf("Der Kunstrueckgabebeirat hat in seiner Sitzung beschlossen dem Bundesminister die Rueckgabe zu empfehlen"),
         B + "/beiratsbeschluesse/Kurz_2005-01-01.pdf": make_pdf("kurz")}


class ProvSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == LIST:
            return PAGE
        return FILES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, module_funcs=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, types=lambda u: "application/pdf" if u.endswith(".pdf") else None)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, module_funcs=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
