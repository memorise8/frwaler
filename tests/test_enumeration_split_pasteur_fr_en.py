"""L4-20261018: pasteur-fr-en on the redesigned site (new Drupal node ids).

Identity rule (user decision 2026-10-18, product dedup key = (site_id, post_number, meta_url)):
documents in crawler/sites/custom/pasteur-fr-en.node-map.csv keep their registered
post_number (old node id) AND their registered meta_url byte for byte; the new address
goes to metadata.current_url. Pages not in the table get post_number "n<new node id>"
and the new address. Values below are real rows of the table (operations DB + 301 Location).
Memory-only: no network, no database."""
import ast
import hashlib
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load

SITE = 'pasteur-fr-en'
NEW = 'https://www.pasteur.fr/en/whats-new/press-area/press-releases-and-press-kits/'
# (old node id, registered meta_url, new slug) — one per historical URL pattern
MAPPED = [
    ('21255', 'https://www.pasteur.fr/en/press-area/press-documents/human-protein-inherited-bacteria-reveals-overlooked-aspect-human-immunity',
     'human-protein-inherited-bacteria-reveals-overlooked-aspect-human-immunity'),
    ('153', 'https://www.pasteur.fr/en/patronage-institut-pasteur-and-total-foundation-renew-their-common-commitment-against-infectious',
     'patronage-institut-pasteur-and-total-foundation-renew-their-common-commitment-against-infectious'),
    ('21825', 'https://www.pasteur.fr/en/home/press-area/press-documents/senescence-behind-aggressive-nature-postpartum-breast-cancer',
     'senescence-behind-aggressive-nature-postpartum-breast-cancer'),
    ('20363', 'https://www.pasteur.fr/en/home/research-journal/press-documents/resistance-mutations-nirsevimab-are-rare-respiratory-syncytial-virus-rsv?language=fr',
     'resistance-mutations-nirsevimab-are-rare-respiratory-syncytial-virus-rsv'),
]
BODY = ('Synthetic press-release body with résumé and 자료 for the offline identity test. ' * 3).strip()
ORIGINAL_METHOD_SHA256 = {  # crawl() and the original parsers must stay byte-identical
    'crawl': 'c4998f336a71799f023d6cff6b4482b121d6603445481c91b895baadf411e7f4',
    '_parse_list': '869fb78b734ac30c0da39297a6e501c167e98d7404f70c0c468b76384ae6b5d7',
    '_parse_detail': '166b4c7a573143a396b6e16f56df0ff57d103141fc11a6787840f2336020a104',
    '_paper_from_parsed': '65710893dcdeef435fe4f0b88f4a0623490a8448a093d83cb387db52dcca0c93',
    '_extract_node_id': 'f4b9927e194eea924c5cfcaf6136706e7cbe7cc908499de3e31272987b50b3da',
}


def teaser(slug, label='Press Release', date='2025-07-25'):
    return (f'<div class="views-row"><article class="teaser"><div class="teaser__content">'
            f'<p class="teaser__label"><span class="tag -light"><span>{label}</span></span></p>'
            f'<h3 class="teaser__heading"><a class="teaser__link" href="/en/whats-new/press-area/press-releases-and-press-kits/{slug}">'
            f'<span>Title {slug[:20]}</span></a></h3><p class="teaser__meta"><time datetime="{date}">x</time></p></div></article></div>')


def listing(*rows):
    return ('<html><body><main><div class="js-view-dom-id-abc">' + ''.join(rows) +
            '</div><nav class="pager"><a href="?page=1">next</a></nav></main></body></html>')


