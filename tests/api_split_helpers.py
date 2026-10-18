"""Helpers for public-bibliographic-API site splits (session API, 2026-10-18).

The original crawl() runs against an in-memory transport (no network, no process);
the enumeration path runs the shipped plan's adapter on the same responses. Papers
must be identical field by field (after the framework's clean_paper)."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]


def api_plan(site):
    return json.loads((ROOT / "docs/examples/api-sites" / (site + ".json")).read_text())


def crawl_sha(site):
    """sha256 of the crawl() method source text (ast segment) in the crawler module."""
    import ast
    src = (ROOT / "crawler/sites/custom" / (site + ".py")).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "crawl":
            return hashlib.sha256(ast.get_source_segment(src, node).encode()).hexdigest()
    raise AssertionError("no crawl()")


def native(site, body_for, *, method_patches=(), module_patches=(), init=None, limit=None):
    """Run the original crawl(); ``body_for(url, **kw)`` answers every fetch.
    Returns the papers handed to _save_paper."""
    crawler, module = load(site)
    captured = []
    crawler._delay = 0
    crawler._last_save_created = True
    crawler._check_cancelled = lambda: None
    crawler._report_source_failure = lambda *a, **k: None
    try:
        crawler.delivery_source_failure = None
    except AttributeError:
        pass  # a read-only property on BaseCrawler
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    if init:
        init(crawler)
    for name in method_patches:
        setattr(crawler, name, body_for)
    with contextlib.ExitStack() as stack:
        for name in module_patches:
            stack.enter_context(patch.object(module, name, body_for))
        stack.enter_context(patch("time.sleep", lambda *a: None))
        with contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=limit)
    return captured


class FakeFetcher:
    def __init__(self, handler):
        self.handler = handler
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.handler(url)


def pipeline(site, listing_urls, handler):
    """Parse the given listing URLs with the shipped plan and materialize every item."""
    plan = site_plan(api_plan(site), site, registered=[site])
    crawler, _ = load(site)
    adapter = Adapter(site, plan.section("adapter"), None, crawler=crawler)
    parser = ListingParser(plan.listing["parser"], total_cfg=plan.listing.get("total"), adapter=adapter)
    fetcher = FakeFetcher(handler)
    papers, items = [], []
    for url in listing_urls:
        page = parser.parse(fetcher.get(url))
        items.extend(page.items)
    for item in items:
        paper = adapter.materialize(item, fetcher)
        if paper is not None:
            papers.append(paper)
    return papers, items, fetcher


def json_response(url, value, status=200):
    body = value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=False)
    return Response(status, {"Content-Type": "application/json"}, body, url)


def html_response(url, body, status=200):
    return Response(status, {"Content-Type": "text/html; charset=utf-8"}, body, url)


def run_e2e(site, handler, crawler_init=None, plan_override=None):
    """The shipped plan through the real SiteRunner with a fake transport."""
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(api_plan(site)["sites"][site])
    plan.update(plan_override or {})
    plan["request_interval_s"] = 0
    env = Env(site)
    try:
        _, module = load(site)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == site)
        try:
            crawler = cls(env.conn, 0)
        except TypeError:
            crawler = cls(env.conn)
        if crawler_init:
            crawler_init(crawler)

        def robots_first(url):
            if urlsplit(url).path == "/robots.txt":
                return Response(404, {}, b"", url)
            return handler(url)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, robots_first, crawler=crawler)
        return result, env.docs()
    finally:
        env.close()


def identities(papers):
    return [paper_identity(clean_paper(p)) for p in papers]


def query(url, name, default=None):
    return (parse_qs(urlsplit(url).query).get(name) or [default])[0]
