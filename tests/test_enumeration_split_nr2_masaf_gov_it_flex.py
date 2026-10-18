"""NR2-20261018: masaf-gov-it-flex enumeration split (crawl() unchanged), memory-only.
crawl() reads one listing per year (?YY=2026 ... 2014; only 2022+ pages list slug URLs that
crawl() keeps); the plan reads years 2022-2026 as explicit segments."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr_a_split_helpers import NRSplitChecks

SITE = 'masaf-gov-it-flex'
LIST = 'https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/331'


def body_for(fixture, url):
    if url in fixture['details']:
        return fixture['details'][url]
    return fixture['listing_body'] if url == f'{LIST}?YY=2026' else fixture['empty_body']


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _module = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(crawler, '_curl', lambda url, *a, **k: body_for(fixture, url)), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, []

    def test_added_listing_rejects_wrong_shape(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                crawler._enumeration_items({'error': 'not a listing'}, f'{LIST}?YY=2026')
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)

    def test_year_comes_from_the_year_page(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'].encode(), f'{LIST}?YY=2025&page=1')
        self.assertEqual([2025, 2025], [r['year'] for r in rows])
        rows = crawler._enumeration_items(self.fixture['listing_body'].encode(), LIST)
        self.assertEqual([2026, 2026], [r['year'] for r in rows])

    def test_old_flex_urls_are_skipped_like_crawl(self):
        crawler, _ = load(SITE)
        body = self.fixture['listing_body'].replace('https://www.masaf.gov.it/olio_igp_test',
                                                    'https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/8222')
        rows = crawler._enumeration_items(body.encode(), f'{LIST}?YY=2026')
        self.assertEqual(['https://www.masaf.gov.it/pesca_fondo_test'], [r['url'] for r in rows])


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0
        years = [s['params']['YY'] for s in plan['segments']['items']]
        pages, details = {}, {}
        for y in years:
            body = fixture['listing_body']
            for old in fixture['details']:
                body = body.replace(old, f'{old}_{y}')
                details[f'{old}_{y}'] = fixture['details'][old]
            pages[y] = body

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url in details:
                return Response(200, {'Content-Type': 'text/html'}, details[url], url)
            if url.startswith(LIST + '?'):
                from urllib.parse import parse_qs, urlsplit
                y = parse_qs(urlsplit(url).query)['YY'][0]
                return Response(200, {'Content-Type': 'text/html'}, pages[y], url)  # ?page= is ignored
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2 * len(years), len(docs))
        self.assertEqual(2 * len(years), len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
