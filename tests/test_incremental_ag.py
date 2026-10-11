import contextlib
import io
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='ag-gov-au-publications'
BASE='https://www.ag.gov.au'
BODY='Controlled substantive publication description for native metadata and request counting only. '


def listing(ids):
    return '<html><div class="view-content">'+''.join(
        f'<div class="views-row"><article class="node--type-publication"><div class="search__title"><a href="/publications/doc-{i}">Research {i}</a></div><div class="search__info"><time datetime="2026-10-{i:02}T12:00:00Z"></time></div><div class="search__summary">{BODY}</div></article></div>'
        for i in ids)+'</div></html>'


def detail(i):
    return f'<html><main><h1>Research {i}</h1><div class="field--name-body">{BODY}</div><div class="field--name-field-publication-date"><time datetime="2026-10-{i:02}"></time></div><div class="field--name-field-attachments"><a href="/files/doc-{i}.docx" type="application/vnd.openxmlformats-officedocument.wordprocessingml.document">Word</a><a href="/files/doc-{i}.pdf" type="application/pdf">PDF</a></div></main></html>'


class AgFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_transport_failure_stops_before_other_sections(self):
        with patch.object(self.c._stealth,'fetch_html',return_value=(None,{'final_reason':'all_layers_failed'})) as fetch:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(fetch.call_count,3)
            self.assertTrue(all(call.args[0]==self.c.SECTIONS[0][0] for call in fetch.call_args_list))
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_denied_success_body_is_not_saved_or_retried(self):
        with patch.object(self.c._stealth,'fetch_html',return_value=(DENIAL,{'final_reason':'ok'})) as fetch:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(fetch.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_missing_detail_is_not_a_successful_long_snippet(self):
        replies=[(listing([1]),{'final_reason':'ok'})]+[(None,{'final_reason':'all_layers_failed'})]*3
        with patch.object(self.c._stealth,'fetch_html',side_effect=replies), \
                patch.object(self.c,'_save_paper',side_effect=AssertionError('missing source saved')):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_native_dates_identity_and_PDF_selection_preserved(self):
        with patch.object(self.c._stealth,'fetch_html',return_value=(detail(1),{'final_reason':'ok'})):
            p=self.c._scrape_detail(dict(url=BASE+'/publications/doc-1',title='Research 1',listed_date='2026-10-01'),'Integrity')
        self.assertEqual(p['external_id'],'doc-1');self.assertEqual(p['published_date'],'2026-10-01')
        self.assertEqual(p['pdf_url'],BASE+'/files/doc-1.pdf');self.assertEqual(p['abstract'],BODY.strip())

    def test_reviewed_scope_hold_keeps_baseline_incomplete(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('original_pdf_search_scope_excluded',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
