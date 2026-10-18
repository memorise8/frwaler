"""csem-ch-en: Prismic search API listing whose master ref is read once per run from the
public API root (ext:dynamic_ref), detail API record + public press page per item
(ext:two_step_detail with url_template and ext:two_step_keep_first; the page is crawl()'s
short-abstract HTML fallback). crawl() (unchanged) and the shipped plan are compared on
synthetic data; no network."""
import contextlib
import copy
import io
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'csem-ch-en'
API_ROOT = 'https://csem.cdn.prismic.io/api/v2'
SEARCH = 'https://csem.cdn.prismic.io/api/v2/documents/search'
PAGE_ONE = 'https://www.csem.ch/en/press/synthetic-press-one/'


def with_api_root(fixture):
    f = copy.deepcopy(fixture)
    f['details'][API_ROOT] = f['api_root']
    return f


def short_api_text(fixture):
    """Record one's API text below 50 chars: crawl() falls back to the public page."""
    f = copy.deepcopy(fixture)
    listing = json.loads(f['listing_body'])
    for rec in [listing['results'][0]] + [d['results'][0] for u, d in f['details'].items()
                                          if isinstance(d, dict) and 'SYNTHid000000001' in u]:
        rec['data']['content'] = []
        rec['data']['slices'] = []
    f['listing_body'] = json.dumps(listing, ensure_ascii=False)
    return f


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with a memory _curl_get (API root, search pages, detail API, public pages)."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls = [], []

        def get(url, context='request', accept=None, referer=None):
            calls.append(url)
            if url == API_ROOT:
                return json.dumps(fixture['api_root']), url
            if url in fixture['details']:
                value = fixture['details'][url]
                return (json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value), url
            self.assertTrue(url.startswith(SEARCH + '?'), url)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(['synthetic-master-ref'], query['ref'])  # the master ref, not the first ref
            return (fixture['listing_body'] if query['page'] == ['1'] else fixture['empty_body']), url
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.calls = calls
        return captured, calls

    def test_plan_lists_the_same_query_as_crawl(self):
        crawler, _ = load(SITE)
        expected = parse_qs(urlsplit(crawler._list_url('R', 3)).query)
        listing = self.plan['sites'][SITE]['listing']
        self.assertEqual(listing['url'], SEARCH)
        params = dict(listing['params'], **{listing['page_param']: 3})
        self.assertEqual({k: (v if isinstance(v, list) else [str(v)]) for k, v in params.items()},
                         dict(expected, ref=['{dyn:ref}']))
        self.assertEqual(crawler.PAGE_SIZE, listing['page_size'])
        self.assertEqual(API_ROOT, listing['dynamic']['url'])

    def test_dynamic_ref_regex_picks_crawls_master_ref(self):
        crawler, _ = load(SITE)
        rule = self.plan['sites'][SITE]['listing']['dynamic']['values']['ref']
        self.assertTrue(rule['public'])
        root = self.fixture['api_root']
        with patch.object(crawler, '_curl_json', lambda *a, **k: root):
            expected = crawler._fetch_master_ref()
        for text in (json.dumps(root), json.dumps(root, indent=2),
                     json.dumps({'refs': [{'isMasterRef': True, 'label': 'M', 'ref': 'r-1', 'id': 'master'}]})):
            m = re.search(rule['regex'], text)
            self.assertIsNotNone(m)
            if 'r-1' not in text:
                self.assertEqual(expected, m.group(1))
        self.assertIsNone(re.search(rule['regex'], json.dumps({'refs': [{'id': 'x', 'ref': 'y'}]})))

    def test_both_responses_feed_the_paper(self):
        expected, _ = self.native()
        actual = self.pipeline()
        self.assertEqual(['SYNTHid000000001', 'SYNTHid000000002'], [p['external_id'] for p in actual])
        self.assertEqual(1, json.loads(actual[0]['metadata'])['list_page'])
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(expected, self.raw_papers)
        self.assertFalse([u for u in self.calls if u.startswith('https://www.csem.ch/')])  # long API text

    def test_short_api_text_falls_back_to_public_page_like_crawl(self):
        f = short_api_text(self.fixture)
        expected, calls = self.native(f)
        self.assertIn(PAGE_ONE, calls)
        self.assertTrue(expected[0]['abstract'].startswith('Synthetic press one'))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertEqual(expected, self.raw_papers)

    def test_identity_is_prismic_id(self):
        expected, _ = self.native()
        self.assertEqual([('SYNTHid000000001', PAGE_ONE),
                          ('SYNTHid000000002', 'https://www.csem.ch/en/press/synthetic-press-two/')],
                         [paper_identity(p) for p in expected])
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in self.pipeline()])

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items({'error': 'not a listing'}, SEARCH + '?page=1')
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        self.fixture = with_api_root(self.fixture)
        real = run_split

        def run(site, **kw):
            kw['fixture'] = with_api_root(kw.get('fixture') or self.fixture)
            return real(site, **kw)
        p = patch(__name__ + '.run_split', run)
        p.start()
        self.addCleanup(p.stop)
        import tests.collector_split_helpers as h1
        import tests.collector_split2_helpers as h2
        for mod in (h1, h2):
            p = patch.object(mod, 'run_split', run)
            p.start()
            self.addCleanup(p.stop)

    def test_short_api_text_run_saves_with_fallback(self):
        f = short_api_text(self.fixture)
        result, docs = run_split(SITE, fixture=f,
                                 end=lambda url: Response(200, {'Content-Type': 'application/json'}, f['empty_body'], url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
