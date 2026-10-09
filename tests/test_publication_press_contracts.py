"""Native publication/press HTML through actual isolated database writes."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tests.test_coupled_site_counts import CoupledContract

DATES = [('2025', None, 'year', 'partial'),
         ('July 2025', None, 'month', 'partial'),
         ('2025-02-30', None, 'day', 'invalid'),
         ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
         ('2025-07-11 suffix', None, None, 'unrecognized'),
         ('03/04/2025', None, 'day', 'ambiguous'),
         ('July 11, 2025', '2025-07-11', 'day', 'normalized'),
         ('2025-07', None, 'month', 'partial')]


class PublicationContract(CoupledContract):
    def test_failed_listing_reported(self):
        c = self.crawler(); self.fetch.side_effect = lambda *a, **k: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_failed_details_reported(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: original(url, **kw) if self.is_list(url) else None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')

    def test_repeated_listing_reported(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: original(self.first_list, **kw) if self.is_list(url) else original(url, **kw)
        self.assertEqual(c.crawl(), 5)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(c.delivery_exhausted)

    def test_zero_limit_no_fetch(self):
        c = self.crawler(); self.assertEqual(c.crawl(limit=0), 0)
        self.fetch.assert_not_called()

    def test_excluded_first_page_does_not_hide_next_page(self):
        self.excluded = set(range(1, 6))
        self.assertEqual(self.crawler().crawl(), 3)
        self.assertEqual(self.count(), 3)


class CanurbCounts(PublicationContract, unittest.TestCase):
    SITE = 'canurb-org-publications'; LIST_METHOD = '_curl_get'
    first_list = 'https://canurb.org/publications/'

    def is_list(self, url):
        return url == self.first_list or '/page/' in url

    def crawler(self):
        c = self.configured(self.module.CanurbOrgPublicationsCrawler(self.conn))
        def fetch(url, **kw):
            if self.is_list(url):
                page = 2 if '/page/2/' in url else 1
                return '<main>'+''.join(
                    f'<article class="publications post-{n}"><a href="/publications/report-{n}/"><h4>Report {n}</h4></a></article>'
                    for n in (range(1, 6) if page == 1 else range(6, 9)))+(
                    '<a rel="next" href="/publications/page/2/">Next</a>' if page == 1 else '')+'</main>'
            n = int(url.rstrip('/').rsplit('-', 1)[1])
            abstract = '' if n in getattr(self, 'excluded', set()) else 'Urban research evidence. '*12
            return '<h1 class="entry-title">Report '+str(n)+'</h1><script type="application/ld+json">'+json.dumps(
                {'datePublished': DATES[n-1][0]})+'</script><meta property="og:description" content="'+abstract+'">'
        c._curl_get = self.fetch = Mock(side_effect=fetch)
        return c

    def test_source_publication_dates_keep_precision(self):
        self.assertEqual(self.crawler().crawl(), 8)
        rows = self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row, (raw, value, precision, status) in zip(rows, DATES):
            with self.subTest(raw=raw):
                self.assertEqual(tuple(row.values()) if isinstance(row, dict) else tuple(row), (value, raw, precision, status))

    def test_modified_date_is_not_publication_date(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: original(url, **kw).replace('datePublished', 'dateModified')
        self.assertEqual(c.crawl(), 8)
        for row in self.conn.execute('SELECT published_date,published_date_status FROM documents').fetchall():
            self.assertEqual(tuple(row.values()) if isinstance(row, dict) else tuple(row), (None, 'missing'))

    def test_meta_publication_date_used_when_json_only_modified(self):
        c = self.crawler()
        soup = c._make_soup('<script type="application/ld+json">{"dateModified":"2026-01-01"}</script><meta property="article:published_time" content="July 2025">')
        self.assertEqual(c._extract_published_date(soup), 'July 2025')


class OneraCounts(PublicationContract, unittest.TestCase):
    SITE = 'onera-fr-fr'; LIST_METHOD = '_fetch'
    first_list = 'https://www.onera.fr/fr/presse?page=0'
    dates = [('02/10/2026', '2026-10-02', 'day', 'normalized'),
             ('22/07/2026', '2026-07-22', 'day', 'normalized'),
             ('31/02/2025', None, 'day', 'invalid'),
             ('juillet 2025', None, 'month', 'partial'),
             ('2025', None, 'year', 'partial'),
             ('11 juillet 2025', '2025-07-11', 'day', 'normalized'),
             ('2025-07-11 suffix', None, None, 'unrecognized'),
             ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized')]

    def test_source_pagination_starts_at_zero(self):
        c = self.crawler(); c.crawl(limit=1)
        self.assertEqual(self.fetch.call_args_list[0].args[0], 'https://www.onera.fr/fr/presse?page=0')

    def is_list(self, url):
        return '?page=' in url

    def crawler(self):
        c = self.configured(self.module.OneraFrFrCrawler(self.conn))
        def fetch(url, **kw):
            if self.is_list(url):
                page = int(parse_qs(urlsplit(url).query)['page'][0])
                return '<main>'+''.join(
                    f'<div class="home-card"><a class="stretched-link" href="/fr/presse/report-{n}">Open</a><div class="title">Report {n}</div><div class="meta">{self.dates[n-1][0]}</div></div>'
                    for n in (range(1, 6) if page == 0 else range(6, 9)))+(
                    '<a rel="next" href="?page=1">Next</a>' if page == 0 else '')+'</main>'
            n = int(url.rsplit('-', 1)[1])
            abstract = '' if n in getattr(self, 'excluded', set()) else 'Aerospace research evidence. '*12
            return f'<h1>Report {n}</h1><link rel="shortlink" href="/node/{n}"><div class="bg-white"><div class="wysiwyg">'+abstract+'</div></div>'
        c._fetch = self.fetch = Mock(side_effect=fetch)
        return c

    def test_nonadvancing_paginator_reported(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: original(url, **kw).replace('href="?page=1"', 'href="?page=0"')
        self.assertEqual(c.crawl(), 5)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')

    def test_native_dmy_and_partial_dates_retained(self):
        self.assertEqual(self.crawler().crawl(), 8)
        rows = self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row, (raw, value, precision, status) in zip(rows, self.dates):
            with self.subTest(raw=raw):
                self.assertEqual(tuple(row.values()) if isinstance(row, dict) else tuple(row), (value, raw, precision, status))

    def test_partial_detail_date_not_replaced_by_full_listing_date(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: original(url, **kw) if self.is_list(url) else original(url, **kw).replace('class="bg-white">', 'class="bg-white"><div class="date">2025</div>')
        self.assertEqual(c.crawl(), 8)
        for row in self.conn.execute('SELECT published_date,published_date_raw,published_date_status FROM documents').fetchall():
            self.assertEqual(tuple(row.values()) if isinstance(row, dict) else tuple(row), (None, '2025', 'partial'))


for cls in (CanurbCounts, OneraCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
