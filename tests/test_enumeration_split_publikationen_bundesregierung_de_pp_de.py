"""L3b-20261018 (X2): enumeration split for publikationen-bundesregierung-de-pp-de
(ext:post_listing + ext:post_listing_nested_page). crawl() POSTs a JSON body with the
page number at search.page; the plan sends the same body through listing.body.
crawl() also sends a CSRF header from /service/csrf; the endpoint answers without it
(checked live 2026-10-18), so no dynamic value is used. Identity (record id) unchanged.
Synthetic JSON/HTML only; no network."""
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

SITE = 'publikationen-bundesregierung-de-pp-de'
BASE = 'https://www.publikationen-bundesregierung.de'
ENDPOINT = BASE + '/pp-de/2277526!searchJson'
START = BASE + '/pp-de/herausgeber/bundesministerium-fuer-wohnen-stadtentwicklung-und-bauwesen-bmwsb-'
LONG = ('Synthetic description of a fictional federal publication used only as a regression fixture, '
        'long enough for the abstract rules of the crawler.')


def payload(i):
    return (f'<div><a class="bpa-pub-details-pp__link" href="/pp-de/publikationssuche/synthetic-{i}"></a>'
            f'<span class="bpa-pub-details-pp__title">Synthetic title {i}</span>'
            f'<span class="bpa-pub-details-pp__subtitle">Sub {i}</span>'
            f'<span class="bpa-pub-details-pp__label">BMWSB</span>'
            f'<p class="bpa-pub-details-pp__text">Teaser {i}</p>'
            f'<a class="bpa-pub-details-pp__btn" href="/resource/synthetic-{i}.pdf">PDF</a></div>')


def listing(ids, page=1, page_count=1):
    return {'result': {'count': len(ids), 'page': page, 'pageCount': page_count, 'sortOrder': 'sortDate desc',
                       'items': [{'id': i, 'payload': payload(i), 'sortDate': f'2031-0{n + 1}-02T10:00:00',
                                  'subject': ['77'], 'cssClasses': '', 'theme': ''} for n, i in enumerate(ids)]},
            'filters': [{'items': [{'value': 'subject:77', 'displayName': 'Synthetic subject'}]}]}


def detail(i, text=LONG):
    return (f'<html><head><meta name="description" content="Meta {i}"></head><body>'
            f'<div class="bpa-pub-details-pp--single-header"><h1 class="bpa-page-header-pp__title">Synthetic title {i}</h1>'
            f'<p class="bpa-page-header-pp__subtitle">Sub {i}</p></div>'
            f'<div class="bpa-pub-details-pp--single"><p class="bpa-pub-details-pp__data">Broschüre, 12 Seiten</p>'
            f'<dl class="bpa-pub-details-pp__data-list"><div><dt>Stand:</dt><dd>1. März 2031</dd></div></dl>'
            f'<time datetime="2031-03-01">1. März 2031</time>'
            f'<a class="bpa-pub-details-pp__btn" href="/resource/synthetic-{i}.pdf">PDF</a></div>'
            f'<div class="bpa-pub-details-pp__text bpa-richtext"><p>{text}</p></div></body></html>')


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class RegisteredSplit(unittest.TestCase):
    IDS = ['91001', '91002']

    def details(self, text=LONG):
        return {f'{BASE}/pp-de/publikationssuche/synthetic-{i}': detail(i, text) for i in self.IDS}

    def native(self, details):
        crawler, _ = load(SITE)
        captured, bodies = [], []
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def get(url, *, context, referer=None, method='GET', body=None, accept=None, extra_headers=None):
            if url == START:
                return '<div id="bpa-searchresultsapp-pp-react-root" data-json-base-url="/pp-de/2277526!searchJson"></div>', url
            if url == crawler.CSRF_URL:
                return '{}', url
            if url == ENDPOINT:
                bodies.append(json.loads(body))
                return json.dumps(listing(self.IDS)), url
            return details.get(url, ''), url
        crawler._curl_get_text = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.bodies = bodies
        return captured

    def pipeline(self, details, redirect=False):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, json.dumps(listing(self.IDS)), ENDPOINT))
        calls = []

        def fetch(url):
            self.assertIn(url, details, 'undeclared detail request')
            calls.append(url)
            return Response(200, {'Content-Type': 'text/html'}, details[url], url + '?moved=1' if redirect else url)
        papers = [adapter.materialize(item, SimpleNamespace(get=fetch)) for item in page.items]
        self.assertEqual(list(details), calls)
        return [x for x in papers if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.details())
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.details())
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])
        self.assertEqual(self.IDS, [x['post_number'] for x in actual])
        self.assertEqual(1, json.loads(actual[0]['metadata'])['list_page'])

    def test_plan_body_is_crawl_body_with_page_placeholder(self):
        self.native(self.details())
        body = plan()['sites'][SITE]['listing']['body']
        self.assertEqual(self.bodies[0], json.loads(json.dumps(body).replace('"{page}"', '1')))

    def test_detail_redirect_keeps_crawl_effective_url_rule(self):
        crawler, _ = load(SITE)
        moved = self.pipeline(self.details(), redirect=True)
        # crawl() stores the effective (redirected) detail URL; the split gets it as resp.url.
        self.assertTrue(all(x['url'].endswith('?moved=1') for x in moved))
        self.assertEqual(self.IDS, [x['external_id'] for x in moved])

    def test_short_abstract_is_enriched_like_crawl(self):
        expected = self.native(self.details('Kurz.'))
        self.assertEqual([clean_paper(x) for x in expected], self.pipeline(self.details('Kurz.')))

    def test_wrong_shape_is_listing_parse_error(self):
        crawler, _ = load(SITE)
        for bad in ({'error': 'x'}, [], {'result': {}}):
            with self.assertRaises(PermanentFailure) as ctx:
                crawler._enumeration_items(bad, ENDPOINT)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_original_code_unchanged(self):
        """crawl(), the methods and module helpers it uses keep their source (hashes at the split's base)."""
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
    def test_end_to_end_run_completes_with_clamped_last_page(self):
        details = self.details()
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0
        posts = []

        class Transport:
            def request(self, method, url, *, headers=None, data=None, **kw):
                if url.endswith('/robots.txt'):
                    return Response(404, {}, b'', url)
                if method == 'POST':
                    body = json.loads(data)
                    posts.append(body['search']['page'])
                    page = min(body['search']['page'], 1)  # the API clamps a page past the end
                    return Response(200, {'Content-Type': 'application/json'}, json.dumps(listing(RegisteredSplit.IDS, page)), url)
                return Response(200, {'Content-Type': 'text/html'}, details[url], url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn), transport=Transport())
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(set(self.IDS), {d['post_number'] for d in docs})
        self.assertIn(1, posts)


