"""Reuse existing crawlers' parsing and save path without their fetch loops.

The framework fetches every page itself (through the gate) and hands the
content to the crawler's parsing method; saving always goes through the
crawler instance's ``_save_paper`` so dedup (site_id, post_number, meta_url),
date normalization and the sqlite/Postgres routing stay exactly as they are.

Reused crawler methods run inside ``guard.guarded()``: any socket connect or
process spawn they attempt becomes an explicit ``AdapterUngatedNetwork`` stop
instead of silently bypassing the request gate.
"""
from __future__ import annotations

import json
import sqlite3

from .guard import AdapterUngatedNetwork, guarded
from .http import EnumerationStop, PermanentFailure

__all__ = ["Adapter", "AdapterConfigError", "AdapterUngatedNetwork", "IdentityCollision", "IdentityReview",
           "IdentityRuleConfig", "review_rule",
           "clean_paper", "paper_identity"]


class AdapterConfigError(ValueError):
    pass


class IdentityReview(Exception):
    """The backend's identity rule refuses this item until a person reviews it."""


class IdentityRuleConfig(EnumerationStop):
    """The backend identity rule rejects how this site computes post_number/meta_url
    (e.g. krivet_post_number_must_match_pstNo): every item would fail, stop the site."""


def review_rule(message):
    """True for item-level identity problems a person must review (stored-row
    collisions/repairs); False for rule/configuration errors."""
    return "requires_review" in message or "repair_required" in message or message.startswith("ambiguous_")


class IdentityCollision(Exception):
    """Same (site_id, post_number) as a stored row, but a different document."""


class _Blocked:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AdapterUngatedNetwork(f"adapter_ungated_network:{self._name}.{attr}")

    def __call__(self, *args, **kwargs):
        raise AdapterUngatedNetwork(f"adapter_ungated_network:{self._name}")


