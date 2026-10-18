import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "indicators-be-nl"
B = "https://indicators.be"
INDEX = """<html><body>
<a href="/nl/i/G01_PSE/Risico_op_armoede_%28i01%29">i01</a>
<a href="/nl/i/G01_CPO/Kinderarmoede_%28i02%29">i02</a>
<a href="/nl/i/G01_PSE/Risico_op_armoede_%28i01%29">dup</a>
<a href="/nl/t/SDG/">SDG</a></body></html>"""
TEXT = ("Het risico op armoede of sociale uitsluiting meet het aandeel van de bevolking dat onder de "
        "armoedegrens leeft of ernstige materiële deprivatie ervaart.")


def detail(title, text):
    return (f'<html><body><div class="col-md-9"><h4><i></i>{title}</h4><ul class="blog-info"><li>31/10/2025</li></ul>'
            f'<p>{text}</p></div></body></html>')


PAGES = {B + "/nl/i/G01_PSE/Risico_op_armoede_%28i01%29": detail("Risico op armoede (i01)", TEXT),
         B + "/nl/i/G01_CPO/Kinderarmoede_%28i02%29": detail("Kinderarmoede (i02)", "kort")}


class IndicatorsSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == B + "/nl/t/SDG/":
            return INDEX
        return PAGES.get(url)

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