def detail(node_id, title='Detail title', body=BODY):
    ld = json.dumps({'@context': 'https://schema.org', '@type': 'NewsArticle', 'headline': title,
                     'datePublished': '2025-07-25T00:00:00+00:00', 'articleBody': body}, ensure_ascii=False)
    settings = json.dumps({'path': {'currentPath': f'node/{node_id}'}}).replace('/', '\\/')
    return (f'<html><head><meta property="og:description" content="Short teaser only.">'
            f'<script type="application/ld+json">{ld}</script>'
            f'<script type="application/json" data-drupal-selector="drupal-settings-json">{settings}</script></head>'
            f'<body class="path-node page-node-type-document-presse"><main><h1 class="hero__title">{title}</h1>'
            f'<time class="hero__date">25 July 2025</time></main></body></html>')


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class PasteurNewSite(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('network/process I/O attempted')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.crawler, self.module = load(SITE)
        type(self.crawler)._node_map_cache = None
        self.addCleanup(lambda: setattr(type(self.crawler), '_node_map_cache', None))

    def adapter(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        return p, Adapter(SITE, p.section('adapter'), None, crawler=self.crawler)

    def through_plan(self, slug, node_id, body=BODY):
        p, adapter = self.adapter()
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, listing(teaser(slug)), p.listing['url']))
        self.assertEqual(1, len(page.items))
        fetch = lambda url: Response(200, {'Content-Type': 'text/html'}, detail(node_id, body=body), url)
        return adapter.materialize(page.items[0], type('F', (), {'get': staticmethod(fetch)})())

    def redirecting_fetcher(self, node_id, query=''):
        """Old registered URL -> 301 -> new page: the fetcher returns the final response
        (Fetcher.get follows redirects hop by hop; Response.url is the final URL)."""
        old_to_new = {old: NEW + slug + query for _, old, slug in MAPPED}
        calls = []
        def get(url):
            calls.append(url)
            final = old_to_new.get(url, url)
            return Response(200, {'Content-Type': 'text/html'}, detail(node_id), final)
        return type('F', (), {'get': staticmethod(get)})(), calls

    # 1
    def test_mapped_page_keeps_old_node_id(self):
        paper = self.through_plan(MAPPED[0][2], '3001')
        self.assertEqual('21255', paper['post_number'])
        self.assertEqual('21255', paper_identity(paper)[0])

    # 2
    def test_mapped_meta_url_is_registered_value_byte_for_byte(self):
        for old_id, old_url, slug in MAPPED:
            with self.subTest(old_id=old_id):
                paper = self.through_plan(slug, '3002')
                post_number, meta_url = paper_identity(paper)
                self.assertEqual(old_id, post_number)
                self.assertEqual(old_url.encode('utf-8'), meta_url.encode('utf-8'))

    # 3
    def test_new_address_and_new_node_id_go_to_metadata(self):
        paper = self.through_plan(MAPPED[2][2], '3233')
        meta = json.loads(paper['metadata'])
        self.assertEqual(NEW + MAPPED[2][2], meta['current_url'])
        self.assertEqual('3233', meta['new_node_id'])
        self.assertEqual('node_map_old_node_id', meta['identity_source'])

    # 4
    def test_unmapped_page_gets_n_prefixed_new_node_id_and_new_address(self):
        slug = 'gut-microbes-help-set-bodys-response-vaccination'
        paper = self.through_plan(slug, '3783')
        self.assertEqual(('n3783', NEW + slug), paper_identity(paper))
        self.assertEqual('new_site_node_id', json.loads(paper['metadata'])['identity_source'])

    # 5
    def test_only_press_release_rows_are_listed(self):
        rows = self.crawler._enumeration_items(listing(
            teaser('a-release'), teaser('a-kit', label='Press Kit'), teaser('a-feature', label='In-depth Feature')))
        self.assertEqual([NEW + 'a-release'], [r['url'] for r in rows])

    # 6
    def test_query_string_on_new_path_maps_to_same_key(self):
        old_id, old_url, slug = MAPPED[3]
        table = self.crawler._node_map()
        self.assertEqual((old_id, old_url), table[self.crawler._path_key(NEW + slug + '?language=fr')])
        self.assertEqual((old_id, old_url), table[self.crawler._path_key('/en/whats-new/press-area/press-releases-and-press-kits/' + slug + '/')])

    # 7
    def test_missing_or_malformed_table_stops_without_a_paper(self):
        import os, tempfile
        cls = type(self.crawler)
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (('missing.csv', None), ('cols.csv', 'old_node_id,new_path\n1,/x\n')):
                path = os.path.join(tmp, name)
                if content is not None:
                    open(path, 'w').write(content)
                with self.subTest(case=name), patch.object(cls, 'NODE_MAP_FILE', path):
                    cls._node_map_cache = None
                    with self.assertRaises(PermanentFailure):
                        self.crawler._enumeration_paper(detail('3001'), NEW + MAPPED[0][2])
        cls._node_map_cache = None

    # 8
    def test_crawl_and_original_parsers_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in ORIGINAL_METHOD_SHA256.items():
            self.assertEqual(digest, actual[name], name)

    # 10 (trial 20261018-tpasteur: identity must be computable from the registered URL alone)
    def test_identity_from_registered_url_only_equals_operations_db(self):
        from crawler.enumeration.parsing import Item
        p, adapter = self.adapter()
        self.assertFalse(adapter.identity_uses_list_fields())
        for old_id, old_url, slug in MAPPED:
            with self.subTest(old_id=old_id):
                query = '?language=fr' if '?' in old_url else ''
                fetcher, calls = self.redirecting_fetcher('3100', query)
                ident = adapter.identity(Item(key=old_url, url=old_url), fetcher)
                self.assertEqual([old_url], calls)
                self.assertEqual((old_id, old_url), ident)
                self.assertEqual(old_url.encode('utf-8'), ident[1].encode('utf-8'))

    # 9
    def test_body_from_article_json_and_short_body_skip_and_end_page(self):
        paper = self.through_plan(MAPPED[0][2], '3001')
        self.assertEqual(BODY, paper['abstract'])  # not the short og:description
        self.assertIsNone(self.through_plan(MAPPED[0][2], '3001', body='Short.'))
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        end = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                    '<html><main><div class="js-view-dom-id-x"></div><nav class="pager"></nav></main></html>',
                                    p.listing['url'] + '?page=57'))
        self.assertTrue(end.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', p.listing['url']))
        self.assertTrue(err.extra.get('parse_error'))


class NodeMapFile(unittest.TestCase):
    def test_table_has_314_unique_rows_with_three_columns(self):
        import csv
        rows = list(csv.DictReader(open(ROOT / 'crawler/sites/custom/pasteur-fr-en.node-map.csv', newline='', encoding='utf-8')))
        self.assertEqual(314, len(rows))
        self.assertEqual(['old_node_id', 'old_meta_url', 'new_path'], list(rows[0].keys()))
        self.assertEqual(314, len({r['old_node_id'] for r in rows}))
        self.assertEqual(314, len({r['new_path'].split('?')[0] for r in rows}))


if __name__ == '__main__':
    unittest.main()
