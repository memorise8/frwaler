import json
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as base
from crawler.enumeration.adapters import clean_paper, paper_identity
from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'wallonie-be-fr'

    def native(self, fixture=None):
        # crawl() fetches through _curl_get_text (body, effective URL) and sleeps
        # detail_delay, which __init__ (skipped here) sets.
        original_load = base.load

        def load(site):
            crawler, module = original_load(site)
            crawler.detail_delay = 0
            crawler._curl_get_text = lambda url, **kw: (crawler._curl_get(url), url)
            return crawler, module

        with patch.object(base, 'load', load):
            return super().native(fixture)

    def test_detail_redirect_preserves_registered_identity(self):
        # crawl() records curl's effective URL in metadata.detail_effective_url, the plan
        # the fetcher's final URL: identity and every other field stay the same.
        expected = [clean_paper(p) for p in self.native()[0]]
        actual = self.pipeline(redirect=True)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        for e, a in zip(expected, actual):
            em, am = json.loads(e.pop('metadata')), json.loads(a.pop('metadata'))
            self.assertEqual(e, a)
            self.assertEqual(em.pop('detail_effective_url') + '?moved=1', am.pop('detail_effective_url'))
            self.assertEqual(em, am)


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'wallonie-be-fr'
