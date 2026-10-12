"""Reuse existing crawlers' detail parsing and save path without their fetch loops.

The framework fetches every page itself (through the gate) and hands the
content to the crawler's parsing method; saving always goes through the
crawler instance's ``_save_paper`` so dedup (site_id, post_number, meta_url)
and the sqlite/Postgres routing stay exactly as they are.

Any network access attempted from inside a reused crawler method (its own
curl/requests calls) is turned into an explicit ``AdapterUngatedNetwork``
stop instead of silently bypassing the request gate.
"""
from __future__ import annotations

import json
import sys
import types

from .http import EnumerationStop

_NETWORK_MODULES = ("subprocess", "requests", "httpx", "urllib3", "cloudscraper", "curl_cffi", "pycurl")


class AdapterUngatedNetwork(EnumerationStop):
    pass


class AdapterConfigError(ValueError):
    pass


class _Blocked:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AdapterUngatedNetwork(f"adapter_ungated_network:{self._name}.{attr}")

    def __call__(self, *args, **kwargs):
        raise AdapterUngatedNetwork(f"adapter_ungated_network:{self._name}")


def _make_plan_generic(site_id, base_url, config, conn):
    """A GenericCrawler built from an in-memory config (no file), for reuse of its parsers/saver."""
    from crawler.base_crawler import BaseCrawler
    from crawler.generic_crawler import GenericCrawler

    class PlanGenericCrawler(GenericCrawler):
        def __init__(self):
            self._config = dict(config, site_id=site_id, site_name=config.get("site_name") or site_id,
                                base_url=base_url)
            BaseCrawler.__init__(self, conn, 0)
            self._config_path = None
            self._fetch_method = None
            self._use_browser = False
            self._browser = None
            self._playwright = None
            self._cloudscraper = None
            self.delivery_listing_stop = None

    return PlanGenericCrawler()


def paper_from_item(site_id, item, extract_id=None):
    payload = item.payload or {}
    external_id = payload.get("external_id")
    if not external_id:
        external_id = extract_id(item.url) if (extract_id and item.url) else item.key
    return {
        "id": None,
        "site_id": site_id,
        "external_id": external_id,
        "title": payload.get("title") or item.title,
        "authors": json.dumps(payload.get("authors") or [], ensure_ascii=False),
        "abstract": payload.get("abstract") or "",
        "category": payload.get("category", ""),
        "keywords": json.dumps(payload.get("keywords") or [], ensure_ascii=False),
        "published_date": payload.get("published_date") or item.date or "",
        "url": item.url,
        "pdf_url": payload.get("pdf_url", ""),
        "doi": payload.get("doi", ""),
        "department": payload.get("department", ""),
        "metadata": json.dumps({}, ensure_ascii=False),
    }


