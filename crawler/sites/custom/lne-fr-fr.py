# -*- coding: utf-8 -*-
"""Crawler for LNE French press releases.

List endpoint: https://www.lne.fr/fr/communiques-de-presse?page=N
Detail endpoint: each press-release HTML page linked from the list.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import unquote, urlencode, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised only in stripped envs
    BeautifulSoup = None


class LneFrFrCrawler(BaseCrawler):
    site_id = "lne-fr-fr"
    site_name = "Custom: lne-fr-fr"
    base_url = "https://www.lne.fr"

    START_URL = "https://www.lne.fr/fr/communiques-de-presse"
    MAX_PAGES = 200
    CURL_TIMEOUT = 60
    WALL_CLOCK_BUDGET = 25 * 60
    MIN_ABSTRACT_CHARS = 50
    BACKOFF = (1, 3, 9)

    MONTHS_FR = {
        "janvier": "01",
        "fevrier": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "aout": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "decembre": "12",
    }

    def crawl(self, limit=None):
        saved = 0
        page = 0
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        while page < self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time >= self.WALL_CLOCK_BUDGET - 30:
                print(f"[{self.site_id}] wall-clock budget nearly reached; exiting cleanly.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed or empty; stopping.")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} parse failed; stopping.")
                break

            items = self._parse_list(soup, page, list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping.")
                break

            new_items = []
            for item in items:
                item_url = item.get("url")
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping.")
                break

            for idx, item in enumerate(new_items, start=1):
                if limit is not None and saved >= limit:
                    break

                detail_url = item.get("url")
                try:
                    if time.time() - start_time >= self.WALL_CLOCK_BUDGET - 30:
                        print(f"[{self.site_id}] wall-clock budget nearly reached; exiting cleanly.")
                        return saved

                    time.sleep(self._detail_delay())
                    detail_raw = self._curl_get(detail_url, context=f"detail {detail_url}")
                    if not detail_raw:
                        print(f"[{self.site_id}] item {detail_url} failed: empty detail response")
                        continue

                    detail_soup = self._make_soup(detail_raw, context=f"detail {detail_url}")
                    if detail_soup is None:
                        print(f"[{self.site_id}] item {detail_url} failed: detail parse error")
                        continue

                    parsed = self._parse_detail(detail_soup, detail_raw, item)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = self._to_paper(parsed)
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url or idx} failed: {exc}")
                    continue

            page += 1

        if page >= self.MAX_PAGES:
            print(f"[{self.site_id}] Safety cap of {self.MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _list_url(self, page: int) -> str:
        if page <= 0:
            return self.START_URL
        query = urlencode({
            "page": str(page),
            "sort_by": "field_presse_date_value",
            "sort_order": "DESC",
        })
        return f"{self.START_URL}?{query}"

    def _parse_list(self, soup, page: int, list_url: str) -> list[dict]:
        records = []
        for teaser in soup.select(".actus-teaser__item"):
            link = teaser.select_one("a.node_teaser_block__link[href]")
            if not link:
                continue

            href = (link.get("href") or "").strip()
            detail_url = urljoin(self.base_url, href)

            title_el = teaser.select_one(".actus-teaser__title .field--name-title")
            if title_el is None:
                title_el = teaser.select_one(".actus-teaser__title")
            title = self._clean_text(title_el.get_text(" ", strip=True)) if title_el else ""

            date_el = teaser.select_one(".actus-teaser__date")
            listed_date_raw = self._clean_text(date_el.get_text(" ", strip=True)) if date_el else ""
            listed_date = self._parse_fr_date(listed_date_raw)

            text_el = teaser.select_one(".actus-teaser__text")
            list_abstract = self._clean_text(text_el.get_text(" ", strip=True)) if text_el else ""

            slug = self._slug_from_url(detail_url)
            records.append({
                "url": detail_url,
                "slug": slug,
                "list_page": page,
                "list_url": list_url,
                "list_title": title,
                "list_abstract": list_abstract,
                "listed_date_raw": listed_date_raw,
                "listed_date": listed_date,
                "category": "Communique de presse",
            })
        return records

    def _parse_detail(self, soup, raw_html: str, list_item: dict) -> dict:
        detail_url = list_item["url"]
        settings = self._extract_drupal_settings(soup)
        node_id = self._extract_node_id(raw_html, settings)
        slug = list_item.get("slug") or self._slug_from_url(detail_url)

        title_el = soup.select_one("main .edito h1") or soup.select_one("h1")
        title = self._clean_text(title_el.get_text(" ", strip=True)) if title_el else ""
        if not title:
            title = self._meta_content(soup, "og:title") or list_item.get("list_title") or slug

        date_el = soup.select_one("main .edito .teaser__date") or soup.select_one(".teaser__date")
        detail_date_raw = self._clean_text(date_el.get_text(" ", strip=True)) if date_el else ""
        published_date = self._parse_fr_date(detail_date_raw) or list_item.get("listed_date")
        listed_date = list_item.get("listed_date") or published_date

        abstract = self._extract_abstract(soup)
        if not abstract:
            abstract = self._meta_content(soup, "description") or list_item.get("list_abstract") or ""

        authors = self._extract_authors(soup)
        pdf_url = self._extract_pdf_url(soup)
        original_filename = self._filename_from_url(pdf_url)
        external_id = node_id or slug
        post_number = node_id if node_id and node_id.isdigit() else slug

        metadata = {
            "posted_date": list_item.get("listed_date_raw") or detail_date_raw,
            "posted_date_iso": listed_date,
            "listed_date": listed_date,
            "listed_date_raw": list_item.get("listed_date_raw"),
            "detail_date_raw": detail_date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": node_id,
            "nid": node_id,
            "slug": slug,
            "post_number": post_number,
            "currentPath": self._dig(settings, ("path", "currentPath")),
            "statistics": settings.get("statistics") if isinstance(settings, dict) else None,
            "listEndpoint": list_item.get("list_url"),
            "detailEndpoint": detail_url,
            "list_page": list_item.get("list_page"),
            "list_title": list_item.get("list_title"),
            "list_abstract": list_item.get("list_abstract"),
            "og_type": self._meta_content(soup, "og:type"),
            "og_description": self._meta_content(soup, "og:description"),
            "source": "LNE Drupal 10 HTML",
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "Laboratoire national de metrologie et d'essais (LNE)",
            "department": "Presse",
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": self._extract_keywords(soup),
            "category": "Communique de presse",
            "doi": None,
            "original_filename": original_filename,
            "metadata": metadata,
        }

    def _to_paper(self, parsed: dict) -> dict:
        paper = {
            "id": parsed.get("id"),
            "site_id": self.site_id,
            "external_id": parsed.get("external_id"),
            "post_number": parsed.get("post_number"),
            "title": parsed.get("title"),
            "abstract": parsed.get("abstract"),
            "published_date": parsed.get("published_date"),
            "listed_date": parsed.get("listed_date"),
            "posted_date": parsed.get("posted_date") or parsed.get("listed_date"),
            "authors": parsed.get("authors"),
            "publisher": parsed.get("publisher"),
            "department": parsed.get("department"),
            "journal": parsed.get("journal"),
            "url": parsed.get("url"),
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": parsed.get("category"),
            "doi": parsed.get("doi"),
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(parsed.get("metadata") or {}, ensure_ascii=False),
        }
        return paper

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(self.CURL_TIMEOUT),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"curl exit {result.returncode}: {stderr}")
                body = result.stdout
                if not body:
                    raise RuntimeError("empty response")
                return body.decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    time.sleep(self.BACKOFF[attempt - 1])
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw: str | bytes, context: str = "HTML"):
        if BeautifulSoup is None:
            print(f"[{self.site_id}] BeautifulSoup unavailable for {context}")
            return None
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _extract_abstract(self, soup) -> str:
        edito = soup.select_one("main .edito") or soup.select_one(".edito")
        if edito is None:
            return ""

        parts = []
        chapo = edito.select_one(".field-name-field-page-chapo")
        if chapo is not None:
            text = self._clean_text(chapo.get_text(" ", strip=True))
            if text:
                parts.append(text)

        for el in edito.select("p, li"):
            text = self._clean_text(el.get_text(" ", strip=True))
            if not text:
                continue
            if text in parts:
                continue
            parts.append(text)

        return "\n\n".join(parts).strip()

    def _extract_authors(self, soup) -> str | None:
        authors = []
        for el in soup.select("main .edito .author, .edito .author"):
            text = self._clean_text(el.get_text(" ", strip=True))
            if text:
                authors.append(text)
        return "; ".join(dict.fromkeys(authors)) if authors else None

    def _extract_keywords(self, soup) -> str | None:
        values = []
        for selector in ('meta[name="keywords"]', 'meta[property="article:tag"]'):
            for meta in soup.select(selector):
                content = self._clean_text(meta.get("content") or "")
                if content:
                    values.extend([p.strip() for p in content.split(",") if p.strip()])
        return ",".join(dict.fromkeys(values)) if values else None

    def _extract_pdf_url(self, soup) -> str | None:
        for link in soup.select("a[href]"):
            href = (link.get("href") or "").strip()
            path = urlparse(href).path.lower()
            if path.endswith(".pdf"):
                return urljoin(self.base_url, href)
        return None

    def _extract_drupal_settings(self, soup) -> dict:
        script = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
        if script is None:
            return {}
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _extract_node_id(self, raw_html: str, settings: dict) -> str | None:
        nid = self._dig(settings, ("statistics", "data", "nid"))
        if nid:
            return str(nid)

        current_path = self._dig(settings, ("path", "currentPath"))
        match = re.search(r"\bnode/(\d+)\b", str(current_path or ""))
        if match:
            return match.group(1)

        match = re.search(r"\bnode/(\d+)\b", raw_html or "")
        if match:
            return match.group(1)
        return None

    def _meta_content(self, soup, name: str) -> str | None:
        selectors = [
            f'meta[property="{name}"]',
            f'meta[name="{name}"]',
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if el is not None:
                content = self._clean_text(el.get("content") or "")
                if content:
                    return content
        return None

    def _parse_fr_date(self, raw: str | None) -> str | None:
        if not raw:
            return None
        text = self._strip_accents(self._clean_text(raw)).lower()
        text = text.replace("1er ", "1 ")
        match = re.search(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", text)
        if not match:
            match_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
            return match_iso.group(0) if match_iso else None
        day, month_name, year = match.groups()
        month = self.MONTHS_FR.get(month_name)
        if not month:
            return None
        return f"{year}-{month}-{day.zfill(2)}"

    def _clean_text(self, value: str | None) -> str:
        if not value:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _strip_accents(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        return unquote(path.rsplit("/", 1)[-1]) if path else url

    def _filename_from_url(self, url: str | None) -> str | None:
        if not url:
            return None
        tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        return tail or None

    def _detail_delay(self) -> float:
        try:
            return float(getattr(self, "_delay", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def _dig(self, data, path):
        cur = data
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        return cur
