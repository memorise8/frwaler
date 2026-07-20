# -*- coding: utf-8 -*-
"""Crawler for Ministerio de Educación, Formación Profesional y Deportes — Prensa/Actualidad.

List endpoint: GET /prensa/actualidad.html?texto=&desde=YYYY-MM-DD&hasta=YYYY-MM-DD
  — returns all matching press releases in a single HTML page (no JS pagination).
  — article items: div.enlace  > span.fecha, p.titulo a, p.descripcion
Detail page: /prensa/actualidad/YYYY/MM/YYYYMMDD-slug.html
  — div#contenido.noticias > h1 (title), p#fecha (date), div.cte (body)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "educacionfpydeportes-gob-es-prensahtml"
_BASE_URL = "https://www.educacionfpydeportes.gob.es"
_LIST_URL = f"{_BASE_URL}/prensa/actualidad.html"
_PUBLISHER = "Ministerio de Educación, Formación Profesional y Deportes"
_ABSTRACT_MIN_CHARS = 50
_ABSTRACT_TARGET_CHARS = 100
_MAX_PAGES = 200


class EducacionFpyDeportesPrensaCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: educacionfpydeportes-gob-es-prensahtml"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:200]}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/3 failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw):
        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            text = value.get_text(separator=" ")
        else:
            text = str(value)
        text = unescape(text)
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _parse_date(raw: str) -> str | None:
        """Convert DD/MM/YYYY → YYYY-MM-DD; returns None on failure."""
        if not raw:
            return None
        s = raw.strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # List-page fetching & parsing
    # ------------------------------------------------------------------

    def _fetch_list(self) -> list[dict]:
        """Fetch all press releases via a single wide-date search request.

        The site's search form accepts `desde`/`hasta` (ISO dates) and returns
        all matching items in a single un-paginated HTML page.
        """
        url = f"{_LIST_URL}?texto=&desde=2000-01-01&hasta=2030-12-31"
        raw = self._curl(url, referer=_BASE_URL + "/prensa.html")
        if not raw:
            print(f"[{_SITE_ID}] list fetch failed")
            return []

        soup = self._parse_html(raw)
        if not soup:
            print(f"[{_SITE_ID}] list parse failed")
            return []

        items = []
        for div in soup.select("div.enlace"):
            fecha_el = div.find("span", class_="fecha")
            date_raw = self._clean(fecha_el) if fecha_el else ""

            titulo_a = div.select_one("p.titulo a")
            if not titulo_a:
                continue
            title = self._clean(titulo_a.get_text())
            href = titulo_a.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                article_url = f"{_BASE_URL}{href}"
            elif href.startswith("http"):
                article_url = href
            else:
                article_url = urljoin(_BASE_URL, href)

            desc_el = div.select_one("p.descripcion")
            description = self._clean(desc_el) if desc_el else ""

            items.append({
                "title": title,
                "url": article_url,
                "date_raw": date_raw,
                "description": description,
            })

        print(f"[{_SITE_ID}] list: {len(items)} articles found")
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch and parse a detail page; returns a partial paper dict or None."""
        raw = self._curl(url, referer=_LIST_URL)
        if not raw:
            return None

        soup = self._parse_html(raw)
        if not soup:
            return None

        result: dict = {}

        # Title from h1 inside #contenido
        contenido = soup.find(id="contenido")
        if contenido:
            h1 = contenido.find("h1")
            if h1:
                result["title"] = self._clean(h1)

            # Date from p#fecha
            fecha_p = contenido.find("p", id="fecha")
            if fecha_p:
                result["date_raw"] = self._clean(fecha_p)

            # Full body text from div.cte
            cte = contenido.find("div", class_="cte")
            if cte:
                result["abstract"] = self._clean(cte)

        # PDF links (look globally on the page)
        pdf_url = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf"):
                if href.startswith("/"):
                    pdf_url = f"{_BASE_URL}{href}"
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = urljoin(_BASE_URL, href)
                break
        if pdf_url:
            result["pdf_url"] = pdf_url

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_WALL_SECS = 25 * 60

        # Phase 1: get full article list (single wide-date search request)
        articles = self._fetch_list()
        if not articles:
            print(f"[{_SITE_ID}] no articles found — aborting")
            return 0

        # Simulate "pages" over the flat list for progress logging.
        # Each "page" = 15 items (the site's default page size).
        PAGE_SIZE = 15
        total = len(articles)
        p = 0

        for i, article in enumerate(articles):
            if saved >= limit_or_inf:
                break

            # Wall-clock safety
            elapsed = time.time() - start_time
            if elapsed > MAX_WALL_SECS:
                print(f"[{_SITE_ID}] wall-clock limit reached after {saved} saved")
                break

            # Simulated page boundary logging
            new_p = i // PAGE_SIZE
            if new_p != p:
                p = new_p
                if p % 10 == 0:
                    print(
                        f"[{_SITE_ID}] page {p}: saved {saved}/{limit_or_inf}"
                    )
                if p >= _MAX_PAGES:
                    print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached")
                    break

            url = article["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Extract external_id from URL path
            # e.g. /prensa/actualidad/2026/05/20260513-iesjaen.html → 20260513-iesjaen
            slug_match = re.search(
                r"/prensa/actualidad/\d+/\d+/([^/]+)\.html", url
            )
            if slug_match:
                external_id = slug_match.group(1)
            else:
                external_id = url.split("/")[-1].replace(".html", "")

            # post_number: leading numeric date portion of the slug
            num_match = re.match(r"^(\d+)", external_id)
            post_number = num_match.group(1) if num_match else external_id

            listed_date = self._parse_date(article.get("date_raw", ""))

            try:
                time.sleep(self._detail_delay)

                detail = self._fetch_detail(url)
                if detail is None:
                    print(f"[{_SITE_ID}] item {url} detail fetch failed; skipping")
                    continue

                # Title: prefer detail h1
                title = detail.get("title") or article["title"]

                # Abstract: prefer full body from detail page
                abstract = detail.get("abstract", "")
                if len(abstract) < _ABSTRACT_TARGET_CHARS:
                    # Fall back to list description if body is thin
                    fallback = article.get("description", "")
                    if len(fallback) > len(abstract):
                        abstract = fallback

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(
                        f"[{_SITE_ID}] skipping {url}: abstract too short "
                        f"({len(abstract)} chars)"
                    )
                    continue

                # Date: prefer detail-page date
                date_raw_detail = detail.get("date_raw", "")
                published_date = self._parse_date(date_raw_detail) or listed_date

                pdf_url = detail.get("pdf_url")

                metadata = {
                    "posted_date": article.get("date_raw", ""),
                    "slug": external_id,
                    "list_description": article.get("description", ""),
                }

                paper: dict = {
                    "external_id": external_id,
                    "url": url,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": listed_date,
                    "publisher": _PUBLISHER,
                    "pdf_url": pdf_url,
                    "post_number": post_number,
                    "category": "Prensa / Actualidad",
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] done: saved {saved} documents")
        return saved
