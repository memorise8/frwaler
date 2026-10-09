"""Actual DB saves with controlled public-API responses; not live-source proof."""
import contextlib
import importlib.util
import io
import math
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler import db_libertree
from crawler.base_crawler import BaseCrawler, CrawlCancelled

ROOT = Path(__file__).resolve().parents[1]


class ApiResumeContract:
    BACKEND = 'sqlite'

    def setUp(self):
        spec = importlib.util.spec_from_file_location('api_fixture_'+self.SITE.replace('-', '_'), ROOT/'crawler/sites/custom'/f'{self.SITE}.py')
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.cls = next(c for c in vars(self.module).values() if isinstance(c, type) and issubclass(c, BaseCrawler) and getattr(c, 'site_id', None) == self.SITE)
        backend = db_libertree
        if self.BACKEND == 'postgres':
            from crawler import db_pg
            backend = db_pg; self.conn = backend.open_db(os.environ['TEST_PG_DSN'])
            self.conn.execute('DROP SCHEMA public CASCADE'); self.conn.execute('CREATE SCHEMA public'); self.conn.commit()
        else:
            self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        backend.init_db(self.conn); backend.upsert_site(self.conn, self.SITE, self.SITE, 'https://fixture.invalid')
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': self.BACKEND}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(self.module.time, 'sleep'))
        self.enterContext(patch.object(self.cls, '_PAGE_SIZE', 3))

    def count(self):
        row = self.conn.execute('SELECT count(*) AS n FROM documents').fetchone()
        return row['n'] if isinstance(row, dict) else row[0]

    def identities(self):
        rows = self.conn.execute('SELECT post_number,seq_id FROM documents').fetchall()
        return {r['post_number']: r['seq_id'] for r in rows} if rows and isinstance(rows[0], dict) else dict(rows)

    def crawl(self, limit=None, *, filtered=(), cap=None, repeat=False, cancel_at=None, cancel_now=False, fail_save_at=None):
        federal = self.SITE.startswith('federalregister')
        if federal:
            records = [{'document_number': str(n), 'html_url': f'https://fixture.invalid/{n}',
                        'title': '' if n in filtered else f'Scientific report {n}', 'abstract': 'Research evidence. '*10,
                        'publication_date': '2026-01-05'} for n in range(9)]
        else:
            records = [{'ID': str(n), 'Title': '' if n in filtered else f'Annual Report {n}',
                        'ContentType': 'annual_report', 'UrlSlug': f'report-{n}', 'Entity': 'Research department',
                        'PublicationDate': '2026-01-05', 'ReportingYear': '2026'} for n in range(9)]
        self.offsets = []; crawler = self.cls(self.conn); self.last_crawler = crawler
        self.addCleanup(crawler._session.close); crawler.delivery_mode = 'incremental'
        if fail_save_at is not None:
            actual_save = crawler._save_paper; attempts = []
            def save(paper):
                attempts.append(1)
                if len(attempts) == fail_save_at: raise RuntimeError('injected failure before DB commit')
                return actual_save(paper)
            crawler._save_paper = save
        if cancel_at is not None: crawler.delivery_should_cancel = lambda: self.count() >= cancel_at
        if cancel_now: crawler.delivery_should_cancel = lambda: True
        if federal:
            def request(url, params):
                p = dict(params); size = int(p['per_page']); size = min(size, cap) if cap else size
                offset = (int(p['page'])-1)*size; self.offsets.append(offset)
                if repeat: offset = 0
                response = Mock(); response.json.return_value = {'results': records[offset:offset+size], 'count': 9, 'total_pages': math.ceil(9/size)}
                return response
            crawler._request = request
        else:
            def fetch(offset, size):
                self.offsets.append(offset)
                if repeat: offset = 0
                size = min(size, cap) if cap else size
                return records[offset:offset+size]
            crawler._fetch_page = fetch
        return crawler.crawl(limit)

    def test_repeated_limits_reach_remainder_then_zero_with_same_identities(self):
        prior = {}
        for expected_new, expected_total in ((2, 2), (2, 4), (2, 6), (2, 8), (1, 9), (0, 9)):
            self.assertEqual(self.crawl(2), expected_new); self.assertEqual(self.count(), expected_total)
            after = self.identities(); self.assertTrue(all(after[k] == v for k,v in prior.items())); prior = after

    def test_excluded_middle_page_does_not_hide_later_records(self):
        self.assertEqual(self.crawl(filtered=(3, 4, 5)), 6)
        self.assertEqual(self.count(), 6); self.assertIn(6, self.offsets)

    def test_short_server_windows_reach_all_ids(self):
        self.assertEqual(self.crawl(cap=2), 9); self.assertEqual(self.count(), 9)
        self.assertEqual(self.offsets[:5], [0, 2, 4, 6, 8])

    def test_mid_page_cancel_resumes_without_duplicate_save_counts(self):
        with self.assertRaises(CrawlCancelled): self.crawl(cancel_at=2)
        prior = self.identities(); self.assertEqual(self.crawl(), 7)
        self.assertTrue(all(self.identities()[k] == v for k,v in prior.items()))

    def test_cancel_before_listing_sends_no_request(self):
        with self.assertRaises(CrawlCancelled): self.crawl(cancel_now=True)
        self.assertEqual(self.offsets, [])

    def test_repeated_source_page_reports_failure_not_exhaustion(self):
        self.assertEqual(self.crawl(repeat=True), 3)
        self.assertEqual(len(self.offsets), 2)
        self.assertEqual(self.last_crawler.delivery_source_failure, 'invalid_response')
        self.assertFalse(self.last_crawler.delivery_exhausted)

    def test_failed_row_is_reported_and_recovered_on_overlap_restart(self):
        self.assertEqual(self.crawl(fail_save_at=4), 8)
        self.assertEqual(self.last_crawler.delivery_source_failure, 'item_failed')
        prior = self.identities(); self.assertEqual(self.crawl(), 1)
        self.assertEqual(self.count(), 9)
        self.assertTrue(all(self.identities()[k] == v for k,v in prior.items()))

    def test_http_success_with_invalid_json_shape_is_not_empty_source(self):
        crawler = self.cls(self.conn); self.addCleanup(crawler._session.close)
        response = Mock(); response.json.return_value = {'error': 'upstream response is not a document list'}
        if self.SITE.startswith('federalregister'):
            crawler._request = Mock(return_value=response)
        else:
            crawler._session.post = Mock(return_value=response)
        self.assertEqual(crawler.crawl(), 0)
        self.assertEqual(crawler.delivery_source_failure, 'invalid_response')
        self.assertFalse(crawler.delivery_exhausted)


