import html
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_bfr import BfRAdapter, SCOPES
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE = 'bfr-bund-de-en'
ROOT = 'https://www.bfr.bund.de'
BODY = 'This controlled publication explains scientific research and the conclusions of the report. ' * 4


def record(i, scope):
    return {'uid': i, 'title': f'Report {i}', 'publishingDate': f'{i:02}.09.2026',
            'url': ROOT+f'/en/publication/item-{i}/', 'groupingName': scope,
            'download': {'url': ROOT+f'/assets/item-{i}.pdf'}, 'teaser': ''}


def api(docs, scope, page, size=3):
    items = [record(i, scope) for i in docs[scope][(page-1)*size:page*size]]
    data = {'allResultCount': len(docs[scope]), 'pagination': {'current': page, 'numberOfPages': max(1, (len(docs[scope])+size-1)//size)},
            'documents': {'list': {'count': len(items), 'results': items}},
            'facetsActive': {'selected': [{'name': 'folder', 'item': f.split(':')[1]} for f in SCOPES[scope]['filters']]}}
    return {'content': {'colPos0': [{'content': {'data': data}}]}}


def response_for(url, docs, *, excluded=False, **kwargs):
    assert kwargs.get('headers', {}).get('Accept-Encoding') == 'gzip, deflate'
    for scope, spec in SCOPES.items():
        if url == spec['url']:
            filters = html.escape(json.dumps(spec['filters']), quote=True)
            return Mock(text=f'<div id="search-app" data-endpoint="/api/v1/en/mini-suche" data-restriction-filters="{filters}"></div>')
    if '/api/' in url:
        q = parse_qs(urlsplit(url).query)
        scope = next(k for k, s in SCOPES.items() if s['filters'] == q['tx_solr[filter][]'])
        data = api(docs, scope, int(q.get('tx_solr[page]', ['1'])[0]))
        if excluded:
            for r in data['content']['colPos0'][0]['content']['data']['documents']['list']['results']:
                if r['uid'] == 6:
                    r['download'] = None
        return Mock(json=lambda: data)
    i = int(urlsplit(url).path.rstrip('/').rsplit('-', 1)[-1])
    return Mock(text=f'<html><h1>Report {i}</h1><meta name="description" content="{"" if excluded and i == 6 else BODY}"></html>')


class BfRIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite')
        self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn)
        init_schema(self.conn)
        self.cls = CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn, SITE, 'BfR', ROOT)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(BfRAdapter, 'rows', 3))
        self.docs = {'annual': list(range(20, 11, -1)), 'scientific': [8, 7, 6]}
        self.calls = []
        self.excluded = False

    def instance(self):
        c = self.cls(self.conn, delay=0)
        self.addCleanup(c._session.close)
        def request(url, **kwargs):
            self.calls.append(url)
            return response_for(url, self.docs, excluded=self.excluded, **kwargs)
        c._request = request
        return c, adapter_for(c)

    def collect(self, limit=None, full=False):
        c, a = self.instance()
        if full:
            c.delivery_mode = 'full'
        col = Collector(c, a, Policy(full_check_days=0))
        return col.run(limit), col

    def test_both_scopes_save_oldest_first_and_repeat_skips_details(self):
        self.assertEqual(self.collect()[0], 12)
        rows = list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([int(r[0]) for r in rows], sorted(self.docs['annual']+self.docs['scientific']))
        self.assertTrue(Store(self.conn, SITE).state()['baseline_complete'])
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 0)
        self.assertEqual(col.stats['detail_requests'], 0)
        self.assertTrue(all('/en/publication/item-' not in u for u in self.calls))

    def test_unknown_older_scope_head_cannot_hide_behind_newer_known_scope(self):
        self.collect()
        self.docs['scientific'].insert(0, 9)
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 1)
        self.assertEqual(col.stats['detail_requests'], 1)
        self.assertIsNotNone(self.conn.execute('SELECT seq_id FROM documents WHERE post_number=?', ('9',)).fetchone())

    def test_partial_delivery_resumes_without_listing_requests(self):
        self.assertEqual(self.collect(3)[0], 3)
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))
        self.calls.clear()
        self.assertEqual(self.collect()[0], 9)
        self.assertEqual(self.calls, [])

    def test_source_ignored_filter_or_total_change_fails_closed(self):
        c, a = self.instance()
        original = c._request
        def request(url, **kwargs):
            response = original(url, **kwargs)
            if '/api/' in url:
                data = response.json()
                data['content']['colPos0'][0]['content']['data']['facetsActive']['selected'] = []
                return Mock(json=lambda: data)
            return response
        c._request = request
        with self.assertRaisesRegex(ValueError, 'ignored catalogue filter'):
            a.fetch(None, 'native')

    def test_native_pdf_fallback_and_identity_are_preserved(self):
        c, a = self.instance()
        raw = record(6, 'scientific')
        item = c._parse_list_records({'documents': {'list': {'results': [raw]}}})[0]
        item['_source_scope'] = 'scientific'
        def request(url, **kwargs):
            if url.endswith('.pdf'):
                return Mock(content=b'%PDF- controlled transport fixture')
            return Mock(text='<html><h1>Report 6</h1></html>')
        c._request = request
        with patch.object(c, '_extract_pdf_text', return_value=BODY):
            paper, exclusion = a.prepare(item)
        self.assertIsNone(exclusion)
        self.assertEqual(paper['external_id'], '6')
        self.assertEqual(paper['url'], raw['url'])
        self.assertEqual(json.loads(paper['metadata'])['restriction_filters'], ['folder:110'])
        self.assertEqual(json.loads(paper['metadata'])['abstract_source'], 'pdf_text')

    def test_native_no_body_no_pdf_exclusion_revalidated_and_recovers_on_full_check(self):
        self.excluded = True
        self.assertEqual(self.collect(3)[0], 3)
        self.assertIn('6', Store(self.conn, SITE).state()['cursor']['exclusions'])
        self.calls.clear()
        self.assertEqual(self.collect()[0], 8)
        self.assertEqual(len(self.calls), 1)
        self.excluded = False
        self.assertEqual(self.collect(full=True)[0], 1)
        self.assertEqual(Store(self.conn, SITE).state()['cursor']['exclusions'], {})

    def test_changed_exclusion_keeps_pending_and_does_not_certify_baseline(self):
        self.excluded = True
        self.collect(3)
        self.excluded = False
        with self.assertRaisesRegex(ValueError, 'exclusion evidence changed'):
            self.collect()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 3)

    def test_zip_is_not_sent_to_pdf_extractor_or_silently_excluded(self):
        c, a = self.instance()
        raw = record(6, 'scientific')
        item = c._parse_list_records({'documents': {'list': {'results': [raw]}}})[0]
        item['_source_scope'] = 'scientific'
        def request(url, **kwargs):
            return Mock(content=b'PK\x03\x04archive') if url.endswith('.pdf') else Mock(text='<html><h1>Report 6</h1></html>')
        c._request = request
        with patch.object(c, '_extract_pdf_text') as extract:
            with self.assertRaisesRegex(UnsupportedSource, 'archive_not_pdf'):
                a.prepare(item)
            extract.assert_not_called()


if __name__ == '__main__':
    unittest.main()
