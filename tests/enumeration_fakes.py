"""Fake HTTP sources for crawler.enumeration tests. No real network."""
from __future__ import annotations

import gzip
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Fetcher, Response, TransportError
from crawler.enumeration.plan import site_plan
from crawler.enumeration.runner import SiteRunner
from crawler.enumeration.store import Store

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n2 0 obj << /Type /Page >> endobj\n3 0 obj << /Type /Page >> endobj\n%%EOF"


def html(body, title="list"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


class FakeTransport:
    """Routes requests to a handler(url) -> Response. Records every URL."""

    def __init__(self, handler):
        self.handler = handler
        self.urls = []

    def request(self, method, url, *, headers=None):
        self.urls.append(url)
        result = self.handler(url)
        if isinstance(result, Exception):
            raise result
        return result


class FakeBoard:
    """Newest-first paginated board. id 1 is the oldest item."""

    def __init__(self, n_items, *, page_size=10, start_page=1, end_mode="empty", host="https://board.test",
                 page_param="page", show_total=True, robots=None, bare_first_page=True):
        self.ids = list(range(n_items, 0, -1))
        self.page_size = page_size
        self.start_page = start_page
        self.end_mode = end_mode
        self.host = host
        self.page_param = page_param
        self.show_total = show_total
        self.robots = robots
        self.bare_first_page = bare_first_page
        self.detail_status = {}      # id -> status code (permanent) or list of codes (consumed)
        self.detail_override = {}    # id -> raw html
        self.list_hooks = []         # callables(page) run before serving a list page
        self.list_requests = 0
        self.next_id = n_items + 1
        self.total_override = None

    def item_date(self, i):
        return (date(2000, 1, 1) + timedelta(days=i)).isoformat()

    def insert(self, count):
        new = list(range(self.next_id + count - 1, self.next_id - 1, -1))
        self.next_id += count
        self.ids = new + self.ids

    def pages(self):
        return max(1, -(-len(self.ids) // self.page_size))

    def rows(self, index):
        chunk = self.ids[index * self.page_size:(index + 1) * self.page_size]
        return "".join(f'<tr><td class="date">{self.item_date(i)}</td><td><a href="/view?id={i}">Title {i}</a></td></tr>'
                       for i in chunk)

    def list_page(self, page):
        self.list_requests += 1
        for hook in list(self.list_hooks):
            hook(self, page)
        index = page - self.start_page
        last = self.pages() - 1
        if index < 0:
            return Response(200, {"Content-Type": "text/html"}, html("<table><tbody></tbody></table>"))
        if index > last:
            if self.end_mode == "empty":
                body = "<table><tbody></tbody></table>"
            elif self.end_mode == "clamp":
                body = f"<table><tbody>{self.rows(last)}</tbody></table>"
            elif self.end_mode == "wrap":
                body = f"<table><tbody>{self.rows(0)}</tbody></table>"
            elif self.end_mode == "404":
                return Response(404, {"Content-Type": "text/html"}, html("not found"))
            else:
                raise AssertionError(self.end_mode)
        else:
            body = f"<table><tbody>{self.rows(index)}</tbody></table>"
        if self.show_total:
            total = len(self.ids) if self.total_override is None else self.total_override
            body += f"<p>총 {total} 건</p>"
        return Response(200, {"Content-Type": "text/html; charset=utf-8"}, html(body))

    def detail(self, i):
        status = self.detail_status.get(i)
        if isinstance(status, list):
            code = status.pop(0) if status else 200
        else:
            code = status or 200
        if isinstance(code, Exception):
            return code
        if code != 200:
            return Response(code, {"Content-Type": "text/html"}, html("error"))
        if i in self.detail_override:
            return Response(200, {"Content-Type": "text/html"}, self.detail_override[i])
        body = (f'<h1 class="title">Title {i}</h1><div class="abstract">Abstract of {i}</div>'
                f'<span class="date">{self.item_date(i)}</span><a class="pdf" href="/files/{i}.pdf">PDF</a>')
        return Response(200, {"Content-Type": "text/html"}, html(body, f"Title {i}"))

    def __call__(self, url):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.path == "/robots.txt":
            if self.robots is None:
                return Response(404, {}, b"")
            return Response(200, {"Content-Type": "text/plain"}, self.robots)
        if parts.path == "/list":
            if self.page_param in q:
                return self.list_page(int(q[self.page_param]))
            if self.bare_first_page:
                return self.list_page(self.start_page)
            return self.list_page(self.start_page + 1)
        if parts.path == "/view":
            return self.detail(int(q["id"]))
        if parts.path.startswith("/files/"):
            return Response(200, {"Content-Type": "application/pdf"}, PDF)
        return Response(404, {}, b"")


def board_plan(**over):
    site = {
        "strategy": "last_page_backward",
        "request_interval_s": 0,
        "base_url": "https://board.test",
        "listing": {"url": "https://board.test/list", "page_param": "page", "start_page": 1,
                    "parser": {"type": "html", "item_container": "tbody tr", "item_link": "a[href]",
                               "date": "td.date", "id_regex": r"id=(\d+)"},
                    "total": {"type": "regex", "pattern": r"총\s*([\d,]+)\s*건"}},
        "adapter": {"type": "selectors", "id_regex": r"id=(\d+)",
                    "selectors": {"title": "h1.title", "abstract": "div.abstract", "date": "span.date",
                                  "pdf_link": "a.pdf"}},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(site.get(key), dict):
            site[key] = {**site[key], **value}
        else:
            site[key] = value
    return site


class Env:
    """Temporary state store + libertree sqlite DB for one site."""

    def __init__(self, site_id="fake-site"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.site_id = site_id
        self.store = Store(self.root / "enumeration.sqlite")
        self.conn = db_libertree.open_db(self.root / "libertree.db")
        db_libertree.init_db(self.conn)
        db_libertree.upsert_site(self.conn, site_id, site_id, "https://board.test")

    def close(self):
        self.store.close()
        self.conn.close()
        self.tmp.cleanup()

    def plan(self, site):
        return site_plan({"sites": {self.site_id: site}}, self.site_id, min_interval=0)

    def fetcher(self, plan, transport, **kw):
        kw.setdefault("sleep", lambda s: None)
        return Fetcher(self.site_id, transport, self.root / "gates", interval=plan.get("request_interval_s"),
                       max_requests=kw.pop("max_requests", plan.get("max_requests")),
                       max_runtime_s=plan.get("max_runtime_s"), retries=plan.get("retries"),
                       backoff_s=0, respect_robots=plan.get("respect_robots"), **kw)

    def run(self, site, handler, *, crawler=None, today="2026-10-12", transport=None, refresh=False,
            retry_only=False, **fetch_kw):
        plan = self.plan(site)
        transport = transport or FakeTransport(handler)
        adapter = Adapter(self.site_id, plan.raw.get("adapter"), self.conn, crawler=crawler, base_url=plan.base_url)
        try:
            runner = SiteRunner(plan, self.store, self.fetcher(plan, transport, **fetch_kw), adapter, today=today)
            result = runner.run(refresh=refresh, retry_only=retry_only)
        finally:
            adapter.close()
        result["transport"] = transport
        return result

    def docs(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT seq_id, post_number, meta_url, title, published_date, pdf_url FROM documents ORDER BY seq_id")]


def wp_handler(n, per_page_max=100, base="https://wp.test"):
    posts = [{"id": i, "link": f"{base}/?p={i}", "date": f"2010-01-{(i % 28) + 1:02d}T00:00:00",
              "title": {"rendered": f"Post {i}"}} for i in range(1, n + 1)]

    def handler(url):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        if parts.path == "/wp-json/wp/v2/posts":
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            assert q.get("order") == "asc" and q.get("orderby") == "date", q
            per = min(int(q.get("per_page", 10)), per_page_max)
            page = int(q.get("page", 1))
            pages = max(1, -(-n // per))
            if page > pages:
                return Response(400, {"Content-Type": "application/json"},
                                json.dumps({"code": "rest_post_invalid_page_number"}))
            chunk = posts[(page - 1) * per: page * per]
            return Response(200, {"Content-Type": "application/json", "X-WP-Total": str(handler.total),
                                  "X-WP-TotalPages": str(pages)}, json.dumps(chunk))
        if parts.path == "/":
            return Response(200, {"Content-Type": "text/html"}, html("<h1>x</h1>"))
        return Response(404, {}, b"")

    handler.total = n
    return handler


def gz(data):
    return gzip.compress(data)