class FederalCursorContract:
    def cursor_crawl(self, corrupt=None):
        crawler = self.cls(self.conn); self.addCleanup(crawler._session.close)
        calls = []
        def request(url, params=None):
            query = dict(params) if params else {k:v[0] for k,v in parse_qs(urlsplit(url).query).items()}
            page = int(query['page']); calls.append(page)
            if page > 1: self.assertEqual(query['search_after_cursor'], f'cursor-{page}')
            rows = [{'document_number': str(n), 'html_url': f'https://fixture.invalid/{n}',
                     'title': f'Research {n}', 'abstract': 'Scientific evidence. '*10} for n in range((page-1)*3, page*3)]
            nxt = dict(query, page=str(page+1), search_after_cursor=f'cursor-{page+1}', format='json')
            link = crawler._API_BASE+'?'+urlencode(nxt) if page < 3 else None
            if corrupt == 'foreign': link = 'https://outside.test/api?'+urlencode(nxt)
            if corrupt == 'scope':
                nxt['conditions[agencies][]'] = 'different-agency'; link = crawler._API_BASE+'?'+urlencode(nxt)
            response = Mock(); response.json.return_value = {'results': rows, 'count': 3, 'total_pages': 1, 'next_page_url': link}
            return response
        crawler._request = request
        return crawler.crawl(), calls, crawler

    def test_server_cursor_continues_beyond_reported_page_total(self):
        saved, calls, crawler = self.cursor_crawl()
        self.assertEqual(saved, 9); self.assertEqual(calls, [1, 2, 3])
        self.assertIsNone(crawler.delivery_source_failure)

    def test_next_links_cannot_change_host_or_query_membership(self):
        for corruption in ('foreign', 'scope'):
            with self.subTest(corruption=corruption):
                _, calls, crawler = self.cursor_crawl(corruption)
                self.assertEqual(calls, [1])
                self.assertEqual(crawler.delivery_source_failure, 'invalid_response')


for name, site in [('FederalRegister', 'federalregister-gov-documents'), ('Transparency', 'transparency-gov-au-publications')]:
    bases = (FederalCursorContract, ApiResumeContract, unittest.TestCase) if name == 'FederalRegister' else (ApiResumeContract, unittest.TestCase)
    globals()[name] = type(name, bases, {'SITE': site})
    pg = type(name+'Postgres', bases, {'SITE': site, 'BACKEND': 'postgres'})
    globals()[name+'Postgres'] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del pg


if __name__ == '__main__':
    unittest.main()
