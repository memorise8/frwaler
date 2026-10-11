import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_adruk import AdrukAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for
from tests.test_adruk_delivery_scopes import BODY, ROOT, ANNUAL, ANNUAL_PDF, detail

SITE = 'adruk-org-news-publications'


def fixtures():
    return {'impact': [f'I{i}' for i in range(18, 12, -1)],
            'publications': ['P6', 'P5', 'P4', 'P3', 'P2.pdf', 'P1.pdf']}


def response_for(url, docs, **kwargs):
    native = CRAWLERS[SITE].crawl.__globals__
    if url == native['_ANNUAL_INDEX']:
        return Mock(text=f'<main><h1>Annual Reports</h1><a href="{ANNUAL}">2025-2026</a></main>')
    if url == ANNUAL:
        return Mock(text=f'<main><h1>Annual Report 2025-2026</h1><a href="{ANNUAL_PDF}">Download</a></main>')
    for scope, cfg in native['_SCOPES'].items():
        for page in range(len(docs[scope]) // 2 + 1):
            if url != native['_list_url'](scope, page):
                continue
            total = len(docs[scope]); items = docs[scope][page * 2:page * 2 + 2]; cards = []
            for uid in items:
                title = 'ADR UK Annual Report 2025 - 2026' if uid == 'P5' else 'Research ' + uid
                href = '/fileadmin/' + uid if uid.endswith('.pdf') else cfg['detail'] + uid + '/'
                date = int(uid[1:].split('.')[0])
                cards.append(f'<div class="record {cfg["model"]}"><a href="{href}"><h2>{title}</h2><span class="date">{date} September 2026</span><p>{BODY}</p></a></div>')
            first = page * 2 + 1 if total else 0; last = min((page + 1) * 2, total)
            nxt = f'<a href="{native["_list_url"](scope, page+1)}">Next »</a>' if last < total else ''
            return Mock(text=f'<p>Displaying results {first} to {last} out of {total}</p>' + ''.join(cards) + nxt)
    uid = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    return Mock(text=detail(uid, date=f'{int(uid[1:])} September 2026'))


class AdrukIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name) / 'owned.sqlite'); self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn); init_schema(self.conn)
        self.cls = CRAWLERS[SITE]; db_libertree.upsert_site(self.conn, SITE, 'ADR UK', self.cls.base_url)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(AdrukAdapter, 'rows', 2))
        self.docs = fixtures(); self.calls = []; self.policy = Policy(full_check_days=0)

    def instance(self, **kwargs):
        c = self.cls(self.conn, delay=0); self.addCleanup(c._session.close)
        def request(url, **kw):
            self.calls.append(url); return response_for(url, self.docs, **kw)
        c._request = request
        return c, AdrukAdapter(c, **kwargs) if kwargs else adapter_for(c)

    def collect(self, limit=None):
        c, a = self.instance(); col = Collector(c, a, self.policy)
        return col.run(limit), col

    def test_full_merge_oldest_save_and_constant_alias_repeat_cost(self):
        self.assertEqual(self.collect()[0], 12)
        rows = list(self.conn.execute('SELECT post_number,pdf_url,published_date_raw,listed_date_raw FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows], list(reversed(self.docs['publications'])) + list(reversed(self.docs['impact'])))
        self.assertEqual(rows[4][1], ANNUAL_PDF)
        self.assertEqual(rows[0][2:], ('1 September 2026', '1 September 2026'))
        state = Store(self.conn, SITE).state()
        self.assertTrue(state['baseline_complete']); self.assertEqual(state['order'], 'newest_first')
        self.calls = []; saved, col = self.collect()
        self.assertEqual(saved, 0); self.assertEqual(col.stats['detail_requests'], 0)
        self.assertEqual(col.stats['stop_reason'], 'known_boundary_plus_overlap')
        self.assertEqual(len(self.calls), 8)  # two aliases + two pages/anchor per scope
        self.assertEqual(col.stats['http_requests'], len(self.calls))

    def test_interruption_retains_alias_proof_and_remaining_details(self):
        self.assertEqual(self.collect(3)[0], 3)
        state = Store(self.conn, SITE).state(); self.assertTrue(state['discovery_exhausted'])
        self.assertFalse(state.get('baseline_complete', False))
        self.calls = []; self.assertEqual(self.collect()[0], 9)
        self.assertEqual(len(self.calls), 8)  # annual paper was verified and staged in discovery
        self.assertTrue(all('/article/' in u or '/publication/' in u for u in self.calls))

    def test_new_older_scope_head_is_not_hidden(self):
        self.collect(); self.docs['publications'].insert(0, 'P7'); self.calls = []
        self.assertEqual(self.collect()[0], 1)
        self.assertEqual(sum('/publication/' in u for u in self.calls), 1)

    def test_changed_alias_label_revalidates_without_extra_document(self):
        self.collect(); c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: Mock(text=native(url, **kw).text.replace('2025-2026</a>', 'Updated label</a>'))
        self.assertEqual(Collector(c, a, self.policy).run(), 0)
        # Changed index metadata invalidates only that proof and revisits its publication.
        self.assertEqual(a.detail_requests, 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 12)

    def test_changed_alias_pdf_is_reconciled_before_boundary(self):
        self.collect(); c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: Mock(text=native(url, **kw).text.replace('Annual_Report_2025-26.pdf', 'Annual_Report_2025-26_revised.pdf'))
        self.assertEqual(Collector(c, a, self.policy).run(), 0)
        self.assertEqual(a.detail_requests, 1)
        state = Store(self.conn, SITE).state()
        self.assertEqual(state['cursor']['annual_certified'][ANNUAL]['pdf_url'], ANNUAL_PDF.replace('.pdf', '_revised.pdf'))

    def test_unrepresented_annual_blocks_baseline(self):
        self.docs['publications'].remove('P5')
        with self.assertRaisesRegex(ValueError, 'not fully represented'):
            self.collect()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))

    def test_bad_annual_detail_blocks_alias_proof(self):
        c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: Mock(text=detail('P5', linked=False)) if url.endswith('/P5/') else native(url, **kw)
        c._same_pdf = Mock(return_value=False)
        with self.assertRaisesRegex(ValueError, 'annual representation mismatch'):
            Collector(c, a, self.policy).run()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))

    def test_source_failure_keeps_pending_and_does_not_certify_baseline(self):
        c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: None if url.endswith('/I13/') else native(url, **kw)
        with self.assertRaises(ConnectionError):
            Collector(c, a, self.policy).run()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 6)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0], 6)
        self.calls = []; self.assertEqual(self.collect()[0], 6)
        self.assertEqual(len(self.calls), 6)

    def test_partial_date_or_nonchronological_cards_are_unsupported(self):
        c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: Mock(text=native(url, **kw).text.replace('18 September 2026', 'September 2026'))
        with self.assertRaises(UnsupportedSource):
            Collector(c, a, self.policy).run()

    def test_nonchronological_cards_are_unsupported(self):
        self.docs['impact'].reverse()
        with self.assertRaises(UnsupportedSource): self.collect()

    def test_anchor_change_retains_cursor(self):
        c, a = self.instance(); Collector(c, a, Policy(max_pages=1, full_check_days=0)).run()
        self.docs['impact'][0] = 'I19'
        with self.assertRaisesRegex(ValueError, 'previous page changed'): self.collect()

    def test_cross_scope_slug_collision_is_unsupported(self):
        self.docs['impact'] = ['P6']
        with self.assertRaisesRegex(UnsupportedSource, 'slug_collision'): self.collect()

    def test_periodic_full_recheck_keeps_rows_and_refreshes_alias_and_details(self):
        self.collect(); before = list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')); self.calls = []
        c, a = self.instance(); c.delivery_mode = 'full'
        self.assertEqual(Collector(c, a, self.policy).run(), 0)
        self.assertEqual(a.detail_requests, 10)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')), before)

    def test_new_annual_alias_is_validated_and_saved_once(self):
        self.collect(); self.docs['publications'].insert(0, 'P7')
        c, a = self.instance(); native = c._request
        root = 'https://reports.adruk.org/annual-report-2026-2027/'
        pdf = 'https://reports.adruk.org/fileadmin/Annual_Report_2026-27.pdf'
        def request(url, **kw):
            if url == root:
                return Mock(text=f'<main><h1>Annual Report 2026-2027</h1><a href="{pdf}">Download</a></main>')
            raw = native(url, **kw).text
            if url == a.native['_ANNUAL_INDEX']:
                raw = raw.replace('</main>', f'<a href="{root}">Annual Report 2026-2027</a></main>')
            raw = raw.replace('Research P7', 'ADR UK Annual Report 2026 - 2027')
            if url.endswith('/P7/'):
                raw = raw.replace('</div></div>', f'<a href="{root}">Annual report</a></div></div>')
            return Mock(text=raw)
        c._request = request
        self.assertEqual(Collector(c, a, self.policy).run(), 1)
        row = self.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P7'").fetchone()
        self.assertEqual(row[0], pdf); self.assertEqual(a.detail_requests, 1)
        self.assertEqual(len(Store(self.conn, SITE).state()['cursor']['annual_certified']), 2)

    def test_cross_page_date_inversion_is_unsupported(self):
        self.docs['impact'][2] = 'I19'
        with self.assertRaisesRegex(UnsupportedSource, 'cross_page_date_order_changed'): self.collect()

    def test_periodic_recheck_records_changed_annual_pdf_without_overwriting(self):
        self.collect(); c, a = self.instance(); native = c._request
        c._request = lambda url, **kw: Mock(text=native(url, **kw).text.replace('Annual_Report_2025-26.pdf', 'Annual_Report_2025-26_revised.pdf'))
        c.delivery_mode = 'full'; self.assertEqual(Collector(c, a, self.policy).run(), 0)
        original = self.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P5'").fetchone()[0]
        self.assertEqual(original, ANNUAL_PDF)
        changes = self.conn.execute("SELECT kind,payload FROM crawl_incremental_changes WHERE item_key='P5'").fetchall()
        self.assertEqual(len(changes), 1); self.assertEqual(changes[0][0], 'modified')
        self.assertEqual(json.loads(changes[0][1])['pdf_url'], ANNUAL_PDF.replace('.pdf', '_revised.pdf'))

    def test_impact_only_subset_does_not_claim_annual_scope(self):
        c, a = self.instance(scopes=['impact'])
        self.assertEqual(Collector(c, a, self.policy).run(), 6)
        self.assertNotIn(c.crawl.__globals__['_ANNUAL_INDEX'], self.calls)

    def test_native_transport_restored_on_failure(self):
        c, a = self.instance(); original = c._html; session = c._session.request
        c._request = Mock(side_effect=ConnectionError('unavailable'))
        with self.assertRaises(ConnectionError): a.fetch(None, 'native')
        self.assertEqual(c._html, original); self.assertEqual(c._session.request, session)

if __name__ == '__main__': unittest.main()
