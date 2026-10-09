"""Native entry points, including independent alternate-source paths, with real DBs."""
import json
import os
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from tests.test_coupled_site_counts import CoupledContract


def paper(n, host):
    return {'external_id':str(n),'post_number':str(n),'title':f'Research {n}',
            'url':f'https://{host}/{n}/','abstract':'Scientific research. '*10}


class EprintsCounts(CoupledContract, unittest.TestCase):
    SITE='eprints-soton-ac-uk-view'; LIST_METHOD='_get_years'
    def crawler(self):
        c=self.configured(self.module.EprintsSotonAcUkViewCrawler(self.conn));c._close_browser=Mock()
        c._get_years=lambda:['2025','2024']
        c._get_eprint_urls_for_year=lambda year:[paper(n,'eprints.soton.ac.uk')['url'] for n in (range(1,6) if year=='2025' else range(6,9))]
        c._parse_detail_page=lambda url:paper(int(url.strip('/').rsplit('/',1)[1]),'eprints.soton.ac.uk')
        return c


class NofimaCounts(CoupledContract, unittest.TestCase):
    SITE='nofima-com-publication'; LIST_METHOD='_fetch_html'
    def crawler(self):
        c=self.configured(self.module.NofimaCOMPublicationCrawler(self.conn));c._close_browser=Mock()
        def listing(url):
            page=int(url.rstrip('/').rsplit('/',1)[1]) if '/page/' in url else 1
            return '<main>'+''.join(f'<a class="card-publication" href="https://nofima.com/publication/{n}/">Research</a>' for n in {1:range(1,6),2:range(6,9)}.get(page,[]))+'</main>'
        c._fetch_html=listing
        c._fetch_detail=lambda url:paper(int(url.strip('/').rsplit('/',1)[1]),'nofima.com')
        return c


class DrdcCounts(CoupledContract, unittest.TestCase):
    SITE='pubs-drdc-rddc-gc-ca-basis'; LIST_METHOD='_site_reachable'
    def crawler(self):
        c=self.configured(self.module.PubsDrdcRddcGcCaBasisCrawler(self.conn))
        c._site_reachable=lambda:True
        c._sdf_url=lambda:'https://pubs.drdc-rddc.gc.ca/list/1'
        c._curl=lambda url,**kwargs:url
        c._parse_list_page=lambda raw:[paper(n,'pubs.drdc-rddc.gc.ca') for n in (range(1,6) if raw.endswith('/list/1') else range(6,9))]
        parse=c._parse_html
        c._parse_html=lambda raw:parse('<a href="https://pubs.drdc-rddc.gc.ca/list/2">Next</a>' if raw.endswith('/list/1') else '<main>No next</main>')
        c._parse_detail_page=lambda raw,item:dict(item)
        c._crawl_openalex=Mock(side_effect=AssertionError('primary source must not fall through'))
        return c

    def test_repeating_primary_next_link_is_stopped_and_reported(self):
        c=self.crawler();parse=c._parse_html
        c._parse_html=lambda raw:parse('https://pubs.drdc-rddc.gc.ca/list/1')
        self.assertEqual(c.crawl(),8)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.assertFalse(c.delivery_exhausted)


