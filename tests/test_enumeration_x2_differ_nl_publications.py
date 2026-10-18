"""L3b X2 (ext:two_step_keep_first): differ-nl-publications — thesis list (?page=N from 0), bibcite
detail page (first step) and the external record its 'URL' row links to (second step, only hosts
in allow_hosts). _parse_detail/_parse_external_record run unchanged on the fetched pages, so the
paper equals crawl()'s. An external record the plan did not fetch is an item failure.
crawl() is unchanged. Synthetic HTML only, no network."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'differ-nl-publications'
ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.x2.json')).read_text())['sites'][SITE]
START = 'https://www.differ.nl/publications?author=&year=&type=thesis&combine='
ABSTRACT = 'Synthetic thesis abstract for the offline enumeration test; it imitates a repository record only.'


def listing(ids):
    rows = ''.join(f'<div class="views-row"><div class="views-field-title"><a href="/bibcite/reference/{i}">Thesis {i}</a></div>'
                   f'<div class="views-field-author-target-id-1"><span class="field-content">A. Author</span></div>'
                   f'<div class="views-field-bibcite-type-of-work"><span class="field-content">, PhD (2020/10/1{i % 10})</span></div></div>'
                   for i in ids)
    empty = '' if ids else '<div class="view-empty">No results</div>'
    return f'<html><body><div class="view-differ-repository">{rows}{empty}</div></body></html>'


def detail(i, url):
    url_row = f'<tr><td>URL</td><td><a href="{url}">{url}</a></td></tr>' if url else ''
    return (f'<html><body><h1 class="title">Thesis {i}</h1><div class="bibcite-reference"><table>'
            f'<tr><td>Author</td><td><div class="field__item">A. Author</div></td></tr>'
            f'<tr><td>Date Published</td><td>10/2020</td></tr><tr><td>University</td><td>Synthetic University</td></tr>'
            f'{url_row}<tr><td>PId</td><td>tue:{i}</td></tr></table></div>'
            f'<div class="bibcite-citation">A. Author. Thesis {i}. Synthetic University, 2020.</div></body></html>')


def record(i):
    return (f'<html><head><meta name="citation_title" content="Thesis {i}"><meta name="citation_author" content="Author, A.">'
            f'<meta name="citation_pdf_url" content="https://research.tue.nl/files/{i}/thesis.pdf">'
            f'<meta name="citation_publication_date" content="2020/10/16"></head><body>'
            f'<div class="rendering_researchoutput_abstractportal"><div class="textblock">{ABSTRACT}</div></div>'
            f'<div class="rendering_researchoutput_keywordgroupsportal"><ul><li>plasma</li></ul></div></body></html>')


TUE = 'https://research.tue.nl/en/publications/thesis-{}'
URLS = {31: TUE.format(31), 32: TUE.format(32), 33: None}


class X2KeepFirst(unittest.TestCase):
    def setUp(self):
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('io')
            raise AssertionError('attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))
        self.pages = {START: listing(list(URLS))}
        for i, u in URLS.items():
            self.pages[f'https://www.differ.nl/bibcite/reference/{i}'] = detail(i, u)
            if u:
                self.pages[u] = record(i)

    def native(self):
        crawler, _ = load(SITE)
        crawler._delay = 0
        crawler._last_save_created = True
        saved = []
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._curl_get = lambda url, referer=None: self.pages.get(url)
        with contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl()
        return saved

    def test_papers_equal_crawl(self):
        expected = self.native()
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.pages[START], START)
        got = []
        for row in rows:
            first = self.pages[row['detail_url']]
            second = self.pages.get(URLS[int(row['external_id'])]) or first
            paper = crawler._enumeration_paper(second, first, row)
            if paper is not None:
                got.append(paper)
        self.assertEqual(expected, got)
        self.assertEqual(['31', '32'], [p['external_id'] for p in got])  # 33: no URL row, citation under 50 chars: skipped as in crawl()
        self.assertTrue(all(p['pdf_url'].startswith('https://research.tue.nl/files/') for p in got))

    def test_external_record_not_fetched_fails_the_item(self):
        crawler, _ = load(SITE)
        row = crawler._enumeration_items(listing([41]), START)[0]
        first = detail(41, 'https://repository.example.org/handle/41')  # host outside allow_hosts: no second step
        with self.assertRaises(ValueError):
            crawler._enumeration_paper(first, first, row)

    def test_plan_end_to_end(self):
        from tests.enumeration_fakes import Env
        site_plan({'version': 1, 'sites': {SITE: PLAN}}, SITE, registered=[SITE])
        plan = dict(PLAN, request_interval_s=0)
        urls = []

        def handler(url):
            urls.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if parts.path == '/publications':
                page = int(parse_qs(parts.query).get('page', ['0'])[0])
                return Response(200, {'Content-Type': 'text/html'}, listing(list(URLS) if page == 0 else []), url)
            if url in self.pages:
                return Response(200, {'Content-Type': 'text/html'}, self.pages[url], url)
            return Response(404, {}, b'')
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertIn(result['status'], ('complete', 'complete_with_review'), result)
        self.assertEqual(['31', '32'], sorted(d['post_number'] for d in docs))
        self.assertIn(TUE.format(31), urls)


if __name__ == '__main__':
    unittest.main()
