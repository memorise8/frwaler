"""rigsrevisionen-dk-revisionssager split: sitemap plan (include = crawl's _ITEM_PATH_RE) +
crawl's _parse_detail. Memory only; no network, process or database."""
import contextlib
import copy
import io
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'rigsrevisionen-dk-revisionssager'
ROOT = Path(__file__).resolve().parents[1]
B = 'https://www.rigsrevisionen.dk'
LONG = 'Syntetisk tekst til L2b-regressionstest, résumé og 자료 holdt ens mellem crawl og plan. ' * 3
U1 = B + '/revisionssager-arkiv/2019/dec/beretning-om-indkoeb'
U2 = B + '/revisionssager-arkiv/2012/jan/notat-om-projekt'
SITEMAP = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'<url><loc>{U1}</loc></url><url><loc>{U2}</loc></url>'
           f'<url><loc>{B}/revisionssager-arkiv/2017/jan</loc></url>'
           f'<url><loc>{B}/nyheder/2024/nov/notat-til-statsrevisorerne-4</loc></url></urlset>')


def detail(title, date, text, pdf):
    return (f'<html><body><span class="label">Beretning</span><h1>{title}</h1><span data-date="{date}T00:00:00"></span>'
            f'<div class="rich-text"><p>{text}</p></div>'
            f'<a href="/Media/1/{pdf}">Hele beretningen</a></body></html>')


DETAILS = {U1: detail('Beretning om indkøb', '2019-12-06', LONG, 'SR0819.pdf'),
           U2: detail('Notat om projekt', '2012-01-31', LONG, 'c302-12.pdf')}


class RigsrevisionenSplit(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / f'{SITE}.json').read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, side_effect=AssertionError('real I/O forbidden')); p.start(); self.addCleanup(p.stop)

    def native(self, details):
        crawler, _ = load(SITE)
        captured = []
        crawler.detail_delay = 0
        crawler._CRAWL_BUDGET_SECS = 3600
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = lambda url, **kw: SITEMAP if url.endswith('sitemap.xml') else details[url]
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def pipeline(self, details):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        include = [re.compile(x) for x in p.section('sitemap')['include']]
        locs = re.findall(r'<loc>([^<]+)</loc>', SITEMAP)
        wanted = [u for u in locs if any(r.search(u) for r in include)]
        self.assertEqual([U1, U2], wanted)
        fetch = SimpleNamespace(get=lambda url: Response(200, {'Content-Type': 'text/html'}, details[url], url))
        out = []
        for u in wanted:
            paper = adapter.materialize(Item(key=u, url=u, payload={'detail_url': u}), fetch)
            if paper is not None:
                out.append(paper)
        return out

    def test_papers_and_identity_equal_original_crawl(self):
        expected = self.native(DETAILS)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(DETAILS)
        self.assertEqual(sorted([clean_paper(p) for p in expected], key=lambda p: p['url']),
                         sorted(actual, key=lambda p: p['url']))
        self.assertEqual({paper_identity(p) for p in expected}, {paper_identity(p) for p in actual})

    def test_short_abstract_skip_is_preserved(self):
        short = {u: b.replace(LONG, 'Kort.') for u, b in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.pipeline(short))

    def test_include_matches_crawl_item_rule(self):
        crawler, _ = load(SITE)
        include = re.compile(self.plan['sites'][SITE]['sitemap']['include'][0])
        for loc in re.findall(r'<loc>([^<]+)</loc>', SITEMAP):
            self.assertEqual(bool(include.search(loc)), bool(crawler._ITEM_PATH_RE.match(loc[len(B):])), loc)


if __name__ == '__main__':
    unittest.main()
