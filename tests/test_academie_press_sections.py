"""All source sections, article ownership/date roles and actual DB repeat/cancel behavior."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from bs4 import BeautifulSoup
from crawler.academie_source import BASE, START, SECTIONS, editorial_items, parse_article
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import AcademieCounts


def article(url, *, newspaper=False):
    kind='Parution dans la presse' if newspaper else 'Actualités'
    cls='press-release' if newspaper else 'news'
    cta='<a href="https://journal.invalid/story">Read article</a>' if newspaper else '<a href="/files/own.pdf">Lire le communiqué</a>'
    extra='' if newspaper else '<a href="/files/own-en.pdf">English version</a>'
    return (f'<html><head><link rel="canonical" href="{url}"></head><body><main>'
            f'<div class="Node NodeEdito NodeEdito--{cls}"><div class="NodeEdito-header">'
            f'<h1>Full source article title</h1><p class="NodeEdito-surtitle">{kind}</p>'
            '<div class="NodeEdito-date">31.01.2025</div><div class="NodeEdito-chapo">Source introduction.</div>'
            f'<div class="NodeEdito-cta">{cta}</div></div><div class="NodeEdito-content">'
            '<img src="/photo.jpg" alt="Not an article body">'
            f'<div class="ParagraphText">{"Original scientific source evidence and explanations. "*8}{extra}</div></div></div>'
            '<a href="/brandbook.pdf">Common brand guide</a></main></body></html>')


def listing(page):
    values=[1,2] if page==0 else [3] if page==1 else []
    records=''.join(f'<div class="views-row"><h4 class="ParagraphAccordion-item-title"><a href="/files/{n}.pdf">Report {n}<time datetime="2025-01-31T12:00:00+01:00">31/01/2025</time></a></h4></div>' for n in values)
    sections=''
    for name,path,title in [(SECTIONS[0],'/article','Curated press title'),(SECTIONS[1],'/column','Newspaper title'),(SECTIONS[2],'/files/dossier.pdf','Press dossier')]:
        sections+=f'<div class="ParagraphAccordion"><h3>{name}</h3><div class="ParagraphAccordion-item"><a href="{path}"><h4 class="ParagraphAccordion-item-title">{title}</h4></a></div></div>'
    pager='<ul data-drupal-views-infinite-scroll-pager><li><a rel="next" href="?page=1">Next</a></li></ul>' if page==0 else ''
    return f'<html><body><div class="NodeEdito--pressArea">{sections}</div><div class="NodeEdito-view"><h2 class="NodeEdito-view-title">Communiqué de presse</h2>{records}{pager}</div></body></html>'


class AcademieSections(unittest.TestCase):
    BACKEND='sqlite'

    def setUp(self):
        self.case=AcademieCounts(methodName='test_repeat_three_reaches_remainder_and_then_zero');self.case.BACKEND=self.BACKEND
        self.case.setUp();self.addCleanup(self.case.doCleanups);self.module=self.case.module;self.conn=self.case.conn

    def crawler(self):
        c=self.module.AcademieSciencesFrEspacePresseCrawler(self.conn,delay=0);c.delivery_mode='full';self.calls=[]
        def fetch(url,**kw):
            self.calls.append(url)
            if url in (BASE+'/article',BASE+'/column'):return article(url,newspaper=url.endswith('/column'))
            return listing(int(parse_qs(urlsplit(url).query).get('page',['0'])[0]))
        c._fetch_text=fetch;c._fetch_bytes=lambda *a,**kw:b'%PDF-fixture'
        c._extract_pdf_text=lambda *a:'Scientific report source title\n'+'Original scientific research and results with source evidence. '*20
        return c

    def rows(self):
        names=['seq_id','post_number','meta_url','title','abstract','pdf_url','published_date','published_date_raw','listed_date','listed_date_raw','summary']
        rows=self.conn.execute('SELECT '+','.join(names)+' FROM documents ORDER BY seq_id').fetchall()
        return [dict(r) if isinstance(r,dict) else dict(zip(names,r)) for r in rows]

    def test_all_three_sections_emitted_once_and_keep_existing_PDF_identity(self):
        c=self.crawler();first,next_page=c._parse_list_items(self.module._make_soup(listing(0)),1,START)
        second,_=c._parse_list_items(self.module._make_soup(listing(1)),2,START+'?page=1')
        self.assertEqual((len(first),len(second),next_page),(5,1,True))
        self.assertEqual(c.crawl(),6);self.assertEqual({r['post_number'] for r in self.rows()},{'files/1.pdf','files/2.pdf','files/3.pdf','files/dossier.pdf','article','column'})
        before=self.rows();self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.rows(),before)

    def test_portal_raw_date_and_article_owned_PDF_are_separate_from_publication(self):
        self.assertEqual(self.crawler().crawl(),6)
        row=next(r for r in self.rows() if r['post_number']=='article')
        self.assertEqual(row['listed_date_raw'],'31.01.2025');self.assertEqual(row['listed_date'],'2025-01-31')
        self.assertIsNone(row['published_date']);self.assertIsNone(row['published_date_raw'])
        self.assertEqual(row['pdf_url'],BASE+'/files/own.pdf')
        self.assertNotIn('Common brand guide',row['abstract']);self.assertNotIn('Not an article body',row['abstract'])
        self.assertFalse(next(r for r in self.rows() if r['post_number']=='column')['pdf_url'])

    def test_cancel_new_row_budget_and_repeat_preserve_existing_rows(self):
        self.assertEqual(self.crawler().crawl(limit=3),3);before=self.rows()
        c=self.crawler();c.delivery_should_cancel=lambda:self.case.count()>=4
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.case.count(),4);self.assertEqual(self.crawler().crawl(),2)
        self.assertEqual(self.rows()[:3],before);self.assertEqual(self.crawler().crawl(limit=3),0)

    def test_foreign_ambiguous_or_missing_explicit_sections_are_rejected(self):
        raw=listing(0)
        for changed in [raw.replace('/article','https://outside.invalid/article'),raw.replace(SECTIONS[1],'Unreviewed section'),raw.replace(SECTIONS[1],SECTIONS[0]),raw.replace('<h3>'+SECTIONS[2]+'</h3>',''),raw.replace('href="/article"','href="/article?other=1"')]:
            with self.subTest(changed=changed),self.assertRaises(ValueError):editorial_items(BeautifulSoup(changed,'lxml'),1,START)

    def test_paginated_origin_type_and_adjacent_link_must_remain_source_bound(self):
        c=self.crawler();raw=listing(0)
        for changed in [raw.replace('href="/files/1.pdf"','href="https://outside.invalid/1.pdf"'),
                        raw.replace('href="/files/1.pdf"','href="/files/1.html"'),
                        raw.replace('href="?page=1"','href="?page=2"'),
                        raw.replace('NodeEdito-view-title','unknown-heading')]:
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                c._parse_list_items(self.module._make_soup(changed),1,START)

    def test_article_identity_structure_quality_and_primary_PDF_guards(self):
        item=next(v for v in editorial_items(BeautifulSoup(listing(0),'lxml'),1,START) if v['kind']=='HTML')
        raw=article(BASE+'/article')
        detail=parse_article(raw,item);self.assertEqual(detail['title'],'Full source article title')
        self.assertEqual(detail['listing_title'],'Curated press title');self.assertEqual(len(detail['article_PDF_urls']),2)
        for changed in [raw.replace('href="'+BASE+'/article"','href="'+BASE+'/wrong"'),raw.replace('</html>',''),raw.replace('class="NodeEdito-content"','class="other"'),raw.replace('Actualités','Unknown category'),raw.replace('/files/own.pdf','https://outside.invalid/own.pdf'),raw.replace('<h1>','<h1></h1><h1>'),raw.replace('</head>','<base href="https://outside.invalid/"></head>'),raw.replace('Original scientific source evidence and explanations. '*8,'\ufffd'*300),raw.replace('class="NodeEdito-cta"','class="not-primary"')]:
            with self.subTest(changed=changed),self.assertRaises((ValueError,PermissionError)):parse_article(changed,item)

    def test_invalid_date_keeps_raw_and_does_not_gain_a_valid_day(self):
        c=self.crawler();fetch=c._fetch_text
        c._fetch_text=lambda url,**kw:fetch(url,**kw).replace('31.01.2025','31.02.2025')
        self.assertEqual(c.crawl(),6);row=next(r for r in self.rows() if r['post_number']=='article')
        self.assertEqual(row['listed_date_raw'],'31.02.2025');self.assertIsNone(row['listed_date'])

    def test_source_fetch_uses_verified_TLS_no_redirects_retries_or_partial_body(self):
        for output,rc,want in [(b'payload\n200',0,b'payload'),(b'partial\n200',18,None),(b'forbidden\n403',0,None),(b'redirect\n302',0,None)]:
            c=self.crawler()
            with patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(stdout=output,returncode=rc)) as run:
                self.assertEqual(self.module.AcademieSciencesFrEspacePresseCrawler._fetch_bytes(c,BASE+'/file.pdf'),want)
                command=run.call_args.args[0]
                self.assertFalse({'-k','--insecure','-L','--location','--retry'}&set(command));self.assertEqual(run.call_count,1)
                if want is None:
                    self.assertIsNone(self.module.AcademieSciencesFrEspacePresseCrawler._fetch_bytes(c,BASE+'/another.pdf'));self.assertEqual(run.call_count,1)

    def test_failed_article_is_not_reported_as_a_complete_remaining_scan(self):
        c=self.crawler();fetch=c._fetch_text;c._fetch_text=lambda url,**kw:fetch(url,**kw).replace('href="'+BASE+'/article"','href="'+BASE+'/wrong"')
        self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'item_failed');before=self.rows()
        self.assertEqual(self.crawler().crawl(),4);self.assertEqual(self.rows()[:2],before)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'TEST_PG_DSN not configured')
class AcademiePostgresSections(AcademieSections):
    BACKEND='postgres'


if __name__=='__main__':unittest.main()
