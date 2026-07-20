# -*- coding: utf-8 -*-
"""Crawler for igualdad.gob.es/comunicacion/sala-de-prensa/ (press releases).

WordPress site — REST API is authentication-restricted, so we crawl
HTML list pages and individual detail pages.

List URL pattern:  /comunicacion/sala-de-prensa/           (page 1)
                   /comunicacion/sala-de-prensa/page/N/   (page N)
Card selector:     div.content-notice > div.content-text > p.title > a[href]
Date in card:      Spanish "DD de <mes> de YYYY" text
Detail content:    div.container.site-content
  h1                                        → title
  p.fecha-igu                               → published_date
  div.content-description li                → subtitle bullets
  p.wp-block-paragraph                      → body paragraphs
post_number:       body class page-id-N (WordPress post ID)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IgualdadGobEsComunicacionCrawler(BaseCrawler):
    site_id = "igualdad-gob-es-comunicacion"
    site_name = "Custom: igualdad-gob-es-comunicacion"
    base_url = "https://www.igualdad.gob.es"

    START_URL = "https://www.igualdad.gob.es/comunicacion/sala-de-prensa/"
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 100
    MAX_PAGES = 200
    _CURL_META = "__IGU_CURL_META__:"

    MONTHS_ES = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl sala de prensa HTML list pages and individual detail pages."""
        saved = 0
        page = 0
        seen_urls: set = set()
        start_time = time.time()

        try:
            while True:
                if time.time() - start_time > 1500:
                    print(f"[{self.site_id}] 25-minute wall-clock budget exhausted; stopping")
                    break
                if limit is not None and saved >= limit:
                    break
                if page >= self.MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                    break

                list_url = self._list_url(page)
                raw = self._curl_get(list_url, context=f"list page {page}", referer=self.START_URL)
                if not raw:
                    print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                    break

                soup = self._make_soup(raw, context=f"list page {page}")
                if soup is None:
                    print(f"[{self.site_id}] list page {page} parse failed; stopping")
                    break

                records = self._parse_list(soup, list_url)
                if not records:
                    print(f"[{self.site_id}] no records at list page {page}; stopping")
                    break

                if page % 10 == 0:
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                for idx, record in enumerate(records, start=1):
                    if limit is not None and saved >= limit:
                        break

                    item_label = f"page {page} item {idx}"
                    try:
                        detail_url = record.get("url") or ""
                        if not detail_url:
                            raise RuntimeError("record has no URL")
                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)

                        time.sleep(self.detail_delay)
                        detail_raw = self._curl_get(
                            detail_url,
                            context=f"detail {item_label}",
                            referer=list_url,
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(detail_raw, context=f"detail {item_label}")
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                        parsed = self._parse_detail(detail_soup, detail_raw, record)
                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
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
                            "posted_date": parsed["listed_date"],
                            "url": parsed["url"],
                            "publisher": "Ministerio de Igualdad",
                            "pdf_url": parsed.get("pdf_url") or "",
                            "keywords": parsed.get("keywords") or "",
                            "original_filename": parsed.get("original_filename") or "",
                            "metadata": json.dumps(parsed.get("metadata", {}), ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        counter = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] saved {counter}: {parsed['title'][:80]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {item_label} failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break
                if not self._has_next_page(soup, page):
                    break
                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user after {saved} saved")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}",
            "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.7",
            "-w", "\n" + self._CURL_META + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                stdout = (res.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, effective_url = self._split_curl_output(stdout, url)
                if res.returncode != 0:
                    stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {res.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body.strip():
                    raise RuntimeError(f"empty response for {effective_url}")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _split_curl_output(self, raw, fallback_url):
        pos = raw.rfind("\n" + self._CURL_META)
        if pos == -1:
            return raw, "", fallback_url
        body = raw[:pos]
        meta = raw[pos + 1 + len(self._CURL_META):].strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
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

    def _parse_list(self, soup, list_url):
        """Return list of record dicts from a sala-de-prensa list page."""
        records = []
        seen: set = set()
        for card in soup.find_all("div", class_="content-notice"):
            title_p = card.find("p", class_="title")
            if not title_p:
                continue
            link = title_p.find("a", href=True)
            if not link:
                continue

            url = link["href"].strip()
            # Only accept proper press-release detail URLs
            if not re.search(r"/comunicacion/sala-de-prensa/[^/]+/$", url):
                continue
            if url in seen:
                continue
            seen.add(url)

            title = self._node_text(link)
            if not title:
                continue

            content_text = card.find("div", class_="content-text")
            date_raw, listed_date = "", ""
            if content_text:
                card_text = content_text.get_text(" ", strip=True)
                date_raw, listed_date = self._parse_es_date(card_text)

            slug = urlparse(url).path.strip("/").split("/")[-1]
            records.append({
                "title": title,
                "url": url,
                "external_id": slug,
                "listed_date": listed_date,
                "list_date_raw": date_raw,
                "list_url": list_url,
            })
        return records

    def _parse_detail(self, soup, raw_html, record):
        """Parse a sala-de-prensa detail page into a normalised dict."""
        for tag in soup.find_all(["script", "style", "noscript"]):
            tag.decompose()

        # Canonical URL
        canonical = (
            self._link_href(soup, "link[rel='canonical']")
            or self._meta_content(soup, "og:url")
            or record.get("url", "")
        )
        url = urljoin(self.base_url, canonical) if canonical else record.get("url", "")

        # WordPress post ID → use as external_id / post_number
        external_id = None
        body_tag = soup.find("body")
        if body_tag:
            for cls in body_tag.get("class", []):
                m = re.match(r"page-id-(\d+)$", cls)
                if m:
                    external_id = m.group(1)
                    break
        if not external_id:
            m2 = re.search(r"[?&]p=(\d+)", raw_html or "")
            if m2:
                external_id = m2.group(1)
        if not external_id:
            external_id = (
                record.get("external_id")
                or urlparse(url).path.strip("/").split("/")[-1]
            )

        # Article container
        site_content = soup.find("div", class_="site-content")

        # Title
        h1 = site_content.find("h1") if site_content else soup.find("h1")
        title = self._node_text(h1)
        if not title:
            title = self._clean_title(self._meta_content(soup, "og:title"))
        if not title:
            title = record.get("title", "")
        title = self._clean_title(title)

        # Published date (p.fecha-igu)
        fecha_el = soup.find("p", class_="fecha-igu")
        date_raw, published_date = "", ""
        if fecha_el:
            date_raw, published_date = self._parse_es_date(self._node_text(fecha_el))
        if not published_date:
            mod_time = self._meta_content(soup, "article:modified_time")
            if mod_time:
                m3 = re.match(r"(\d{4}-\d{2}-\d{2})", mod_time)
                if m3:
                    published_date = m3.group(1)

        # Abstract ─ build from subtitle bullets + body paragraphs
        abstract_parts = []
        if site_content:
            # 1. Subtitle bullet points inside banner-details-igu
            for desc_div in site_content.find_all("div", class_="content-description"):
                for li in desc_div.find_all("li"):
                    txt = self._node_text(li)
                    if txt and txt not in abstract_parts:
                        abstract_parts.append(txt)

            # 2. Main body: p.wp-block-paragraph
            for p in site_content.find_all("p", class_="wp-block-paragraph"):
                txt = self._node_text(p)
                if txt and txt not in abstract_parts:
                    abstract_parts.append(txt)

            # 3. Fallback: all substantial <p> not already included
            if len("\n".join(abstract_parts)) < self.MIN_ABSTRACT_CHARS:
                skip_classes = {"fecha-igu", "mb-0", "title"}
                for p in site_content.find_all("p"):
                    p_classes = set(p.get("class") or [])
                    if p_classes & skip_classes:
                        continue
                    txt = self._node_text(p)
                    if txt and len(txt) > 30 and txt not in abstract_parts:
                        abstract_parts.append(txt)

            # 4. Also pull headings and list items from body area
            if len("\n".join(abstract_parts)) < self.MIN_ABSTRACT_CHARS:
                for tag in site_content.find_all(["h2", "h3", "li"]):
                    txt = self._node_text(tag)
                    if txt and len(txt) > 15 and txt not in abstract_parts:
                        abstract_parts.append(txt)

        abstract = "\n\n".join(abstract_parts)

        # Fallback: og:description (truncated, but better than nothing)
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            og_desc = self._meta_content(soup, "og:description", "description")
            if og_desc:
                og_desc = re.sub(
                    r"\s*[…\.]{0,3}\s*Sigue leyendo.*$", "", og_desc, flags=re.I
                ).strip()
                if len(og_desc) > len(abstract):
                    abstract = og_desc

        # PDF links
        pdf_url = ""
        original_filename = ""
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r"\.pdf(?:[?#]|$)", href, flags=re.I):
                pdf_links.append(href)
        if pdf_links:
            pdf_url = pdf_links[0]
            original_filename = urlparse(pdf_url).path.split("/")[-1]

        metadata = {
            "source": "igualdad.gob.es WordPress HTML",
            "wp_post_id": external_id,
            "list_url": record.get("list_url", ""),
            "posted_date": record.get("listed_date", ""),
            "list_date_raw": record.get("list_date_raw", ""),
            "fecha_igu_raw": date_raw,
            "pdf_links": pdf_links,
        }

        return {
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": record.get("listed_date", ""),
            "url": url,
            "pdf_url": pdf_url,
            "keywords": "",
            "original_filename": original_filename,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 0:
            return self.START_URL
        return f"{self.START_URL}page/{page + 1}/"

    def _has_next_page(self, soup, page):
        nav = soup.find("nav", class_="pagination-igu")
        if not nav:
            return False
        # Primary: "Última" link tells us the last HTML page number
        for a in nav.find_all("a", href=True):
            txt = a.get_text(strip=True)
            if "ltima" in txt:  # handles accented "Última"
                m = re.search(r"/page/(\d+)/?", a["href"])
                if m:
                    last_html = int(m.group(1))
                    current_html = page + 1  # page=0 → HTML page 1
                    return current_html < last_html
        # Fallback: "Siguiente" chevron link is non-disabled
        siguiente = nav.find("img", alt="Siguiente")
        if siguiente:
            parent_li = siguiente.find_parent("li")
            if parent_li and "disabled" not in (parent_li.get("class") or []):
                return True
        # Fallback 2: explicit next-page number link exists
        next_html = page + 2
        for a in nav.find_all("a", href=True):
            if re.search(rf"/page/{next_html}/?", a["href"]):
                return True
        return False

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @classmethod
    def _parse_es_date(cls, text):
        """Extract a Spanish 'DD de <mes> de YYYY' date from text.

        Returns (raw_match_str, iso_date_str).  Both empty if not found.
        """
        if not text:
            return "", ""
        m = re.search(
            r"\b(\d{1,2})\s+de\s+"
            r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
            r"septiembre|octubre|noviembre|diciembre)\s+de\s+"
            r"((?:19|20)\d{2})\b",
            text,
            flags=re.I,
        )
        if not m:
            return "", ""
        day = int(m.group(1))
        month = cls.MONTHS_ES.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        if not month:
            return m.group(0), ""
        return m.group(0), f"{year:04d}-{month:02d}-{day:02d}"

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _node_text(cls, node):
        if node is None:
            return ""
        return cls._one_line(node.get_text(" ", strip=True))

    @classmethod
    def _clean_title(cls, title):
        title = cls._one_line(title)
        title = re.sub(r"\s*[-|–]\s*Ministerio de Igualdad.*$", "", title, flags=re.I)
        return title.strip()

    @staticmethod
    def _meta_content(soup, *keys):
        for key in keys:
            for attr in ("name", "property"):
                node = soup.find("meta", attrs={attr: key})
                if node and node.get("content"):
                    return node["content"].strip()
        return ""

    @staticmethod
    def _link_href(soup, selector):
        node = soup.select_one(selector)
        if node and node.get("href"):
            return node["href"].strip()
        return ""