class Adapter:
    """mode: list_only | selectors | crawler."""

    def __init__(self, site_id, cfg, conn, *, crawler=None, base_url=""):
        self.site_id = site_id
        self.cfg = cfg or {"type": "list_only"}
        self.mode = self.cfg.get("type", "list_only")
        self._restore = []
        if self.mode == "crawler":
            if crawler is None:
                raise AdapterConfigError("adapter type 'crawler' requires a registered crawler")
            self.crawler = crawler
        elif self.mode in ("list_only", "selectors"):
            options = {"id_regex": self.cfg["id_regex"]} if self.cfg.get("id_regex") else {}
            detail = {"selectors": self.cfg.get("selectors", {})} if self.mode == "selectors" else {}
            self.crawler = _make_plan_generic(site_id, base_url, {"options": options, "detail_page": detail}, conn)
            if self.mode == "selectors" and not detail["selectors"]:
                raise AdapterConfigError("adapter type 'selectors' requires selectors")
        else:
            raise AdapterConfigError(f"unknown adapter type: {self.mode}")
        self._prepare_crawler()

    @property
    def is_generic(self):
        from crawler.generic_crawler import GenericCrawler
        return isinstance(self.crawler, GenericCrawler)

    def _prepare_crawler(self):
        c = self.crawler
        # Full enumeration: known documents must neither stop the run nor be
        # treated as "up to date"; the item ledger owns skip decisions.
        c.delivery_mode = "backfill"
        c.DELIVERY_STOP_ON_KNOWN = False
        c._delay = 0
        c._session = _Blocked("crawler._session")
        c._request = _Blocked("crawler._request")
        if self.is_generic:
            method = getattr(c, "_fetch_method", None)
            if method in ("browser", "cloudscraper"):
                # cloudscraper is a WAF bypass; browser fetching is not governed here.
                raise AdapterConfigError(f"fetch_method_{method}_unsupported")
            return
        module = sys.modules.get(type(c).__module__)
        if module is not None:
            for name in _NETWORK_MODULES:
                value = module.__dict__.get(name)
                if isinstance(value, types.ModuleType):
                    self._restore.append((module, name, value))
                    setattr(module, name, _Blocked(name))

    def close(self):
        for module, name, value in self._restore:
            setattr(module, name, value)
        self._restore = []

    # -- listing -------------------------------------------------------------
    def parse_list(self, resp):
        c = self.crawler
        if self.is_generic and not self.cfg.get("list_method"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text(), "html.parser")
            selectors = (c._config.get("list_page") or {}).get("selectors", {})
            return c._extract_list_items(soup, selectors)
        name = self.cfg.get("list_method")
        if not name:
            raise AdapterConfigError("listing parser 'adapter' requires adapter.list_method")
        return getattr(c, name)(*self._args(self.cfg.get("list_args", ["html"]), resp, None))

    def _args(self, spec, resp, item):
        out = []
        for token in spec:
            if token == "html":
                out.append(resp.text())
            elif token == "raw":
                out.append(resp.body)
            elif token == "soup":
                from bs4 import BeautifulSoup
                out.append(BeautifulSoup(resp.text(), "html.parser"))
            elif token == "json":
                out.append(resp.json())
            elif token == "url":
                out.append(resp.url)
            elif token == "item":
                out.append(dict(item.payload) if item else {})
            elif token == "resp":
                out.append(resp)
            else:
                raise AdapterConfigError(f"unknown adapter argument token: {token}")
        return out

    # -- detail ------------------------------------------------------------
    def needs_detail(self):
        if self.mode == "list_only":
            return False
        if self.mode == "selectors":
            return True
        if self.is_generic and not self.cfg.get("detail_method"):
            return bool((self.crawler._config.get("detail_page") or {}).get("selectors"))
        return bool(self.cfg.get("detail_method"))

    def materialize(self, item, fetcher):
        """Return a paper dict (or None). Detail requests go through ``fetcher``."""
        c = self.crawler
        if not self.needs_detail():
            if self.mode == "crawler" and not self.is_generic and self.cfg.get("list_returns_paper"):
                paper = dict(item.payload)
            else:
                extract = c._extract_id if self.is_generic else None
                paper = paper_from_item(self.site_id, item, extract)
            return self._finish(paper, item)
        resp = fetcher.get(item.url)
        if self.is_generic and not self.cfg.get("detail_method"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text(), "html.parser")
            c._fetch_page = lambda url, params=None, _soup=soup: _soup
            try:
                basic = dict(item.payload)
                basic.setdefault("external_id", c._extract_id(item.url))
                basic.setdefault("title", item.title)
                paper = c._fetch_detail(item.url, basic, c._config.get("detail_page") or {})
            finally:
                del c._fetch_page
            return self._finish(paper, item)
        result = getattr(c, self.cfg["detail_method"])(*self._args(self.cfg.get("detail_args", ["html", "item"]), resp, item))
        return self._finish(result, item)

    def _finish(self, paper, item):
        if not isinstance(paper, dict):
            return None
        paper = dict(paper)
        paper.setdefault("site_id", self.site_id)
        if not paper.get("url") and not paper.get("meta_url"):
            paper["url"] = item.url
        return paper

    # -- save ----------------------------------------------------------------
    def save(self, paper):
        seq_id = self.crawler._save_paper(paper)
        return seq_id, bool(getattr(self.crawler, "_last_save_created", False))

    @staticmethod
    def file_urls(paper):
        out = []
        if paper.get("pdf_url"):
            out.append((paper["pdf_url"], "pdf"))
        for entry in paper.get("attachments") or []:
            url = entry.get("url") if isinstance(entry, dict) else entry
            if url and url != paper.get("pdf_url"):
                out.append((url, "attachment"))
        return out
