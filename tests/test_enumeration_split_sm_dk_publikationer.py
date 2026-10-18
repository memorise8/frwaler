"""L3b-20261018 (X3): enumeration split for sm-dk-publikationer.
crawl() reads context/hash from the public list page, POSTs proxy.gba (versus/callback
JSON body, arg1 = page number) and stores the requested page number in
metadata.list_page. The plan sends the same body (post_listing_nested_page + dynamic
values) and the paper method takes the page number from list_position
(ext:list_page_arg). Identity (external_id = URL path) does not use the position.
Synthetic pages only (no network)."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load
from tests.enumeration_fakes import Env
from tests.test_enumeration_extensions import RecordingTransport

SITE = 'sm-dk-publikationer'
LIST = 'https://www.sm.dk/publikationer'
PROXY = LIST + '/proxy.gba'
MAIN = ('<html><body><div class="itemlist"></div><script>application.script.register(\'itemlist\', '
        '{"options":{"generator":"x"},"context":"U3ludGhldGlj+/=","hash":"0123abcdef"});</script></body></html>')
TEXT = 'Synthetic publication description with enough words to pass the minimum length rule of the crawler.'


def card(slug, date, title):
    return (f'<div class="item" data-url="{LIST}/2031/{slug}"><span class="date">{date}</span>'
            f'<h3 class="heading">{title}</h3><span class="label">Rapport</span><p>{title} teaser</p></div>')


PAGES = {1: card('a', '01-02-2031', 'Synthetic A') + card('b', '15-01-2031', 'Synthetic B'),
         2: card('c', '03-12-2030', 'Synthetic C'), 3: ''}
DETAILS = {f'{LIST}/2031/{s}': f'<html><head><meta name="description" content="{TEXT} {s}"></head><body><main>'
           f'<a href="/media/{s}.pdf">PDF</a></main></body></html>' for s in 'abc'}


def proxy_answer(page):
    return {'success': True, 'value': {'page': PAGES.get(page, ''), 'lastPage': page >= 2, 'lastGroupName': ''}}


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class RegisteredSplit(unittest.TestCase):
    def native(self):
        crawler, module = load(SITE)
        captured, posts = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        get = lambda url, **kw: MAIN if url == LIST else DETAILS.get(url)
        post = lambda ctx, h, page, **kw: posts.append((ctx, h, page)) or proxy_answer(page)
        with patch.object(module, '_curl_get', get), patch.object(module, '_curl_post_proxy', post),                 patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=None)
        self.assertEqual(len(captured), result)
        self.assertEqual([('U3ludGhldGlj+/=', '0123abcdef', p) for p in (1, 2, 3)], posts)
        return captured

    def pipeline(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        papers = []
        for n in (1, 2):
            page = parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(proxy_answer(n)), PROXY))
            for i, item in enumerate(page.items):
                item.list_position = {'page': n, 'n': n, 'position': i + 1}
                fetch = lambda url: Response(200, {'Content-Type': 'text/html'}, DETAILS[url], url)
                papers.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        end = parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(proxy_answer(3)), PROXY))
        self.assertEqual([], end.items)
        self.assertTrue(end.extra.get('explicit_empty'))
        return [x for x in papers if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        actual = self.pipeline()
        self.assertEqual(3, len(expected))
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])
        self.assertEqual([1, 1, 2], [json.loads(x['metadata'])['list_page'] for x in actual])
        self.assertEqual('publikationer/2031/c', actual[2]['external_id'])

    def test_answer_without_success_is_an_error_not_an_end(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps({'success': False}), PROXY))

    def test_identity_without_position_is_the_same(self):
        crawler, _ = load(SITE)
        item = {'url': f'{LIST}/2031/a', 'title': 'A', 'date_raw': '01-02-2031', 'labels': [], 'teaser': ''}
        a = crawler._enumeration_paper(DETAILS[item['url']], None, item)
        b = crawler._enumeration_paper(DETAILS[item['url']], {'page': 7, 'n': 7, 'position': 3}, item)
        self.assertEqual(paper_identity(clean_paper(a)), paper_identity(clean_paper(b)))

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

    def test_end_to_end_run_posts_the_crawler_body(self):
        bodies = []

        def handle(method, url, data):
            path = urlsplit(url).path
            if path == '/robots.txt':
                return Response(404, {}, b'')
            if method == 'POST' and url == PROXY:
                body = json.loads(data)
                bodies.append(body)
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(proxy_answer(body['args']['arg1'])), url)
            if url == LIST:
                return Response(200, {'Content-Type': 'text/html'}, MAIN, url)
            if url in DETAILS:
                return Response(200, {'Content-Type': 'text/html'}, DETAILS[url], url)
            return Response(404, {}, b'')
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn), transport=RecordingTransport(handle))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(3, len(docs))
        self.assertTrue(bodies)
        self.assertEqual({'U3ludGhldGlj+/='}, {b['args']['arg0']['context'] for b in bodies})
        self.assertEqual({'0123abcdef'}, {b['args']['arg0']['hash'] for b in bodies})
        self.assertEqual('GetPage', bodies[0]['method'])


ORIGINAL_SHA256 = {"_make_soup": "c66242ed5db1c235f5022e37b280ac923db9dcbe751dcaa99674519ebef8bae1", "_curl_get": "b5930dfc7ad4bce9a0de6aaafd0eb3e808ac865e4ac441f8e4a112a6af60a65a", "_curl_post_proxy": "3604e8426768a4e9b6f18480c1e4f9f72f0aaae11981f7048f255337c06bc6e4", "_extract_context_and_hash": "0c516ee7be33ac3210da0e500593e2f3e9977fbe94168913774d0b5e0612cb23", "_parse_list_html": "51791f419bd09982be0ebedc1f5c34fe6557f4cd73a1676928c1b8bdd48901ad", "_parse_date": "3daa7a35440883158251eec1a3bd6acab75e468d9fee0baebb6a90fb2034383e", "_date_to_post_number": "b54d825dc3a3d4c15b12df8b49bdebf22128b3d5e7a249f2335481e8f0521476", "_parse_detail_page": "be117cf06c945d21d4d8e5f05617ff8414dc7dfa3ce5868d4fd89427b5cc8b79", "SmDkPubikationerCrawler.crawl": "d5d94099d945abdb44286c52dec71a5045c5da2c3723d83938e42decb158dd4d"}

if __name__ == '__main__':
    unittest.main()
