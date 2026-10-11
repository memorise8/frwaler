import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from crawler import db_libertree
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from delivery.scripts.review_afd_incremental import listing
from tests.test_incremental_accc import DENIAL

SITE='afd-fr-fr'
BASE='https://www.afd.fr'


def list_html(ids,*,dates=False):
    return '<html><input name="type[170]" value="170" checked>'+''.join(
        f'<div class="views-row"><a class="fr-card__link" href="/fr/ressources/doc-{i}">Research {i}</a>'
        +('<time datetime="2026-10-01"></time>' if dates else '')
        +'<img src="/files/2026-10/cover.png"></div>' for i in ids)+'</html>'


def detail(i):
    data=dict(pianoAnalytics={'dataLayer':dict(content_nid=str(1000+i),content_title=f'Research {i}',
        content_field_publication_date=f'2026-09-{i:02}',content_field_resource_type='Document de recherche',
        content_field_collection='Controlled series')})
    return '<html><meta name="description" content="'+('Controlled substantive research description. '*5)+'">'+f'<a href="/files/doc-{i}.pdf">PDF</a><script data-drupal-selector="drupal-settings-json">'+json.dumps(data)+'</script></html>'


class Source:
    def __init__(self):self.ids=[3,2,1];self.calls=[];self.fail=None
    def __call__(self,url,**kwargs):
        self.calls.append(url)
        if self.fail:return self.fail
        if '/publications/liste?' in url:
            page=int(parse_qs(urlsplit(url).query)['page'][0]);return list_html(self.ids[page*2:page*2+2])
        return detail(int(url.rsplit('-',1)[-1]))


class AfdNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=db_libertree.open_db(Path(self.tmp.name)/'owned.sqlite');self.addCleanup(self.conn.close)
        db_libertree.init_db(self.conn);db_libertree.upsert_site(self.conn,SITE,'AFD',BASE)
        self.cls=CRAWLERS[SITE];self.source=Source()
        self.enterContext(patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}))
        self.enterContext(patch.dict(self.cls.crawl.__globals__,{'_curl_get':self.source}))
        self.enterContext(patch('time.sleep'));self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def crawler(self):
        c=self.cls(self.conn,delay=0);self.addCleanup(c._session.close);return c

    def test_repeat_returns_zero_and_existing_prefix_does_not_consume_limit(self):
        self.assertEqual(self.crawler().crawl(),3)
        before=[tuple(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        self.assertEqual(self.crawler().crawl(),0)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM documents ORDER BY seq_id')],before)
        self.source.ids.append(4);self.assertEqual(self.crawler().crawl(limit=1),1)
        rows=list(self.conn.execute('SELECT post_number FROM documents ORDER BY seq_id'))
        self.assertEqual([r[0] for r in rows],['1003','1002','1001','1004'])

    def test_native_detail_fields_and_actual_insert_result(self):
        c=self.crawler();self.assertTrue(c._process_item(BASE+'/fr/ressources/doc-1'))
        self.assertFalse(c._process_item(BASE+'/fr/ressources/doc-1'))
        r=self.conn.execute('SELECT * FROM documents').fetchone()
        self.assertEqual(r['post_number'],'1001');self.assertEqual(r['title'],'Research 1')
        self.assertEqual(r['published_date'],'2026-09-01');self.assertEqual(r['pdf_url'],BASE+'/files/doc-1.pdf')

    def test_denial_never_becomes_zero_card_success(self):
        self.source.fail=DENIAL;c=self.crawler();self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertFalse(c.delivery_exhausted)
        self.assertEqual(len(self.source.calls),1)

    def test_detail_fetch_failure_stops_before_remaining_items(self):
        calls=[]
        def failed(url):calls.append(url);return list_html([3,2]) if '/publications/liste?' in url else None
        with patch.dict(self.cls.crawl.__globals__,{'_curl_get':failed}):
            c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertEqual(len(calls),2)

    def test_list_dates_cannot_be_invented_from_upload_paths(self):
        rows=listing(list_html([1,2]));self.assertTrue(all(not i['dates'] for i in rows))
        self.assertTrue(all(not i['slug'].isdecimal() for i in rows))
        self.assertEqual(listing(list_html([1],dates=True))[0]['dates'],['2026-10-01'])

    def test_reviewed_hold_does_not_certify_baseline(self):
        r=eligibility(SITE,self.cls);self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('listing_dates_missing',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
