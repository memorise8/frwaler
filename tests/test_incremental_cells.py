import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from delivery.scripts.review_cells_incremental import parse, URL

BODY = 'Controlled synthetic PDF extraction result for native request counting and metadata persistence only. '


def listing(years=(2024,2023), *, dates=False):
    cards=[];blocks={'columns':{'@type':'columnsBlock'}}
    for y in years:
        path=f'/en/assets/pdfs/public/report-{y}.pdf/@@download/file'
        title=f'Activity Report {y}'
        cards.append(f'<div><h3 id="activity-report-{y}">{title}</h3><a href="{path}">PDF</a></div>')
        blocks['title-'+str(y)]={'@type':'slate','plaintext':title,'value':[{'type':'h3'}]}
        ref={'@id':'http://localhost:8091/alba'+path,'title':f'report-{y}.pdf'}
        if dates:ref['effective']='2025-05-06'
        blocks['image-'+str(y)]={'@type':'image','href':[ref]}
    state={'content':{'data':{'title':'Corporate publications','@type':'Document','items_total':0,
        'created':'2024-06-15','effective':'2024-06-15','modified':'2025-07-24','blocks':blocks}}}
    return ('<html><main>'+''.join(cards)+'</main><script>window.__data='+json.dumps(state)+';</script></html>').encode()


class CurlFixture:
    """Intercept native subprocess HTTP requests; no real file/body acceptance."""
    def __init__(self, raw, pdf_urls):
        self.raw=raw;self.pdf_urls=set(pdf_urls);self.calls=[];self.files={}

    def __call__(self, cmd, **kwargs):
        if cmd[0]=='curl':
            url=cmd[-1]
            if url==URL:
                self.calls.append({'kind':'list','url':url})
                return SimpleNamespace(returncode=0,stdout=self.raw,stderr=b'')
            if url not in self.pdf_urls:raise AssertionError('unexpected native PDF request')
            path=Path(cmd[cmd.index('-o')+1]);assert path.exists()
            self.calls.append({'kind':'pdf','url':url});self.files[str(path)]=url
            path.write_bytes(b'%PDF-fixture-not-source\n'+b'controlled fixture '*100)
            return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
        if cmd[0]=='pdftotext':
            assert cmd[1:5]==['-f','1','-l','3']
            return SimpleNamespace(returncode=0,stdout=(BODY+self.files[cmd[-2]]).encode(),stderr=b'')
        raise AssertionError('unreviewed process invocation')


class CellsIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS['cells-es-en'](None,delay=0);self.addCleanup(self.c._session.close)

    def test_reporting_years_and_container_dates_cannot_prove_publication_order(self):
        items,evidence=parse(self.c,listing())
        self.assertEqual([i['reporting_year_raw'] for i in items],['2024','2023'])
        self.assertEqual(evidence['inferred_order'],('arbitrary','order_ambiguous'))
        self.assertEqual(evidence['proof']['date']['missing'],2)
        self.assertEqual(evidence['embedded_items_total'],0)
        self.assertEqual(len(items),2)

    def test_new_per_report_date_metadata_requires_renewed_review(self):
        with self.assertRaisesRegex(ValueError,'per-report date metadata'):parse(self.c,listing(dates=True))

    def test_new_pager_invalidates_static_catalogue_review(self):
        with self.assertRaisesRegex(ValueError,'date/pager'):parse(self.c,listing().replace(b'</main>',b'<a rel="next">More</a></main>'))

    def test_mismatched_rendered_and_embedded_reports_rejected(self):
        with self.assertRaisesRegex(ValueError,'references differ'):
            parse(self.c,listing().replace(b'http://localhost:8091/alba/en/assets/pdfs/public/report-2024',b'http://localhost:8091/alba/en/assets/pdfs/public/report-2000'))

    def test_native_preserves_year_and_identity_without_inventing_publication_day(self):
        raw=listing();items,_=parse(self.c,raw);fixture=CurlFixture(raw,[i['pdf_url'] for i in items]);papers=[]
        def save(p):papers.append(p);self.c._last_save_created=True
        self.c._save_paper=save
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',fixture),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.c.crawl(),2)
        self.assertEqual([c['kind'] for c in fixture.calls],['list','pdf','pdf'])
        for p,i in zip(papers,items):
            self.assertEqual(p['external_id'],i['anchor']);self.assertEqual(p['post_number'],i['reporting_year_raw'])
            self.assertEqual(p['pdf_url'],i['pdf_url']);self.assertIn(i['pdf_url'],p['abstract'])
            self.assertIsNone(p['published_date']);self.assertIsNone(p['listed_date'])
            meta=json.loads(p['metadata']);self.assertEqual(meta['year'],i['reporting_year_raw']);self.assertIsNone(meta['posted_date'])

    def test_reviewed_hold_cannot_certify_incremental_baseline(self):
        result=eligibility(self.c.site_id,type(self.c))
        self.assertEqual(result['classification'],'full_rescan_required')
        self.assertIsNone(result['adapter']);self.assertFalse(result['baseline_complete'])
        self.assertIn('reporting_years_not_publication_dates',result['reason'])
        self.assertTrue(result['periodic_full_check'])


if __name__=='__main__':unittest.main()
