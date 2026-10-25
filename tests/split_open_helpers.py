"""S-open (10-25) enumeration split checks: memory only, no network/process/DB.

Each test gives a small hand-written listing/detail fixture, runs
  (a) the site's original crawl() with its fetch helper replaced by a dict lookup, and
  (b) the plan adapter (list_method -> detail_method -> paper_method) on the same bodies,
and compares the papers, so external_id/post_number/url/skip rules stay identical.
"""
import contextlib
import copy
import io
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from tests.collector_split_helpers import load


def adapter_for(site, adapter_cfg):
    crawler, module = load(site)
    crawler._delay = 0
    return Adapter(site, adapter_cfg, None, crawler=crawler), crawler, module


def listing_items(adapter, parser_cfg, body, url, content_type="text/html"):
    parser = ListingParser(parser_cfg, adapter=adapter)
    return parser.parse(Response(200, {"Content-Type": content_type}, body, url))


class DictFetcher:
    """Fetcher stand-in: detail URL -> body (str)."""

    def __init__(self, bodies, content_type="text/html"):
        self.bodies = bodies
        self.content_type = content_type
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(200, {"Content-Type": self.content_type}, self.bodies[url], url)


def pipeline(adapter, items, bodies, content_type="text/html"):
    fetcher = DictFetcher(bodies, content_type)
    out = []
    for it in items:
        paper = adapter.materialize(it, fetcher)
        if paper is not None:
            out.append(clean_paper(paper))
    return out


def native(site, transport, fetch_attr="_curl_get", limit=10, extra=None):
    """Run the original crawl() with ``fetch_attr`` replaced by ``transport``; return saved papers."""
    crawler, module = load(site)
    captured = []
    crawler._delay = 0
    crawler._last_save_created = True
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    setattr(crawler, fetch_attr, transport)
    for k, v in (extra or {}).items():
        setattr(crawler, k, v)
    with patch("time.sleep", lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
        crawler.crawl(limit=limit)
    return captured


def identities(papers):
    return [paper_identity(clean_paper(p)) for p in papers]
