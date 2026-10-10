"""CSO monthly links, exact URL identity, and preservation of historical rows."""
import os
import unittest
from unittest.mock import patch

from crawler.db_backend import get_backend
from tests.test_delivery_site_new_counts import SiteContract


URL = 'https://www.cso.ie/en/csolatestnews/pressreleases/2025pressreleases/statement-one/'
URL2 = URL.replace('statement-one', 'statement-two')
MODULE = '''<div class="conferencePapers"><div class="moduleBody"><ul>
<li><a href="%s" id="640995_link_1"><i></i>First report - 30 December 2025</a></li>
<li><a href="%s" id="640995_link_2">Second &amp; final report - 23 December 2025</a></li>
</ul></div></div>''' % (URL, URL2)
LEGACY = '''<span id="d.en.123456"></span><ul class="links"><li>
<a href="%s">First report</a><span class="externalSource">- 30 December 2025</span>
</li></ul>''' % URL.replace('https://www.cso.ie', '')


class CsoCurrentLayout(unittest.TestCase):
    SITE = 'cso-ie-en'
    BACKEND = 'sqlite'
    setUp = SiteContract.setUp
    count = SiteContract.count

    def crawler(self, html=MODULE):
        c = self.module.CsoIeEnCrawler(self.conn)
        self.addCleanup(c._session.close)
        c.delivery_mode = 'full'
        c._delay = 0
        self.enterContext(patch.object(self.module, '_EARLIEST_YEAR', self.module.datetime.now().year))
        self.enterContext(patch.object(self.module, '_curl_get', return_value=html))
        self.enterContext(patch.object(self.module, '_extract_detail', return_value={
            'abstract': 'Controlled body for persistence contracts. ' * 5,
            'published_date': '2025-12-30',
        }))
        return c

    def row(self, seq):
        mark = '%s' if self.BACKEND == 'postgres' else '?'
        cursor = self.conn.execute(f'SELECT * FROM documents WHERE seq_id={mark}', (seq,))
        row = cursor.fetchone()
        return dict(row) if hasattr(row, 'keys') else dict(zip([c[0] for c in cursor.description], row))

    def historical(self):
        seq = get_backend().insert_document(self.conn, dict(
            site_id=self.SITE, post_number='retained-original-id', meta_url=URL,
            title='Historical title', published_date='2025-12-30', listed_date='2025-12-30',
            pdf_downloaded=1, text_extracted=1, pdf_sha256='a' * 64,
            summary='Retained translated summary', original_filename='kept.pdf',
        ))
        return seq

    def test_month_group_and_link_position_are_not_document_ids(self):
        for parse in (self.module._parse_list_page, self.module._parse_list_regex):
            rows = parse(MODULE)
            self.assertEqual([r['url'] for r in rows], [URL, URL2])
            self.assertEqual([r['node_id'] for r in rows], [None, None])
            self.assertEqual(rows[1]['title'], 'Second & final report')
            self.assertEqual(rows[0]['listed_date'], '2025-12-30')
            self.assertEqual(rows[0]['listing_anchor'], '640995_link_1')

    def test_legacy_node_ids_win_when_both_layouts_link_the_same_release(self):
        for parse in (self.module._parse_list_page, self.module._parse_list_regex):
            rows = parse(LEGACY + MODULE)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['node_id'], '123456')
            self.assertIsNone(rows[1]['node_id'])

    def test_bad_dates_external_links_and_navigation_are_not_monthly_items(self):
        for html in (
            MODULE.replace('www.cso.ie', 'example.org'),
            MODULE.replace('30 December 2025', '31 February 2025').replace('23 December 2025', 'No date'),
            MODULE.replace('640995_link_', 'nav_'),
        ):
            self.assertEqual(self.module._parse_list_page(html), [])
            self.assertEqual(self.module._parse_list_regex(html), [])

    def test_new_null_ids_repeat_and_changed_link_positions_do_not_duplicate(self):
        self.assertEqual(self.crawler().crawl(limit=1), 1)
        self.assertEqual(self.crawler(MODULE.replace('640995_link_', '999999_link_')).crawl(), 1)
        self.assertEqual(self.crawler().crawl(), 0)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents WHERE post_number IS NULL').fetchone()[0]
                         if self.BACKEND == 'sqlite' else self.conn.execute('SELECT count(*) AS n FROM documents WHERE post_number IS NULL').fetchone()['n'], 2)

    def test_historical_id_seq_dates_file_flags_and_summary_are_preserved(self):
        seq = self.historical()
        before = self.row(seq)
        self.assertEqual(self.crawler().crawl(), 1)
        self.assertEqual(self.row(seq), before)
        self.assertEqual(self.count(), 2)
        self.assertEqual(get_backend().find_by_dedup_key(self.conn, self.SITE, None, URL), seq)

    def test_return_to_legacy_layout_keeps_existing_null_id_row(self):
        self.crawler().crawl(limit=1)
        seq = get_backend().find_by_dedup_key(self.conn, self.SITE, None, URL)
        before = self.row(seq)
        self.assertEqual(self.crawler(LEGACY).crawl(), 0)
        self.assertEqual(self.row(seq), before)

    def test_conflicting_historical_rows_are_held_without_merge(self):
        seq = self.historical()
        before = self.row(seq)
        mark = '%s' if self.BACKEND == 'postgres' else '?'
        self.conn.execute(f'INSERT INTO documents(site_id,post_number,meta_url,title) VALUES ({mark},{mark},{mark},{mark})',
                          (self.SITE, 'another-original-id', URL, 'Second historical row'))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'cso_identity_collision_requires_review'):
            get_backend().find_by_dedup_key(self.conn, self.SITE, None, URL)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.row(seq), before)

    def test_other_sites_keep_their_existing_dedup_contract(self):
        backend = get_backend()
        backend.upsert_site(self.conn, 'other-site', 'Other', 'https://fixture.invalid')
        doc = dict(site_id='other-site', post_number='one', meta_url=URL, title='One')
        first = backend.insert_document(self.conn, doc)
        second = backend.insert_document(self.conn, dict(doc, post_number='two'))
        self.assertNotEqual(first, second)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class CsoCurrentLayoutPostgres(CsoCurrentLayout):
    BACKEND = 'postgres'

    def test_translation_and_file_queue_references_survive_missing_source_number(self):
        from delivery.db.schema import init_catalogue_schema, init_delivery_schema
        from delivery.worker import jobs
        from delivery.files import jobs as file_jobs
        init_catalogue_schema(self.conn)
        init_delivery_schema(self.conn)
        seq = self.historical()
        self.conn.execute("INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version,state,translation_text) VALUES (%s,'title','en-US',%s,'fixture','fixture','completed','Retained translation')", (seq, 'a' * 64))
        self.conn.commit()
        job_id = jobs.enqueue_job(self.conn, self.SITE, mode='backfill', process_files=True)
        self.assertEqual(jobs.claim_next_job(self.conn)['id'], job_id)
        file_jobs.enqueue(self.conn, job_id, seq)
        document = self.row(seq)
        translation = dict(self.conn.execute('SELECT * FROM document_translations').fetchone())
        file_job = dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone())
        self.assertEqual(self.crawler().crawl(), 1)
        self.assertEqual(self.row(seq), document)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM document_translations').fetchone()), translation)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone()), file_job)
