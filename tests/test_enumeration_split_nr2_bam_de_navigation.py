"""NR2-20261018: bam-de-navigation enumeration split (crawl() unchanged), memory-only.
The TYPO3 pager needs the per-page cHash link, so the plan follows it (X2 next_link)."""
import contextlib
import copy
import io
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr2_a_split_helpers import NR2SplitChecks

SITE = 'bam-de-navigation'
LIST = 'https://bam.de/en/news/papers-of-the-month'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_follows_next_link_to_the_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0
        page2 = fixture['listing_body'].replace('jenseits-der-farbe', 'page-two-a').replace(
            'atomare-strukturen', 'page-two-b')
        page2 = page2[:page2.index('<a class="page-link')] + '</main></body></html>'
        details = dict(fixture['details'])
        for old, new in (('jenseits-der-farbe', 'page-two-a'), ('atomare-strukturen', 'page-two-b')):
            src = next(v for k, v in fixture['details'].items() if k.endswith(old))
            details['https://www.bam.de/en/news/papers-of-the-month/paper/' + new] = src.replace('docId/6', 'docId/7')

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url in details:
                return Response(200, {'Content-Type': 'text/html'}, details[url], url)
            if url == LIST:
                return Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)
            if url.startswith(LIST + '?') and 'currentPage%5D=2' in url:
                return Response(200, {'Content-Type': 'text/html'}, page2, url)
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
        self.assertEqual(4, len(docs))
        self.assertEqual(4, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
