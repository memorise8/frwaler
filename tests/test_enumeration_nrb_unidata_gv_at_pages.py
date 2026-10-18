import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "unidata-gv-at-pages"
B = "https://unidata.gv.at"


def card(name, title, date):
    return (f'<a class="publication-card" href="/assets/files/publications/{name}.pdf" title="{title}">'
            f'<h3 class="headline">{title}</h3><span class="date">{date}</span><span class="size">2 MB</span></a>')


P1 = ("<html><body><main>" + card("Wissensbilanz-2025-Uni-Wien", "Wissensbilanz 2025 Universität Wien", "01.10.2026")
      + card("LV-Ergaenzung", "Leistungsvereinbarung Ergänzung", "29.09.2026")
      + '<a class="pagination-link" href="/publikationen/p2">2</a></main></body></html>')
P2 = "<html><body><main>" + card("Wissensbilanz-2024-Uni-Graz", "Wissensbilanz 2024 Universität Graz", "01.10.2025") + "</main></body></html>"
EMPTY = "<html><body><main></main></body></html>"


class UnidataSplit(unittest.TestCase):
    def serve(self, url):
        u = url.split("?")[0]
        if u == B + "/publikationen":
            return P1
        if u == B + "/publikationen/p2":
            return P2
        if u.startswith(B + "/publikationen/p"):
            return EMPTY
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_text"])
        self.assertGreaterEqual(len(expected), 1)
        result, docs = pipeline(SITE, self.serve)
        self.assertTrue(result["status"].startswith("complete"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_text"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
