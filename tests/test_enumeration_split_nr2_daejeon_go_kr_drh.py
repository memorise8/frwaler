"""NR2-20261018: daejeon-go-kr-drh enumeration split (crawl() unchanged), memory-only."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks as NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'daejeon-go-kr-drh'

    def test_short_abstract_skip_is_preserved(self):
        # _build_abstract() adds title/department/date context to a short body; with that
        # context the result decides crawl()'s 100-character minimum exactly as the split does.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_list_page_number_comes_from_the_url(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'].encode(),
                                          'https://www.daejeon.go.kr/drh/board/boardNormalList.do?pageIndex=7')
        self.assertEqual([7, 7], [r['page'] for r in rows])


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'daejeon-go-kr-drh'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
