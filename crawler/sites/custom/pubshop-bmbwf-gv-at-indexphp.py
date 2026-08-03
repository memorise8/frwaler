# -*- coding: utf-8 -*-
"""Crawler for BMBWF Publikationenshop (pubshop.bmbwf.gv.at).

List page:   index.php?article_id=1&type=gesamtkatalog&elementsPerRow=20&sort=title&offset=X
Detail page: index.php?article_id=9&type=gesamtkatalog&pub=<ID>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlsplit, parse_qsl

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PubshopBmbwfGvAtIndexphpCrawler(BaseCrawler):
    site_id = "pubshop-bmbwf-gv-at-indexphp"
    site_name = "Custom: pubshop-bmbwf-gv-at-indexphp"
    base_url = "https://pubshop.bmbwf.gv.at"

    _LIST_URL = "https://pubshop.bmbwf.gv.at/index.php"
    _BACKOFF = (1, 3, 9)
    _MIN_ABSTRACT = 50
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _CRAWL_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_ts = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        offset = 0
        for page in range(self._MAX_PAGES):
            # Time budget
            if time.time() - start_ts > self._CRAWL_BUDGET_S:
                print(f"[{self.site_id}] 25-min budget reached at page {page}; exiting cleanly")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = (
                f"{self._LIST_URL}?article_id=1&type=gesamtkatalog"
                f"&elementsPerRow={self._PAGE_SIZE}&sort=title&offset={offset}"
            )
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] list page {page} parse failed; stopping")
                break

            items = self._parse_list_page(soup)
            if not items:
                print(f"[{self.site_id}] list page {page} returned 0 items; done")
                break

            new_items = [it for it in items if it["detail_url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] list page {page}: all items already seen; done")
                break

            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_ts > self._CRAWL_BUDGET_S:
                    print(f"[{self.site_id}] time budget exceeded mid-page; stopping")
                    break

                seen_urls.add(it["detail_url"])
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        it["detail_url"], context=f"detail pub={it['pub_id']}"
                    )
                    if not detail_raw:
                        print(f"[{self.site_id}] item pub={it['pub_id']} detail fetch failed; skipping")
                        continue

                    detail_soup = self._make_soup(detail_raw)
                    if detail_soup is None:
                        print(f"[{self.site_id}] item pub={it['pub_id']} parse failed; skipping")
                        continue

                    paper = self._parse_detail(detail_soup, it)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item pub={it['pub_id']} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item pub={it.get('pub_id', '?')} failed: {exc}")
                    continue

            offset += self._PAGE_SIZE

        else:
            # for-loop exhausted without break → hit MAX_PAGES
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_list_page(self, soup):
        items = []
        for el in soup.select("div.pubListElement"):
            # Prefer the title link; fall back to the image link
            link = el.select_one("div.pubDetails div.pubElementTitle a[href]")
            if not link:
                link = el.select_one("div.pubImage a[href]")
            if not link:
                continue

            href = link.get("href", "")
            if not href:
                continue

            m = re.search(r"[?&]pub=(\d+)", href)
            if not m:
                continue
            pub_id = m.group(1)
            detail_url = urljoin(self.base_url, href)
            items.append({"pub_id": pub_id, "detail_url": detail_url})
        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, soup, list_item):
        pub_id = list_item["pub_id"]

        title_el = soup.select_one("div.pubTitle")
        if not title_el:
            print(f"[{self.site_id}] pub={pub_id}: no title element; skipping")
            return None
        title = self._clean(title_el.get_text(" ", strip=True))
        if not title:
            print(f"[{self.site_id}] pub={pub_id}: empty title; skipping")
            return None

        subtitle_el = soup.select_one("div.pubTitleAdditional")
        subtitle = self._clean(subtitle_el.get_text(" ", strip=True)) if subtitle_el else ""

        fields, description = self._parse_fields(soup)

        # If description still short, prepend subtitle
        if subtitle and len(description) < 100:
            description = self._clean(
                f"{subtitle}. {description}" if description else subtitle
            )

        # PDF download link
        pdf_url = ""
        original_filename = ""
        dl_link = soup.select_one("div.pubDownload a[href]")
        if not dl_link:
            # also look in sidebar pubActions
            dl_link = soup.select_one("div.pubActions div.pubDownload a[href]")
        if dl_link:
            raw_href = dl_link.get("href", "")
            if raw_href:
                pdf_url = urljoin(self.base_url, raw_href)
                original_filename = self._filename_from_url(pdf_url)

        # Authors
        authors = fields.get("VerfasserIn", "")

        # Publisher: prefer HerausgeberIn, fall back to Verlag
        publisher_parts = [
            p for p in (fields.get("HerausgeberIn"), fields.get("Verlag"))
            if p
        ]
        publisher = "; ".join(publisher_parts)

        # Published date
        year = fields.get("Erscheinungsjahr", "").strip()
        published_date = f"{year}-01-01" if re.fullmatch(r"\d{4}", year) else ""

        # Series / journal
        series = fields.get("Reihentitel", "")
        series_number = fields.get("Reihennummer", "")
        journal = f"{series} {series_number}".strip() if series_number else series

        # Keywords from category tags on detail page
        cats = [self._clean(a.get_text()) for a in soup.select("ul.slimlist li a")]
        cats = [c for c in cats if c]
        keywords = ",".join(cats)

        # Category (media type)
        category = fields.get("Medientyp", "")

        # ISBN / DOI
        isbn = fields.get("ISBN", "")
        doi = fields.get("DOI", "")

        metadata = {
            "pub_id": pub_id,
            "subtitle": subtitle,
            "series": series,
            "series_number": series_number,
            "erscheinungsort": fields.get("Erscheinungsort", ""),
            "umfang": fields.get("Umfang", ""),
            "medientyp": category,
            "format": fields.get("Format", ""),
            "isbn": isbn,
            "standort": fields.get("Standort", ""),
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "site_id": self.site_id,
            "external_id": pub_id,
            "post_number": pub_id,
            "title": title,
            "abstract": description,
            "published_date": published_date,
            "listed_date": "",
            "authors": authors,
            "publisher": publisher,
            "journal": journal,
            "url": list_item["detail_url"],
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "department": "",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # HTML field parsing helpers
    # ------------------------------------------------------------------

    def _parse_fields(self, soup):
        """Return (fields_dict, description_str) from div.pubDetails paragraphs."""
        fields: dict[str, str] = {}
        description = ""

        for p in soup.select("div.pubDetails p"):
            # Detect Beschreibung paragraph first (may contain colons in the text)
            strong_texts = [s.get_text(strip=True) for s in p.find_all("strong")]
            if any("Beschreibung" in st for st in strong_texts):
                full = p.get_text(separator=" ", strip=True)
                idx = full.find("Beschreibung:")
                if idx >= 0:
                    description = self._clean(full[idx + len("Beschreibung:"):])
                continue

            # Parse "Label: value" pairs — one <p> may hold several via <br/>
            p_text = p.get_text(separator="\n", strip=True)
            pending_key: str | None = None
            for raw_line in p_text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                m = re.match(r"^([^:]{1,60}):\s*(.*)", line)
                if m:
                    key = self._clean(m.group(1))
                    val = self._clean(m.group(2))
                    if key:
                        if val:
                            fields[key] = val
                            pending_key = key
                        else:
                            # Value might be on the next line (e.g., Standort:)
                            pending_key = key
                elif pending_key and line:
                    # Continuation of a value-less label
                    fields[pending_key] = self._clean(line)
                    pending_key = None

        return fields, description

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request"):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--compressed", "--max-time", "45", "--connect-timeout", "15",
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
            url,
        ]
        last_error = ""
        for attempt, wait in enumerate(self._BACKOFF, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                body = result.stdout or b""
                if result.returncode == 0 and body.strip():
                    return body.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl exit {result.returncode}; empty body"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} curl failed (attempt {attempt}/3): {last_error}")
            if attempt < 3:
                print(f"[{self.site_id}] retrying in {wait}s…")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all parsers failed: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return ""
        # Try path segment first
        path = urlsplit(url).path
        if path:
            seg = path.rstrip("/").rsplit("/", 1)[-1]
            if "." in seg and len(seg) <= 200:
                return seg
        # Fall back to rex_media_file query param
        q = dict(parse_qsl(urlsplit(url).query))
        fn = q.get("rex_media_file", "")
        return fn if fn else ""
