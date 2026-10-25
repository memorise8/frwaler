"""consult-environment-govt-nz-climate: _enumeration_items / _enumeration_detail reuse crawl()'s
list validation and respondent parser (crawl() unchanged; external_id = uuId).
Minimal hand-written HTML."""
import unittest

from tests.collector_split_helpers import load

SITE = 'consult-environment-govt-nz-climate'
BASE = 'https://consult.environment.govt.nz/climate/nz-ets-unit-settings-and-regulatory-updates-2025/consultation/'
LIST_URL = BASE + 'published_select_respondent'
LIST = ('<html><body><main><h1>NZ ETS unit settings and annual regulatory updates 2025</h1>'
        '<h3>2 Published responses</h3>'
        '<a href="view_respondent?uuId=111">One</a><a href="view_respondent?uuId=222">Two</a>'
        '<a href="view_respondent?uuId=111">One again</a></main></body></html>')
DETAIL = ('<html><body><main><div class="cs-question-container"><h4>Submitter name</h4>'
          '<div data-test-hook="x">Jane Example</div></div>'
          '<div class="cs-question-container"><h4>Your views</h4><div data-test-hook="y">'
          + 'Settings should stay predictable for participants. ' * 2 + '</div></div></main></body></html>')


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def test_items(self):
        items = self.crawler._enumeration_items(LIST, LIST_URL)
        self.assertEqual(['111', '222'], [i['uuId'] for i in items])
        self.assertEqual(BASE + 'view_respondent?uuId=111', items[0]['url'])
        with self.assertRaises(ValueError):
            self.crawler._enumeration_items('<html><body><main></main></body></html>', LIST_URL)

    def test_paper(self):
        item = self.crawler._enumeration_items(LIST, LIST_URL)[0]
        paper = self.crawler._enumeration_detail(DETAIL, item)
        self.assertEqual(paper, self.crawler._parse_respondent(DETAIL, '111', item['url']))
        self.assertEqual(('111', 'Jane Example', '2025-08-19'),
                         (paper['external_id'], paper['title'], paper['published_date']))
        self.assertIsNone(self.crawler._enumeration_detail('<html><main></main></html>', item))


if __name__ == '__main__':
    unittest.main()
