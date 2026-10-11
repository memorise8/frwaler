import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_archives_nationales import ArchivesNationalesAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE = 'archives-nationales-culture-gouv-fr-presse'
BODY = 'This controlled publication has a sufficiently long original card description.'


def listing(docs, page, *, excluded=(), bad_date=None):
    ids = docs[page*3:page*3+3]
    last = max(0, (len(docs)-1)//3)
    cards = ''
    for i in ids:
        cards += (f'<article class="node--type-detail-publication"><a class="fr-card__link" '
                  f'href="https://s3.fr-par.scw.cloud/an-p0-bucket/an-public/files/item-{i}.pdf?VersionId={i}">'
                  f'<span class="field--name-title">Publication {i}</span></a><p class="fr-card__desc">'
                  f'{"" if i in excluded else BODY}</p><p class="fr-card__detail">'
                  f'Publié(e) le {bad_date if bad_date and i == 1 else str(i)+" septembre 2026"}</p></article>')
    current = min(page, last)+1
    pager = f'<a aria-current="page">{current}</a>'
    for n in range(last+1):
        pager += f'<a href="?{ArchivesNationalesAdapter.query}&page={n}">{n+1}</a>'
    if page < last:
        pager += f'<a class="fr-pagination__link--next" href="?{ArchivesNationalesAdapter.query}&page={page+1}">Next</a>'
    return (f'<html><title>Presse - Page {current} sur {last+1} - Archives nationales</title>'
            '<div id="block-archives-nationales-content"><input checked name="field_type_publication_target_id_1[32]" value="32">'
            '<select name="created[value]"><option selected value="">All</option></select>'
            f'{cards}<nav class="fr-pagination">{pager}</nav></div></html>')


class ArchivesIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite'); self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn); init_schema(self.conn)
        self.cls = CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn, SITE, self.cls.site_name, self.cls.base_url)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(ArchivesNationalesAdapter, 'rows', 3))
        self.docs, self.calls, self.excluded = list(range(12, 0, -1)), [], set()
        self.transform = lambda page, html: html

    def instance(self):
        c = self.cls(self.conn, delay=0); self.addCleanup(c._session.close)
        def request(url, **kwargs):
            self.assertEqual(kwargs['headers']['Accept-Encoding'], 'gzip, deflate')
            self.calls.append(url)
            page = int(parse_qs(urlsplit(url).query).get('page', ['0'])[0])
            return Mock(text=self.transform(page, listing(self.docs, page, excluded=self.excluded)))
        c._request = request
        return c, adapter_for(c)

    def collect(self, limit=None, full=False, max_pages=100):
        c, a = self.instance()
        if full: c.delivery_mode = 'full'
        col = Collector(c, a, Policy(full_check_days=0, max_pages=max_pages))
        return col.run(limit), col

    def test_full_resume_repeat_preserves_oldest_first_and_avoids_remaining_pages(self):
        self.assertEqual(self.collect(3)[0], 3)
        state = Store(self.conn, SITE).state()
        self.assertTrue(state['discovery_exhausted']); self.assertFalse(state.get('baseline_complete', False))
        self.assertEqual(len(self.calls), 8)
        self.calls.clear(); self.assertEqual(self.collect()[0], 9); self.assertEqual(self.calls, [])
        rows = list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows], [str(i) for i in range(1, 13)])
        self.assertTrue(Store(self.conn, SITE).state()['baseline_complete'])
        self.calls.clear(); saved, col = self.collect()
        self.assertEqual(saved, 0); self.assertEqual(len(self.calls), 3)
        self.assertEqual(col.stats['stop_reason'], 'known_boundary_plus_overlap')
        self.assertEqual(col.stats['save_attempts'], 0)

    def test_new_items_are_saved_oldest_first(self):
        self.collect(); self.docs = [15, 14, 13]+self.docs
        self.assertEqual(self.collect()[0], 3)
        rows = list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows[-3:]], ['13', '14', '15'])

    def test_oldest_first_route_is_full_rescan_without_reverse_endpoint(self):
        self.docs.reverse(); self.collect()
        state = Store(self.conn, SITE).state()
        self.assertEqual(state['order'], 'oldest_first')
        self.assertEqual(state['support'], 'full_rescan_required')

    def test_interior_date_inversion_prevents_early_stop(self):
        self.docs[5:7] = self.docs[5:7][::-1]; self.collect()
        self.assertEqual(Store(self.conn, SITE).state()['support'], 'full_rescan_required')

    def test_changed_anchor_retains_pending_without_baseline(self):
        self.collect(max_pages=1)
        self.docs[0] = 13
        with self.assertRaisesRegex(ValueError, 'previous source page changed'): self.collect()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0], 3)

    def test_short_summaries_record_exclusions_and_full_check_recovers_them(self):
        self.excluded = {1, 4, 5, 6}; self.assertEqual(self.collect()[0], 8)
        state = Store(self.conn, SITE).state()
        self.assertEqual(set(state['cursor']['exclusions']), {'1', '4', '5', '6'})
        self.assertEqual(len(state['cursor']['seen']), 12)
        self.excluded = set(); self.assertEqual(self.collect(full=True)[0], 4)
        self.assertEqual(Store(self.conn, SITE).state()['cursor']['exclusions'], {})

    def test_exclusion_cannot_hide_reversed_date_on_repeat(self):
        self.excluded = {8}; self.collect()
        self.transform = lambda p, html: html.replace('le 8 septembre', 'le 18 septembre')
        with self.assertRaises(UnsupportedSource): self.collect()

    def test_stored_item_cannot_become_silent_exclusion(self):
        self.collect(); self.excluded = {12}
        with self.assertRaisesRegex(ValueError, 'existing document'): self.collect()

    def test_cross_page_duplicate_is_not_exhaustion(self):
        self.docs[3] = self.docs[2]
        with self.assertRaisesRegex(ValueError, 'cross-page duplicate'): self.collect()

    def test_challenge_page_cannot_be_empty_exhaustion(self):
        self.transform = lambda p, html: '<html><title>Challenge</title></html>' if p == 4 else html
        with self.assertRaisesRegex(ValueError, 'original press view'): self.collect()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))

    def test_generic_presse_title_requires_heading_and_original_pager_proof(self):
        self.transform = lambda p, html: html.replace('Presse - Page 3 sur 4 - Archives nationales',
                                                     'Presse').replace('<div id=', '<h1>Presse</h1><div id=')
        self.assertEqual(self.collect()[0], 12)

    def test_explicit_title_must_agree_with_pager(self):
        self.transform = lambda p, html: html.replace('Presse - Page 3 sur 4', 'Presse - Page 2 sur 4')
        with self.assertRaisesRegex(ValueError, 'page identity mismatch'): self.collect()

    def test_invalid_date_prevents_completion(self):
        self.transform = lambda p, html: html.replace('le 1 septembre 2026', 'le 31 février 2026')
        with self.assertRaisesRegex(UnsupportedSource, 'date_unavailable'): self.collect()

    def test_changed_filter_is_rejected(self):
        self.transform = lambda p, html: html.replace('value="32"', 'value="33"')
        with self.assertRaisesRegex(ValueError, 'filter echo'): self.collect()

    def test_unstable_file_key_is_rejected(self):
        self.transform = lambda p, html: html.replace('VersionId=12', 'token=12')
        with self.assertRaisesRegex(UnsupportedSource, 'identity_unverified'): self.collect()

    def test_periodic_full_detects_modified_and_missing_without_mutating_documents(self):
        self.collect(); before = list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        self.docs.remove(2)
        self.transform = lambda p, html: html.replace('Publication 1<', 'Revised publication 1<')
        saved, col = self.collect(full=True)
        self.assertEqual(saved, 0)
        changes = list(self.conn.execute('SELECT item_key,kind FROM crawl_incremental_changes'))
        self.assertIn(('1', 'modified'), [tuple(r) for r in changes])
        self.assertEqual([r[0] for r in changes if r[1] == 'missing'], ['2'])
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')), before)


if __name__ == '__main__': unittest.main()
