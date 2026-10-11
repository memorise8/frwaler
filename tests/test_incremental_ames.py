import contextlib
import html
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='ameslab-gov-news'
BASE='https://www.ameslab.gov'
BODY='Controlled substantive scientific news article text for storage and request counting.'


def listing(rows):
    return '<html>'+''.join(f'<article class="card"><a class="card__link" href="{html.escape(url)}"><h3 class="card__title">{html.escape(title)}</h3><p class="card__subtext">{BODY}</p></a></article>' for url,title in rows)+'</html>'


def detail(title):
    return f'<html><head><meta charset="utf-8"></head><body><section class="hero"><h1>{html.escape(title)}</h1></section><div class="textual"><p>{BODY}</p></div></body></html>'


class AmesFailureTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_failed_list_stops_before_other_categories(self):
        with patch.object(self.c,'_curl_get',return_value=None) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_failed_detail_does_not_skip_to_next_document(self):
        with patch.object(self.c,'_curl_get',side_effect=[listing([(BASE+'/news/one','Research')]),None]) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,2)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_challenge_cannot_be_a_document_or_empty_list(self):
        with patch.object(self.c,'_curl_get',return_value=DENIAL):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_HTTP_error_curl_exit_is_retried_then_rejected(self):
        def run(cmd,**kwargs):
            self.assertTrue(any(a.startswith('-') and 'f' in a and 's' in a for a in cmd))
            return SimpleNamespace(returncode=22,stdout=b'',stderr=b'HTTP403')
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',side_effect=run) as fetch:
            self.assertIsNone(self.c._curl_get(BASE));self.assertEqual(fetch.call_count,3)
            self.assertIsNone(self.c._curl_head_content_disposition(BASE));self.assertEqual(fetch.call_count,4)

    def test_no_date_or_numeric_ID_is_invented_for_synthetic_detail(self):
        item={'url':BASE+'/news/one','title':'Research','category':'News Release'}
        paper=self.c._build_paper(item,self.c._parse_detail_page(detail('Research'),item['url'],'News Release'))
        self.assertEqual(paper['external_id'],'one');self.assertIsNone(paper['published_date'])
        self.assertEqual(paper['abstract'],BODY)

    def test_reviewed_hold_never_certifies_list_order(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('listing_dates_and_node_IDs_detail_only',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
