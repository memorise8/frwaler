import unittest
from urllib.parse import unquote

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities

SITE = "sib-swiss-community"
START = "https://www.sib.swiss/community/publications/scientific-publications-2025"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE = """<html><body><main><ul>
<li>Muster A, Beispiel B. <a href="https://doi.org/10.1093/nar/gkae001">A database of protein families</a>. <em>Nucleic Acids Res</em> 2025.</li>
<li>Doe J. <a href="https://doi.org/10.1101/2025.01.01.000001">A preprint without abstract</a>. <em>bioRxiv</em></li>
<li>Muster A. <a href="https://doi.org/10.1093/nar/gkae001">duplicate</a></li>
</ul></main></body></html>"""
ABS = "We present a curated database of protein families with annotations for thousands of species and tools."
EP = {"10.1093/nar/gkae001": {"resultList": {"result": [{"abstractText": ABS, "firstPublicationDate": "2025-01-05",
                                                         "authorString": "Muster A, Beispiel B", "pmid": "1",
                                                         "journalInfo": {"journal": {"title": "NAR"}},
                                                         "authorList": {"author": [{"fullName": "Muster A"}]}}]}},
      "10.1101/2025.01.01.000001": {"resultList": {"result": []}}}


class SibSplit(unittest.TestCase):
    def serve(self, url):
        if url.split("?")[0] == START:
            return PAGE
        if url.startswith(EPMC):
            doi = unquote(url.split("query=")[1].split("&")[0])[4:]
            return EP.get(doi)
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve)
        self.assertIn(result["status"], ("complete", "complete_with_review", "complete_unverified"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
