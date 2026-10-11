import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from crawler import db_libertree
from crawler.incremental import Collector, Policy, Store, UnsupportedSource, init_schema
from crawler.incremental_asn import ASNAdapter, BASE, SCOPES, NOT_FOUND
from crawler.sites import CRAWLERS
from delivery.incremental_registry import adapter_for

SITE = 'asn-fr-information'
BODY = 'This controlled report describes findings and evidence in sufficient detail for the original native parser. '


def url(scope, i): return BASE+SCOPES[scope]['path']+'/item-'+str(i)
def posted(scope, i): return f'{i:02d}/09/{2026 if scope == "annual" else 2025}'
def identity(scope, i): return (1000 if scope == 'annual' else 2000)+i


def listing(scope, docs, page):
    last = max(1, (len(docs)+2)//3)
    if page > last: return f'<html><h1>{NOT_FOUND}</h1></html>', 404
    cards = ''.join(f'<article class="Teaser"><a class="Teaser-titleLink" href="{url(scope,i)}">Report {scope} {i}</a>'
                    f'<p class="Teaser-date">Publié le {posted(scope,i)}</p><div class="Teaser-text">'
                    f'<div class="eztext-field">{BODY}</div></div></article>' for i in docs[(page-1)*3:page*3])
    pager = ''
    if last > 1:
        links = ''.join(f'<li><a href="{SCOPES[scope]["path"]}?page={n}">{n}</a></li>' for n in range(1,last+1) if n != page)
        nxt = f'<li class="next"><a href="{SCOPES[scope]["path"]}?page={page+1}">Next</a></li>' if page < last else ''
        pager = f'<ul class="pagination"><li class="active">{page} (current)</li>{links}{nxt}</ul>'
    return (f'<html><h1>{SCOPES[scope]["title"]}</h1><div class="ListPage"><div class="Wrapper-gridMain">'
            f'<h3>{len(docs)} publications</h3>{pager}{cards}{pager}</div></div></html>', 200)


def detail(scope, i, *, content_id=None, body=BODY):
    key = identity(scope, i) if content_id is None else content_id
    return (f'<html><link rel="canonical" href="{url(scope,i)}"><div class="ArticleHeader"><h1>Report {scope} {i}</h1><p class="ArticleHeader-date">'
            f'Publié le {posted(scope,i)}</p></div><div class="Wrapper-gridMain"><div class="RichText">'
            f'<div class="ezrichtext-field">{body}</div></div></div>'
            f'<a href="/content/download/{key}/v1/file/report-{i}.pdf">PDF</a></html>')


def response_for(target, docs, **kwargs):
    p = urlsplit(target); scope = next(s for s in SCOPES if p.path.startswith(SCOPES[s]['path']))
    if p.path == SCOPES[scope]['path']:
        raw, status = listing(scope, docs[scope], int(parse_qs(p.query).get('page', ['1'])[0]))
    else: raw, status = detail(scope, int(p.path.rsplit('-',1)[-1])), 200
    r = requests.Response(); r.url = target; r.status_code = status; r._content = raw.encode(); r.encoding = 'utf-8'
    return r


class ASNIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.conn = db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite'); self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn); init_schema(self.conn); self.cls = CRAWLERS[SITE]
        db_libertree.upsert_site(self.conn,SITE,self.cls.site_name,self.cls.base_url)
        self.enterContext(patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'sqlite'}))
        self.enterContext(patch.object(ASNAdapter, 'rows', 3))
        self.docs = {'expertise': [3,2,1], 'annual': list(range(12,0,-1))}
        self.calls = []; self.transform = lambda u, r:r

    def instance(self):
        c = self.cls(self.conn, delay=0); self.addCleanup(c._session.close)
        def request(method, target, **kwargs):
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['headers']['Accept-Encoding'], 'gzip, deflate')
            self.calls.append(target); return self.transform(target, response_for(target,self.docs,**kwargs))
        c._session.request = request
        return c, adapter_for(c)

    def collect(self, limit=None, full=False, max_pages=100):
        c,a = self.instance()
        if full: c.delivery_mode = 'full'
        col = Collector(c,a,Policy(full_check_days=0,max_pages=max_pages))
        return col.run(limit), col

    def test_both_scopes_resume_oldest_first_and_repeat_avoids_details(self):
        self.assertEqual(self.collect(3)[0],3)
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))
        self.calls.clear(); self.assertEqual(self.collect()[0],12); self.assertEqual(self.calls,[])
        rows = list(self.conn.execute('SELECT post_number,listed_date FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows], [str(identity('expertise',i)) for i in range(1,4)]+[str(identity('annual',i)) for i in range(1,13)])
        self.calls.clear(); saved,col = self.collect()
        self.assertEqual(saved,0); self.assertEqual(col.stats['detail_requests'],0)
        self.assertEqual(col.stats['stop_reason'],'known_boundary_plus_overlap')
        self.assertEqual(len(self.calls),5)
        self.assertFalse(any('rapports-de-l-asnr?page=3' in u for u in self.calls))

    def test_older_new_expertise_item_is_not_hidden_by_known_annual_head(self):
        self.collect(); self.docs['expertise'].insert(0,4)
        self.assertEqual(self.collect()[0],1)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM documents WHERE post_number=?',(str(identity('expertise',4)),)).fetchone())

    def test_new_items_in_both_scopes_are_delivered_oldest_first(self):
        self.collect(); self.docs['expertise'].insert(0,4); self.docs['annual'].insert(0,13)
        self.assertEqual(self.collect()[0],2)
        keys = [r[0] for r in self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id')]
        self.assertEqual(keys[-2:],['2004','1013'])

    def test_oldest_first_original_route_is_full_rescan(self):
        self.docs['annual'].reverse(); self.collect()
        self.assertEqual(Store(self.conn,SITE).state()['support'],'full_rescan_required')

    def test_first_last_dates_do_not_hide_interior_reversal(self):
        self.docs['annual'][4:6] = self.docs['annual'][4:6][::-1]; self.collect()
        self.assertEqual(Store(self.conn,SITE).state()['support'],'full_rescan_required')

    def test_anchor_change_prevents_partial_discovery_completion(self):
        self.collect(max_pages=1); self.docs['annual'][0] = 13
        with self.assertRaisesRegex(ValueError,'previous page changed'): self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_duplicate_native_file_id_across_two_source_pages_is_held(self):
        def changed(u,r):
            if u == url('expertise',3): r._content = detail('expertise',3,content_id=1012).encode()
            return r
        self.transform = changed
        with self.assertRaisesRegex(UnsupportedSource,'shared_by_multiple'): self.collect()
        self.assertFalse(Store(self.conn,SITE).state().get('baseline_complete',False))

    def test_expected_404_must_have_original_end_page_shape(self):
        def changed(u,r):
            if r.status_code == 404: r._content = b'<html><h1>Access denied</h1></html>'
            return r
        self.transform = changed
        with self.assertRaisesRegex(ValueError,'adjacent boundary'): self.collect()

    def test_unexpected_404_on_nonterminal_page_is_not_exhaustion(self):
        def changed(u,r):
            if u.endswith('rapports-de-l-asnr?page=2'): r.status_code = 404
            return r
        self.transform = changed
        with self.assertRaises(requests.HTTPError): self.collect()

    def test_total_mismatch_is_rejected(self):
        def changed(u,r):
            r._content = r.content.replace(b'12 publications',b'13 publications'); return r
        self.transform = changed
        with self.assertRaisesRegex(ValueError,'pager mismatch|row count mismatch'): self.collect()

    def test_date_missing_and_invalid_native_date_are_rejected(self):
        def changed(u,r):
            r._content = r.content.replace(b'12/09/2026',b'31/02/2026'); return r
        self.transform = changed
        with self.assertRaisesRegex(UnsupportedSource,'precise_card_date'): self.collect()

    def test_detail_failure_cannot_use_teaser_as_success(self):
        def changed(u,r):
            if '/item-' in u:r._content = b'<html><h1>Challenge</h1></html>'
            return r
        self.transform = changed
        with self.assertRaisesRegex(ValueError,'detail shape'): self.collect()

    def test_wrong_detail_canonical_is_rejected(self):
        def changed(u,r):
            if '/item-' in u:r._content=r.content.replace(b'rel="canonical"',b'rel="alternate"')
            return r
        self.transform=changed
        with self.assertRaisesRegex(UnsupportedSource,'canonical_identity'):self.collect()

    def test_native_short_body_is_recorded_rechecked_and_recovered_by_full_check(self):
        def changed(u,r):
            if u.endswith('rapports-d-expertise'):r._content=r.content.replace(BODY.encode(),b'Short')
            if u == url('expertise',1):r._content=detail('expertise',1,body='Short').encode()
            return r
        self.transform=changed;self.assertEqual(self.collect(3)[0],3)
        self.calls.clear();self.assertEqual(self.collect()[0],11)
        self.assertEqual(self.calls,[url('expertise',1)])
        self.calls.clear();saved,col=self.collect();self.assertEqual(saved,0)
        self.assertEqual([u for u in self.calls if '/item-' in u],[url('expertise',1)])
        self.assertEqual(set(Store(self.conn,SITE).state()['cursor']['exclusions']),{url('expertise',1)})
        self.transform=lambda u,r:r;self.assertEqual(self.collect(full=True)[0],1)
        self.assertEqual(Store(self.conn,SITE).state()['cursor']['exclusions'],{})

    def test_full_check_reports_edits_and_missing_without_changing_documents(self):
        self.collect(); before=list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id'))
        self.docs['annual'].remove(1)
        def changed(u,r):
            if u == url('annual',2):r._content=r.content.replace(BODY.encode(),(BODY+'Changed.').encode())
            return r
        self.transform=changed; self.assertEqual(self.collect(full=True)[0],0)
        changes=[tuple(r) for r in self.conn.execute('SELECT item_key,kind FROM crawl_incremental_changes')]
        self.assertIn((url('annual',1),'missing'),changes);self.assertIn((url('annual',2),'modified'),changes)
        self.assertEqual(list(self.conn.execute('SELECT * FROM documents ORDER BY seq_id')),before)


if __name__ == '__main__':unittest.main()
