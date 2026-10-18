import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'brgm-fr-fr'

    def test_cards_outside_main_match_original_crawl(self):
        import copy
        fixture = copy.deepcopy(self.fixture)
        fixture['listing_body'] = fixture['listing_body'].replace('<main>', '').replace('</main>', '')
        expected, _ = self.native(fixture)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, self.pipeline(fixture))


    def test_malformed_detail_html_is_parsed_like_crawl(self):
        # crawl() parses details with self._make_soup (html5lib first); the plan's
        # "soup" token would use html.parser. Unclosed paragraphs separate the two.
        import copy
        from bs4 import BeautifulSoup
        from crawler.enumeration.adapters import clean_paper
        from tests.collector_split_helpers import load
        fixture = copy.deepcopy(self.fixture)
        for url, body in fixture['details'].items():
            fixture['details'][url] = body.replace(
                '</p></div>', '<p>Second unclosed paragraph of the synthetic body for the parser check.</div>')
        crawler, _ = load(self.SITE)
        body = next(iter(fixture['details'].values()))
        self.assertNotEqual(crawler._extract_abstract(crawler._make_soup(body)),
                            crawler._extract_abstract(BeautifulSoup(body, 'html.parser')),
                            'fixture must make html5lib and html.parser disagree')
        expected, _ = self.native(fixture)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(fixture)
        self.assertEqual(expected, self.raw_papers)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual('_enumeration_detail', self.plan['sites'][self.SITE]['adapter']['detail_method'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'brgm-fr-fr'
