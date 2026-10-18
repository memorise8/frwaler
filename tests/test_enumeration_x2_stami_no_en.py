"""L3b X2: stami-no-en through the framework — path paging /en/publication/page/{page}/?fwp_...
(first page without /page/1/), detail page plus the WordPress REST record as a second step built
from the row's post id (ext:detail_aux_request + ext:two_step_keep_first). The paper equals what
crawl() saves; the curl HEAD filename lookup is never run inside the adapter (such an item fails).
crawl() is unchanged. Synthetic HTML/JSON only, no network."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'stami-no-en'
ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.x2.json')).read_text())['sites'][SITE]
START = 'https://stami.no/en/publication/?fwp_publikasjontype=academic-article'
ABSTRACT = 'Synthetic abstract for the offline enumeration test; it imitates the layout of a publication page only.'


def listing(ids):
    rows = ''.join(f'<article id="post-{i}" class="post-list-item publication"><time datetime="2024">2024</time>'
                   f'<h2 class="post-list-item-title"><a href="https://stami.no/en/publication/p{i}/">Paper {i}</a></h2>'
                   f'<p class="post-list-item-excerpt"><span>A. Author</span></p></article>' for i in ids)
    return f'<html><body><main>{rows}</main></body></html>'


def detail(i, link='https://doi.org/10.1000/xyz{i}', rest=True):
    rest_link = (f'<link rel="alternate" type="application/json" href="https://stami.no/en/wp-json/wp/v2/publication/{i}">'
                 if rest else '')
    return (f'<html><head>{rest_link}<script type="application/ld+json">{{"@graph":[{{"datePublished":"2025-01-0{i % 9 + 1}T10:00:00+00:00"}}]}}</script>'
            f'</head><body class="postid-{i}"><h1 class="header-title">Paper {i}</h1><div class="publication-type">Academic Article</div>'
            f'<div class="meta-block published">2024</div>'
            f'<div class="content-entry intro-text-container publication-container"><p>{ABSTRACT}</p>'
            f'<div class="references people"><span>A. Author</span><span>B. Author</span></div>'
            f'<div class="references references-secondary"><span class="reference">Journal X, 12 (3)</span>'
            f'<a href="{link.format(i=i)}">Read publication</a></div></div></body></html>')


def rest(i):
    return {'id': i, 'date': '2025-02-03T10:00:00', 'date_gmt': '2025-02-03T09:00:00', 'modified': '2025-03-01T10:00:00',
            'modified_gmt': '2025-03-01T09:00:00', 'slug': f'p{i}', 'status': 'publish', 'type': 'publication',
            'link': f'https://stami.no/en/publication/p{i}/'}


class X2TwoStep(unittest.TestCase):
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

    def native(self, ids, details, records):
        """crawl() itself, fed from synthetic pages; returns the papers it saves."""
        crawler, _ = load(SITE)
        crawler._delay = 0
        crawler._last_save_created = True
        saved = []
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._get_text = lambda url, referer=None: listing(ids) if url == START else details.get(url)
        crawler._get_json = lambda url, referer=None: records.get(url.rsplit('/', 1)[1])
        with contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl()
        return saved

    def test_paper_equals_crawl(self):
        ids = [11, 12]
        details = {f'https://stami.no/en/publication/p{i}/': detail(i) for i in ids}
        records = {str(i): rest(i) for i in ids}
        expected = self.native(ids, details, records)
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(listing(ids), START)
        got = [crawler._enumeration_paper(json.dumps(records[r['post_id']]), details[r['url']], r) for r in rows]
        self.assertEqual(expected, got)
        self.assertEqual(['11', '12'], [p['external_id'] for p in got])
        # Unreadable REST record: crawl() goes on with {}; so does the method.
        expected = self.native(ids, details, {})
        got = [crawler._enumeration_paper('<html>not json</html>', details[r['url']], r) for r in rows]
        self.assertEqual(expected, got)
        page2 = crawler._enumeration_items(listing(ids), 'https://stami.no/en/publication/page/2/?fwp_publikasjontype=academic-article')
        self.assertEqual({'https://stami.no/en/publication/page/2/?fwp_publikasjontype=academic-article'},
                         {r['list_endpoint'] for r in page2})

    def test_head_lookup_and_post_id_mismatch_fail_the_item(self):
        crawler, _ = load(SITE)
        row = crawler._enumeration_items(listing([13]), START)[0]
        with self.assertRaises(ValueError):  # dotless PDF link: crawl() would run curl -I
            crawler._enumeration_paper('{}', detail(13, link='https://stami.no/download?format=pdf'), row)
        with self.assertRaises(ValueError):
            crawler._enumeration_paper('{}', detail(14), row)
        short = detail(13).replace(ABSTRACT, 'short')
        self.assertIsNone(crawler._enumeration_paper(json.dumps(rest(13)), short, row))

    def test_plan_end_to_end(self):
        from tests.enumeration_fakes import Env
        site_plan({'version': 1, 'sites': {SITE: PLAN}}, SITE, registered=[SITE])
        plan = dict(PLAN, request_interval_s=0)
        urls = []

        def handler(url):
            urls.append(url)
            path = urlsplit(url).path
            if path == '/robots.txt':
                return Response(404, {}, b'')
            if url == START:
                return Response(200, {'Content-Type': 'text/html'}, listing([21, 22]), url)
            if path.startswith('/en/wp-json/wp/v2/publication/'):
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(rest(int(path.rsplit('/', 1)[1]))), url)
            if path in ('/en/publication/p21/', '/en/publication/p22/'):
                return Response(200, {'Content-Type': 'text/html'}, detail(int(path.strip('/')[-2:])), url)
            return Response(404, {'Content-Type': 'text/html'}, '<html>Not found</html>', url)
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
        self.assertEqual(['21', '22'], sorted(d['post_number'] for d in docs))
        self.assertIn('https://stami.no/en/wp-json/wp/v2/publication/21', urls)
        self.assertIn('https://stami.no/en/publication/page/2/?fwp_publikasjontype=academic-article', urls)


if __name__ == '__main__':
    unittest.main()
