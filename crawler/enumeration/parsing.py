"""Listing parsers: HTML selectors, JSON paths, or a reused crawler method.

Each parser turns one listing response into ``Page(items, total, fingerprint)``.
Identity (``Item.key``) defaults to the absolute detail URL without fragment;
``id_regex`` or a JSON key can replace it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from delivery.scripts.discovery_structure import date_value

from .urls import normalize_key_url


_MISSING = object()


@dataclass
class Item:
    key: str
    url: str
    date: str | None = None
    title: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class Page:
    items: list
    total: int | None = None
    status: int = 200
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self):
        if not self.items:
            return None
        return hashlib.sha256("\n".join(i.key for i in self.items).encode()).hexdigest()

    @property
    def keys(self):
        return [i.key for i in self.items]


def json_path(data, path, default=None):
    """Dotted path lookup; integer segments index lists. Empty path returns data."""
    if not path:
        return data
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[,\s.]", "", str(value))
    return int(digits) if digits.isdigit() else None


def extract_total(cfg, resp, text=None, data=None):
    """Source-reported total from header / JSON path / regex. None when absent."""
    if not cfg:
        return None
    kind = cfg.get("type")
    if kind == "header":
        return _to_int(resp.header(cfg["name"]))
    if kind == "json":
        if data is None:
            try:
                data = resp.json()
            except ValueError:
                return None
        return _to_int(json_path(data, cfg["path"]))
    if kind == "regex":
        m = re.search(cfg["pattern"], text if text is not None else resp.text(), re.I | re.S)
        return _to_int(m.group(1)) if m else None
    raise ValueError(f"unknown total type: {kind}")


def _json_listing(cfg):
    from .contracts import json_listing
    return json_listing(cfg)


class ListingParser:
    def __init__(self, cfg, *, total_cfg=None, adapter=None):
        self.cfg = cfg or {}
        self.kind = self.cfg.get("type", "html")
        self.total_cfg = total_cfg
        self.adapter = adapter
        self.id_regex = re.compile(self.cfg["id_regex"]) if self.cfg.get("id_regex") else None

    def _key(self, url, raw_id=None):
        if raw_id not in (None, ""):
            return str(raw_id)
        if self.id_regex:
            m = self.id_regex.search(url)
            if m:
                return m.group(1) if m.groups() else m.group(0)
        return normalize_key_url(url)

    def parse(self, resp):
        if self.kind == "html":
            page = self._html(resp)
        elif self.kind == "json":
            page = self._json(resp)
        elif self.kind == "adapter":
            page = self._adapter(resp)
        else:
            raise ValueError(f"unknown listing parser type: {self.kind}")
        if getattr(resp, "login_suspect", False):
            page.extra["login_suspect"] = True
        contract_json = (self.kind == "adapter" and self.adapter is not None
                         and getattr(self.adapter, "contract", None) is not None
                         and _json_listing(self.adapter.cfg))
        if not page.items and not page.extra.get("parse_error") and self.kind != "json" and not contract_json:
            # An empty HTML listing is an end candidate only when the listing's own
            # structure is present; an error/foreign page is a parse error.
            marker = self.listing_marker()
            if marker is None:
                page.extra["parse_error"] = "empty_without_listing_marker"
            else:
                from bs4 import BeautifulSoup
                if BeautifulSoup(resp.text(), "html.parser").select_one(marker) is None:
                    page.extra["parse_error"] = "listing_structure_missing"
        if not page.items and not page.extra.get("parse_error"):
            page.extra["explicit_empty"] = True
        return page

    def listing_marker(self):
        """CSS selector of the element that holds the list (configured, or the
        ancestor part of the item container selector, e.g. "tbody" for "tbody tr")."""
        if self.cfg.get("listing_marker"):
            return self.cfg["listing_marker"]
        container = self.cfg.get("item_container")
        if container is None and self.kind == "adapter" and self.adapter is not None:
            config = getattr(self.adapter.crawler, "_config", None) or {}
            container = ((config.get("list_page") or {}).get("selectors") or {}).get("item_container")
        if container is None and self.kind == "html":
            container = "tr"
        container = container or ""
        if "," in container:
            return None  # a selector group has no single ancestor part
        parts = container.split()[:-1]
        while parts and parts[-1] in (">", "+", "~"):
            parts.pop()  # "a > b" -> "a", never the dangling "a >"
        marker = " ".join(parts)
        if not marker:
            return None
        try:
            from bs4 import BeautifulSoup
            BeautifulSoup("", "html.parser").select_one(marker)
        except Exception:
            return None  # an unusable derived selector must not turn an empty page into an error
        return marker

    def _html(self, resp):
        from bs4 import BeautifulSoup
        text = resp.text()
        soup = BeautifulSoup(text, "html.parser")
        cfg = self.cfg
        if cfg.get("exclude"):
            for node in soup.select(cfg["exclude"]):
                node.decompose()
        items, seen = [], set()
        for row in soup.select(cfg.get("item_container", "tr")):
            link = row.select_one(cfg.get("item_link", "a[href]"))
            if link is None:
                continue
            href = link.get(cfg.get("item_link_attr", "href"), "")
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            url = urljoin(resp.url, href)
            raw_id = link.get(cfg["item_id_attr"]) if cfg.get("item_id_attr") else None
            key = self._key(url, raw_id)
            # Only exact repeats are dropped; the same key with another URL is a
            # conflict the runner must ledger, never silently discarded.
            if (key, url) in seen:
                continue
            seen.add((key, url))
            title = ""
            if cfg.get("title"):
                node = row.select_one(cfg["title"])
                title = node.get_text(" ", strip=True) if node else ""
            title = title or link.get_text(" ", strip=True)
            raw_date = ""
            if cfg.get("date"):
                node = row.select_one(cfg["date"])
                if node is not None:
                    raw_date = node.get("datetime") or node.get_text(" ", strip=True)
            payload = {"detail_url": url, "title": title, "published_date": raw_date}
            if cfg.get("category"):
                node = row.select_one(cfg["category"])
                payload["category"] = node.get_text(" ", strip=True) if node else ""
            items.append(Item(key=key, url=url, date=date_value(raw_date), title=title, payload=payload))
        total = extract_total(self.total_cfg, resp, text=text)
        return Page(items, total, resp.status)

    def _json(self, resp):
        cfg = self.cfg
        try:
            data = resp.json()
        except ValueError:
            data = None
        if data is None:
            return Page([], extract_total(self.total_cfg, resp), resp.status, {"parse_error": "invalid_json"})
        rows = json_path(data, cfg.get("items_path"), _MISSING)
        if rows is _MISSING or rows is None:
            return Page([], None, resp.status, {"parse_error": "items_path_missing"})
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(rows, list):
            return Page([], None, resp.status, {"parse_error": "items_not_a_list"})
        items, seen = [], set()
        for row in rows:
            record = json_path(row, cfg.get("item_path")) if cfg.get("item_path") else row
            if not isinstance(record, dict):
                continue
            if cfg.get("exclude_if_true") and json_path(record, cfg["exclude_if_true"]):
                continue  # pinned/sticky entries outside the source order
            raw_id = json_path(record, cfg.get("key")) if cfg.get("key") else None
            if cfg.get("url_template"):
                try:
                    url = cfg["url_template"].format(**{k: v for k, v in record.items() if isinstance(v, (str, int))})
                except (KeyError, IndexError):
                    continue
            else:
                url = json_path(record, cfg.get("url", "url")) or ""
            url = urljoin(resp.url, str(url)) if url else ""
            if not url and raw_id in (None, ""):
                continue
            key = self._key(url, raw_id)
            if (key, url) in seen:
                continue
            seen.add((key, url))
            title = json_path(record, cfg.get("title", "title"))
            if isinstance(title, dict):
                title = title.get("rendered", "")
            raw_date = json_path(record, cfg.get("date", "date"))
            # The raw source string goes to the save path, which normalizes it
            # (value/raw/status/precision); nothing is invented when absent.
            payload = {"record": record, "detail_url": url, "title": str(title or "")}
            if raw_date:
                payload["published_date"] = str(raw_date)
            items.append(Item(key=key, url=url, date=date_value(raw_date) if raw_date else None,
                              title=str(title or ""), payload=payload))
        return Page(items, extract_total(self.total_cfg, resp, data=data), resp.status)

    def _adapter(self, resp):
        if self.adapter is None:
            raise ValueError("listing parser type 'adapter' requires a crawler adapter")
        strict = getattr(self.adapter, "contract", None) is not None
        if strict and _json_listing(self.adapter.cfg):
            try:
                resp.json()
            except ValueError:
                # Same classification as the json parser: a listing error, never an empty page.
                return Page([], None, resp.status, {"parse_error": "invalid_json"})
        rows = self.adapter.parse_list(resp) or []
        cfg = self.cfg
        items, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if strict:
                # Only the declared field (default "url"); never another key as a fallback.
                url = row.get(cfg.get("url_field", "url")) or ""
            else:
                url = row.get(cfg.get("url_field", "detail_url")) or row.get("url") or ""
            if strict and not isinstance(url, str):
                from .contracts import fail
                fail("listing_url_type")
            url = urljoin(resp.url, str(url)) if url else ""
            # Registered config crawlers return their legacy external_id; use it.
            raw_id = row.get(cfg.get("key_field", "external_id"))
            if strict:
                from .contracts import fail
                if raw_id is not None and type(raw_id) not in (str, int):
                    fail("listing_key_type")
                if not url and raw_id in (None, ""):
                    fail("listing_identity_missing")
                if not url and self.adapter.cfg.get("detail_method"):
                    fail("listing_url_missing")  # the detail request needs the item's own URL
            if not url and raw_id in (None, ""):
                continue
            key = self._key(url, raw_id)
            if (key, url) in seen:
                continue
            seen.add((key, url))
            raw_date = row.get(cfg.get("date_field", "published_date")) or ""
            items.append(Item(key=key, url=url, date=date_value(raw_date) if raw_date else None,
                              title=str(row.get(cfg.get("title_field", "title")) or ""),
                              payload=json.loads(json.dumps(row, default=str))))
        return Page(items, extract_total(self.total_cfg, resp), resp.status)
