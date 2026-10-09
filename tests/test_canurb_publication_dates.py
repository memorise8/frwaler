"""Preserve report publication dates separately from webpage posting dates."""
import json,os,unittest
from tests import test_publication_press_contracts as press

class CanurbPublicationDates(press.CanurbCounts):
    def test_detail_publication_precision_is_not_replaced_by_web_post_day(self):
        c=self.crawler();fetch=self.fetch.side_effect;papers={};save=c._save_paper
        values=['July 2026','2017','October 1 2021','August 18, 2021','not specified','03/04/2025','July 2025','']
        expected=[(None,'partial','month'),(None,'partial','year'),('2021-10-01','normalized','day'),('2021-08-18','normalized','day'),(None,'unrecognized',None),(None,'ambiguous','day'),(None,'partial','month'),(None,'missing',None)]
        def detail(url,**kw):
            raw=fetch(url,**kw)
            if self.is_list(url):return raw
            n=int(url.rstrip('/').rsplit('-',1)[1])
            return raw+'<aside class="single__date"><h3>Date:</h3><div>2000-01-01</div></aside><article><div class="single__info single__date"><h3>Date:</h3><div>'+values[n-1]+'</div></div></article>'
        def emit(paper):papers[str(paper['external_id'])]=dict(paper);return save(paper)
        self.fetch.side_effect=detail;c._save_paper=emit;self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT post_number,published_date,published_date_raw,published_date_status,published_date_precision,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,raw,result,web in zip(rows,values,expected,press.DATES):
            fields=('post_number','published_date','published_date_raw','published_date_status','published_date_precision','listed_date_raw')
            r=row if isinstance(row,dict) else dict(zip(fields,row))
            self.assertEqual((r['published_date'],r['published_date_status'],r['published_date_precision']),result)
            self.assertEqual(r['published_date_raw'],raw);self.assertEqual(r['listed_date_raw'],web[0])
            meta=json.loads(papers[r['post_number']]['metadata'])
            self.assertEqual(meta['publication_date_source'],'detail_publication_date')
            self.assertEqual(meta['web_date_published_raw'],web[0])

    def test_modified_label_is_not_used_as_publication_date(self):
        c=self.crawler();fetch=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:fetch(url,**kw)+'<article><div class="single__date"><h3>Updated:</h3><div>2026-10-10</div></div></article>'
        self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw FROM documents ORDER BY seq_id').fetchall()
        actual=[r['published_date_raw'] if isinstance(r,dict) else r[0] for r in rows]
        self.assertEqual(actual,[r[0] for r in press.DATES])

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class CanurbPublicationDatesPostgres(CanurbPublicationDates):
    BACKEND='postgres'
