# -*- coding: utf-8 -*-
"""Crawler for Ministerio de Defensa de España – Notas de Prensa.

List pages load ALL items client-side via jplist.js — no server-side
pagination.  One HTML page per year:
  current year: https://www.defensa.gob.es/gabinete/notasPrensa/
  past years:   https://www.defensa.gob.es/gabinete/notasPrensa/YYYY/
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class DefensaGobEsGabineteCrawler(BaseCrawler):
    site_id = "defensa-gob-es-gabinete"
    site_name = "Custom: defensa-gob-es-gabinete"
    base_url = "https://www.defensa.gob.es"

    _LIST_BASE = "https://www.defensa.gob.es/gabinete/notasPrensa/"
    _MIN_YEAR = 2007
    _MAX_PAGES = 200
    _CURL_TIMEOUT = 45
    _BACKOFF = (1, 3, 9)
    _MIN_ABSTRACT = 50  # chars — skip if shorter

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay
        self._crawl_start = time.time()

    # ------------------------------------------------------------------
    # Main crawl entry
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        page_count = 0
        current_year = datetime.now().year
        limit_str = str(limit) if limit is not None else "inf"

        for year in range(current_year, self._MIN_YEAR - 1, -1):
            if limit is not None and saved >= limit:
                break
            if page_count >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")
                break
            # wall-clock budget: 25 minutes
            if time.time() - self._crawl_start > 25 * 60:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping")
                break

            list_url = self._list_url(year, current_year)
            if page_count % 10 == 0:
                print(f"[{self.site_id}] page {page_count} (year={year}): saved {saved}/{limit_str}")

            raw = self._curl_get(list_url, context=f"list year={year}")
            if not raw:
                print(f"[{self.site_id}] list page year={year} failed; stopping")
                break

            soup = self._make_soup(raw, context=f"list year={year}")
            if soup is None:
                print(f"[{self.site_id}] parse failed for year={year}; stopping")
                break

            records = self._parse_list(soup, list_url)
            if not records:
                print(f"[{self.site_id}] no records for year={year}; stopping")
                break

            print(f"[{self.site_id}] year={year}: {len(records)} records on list page")
            page_count += 1

            for idx, record in enumerate(records, 1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - self._crawl_start > 25 * 60:
                    break

                item_label = f"year={year} item={idx}"
                try:
                    detail_url = record.get("url", "")
                    if not detail_url:
                        raise RuntimeError("record has no detail URL")
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    time.sleep(self._detail_delay)
                    detail_raw = self._curl_get(
                        detail_url, context=f"detail {item_label}", referer=list_url
                    )
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw, context=f"detail {item_label}")
                    if detail_soup is None:
                        raise RuntimeError("detail page could not be parsed")

                    parsed = self._parse_detail(detail_soup, record)
                    abstract = (parsed.get("abstract") or "").strip()
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "abstract": abstract,
                        "published_date": parsed["published_date"],
                        "posted_date": parsed["listed_date"],  # → posted_date column via adapter
                        "url": parsed["url"],                  # → meta_url via adapter
                        "pdf_url": parsed["pdf_url"] or None,
                        "keywords": None,
                        "department": "Ministerio de Defensa de España",  # → publisher
                        "doi": None,
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] {item_label} failed: {exc}")
                    continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _list_url(self, year: int, current_year: int) -> str:
        if year == current_year:
            return self._LIST_BASE
        return f"{self._LIST_BASE}{year}/"

    def _curl_get(self, url: str, context: str = "request", referer: str | None = None) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self._CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self._CURL_TIMEOUT + 10
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    wait = self._BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, context: str = "HTML") -> BeautifulSoup | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup: BeautifulSoup, list_url: str) -> list:
        records = []
        seen: set = set()
        for li in soup.select("li.listados1.list-group-item.noticias"):
            try:
                date_node = li.select_one("dt.fecha")
                link = li.select_one("dd a[href]")
                teaser_node = li.select_one("dd p")
                if link is None:
                    continue

                href = link.get("href", "").strip()
                if not href:
                    continue

                detail_url = urljoin(list_url, href)
                if detail_url in seen:
                    continue
                seen.add(detail_url)

                date_raw = self._node_text(date_node)
                title = self._node_text(link)
                if not title:
                    continue

                records.append({
                    "url": detail_url,
                    "title": title,
                    "teaser": self._node_text(teaser_node),
                    "listed_date_raw": date_raw,
                    "listed_date": self._parse_date_es(date_raw),
                    "external_id": self._external_id_from_url(detail_url),
                    "post_number": self._post_number_from_url(detail_url),
                })
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue
        return records

    def _parse_detail(self, soup: BeautifulSoup, record: dict) -> dict:
        # Date from detail header
        hfecha_node = soup.select_one("span.hFecha")
        date_raw = self._node_text(hfecha_node)
        published_date = self._parse_date_es(date_raw) or record.get("listed_date") or ""

        # Category label
        hcabnot = soup.select_one("span.hCabNot")
        category = self._node_text(hcabnot)

        # Main article text from div.contenido
        abstract = self._extract_body_text(soup)

        # Fall back to meta description if body is short
        if len(abstract) < self._MIN_ABSTRACT:
            meta = self._meta_content(soup, "description", "og:description")
            if meta:
                abstract = meta

        # PDF links (scan entire page)
        pdf_url = ""
        original_filename = None
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                pdf_url = urljoin(self.base_url, href)
                seg = urlparse(pdf_url).path.rsplit("/", 1)[-1].split("?")[0]
                original_filename = seg or None
                break

        # Canonical URL
        canonical = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url")
        )
        url = urljoin(self.base_url, canonical) if canonical else record.get("url", "")

        metadata = {
            "posted_date": record.get("listed_date", ""),         # adapter picks up posted_date
            "listed_date_raw": record.get("listed_date_raw", ""),
            "post_number": record.get("post_number"),
            "teaser": record.get("teaser", ""),
            "category": category,
            "detail_fecha_raw": date_raw,
            "originalFilename": original_filename,                 # adapter key
            "source": "defensa.gob.es HTML press release page",
        }

        return {
            "external_id": record.get("external_id", ""),
            "post_number": record.get("post_number"),
            "title": record.get("title", ""),
            "abstract": abstract.strip(),
            "published_date": published_date,
            "listed_date": record.get("listed_date", ""),
            "url": url or record.get("url", ""),
            "pdf_url": pdf_url or None,
            "category": category or None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _extract_body_text(self, soup: BeautifulSoup) -> str:
        contenido = soup.select_one("div.contenido")
        container = contenido if contenido else soup

        # Work on a copy to avoid mutating the original
        try:
            work = BeautifulSoup(str(container), "html.parser")
        except Exception:
            work = container

        for bad in work.select(
            "script, style, noscript, .barraLinks, .compartirRRSS, "
            ".gallery, #galeriaImagenes, dl.incluye, .menuLateral, "
            "nav, .nav, .navbar, header, footer, .Pie, .pie-unificado"
        ):
            try:
                bad.decompose()
            except Exception:
                pass

        parts = []
        seen_texts: set = set()
        for node in work.find_all(["p", "h2", "h3", "h4", "li"], recursive=True):
            text = self._node_text(node)
            if not text:
                continue
            key = re.sub(r"\s+", "", text.lower())
            if key in seen_texts:
                continue
            # Skip navigation noise
            if len(text) < 10 and not re.search(r"\w{4,}", text):
                continue
            seen_texts.add(key)
            parts.append(text)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date_es(text: str) -> str:
        """Convert DD/MM/YYYY → YYYY-MM-DD."""
        if not text:
            return ""
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            return f"{y}-{mo:02d}-{d:02d}"
        m2 = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
        if m2:
            return m2.group(0)
        return ""

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        """Strip base prefix and .html extension to get a stable external_id."""
        path = urlparse(url).path
        marker = "/gabinete/notasPrensa/"
        idx = path.find(marker)
        if idx != -1:
            path = path[idx + len(marker):]
        path = path.rstrip("/")
        if path.endswith(".html"):
            path = path[:-5]
        return path or url

    @staticmethod
    def _post_number_from_url(url: str) -> str | None:
        """Extract YYYYMMDD from slug, e.g. DGC-260601-... → '20260601'."""
        filename = urlparse(url).path.rsplit("/", 1)[-1]
        # Pattern: DGC-YYMMDD-slug or DTC-YYMMDD_slug
        m = re.search(r"-(\d{2})(\d{2})(\d{2})[_-]", filename)
        if m:
            yy, mm, dd = m.group(1), m.group(2), m.group(3)
            return f"20{yy}{mm}{dd}"
        # Fallback: any 6-digit number
        m2 = re.search(r"(\d{6})", filename)
        if m2:
            return m2.group(1)
        return None

    @staticmethod
    def _node_text(node) -> str:
        if node is None:
            return ""
        raw = node.get_text(" ", strip=True)
        raw = raw.replace("\xa0", " ").replace("​", "")
        return re.sub(r"\s+", " ", raw).strip()

    @staticmethod
    def _meta_content(soup: BeautifulSoup, *keys: str) -> str:
        for key in keys:
            for attr in ("name", "property"):
                node = soup.find("meta", attrs={attr: key})
                if node and node.get("content"):
                    return node["content"].strip()
        return ""

    @staticmethod
    def _link_href(soup: BeautifulSoup, selector: str) -> str:
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node["href"].strip()
        return ""
