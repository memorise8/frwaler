import unittest

from tests.collector_split_helpers import load
from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "bmwet-gv-at-services"
B = "https://www.bmwet.gv.at"
INDEX = B + "/Services/Publikationen.html"
PAGE = """<html><body><div id="content">
<div class="card-collapse"><h3><button>Allgemeine Wirtschaftspolitik</button></h3><ul>
<li><a href="/Services/Publikationen/allgemeine-wirtschaftspolitik/Insolvenzueberhang.html">Insolvenzüberhang</a></li>
<li><a href="/Services/Publikationen/allgemeine-wirtschaftspolitik/Kurz.html">Kurz</a></li>
<li><a href="/dam/jcr:1/x.pdf">PDF <span class="fileinfo">(PDF, 1 MB)</span></a></li>
</ul></div></div></body></html>"""
TEXT = ("Die Studie untersucht den Insolvenzüberhang in Österreich nach der Pandemie und die Auswirkungen "
        "staatlicher Hilfen auf die Unternehmensdynamik.")


def detail(title, text):
    return (f'<html><body><div id="content"><h1>{title}</h1><p>{text}</p>'
            f'<a href="/dam/jcr:abc/Studie.pdf">Studie (PDF)</a></div></body></html>')


PAGES = {B + "/Services/Publikationen/allgemeine-wirtschaftspolitik/Insolvenzueberhang.html": detail("Insolvenzüberhang", TEXT),
         B + "/Services/Publikationen/allgemeine-wirtschaftspolitik/Kurz.html": detail("Kurz", "x")}


class BmwetSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == INDEX:
            return PAGE
        return PAGES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"])), doc_identities(docs))

    def test_pdf_link_skips_cover_image(self):
        crawler, _ = load(SITE)
        page = B + "/Services/Publikationen/energie/Bauplan.html"
        html = (f'<html><body><div id="content"><h1>Bauplan</h1><p>{TEXT}</p>'
                '<a href="/dam/jcr:07/Bauplan_Energiewende-titel.jpg"><img src="x.jpg"></a>'
                '<a href="/dam/jcr:07/Bauplan_Energiewende.pdf">Bauplan (PDF)</a></div></body></html>')
        paper = crawler._parse_detail_page(html, page)
        self.assertEqual(B + "/dam/jcr:07/Bauplan_Energiewende.pdf", paper["pdf_url"])
        self.assertEqual("Bauplan_Energiewende.pdf", paper["pdf_filename"])
        only_image = html.replace('<a href="/dam/jcr:07/Bauplan_Energiewende.pdf">Bauplan (PDF)</a>', "")
        self.assertIsNone(crawler._parse_detail_page(only_image, page)["pdf_url"])


if __name__ == "__main__":
    unittest.main()
