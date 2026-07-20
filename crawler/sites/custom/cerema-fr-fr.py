# -*- coding: utf-8 -*-
"""Crawler for Cerema (Centre d'études et d'expertise sur les risques, l'environnement, la mobilité et l'aménagement).

Target: https://www.cerema.fr/fr/presse/dossier
Drupal 11 Views listing — HTML pagination (?page=N), 15 items/page.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs

from crawler.base_crawler import BaseCrawler

_SITE_ID = "cerema-fr-fr"
_BASE_URL = "https://www.cerema.fr"
_LIST_URL = "https://www.cerema.fr/fr/presse/dossier"
_PAGE_SIZE = 15
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 100

_FR_MONTHS = {
    "jan": "01", "fév": "02", "fev": "02", "mar": "03", "avr": "04",
    "mai": "05", "juin": "06", "juil": "07", "aoû": "08", "aou": "08",
    "sep": "09", "oct": "10", "nov": "11", "déc": "12", "dec": "12",
}


class CeremaFrFrCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: cerema-fr-fr"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 45) -> str | None:
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15, check=False)
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = f"exit={result.returncode}"

            if attempt < len(waits):
                print(f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed ({last_error}) for {url}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw: str | bytes):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _parse_fr_date(day: str, month: str, year: str) -> str:
        """Convert French day/month-abbrev/year to YYYY-MM-DD."""
        month_key = month.lower().strip()[:4].rstrip(".")
        mm = _FR_MONTHS.get(month_key)
        if not mm:
            # Try normalising accented chars
            month_key2 = month_key.replace("é", "e").replace("è", "e").replace("û", "u")
            mm = _FR_MONTHS.get(month_key2)
        if not mm:
            return ""
        try:
            dd = day.strip().zfill(2)
            yyyy = year.strip()
            return f"{yyyy}-{mm}-{dd}"
        except Exception:
            return ""

    @staticmethod
    def _node_id_from_article(article) -> str:
        """Extract numeric node ID from article[data-history-node-id] or class node--NNN."""
        if article is None:
            return ""
        nid = article.get("data-history-node-id", "").strip()
        if nid:
            return nid
        for cls in article.get("class", []):
            m = re.match(r"node--(\d+)$", cls)
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _pdf_from_soup(soup, base: str = _BASE_URL) -> tuple[str, str]:
        """Return (pdf_url, original_filename) for the first PDF link found."""
        if soup is None:
            return "", ""
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href:
                continue
            if re.search(r"\.pdf(?:[?&#]|$)", href, re.I):
                full = href if href.startswith("http") else urljoin(base, href)
                # Extract filename from query param ?file=…/name.pdf or last path segment
                parsed = urlparse(href)
                qs_file = parse_qs(parsed.query).get("file", [""])
                if qs_file and qs_file[0]:
                    fname = qs_file[0].rsplit("/", 1)[-1]
                else:
                    fname = parsed.path.rsplit("/", 1)[-1] or ""
                return full, fname
        return "", ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        url = _LIST_URL if page == 0 else f"{_LIST_URL}?page={page}"
        return self._curl(url, referer=_BASE_URL + "/")

    def _parse_list_page(self, raw: str) -> list[dict]:
        soup = self._parse_html(raw)
        if soup is None:
            return []

        items = []
        for row in soup.select(".views-row"):
            try:
                art = row.find("article")
                if art is None:
                    continue

                node_id = self._node_id_from_article(art)

                # Link & title from h2 > a
                h2 = row.find("h2")
                link_node = h2.find("a", href=True) if h2 else row.find("a", href=True)
                if link_node is None:
                    continue
                href = link_node.get("href", "").strip()
                if not href:
                    continue
                url = urljoin(_BASE_URL, href)
                title = self._one_line(link_node.get_text(" ", strip=True))
                if not title:
                    continue

                # Date from .publication-date > .day / .month / .year
                date_str = ""
                pub_div = row.find(class_="publication-date")
                if pub_div:
                    day = pub_div.find(class_="day")
                    month = pub_div.find(class_="month")
                    year = pub_div.find(class_="year")
                    if day and month and year:
                        date_str = self._parse_fr_date(
                            day.get_text(strip=True),
                            month.get_text(strip=True),
                            year.get_text(strip=True),
                        )

                # Category
                cat_node = row.find(class_=re.compile(r"field--name-field-press.*category"))
                category = self._one_line(cat_node.get_text(" ", strip=True)) if cat_node else ""

                items.append({
                    "node_id": node_id,
                    "url": url,
                    "title": title,
                    "published_date": date_str,
                    "category": category,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> str | None:
        return self._curl(url, referer=_LIST_URL)

    def _parse_detail(self, raw: str, item: dict) -> dict:
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        url = item["url"]
        title = item["title"]
        published_date = item.get("published_date", "")
        category = item.get("category", "")

        # Node ID: prefer from article class on detail page
        art = soup.find("article")
        node_id = self._node_id_from_article(art) or item.get("node_id", "")

        # Slug as fallback external_id
        slug = urlparse(url).path.strip("/").rsplit("/", 1)[-1] or url
        external_id = node_id if node_id else slug
        post_number = node_id if node_id else None

        # Refine title from h1 or og:title
        h1 = soup.find("h1")
        if h1:
            t = self._one_line(h1.get_text(" ", strip=True))
            if t:
                title = t
        if not title:
            meta_title = soup.find("meta", attrs={"property": "og:title"})
            if meta_title and meta_title.get("content"):
                title = self._one_line(meta_title.get("content", ""))

        # Abstract from field--name-body
        abstract = ""
        body_field = None
        if art:
            body_field = art.find(class_="field--name-body")
        if body_field is None:
            body_field = soup.find(class_="field--name-body")

        if body_field:
            for bad in body_field.select("script, style, noscript"):
                bad.decompose()
            parts = []
            for node in body_field.find_all(["p", "li", "div"], recursive=True):
                if node.find(["p", "li"]):
                    continue
                text = self._one_line(node.get_text(" ", strip=True))
                if text and len(text) > 20:
                    parts.append(text)
            if parts:
                abstract = "\n\n".join(parts)

        # Fallback: og:description / meta description
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            for attr_pair in [
                ("property", "og:description"),
                ("name", "description"),
            ]:
                meta = soup.find("meta", attrs={attr_pair[0]: attr_pair[1]})
                if meta and meta.get("content"):
                    text = self._clean(meta.get("content", ""))
                    if len(text) > len(abstract):
                        abstract = text
                if len(abstract) >= _ABSTRACT_MIN_CHARS:
                    break

        abstract = self._clean(abstract)

        # PDF
        pdf_url, original_filename = self._pdf_from_soup(soup)

        # Keywords from any tag/keyword field
        kw_parts = []
        for sel in ["field--name-field-tags", "field--name-field-keywords", "field--name-field-domaine"]:
            el = soup.find(class_=sel)
            if el:
                kw_parts += [self._one_line(a.get_text(" ", strip=True)) for a in el.find_all("a")]
        keywords = ",".join(kw_parts) if kw_parts else ""

        metadata = {
            "source": "cerema.fr press listing + detail page",
            "list_url": _LIST_URL,
            "canonical_url": url,
            "node_id": node_id,
            "slug": slug,
        }

        return {
            "id": external_id,
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "authors": "",
            "publisher": "Cerema",
            "department": "",
            "journal": "",
            "category": category,
            "keywords": keywords,
            "published_date": published_date,
            "listed_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        for page in range(_MAX_PAGES):
            # 25-minute wall-clock budget
            if time.time() - crawl_start > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] no rows on page {page}; stopping")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping")
                break

            if page == _MAX_PAGES - 1:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                seen_urls.add(url)

                try:
                    time.sleep(self._detail_delay)
                    raw_detail = self._fetch_detail(url)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {url} failed: empty detail response")
                        continue

                    paper = self._parse_detail(raw_detail, item)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] item {url} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
