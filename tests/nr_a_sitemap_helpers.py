"""NR-20261018 group A: checks for sitemap-plan splits (crawl() unchanged), memory-only.
The original crawl() reads a fixture sitemap/listing through a fake transport; the framework
adapter materializes the same sitemap URLs from the same detail bodies. Papers must match."""
import contextlib
import copy
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]


class NRSitemapChecks:
    TRANSPORTS = ('_curl_get', '_curl', '_fetch', '_get')

    def setUp(self):
        self.fixture = json.loads((ROOT / 'tests/fixtures/collector_splits' / (self.SITE + '.json')).read_text())
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (self.SITE + '.json')).read_text())
        def denied(*a, **k):
            raise AssertionError('sitemap split test attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []
        def transport(url, *a, **k):
            return fixture['bodies'].get(url, fixture.get('default_body', ''))
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            for name in self.TRANSPORTS:
                if callable(getattr(type(crawler), name, None)):
                    stack.enter_context(patch.object(crawler, name, transport, create=True))
                if callable(getattr(module, name, None)):
                    stack.enter_context(patch.object(module, name, transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(limit=None)
        return captured

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        sm = p.section('sitemap')
        xml = fixture['bodies'][sm['sitemap_url']]
        locs = [l.strip() for l in re.findall(r'<loc>([^<]+)</loc>', xml)]
        inc = [re.compile(x) for x in sm.get('include', [])]
        wanted = [l for l in locs if not inc or any(r.search(l) for r in inc)]
        def fetch(url):
            return Response(200, {'Content-Type': 'text/html'}, fixture['bodies'][url], url)
        out = []
        for loc in wanted:
            paper = adapter.materialize(Item(key=loc, url=loc, payload={'detail_url': loc}), SimpleNamespace(get=fetch))
            if paper is not None:
                out.append(paper)
        return out

    def test_papers_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertGreaterEqual(len(expected), 2)
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(len(expected), len({paper_identity(p) for p in actual}))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        self.assertEqual([clean_paper(p) for p in self.native(f)], self.pipeline(f))
