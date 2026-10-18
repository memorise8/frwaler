"""ulsan-go-kr-u: board rows (GET list.ulsan page=N) + view.do page through the unchanged
crawl() (POST page=N via module _curl_raw) vs the plan adapter. Memory only."""
import ast
import contextlib
import copy
import hashlib
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from tests.collector_split_helpers import ROOT, load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'ulsan-go-kr-u'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; pages = []

        def curl_raw(url, *, post_data=None, timeout=30):
            if post_data is not None:
                pages.append(post_data)
                body = fixture['listing_body'] if post_data == 'page=1' else fixture['empty_body']
            else:
                calls.append(url)
                body = fixture['details'][url]
            return body.encode('utf-8')

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_raw', curl_raw), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(pages), 2)
        return captured, calls

    def test_identity_is_data_id(self):
        papers = self.pipeline()
        self.assertEqual(self.fixture['expected_ids'], [p['external_id'] for p in papers])
        self.assertEqual(list(self.fixture['details']), [p['url'] for p in papers])

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['module_function_source_sha256'], actual)

    def test_module_fetcher_is_restored(self):
        crawler, module = load(self.SITE)
        original = module._curl_raw
        url = next(iter(self.fixture['details']))
        item = crawler._enumeration_items(self.fixture['listing_body'])[0]
        with guarded():
            crawler._enumeration_paper(self.fixture['details'][url], item)
        self.assertIs(original, module._curl_raw)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE
