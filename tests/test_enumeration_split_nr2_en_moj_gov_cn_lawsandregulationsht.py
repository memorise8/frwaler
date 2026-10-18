"""NR2-20261018: en-moj-gov-cn-lawsandregulationsht enumeration split (crawl() unchanged),
memory-only. List pages lawsandregulations.html, _2.html, ... (X1 path paging)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks

SITE = 'en-moj-gov-cn-lawsandregulationsht'
PDF = 'http://en.moj.gov.cn/pdf/RegulationsonClosureofFinancialInstitutions.pdf'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def _pdf_listing(self, abstract):
        box = (f'<div class="lis_box"><h3><a href="{PDF}">PDF regulation</a></h3><b>2020-01-01</b>'
               f'<p class="pc_box">{abstract}</p></div>')
        return self.fixture['listing_body'].replace('<div class="mr950_cont">', '<div class="mr950_cont">' + box, 1)

    def test_pdf_only_rows_use_the_list_abstract_like_crawl(self):
        for abstract in (self.fixture['long_text'], 'short'):
            listing = self._pdf_listing(abstract)
            crawler, module = load(SITE)
            captured = []
            crawler._last_save_created = True
            crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
            calls = []

            def get(url):
                calls.append(url)
                if url in self.fixture['details']:
                    return self.fixture['details'][url]
                return listing if len([c for c in calls if 'lawsandregulations' in c]) == 1 else self.fixture['empty_body']
            with patch.object(crawler, '_curl_get', get), patch('time.sleep', lambda *a: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(limit=None)
            native_pdf = [p for p in captured if p['url'] == PDF]
            list_url = 'https://en.moj.gov.cn/lawsandregulations.html'
            row = next(r for r in crawler._enumeration_items(listing, list_url) if r['url'] == PDF)
            self.assertEqual(list_url, row['detail_url'])
            paper = crawler._enumeration_paper(listing.encode(), row)
            self.assertEqual([clean_paper(p) for p in native_pdf], [clean_paper(paper)] if paper else [])
            self.assertNotIn(PDF, calls)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_STATUS = 404


if __name__ == '__main__':
    unittest.main()
