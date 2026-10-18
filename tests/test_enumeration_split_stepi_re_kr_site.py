"""stepi-re-kr-site: split (_enumeration_items / _enumeration_paper) must produce exactly the
papers crawl() saves; crawl() is unchanged and external_id stays the list display number."""
import json
import unittest

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'stepi-re-kr-site'
# Shape of the real page past the last one (pageIndex=139, 2026-10-18): table body without rows.
EMPTY_PAGE = '<html><body><table><tbody><tr><td colspan="6">등록된 게시물이 없습니다.</td></tr></tbody></table></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_short_abstract_skip_is_preserved(self):
        # crawl() falls back to a template abstract (>= 104 chars), so a short site abstract
        # never skips a row; both paths must agree on that.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?cateCont=A0205&pageIndex=139'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_and_detail_only_for_reidx_rows(self):
        expected, _ = self.native()
        self.assertEqual(['1371', '381'], [p['external_id'] for p in expected])
        self.assertIsNone(expected[0]['category'])            # no reIdx: list row only
        self.assertEqual('과학기술정책', expected[1]['category'])  # reIdx: detail merged


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
