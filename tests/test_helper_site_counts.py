"""Public crawl entry points exercising nested/helper saves against real databases."""
import os
import re
import unittest
from unittest.mock import Mock, patch

from tests.test_coupled_site_counts import CoupledContract


class ArcepCounts(CoupledContract, unittest.TestCase):
    SITE = 'arcep-fr-actualites'; LIST_METHOD = '_get_chash'

    def crawler(self):
        c = self.configured(self.module.ArcepFrActualitesCrawler(self.conn))
        c._get_chash = lambda: 'fixture'; c._close_browser = Mock()
        c._curl = lambda url: url
        def listing(url):
            page = int(re.search(r'/gp-page/(\d+)', url)[1])
            return ([{'external_id': str(n), 'title': f'Research {n}', 'abstract': 'Scientific research. '*10}
                     for n in {1:range(1,6),2:range(6,9)}.get(page, [])],
                    'https://www.arcep.fr/gp-page/2.html' if page == 1 else None)
        c._parse_page = listing
        return c

    def test_filtered_first_page_does_not_hide_next_page(self):
        c = self.crawler(); parse = c._parse_page
        def filtered(raw):
            items, nxt = parse(raw)
            if nxt:
                for item in items: item['abstract'] = 'short'
            return items, nxt
        c._parse_page = filtered
        self.assertEqual(c.crawl(), 3)
        self.assertEqual(self.count(), 3)


class CivilCounts(CoupledContract, unittest.TestCase):
    SITE = 'civilprotection-gov-gr-deltia-tupou'; LIST_METHOD = '_get'

    def crawler(self):
        c = self.configured(self.module.CivilprotectionGovGrDeltia(self.conn))
        if c._cffi is not None: self.addCleanup(c._cffi.close)
        def fetch(url, **kwargs):
            if 'deltia-tupou?' not in url: return '<main>Detail</main>'
            second = 'page=1' in url
            rows = ''.join(f'<div class="views-row"><a href="/research/{n}">Research {n}</a></div>' for n in (range(6,9) if second else range(1,6)))
            more = '' if second else '<a class="cp-pagination-item next" href="?page=1%2C0">Next</a>'
            return rows+more
        c._get = fetch
        c._parse_detail = lambda soup,url,*args: {'external_id': url.rsplit('/',1)[1], 'post_number': url.rsplit('/',1)[1],
            'title':'Research', 'abstract':'Scientific research. '*10,'published_date':None,'pdf_url':None,'keywords':None,
            'category':None,'original_filename':None,'metadata':{}}
        return c


class InriaCounts(CoupledContract, unittest.TestCase):
    SITE = 'inria-fr-fr'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.InriaFrFrCrawler(self.conn))
        c._close_browser = Mock()
        c._curl_get = lambda url,**kwargs: ''.join(f'<article class="news" about="/fr/research/{n}"><h3>Research</h3></article>' for n in range(1,9))
        c._parse_detail = lambda soup,url,*args: {'external_id':url.rsplit('/',1)[1],'title':'Research','abstract':'Scientific research. '*10,
            'authors':None,'publisher':None,'category':None,'keywords':None,'published_date':None,'metadata':{}}
        return c


class TransportesCounts(CoupledContract, unittest.TestCase):
    SITE = 'transportes-gob-es-el-ministerio'; LIST_METHOD = '_curl'; LIST_MODULE = True

    def crawler(self):
        c = self.configured(self.module.TransportesGobEsElMinisterioCrawler(self.conn))
        self.enterContext(patch.object(self.module,'_curl',return_value='<main>0</main>'))
        self.enterContext(patch.object(self.module,'_make_soup',side_effect=lambda raw:raw))
        self.enterContext(patch.object(self.module,'_extract_dom_id',return_value='fixture'))
        self.enterContext(patch.object(self.module,'_fetch_ajax_soup',side_effect=lambda page,dom: str(page)))
        self.enterContext(patch.object(self.module,'_parse_listing',side_effect=lambda soup:[{'url':f'https://www.transportes.gob.es/research/{n}',
            'slug':str(n),'node_id':str(n),'title':f'Research {n}'} for n in {'<main>0</main>':range(1,6),'1':range(6,9)}.get(soup,[])]))
        self.enterContext(patch.object(self.module,'_fetch_detail',return_value=('Scientific research. '*10,None,None)))
        return c


for cls in (ArcepCounts,CivilCounts,InriaCounts,TransportesCounts):
    pg = type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls, pg
