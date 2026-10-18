import unittest

from tests.nrb_helpers import native, native_docs, pipeline, doc_identities, query

SITE = "edoc-rki-de-handle"
B = "https://edoc.rki.de"
DISC = B + "/handle/176904/2/discover"
LIST = """<html><body><div id="aspect_discovery_SimpleSearch_div_search-results">
<div class="pagination-info">Zeige 1-2 von 2</div>
<div class="ds-artifact-item"><a href="/handle/176904/13919"><span class="artifact-title">Bekanntmachung A</span></a>
<span class="badge-date"><span class="date">2025-03-01</span></span></div>
<div class="ds-artifact-item"><a href="/handle/176904/13780"><span class="artifact-title">Bekanntmachung B</span></a></div>
<div class="ds-artifact-item"><a href="/handle/176904/2">collection</a></div>
</div></body></html>"""
EMPTY = '<html><body><div id="aspect_discovery_SimpleSearch_div_search-results"></div></body></html>'
LONG = ("Diese Bekanntmachung beschreibt die Empfehlungen der Kommission fuer Krankenhaushygiene und "
        "Infektionspraevention zu Anforderungen an die Hygiene bei der Reinigung von Flaechen.")


def mets(abstract):
    ab = f'<dim:field mdschema="dc" element="description" qualifier="abstract">{abstract}</dim:field>' if abstract else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><mets:METS xmlns:mets="http://www.loc.gov/METS/" '
            'xmlns:dim="http://www.dspace.org/xmlns/dspace/dim" xmlns:xlink="http://www.w3.org/TR/xlink/">'
            '<dim:dim><dim:field mdschema="dc" element="title">Titel</dim:field>'
            '<dim:field mdschema="dc" element="date" qualifier="issued">2025-03-01</dim:field>'
            f'{ab}</dim:dim></mets:METS>')


METS = {"176904/13919": mets(LONG), "176904/13780": mets("")}


class EdocSplit(unittest.TestCase):
    def serve(self, url):
        if url.startswith(DISC):
            return LIST if query(url).get("page", "1") == "1" else EMPTY
        for h, body in METS.items():
            if url.startswith(f"{B}/metadata/handle/{h}/mets.xml"):
                return body
        return None

    def test_plan_stores_what_crawl_saves(self):
        expected = native(SITE, self.serve, net_methods=["_curl_get"])
        self.assertEqual(1, len(expected))
        result, docs = pipeline(SITE, self.serve, types=lambda u: "text/xml; charset=utf-8" if "mets.xml" in u else None)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        self.assertEqual(doc_identities(native_docs(SITE, self.serve, net_methods=["_curl_get"])), doc_identities(docs))


if __name__ == "__main__":
    unittest.main()
