"""CIRAD and Ifremer native HTML with real storage, dates and restarts."""
import json,os,re,unittest
from unittest.mock import Mock
from urllib.parse import parse_qs,urlsplit
from tests.test_coupled_site_counts import CoupledContract

DATES=[('2025',None,'year','partial'),('juillet 2025',None,'month','partial'),
       ('2025-02-30',None,'day','invalid'),('2025-07-11T12:34:56Z','2025-07-11','day','normalized'),
       ('2025-07-11 suffix',None,None,'unrecognized'),('03/04/2025',None,'day','ambiguous'),
       ('11 juillet 2025','2025-07-11','day','normalized'),('2025-07',None,'month','partial')]

class FrenchContract(CoupledContract):
    def test_zero_limit_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()

    def test_raw_publication_and_listing_dates_retained(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            with self.subTest(raw=raw):
                values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
                self.assertEqual(values,(value,raw,precision,status,raw))

    def test_failed_listing_reported(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')

    def test_failed_detail_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw) if self.is_list(url) else None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_repeated_listing_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(self.first_list,**kw) if self.is_list(url) else original(url,**kw)
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_excluded_first_page_still_reaches_next_page(self):
        self.excluded=set(range(1,6));self.assertEqual(self.crawler().crawl(),3)


class CiradCounts(FrenchContract,unittest.TestCase):
    SITE='cirad-fr-espace-presse';LIST_METHOD='_fetch'
    first_list='https://www.cirad.fr/espace-presse/communiques-de-presse'
    def is_list(self,url):return url.split('?')[0]==self.first_list
    def crawler(self):
        c=self.configured(self.module.CiradFrEspacePresseCrawler(self.conn))
        def fetch(url,**kw):
            if self.is_list(url):
                page=int(parse_qs(urlsplit(url).query).get('page',['0'])[0])
                return '<main>'+''.join(f'<article class="Card"><a class="Card-link" href="/espace-presse/communiques-de-presse/2025/report-{n}">Open</a><h3 class="Card-title">Report {n}</h3><span class="Card-date">{DATES[n-1][0]}</span></article>' for n in {0:range(1,6),1:range(6,9)}.get(page,[]))+('<a rel="next" href="?page=1">Next</a>' if page==0 else '')+'</main>'
            n=int(url.rsplit('-',1)[1]);text='' if n in getattr(self,'excluded',set()) else 'Research results. '*15
            return f'<h1 class="Hero-title">Report {n}</h1><meta property="article:published_time" content="{DATES[n-1][0]}"><div class="Hero-desc">'+text+'</div>'
        c._fetch=self.fetch=Mock(side_effect=fetch);return c

    def test_source_next_link_is_followed(self):
        self.assertEqual(self.crawler().crawl(),8)
        urls=[call.args[0] for call in self.fetch.call_args_list]
        self.assertIn(self.first_list+'?page=1',urls);self.assertNotIn(self.first_list+'?page=2',urls)

    def test_out_of_scope_pagination_is_not_followed(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw).replace('href="?page=1"','href="https://other.invalid/?page=1"')
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.assertFalse(any('other.invalid' in call.args[0] for call in self.fetch.call_args_list))


class IfremerCounts(FrenchContract,unittest.TestCase):
    SITE='ifremer-fr-fr';LIST_METHOD='_curl_get'
    first_list='https://www.ifremer.fr/fr/espace-presse'
    def is_list(self,url):return url.split('?')[0]==self.first_list
    def crawler(self):
        c=self.configured(self.module.IfremerFrFrCrawler(self.conn))
        def fetch(url,**kw):
            if self.is_list(url):
                page=int(parse_qs(urlsplit(url).query).get('page',['0'])[0])
                return '<div id="press-releases">'+''.join(f'<div class="views-row"><article class="node--type-press"><a class="node-title" href="/fr/presse/report-{n}">Report {n}</a><time datetime="{DATES[n-1][0]}">Different display value</time></article></div>' for n in {0:range(1,6),1:range(6,9)}.get(page,[]))+'</div>'+('<ul class="pager-show-more"><li><a href="?page=1">More</a></li></ul>' if page==0 else '')
            n=int(url.rsplit('-',1)[1]);text='' if n in getattr(self,'excluded',set()) else 'Ocean research evidence. '*12
            return f'<article class="node--type-press"><h1>Report {n}</h1><time datetime="{DATES[n-1][0]}">Different detail display</time><div class="field--name-field-teaser">'+text+'</div></article><script type="application/json" data-drupal-selector="drupal-settings-json">'+json.dumps({'path':{'currentPath':f'node/{n}'}})+'</script>'
        c._curl_get=self.fetch=Mock(side_effect=fetch);return c

    def test_display_only_time_retains_source_date(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:re.sub(r'<time datetime="([^"]+)">[^<]*</time>',lambda m:'<time>'+m[1]+'</time>',original(url,**kw))
        self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,date in zip(rows,DATES):
            self.assertEqual(tuple(row.values()) if isinstance(row,dict) else tuple(row),(date[0],date[0]))


for cls in (CiradCounts,IfremerCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
