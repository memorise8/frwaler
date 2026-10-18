"""NR-20261018: ntp-niehs-nih-gov-publications enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'ntp-niehs-nih-gov-publications'

    def native(self, fixture=None, moved=False):
        # crawl()'s _curl_get returns {body, http_code, final_url} (curl -L effective URL).
        import contextlib, copy, io, json
        from unittest.mock import patch
        from tests.collector_split_helpers import load
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, lists = [], [], []
        def transport(url, *a, **k):
            calls.append(url)
            if url in fixture['details']:
                return {'body': fixture['details'][url], 'http_code': 200, 'final_url': url + ('?moved=1' if moved else '')}
            lists.append(url)
            return {'body': fixture['listing_body'] if len(lists) == 1 else fixture['empty_body'], 'http_code': 200, 'final_url': url}
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(crawler, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured, calls

    def test_detail_redirect_preserves_registered_identity(self):
        # crawl() keys the paper URL on the final (redirected) URL; the plan passes resp.url.
        from crawler.enumeration.adapters import clean_paper
        expected, _ = self.native(moved=True)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(redirect=True))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'ntp-niehs-nih-gov-publications'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
