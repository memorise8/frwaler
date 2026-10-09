"""Publisher article text outranks demonstrably copied social metadata."""
import json,os,unittest
from tests import test_canurb_linked_publications as linked

class CanurbBodyPrecedence(linked.LinkedPublicationCounts):
    def test_article_body_precedes_unrelated_social_description_and_sidebars(self):
        c=self.crawler();fetch=self.fetch.side_effect;papers={};save=c._save_paper
        def detail(url,**kw):
            raw=fetch(url,**kw)
            if '/publications/report-' not in url:return raw
            n=int(url.rstrip('/').rsplit('-',1)[1]);body=f'Actual report {n}: '+('Housing supply and emissions evidence. '*10)
            return raw+'<aside class="the__content">'+('Unrelated sidebar. '*20)+'</aside><main><article><div class="the__content"><nav>Navigation noise</nav><p>'+body+'</p><footer>Footer noise</footer></div></article></main>'
        def emit(paper):papers[str(paper['external_id'])]=dict(paper);return save(paper)
        c._curl_get.side_effect=detail;c._save_paper=emit
        self.assertEqual(c.crawl(),8)
        for n in range(3,9):
            paper=papers[str(n)];self.assertTrue(paper['abstract'].startswith(f'Actual report {n}:'))
            self.assertNotIn('sidebar',paper['abstract']);self.assertNotIn('noise',paper['abstract'])
            metadata=json.loads(paper['metadata']);self.assertEqual(metadata['abstract_source'],'detail_body_excerpt')
            self.assertIn('Research evidence',metadata['social_description_raw'])
        rows=self.conn.execute('SELECT post_number,abstract FROM documents').fetchall()
        actual={r['post_number']:r['abstract'] for r in rows} if self.BACKEND=='postgres' else dict(rows)
        self.assertTrue(actual['3'].startswith('Actual report 3:'))

    def test_short_article_keeps_supported_metadata_fallback(self):
        c=self.crawler();fetch=self.fetch.side_effect
        c._curl_get.side_effect=lambda url,**kw:fetch(url,**kw)+'<article><div class="the__content">Download</div></article>'
        self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT post_number,abstract FROM documents').fetchall()
        actual={r['post_number']:r['abstract'] for r in rows} if self.BACKEND=='postgres' else dict(rows)
        self.assertIn('Research evidence',actual['3'])

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class CanurbBodyPrecedencePostgres(CanurbBodyPrecedence):
    BACKEND='postgres'
