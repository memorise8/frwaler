"""hec-edu-en: crawl() lists through Drupal Views AJAX (POST); the plan reads the
same view as GET ?page=N. The native run feeds crawl() the fixture cards as the
AJAX insert command, so the comparison covers the same card HTML both ways."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, run_split


def untitled_fixture(fixture):
    """Older press releases: empty card title (<span class="h4"></span>), title only in the detail <h1>."""
    f = copy.deepcopy(fixture)
    for n in (1, 2):
        f['listing_body'] = f['listing_body'].replace('<span>Offline %d</span>' % n, '')
        url = 'https://www.hec.edu/en/news/offline-%d' % n
        f['details'][url] = f['details'][url].replace('<body>', '<body><h1>Detail  title\n %d</h1>' % n)
    return f


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'hec-edu-en'

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; ajax_pages = []

        def get(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            return fixture['empty_body']  # list page read only for the view_dom_id

        def ajax(page, view_dom_id, *args, **kwargs):
            ajax_pages.append(page)
            data = fixture['listing_body'] if page == 0 else fixture['empty_body']
            return json.dumps([{'command': 'settings'}, {'command': 'insert', 'data': data}])

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        crawler._ajax_post = ajax
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(ajax_pages), 2)
        return captured, calls

    def test_empty_card_title_falls_back_to_detail_h1(self):
        f = untitled_fixture(self.fixture)
        expected, _ = self.native(f)
        # crawl() itself would save these with an empty title (framework: empty_item).
        self.assertEqual(['', ''], [p['title'] for p in expected])
        got = self.pipeline(f)
        self.assertEqual(['Detail title 1', 'Detail title 2'], [p['title'] for p in got])
        for native_paper, paper in zip(expected, got):
            native_paper = dict(clean_paper(native_paper), title=paper['title'])
            self.assertEqual(native_paper, paper)  # identity and every other field unchanged

    def test_card_title_wins_over_detail_h1(self):
        f = copy.deepcopy(self.fixture)
        for url in f['details']:
            f['details'][url] = f['details'][url].replace('<body>', '<body><h1>Other</h1>')
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'hec-edu-en'

    def test_untitled_cards_complete_end_to_end(self):
        f = untitled_fixture(self.fixture)
        end = lambda url: Response(200, {'Content-Type': f['content_type']}, f['empty_body'], url)
        result, docs = run_split(self.SITE, fixture=f, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['Detail title 1', 'Detail title 2'], sorted(d['title'] for d in docs))
