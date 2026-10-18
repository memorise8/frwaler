"""motir-go-kr-kor: split (_enumeration_items / _enumeration_paper) must produce exactly the
papers crawl() saves; crawl() is unchanged (it reads board ATCL3f49a5a8c, 보도자료).
The shipped plan reads the registered board ATCL449580de4 (간행물/발행물): the board code
comes from the list URL, identity stays bbs_seq_n."""
import copy
import json
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_plan

SITE = 'motir-go-kr-kor'
CRAWL_BOARD = 'https://www.motir.go.kr/kor/article/ATCL3f49a5a8c'
PLAN_BOARD = 'https://www.motir.go.kr/kor/article/ATCL449580de4'


def crawl_board_plan(plan):
    """The shipped plan pointed at crawl()'s board, for paper-for-paper comparison."""
    plan = copy.deepcopy(plan)
    plan['sites'][SITE]['listing']['url'] = CRAWL_BOARD
    return plan


def plan_board_fixture(fixture):
    out = copy.deepcopy(fixture)
    out['details'] = {k.replace(CRAWL_BOARD, PLAN_BOARD): v for k, v in fixture['details'].items()}
    return out


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        self.assertEqual(PLAN_BOARD, self.plan['sites'][SITE]['listing']['url'])
        self.plan = crawl_board_plan(self.plan)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = PLAN_BOARD + '?pageIndex=37'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_registered_board_builds_its_own_detail_urls(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'], PLAN_BOARD + '?pageIndex=1')
        self.assertEqual(['861', '860'], [r['bbs_seq_n'] for r in rows])
        self.assertEqual([PLAN_BOARD + '/861/view', PLAN_BOARD + '/860/view'], [r['url'] for r in rows])
        body = next(iter(self.fixture['details'].values()))
        paper = crawler._enumeration_paper(body, rows[0])
        self.assertEqual('861', paper['external_id'])
        self.assertEqual(PLAN_BOARD + '/861/view', paper['url'])
        self.assertEqual('ATCL449580de4', json.loads(paper['metadata'])['atclId'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, body, url)
        result, docs = run_split(SITE, end=end, fixture=plan_board_fixture(self.fixture))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(['860', '861'], sorted(d['post_number'] for d in docs))
        self.assertTrue(all('ATCL449580de4' in d['meta_url'] for d in docs))

    def test_crawl_board_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, body, url)
        result, docs = run_split(SITE, end=end, plan=crawl_board_plan({'sites': {SITE: split_plan(SITE)}})['sites'][SITE])
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
