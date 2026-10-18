"""ND-1 (2026-10-19): enumeration split for issibern-ch-outreach (FacetWP POST listing).
The framework POSTs crawl()'s refresh body; _enumeration_items/_enumeration_paper must give
crawl()'s papers. Only metadata.facetwp_nonce differs (a per-session value crawl() reads
from the start page and merely records; None here). Offline: synthetic pages."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'issibern-ch-outreach'
BASE = 'https://www.issibern.ch'
ENDPOINT = BASE + '/wp-json/facetwp/v1/refresh'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1,
                        'list_method': '_enumeration_items', 'list_args': ['json', 'url'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'url', 'item']},
            'listing': {'url': ENDPOINT, 'method': 'POST', 'idempotent': True, 'body_type': 'json',
                        'body': {'action': 'facetwp_refresh', 'data': {
                            'facets': {'search': '', 'n_categories': []}, 'frozen_facets': {},
                            'http_params': {'get': {}, 'uri': 'outreach/news', 'url_vars': []},
                            'template': 'news', 'extras': {}, 'soft_refresh': 1, 'is_bfcache': 1,
                            'first_load': 0, 'paged': '{page}'}},
                        'page_param': 'paged', 'start_page': 1,
                        'parser': {'type': 'adapter', 'url_field': 'url', 'date_field': 'listed_date',
                                   'title_field': 'title'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthetic outreach résumé with Korean 자료 for an offline equality test of the ISSI news split. '
NEWS = [('first-story', '23739', '13 October 2026', LONG * 2), ('second-story', '23731', '12 October 2026', LONG * 2),
        ('short-story', '23700', '1 May 2026', 'Too short.')]


def card(slug, date, text):
    return (f'<div class="news-item"><h3><a href="/{slug}/">{slug.replace("-", " ").title()}</a></h3>'
            f'<span class="news-date">{date}</span><p>{text[:40]}</p>'
            f'<span class="news-categories-item">Highlights</span><span class="news-tags-item">Planets</span></div>')


PAGE = {'template': ''.join(card(s, d, t) for s, n, d, t in NEWS),
        'settings': {'pager': {'page': 1, 'per_page': 10, 'total_rows': 3, 'total_pages': 1}}}
EMPTY = {'template': '', 'settings': {'pager': {'page': 2, 'per_page': 10, 'total_rows': 3, 'total_pages': 1}}}
START = ('<html><body><div class="facetwp-template" data-name="news"></div><script>window.FWP_JSON = '
         '{"ajaxurl":"https:\\/\\/www.issibern.ch\\/wp-json\\/facetwp\\/v1\\/refresh","nonce":"abc123"};</script>'
         '<script>window.FWP_HTTP = {"uri":"outreach\\/news"};</script></body></html>')
DETAILS = {f'{BASE}/{s}/': (f'<html><body class="post-template postid-{n}"><main id="content"><h1>{s}</h1>'
                            f'<div id="news-lead-meta-date">{d}</div><div id="news-lead-text-inner"><p>{t}</p></div>'
                            f'</main></body></html>') for s, n, d, t in NEWS}


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured = []
        def get(url, *, method='GET', json_payload=None, **kw):
            if method == 'POST':
                page = json_payload['data']['paged']
                return json.dumps(PAGE if page == 1 else EMPTY), 200, url
            if url in DETAILS:
                return DETAILS[url], 200, url
            return START, 200, url
        crawler._curl_get_text = get
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def pipeline(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, json.dumps(PAGE), ENDPOINT))
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        return page, [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in page.items]

    def test_papers_equal_crawl_except_session_nonce(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        page, papers = self.pipeline()
        self.assertEqual(3, len(page.items))
        self.assertIsNone(papers[2])  # crawl()'s short-abstract skip
        for e, a in zip(expected, papers[:2]):
            e = clean_paper(e)
            me, ma = json.loads(e.pop('metadata')), json.loads(a.pop('metadata'))
            self.assertEqual('abc123', me.pop('facetwp_nonce'))
            self.assertIsNone(ma.pop('facetwp_nonce'))
            self.assertEqual(me, ma)
            self.assertEqual(e, a)
        self.assertEqual(['23739', '23731'], [p['post_number'] for p in papers[:2]])

    def test_empty_template_is_end_and_bad_shape_fails(self):
        from crawler.enumeration.http import PermanentFailure
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items(EMPTY, ENDPOINT))
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'html': '<p/>'}, ENDPOINT)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'template': PAGE['template'], 'settings': {}}, ENDPOINT)


if __name__ == '__main__':
    unittest.main()