class DrdcAlternateCounts(DrdcCounts):
    def crawler(self):
        c=self.configured(self.module.PubsDrdcRddcGcCaBasisCrawler(self.conn));c._site_reachable=lambda:False
        def response(url,**kwargs):
            first=parse_qs(urlsplit(url).query)['cursor'][0]=='*'
            return json.dumps({'results':[{'id':f'https://openalex.org/W{n}','title':f'Research {n}',
                 'abstract_inverted_index':{'Research':list(range(20))}} for n in (range(1,6) if first else range(6,9))],
                 'meta':{'next_cursor':'second' if first else None}})
        c._curl=response
        return c

    def test_repeating_primary_next_link_is_stopped_and_reported(self):
        c=self.crawler();fetch=c._curl
        def repeated(url,**kwargs):
            data=json.loads(fetch(url,**kwargs));data['meta']['next_cursor']='*';return json.dumps(data)
        c._curl=Mock(side_effect=repeated)
        self.assertEqual(c.crawl(),5)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.assertEqual(c._curl.call_count,1)
        self.assertFalse(c.delivery_exhausted)

    def test_new_save_limit_finishes_before_checking_unused_continuation(self):
        c=self.crawler();fetch=c._curl
        def repeated(url,**kwargs):
            data=json.loads(fetch(url,**kwargs));data['meta']['next_cursor']='*';return json.dumps(data)
        c._curl=Mock(side_effect=repeated)
        self.assertEqual(c.crawl(limit=3),3)
        self.assertIsNone(c.delivery_source_failure)
        self.assertEqual(c._curl.call_count,1)


class StatsCounts(CoupledContract, unittest.TestCase):
    SITE='stats-govt-nz-publications'; LIST_METHOD='_curl_get'
    def crawler(self):
        c=self.configured(self.module.StatsGovtNzPublicationsCrawler(self.conn))
        self.enterContext(patch.object(self.module,'_PAGE_SIZE',5))
        self.enterContext(patch.object(self.module,'_parse_pv_data',side_effect=json.loads))
        def listing(url,**kwargs):
            second='start=5' in url
            return json.dumps({'totalPages':2,'PaginatedBlockPages':[{'ID':n,'PageLink':f'/research/{n}'} for n in (range(6,9) if second else range(1,6))]})
        c._curl_get=listing;c._is_blocked=lambda raw:False
        c._make_paper_from_listing_item=lambda item,url:paper(item['ID'],'stats.govt.nz')
        c._crawl_via_cc=Mock(side_effect=AssertionError('accessible primary must not fall through'))
        return c


class StatsAlternateCounts(StatsCounts):
    def crawler(self):
        c=self.configured(self.module.StatsGovtNzPublicationsCrawler(self.conn))
        self.enterContext(patch.object(self.module,'_parse_pv_data',side_effect=json.loads))
        c._curl_get=lambda *args,**kwargs:None
        c._cc_cdx_query=Mock(side_effect=lambda pattern,**kwargs:[{'url':f'https://stats.govt.nz/research/{n}','ID':n} for n in range(1,9)])
        c._cc_fetch_warc=lambda record:json.dumps(record)
        c._make_paper=lambda pv,url:paper(pv['ID'],'stats.govt.nz')
        return c

    def test_small_limit_does_not_drop_later_archive_patterns(self):
        c=self.crawler()
        def records(pattern,**kwargs):
            nums=range(1,21) if 'information-releases' in pattern else range(21,24)
            return [{'url':f'https://stats.govt.nz/research/{n}','ID':n} for n in nums]
        c._cc_cdx_query.side_effect=records
        self.assertEqual(c.crawl(limit=20),20)
        c=self.crawler();c._cc_cdx_query.side_effect=records
        self.assertEqual(c.crawl(limit=3),3)
        self.assertEqual(self.count(),23)

    def test_archived_listing_items_count_only_new_saves(self):
        for expected in (3,3,2,0):
            c=self.crawler()
            c._cc_cdx_query=lambda *args,**kwargs:[{'url':'https://stats.govt.nz/archive/list'}]
            c._cc_fetch_warc=lambda record:json.dumps({'PaginatedBlockPages':[{'ID':n,'PageLink':f'/research/{n}'} for n in range(1,9)]})
            c._make_paper_from_listing_item=lambda item,url:paper(item['ID'],'stats.govt.nz')
            self.assertEqual(c.crawl(limit=3),expected)
        self.assertEqual(self.count(),8)


for cls in (EprintsCounts,NofimaCounts,DrdcCounts,DrdcAlternateCounts,StatsCounts,StatsAlternateCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
