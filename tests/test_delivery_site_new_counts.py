"""Site crawl loops + real SQLite saves; source responses are controlled fixtures.

This verifies run limits and restart behavior, not live parsing or PDF integrity.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree

ROOT = Path(__file__).resolve().parents[1]


class InterruptedAfterCommit(BaseException):
    pass


class SiteContract:
    BACKEND = 'sqlite'

    def setUp(self):
        source_name = getattr(self, 'SOURCE_NAME', self.SITE)
        spec = importlib.util.spec_from_file_location('fixture_' + self.SITE.replace('-', '_'), ROOT/'crawler/sites/custom'/f'{source_name}.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        backend = db_libertree
        if self.BACKEND == 'postgres':
            from crawler import db_pg
            backend = db_pg
            self.conn = db_pg.open_db(os.environ['TEST_PG_DSN'])
            self.conn.execute('DROP SCHEMA public CASCADE')
            self.conn.execute('CREATE SCHEMA public')
            self.conn.commit()
        else:
            self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        backend.init_db(self.conn)
        backend.upsert_site(self.conn, self.SITE, self.SITE, 'https://fixture.invalid')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(self.module.time, 'sleep'))

    def count(self):
        row = self.conn.execute('SELECT count(*) AS n FROM documents').fetchone()
        return row['n'] if isinstance(row, dict) else row[0]

    def identities(self):
        rows = self.conn.execute('SELECT post_number,seq_id FROM documents').fetchall()
        return {r['post_number']: r['seq_id'] for r in rows} if rows and isinstance(rows[0], dict) else dict(rows)

    def configured(self, c):
        c.delivery_mode = 'full'
        return c

    @staticmethod
    def ids_for_url(url):
        page = int(parse_qs(urlsplit(url).query).get('page', ['0'])[0])
        return {0: range(1, 6), 1: range(6, 9)}.get(page, [])

    def test_repeat_three_reaches_remainder_and_then_zero(self):
        for total, returned in ((3, 3), (6, 3), (8, 2), (8, 0)):
            self.assertEqual(self.crawler().crawl(limit=3), returned)
            self.assertEqual(self.count(), total)
        self.assertEqual(len(self.identities()), 8)

    def test_interrupt_immediately_after_commit_and_restart(self):
        c = self.crawler()
        def after_save(seq):
            if self.count() == 3:
                raise InterruptedAfterCommit()
        c.delivery_document_saved = after_save
        with self.assertRaises(InterruptedAfterCommit):
            c.crawl()
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.crawler().crawl(), 5)
        self.assertEqual(self.count(), 8)

    def test_one_save_failure_is_recovered_on_overlap_restart(self):
        c = self.crawler()
        original = c._save_paper
        attempts = []
        def save(paper):
            attempts.append(1)
            if len(attempts) == 4:
                raise RuntimeError('injected save failure before commit')
            return original(paper)
        with patch.object(c, '_save_paper', side_effect=save):
            self.assertEqual(c.crawl(), 7)
        before = self.identities()
        self.assertEqual(self.crawler().crawl(), 1)
        after = self.identities()
        self.assertEqual(len(after), 8)
        self.assertTrue(all(after[key] == value for key, value in before.items()))


class AcmaCounts(SiteContract, unittest.TestCase):
    SITE = 'acma-gov-au-publications'

    def crawler(self):
        c = self.configured(self.module.AcmaGovAuPublicationsCrawler(self.conn))
        # Two sections overlap: duplicate source records cannot consume the
        # requested new-row budget or cause logical duplicates in storage.
        c.SECTIONS = [('https://fixture.invalid/list', 'Publications'), ('https://fixture.invalid/other', 'Media')]
        c._fetch = lambda url: f'<div data-url="{url}"></div>'
        c._parse_listing = lambda soup: [{'url': f'https://fixture.invalid/doc/{n}', 'n': n} for n in self.ids_for_url(soup.div['data-url'])]
        c._has_next_page = lambda soup: 'page=' not in soup.div['data-url']
        c._scrape_detail = lambda item, category: {'external_id': str(item['n']), 'title': f"Research {item['n']}", 'url': item['url'], 'abstract': 'Scientific abstract. ' * 10}
        return c


class AcccCounts(SiteContract, unittest.TestCase):
    SITE = 'accc-gov-au-search'

    def crawler(self):
        c = self.configured(self.module.ACCCGovAuSearchCrawler(self.conn))
        self.enterContext(patch.object(self.module, '_curl_get', side_effect=lambda url: url))
        self.enterContext(patch.object(self.module, '_parse_list_page', side_effect=lambda url: [{'url': f'https://www.accc.gov.au/doc/{n}', 'title': f'Research {n}'} for n in self.ids_for_url(url)]))
        self.enterContext(patch.object(self.module, '_parse_detail_page', return_value={'abstract': 'Scientific abstract. ' * 10, 'pdf_url': ''}))
        return c


class AcademieCounts(SiteContract, unittest.TestCase):
    SITE = 'academie-sciences-fr-espace-presse'

    def crawler(self):
        c = self.configured(self.module.AcademieSciencesFrEspacePresseCrawler(self.conn))
        c._fetch_text = lambda url, **kwargs: f'<div data-url="{url}"></div>'
        def items(soup, page, url):
            return ([{'pdf_url': f'https://www.academie-sciences.fr/files/{n}.pdf', 'title': f'Research document {n}', 'original_filename': f'{n}.pdf', 'filename_slug': str(n), 'listed_date': '2026-06-05'} for n in self.ids_for_url(url)], page == 1)
        c._parse_list_items = items
        c._fetch_bytes = lambda *args, **kwargs: b'fixture bytes: PDF decoding is outside this test'
        c._extract_pdf_text = lambda *args: 'Scientific research title\n' + 'A sufficiently long account of scientific research and its published results. ' * 20
        return c


class AcccAboutCounts(SiteContract, unittest.TestCase):
    SITE = 'accc-gov-au-about-us'

    def crawler(self):
        c = self.configured(self.module.AcccGovAuAboutUsCrawler(self.conn))
        c._fetch_list_page = lambda page: str(page)
        c._parse_list_page = lambda page: [{'url': f'https://www.accc.gov.au/doc/{n}', 'n': n} for n in {0: range(1, 6), 1: range(6, 9)}.get(int(page), [])]
        c._fetch_detail = lambda url: url
        c._parse_detail = lambda raw, item: {'external_id': str(item['n']), 'title': f"Research {item['n']}", 'url': item['url'], 'abstract': 'Scientific abstract. ' * 10}
        return c


class AmfCounts(SiteContract, unittest.TestCase):
    SITE = 'amf-france-org-fr'

    def crawler(self):
        c = self.configured(self.module.AMFFranceOrgFrCrawler(self.conn))
        c._fetch_sitemap_urls = lambda: [(f'https://www.amf-france.org/fr/doc/{n}', '2026-06-05') for n in range(1, 9)]
        self.enterContext(patch.object(self.module, '_curl_get', side_effect=lambda url: (url, {})))
        self.enterContext(patch.object(self.module, '_extract_article', side_effect=lambda html, url: {'external_id': url.rsplit('/', 1)[-1], 'title': 'Scientific research', 'abstract': 'Scientific abstract. ' * 10}))
        return c


class GobAgencyCounts(SiteContract, unittest.TestCase):
    SITE = 'gob-mx-agricultura'
    SOURCE_NAME = 'gob-mx-prensa'

    def crawler(self):
        c = self.configured(self.module._VARIANTS[self.SITE](self.conn))
        population = getattr(self, 'population', 8)
        page_size = getattr(self, 'page_size', 5)
        def listing(url, **kwargs):
            page = int(parse_qs(urlsplit(url).query)['page'][0])
            ids = range((page-1)*page_size+1, min(page*page_size, population)+1)
            return ' '.join(f'<a href="/{c.AGENCY}/prensa/record-{n}">Record {n}</a>' for n in ids)
        c._get = listing
        c._fetch_article = lambda path: {'external_id': path.rsplit('/', 1)[-1], 'title': 'Scientific research', 'url': 'https://www.gob.mx'+path, 'abstract': 'Scientific abstract. ' * 10}
        return c

    def test_twenty_thousand_known_then_three_three_remaining(self):
        self.population, self.page_size = 20200, 1000
        c = self.crawler()
        placeholder = '%s' if self.BACKEND == 'postgres' else '?'
        cursor = self.conn.cursor()
        cursor.executemany('INSERT INTO documents (site_id,post_number,meta_url,title) VALUES ('+','.join([placeholder]*4)+')',
                           [(self.SITE, f'record-{n}', f'https://www.gob.mx/{c.AGENCY}/prensa/record-{n}', 'Existing research') for n in range(1, 20001)])
        self.conn.commit()
        for added, total in ((3, 20003), (3, 20006), (194, 20200)):
            c = self.crawler()
            c.delivery_mode = 'incremental'
            self.assertEqual(c.crawl(limit=added), added)
            self.assertEqual(self.count(), total)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class AcmaPostgresCounts(AcmaCounts):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class AcccPostgresCounts(AcccCounts):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class AcademiePostgresCounts(AcademieCounts):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class AcccAboutPostgresCounts(AcccAboutCounts):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class AmfPostgresCounts(AmfCounts):
    BACKEND = 'postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class GobAgencyPostgresCounts(GobAgencyCounts):
    BACKEND = 'postgres'


if __name__ == '__main__':
    unittest.main()
