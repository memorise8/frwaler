"""NR-20261018: mef-gov-it-ufficio-stampa enumeration split (crawl() unchanged), memory-only.
crawl() walks year listings (current year down to 2025); the plan reads the same year
listings as explicit date_range_split segments ({segment} = year)."""
import contextlib
import copy
import datetime as _dt
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load, split_plan
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks

YEAR_URL = 'https://www.mef.gov.it/ufficio-stampa/comunicati/2026/index.html?pagesize=10&page=1'


class _Now2026(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 12, 31)


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'mef-gov-it-ufficio-stampa'

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; list_calls = []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            list_calls.append(url)
            return fixture['listing_body'] if url.endswith('/comunicati/2026/') else fixture['empty_body']

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch.object(module, 'datetime', _Now2026), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        self.raw_papers = []
        original = crawler._enumeration_paper

        def capture(*a, **k):
            out = original(*a, **k)
            if out is not None:
                self.raw_papers.append(copy.deepcopy(out))
            return out
        crawler._enumeration_paper = capture
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], YEAR_URL)).items
        self.assertEqual(2, len(items))

        def fetch(url):
            self.assertIn(url, fixture['details'])
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url],
                            url + '?moved=1' if redirect else url)
        papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        return [x for x in papers if x is not None]

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], YEAR_URL))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', YEAR_URL))
        self.assertEqual([], err.items)
        self.assertTrue(err.extra.get('parse_error'))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'mef-gov-it-ufficio-stampa'

    def _run(self, listing_for):
        from tests.enumeration_fakes import Env
        plan = copy.deepcopy(split_plan(self.SITE))
        plan['request_interval_s'] = 0
        fx = self.fixture

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fx['details']:
                return Response(200, {'Content-Type': 'text/html'}, fx['details'][url], url)
            page = int(parse_qs(parts.query).get('page', ['1'])[0])
            return listing_for(parts.path, page, url)

        env = Env(self.SITE)
        try:
            _, module = load(self.SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == self.SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            return result, env.docs()
        finally:
            env.close()

    def test_shipped_plan_completes_end_to_end(self):
        fx = self.fixture

        # 2025 serves the same synthetic releases under its own year path (other URLs/ids).
        # (slugs differ too: the registered external_id is the slug alone.)
        old = fx['listing_body'].replace('/comunicati/2026/', '/comunicati/2025/').replace('/">', '-2025/">')
        for url, body in list(fx['details'].items()):
            fx['details'][url.replace('/2026/', '/2025/')[:-1] + '-2025/'] = body.replace('N° 12', 'N° 2')

        def listing_for(path, page, url):
            if page != 1:
                body = fx['empty_body']
            else:
                body = fx['listing_body'] if '/2026/' in path else old if '/2025/' in path else fx['empty_body']
            return Response(200, {'Content-Type': 'text/html'}, body, url)
        result, docs = self._run(listing_for)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(4, len(docs))
        self.assertEqual(4, len({d['meta_url'] for d in docs}))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        def listing_for(path, page, url):
            return Response(200, {'Content-Type': 'text/html'}, '<html><body><p>Site under maintenance</p></body></html>', url)
        result, docs = self._run(listing_for)
        self.assertNotIn(result['status'], ('complete', 'runner_error'), result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
