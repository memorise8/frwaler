import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_onera import OneraAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE = 'onera-fr-fr'
ROOT = 'https://www.onera.fr'
BODY = 'La recherche scientifique permet de développer de nouvelles technologies aérospatiales. ' * 4


def listing(docs, page):
    ids = docs[page*3:page*3+3]
    cards = ''.join(f'<div class="home-card"><a class="stretched-link" href="/fr/presse/item-{i}"></a>'
                    f'<div class="title">Publication {i}</div><div class="meta">{i:02}/09/2026</div></div>' for i in ids)
    pager = f'<a rel="next" href="?page={page+1}">Voir plus</a>' if (page+1)*3 < len(docs) else ''
    empty = '<div class="view-empty">Aucun communiqué de presse présent sur le site</div>' if not ids else ''
    return f'<div class="view-id-espace_presse view-display-id-liste_communiques">{cards}{pager}{empty}</div>'


def detail(i, *, body=BODY, node=None, pdf=False):
    file = f'<a href="/sites/default/files/item-{i}.pdf">PDF</a>' if pdf else ''
    return (f'<h1>Publication {i}</h1><link rel="shortlink" href="/node/{node or 1000+i}">'
            f'<meta property="og:url" content="{ROOT}/fr/presse/item-{i}"><div class="bg-white">'
            f'<div class="date">Publié le {i:02}/09/2026</div><div class="wysiwyg">{body}</div>{file}</div>')


def response_for(url, docs, **kwargs):
    assert kwargs.get('headers', {}).get('Accept-Encoding') == 'gzip, deflate'
    path = urlsplit(url)
    if '/fr/presse/item-' in path.path:
        return Mock(text=detail(int(path.path.rsplit('-', 1)[-1])))
    return Mock(text=listing(docs, int(parse_qs(path.query)['page'][0])))


class OneraIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite')
        self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn)
        init_schema(self.conn)
        self.cls = CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn, SITE, 'ONERA', ROOT)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(OneraAdapter, 'rows', 3))
        self.docs = list(range(12, 0, -1))
        self.calls = []

    def instance(self):
        c = self.cls(self.conn, delay=0)
        self.addCleanup(c._session.close)
        def request(url, **kwargs):
            self.calls.append(url)
            return response_for(url, self.docs, **kwargs)
        c._request = request
        return c, adapter_for(c)

    def collect(self, limit=None, mode=None):
        c, a = self.instance()
        if mode:
            c.delivery_mode = mode
        col = Collector(c, a, Policy(full_check_days=0))
        return col.run(limit), col

    def test_full_then_repeat_preserves_native_ids_and_avoids_details(self):
        self.assertEqual(self.collect()[0], 12)
        rows = list(self.conn.execute('SELECT post_number,published_date,published_date_raw FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows], [str(1000+i) for i in range(1, 13)])
        self.assertEqual(rows[0][1:], ('2026-09-01', 'Publié le 01/09/2026'))
        self.assertTrue(Store(self.conn, SITE).state()['baseline_complete'])
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(col.stats['detail_requests'], 0)
        self.assertEqual(col.stats['stop_reason'], 'known_boundary_plus_overlap')

    def test_resume_delivers_only_pending_without_refetching_listing(self):
        self.assertEqual(self.collect(3)[0], 3)
        state = Store(self.conn, SITE).state()
        self.assertFalse(state.get('baseline_complete', False))
        self.assertTrue(state['discovery_exhausted'])
        self.calls.clear()
        self.assertEqual(self.collect()[0], 9)
        self.assertEqual(len(self.calls), 9)
        self.assertTrue(all('/fr/presse/item-' in u for u in self.calls))

    def test_new_records_are_stored_oldest_first(self):
        self.collect()
        self.docs = [15, 14, 13]+self.docs
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 3)
        self.assertEqual(col.stats['detail_requests'], 3)
        rows = list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id DESC LIMIT 3'))
        self.assertEqual([r[0] for r in rows], ['1015', '1014', '1013'])

    def test_oldest_first_route_is_inferred_but_requires_full_rescans(self):
        self.docs.reverse()
        self.assertEqual(self.collect()[0], 12)
        state = Store(self.conn, SITE).state()
        self.assertEqual(state['order'], 'oldest_first')
        self.assertEqual(state['support'], 'full_rescan_required')
        self.assertEqual(state['unsupported_reason'], 'newest_end_unavailable')

    def test_missing_pager_cannot_hide_remaining_rows(self):
        c, a = self.instance()
        original = c._request
        def request(url, **kwargs):
            r = original(url, **kwargs)
            if url.endswith('page=0'):
                r.text = r.text.replace('<a rel="next" href="?page=1">Voir plus</a>', '')
            return r
        c._request = request
        with self.assertRaisesRegex(ValueError, 'adjacent records'):
            a.fetch(None, 'native')

    def test_repeated_page_and_changed_anchor_are_rejected(self):
        c, a = self.instance()
        first = a.fetch(None, 'native')
        self.docs[3] = self.docs[0]
        with self.assertRaisesRegex(ValueError, 'repeated cross-page'):
            a.fetch(first.next_cursor, 'native')
        self.docs[0] = 13
        with self.assertRaisesRegex(ValueError, 'previous listing changed'):
            a.fetch(first.next_cursor, 'native')

    def test_native_pdf_fallback_and_transport_restore_on_error(self):
        c, a = self.instance()
        item = c._parse_list(c._soup(listing([1], 0), 'test'))[0]
        c._request = lambda *args, **kwargs: Mock(text=detail(1, body='Short', pdf=True))
        original_fetch, original_request = c._fetch, c._session.request
        with patch.object(c, '_abstract_from_pdf', return_value=(BODY, {'abstract_source': 'publisher_pdf_text_excerpt'})) as extract:
            paper = a.materialize(item)
            self.assertEqual(paper['abstract'], BODY)
            extract.assert_called_once_with(ROOT+'/sites/default/files/item-1.pdf')
        self.assertEqual(c._fetch, original_fetch)
        with patch.object(c, '_abstract_from_pdf', side_effect=ConnectionError('fixture failed')):
            with self.assertRaises(ConnectionError):
                a.materialize(item)
        self.assertEqual(c._fetch, original_fetch)
        self.assertEqual(c._session.request, original_request)

    def test_missing_node_or_changed_canonical_is_not_saved(self):
        c, a = self.instance()
        item = c._parse_list(c._soup(listing([1], 0), 'test'))[0]
        c._request = lambda *args, **kwargs: Mock(text=detail(1).replace('href="/node/1001"', 'href="/other"'))
        with self.assertRaises(UnsupportedSource):
            a.materialize(item)
        c._request = lambda *args, **kwargs: Mock(text=detail(1).replace('content="'+ROOT, 'content="https://other.example'))
        with self.assertRaises(UnsupportedSource):
            a.materialize(item)


if __name__ == '__main__':
    unittest.main()
