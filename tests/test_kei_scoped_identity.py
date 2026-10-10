"""Both storage backends preserve KEI rows while accepting scoped board IDs."""
import os
import sqlite3
import unittest
from urllib.parse import urlencode

from crawler.document_identity import kei_document_id
from crawler.kei_source import BASE, SCOPES
from crawler.db_backend import get_backend
from tests.test_delivery_site_new_counts import SiteContract


def source_url(scope, uid='123', page=1):
    path, mid, name = SCOPES[scope]
    board = scope.startswith('board-')
    return BASE+path+'?'+urlencode(dict(mid=mid, act='view', nPage=page,
                                      **{'bid' if board else 'elibName':name,
                                         'list_no' if board else 'c_id':uid}))


class KeiScopedIdentity(unittest.TestCase):
    SITE = 'kei-re-kr-eliblistes'
    BACKEND = 'sqlite'

    def setUp(self):
        SiteContract.setUp(self)
        if self.BACKEND == 'sqlite':
            self.conn.row_factory = sqlite3.Row
        self.backend = get_backend()

    def doc(self, scope='board-0009', uid='123', **kw):
        return dict(site_id=self.SITE, post_number=f'{scope}:{uid}',
                    meta_url=source_url(scope, uid, **kw), title='Original title',
                    abstract='Original source body', published_date=None,
                    listed_date='2026-09-30 14:41')

    def rows(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY seq_id')]

    def raw_insert(self, post, url):
        mark = '%s' if self.BACKEND == 'postgres' else '?'
        self.conn.execute(f'INSERT INTO documents(site_id,post_number,meta_url,title) VALUES ({mark},{mark},{mark},{mark})',
                          (self.SITE, post, url, 'Historical row'))
        self.conn.commit()

    def test_all_eleven_scopes_can_share_a_number_without_sharing_a_document(self):
        ids = []
        for scope in SCOPES:
            self.assertEqual(kei_document_id(source_url(scope)), (scope, '123'))
            ids.append(self.backend.insert_document(self.conn, self.doc(scope)))
        self.assertEqual(len(set(ids)), 11)
        before = self.rows()
        for scope, seq in zip(SCOPES, ids):
            self.assertEqual(self.backend.insert_document(self.conn, self.doc(scope, page=99)), seq)
        self.assertEqual(self.rows(), before)

    def test_legacy_catalogue_row_is_found_by_new_scoped_source_key(self):
        original = dict(self.doc('researchreport'), post_number='123')
        seq = self.backend.insert_document(self.conn, original)
        before = self.rows()
        self.assertEqual(self.backend.insert_document(self.conn, self.doc('researchreport', page=44)), seq)
        self.assertEqual(self.rows(), before)
        self.assertNotEqual(self.backend.insert_document(self.conn, self.doc('environmentalforum')), seq)
        self.assertNotEqual(self.backend.insert_document(self.conn, self.doc('board-0009')), seq)
        self.assertEqual(self.rows()[0], before[0])

    def test_legacy_catalogue_alias_containing_colon_remains_supported(self):
        doc = dict(self.doc('researchreport'), post_number='legacy-import:123')
        seq = self.backend.insert_document(self.conn, doc)
        before = self.rows()
        self.assertEqual(self.backend.insert_document(self.conn, dict(doc, meta_url=source_url('researchreport', page=77))), seq)
        self.assertEqual(self.backend.insert_document(self.conn, self.doc('researchreport')), seq)
        self.assertEqual(self.rows(), before)

    def test_historical_board_ordinal_link_and_file_flags_are_preserved(self):
        self.raw_insert('old-ordinal-9', source_url('board-0028'))
        self.conn.execute("UPDATE documents SET pdf_downloaded=1,text_extracted=1,pdf_sha256='retained-checksum',summary='Retained summary'")
        self.conn.commit()
        before = self.rows()
        self.assertEqual(self.backend.insert_document(self.conn, self.doc('board-0028', page=8)), before[0]['seq_id'])
        self.assertEqual(self.rows(), before)

    def test_historical_encoded_query_key_and_id_are_found(self):
        url = source_url('board-0031').replace('list_no=123', 'list%5Fno=%31%32%33')
        self.raw_insert('historical-id', url)
        before = self.rows()
        self.assertEqual(self.backend.insert_document(self.conn, self.doc('board-0031')), before[0]['seq_id'])
        self.assertEqual(self.rows(), before)

    def test_duplicate_historical_identity_requires_review_without_merging(self):
        for page in (1, 9):
            self.raw_insert('old-'+str(page), source_url('board-0032', page=page))
        before = self.rows()
        with self.assertRaisesRegex(ValueError, 'collision_requires_review'):
            self.backend.insert_document(self.conn, self.doc('board-0032'))
        self.assertEqual(self.rows(), before)

    def test_scoped_identifier_pointing_at_another_document_is_held_even_with_a_match(self):
        self.raw_insert('old-ordinal', source_url('board-0033'))
        self.raw_insert('board-0033:123', source_url('board-0033', '456'))
        before = self.rows()
        with self.assertRaisesRegex(ValueError, 'scoped_identifier_collision_requires_review'):
            self.backend.insert_document(self.conn, self.doc('board-0033'))
        self.assertEqual(self.rows(), before)

    def test_bare_or_wrong_scope_board_ids_are_not_inserted(self):
        for post in ('123', None, 'board-0028:123', 'researchreport:123'):
            with self.subTest(post=post), self.assertRaisesRegex(ValueError, 'scoped_post_number_required'):
                self.backend.insert_document(self.conn, dict(self.doc(), post_number=post))
        self.assertEqual(self.rows(), [])

    def test_unparseable_historical_source_reference_is_held(self):
        self.raw_insert('old-ordinal', source_url('board-0054')+'&list_no=456')
        before = self.rows()
        with self.assertRaisesRegex(ValueError, 'legacy_identity_requires_review'):
            self.backend.insert_document(self.conn, self.doc('board-0054'))
        self.assertEqual(self.rows(), before)

    def test_navigation_and_search_fields_do_not_change_source_identity(self):
        seq = self.backend.insert_document(self.conn, self.doc('board-0057'))
        url = BASE+'/board.es?list_no=%31%32%33&bid=0057&mid=a10102120000&act=view&nPage=55&keyWord=water#body'
        self.assertEqual(self.backend.find_by_dedup_key(self.conn, self.SITE, 'board-0057:123', url), seq)

    def test_invalid_or_mixed_identity_fields_do_not_create_rows(self):
        original = source_url('board-0075')
        urls = [original.replace('www.kei.re.kr', 'outside.invalid'),
                original.replace('a10102130000', 'a10102020000'),
                original.replace('bid=0075', 'bid=9999'), original+'&bid=0075',
                original+'&list_no=123', original+'&c_id=123',
                original.replace('act=view', 'act=list'),
                source_url('researchreport')+'&list_no=123']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.backend.insert_document(self.conn, dict(self.doc('board-0075'), meta_url=url))
        self.assertEqual(self.rows(), [])

    def test_unrelated_site_keeps_its_existing_url_based_behavior(self):
        site = 'fixture-kei-scoped-other'
        self.backend.upsert_site(self.conn, site, site, BASE)
        first = self.backend.insert_document(self.conn, dict(self.doc(), site_id=site))
        second = self.backend.insert_document(self.conn, dict(self.doc(page=9), site_id=site))
        self.assertNotEqual(first, second)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class KeiScopedIdentityPostgres(KeiScopedIdentity):
    BACKEND = 'postgres'

    def test_scoped_repeat_preserves_translation_and_file_queue_references(self):
        from delivery.db.schema import init_catalogue_schema, init_delivery_schema
        from delivery.worker import jobs
        from delivery.files import jobs as file_jobs
        init_catalogue_schema(self.conn)
        init_delivery_schema(self.conn)
        seq = self.backend.insert_document(self.conn, self.doc('board-0081'))
        self.conn.execute('UPDATE documents SET post_number=%s WHERE seq_id=%s', ('legacy-kept-id', seq))
        self.conn.execute("INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version,state,translation_text) VALUES (%s,'title','en-US',%s,'fixture-model','fixture-prompt','completed','Retained translation')", (seq, 'a'*64))
        self.conn.commit()
        job = jobs.enqueue_job(self.conn, self.SITE, mode='backfill', process_files=True)
        self.assertEqual(jobs.claim_next_job(self.conn)['id'], job)
        file_jobs.enqueue(self.conn, job, seq)
        before = self.rows()
        translation = dict(self.conn.execute('SELECT * FROM document_translations').fetchone())
        file_job = dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone())
        self.assertEqual(self.backend.insert_document(self.conn, self.doc('board-0081', page=77)), seq)
        self.assertEqual(self.rows(), before)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM document_translations').fetchone()), translation)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone()), file_job)


if __name__ == '__main__':
    unittest.main()
