import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks
from crawler.enumeration.http import Response


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'babraham-ac-uk-news'

    def native(self, fixture=None):
        # crawl() fetches through self._curl; route it to the helper's fake transport.
        from unittest.mock import patch
        import tests.collector_split_helpers as h
        original = h.load
        def load(site):
            crawler, module = original(site)
            crawler._curl = lambda url, **kw: crawler._curl_get(url)
            return crawler, module
        with patch.object(h, 'load', load):
            return super().native(fixture)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'babraham-ac-uk-news'
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{
        'babraham-ac-uk-news': (200, 'text/html', '<html><body><main></main></body></html>')})
