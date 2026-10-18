"""L3b X2 (ext:multi_listing): arcnl-nl-publications reads its three delivered thesis lists in one
plan (explicit segments bachelorthesis/masterthesis/phdthesis, path paging /page/N). Rows and
papers equal crawl()'s own conversion (_list_page_links, _parse_detail, _paper with the list
scope as category). crawl() is unchanged. Synthetic HTML only, no network."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'arcnl-nl-publications'
ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.x2.json')).read_text())['sites'][SITE]
FRAME = ('<html><body class="{body}"><header><a href="https://arcnl.nl/about">About</a>'
         '<a href="https://arcnl.nl/publications/types/phdthesis">Theses</a></header>'
         '<section class="section-search-directory"><h1>Publications</h1></section>{rows}</body></html>')
ABSTRACT = 'Synthetic thesis summary used only by the offline enumeration test; it imitates the page layout.'


def listing(scope, slugs):
    rows = ''.join(f'<li class="list__item list__item--archive"><div class="item item--publication">'
                   f'<a class="item__title" href="https://arcnl.nl/publications/{s}">{s}</a></div></li>' for s in slugs)
    return FRAME.format(body=f'archive tax-publication_type term-{scope}',
                        rows=f'<ul class="list">{rows}</ul>' if rows else '')


def detail(slug, post_id, abstract=ABSTRACT):
    return (f'<html><body class="single postid-{post_id}"><div class="article__main"><h1>Thesis {slug}</h1>'
            f'<div class="meta meta--publication"><table><tr><th>Publication date</th><td>3 March 2024</td></tr>'
            f'<tr><th>Reference</th><td>A. Author, Thesis {slug}, University of Amsterdam, UvA, 2024-03-03</td></tr>'
            f'<tr><th>Group</th><td>Synthetic Group</td></tr></table></div>'
            f'<section class="excerpt"><p>{abstract}</p></section>'
            f'<div class="cta cta--apply"><a href="https://request.pub.arcnl.nl/request?att_id={slug}">Download (pre)print</a></div>'
            f'</div></body></html>')


PAGES = {('bachelorthesis', 1): ['b1', 'b2'], ('masterthesis', 1): ['m1'], ('masterthesis', 2): ['m2'],
         ('phdthesis', 1): ['p1']}
DETAILS = {f'https://arcnl.nl/publications/{s}': detail(s, 100 + i)
           for i, s in enumerate(['b1', 'b2', 'm1', 'm2', 'p1'])}


class X2MultiListing(unittest.TestCase):
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

    def test_rows_and_papers_equal_crawl_conversion(self):
        crawler, _ = load(SITE)
        native, _ = load(SITE)
        body = listing('masterthesis', ['m1', 'm2'])
        native._fetch = lambda url: body if '/types/' in url else DETAILS[url]
        links = native._list_page_links(2, 'https://arcnl.nl/publications/types/masterthesis')
        rows = crawler._enumeration_items(body, 'https://arcnl.nl/publications/types/masterthesis/page/2')
        self.assertEqual(links, [r['url'] for r in rows])
        self.assertEqual({'masterthesis'}, {r['category'] for r in rows})
        for row in rows:
            expected = native._paper(native._parse_detail(row['url']), row['url'], 'masterthesis')
            self.assertEqual(expected, crawler._enumeration_paper(DETAILS[row['url']], row))
        self.assertEqual('101', crawler._enumeration_paper(DETAILS[rows[0]['url'].replace('m1', 'b2')],
                                                           dict(rows[0], url=rows[0]['url'].replace('m1', 'b2')))['external_id'])

    def test_skips_and_failures_follow_crawl(self):
        crawler, _ = load(SITE)
        row = {'url': 'https://arcnl.nl/publications/x', 'category': 'phdthesis'}
        self.assertIsNone(crawler._enumeration_paper(detail('x', 9, abstract='short'), row))
        with self.assertRaises(ValueError):
            crawler._enumeration_paper('<html><body>maintenance</body></html>', row)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(listing('phdthesis', ['p1']), 'https://arcnl.nl/publications/types/other')

    def test_plan_reads_three_lists_end_to_end(self):
        from tests.enumeration_fakes import Env
        site_plan({'version': 1, 'sites': {SITE: PLAN}}, SITE, registered=[SITE])
        plan = dict(PLAN, request_interval_s=0)
        urls = []

        def handler(url):
            urls.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in DETAILS:
                return Response(200, {'Content-Type': 'text/html'}, DETAILS[url], url)
            bits = parts.path.strip('/').split('/')
            if bits[:2] == ['publications', 'types']:
                page = int(bits[4]) if len(bits) > 4 else 1
                slugs = PAGES.get((bits[2], page), [])
                body = listing(bits[2], slugs) if slugs else FRAME.format(body=f'paged-{page}', rows='')
                return Response(200, {'Content-Type': 'text/html'}, body, url)
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
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['100', '101', '102', '103', '104'], sorted(d['post_number'] for d in docs))
        self.assertIn('https://arcnl.nl/publications/types/masterthesis/page/2', urls)
        self.assertIn('https://arcnl.nl/publications/types/phdthesis', urls)


if __name__ == '__main__':
    unittest.main()
