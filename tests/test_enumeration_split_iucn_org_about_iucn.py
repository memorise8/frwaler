"""iucn-org-about-iucn: single listing page, report page + IUCN Library record per item
(ext:two_step_detail with ext:two_step_keep_first). crawl() (unchanged) and the shipped plan
are compared on synthetic data; no network."""
import ast
import copy
import hashlib
import json
import unittest

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'iucn-org-about-iucn'
REPORT_A = 'https://iucn.org/resources/annual-reports/synthetic-annual-report-2031'
LIB_A = 'https://portals.iucn.org/library/node/900001'


def without_library_link(fixture):
    """Report A without its library link: crawl() keeps it with a synthesized abstract."""
    f = copy.deepcopy(fixture)
    page = f['details'][REPORT_A]
    f['details'][REPORT_A] = page.replace(f'<a href="{LIB_A}">View in library</a>', '')
    del f['details'][LIB_A]
    return f


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_module_parsers_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        tree = ast.parse(code)
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['original_function_source_sha256'], actual)

    def test_both_pages_feed_the_paper(self):
        expected, calls = self.native()
        # listing + report page + library record per item, like crawl()
        self.assertEqual(4, len([u for u in calls if u in self.fixture['details']]))
        actual = self.pipeline()
        self.assertEqual('2031-01-01', actual[0]['published_date'])  # year from the report page
        self.assertEqual(self.fixture['long_text'], actual[0]['abstract'])  # from the library record
        self.assertTrue(actual[0]['pdf_url'].startswith('https://portals.iucn.org/library/sites/'))
        self.assertEqual(['annual reports', 'synthetic'], json.loads(actual[0]['keywords']))
        self.assertEqual([clean_paper(p) for p in expected], actual)

    def test_missing_library_link_uses_report_page_only(self):
        f = without_library_link(self.fixture)
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertTrue(expected[0]['abstract'].startswith('This IUCN Annual Report (2031)'))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertEqual(expected, self.raw_papers)

    def test_identity_is_report_slug(self):
        expected, _ = self.native()
        self.assertEqual(['synthetic-annual-report-2031', 'synthetic-annual-report-2030'],
                         [paper_identity(p)[0] for p in expected])
        self.assertEqual([REPORT_A, REPORT_A.replace('2031', '2030')], [paper_identity(p)[1] for p in expected])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Single listing page: there is no empty end page (the end is the repeated page);
        a page without report links is a listing error, never an empty end."""
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], p.listing['url']))
        self.assertEqual(2, len(page.items))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', p.listing['url']))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def repeat(self, fixture=None):
        f = fixture or self.fixture
        return lambda url: Response(200, {'Content-Type': 'text/html'}, f['listing_body'], url)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=self.repeat())
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertTrue(all(d['pdf_url'].startswith('https://portals.iucn.org/library/sites/') for d in docs))

    def test_short_paper_does_not_silently_become_complete(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_split(SITE, fixture=f, end=self.repeat(f))
        self.assertEqual('complete', result['status'], result)  # crawl() synthesizes the abstract
        self.assertEqual(2, len(docs))

    def test_missing_library_link_completes(self):
        f = without_library_link(self.fixture)
        result, docs = run_split(SITE, fixture=f, end=self.repeat(f))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
