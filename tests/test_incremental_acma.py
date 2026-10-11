import contextlib
import io
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE = 'acma-gov-au-publications'
BASE = 'https://www.acma.gov.au'


def listing(ids):
    return '<html><main>'+''.join(f'<article class="card"><div class="card-title-wrap"><a class="card-link" href="/doc-{i}"><h3 class="card-title">Research {i}</h3></a></div><time class="card-date" datetime="2026-10-0{i}"></time></article>' for i in ids)+'</main></html>'


def detail(i):
    return f'<html><h1>Research {i}</h1><main><div class="field--name-field-html">Controlled substantive research abstract for document {i}.</div><a href="/files/doc-{i}.pdf">Source PDF</a></main></html>'


class AcmaFailureTests(unittest.TestCase):
    def setUp(self):
        self.c = CRAWLERS[SITE](None, delay=0); self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch('time.sleep'))

    def test_transport_failure_stops_before_other_section_and_never_completes(self):
        with patch.object(self.c._stealth, 'fetch_html', return_value=(None, {'final_reason':'all_layers_failed'})) as fetch:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(fetch.call_count,3)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_recognized_denial_is_not_retried_as_empty_catalogue(self):
        with patch.object(self.c._stealth, 'fetch_html', return_value=(DENIAL, {'final_reason':'ok'})) as fetch:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(fetch.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_detail_transport_failure_never_saves_summary_or_moves_to_next_section(self):
        replies=[(listing([1]),{'final_reason':'ok'})]+[(None,{'final_reason':'all_layers_failed'})]*3
        with patch.object(self.c._stealth,'fetch_html',side_effect=replies) as fetch, \
                patch.object(self.c,'_save_paper',side_effect=AssertionError('failed detail saved')):
            self.assertEqual(self.c.crawl(),0);self.assertEqual(fetch.call_count,4)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_native_fields_preserved_for_good_detail(self):
        with patch.object(self.c._stealth,'fetch_html',return_value=(detail(1),{'final_reason':'ok'})):
            p=self.c._scrape_detail(dict(url=BASE+'/doc-1',title='Research 1',listed_date='2026-10-01'),'Publications')
        self.assertEqual(p['external_id'],'doc-1');self.assertEqual(p['published_date'],'2026-10-01')
        self.assertEqual(p['pdf_url'],BASE+'/files/doc-1.pdf');self.assertIn('substantive research',p['abstract'])

    def test_original_search_scope_is_not_claimed_as_connected(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('original_pdf_search_scope_excluded',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
