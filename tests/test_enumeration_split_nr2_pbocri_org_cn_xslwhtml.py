"""NR2-20261018: pbocri-org-cn-xslwhtml enumeration split (crawl() unchanged), memory-only.
crawl() walks two sections (qklw = 期刊论文, rmyhgzlw = 工作论文); the plan reads them as
explicit segments and the list method takes the category from the section path."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr2_a_split_helpers import NR2SplitChecks

SITE = 'pbocri-org-cn-xslwhtml'
QKLW = 'https://www.pbocri.org.cn/qklw.html'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        # The generic checks parse the listing at the plan's listing URL; give them the
        # journal-paper section (the shipped plan's first segment) as a plain listing.
        site = self.plan['sites'][SITE]
        site['strategy'] = 'last_page_backward'
        site['listing']['url'] = QKLW
        site.pop('segments')

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def get(url, *a, **k):
            if url in fixture['details']:
                return fixture['details'][url]
            return fixture['listing_body'] if url == QKLW else fixture['empty_body']
        with patch.object(crawler, '_curl_get', get), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, []

    def test_pdf_rows_need_no_detail_and_keep_crawl_rule(self):
        crawler, _ = load(SITE)
        body = self.fixture['listing_body'].replace('/25278.html', 'https://file.pbocri.org.cn/' + 'a' * 32 + '.pdf')
        url = 'https://www.pbocri.org.cn/rmyhgzlw.html?page=1'
        row = crawler._enumeration_items(body, url)[0]
        self.assertEqual((True, url, '工作论文'), (row['is_pdf'], row['detail_url'], row['category']))
        self.assertIsNone(crawler._enumeration_paper('', row))  # title < 100 chars: crawl() skips


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url in fixture['details']:
                return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
            base, _, query = url.partition('?')
            first = query in ('', 'page=1')
            if base == QKLW and first:
                return Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)
            return Response(200, {'Content-Type': 'text/html'}, fixture['empty_body'], url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertIn(result['status'], ('complete', 'complete_unverified'), result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
