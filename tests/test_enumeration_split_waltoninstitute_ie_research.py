"""L3b-20261018: enumeration split for waltoninstitute-ie-research.
The site's own WordPress backend: list /wp-json/wp/v2/publication?per_page=100&page=N
&_fields=id,date,slug,link (X-WP-Total), past the last page WordPress answers 400
rest_post_invalid_page_number (end_statuses). Detail = the same per-item REST URL
crawl() requests; paper = unchanged _build_paper() (no Content-Disposition HEAD).
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, WP_PAST_LAST_PAGE

SITE = 'waltoninstitute-ie-research'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, list_calls = [], [], []

        def curl_json(url, context):
            calls.append(url)
            if url in fixture['details']:
                return copy.deepcopy(fixture['details'][url]), {}, 200
            list_calls.append(url)
            if len(list_calls) == 1:
                return json.loads(fixture['listing_body']), {}, 200
            return json.loads(WP_PAST_LAST_PAGE), {}, 400
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._curl_json = curl_json
        crawler._content_disposition_filename = lambda url: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(2, len(list_calls))
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        # _build_paper() builds a bibliographic abstract from the field rows, so a short
        # content text alone does not skip a record; both paths must agree on the result.
        from crawler.enumeration.adapters import clean_paper
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        sparse = copy.deepcopy(f)
        for d in sparse['details'].values():
            d['post_type_data'] = {'data': []}
            d['title'] = {'rendered': ''}
            d['link'] = ''
        self.assertEqual([clean_paper(p) for p in self.native(sparse)[0]], self.pipeline(sparse))

    def test_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])
        self.assertEqual('https://wordpress.waltoninstitute.ie/wp-content/uploads/synthetic-two.pdf', expected[0]['pdf_url'])

    def test_no_head_request_and_no_lingering_override(self):
        crawler, _ = load(self.SITE)
        url = next(iter(self.fixture['details']))
        paper = crawler._enumeration_paper(copy.deepcopy(self.fixture['details'][url]), {'url': url})
        self.assertEqual('990002', paper['post_number'])
        self.assertNotIn('_content_disposition_filename', vars(crawler))

    def test_wordpress_end_body_is_an_empty_page(self):
        crawler, _ = load(self.SITE)
        self.assertEqual([], crawler._enumeration_items(json.loads(WP_PAST_LAST_PAGE), 'x'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