def _strip_nul(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    return value


def clean_paper(paper):
    """NUL characters are rejected by PostgreSQL text; post_number is always text."""
    out = {k: _strip_nul(v) for k, v in paper.items()}
    if out.get("external_id") is not None and not isinstance(out["external_id"], str):
        out["external_id"] = str(out["external_id"])
    return out


def paper_identity(paper):
    """(post_number, meta_url) exactly as BaseCrawler._save_paper will store them."""
    from crawler import libertree_adapter
    doc = libertree_adapter.paper_to_document(dict(paper))
    post_number = doc.get("external_id")
    meta_url = doc.get("meta_url") or doc.get("pdf_url") or ""
    return (None if post_number is None else str(post_number)), meta_url


def _query(conn, sql, args):
    if not isinstance(conn, sqlite3.Connection):
        sql = sql.replace("?", "%s")
    row = conn.execute(sql, args).fetchone()
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row[list(row.keys())[0]]


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
        "published_date": payload.get("published_date") or "",
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
        self.conn = conn
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
        elif self.mode == "crawler" and not (self.cfg.get("detail_method") or self.cfg.get("paper_method")
                                             or self.cfg.get("list_returns_paper")):
            # The paper (and so post_number/meta_url) must come from the crawler's own code.
            raise AdapterConfigError("custom crawler adapter needs detail_method, paper_method or list_returns_paper")

    def close(self):
        """Nothing process-wide is left behind (the guard is thread-local and inert)."""

    # -- listing -------------------------------------------------------------
    def parse_list(self, resp):
        c = self.crawler
        if self.is_generic and not self.cfg.get("list_method"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text(), "html.parser")
            selectors = (c._config.get("list_page") or {}).get("selectors", {})
            with guarded():
                return c._extract_list_items(soup, selectors)
        name = self.cfg.get("list_method")
        if not name:
            raise AdapterConfigError("listing parser 'adapter' requires adapter.list_method")
        args = self._args(self.cfg.get("list_args", ["html"]), resp, None)
        with guarded():
            return getattr(c, name)(*args)

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
            with guarded():
                if self.mode == "crawler" and not self.is_generic and self.cfg.get("paper_method"):
                    paper = getattr(c, self.cfg["paper_method"])(dict(item.payload))
                elif self.mode == "crawler" and not self.is_generic and self.cfg.get("list_returns_paper"):
                    paper = dict(item.payload)
                else:
                    paper = paper_from_item(self.site_id, item, self._legacy_id())
            return self._finish(paper, item)
        resp = fetcher.get(item.url)
        if getattr(resp, "login_suspect", False):
            raise PermanentFailure("login_required_suspect", item.url, resp.status)
        if self.is_generic and not self.cfg.get("detail_method"):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text(), "html.parser")
            c._fetch_page = lambda url, params=None, _soup=soup: _soup
            try:
                basic = dict(item.payload)
                extract = self._legacy_id()
                with guarded():
                    basic.setdefault("external_id", extract(item.url) if extract and item.url else item.key)
                    basic.setdefault("title", item.title)
                    paper = c._fetch_detail(item.url, basic, c._config.get("detail_page") or {})
            finally:
                del c._fetch_page
            return self._finish(paper, item)
        args = self._args(self.cfg.get("detail_args", ["html", "item"]), resp, item)
        with guarded():
            result = getattr(c, self.cfg["detail_method"])(*args)
        return self._finish(result, item)

    def identity_uses_list_fields(self):
        """True when post_number/meta_url come from crawler code fed with listing
        fields, which a URL-only sample cannot reproduce exactly."""
        if self.mode != "crawler" or self.is_generic:
            return False
        if self.cfg.get("paper_method") or self.cfg.get("list_returns_paper"):
            return True
        return "item" in self.cfg.get("detail_args", ["html", "item"])

    def identity(self, item, fetcher):
        """(post_number, meta_url) this framework would store for ``item`` (no save)."""
        paper = self.materialize(item, fetcher)
        return None if paper is None else paper_identity(clean_paper(paper))

    def _legacy_id(self):
        """post_number rule: a registered config crawler (or an explicit id_regex) keeps the
        legacy ``_extract_id`` so dedup matches rows it saved before; otherwise the listing key."""
        if self.is_generic and (self.mode == "crawler" or self.cfg.get("id_regex")):
            return self.crawler._extract_id
        return None

    def _finish(self, paper, item):
        if not isinstance(paper, dict):
            return None
        paper = clean_paper(paper)
        paper.setdefault("site_id", self.site_id)
        if not paper.get("url") and not paper.get("meta_url"):
            paper["url"] = item.url
        return paper

    # -- save ----------------------------------------------------------------
    def _backend(self):
        from crawler.db_backend import get_backend
        return get_backend("sqlite" if isinstance(self.conn, sqlite3.Connection) else "postgres")

    def existing_post_number(self, post_number):
        if post_number is None:
            return None
        return _query(self.conn, "SELECT seq_id FROM documents WHERE site_id=? AND post_number=? ORDER BY seq_id LIMIT 1",
                      (self.site_id, post_number))

    def save(self, paper):
        """Returns (seq_id, created, note); note is None or "existing_document".

        Order: (1) the backend's dedup rule, including the KRIVET/KEI/CSO
        review exceptions, which raise ``IdentityReview`` loudly; a match is the
        same document and takes the normal path (so its files are queued again
        after a crash). (2) A row with the same (site_id, post_number) but no
        dedup match is a different document: ``IdentityCollision``, nothing is
        written and the stored row is left untouched.
        """
        paper = clean_paper(paper)
        post_number, meta_url = paper_identity(paper)
        try:
            try:
                same = self._backend().find_by_dedup_key(self.conn, self.site_id, post_number, meta_url)
            except ValueError as exc:
                if review_rule(str(exc)):
                    raise IdentityReview(str(exc)) from None
                raise IdentityRuleConfig(f"identity_rule_config:{exc}") from None
            if same is None and self.existing_post_number(post_number) is not None:
                raise IdentityCollision(post_number)
            seq_id = self.crawler._save_paper(paper)
        except Exception:
            # psycopg leaves the transaction aborted after any SQL error; without a
            # rollback every following item would fail too.
            rollback = getattr(self.conn, "rollback", None)
            if rollback:
                rollback()
            raise
        created = bool(getattr(self.crawler, "_last_save_created", False))
        return seq_id, created, (None if created or same is None else "existing_document")

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
