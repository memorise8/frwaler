"""NR-20261018: nhlbi-nih-gov-about enumeration split (crawl() unchanged), memory-only.
crawl() reads three fixed source pages and builds papers from their rows; the plan reads the
same pages as explicit segments and converts rows with _enumeration_paper (no detail page)."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'nhlbi-nih-gov-about'
ROOT = Path(__file__).resolve().parents[1]


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        def denied(*a, **k):
            raise AssertionError('attempted I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, fixture):
        crawler, module = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', lambda url, *a, **k: fixture['bodies'].get(url)), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured

    def pipeline(self, fixture):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        out = []
        for seg in p.section('segments')['items']:
            url = seg.get('url') or p.listing['url'].replace('{segment}', seg['key'])
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, fixture['bodies'][url], url + '?page=0'))
            for item in page.items:
                paper = adapter.materialize(item, SimpleNamespace(get=None))
                if paper is not None:
                    out.append(paper)
        return out

    def test_papers_equal_original_crawl(self):
        expected = self.native(self.fixture)
        self.assertEqual(6, len(expected))
        actual = self.pipeline(self.fixture)
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual(6, len({paper_identity(x) for x in actual}))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        self.assertEqual([clean_paper(x) for x in self.native(f)], self.pipeline(f))


if __name__ == '__main__':
    unittest.main()
