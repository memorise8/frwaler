"""National Data Library list fields and the existing persistence contract."""
import os
import unittest
from html import escape
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from tests.test_uk_public_contracts import DataGovCounts, DATES


class DataGovCurrentLayoutCounts(DataGovCounts):
    def crawler(self):
        crawler = super().crawler()
        legacy_fetch = crawler._curl_get

        def fetch(url, **kwargs):
            if 'package_show' in url or 'package_search' in url:
                return legacy_fetch(url, **kwargs)
            page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            ids = {1: range(1, 6), 2: range(6, 9)}.get(page, [])
            cards = ''.join(
                '<li class="datagovuk-search-results__item">'
                f'<h2><a href="/dataset/record-{n}">Research {n}</a></h2>'
                '<div class="datagovuk-search-results__item-row">'
                '<div class="datagovuk-search-results__item-label">Publisher</div>'
                '<div class="datagovuk-search-results__item-value">Fixture publisher</div></div>'
                '<div class="datagovuk-search-results__item-row">'
                '<div class="datagovuk-search-results__item-label">Updated</div>'
                f'<div class="datagovuk-search-results__item-value">{escape(DATES[n-1][0])}</div></div>'
                '<div class="datagovuk-!-colour-grey">A source list summary...</div></li>'
                for n in ids
            )
            return '<main><ul>'+cards+'</ul>'+(
                '<a rel="next" href="/search?page=2">Next page</a>' if page == 1 else ''
            )+'</main>'

        crawler._curl_get = fetch
        return crawler

    def test_current_HTML_uses_its_own_membership_without_API_fallback(self):
        crawler = self.crawler()
        crawler._fetch_api_list_page = Mock(side_effect=AssertionError('unexpected API fallback'))
        self.assertEqual(crawler.crawl(), 8)
        crawler._fetch_api_list_page.assert_not_called()

    def test_HTML_visible_text_is_not_decoded_or_stripped_twice(self):
        crawler = self.crawler()
        title = 'OSNI Causeway Coast &amp; Glens <subset>'
        publisher = 'Research &amp; Evidence'
        summary = 'Values < 20 > 10 and &amp; markers are source text.'
        for layout in ['legacy', 'current']:
            with self.subTest(layout=layout):
                if layout == 'legacy':
                    css = 'dgu-results__result'
                    metadata = f'<dl><dt>Published by</dt><dd>{escape(publisher)}</dd></dl>'
                    description = f'<p>{escape(summary)}</p>'
                else:
                    css = 'datagovuk-search-results__item'
                    metadata = (
                        '<div class="datagovuk-search-results__item-row">'
                        '<div class="datagovuk-search-results__item-label">Publisher</div>'
                        f'<div class="datagovuk-search-results__item-value">{escape(publisher)}</div></div>'
                    )
                    description = f'<div class="datagovuk-!-colour-grey">{escape(summary)}</div>'
                raw = (f'<div class="{css}"><h2><a href="/dataset/record-1">'
                       f'{escape(title)}</a></h2>{metadata}{description}</div>')
                rows, _, _ = crawler._parse_html_list(raw, self.module._START_URL)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['title'], title)
                self.assertEqual(rows[0]['publisher'], publisher)
                self.assertEqual(rows[0]['abstract'], summary)

    def test_current_fields_keep_updated_date_role_and_source_summary(self):
        crawler = self.crawler()
        rows, has_next, next_url = crawler._parse_html_list(
            crawler._curl_get(self.module._START_URL), self.module._START_URL
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]['publisher'], 'Fixture publisher')
        self.assertEqual(rows[0]['listed_date_raw'], '2025')
        # Partial source dates stay raw here; DateContract verifies that
        # persistence records a NULL normalized day and year precision.
        self.assertEqual(rows[0]['listed_date'], '2025')
        self.assertEqual(rows[0]['abstract'], 'A source list summary...')
        self.assertNotIn('published_date', rows[0])
        self.assertTrue(has_next)
        self.assertEqual(next_url, 'https://www.data.gov.uk/search?page=2')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class DataGovCurrentLayoutCountsPostgres(DataGovCurrentLayoutCounts):
    BACKEND = 'postgres'
