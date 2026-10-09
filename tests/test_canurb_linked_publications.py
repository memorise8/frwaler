"""Publication cards pointing to PDFs or external projects remain in scope."""
import json
import os
import unittest
from unittest.mock import Mock
from tests.test_coupled_site_counts import CoupledContract


class LinkedPublicationCounts(CoupledContract, unittest.TestCase):
    SITE = 'canurb-org-publications'; LIST_METHOD = '_curl_get'
    PDF = 'https://canurb.org/wp-content/uploads/report.pdf?download=1'
    EXTERNAL = 'https://project.example.org/report'

    def crawler(self):
        c = self.configured(self.module.CanurbOrgPublicationsCrawler(self.conn))
        self.urls = {1: self.PDF, 2: self.EXTERNAL, **{n: f'https://canurb.org/publications/report-{n}/' for n in range(3, 9)}}
        def fetch(url, **kw):
            if url == self.module._LIST_URL:
                return '<main>'+''.join(
                    f'<article class="publications post-{n} category-research-report"><a href="{target}"><h4>Report {n}</h4></a><div class="posts__content"><p>'+('Publisher source description. '*8)+'</p></div></article>'
                    for n, target in self.urls.items())+'</main>'
            if url in (self.PDF, self.EXTERNAL):
                raise AssertionError('linked resources must not be parsed as CANURB detail HTML')
            return '<h1 class="entry-title">Research report</h1><meta property="og:description" content="'+('Research evidence. '*15)+'">'
        c._curl_get = self.fetch = Mock(side_effect=fetch)
        return c

    def test_linked_cards_preserve_native_id_and_source_metadata(self):
        c = self.crawler(); original = c._save_paper; emitted = {}
        def save(paper):
            emitted[str(paper['external_id'])] = dict(paper)
            return original(paper)
        c._save_paper = save
        self.assertEqual(c.crawl(), 8)
        for key, url, kind in [('1', self.PDF, 'direct_pdf'), ('2', self.EXTERNAL, 'external_publication')]:
            placeholder = '%s' if self.BACKEND == 'postgres' else '?'
            row = self.conn.execute('SELECT post_number,meta_url AS url,pdf_url,pdf_downloaded,text_extracted,published_date,published_date_status FROM documents WHERE post_number='+placeholder, (key,)).fetchone()
            if not isinstance(row, dict):
                row = dict(zip(['post_number','url','pdf_url','pdf_downloaded','text_extracted','published_date','published_date_status'], row))
            self.assertEqual(row['url'], url)
            self.assertEqual(row['pdf_url'], self.PDF if key == '1' else None)
            self.assertFalse(row['pdf_downloaded']); self.assertFalse(row['text_extracted'])
            self.assertIsNone(row['published_date']); self.assertEqual(row['published_date_status'], 'missing')
            # V2 document storage has no general metadata column. Verify the
            # emitted provenance separately from fields actually persisted.
            metadata = json.loads(emitted[key]['metadata'])
            self.assertEqual(metadata['link_type'], kind)
            self.assertEqual(metadata['metadata_source_url'], self.module._LIST_URL)
            self.assertFalse(metadata['linked_resource_fetched'])
        self.assertNotIn(self.PDF, [call.args[0] for call in self.fetch.call_args_list])
        self.assertNotIn(self.EXTERNAL, [call.args[0] for call in self.fetch.call_args_list])

    def test_invalid_link_scheme_is_not_a_publication_target(self):
        c = self.crawler()
        soup = c._make_soup('<article class="publications post-1"><a href="javascript:alert(1)"><h4>Bad link</h4></a></article>')
        self.assertEqual(c._parse_list(soup), [])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class LinkedPublicationPostgres(LinkedPublicationCounts):
    BACKEND = 'postgres'
