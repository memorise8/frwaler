"""swisspolar-ch-expeditions: WP page listing + Crossref works detail (api.crossref.org)."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure
from tests.api_split_helpers import (api_plan, crawl_sha, html_response, identities, json_response, native,
                                     pipeline, run_e2e)
from tests.collector_split_helpers import load, wp_past_last_page

SITE = "swisspolar-ch-expeditions"
LONG = "Sea ice and ocean observations from the Antarctic Circumnavigation Expedition " * 3
CONTENT = (
    '<div class="entry-content"><h3>Project: Ocean biogeochemistry</h3>'
    '<p>Smith, J., Doe, A. (2019). <a href="https://doi.org/10.1000/abc.1">First paper title</a>. <em>Journal A</em>.</p>'
    '<p>Roe, B. (2020). <a href="https://doi.org/10.1000/abc.2">Second <i>paper</i></a> <em>Journal B</em>.</p>'
    '<p>Dup, C. (2019). <a href="https://doi.org/10.1000/abc.1">First paper title</a>. <em>Journal A</em>.</p>'
    '<p>Nodoi, D. (2021). <a href="https://example.org/report.pdf">A report without DOI</a>.</p>'
    '<p>Short, E. (2022). <a href="https://doi.org/10.1000/abc.3">Short abstract paper</a>.</p>'
    '<p>Last updated on 12 March 2026</p></div>'
)
WP = [{"id": 4242, "date": "2020-01-01T00:00:00", "date_gmt": "2020-01-01T00:00:00",
       "modified": "2026-03-12T10:00:00", "modified_gmt": "2026-03-12T09:00:00", "slug": "ace-publications",
       "link": "https://swisspolar.ch/expeditions/ace/ace-publications/", "title": {"rendered": "ACE"},
       "content": {"rendered": CONTENT}}]


def crossref(doi, abstract, extra=None):
    msg = {"DOI": doi.upper() if doi.endswith(".2") else doi, "title": ["Crossref " + doi],
           "abstract": "<jats:p>" + abstract + "</jats:p>", "published": {"date-parts": [[2019, 5, 2]]},
           "author": [{"given": "Jane", "family": "Smith"}, {"name": "ACE Consortium"}],
           "publisher": "Pub", "container-title": ["Journal A"], "subject": ["Oceanography"],
           "link": [{"URL": "https://pub.example/" + doi.rsplit(".", 1)[-1] + ".pdf", "content-type": "application/pdf"}],
           "volume": "12", "issue": "3", "page": "1-9", "type": "journal-article", "URL": "https://doi.org/" + doi}
    msg.update(extra or {})
    return {"status": "ok", "message": msg}


CROSSREF = {
    "https://api.crossref.org/works/10.1000%2Fabc.1": crossref("10.1000/abc.1", LONG),
    "https://api.crossref.org/works/10.1000%2Fabc.2": crossref("10.1000/abc.2", LONG, {"published": None,
                                                                                      "created": {"date-parts": [[2020, 2]]}}),
    "https://api.crossref.org/works/10.1000%2Fabc.3": crossref("10.1000/abc.3", "Too short."),
}
LIST_URL = ("https://swisspolar.ch/wp-json/wp/v2/pages?slug=ace-publications&per_page=1"
            "&_fields=id%2Cdate%2Cdate_gmt%2Cmodified%2Cmodified_gmt%2Cslug%2Clink%2Ctitle%2Ccontent&page=1")


def body_for(url, **kw):
    if "wp-json" in url:
        return json.dumps(WP) if "page=1" in url else json.dumps([])
    if url in CROSSREF:
        return json.dumps(CROSSREF[url])
    raise AssertionError("unexpected request " + url)


def handler(url):
    if "wp-json" in url:
        return json_response(url, WP) if "page=1" in url or "page=" not in url else wp_past_last_page(url)
    if url in CROSSREF:
        return json_response(url, CROSSREF[url])
    raise AssertionError("unexpected request " + url)


class SwissPolarApiSplit(unittest.TestCase):
    def test_crawl_is_unchanged(self):
        # integration-20261013 crawl() source
        self.assertEqual("8cfd2876e7b4f9784c3ed4e7561db44c6b2f599df8b9a4351d3082996c098e66", crawl_sha(SITE))

    def test_papers_equal_original_crawl(self):
        expected = native(SITE, body_for, module_patches=["_curl_get", "_curl_head_filename"])
        got, items, fetcher = pipeline(SITE, [LIST_URL], handler)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], got)
        self.assertEqual(identities(expected), identities(got))
        # Non-DOI rows are never listed (crawl() always skips them: no detail, empty abstract);
        # the repeated DOI is listed once.
        self.assertEqual(["10.1000/abc.1", "10.1000/abc.2", "10.1000/abc.3"], [i.key for i in items])
        self.assertTrue(all(u.startswith(("https://swisspolar.ch/", "https://api.crossref.org/works/"))
                            for u in fetcher.urls))

    def test_crossref_error_record_is_skipped_like_crawl(self):
        bad = dict(CROSSREF)
        bad["https://api.crossref.org/works/10.1000%2Fabc.1"] = {"status": "error", "message": "Resource not found."}
        def h(url):
            return json_response(url, bad[url]) if url in bad else handler(url)
        def b(url, **kw):
            return json.dumps(bad[url]) if url in bad else body_for(url)
        expected = native(SITE, b, module_patches=["_curl_get", "_curl_head_filename"])
        got, _, _ = pipeline(SITE, [LIST_URL], h)
        self.assertEqual([clean_paper(p) for p in expected], got)
        self.assertEqual(1, len(got))

    def test_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({"code": "rest_no_route"}, LIST_URL)
        self.assertEqual([], crawler._enumeration_items([], LIST_URL))

    def test_shipped_plan_end_to_end(self):
        result, docs = run_e2e(SITE, handler)
        self.assertIn(result["status"], ("complete", "complete_with_review"), result)
        # post_number = external_id = Crossref's DOI spelling (same rule as crawl())
        self.assertEqual(["10.1000/ABC.2", "10.1000/abc.1"], sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
