"""ND-1 (2026-10-19): ejournals-epublishing-ekt-gr-indexphp through X3 discover+follow.
Issues are discovered from the journals catalog -> issue/archive pages (and their
/issue/archive/N pages) -> issue/view/<digits> links; each issue page is a segment whose
article links _enumeration_items reads like _get_article_urls; _enumeration_paper is
crawl()'s _parse_article with its skips. Offline: synthetic pages, no network."""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'ejournals-epublishing-ekt-gr-indexphp'
B = 'https://ejournals.epublishing.ekt.gr'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'date_range_split', 'base_url': B,
            'adapter': {'type': 'crawler', 'contract_version': 1, 'list_method': '_enumeration_items',
                        'list_args': ['html', 'url'], 'detail_method': '_enumeration_paper',
                        'detail_args': ['html', 'item'], 'none_is_rule_skip': True},
            'listing': {'url': B + '/index.php/index/journals-catalog', 'page_param': 'page', 'start_page': 1,
                        'parser': {'type': 'adapter', 'url_field': 'url', 'key_field': 'url', 'listing_marker': 'body'}},
            'segments': {'mode': 'discover', 'hub_url': B + '/index.php/index/journals-catalog',
                         'selector': "a[href*='/issue/view/']", 'attr': 'href',
                         'key_regex': '^https://ejournals\\.epublishing\\.ekt\\.gr/index\\.php/[A-Za-z0-9_\\-]+/issue/view/(\\d+)$',
                         'label': 'text', 'link_as_url': True,
                         'follow': {'selector': "a[href$='/issue/archive'], a[href*='/issue/archive/']",
                                    'attr': 'href', 'max_depth': 3, 'max_pages': 1000}}}}}
ISSUE = B + '/index.php/geo/issue/view/1005'
LONG = 'Synthetic Greek geology abstract résumé with Korean 자료 for an offline equality test.'


def article(n, abstract):
    return (f'<html><head><meta name="citation_title" content="Article {n}">'
            f'<meta name="citation_author" content="Author {n}"><meta name="citation_date" content="2001/01/01">'
            f'<meta name="DC.Identifier" content="{n}"><meta name="citation_journal_title" content="Bulletin">'
            f'<meta name="DC.Description" content="{abstract}"></head><body>'
            f'<div class="article-abstract">{abstract}</div></body></html>')


ARTICLES = {f'{B}/index.php/geo/article/view/{n}': article(n, a) for n, a in ((17331, '.'), (17340, LONG), (17341, LONG))}
ISSUE_HTML = ('<html><body>' + ''.join(f'<a href="{u}">x</a><a href="{u}">again</a><a href="{u}/999">pdf</a>'
                                       for u in ARTICLES)
              + f'<a href="{B}/index.php/other/article/view/1">other journal</a></body></html>')
CATALOG = f'<html><body><a href="{B}/index.php/geo">Geo</a><a href="{B}/index.php/geo/issue/current">c</a></body></html>'
ARCHIVE = f'<html><body><a href="{ISSUE}">Vol 1</a></body></html>'


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        pages = {B + '/index.php/index/journals-catalog': CATALOG, B + '/index.php/geo/issue/archive': ARCHIVE,
                 ISSUE: ISSUE_HTML, **ARTICLES}
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.dict(crawler.crawl.__globals__, {'_curl_get': lambda u, retries=3: pages.get(u)}), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def test_papers_equal_crawl(self):
        expected = self.native()
        self.assertEqual(['17340', '17341'], [p['external_id'] for p in expected])  # '.' abstract skipped
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, ISSUE_HTML, ISSUE + '?page=1'))
        self.assertEqual(list(ARTICLES), [i.url for i in page.items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, ARTICLES[u], u)
        papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in page.items]
        self.assertIsNone(papers[0])
        self.assertEqual([clean_paper(e) for e in expected], papers[1:])

    def test_not_an_issue_page(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(ISSUE_HTML, B + '/index.php/index/journals-catalog')


if __name__ == '__main__':
    unittest.main()
