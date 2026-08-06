# -*- coding: utf-8 -*-
"""gob.mx "Archivo de Prensa" crawler (Mexico federal ministries).

Every ministry publishes press releases under
``https://www.gob.mx/<agency>/archivo/prensa``. The whole ``www.gob.mx`` host
sits behind **Akamai Bot Manager** — the first request returns a
``Challenge Validation`` proof-of-work page, not the content. curl / requests
cannot pass it.

Strategy (verified 2026-08-05):
  1. Solve the Akamai challenge **once** with a headless Chromium (Playwright).
     Chromium runs the proof-of-work JS; after ~30 s the page reloads to the
     real content and Akamai sets ``sec_cpt`` + ``akaas_production`` cookies.
  2. Copy those cookies into a **curl_cffi** session (TLS-fingerprint spoof)
     and page through ``?order=DESC&page=N`` rapidly — no browser per page.
  3. If a later page trips the challenge again (cookie expiry, ~2 h TTL),
     transparently re-solve and continue.

Listing pages hold 9 article links ``/<agency>/prensa/<slug>``; we walk until a
page yields no (new) links. Each article's detail page gives title + body.

Caps: ``LIBERTREE_MAX_PAGES`` (default 200) and ``LIBERTREE_MAX_WALL_S``
(default 25 min) bound a normal run; the capacity harness neutralises them.
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class GobMxPrensaCrawler(BaseCrawler):
    """Base crawler; per-agency subclasses only set ``site_id`` + ``AGENCY``."""

    # Empty site_id keeps this abstract base out of the crawler registry
    # (the loader only registers classes with a non-empty string site_id).
    # Concrete per-agency subclasses below set a real site_id.
    site_id = ""
    site_name = "gob.mx Archivo de Prensa"
    base_url = "https://www.gob.mx"

    AGENCY = ""  # e.g. "agricultura"
    PAGE_SIZE = 9
    PAGE_SAFETY_CAP = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    CHALLENGE_MARKER = "Challenge Validation"
    SOLVE_TIMEOUT_SEC = 90
    MIN_TITLE_CHARS = 8

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay)
        self._cffi = None  # lazily created curl_cffi session

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    def _archive_url(self, page):
        return f"{self.base_url}/{self.AGENCY}/archivo/prensa?order=DESC&page={page}"

    def _detail_url(self, path):
        sep = "&" if "?" in path else "?"
        return f"{urljoin(self.base_url, path)}{sep}idiom=es"

    # ------------------------------------------------------------------
    # Akamai challenge: solve once with Playwright, reuse cookies via curl_cffi
    # ------------------------------------------------------------------

    def _solve_challenge(self):
        """Return a cookie header string after passing the Akamai challenge."""
        from playwright.sync_api import sync_playwright

        url = self._archive_url(1)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                ctx = browser.new_context(user_agent=_UA, locale="es-MX")
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                deadline = time.monotonic() + self.SOLVE_TIMEOUT_SEC
                while time.monotonic() < deadline:
                    time.sleep(5)
                    try:
                        html = page.content()
                    except Exception:
                        continue
                    if self.CHALLENGE_MARKER not in html and len(html) > 5000:
                        break
                cookies = ctx.cookies()
            finally:
                browser.close()
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def _ensure_session(self):
        if self._cffi is not None:
            return self._cffi
        from curl_cffi import requests as creq

        jar = self._solve_challenge()
        self._cffi = creq.Session(
            impersonate="chrome124",
            headers={
                "User-Agent": _UA,
                "Cookie": jar,
                "Accept-Language": "es-MX,es;q=0.9,en;q=0.7",
            },
            timeout=40,
        )
        print(f"[{self.site_id}] Akamai challenge solved; cookie session ready")
        return self._cffi

    def _get(self, url, context="request", allow_resolve=True):
        """GET via curl_cffi; re-solve the challenge once if it reappears."""
        session = self._ensure_session()
        for attempt in range(3):
            try:
                resp = session.get(url)
                body = resp.text or ""
                if resp.status_code == 200 and body:
                    if self.CHALLENGE_MARKER in body and allow_resolve:
                        print(f"[{self.site_id}] challenge reappeared; re-solving")
                        self._cffi = None
                        session = self._ensure_session()
                        allow_resolve = False
                        continue
                    return body
                last = f"status {resp.status_code}, len {len(body)}"
            except Exception as exc:  # noqa: BLE001 — network best-effort
                last = str(exc)
            print(f"[{self.site_id}] {context} failed (attempt {attempt + 1}/3): {last}")
            time.sleep((attempt + 1) * 2)
        return None

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen = set()
        deadline = time.monotonic() + self.WALL_CLOCK_BUDGET_SEC
        limit_label = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self.PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] page safety cap reached at {page}; stopping")
                break
            if time.monotonic() > deadline:
                print(f"[{self.site_id}] wall-clock budget exhausted at page {page}")
                break

            html = self._get(self._archive_url(page), context=f"list page {page}")
            if html is None:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            paths = self._article_paths(html)
            fresh = [p for p in paths if p not in seen]
            if not fresh:
                print(f"[{self.site_id}] page {page}: no new articles; stopping (end reached)")
                break
            print(f"[{self.site_id}] page {page}: {len(fresh)} new article(s)")

            for path in fresh:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() > deadline:
                    break
                seen.add(path)
                try:
                    paper = self._fetch_article(path)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[{self.site_id}] article {path} failed: {exc}")
                    continue
                if self._delay:
                    time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _article_paths(self, html):
        pat = re.compile(rf"/{re.escape(self.AGENCY)}/prensa/[A-Za-z0-9\-]+")
        out = []
        seen = set()
        for m in pat.findall(html):
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _fetch_article(self, path):
        url = self._detail_url(path)
        html = self._get(url, context=f"detail {path}")
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")

        title = (
            self._meta(soup, "og:title")
            or self._text(soup.find("h1"))
        )
        title = self._clean(title)
        if len(title) < self.MIN_TITLE_CHARS:
            return None

        abstract = self._meta(soup, "og:description")
        body = self._article_body(soup)
        if body and len(body) > len(abstract):
            abstract = f"{abstract}\n\n{body}" if abstract else body
        abstract = self._clean(abstract)

        published = self._published_date(soup, html)
        external_id = path.rstrip("/").rsplit("/", 1)[-1]

        metadata = {
            "source_format": "html",
            "agency": self.AGENCY,
            "archive": f"{self.base_url}/{self.AGENCY}/archivo/prensa",
            "detail_page": url,
            "slug": external_id,
        }
        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([self.site_name], ensure_ascii=False),
            "abstract": abstract,
            "category": "prensa",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published,
            "url": url,
            "pdf_url": "",
            "doi": "",
            "department": self.site_name,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _article_body(self, soup):
        node = (
            soup.find("article")
            or soup.find("div", class_=re.compile(r"article|contenido|cuerpo|body|texto", re.I))
            or soup.find("main")
        )
        return self._text(node) if node else ""

    def _published_date(self, soup, html):
        meta = (
            self._meta(soup, "article:published_time")
            or self._meta(soup, "date")
            or self._meta(soup, "dcterms:date")
        )
        m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", meta or "")
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _meta(soup, name):
        tag = (
            soup.find("meta", attrs={"property": name})
            or soup.find("meta", attrs={"name": name})
        )
        return (tag.get("content") or "").strip() if tag and tag.get("content") else ""

    @staticmethod
    def _text(node):
        if node is None:
            return ""
        return node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)

    @staticmethod
    def _clean(value):
        if not value:
            return ""
        text = unescape(str(value))
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# ---------------------------------------------------------------------------
# Per-agency variants. One class per gob.mx ministry press archive that
# previously had no crawler. All share the base implementation above; only the
# agency slug (URL segment) and site_id differ. The custom-crawler loader
# registers every class in this module carrying a site_id + crawl().
# ---------------------------------------------------------------------------

_AGENCIES = {
    "gob-mx-agricultura": ("agricultura", "gob.mx Archivo de Prensa — Agriculture"),
    "gob-mx-bienestar": ("bienestar", "gob.mx Archivo de Prensa — Welfare"),
    "gob-mx-buengobierno": ("buengobierno", "gob.mx Archivo de Prensa — Good governance"),
    "gob-mx-defensa": ("defensa", "gob.mx Archivo de Prensa — Defense"),
    "gob-mx-mujeres": ("mujeres", "gob.mx Archivo de Prensa — Women"),
    "gob-mx-salud": ("salud", "gob.mx Archivo de Prensa — Health"),
    "gob-mx-se": ("se", "gob.mx Archivo de Prensa — Economy"),
    "gob-mx-sectur": ("sectur", "gob.mx Archivo de Prensa — Tourism"),
    "gob-mx-sedatu": ("sedatu", "gob.mx Archivo de Prensa — Territorial development"),
    "gob-mx-segob": ("segob", "gob.mx Archivo de Prensa — Interior"),
    "gob-mx-semar": ("semar", "gob.mx Archivo de Prensa — Navy"),
    "gob-mx-semarnat": ("semarnat", "gob.mx Archivo de Prensa — Environment"),
    "gob-mx-sep": ("sep", "gob.mx Archivo de Prensa — Public Education"),
    "gob-mx-shcp": ("shcp", "gob.mx Archivo de Prensa — Finance"),
    "gob-mx-sict": ("sict", "gob.mx Archivo de Prensa — Communications and Transport"),
    "gob-mx-sre": ("sre", "gob.mx Archivo de Prensa — Foreign Affairs"),
    "gob-mx-sspc": ("sspc", "gob.mx Archivo de Prensa — Security and Citizen Protection"),
    "gob-mx-stps": ("stps", "gob.mx Archivo de Prensa — Labor and Social Security"),
}


def _make(site_id, agency, name):
    return type(
        "".join(part.capitalize() for part in site_id.split("-")) + "Crawler",
        (GobMxPrensaCrawler,),
        {"site_id": site_id, "site_name": name, "AGENCY": agency},
    )


_VARIANTS = {sid: _make(sid, ag, nm) for sid, (ag, nm) in _AGENCIES.items()}
globals().update({v.__name__: v for v in _VARIANTS.values()})
