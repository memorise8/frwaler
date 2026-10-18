"""science-astron-nl-science-astron: OpenAlex works listing (api.openalex.org), paper from the row."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure
from tests.api_split_helpers import crawl_sha, identities, json_response, native, pipeline, query, run_e2e
from tests.collector_split_helpers import load

SITE = "science-astron-nl-science-astron"
INV = {w: [i] for i, w in enumerate(("ABSTRACT Radio observations of pulsars with LOFAR reveal "
                                     "scattering and dispersion variations over many years").split())}


def work(n, *, abstract=INV, title="Work", doi=True, landing=True, date="2024-05-01"):
    return {"id": f"https://openalex.org/W{n}", "title": f"{title} {n}" if title else None,
            "abstract_inverted_index": abstract, "doi": f"https://doi.org/10.1/{n}" if doi else None,
            "publication_date": date, "type": "article",
            "authorships": [{"author": {"display_name": "A. Author"}}, {"author": {"display_name": " "}}],
            "primary_location": {"source": {"display_name": "A&A"},
                                 "landing_page_url": f"https://doi.org/10.1/{n}" if landing else None,
                                 "pdf_url": f"https://pub.example/{n}.pdf" if n % 2 else None}}


RESULTS = [work(1), work(2, landing=False), work(3, abstract={"short": [0]}), work(4, title=None),
           work(1), work(5, date="")]
PAGE = {"meta": {"count": len(RESULTS), "next_cursor": None}, "results": RESULTS}
EMPTY = {"meta": {"count": len(RESULTS), "next_cursor": None}, "results": []}


def body_for(url, **kw):
    return json.dumps(PAGE)


def handler(url):
    return json_response(url, PAGE if query(url, "page", "1") == "1" else EMPTY)


class AstronApiSplit(unittest.TestCase):
    def test_crawl_is_unchanged(self):
        self.assertEqual("65b908acf823b57366309c237b50a2f2c0365c19dbffa7f1850e7bba5df20a1c", crawl_sha(SITE))

    def test_papers_equal_original_crawl(self):
        expected = native(SITE, body_for, method_patches=["_curl_get"])
        got, items, fetcher = pipeline(SITE, ["https://api.openalex.org/works?page=1"], handler)
        self.assertEqual(3, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], got)
        self.assertEqual(identities(expected), identities(got))
        self.assertEqual(1, len(fetcher.urls))  # no detail requests

    def test_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({"error": "Invalid query"})
        self.assertEqual([], crawler._enumeration_items(EMPTY))

    def test_shipped_plan_end_to_end(self):
        seg = {"mode": "explicit", "items": [{"key": "2024", "params": {
            "filter": "institutions.id:I922237871,from_publication_date:2024-01-01,to_publication_date:2024-12-31"}}]}
        unique = {"meta": {"count": 5, "next_cursor": None}, "results": [w for i, w in enumerate(RESULTS) if i != 4]}
        def h(url):
            return json_response(url, unique if query(url, "page", "1") == "1" else dict(EMPTY, meta={"count": 5}))
        result, docs = run_e2e(SITE, h, plan_override={"segments": seg})
        # listed 5 = reported 5 per segment and overall; untitled/short-abstract works are review items
        self.assertEqual("complete_with_review", result["status"], result)
        self.assertEqual(["W1", "W2", "W5"], sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
