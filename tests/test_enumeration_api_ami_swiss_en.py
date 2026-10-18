"""ami-swiss-en: year pages (?year=YYYY) + Crossref works detail (api.crossref.org)."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure
from tests.api_split_helpers import (crawl_sha, html_response, identities, json_response, native, pipeline,
                                     query, run_e2e)
from tests.collector_split_helpers import load

SITE = "ami-swiss-en"
BASE = "https://www.ami.swiss/en/research/publications.html"
LONG = "Polymer mechanochemistry enables stress sensing in soft materials and composites. " * 2
FORM = '<form><input type="radio" name="year" value="2025"/><input type="radio" name="year" value="2024"/></form>'


def box(doi, title, authors="A. One, B. Two", journal="Soft Matter", year="2025"):
    return (f'<div class="box"><a href="https://doi.org/{doi}"><span>{title}</span><span>{authors}</span>'
            f'<span><b>{journal}</b> {year}, 12, 3-4</span></a></div>')


PAGES = {
    "2025": '<html><body>' + FORM + '<article class="bg-white">' + box("10.1/a", "Paper A") + box("10.1/b", "Paper B")
            + box("10.1/a", "Paper A") + box("10.1/c", "") + '</article></body></html>',
    "2024": '<html><body>' + FORM + '<article class="bg-white">' + box("10.1/d", "Paper D", year="2024")
            + box("10.1/e", "Paper E short", year="2024") + '</article></body></html>',
    "2023": '<html><body>' + FORM + '<article class="bg-white"><p>No publications.</p></article></body></html>',
}


def cr(doi, abstract, **extra):
    msg = {"DOI": doi.upper(), "abstract": "<jats:p>" + abstract + "</jats:p>",
           "published": {"date-parts": [[2025, 3, 7]]}, "author": [{"given": "Ann", "family": "One"}, {"family": "Two"}],
           "container-title": ["Soft Matter &amp; Co"], "publisher": "RSC", "volume": "12", "issue": "4",
           "subject": ["Chemistry"], "type": "journal-article",
           "link": [{"URL": "https://pub.example/x.pdf", "content-type": "application/pdf"}],
           "created": {"date-parts": [[2025, 1, 1]]}}
    msg.update(extra)
    return {"status": "ok", "message": msg}


CROSSREF = {
    "https://api.crossref.org/works/10.1/a": cr("10.1/a", LONG),
    "https://api.crossref.org/works/10.1/b": cr("10.1/b", LONG, published=None, issued={"date-parts": [[2024]]},
                                                author=[]),
    "https://api.crossref.org/works/10.1/d": cr("10.1/x", LONG),  # DOI mismatch -> skipped
    "https://api.crossref.org/works/10.1/e": cr("10.1/e", "short"),
}


def body_for(url, **kw):
    if url == BASE:
        return PAGES["2025"]
    if url.startswith(BASE + "?year="):
        return PAGES[query(url, "year")]
    if url in CROSSREF:
        return json.dumps(CROSSREF[url])
    raise AssertionError("unexpected " + url)


def handler(url):
    if url.startswith(BASE):
        year = query(url, "year")
        return html_response(url, PAGES.get(year, PAGES["2023"]))
    if url in CROSSREF:
        return json_response(url, CROSSREF[url])
    raise AssertionError("unexpected " + url)


class AmiApiSplit(unittest.TestCase):
    def test_crawl_is_unchanged(self):
        self.assertEqual("241601f6d9cbdcf78a73b88a40e849341de2412f792169dc4aa0571236651610", crawl_sha(SITE))

    def test_papers_equal_original_crawl(self):
        expected = native(SITE, body_for, method_patches=["_curl_get"])
        got, items, fetcher = pipeline(SITE, [BASE + "?year=2025&page=1", BASE + "?year=2024&page=1"], handler)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], got)
        self.assertEqual(identities(expected), identities(got))
        self.assertEqual(["10.1/a", "10.1/b", "10.1/d", "10.1/e"], [i.key for i in items])

    def test_empty_year_is_an_explicit_empty_page(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        from tests.api_split_helpers import api_plan
        plan = site_plan(api_plan(SITE), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(plan.listing["parser"], adapter=Adapter(SITE, plan.section("adapter"), None, crawler=crawler))
        page = parser.parse(html_response(BASE + "?year=2023&page=1", PAGES["2023"]))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get("explicit_empty"))
        with self.assertRaises(PermanentFailure):
            parser.parse(html_response(BASE + "?year=2023&page=1", "<html>maintenance</html>"))

    def test_shipped_plan_end_to_end(self):
        result, docs = run_e2e(SITE, handler, plan_override={"segments": {
            "mode": "range", "granularity": "year", "from": "2023", "to": "2025", "split_to": [],
            "filter": {"type": "parts", "year_param": "year"}}})
        self.assertNotIn(result["status"], ("incomplete", "config_error"), result)
        self.assertEqual(["10.1/A", "10.1/B"], sorted(d["post_number"].upper() for d in docs))
        self.assertEqual(["10.1/a", "10.1/b"], sorted(d["post_number"] for d in docs))


if __name__ == "__main__":
    unittest.main()
