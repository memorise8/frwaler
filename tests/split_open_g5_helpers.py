"""S-open-g5 (10-25): shared checks for enumeration splits added next to an unchanged crawl().

Memory only: the original crawl() runs against a fake transport (patched curl helpers), the split
runs through the real Adapter/ListingParser with a fake fetcher. Any socket/process/DB use fails.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]


def example_plan(site):
    return json.loads((ROOT / 'docs/examples/collector-splits' / (site + '.json')).read_text())


class NoIO:
    """Mixin: deny network/process/database I/O for the whole test."""

    def setUp(self):
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('attempted I/O')
            raise AssertionError('fixture attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))


def native_papers(site, route, *, method_patches=(), module_patches=(), limit=10):
    """Run the original crawl() with ``route(url, *a, **k) -> body`` replacing the named
    crawler methods / module functions. Returns (saved papers, requested urls)."""
    c, module = load(site)  # object.__new__: __init__ (and any network in it) is not run
    captured, calls = [], []

    def transport(url, *args, **kwargs):
        calls.append(url)
        return route(url, *args, **kwargs)
    c._delay = 0
    c._detail_delay = 0
    c._last_save_created = True
    c._source_failure = None
    c.delivery_cursor = None
    c.delivery_mode = 'backfill'
    c.DELIVERY_STOP_ON_KNOWN = False
    c._check_cancelled = lambda: None
    c._report_source_failure = lambda *a, **k: None
    c._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    for name in method_patches:
        setattr(c, name, transport)
    with contextlib.ExitStack() as stack:
        for name in module_patches:
            stack.enter_context(patch.object(module, name, transport))
        stack.enter_context(patch('time.sleep', lambda *a: None))
        with contextlib.redirect_stdout(io.StringIO()):
            c.crawl(limit=limit)
    return captured, calls


def split_run(site, list_body, details, *, content_type='text/html; charset=utf-8', page=None,
              detail_type='text/html; charset=utf-8'):
    """Parse ``list_body`` as the plan's listing page and materialize every item through the
    Adapter. ``details``: url -> body (bytes or str). Returns (items, papers, detail urls)."""
    plan = site_plan(example_plan(site), site, registered=[site])
    crawler, _ = load(site)
    adapter = Adapter(site, plan.section('adapter'), None, crawler=crawler)
    lst = plan.listing
    params = dict(lst.get('params') or {})
    if page is not None:
        params[lst.get('page_param', 'page')] = page
    url = build_url(lst['url'], params)
    resp = Response(200, {'Content-Type': content_type}, list_body, url)
    parsed = ListingParser(lst.get('parser'), adapter=adapter).parse(resp)
    calls = []

    def get(u, *a, **k):
        calls.append(u)
        if u not in details:
            raise AssertionError('undeclared detail request: ' + u)
        return Response(200, {'Content-Type': detail_type}, details[u], u)
    papers = []
    for item in parsed.items:
        paper = adapter.materialize(item, SimpleNamespace(get=get))
        if paper is not None:
            papers.append(paper)
    return parsed, papers, calls


def parse_page(site, body, *, content_type='text/html; charset=utf-8', page=None):
    plan = site_plan(example_plan(site), site, registered=[site])
    crawler, _ = load(site)
    adapter = Adapter(site, plan.section('adapter'), None, crawler=crawler)
    lst = plan.listing
    params = dict(lst.get('params') or {})
    if page is not None:
        params[lst.get('page_param', 'page')] = page
    return ListingParser(lst.get('parser'), adapter=adapter).parse(
        Response(200, {'Content-Type': content_type}, body, build_url(lst['url'], params)))


def comparable(paper, drop=('id',)):
    p = clean_paper(dict(paper))
    for k in drop:
        p.pop(k, None)
    p.pop('site_id', None)
    return p
