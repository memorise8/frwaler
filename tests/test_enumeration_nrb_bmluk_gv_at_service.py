import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "bmluk-gv-at-service"
START = "https://www.bmluk.gv.at/service/zahlen-fakten-neu/statistik-agrarmarkt.html"
PAGE = """<html><body><main><div class="richtext_output">
<h2>Marktdaten</h2><ul>
<li><abbr title="Agrarmarkt Austria">AMA</abbr> - <a href="https://www.ama.at/media/1jmhgr20/dashboard_apfel_aug_2025.pdf">Dashboard Apfel August 2025</a></li>
<li><a href="https://www.ama.at/marktinformationen/">Marktinformationen (Startseite)</a></li>
<li>Börse - <a href="https://www.boersewien.at/kursblatt+2400++1119134">Kursblatt</a></li>
<li><a href="https://www.ama.at/media/1jmhgr20/dashboard_apfel_aug_2025.pdf">Dashboard Apfel doppelt</a></li>
</ul><h3>Eier</h3><ul><li><a href="https://www.ama.at/media/4ncjdsll/dashboard_eier_vom_2025_10_08.pdf">Eier 2025</a></li></ul>
</div></main></body></html>"""


class BmlukSplit(unittest.TestCase):
    def serve(self, url):
        return PAGE if url.split("?")[0] == START else None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"])
        self.assertEqual(3, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"])), doc_identities(docs))

    def test_publication_link_stops_instead_of_saving_another_paper(self):
        from crawler.enumeration.http import PermanentFailure
        from tests.collector_split_helpers import load
        crawler, _ = load(SITE)
        page = PAGE.replace("https://www.boersewien.at/kursblatt+2400++1119134",
                            "https://www.bmluk.gv.at/service/publikationen/land/x.html")
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(page)


if __name__ == "__main__":
    unittest.main()
