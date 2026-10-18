"""NR-20261018: nidcr-nih-gov-news-events enumeration split (crawl() unchanged), memory-only.
crawl() walks the current list page and the 2025..2021 year pages; the plan reads the same pages
as explicit segments. Year pages have no pagination: the page param 'p' (not robots-disallowed,
unlike '?page=') returns the same page, which the framework reads as the segment's end."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr_a_split_helpers import NRSplitChecks

SITE = 'nidcr-nih-gov-news-events'
BASE = 'https://www.nidcr.nih.gov/news-events/nidcr-news'


def body_for(fixture, url):
    if url in fixture['details']:
        return fixture['details'][url]
    path = url.split('?', 1)[0]
    return fixture['listing_body'] if path == BASE else fixture['empty_body']


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured = []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        transport = lambda url, *a, **k: body_for(fixture, url)
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, []


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0
        years = ['2025', '2024', '2023', '2022', '2021']
        extra = {}
        for i, y in enumerate(years):
            slug = f'archive-{y}'
            extra[f'{BASE}/{y}'] = fixture['listing_body'].split('<div class="c-view__row">')[0] + (
                f'<div class="c-view__row"><article class="c-teaser--news"><div class="c-teaser__title">'
                f'<a href="/news-events/nidcr-news/{slug}">{slug}</a></div></article></div></div></article></main></body></html>')
            extra[f'{BASE}/{slug}'] = fixture['details'][f'{BASE}/dds-hub'].replace('node/27986', f'node/{30000 + i}')
        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            path = url.split('?', 1)[0]
            body = extra.get(path) or body_for(fixture, url)
            return Response(200, {'Content-Type': 'text/html'}, body, url)
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
        self.assertEqual(7, len(docs))
        self.assertEqual({'27986', '27987'} | {str(30000 + i) for i in range(5)}, {d['post_number'] for d in docs})


if __name__ == '__main__':
    unittest.main()
