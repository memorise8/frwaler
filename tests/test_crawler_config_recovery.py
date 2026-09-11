"""Regression coverage for the September crawler audit findings (no network)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup
import requests

from crawler.generic_crawler import GenericCrawler
from crawler.sites import CRAWLERS


CONFIGS = Path(__file__).resolve().parents[1] / 'crawler/sites/configs'


class GenericPaginationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def crawler(self, pagination):
        path = Path(self.tmp.name) / 'config.json'
        path.write_text(json.dumps({
            'site_id': 'test', 'site_name': 'Test', 'base_url': 'https://example.org',
            'list_page': {'url': 'https://example.org/list', 'pagination': pagination,
                          'selectors': {'item_container': 'li', 'item_link': 'a'}},
        }))
        crawler = GenericCrawler(path, None)
        crawler._save_paper = Mock()
        return crawler

    def page(self, *ids):
        return BeautifulSoup(''.join(f'<li><a href="/{i}">Document {i}</a></li>' for i in ids), 'html.parser')

    def test_static_page_finishes_and_deduplicates_without_limit(self):
        crawler = self.crawler({'type': 'none'})
        crawler._fetch_page = Mock(side_effect=[self.page('a', 'a', 'b'), AssertionError('repeated static page')])
        self.assertEqual(crawler.crawl(), 2)
        self.assertEqual(crawler._fetch_page.call_count, 1)
        self.assertEqual(crawler._save_paper.call_count, 2)

    def test_pagination_stops_on_repeated_last_page(self):
        crawler = self.crawler({'type': 'query_param', 'param': 'page', 'start': 0, 'step': 1})
        crawler._fetch_page = Mock(side_effect=[self.page('a'), self.page('a', 'b'), self.page('b'), AssertionError('loop')])
        self.assertEqual(crawler.crawl(), 2)
        self.assertEqual([c.kwargs['params']['page'] for c in crawler._fetch_page.call_args_list], [0, 1, 2])

    def test_limit_does_not_fetch_an_extra_page(self):
        crawler = self.crawler({'type': 'query_param'})
        crawler._fetch_page = Mock(return_value=self.page('a', 'b'))
        self.assertEqual(crawler.crawl(limit=1), 1)
        self.assertEqual(crawler._fetch_page.call_count, 1)

    def test_only_supported_compression_is_advertised(self):
        crawler = self.crawler({'type': 'none'})
        # requests only advertises optional encodings when a decoder exists.
        with requests.Session() as session:
            self.assertEqual(crawler._session.headers['Accept-Encoding'], session.headers['Accept-Encoding'])


class RecoveredSourcesTest(unittest.TestCase):
    def test_all_configs_are_valid_json(self):
        for path in CONFIGS.glob('*.json'):
            with self.subTest(path=path.name):
                json.loads(path.read_text())

    def test_legacy_ids_resolve_to_working_specialized_implementations(self):
        for legacy, current in [('ncha-gov-cn', 'ncha-gov-cn-col'),
                                ('arcep-fr', 'arcep-fr-actualites'),
                                ('scaht-org', 'scaht-org-en'),
                                ('baw-de', 'baw-de-en'),
                                ('pasteur-fr', 'pasteur-fr-en'),
                                ('directives-doe-gov-directives-', 'directives-doe-gov-directives-browse')]:
            with self.subTest(site=legacy):
                crawler = CRAWLERS[legacy](None)
                self.assertEqual(crawler.site_id, legacy)
                self.assertEqual(crawler.crawl.__func__.__code__.co_filename,
                                 CRAWLERS[current].crawl.__code__.co_filename)

    def test_budget_selects_reports_not_navigation(self):
        crawler = CRAWLERS['doi-gov-budget-briefs'](None)
        soup = BeautifulSoup('<ul class="menu"><li><a href="/bpp">Home</a></li></ul>'
                             '<article><ul><li><a href="/budget/appropriations/2027/highlights">FY 2027 Budget in Brief</a></li></ul></article>', 'html.parser')
        items = crawler._extract_list_items(soup, crawler._config['list_page']['selectors'])
        self.assertEqual([i['title'] for i in items], ['FY 2027 Budget in Brief'])

    def test_guidance_saves_documents_not_portals_and_deduplicates(self):
        crawler = CRAWLERS['doi-gov-guidance'](None)
        def response(url, body):
            result = requests.Response()
            result.status_code = 200
            result.url = url
            result._content = body.encode()
            return result
        crawler._request = Mock(side_effect=[
            response(crawler.START_URL, '<main><a href="/solicitor/opinions">SOL Guidance Documents</a>'
                     '<a href="https://unrelated.example/">Navigation</a></main>'),
            response('https://www.doi.gov/solicitor/opinions', '<main><a href="/docs/opinion.pdf">Opinion A</a>'
                     '<a href="/docs/opinion.pdf">Duplicate</a><a href="/about">About us</a></main>'),
        ])
        crawler._save_paper = Mock()
        self.assertEqual(crawler.crawl(), 1)
        paper = crawler._save_paper.call_args.args[0]
        self.assertEqual(paper['title'], 'Opinion A')
        self.assertEqual(paper['pdf_url'], 'https://www.doi.gov/docs/opinion.pdf')
        self.assertEqual(crawler._request.call_count, 2)

    def test_guidance_does_not_save_error_page(self):
        crawler = CRAWLERS['doi-gov-guidance'](None)
        crawler._request = Mock(return_value=None)
        crawler._save_paper = Mock()
        self.assertEqual(crawler.crawl(), 0)
        crawler._save_paper.assert_not_called()

    def test_gsi_selects_publications_and_normalizes_observed_date(self):
        crawler = CRAWLERS['gsi-ie'](None)
        soup = BeautifulSoup('<nav><a href="/publications/">Publications</a></nav>'
            '<div class="publication-item"><div class="publication-thumbnail"><a href="/blank"></a></div>'
            '<div class="publication-content"><h3><a href="/publications/publication/report/">Report</a></h3>'
            '<em>Published 21 July 2026</em></div></div>', 'html.parser')
        items = crawler._extract_list_items(soup, crawler._config['list_page']['selectors'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Report')
        self.assertEqual(items[0]['published_date'], '2026-07-21')
        self.assertTrue(items[0]['detail_url'].endswith('/publication/report/'))

    def test_pasteur_redesign_list_date_and_pagination(self):
        crawler = CRAWLERS['pasteur-fr-en'](None)
        soup = BeautifulSoup('<main><article class="teaser -news"><h3 class="teaser__heading">'
            '<a href="/en/whats-new/press-area/press-releases-and-press-kits/report">Research news</a></h3>'
            '<div class="teaser__meta">16 July 2026 · 3 min de lecture</div>'
            '<p class="teaser__label">Press Release</p></article>'
            '<li class="pager__item--next"><a href="?page=1">Next</a></li></main>', 'html.parser')
        items = crawler._parse_list(soup)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['listed_date'], '2026-07-16')
        self.assertEqual(items[0]['category'], 'Press Release')
        self.assertTrue(crawler._has_next_page(soup))
        self.assertEqual(crawler._list_url(1), crawler.START_URL + '?page=1')
        self.assertFalse(crawler._has_next_page(BeautifulSoup('<main/>', 'html.parser')))

    def test_pasteur_redesign_extracts_body_without_footer(self):
        crawler = CRAWLERS['pasteur-fr-en'](None)
        html = '<main><h1>Research news</h1><p class="hero__meta"><time>16 July 2026</time></p>'
        html += '<section class="paragraph--type--texte-reprise"><p>First research paragraph.</p></section>'
        html += '<section class="paragraph--type--texte-reprise"><p>Second research paragraph.</p></section>'
        html += '</main><footer><p>Unrelated footer</p></footer>'
        result = crawler._parse_detail(BeautifulSoup(html, 'html.parser'), html, {}, 'https://www.pasteur.fr/en/report')
        self.assertEqual(result['published_date'], '2026-07-16')
        self.assertIn('Second research paragraph.', result['abstract'])
        self.assertNotIn('Unrelated footer', result['abstract'])

    def test_baw_stores_live_document_url_without_inventing_day(self):
        crawler = CRAWLERS['baw-de'](None, delay=0)
        pdf = 'https://izw.baw.de/reports/2024.pdf'
        crawler._fetch_items = Mock(return_value=[{'title': 'Annual Report 2024', 'pdf_url': pdf}])
        crawler._curl_get_bytes = Mock(return_value=b'%PDF-test')
        crawler._extract_pdf_text = Mock(return_value='A real report paragraph. ' * 20)
        crawler._save_paper = Mock()
        self.assertEqual(crawler.crawl(limit=1), 1)
        paper = crawler._save_paper.call_args.args[0]
        self.assertEqual(paper['url'], pdf)
        self.assertIsNone(paper['published_date'])
        self.assertEqual(json.loads(paper['metadata'])['year'], '2024')


if __name__ == '__main__':
    unittest.main()
