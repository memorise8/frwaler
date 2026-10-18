import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "pubshop-bmbwf-gv-at-indexphp"
B = "https://pubshop.bmbwf.gv.at"


def el(pub, title):
    return (f'<div class="pubListElement"><div class="pubDetails"><div class="pubElementTitle">'
            f'<a href="index.php?article_id=9&type=gesamtkatalog&pub={pub}">{title}</a></div></div></div>')


LIST = '<html><body><div class="pubList">' + el(1070, "Deutsch FAIRnetzt") + el(1069, "Lesen Digital") + "</div></body></html>"
EMPTY = '<html><body><div class="pubList"></div></body></html>'
DESC = "Eine Handreichung fuer Lehrkraefte zur Leseförderung im digitalisierten Unterricht mit Beispielen."


def detail(title, desc):
    return (f'<html><body><div class="pubTitle">{title}</div><div class="pubDetails">'
            f'<p><strong>Beschreibung:</strong> {desc}</p><p>Erscheinungsjahr: 2023<br/>Medientyp: Broschüre</p></div>'
            f'<div class="pubDownload"><a href="/media/x.pdf">PDF</a></div></body></html>')


DETAILS = {"1070": detail("Deutsch FAIRnetzt", DESC), "1069": detail("Lesen Digital", "kurz")}


class PubshopSplit(unittest.TestCase):
    def serve(self, url):
        q = query(url)
        if url.startswith(B + "/index.php") and q.get("article_id") == "1":
            return LIST if q.get("offset", "0") == "0" else EMPTY
        if q.get("pub") in DETAILS:
            return DETAILS[q["pub"]]
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
