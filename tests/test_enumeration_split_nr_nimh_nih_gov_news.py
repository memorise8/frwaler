"""NR-20261018: nimh-nih-gov-news enumeration split (crawl() unchanged), memory-only.
crawl() walks /news/science-updates/<year> pages back from the current year; the plan reads
the existing year pages (2007-2025) as explicit segments."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr_a_split_helpers import NRSplitChecks

SITE = 'nimh-nih-gov-news'
LIST = 'https://www.nimh.nih.gov/news/science-updates'


def body_for(fixture, url):
    if url in fixture['details']:
        return fixture['details'][url]
    return fixture['listing_body'] if url.split('?', 1)[0] == f'{LIST}/2025' else ''


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured = []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        class FakeDate:  # crawl() starts at datetime.now().year; pin it to 2026
            @staticmethod
            def now():
                import datetime as _d
                return _d.datetime(2026, 10, 18)
        with patch.object(module, '_curl_get', lambda url, *a, **k: body_for(fixture, url)), \
                patch.object(module, 'datetime', FakeDate), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, []


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0
        years = [s['key'] for s in plan['segments']['items']]
        pages, details = {}, dict(fixture['details'])
        for y in years:
            if y == '2025':
                pages[f'{LIST}/2025'] = fixture['listing_body']
                continue
            body = fixture['listing_body'].replace('/2025/memories', f'/{y}/memories-{y}').replace('/2025/bipolar', f'/{y}/bipolar-{y}')
            pages[f'{LIST}/{y}'] = body
            details[f'{LIST}/{y}/memories-{y}'] = fixture['details'][f'{LIST}/2025/memories']
            details[f'{LIST}/{y}/bipolar-{y}'] = fixture['details'][f'{LIST}/2025/bipolar']
        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            path = url.split('?', 1)[0]
            if url in details:
                return Response(200, {'Content-Type': 'text/html'}, details[url], url)
            if path in pages:
                return Response(200, {'Content-Type': 'text/html'}, pages[path], url)
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
