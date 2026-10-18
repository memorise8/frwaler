"""agriculture-gouv-fr-recherche-developpem: enumeration split (RV 2026-10-19).
Listing = crawl()'s espace-presse theme query (?pg=N, DSFR fr-card); detail = crawl()'s article page.
_enumeration_items/_enumeration_paper repeat crawl()'s card and detail parsing line for line;
crawl() is unchanged (the file is pinned by delivery/incremental_holds.json, hash updated with it).
Memory-only: compares the plan's output with the unchanged crawl()'s save argument and runs the
shipped plan through the real SiteRunner with a fake transport."""
import contextlib
import io
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, load, split_fixture, split_plan

SITE = 'agriculture-gouv-fr-recherche-developpem'


class AgricultureSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_identity_dates_and_pdf_like_crawl(self):
        expected, _ = self.native()
        self.assertEqual(['synthese-recherche-alpha-2031', 'synthese-recherche-beta-2031'],
                         [p['post_number'] for p in expected])
        self.assertEqual(['2031-05-14', '2031-04-02'], [p['published_date'] for p in expected])
        self.assertEqual(['2031-05-13', '2031-04-02'], [p['listed_date'] for p in expected])
        papers = self.pipeline()
        self.assertEqual([p['url'] for p in expected], [p['url'] for p in papers])
        self.assertTrue(papers[0]['pdf_url'].endswith('synthese-alpha.pdf'))


class AgricultureSplitRun(unittest.TestCase):
    """The shipped plan end to end: page 2 repeats page 1 (the theme query repeats its last page)."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'', url)
            if url in fixture['details']:
                return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
            if parts.path == '/espace-presse':
                return Response(200, {'Content-Type': fixture['content_type']}, fixture['listing_body'], url)
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['synthese-recherche-alpha-2031', 'synthese-recherche-beta-2031'],
                         sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