ORIGINAL_SHA256 = {'PublikationenBundesregierungDePpDeCrawler.__init__': '92a1f2a87b6fa015a69f4fb6d8d1a5048356d62586ca3d1d0cdfc05f38cf7b72', 'PublikationenBundesregierungDePpDeCrawler.crawl': '2870d602d8498d57596352d37efdac20318546658526055ab1fa107d46a3e9e1', 'PublikationenBundesregierungDePpDeCrawler._curl_get_text': '299d3f354f43034baba347e93095df06ec8d0d666d3e60b0e221c113bb1c6a51', 'PublikationenBundesregierungDePpDeCrawler._split_curl_meta': 'd8bd18ddb38dc01ac50a45718152d683a34437388158b318229d53b3fc566f55', 'PublikationenBundesregierungDePpDeCrawler._fetch_csrf_headers': 'e7ff82abdcbc6cc8762cb3f039bfb09e7b3a24c70ae9c0b4724bba2167aa51cb', 'PublikationenBundesregierungDePpDeCrawler._fetch_list_page': 'a7bbfeee6b84576cffa9d1e2a5ac051d118c6179256bcaac29a8a4c08417935c', 'PublikationenBundesregierungDePpDeCrawler._make_soup': 'aa21e74255a7b2831671e0ceb3555dccccc49e4bca6d44c9ce8b96c0da9776a4', 'PublikationenBundesregierungDePpDeCrawler._discover_list_endpoint': 'd5627b3cbb68fa93c7e5f0e33512cb67b14ab72a1fe9cdad8bbd6902c97c9088', 'PublikationenBundesregierungDePpDeCrawler._parse_list_records': 'd07798bf98f803ff4ac2da7eb8860f6ed9804098ebd2e677a29050bbf4861d69', 'PublikationenBundesregierungDePpDeCrawler._subject_map': '04ba0ed49787fbc5b2727ecc199f9e461fba069ec7b0be2c81ccb91afeecadd5', 'PublikationenBundesregierungDePpDeCrawler._parse_list_payload': 'd9feeae87b3ec4d305fb29813a33cbde23f063b7bdc6c8cdfcb003dca53094c7', 'PublikationenBundesregierungDePpDeCrawler._parse_detail': '0bf9a814e6a1a0c83b1c7046d3f8b66aed97129fb5c60cd30aedda8f3348de15', 'PublikationenBundesregierungDePpDeCrawler._clean_text': 'c4217d885f696f356e83340babc2fd7e7af5e05e77cabc91fff67e54c938a5e3', 'PublikationenBundesregierungDePpDeCrawler._select_text': 'e0ef9707069778cb36017ae4e167a4aaa15d87a88ff6c44cf1026ecf292fc323', 'PublikationenBundesregierungDePpDeCrawler._meta_content': '9d028ee57162f3987084134b66daa257fdfd7f32f23ec0bee40ed9c51c6e545f', 'PublikationenBundesregierungDePpDeCrawler._title_tag': '52562275fa3647049d37a91c974752ab39c0164189b0556662a9eb0cb52f16c6', 'PublikationenBundesregierungDePpDeCrawler._join_title': 'b61d3971fa2c8c7ee4542b93108e4c2d82029dc9ccf975f70e79170d07023916', 'PublikationenBundesregierungDePpDeCrawler._dedupe': 'e1a558d26f125ab027bc7d8fb5592545edfc7e6fa6fb8d70c5be788ad3594245', 'PublikationenBundesregierungDePpDeCrawler._to_int': '5b8b9246df57f21bc6f756239f46a668ad632945d98bd72b92017fc4b396ec00', 'PublikationenBundesregierungDePpDeCrawler._parse_date': '54c2424c76702b49260a46670fa54fd6ebbb70c3b6698506cb3d8516242f300f'}

if __name__ == '__main__':
    unittest.main()
