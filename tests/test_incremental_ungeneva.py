import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, init_schema
from crawler.incremental_ungeneva import UNGenevaAdapter
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE = 'ungeneva-org-en'
ROOT = 'https://www.ungeneva.org'
LIST = ROOT+'/en/news-media/press-release'
BODY = 'This controlled press publication describes the meeting and its conclusions in detail. ' * 4


def url(i):
    return LIST+f'/{2000+i}/09/'+('annual-review' if i in (1, 12) else f'item-{i}')


def date(i):
    return f'{2000+i}-09-01T12:00:00Z'


def listing(docs, page):
    ids = docs[page*3:page*3+3]
    last = max(0, (len(docs)-1)//3)
    cards = ''.join(f'<div class="views-row"><a class="un-listings-box" href="{url(i)}">'
                    f'<h2 class="item-title">Publication {i}</h2><time datetime="{date(i)}">Date</time></a></div>' for i in ids)
    pager = ''.join(f'<a href="?page={n}"'+(' aria-current="page"' if n == min(page, last) else '')+f'>{n}</a>' for n in range(last+1))
    if page < last:
        pager += f'<li class="pager__item--next"><a rel="next" href="?page={page+1}">Next</a></li>'
    return f'<html><h1>Press Releases</h1><div class="news-listing">{cards}<nav class="pager">{pager}</nav></div></html>'


def detail(i, *, body=BODY, french=False, alternate=True):
    canonical = url(i).replace('/en/', '/fr/') if french else url(i)
    alias = f'<link rel="alternate" hreflang="fr" href="{canonical}">' if french and alternate else ''
    return (f'<html><title>Publication {i} | The United Nations Office at Geneva</title>'
            f'<link rel="canonical" href="{canonical}">{alias}<h1>Publication {i}</h1>'
            f'<time datetime="{date(i)}">Date</time><div class="text-long">{body}</div></html>')


def response_for(target, docs, *, excluded=False, **kwargs):
    assert kwargs.get('headers', {}).get('Accept-Encoding') == 'gzip, deflate'
    if urlsplit(target).path == urlsplit(LIST).path:
        return Mock(text=listing(docs, int(parse_qs(urlsplit(target).query)['page'][0])))
    i = int(urlsplit(target).path.split('/')[-3])-2000
    return Mock(text=detail(i, body='Short' if excluded and i == 1 else BODY, french=i == 2))


class UNGenevaIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite')
        self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn)
        init_schema(self.conn)
        self.cls = CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn, SITE, 'UN Geneva', ROOT)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(UNGenevaAdapter, 'rows', 3))
        self.docs, self.calls, self.excluded = list(range(12, 0, -1)), [], False

    def instance(self):
        c = self.cls(self.conn, delay=0)
        self.addCleanup(c._session.close)
        def request(target, **kwargs):
            self.calls.append(target)
            return response_for(target, self.docs, excluded=self.excluded, **kwargs)
        c._request = request
        return c, adapter_for(c)

    def collect(self, limit=None, full=False):
        c, a = self.instance()
        if full:
            c.delivery_mode = 'full'
        col = Collector(c, a, Policy(full_check_days=0))
        return col.run(limit), col

    def test_same_slug_in_different_years_is_preserved_and_repeat_avoids_details(self):
        self.assertEqual(self.collect()[0], 12)
        rows = list(self.conn.execute('SELECT post_number,meta_url FROM documents ORDER BY seq_id'))
        self.assertEqual([r[1] for r in rows], [url(i) for i in range(1, 13)])
        self.assertEqual(sum(r[0] == 'annual-review' for r in rows), 2)
        self.assertTrue(Store(self.conn, SITE).state()['baseline_complete'])
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(col.stats['detail_requests'], 0)
        self.assertEqual(col.stats['stop_reason'], 'known_boundary_plus_overlap')

    def test_resume_uses_durable_prepared_papers_and_preserves_oldest_first(self):
        self.assertEqual(self.collect(3)[0], 3)
        state = Store(self.conn, SITE).state()
        self.assertFalse(state.get('baseline_complete', False))
        self.assertTrue(state['discovery_exhausted'])
        self.calls.clear()
        self.assertEqual(self.collect()[0], 9)
        self.assertEqual(self.calls, [])

    def test_new_items_are_fetched_once_and_saved_oldest_first(self):
        self.collect()
        self.docs = [15, 14, 13]+self.docs
        self.calls.clear()
        saved, col = self.collect()
        self.assertEqual(saved, 3)
        self.assertEqual(col.stats['detail_requests'], 3)
        rows = list(self.conn.execute('SELECT meta_url FROM documents ORDER BY seq_id DESC LIMIT 3'))
        self.assertEqual([r[0] for r in rows], [url(i) for i in (15, 14, 13)])

    def test_exclusion_is_revalidated_on_resume_and_rediscovered_on_full_check(self):
        self.excluded = True
        self.assertEqual(self.collect(3)[0], 3)
        self.calls.clear()
        self.assertEqual(self.collect()[0], 8)
        self.assertEqual(self.calls, [url(1)])
        self.assertIn(url(1), Store(self.conn, SITE).state()['cursor']['exclusions'])
        self.excluded = False
        self.assertEqual(self.collect(full=True)[0], 1)
        self.assertEqual(Store(self.conn, SITE).state()['cursor']['exclusions'], {})

    def test_changed_exclusion_keeps_pending_and_baseline_incomplete(self):
        self.excluded = True
        self.collect(3)
        self.excluded = False
        with self.assertRaisesRegex(ValueError, 'exclusion evidence changed'):
            self.collect()
        self.assertFalse(Store(self.conn, SITE).state().get('baseline_complete', False))
        self.assertGreater(self.conn.execute('SELECT count(*) FROM crawl_incremental_pending').fetchone()[0], 0)

    def test_french_canonical_requires_explicit_same_path_alternate(self):
        c, a = self.instance()
        item = {'key': url(2), 'url': url(2), 'slug': 'item-2', 'title': 'Publication 2', 'listed_date': date(2)}
        c._request = lambda *args, **kwargs: Mock(text=detail(2, french=True, alternate=False))
        with self.assertRaisesRegex(ValueError, 'canonical identity mismatch'):
            a.prepare(item)

    def test_repeated_page_boundary_and_wrong_selected_page_are_rejected(self):
        c, a = self.instance()
        first = a.fetch(None, 'native')
        self.docs[3] = self.docs[0]
        with self.assertRaisesRegex(ValueError, 'repeated cross-page'):
            a.fetch(first.next_cursor, 'native')
        wrong = listing(self.docs, 1).replace('href="?page=1" aria-current', 'href="?page=0" aria-current')
        c._request = lambda *args, **kwargs: Mock(text=wrong)
        with self.assertRaisesRegex(ValueError, 'pager mismatch'):
            a.read(1)

    def test_missing_body_container_is_not_an_exclusion(self):
        c, a = self.instance()
        item = {'key': url(2), 'url': url(2), 'slug': 'item-2', 'title': 'Publication 2', 'listed_date': date(2)}
        c._request = lambda *args, **kwargs: Mock(text=detail(2).replace('class="text-long"', 'class="changed"'))
        with self.assertRaisesRegex(ValueError, 'body shape mismatch'):
            a.prepare(item)


if __name__ == '__main__':
    unittest.main()
