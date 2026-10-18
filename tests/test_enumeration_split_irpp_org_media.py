"""L3b-20261018 (X3): enumeration split for irpp-org-media.
crawl() follows the load-more pages (/media/page/N/), fetches each card's detail page
(op-eds redirect to policyoptions.irpp.org) and then the page's WordPress REST JSON
link with _embed=1 (_with_embed); external_id = '<type>:<id>' from that JSON. The plan
reads the same pages by number, the second step fetches the JSON link with add_params
{"_embed": "1"} (ext:second_step_link_params) and the paper method runs crawl()'s own
_parse_detail with that response standing in for _fetch_json. Synthetic pages only."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load
from tests.enumeration_fakes import Env

SITE = 'irpp-org-media'
START = 'https://irpp.org/media/'
PAGE2 = 'https://irpp.org/media/page/2/'
NR = 'https://irpp.org/news-release/synthetic-release/'
OPED = 'https://irpp.org/op-ed/synthetic-oped/'
OPED_FINAL = 'https://policyoptions.irpp.org/magazines/synthetic-oped/'
POD = 'https://irpp.org/podcast/synthetic-podcast/'
TEXT = 'Synthetic body text for a regression fixture with enough words to pass the minimum length rule.'


def card(url, title, icon):
    return (f'<article><a class="article__link" href="{url}"><span class="icon__oval icon--{icon}"></span>'
            f'<h3 class="article__title">{title}</h3></a><p class="date-subtitle">March 3, 2031</p>'
            f'<p class="article__subtext">{title} excerpt</p></article>')


def listing(cards, nxt=None):
    more = f'<a class="load-more-link" href="{nxt}">More</a>' if nxt else ''
    return (f'<html><body class="page-template-page-media"><div class="load-more__container">'
            f'<div class="event__list">{cards}</div>{more}</div></body></html>')


LIST1 = listing(card(NR, 'Synthetic release', 'news-release') + card(OPED, 'Synthetic op-ed', 'op-ed'), PAGE2)
LIST2 = listing(card(POD, 'Synthetic podcast', 'podcast'))
EMPTY = '<html><body class="page-template-page-media"><main></main></body></html>'


def detail(json_href, canonical, author):
    link = f'<link rel="alternate" type="application/json" href="{json_href}">' if json_href else ''
    return (f'<html><head>{link}<link rel="canonical" href="{canonical}">'
            f'<meta property="og:description" content="{TEXT}"></head><body>'
            f'<div class="AuthorLinks"><a>{author}</a></div><article><p>{TEXT}</p></article></body></html>')


def wp(kind, i, link):
    return json.dumps({'id': i, 'type': kind, 'link': link, 'date': '2031-03-03T10:00:00', 'modified': '2031-03-04T10:00:00',
                       'title': {'rendered': f'Synthetic {kind} &amp; title'},
                       'content': {'rendered': f'<p>{TEXT} {kind} content that is longer than the others.</p>'},
                       'excerpt': {'rendered': '<p>short</p>'},
                       '_embedded': {'wp:term': [[{'taxonomy': 'topic', 'name': 'Synthetic topic', 'slug': 't'}]],
                                     'wp:featuredmedia': [{'source_url': 'https://irpp.org/img.png'}]}})


NR_JSON = 'https://irpp.org/wp-json/wp/v2/news-release/101'
OPED_JSON = 'https://policyoptions.irpp.org/wp-json/wp/v2/issues/303914'
PAGES = {  # url -> (final url, content type, body)
    NR: (NR, 'text/html', detail(NR_JSON, NR, 'A. Author')),
    OPED: (OPED_FINAL, 'text/html', detail(OPED_JSON, OPED_FINAL, 'B. Author')),
    POD: (POD, 'text/html', detail(None, POD, 'C. Author')),
    NR_JSON + '?_embed=1': (NR_JSON + '?_embed=1', 'application/json', wp('news-release', 101, NR)),
    OPED_JSON + '?_embed=1': (OPED_JSON + '?_embed=1', 'application/json', wp('issues', 303914, OPED_FINAL)),
}


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class RegisteredSplit(unittest.TestCase):
    def native(self):
        crawler, _ = load(SITE)
        captured, calls = [], []

        def get(url):
            calls.append(url)
            if url == START:
                return LIST1, START
            if url == PAGE2:
                return LIST2, PAGE2
            final, _, body = PAGES[url]
            return body, final
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=None)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        calls = []

        def fetch(url):
            calls.append(url)
            final, ctype, body = PAGES[url]
            return Response(200, {'Content-Type': ctype}, body, final)
        papers = []
        for url, body in ((START, LIST1), ('https://irpp.org/media/page/2/', LIST2)):
            for item in parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url)).items:
                papers.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        self.assertIn(NR_JSON + '?_embed=1', calls)
        self.assertIn(OPED_JSON + '?_embed=1', calls)
        return [x for x in papers if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        actual = self.pipeline()
        self.assertEqual(3, len(expected))
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])
        self.assertEqual(['news-release:101', 'issues:303914', POD], [x['external_id'] for x in actual])
        self.assertEqual('Policy Options', actual[1]['department'])
        self.assertTrue(json.loads(actual[0]['metadata'])['jsonUrl'].endswith('?_embed=1'))
        self.assertEqual('["Synthetic topic"]', actual[0]['keywords'])

    def test_page_past_the_end_is_explicit_empty_and_a_foreign_page_is_not(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, 'https://irpp.org/media/page/80/'))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>',
                                        'https://irpp.org/media/page/80/'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))

    def test_original_code_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        actual = {}
        for n in ast.parse(code).body:
            if isinstance(n, ast.FunctionDef):
                actual[n.name] = hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, ast.FunctionDef):
                        actual[f'{n.name}.{m.name}'] = hashlib.sha256(ast.get_source_segment(code, m).encode()).hexdigest()
        for name, digest in ORIGINAL_SHA256.items():
            self.assertEqual(digest, actual.get(name), name)

    def test_end_to_end_run_completes(self):
        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url == START:
                return Response(200, {'Content-Type': 'text/html'}, LIST1, url)
            if url == PAGE2:
                return Response(200, {'Content-Type': 'text/html'}, LIST2, url)
            if url == OPED:
                return Response(301, {'Location': OPED_FINAL}, b'', url)
            if url == OPED_FINAL:
                return Response(200, {'Content-Type': 'text/html'}, PAGES[OPED][2], url)
            if url in PAGES:
                final, ctype, body = PAGES[url]
                return Response(200, {'Content-Type': ctype}, body, url)
            return Response(200, {'Content-Type': 'text/html'}, EMPTY, url)
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual({'news-release:101', 'issues:303914', POD}, {d['post_number'] for d in docs})


ORIGINAL_SHA256 = {"IRPPOrgMediaCrawler._curl_get": "50f95c7d3a7df54faf385c80b254e8ec5a6a9d4a9f5e6c396c55e00f9643db96", "IRPPOrgMediaCrawler._split_curl_output": "abaa97f6f2158bf74ee5cb7b59be31f5cd937d49c018286c4d9c179f7a4062c1", "IRPPOrgMediaCrawler._fetch_json": "3f81ba621effdfd2fad4a44c434ca9b57d3cc0c1baafd1c280e88f84f7660478", "IRPPOrgMediaCrawler._soup": "61cde3ce014a23a6518f52bb4e6acb4de0ac0bf6bc4b70f12ae952ba881a7f02", "IRPPOrgMediaCrawler._html_to_text": "765b57669e13690b8c549aced8d5bfab699bf320859d948bf36f26a292b3e261", "IRPPOrgMediaCrawler._clean_text": "4dc2657b3a7e3e19e0dc2fa884b3916a6dcdc2c24a45c0bfb4c8026164d92e49", "IRPPOrgMediaCrawler._node_text": "a27e8b59939586c21e418622560631b76ca7afdb2de76e10f293b8ca0c072f01", "IRPPOrgMediaCrawler._dedupe": "30e7a63f250c216a62138317036ce9f92e6de1d62d12eff455907b49c5378bbd", "IRPPOrgMediaCrawler._json_array": "992a18ed903cbcf5a9f3fa727de4ab1ed864f13066eb3491639f79d1aceb437d", "IRPPOrgMediaCrawler._json_object": "5105572a27e23c6e4b3eeec322c084c97f391100605b5503f3871f88d45478e2", "IRPPOrgMediaCrawler._with_embed": "0f08d1578985d002d3b4d582fc4e82b5bfa4a0abb8715d22d9f055149aa20b76", "IRPPOrgMediaCrawler._parse_date": "d559de1e3938b465a4dc50821b35c5b1156c73cc18ae3c713a3d1977bf1bb55c", "IRPPOrgMediaCrawler._media_type_from_url": "2437112a4f72badadac316ba6e52dd9d14ea92501434be45fa26789bd79340a9", "IRPPOrgMediaCrawler._parse_list_items": "4c006d9f53813d5d0dcc114e39673c77876352bc27064acf4f68213342ace02a", "IRPPOrgMediaCrawler._next_list_url": "8f17f08da0a2e326b20cfa3c52ea8d682c885a8a0abf3874ed158e3daaffd40d", "IRPPOrgMediaCrawler._meta_content": "55d6ebd542287b6aa09460ec01cb439b8d063d2a96a1134b85c88675e111f733", "IRPPOrgMediaCrawler._canonical_url": "9f6711fc7808c8f99cd5089c2eb964c88c28bd0eda168150f40488d2bbbb4a33", "IRPPOrgMediaCrawler._detail_json_url": "d2073057737ad98097cdb151267286472e29d989b8653e8a414be1fba0c01154", "IRPPOrgMediaCrawler._extract_authors": "f4a974545c5ad3f8b30acc1ca7f6a81de10190dfa59e21da9939e9c5f5826d55", "IRPPOrgMediaCrawler._extract_terms": "1c0d9e5bb015d10396fd7e1042224bb156b57483b1122035fa443b89f381da18", "IRPPOrgMediaCrawler._extract_featured_image": "3d9cd25d006c022dd34b4d0b794f91d06c417fc1ef331e151485ff4d3ae5c6f6", "IRPPOrgMediaCrawler._extract_pdf_url": "8051a375fd4198e3818d0747ba5045e4e36f77562e280a36069039b35bee7375", "IRPPOrgMediaCrawler._fallback_content_from_html": "a4737f18724e575a38b6d0245a8a5d7ecd46404a25b75392feadbb923acc6fe4", "IRPPOrgMediaCrawler._parse_detail": "93393694062d69511a09617e8b5b48aafdb157077acb66c7fb848a96eb72e96e", "IRPPOrgMediaCrawler.crawl": "651c2f94d980139d6437a67e082d707d6b1fbfb20c027574ed16573b2b1894cd"}

if __name__ == '__main__':
    unittest.main()
