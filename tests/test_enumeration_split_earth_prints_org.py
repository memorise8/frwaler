"""L2a (2026-10-18): enumeration methods for earth-prints-org (DSpace 7 REST).
The enumeration path must produce the paper the unchanged crawl() saves
(external_id = item uuid, url = handle page). Offline: synthetic JSON."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'earth-prints-org'
BASE = 'https://www.earth-prints.org'
API = BASE + '/server/api'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1,
                        'list_method': '_enumeration_items', 'list_args': ['json'],
                        'detail_method': '_enumeration_detail', 'detail_args': ['json'],
                        'paper_method': '_enumeration_paper', 'paper_args': ['item', 'detail']},
            'listing': {'url': API + '/discover/search/objects',
                        'params': {'configuration': 'researchoutputs', 'size': 25, 'page': 0,
                                   'sort': 'dc.date.accessioned,DESC',
                                   'f.has_content_in_original_bundle': 'true,equals'},
                        'page_param': 'page', 'start_page': 0,
                        'parser': {'type': 'adapter', 'key_field': 'uuid', 'url_field': 'api_url',
                                   'title_field': 'title'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthetic volcanology abstract résumé with Korean 자료 for an offline equality test. ' * 3


def meta(**kv):
    return {k: [{'value': v}] for k, v in kv.items()}


def item(uuid, handle, abstract=LONG):
    return {'uuid': uuid, 'handle': handle, 'name': f'Paper {uuid}',
            'metadata': meta(**{'dc.title': f'Paper {uuid}', 'dc.description.abstract': abstract,
                                'dc.date.issued': '2026-10-12', 'dc.date.accessioned': '2026-10-13T08:00:00Z',
                                'dc.contributor.author': 'Rossi, M.', 'dc.type': 'article'})}


def detail(uuid, handle, abstract=LONG):
    d = item(uuid, handle, abstract)
    d['_embedded'] = {'bundles': {'_embedded': {'bundles': [{'name': 'ORIGINAL', '_embedded': {'bitstreams': {
        '_embedded': {'bitstreams': [{'uuid': 'b-' + uuid, 'name': f'{uuid}.pdf', 'sizeBytes': 10,
                                      '_links': {'content': {'href': f'{API}/core/bitstreams/b-{uuid}/content'}}}]}}}}]}}}
    return d


def listing(items):
    return {'_embedded': {'searchResult': {'_embedded': {'objects': [
        {'_embedded': {'indexableObject': i}} for i in items]}, '_links': {}}}}


LIST = listing([item('u1', '2122/1'), item('u2', '2122/2')])
EMPTY = listing([])


def details(abstract=LONG):
    return {f'{API}/core/items/{u}?embed=bundles/bitstreams': detail(u, h, abstract)
            for u, h in (('u1', '2122/1'), ('u2', '2122/2'))}


class Enumeration(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, det):
        crawler, _ = load(SITE)
        captured, calls = [], []

        def transport(url, context=None):
            if url in det:
                return copy.deepcopy(det[url])
            calls.append(url)
            return copy.deepcopy(LIST if len(calls) == 1 else EMPTY)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_json = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, det):
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, json.dumps(LIST), API + '/discover/search/objects?page=0')).items
        self.assertEqual(list(det), [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'application/json'}, json.dumps(det[u]), u)
        papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native(details())
        self.assertEqual(['u1', 'u2'], [p['external_id'] for p in expected])
        self.assertEqual(BASE + '/handle/2122/1', expected[0]['url'])
        actual = self.pipeline(details())
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        self.assertEqual([], self.native(details('Short.')))
        self.assertEqual([], self.pipeline(details('Short.')))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = API + '/discover/search/objects?page=677'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(EMPTY), url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            bad = parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"error":"maintenance"}', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'))
        else:
            self.assertTrue(bad.extra.get('parse_error'))
            self.assertFalse(bad.extra.get('explicit_empty', False))


if __name__ == '__main__':
    unittest.main()
