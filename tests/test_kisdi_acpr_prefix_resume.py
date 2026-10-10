"""Complete source prefixes and persisted rows must agree before a cursor resumes."""
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tests.test_delivery_site_new_counts import SiteContract
from tests import test_kisdi_delivery_scopes as kisdi
from tests import test_acpr_delivery_scopes as acpr
from crawler.base_crawler import CrawlCancelled


class PrefixChecks:
    BACKEND = 'sqlite'
    count = SiteContract.count
    identities = SiteContract.identities

    def setUp(self):
        SiteContract.setUp(self)
        self.changed_first_id = False
        self.repeated_last_page_id = False

    def crawler(self, cursor=None):
        is_kisdi = self.SITE == 'kisdi-re-kr-report'
        klass = self.module.KisdiReKrReportCrawler if is_kisdi else self.module.ACPRBanqueFranceCrawler
        c = klass(self.conn, delay=0); self.addCleanup(c._session.close)
        c.delivery_mode = 'backfill'; c.delivery_cursor = cursor
        def html(*args, **kwargs):
            url = args[1] if is_kisdi else args[0]
            parsed = urlsplit(url); q = parse_qs(parsed.query)
            if is_kisdi:
                scope = next(s for s,(_,m) in kisdi.SCOPES.items() if m == q['arrMasterId'][0])
                if parsed.path == '/report/list.do':
                    ids = [str(i) for i in range(101,109)] if scope == 'basic' else None
                    if ids and self.changed_first_id: ids[0] = '109'
                    if ids and self.repeated_last_page_id: ids[6] = '101'
                    return kisdi.listing(scope,int(q['pageIndex'][0]),ids=ids)
                return kisdi.detail(q['artId'][0])
            for scope,path in acpr.PATHS.items():
                if parsed.path == path:
                    ids = [f's{i}' for i in range(1,9)] if scope == 'studies' else None
                    if ids and self.changed_first_id: ids[0] = 's9'
                    if ids and self.repeated_last_page_id: ids[6] = 's1'
                    return acpr.listing(scope,int(q['page'][0]),ids=ids)
            return acpr.detail(parsed.path.rsplit('/',1)[-1])
        self.fetch = Mock(side_effect=html)
        if is_kisdi: c._curl = self.fetch
        else: c._curl_get = self.fetch
        return c

    def delete_fixture_row(self, uid):
        marker = '%s' if self.BACKEND == 'postgres' else '?'
        self.conn.execute(f'DELETE FROM documents WHERE site_id={marker} AND post_number={marker}', (self.SITE,uid));self.conn.commit()

    def first_id(self): return '101' if self.SITE == 'kisdi-re-kr-report' else 's1'

    def test_missing_earlier_page_row_stops_before_details(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor
        self.delete_fixture_row(self.first_id());before=self.identities();c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.identities(),before);self.assertIsNone(c.delivery_pending_cursor)

    def test_equal_count_first_page_change_outside_neighbor_anchors_is_held(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor
        self.changed_first_id=True;before=self.identities();c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.identities(),before);self.assertIsNone(c.delivery_pending_cursor)

    def test_missing_row_in_previous_scope_is_held(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=9),9);cursor=c.delivery_pending_cursor
        self.delete_fixture_row(self.first_id());before=self.identities();c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.identities(),before);self.assertIsNone(c.delivery_pending_cursor)

    def test_missing_row_within_current_page_prefix_is_held(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=5),5);cursor=c.delivery_pending_cursor
        self.delete_fixture_row('105' if self.SITE == 'kisdi-re-kr-report' else 's5');before=self.identities();c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.identities(),before);self.assertIsNone(c.delivery_pending_cursor)

    def test_legacy_cursor_is_verified_then_upgraded_without_reset(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=dict(c.delivery_pending_cursor)
        self.assertIn('prefix_hash',cursor);cursor.pop('prefix_hash');before=self.identities()
        c=self.crawler(cursor);self.assertEqual(c.crawl(),6);self.assertTrue(c.delivery_exhausted)
        self.assertEqual(c.delivery_pending_cursor['version'],1)
        self.assertEqual(len(c.delivery_pending_cursor['prefix_hash']),64)
        self.assertTrue(all(self.identities()[uid]==seq for uid,seq in before.items()))

    def test_prefix_hash_mismatch_or_invalid_type_is_held(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor
        for value,reason in [('0'*64,'invalid_response'),(False,'configuration')]:
            c=self.crawler(dict(cursor,prefix_hash=value));self.assertEqual(c.crawl(),0)
            self.assertEqual(c.delivery_source_failure,reason);self.assertIsNone(c.delivery_pending_cursor)
            if reason=='configuration':self.fetch.assert_not_called()

    def test_duplicate_later_source_page_is_not_completion(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor
        self.repeated_last_page_id=True;c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertFalse(c.delivery_exhausted);self.assertEqual(self.count(),6)

    def test_prefix_verification_budget_does_not_advance_cursor(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor;c=self.crawler(cursor)
        with patch.object(self.module,'_WALL_SECONDS',1),patch.object(self.module.time,'monotonic',side_effect=lambda:2 if self.fetch.called else 0):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
        self.assertEqual(self.count(),6)

    def test_cancel_during_prefix_verification_propagates(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);cursor=c.delivery_pending_cursor;c=self.crawler(cursor)
        c.delivery_should_cancel=lambda:self.fetch.called
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertIsNone(c.delivery_pending_cursor);self.assertEqual(self.count(),6)


class KisdiPrefixResume(PrefixChecks, unittest.TestCase): SITE='kisdi-re-kr-report'
class AcprPrefixResume(PrefixChecks, unittest.TestCase): SITE='acpr-banque-france-fr-fr'

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class KisdiPrefixResumePostgres(KisdiPrefixResume): BACKEND='postgres'

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AcprPrefixResumePostgres(AcprPrefixResume): BACKEND='postgres'
