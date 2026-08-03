# -*- coding: utf-8 -*-
"""Crawler for mod.mil.gr Ανακοινώσεις Τύπου.

The WordPress REST endpoint is blocked/disabled for this site in practice
(browser-header curl receives the site's 400/404 error page), while the
category and detail HTML endpoints are stable:

  list:   /category/anakoinoseis-typoy/page/N/
  detail: post permalinks listed in article.elementor-post
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_GREEK_MONTHS = {
    "ιανουαριου": 1,
    "ιανουαριος": 1,
    "φεβρουαριου": 2,
    "φεβρουαριος": 2,
    "μαρτιου": 3,
    "μαρτιος": 3,
    "απριλιου": 4,
    "απριλιος": 4,
    "μαιου": 5,
    "μαιος": 5,
    "ιουνιου": 6,
    "ιουνιος": 6,
    "ιουλιου": 7,
    "ιουλιος": 7,
    "αυγουστου": 8,
    "αυγουστος": 8,
    "σεπτεμβριου": 9,
    "σεπτεμβριος": 9,
    "οκτωβριου": 10,
    "οκτωβριος": 10,
    "νοεμβριου": 11,
    "νοεμβριος": 11,
    "δεκεμβριου": 12,
    "δεκεμβριος": 12,
}


class ModMilGrCategoryCrawler(BaseCrawler):
    site_id = "mod-mil-gr-category"
    site_name = "Custom: mod-mil-gr-category"
    base_url = "https://www.mod.mil.gr"

    START_URL = "https://www.mod.mil.gr/category/anakoinoseis-typoy/"
    CATEGORY = "Ανακοινώσεις Τύπου"
    PUBLISHER = "Υπουργείο Εθνικής Άμυνας; Ελληνική Δημοκρατία"

    MAX_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    WALL_GRACE_SECONDS = 30
    CURL_TIMEOUT = 45
    BACKOFF = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    MIN_SAVED_ABSTRACT_CHARS = 100

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    _CURL_HEADERS = (
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language: el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control: no-cache",
        "Upgrade-Insecure-Requests: 1",
    )

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        started_at = time.monotonic()
        limit_label = str(limit) if limit is not None else "inf"

        for page in range(1, self.MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started_at
            if elapsed >= self.MAX_WALL_SECONDS - self.WALL_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute budget; exiting cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, referer=self.START_URL if page > 1 else None, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list parse failed at page {page}; stopping")
                break

            records = self._parse_list_page(soup)
            if not records:
                print(f"[{self.site_id}] page {page}: no records found; end of list")
                break

            new_this_page = 0
            for record in records:
                if limit is not None and saved >= limit:
                    break

                url = record.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_this_page += 1

                try:
                    saved += self._process_record(record, list_url)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

                time.sleep(self.detail_delay)

            if new_this_page == 0:
                print(f"[{self.site_id}] page {page}: no new records; stopping")
                break

            if not self._has_next_page(soup, page):
                print(f"[{self.site_id}] no next page after page {page}; stopping")
                break

        else:
            print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")

        print(f"[{self.site_id}] crawl complete: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # List and detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, page):
        if page <= 1:
            return self.START_URL
        return f"{self.START_URL}page/{page}/"

    def _parse_list_page(self, soup):
        records = []
        for article in soup.select("article.elementor-post"):
            title_link = article.select_one(".elementor-post__title a[href]")
            if not title_link:
                continue

            href = (title_link.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)

            classes = article.get("class", [])
            post_id = self._post_id_from_classes(classes)
            title = self._one_line(title_link.get_text(" ", strip=True))

            date_el = article.select_one(".elementor-post-date")
            listed_date_raw = self._one_line(date_el.get_text(" ", strip=True) if date_el else "")
            listed_date = self._parse_date(listed_date_raw)

            excerpt_el = article.select_one(".elementor-post__excerpt")
            excerpt = self._one_line(excerpt_el.get_text(" ", strip=True) if excerpt_el else "")

            img = article.select_one(".elementor-post__thumbnail img")
            image_url = None
            if img and img.get("src"):
                image_url = urljoin(self.base_url, img.get("src"))

            records.append({
                "post_id": post_id,
                "post_number": post_id,
                "url": url,
                "title": title,
                "listed_date_raw": listed_date_raw,
                "listed_date": listed_date,
                "excerpt": excerpt,
                "category_classes": [c for c in classes if str(c).startswith("category-")],
                "image_url": image_url,
            })
        return records

    def _process_record(self, record, list_url):
        url = record["url"]
        raw = self._curl_get(url, referer=list_url, context=f"detail {url}")
        if not raw:
            print(f"[{self.site_id}] detail fetch failed for {url}; skipping")
            return 0

        soup = self._make_soup(raw, context=f"detail {url}")
        if soup is None:
            print(f"[{self.site_id}] detail parse failed for {url}; skipping")
            return 0

        parsed = self._parse_detail(soup, record)
        abstract = parsed["abstract"]
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract too short ({len(abstract)} chars)")
            return 0
        if len(abstract) < self.MIN_SAVED_ABSTRACT_CHARS:
            print(f"[{self.site_id}] item {url} skipped: abstract below save threshold ({len(abstract)} chars)")
            return 0

        metadata = {
            "posted_date": record.get("listed_date_raw"),
            "listed_date": record.get("listed_date"),
            "originalFilename": parsed.get("original_filename"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "post_id": parsed.get("post_id"),
            "node_id": parsed.get("post_id"),
            "post_number": parsed.get("post_id"),
            "slug": self._slug_from_url(url),
            "category_classes": record.get("category_classes") or [],
            "listed_excerpt": record.get("excerpt"),
            "listed_image_url": record.get("image_url"),
            "detail_image_url": parsed.get("image_url"),
            "detail_date_raw": parsed.get("detail_date_raw"),
            "datePublished": parsed.get("date_published_raw"),
            "dateModified": parsed.get("date_modified_raw"),
            "author_raw": parsed.get("author_raw"),
            "og_url": parsed.get("og_url"),
            "og_description": parsed.get("og_description"),
            "source": "mod.mil.gr category/detail HTML",
            "list_endpoint": self.START_URL,
            "detail_endpoint": "WordPress post permalink HTML",
            "wp_rest_status": "tested separately; site returned 400/404 error page",
        }

        post_id = parsed.get("post_id") or record.get("post_id") or self._slug_from_url(url)
        published_date = parsed.get("published_date") or record.get("listed_date")
        authors = parsed.get("author_raw")

        self._save_paper({
            "id": None,
            "site_id": self.site_id,
            "external_id": str(post_id) if post_id else self._slug_from_url(url),
            "post_number": str(post_id) if post_id else self._slug_from_url(url),
            "title": parsed["title"],
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": record.get("listed_date"),
            "posted_date": record.get("listed_date"),
            "authors": authors,
            "publisher": self.PUBLISHER,
            "department": parsed.get("department"),
            "journal": None,
            "url": url,
            "pdf_url": parsed.get("pdf_url"),
            "keywords": parsed.get("keywords"),
            "category": self.CATEGORY,
            "doi": None,
            "original_filename": parsed.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        })
        print(f"[{self.site_id}] saved {parsed['title'][:80]}")
        return 1

    def _parse_detail(self, soup, record):
        single = soup.select_one(".elementor-location-single")

        title = ""
        if single:
            h1 = single.select_one(".elementor-widget-theme-post-title h1")
            if h1:
                title = self._one_line(h1.get_text(" ", strip=True))
        if not title:
            title = self._clean_title(self._meta(soup, "og:title") or record.get("title") or "")
        if not title:
            title = record.get("title") or "(untitled)"

        detail_date_raw = ""
        if single:
            h1_widget = single.select_one(".elementor-widget-theme-post-title")
            if h1_widget:
                date_widget = self._next_heading_widget_text(h1_widget)
                if date_widget:
                    detail_date_raw = date_widget

        schema = self._rank_math_schema(soup)
        date_published_raw = self._schema_value(schema, "datePublished")
        date_modified_raw = self._schema_value(schema, "dateModified")
        author_raw = self._schema_author(schema)
        description = self._schema_value(schema, "description") or self._meta(soup, "og:description") or ""

        published_date = (
            self._iso_date(date_published_raw)
            or self._parse_date(detail_date_raw)
            or record.get("listed_date")
        )

        post_id = record.get("post_id") or self._post_id_from_detail(soup)
        content = soup.select_one(".elementor-widget-theme-post-content")
        abstract = self._build_abstract(content, description, record.get("excerpt"))

        pdf_url, original_filename = self._find_pdf(content)
        if pdf_url and not original_filename:
            headers = self._curl_head(pdf_url, referer=record.get("url"), context=f"head {pdf_url}")
            original_filename = self._filename_from_headers(headers) or self._filename_from_url(pdf_url)

        image_url = self._meta(soup, "og:image")
        if image_url:
            image_url = urljoin(self.base_url, image_url)

        keywords = self._keywords_from_classes(record.get("category_classes") or [])
        department = self._department_from_classes(record.get("category_classes") or [])

        return {
            "post_id": post_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "date_published_raw": date_published_raw,
            "date_modified_raw": date_modified_raw,
            "detail_date_raw": detail_date_raw,
            "author_raw": author_raw,
            "department": department,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "image_url": image_url,
            "og_url": self._meta(soup, "og:url"),
            "og_description": self._meta(soup, "og:description"),
        }

    def _build_abstract(self, content, schema_description, list_excerpt):
        parts = []
        if content is not None:
            frag = self._make_soup(str(content), context="post content fragment")
            if frag is not None:
                for bad in frag.select("script, style, img, figure, .gallery, .post-gallery"):
                    bad.decompose()
                for node in frag.find_all(["p", "li", "blockquote", "h2", "h3", "h4"]):
                    text = self._one_line(node.get_text(" ", strip=True))
                    if text and text not in parts:
                        parts.append(text)
        text = "\n\n".join(parts)
        text = self._normalize_text(text)
        if len(text) >= self.MIN_SAVED_ABSTRACT_CHARS:
            return text[:5000]

        fallback = self._normalize_text(schema_description or list_excerpt or "")
        if len(fallback) > len(text):
            return fallback[:5000]
        return text[:5000]

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, context="request"):
        return self._curl(url, method="GET", referer=referer, context=context)

    def _curl_head(self, url, referer=None, context="request"):
        return self._curl(url, method="HEAD", referer=referer, context=context, want_headers=True)

    def _curl(self, url, method="GET", referer=None, context="request", want_headers=False):
        headers = []
        for header in self._CURL_HEADERS:
            headers.extend(["-H", header])
        if referer:
            headers.extend(["-H", f"Referer: {referer}"])

        marker = b"\n__MOD_HTTP_STATUS__:"
        last_error = ""
        for attempt, wait in enumerate(self.BACKOFF, start=1):
            cmd = [
                "curl",
                "--tls-max",
                "1.3",
                "-skL",
                "--compressed",
                "--max-time",
                str(self.CURL_TIMEOUT),
                "-A",
                self._USER_AGENT,
            ]
            if method == "HEAD":
                cmd.append("-I")
            cmd.extend(headers)
            cmd.extend(["-w", "\n__MOD_HTTP_STATUS__:%{http_code}", url])

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                output = result.stdout or b""
                body, status = self._split_curl_status(output, marker)
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()

                if result.returncode == 0 and status and 200 <= status < 400 and body:
                    if want_headers:
                        return body.decode("utf-8", errors="replace")
                    return body.decode("utf-8", errors="replace")

                last_error = f"curl exit={result.returncode} status={status} stderr={stderr[:200]}"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_error = str(exc)

            print(f"[{self.site_id}] {context} fetch attempt {attempt}/3 failed: {last_error}")
            if attempt < len(self.BACKOFF):
                time.sleep(wait)

        print(f"[{self.site_id}] {context} fetch failed after 3 attempts for {url}: {last_error}")
        return None

    def _split_curl_status(self, output, marker):
        if marker not in output:
            return output, None
        body, status_bytes = output.rsplit(marker, 1)
        status_text = status_bytes.strip().decode("ascii", errors="ignore")
        try:
            return body, int(status_text)
        except ValueError:
            return body, None

    # ------------------------------------------------------------------
    # Generic extraction helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    def _has_next_page(self, soup, page):
        next_link = soup.select_one('link[rel="next"]')
        if next_link and next_link.get("href"):
            return True

        target = str(page + 1)
        for a_tag in soup.select("a.page-numbers[href]"):
            text = self._one_line(a_tag.get_text(" ", strip=True))
            href = a_tag.get("href") or ""
            if text.endswith(target) or f"/page/{page + 1}/" in href:
                return True
        return False

    def _post_id_from_classes(self, classes):
        for cls in classes or []:
            match = re.match(r"post-(\d+)$", str(cls))
            if match:
                return match.group(1)
        return None

    def _post_id_from_detail(self, soup):
        single = soup.select_one(".elementor-location-single")
        if single:
            post_id = self._post_id_from_classes(single.get("class", []))
            if post_id:
                return post_id
        match = re.search(r"\bpost-(\d+)\b", str(soup)[:200000])
        return match.group(1) if match else None

    def _next_heading_widget_text(self, widget):
        parent = widget.parent
        if parent is None:
            return ""
        seen_self = False
        for child in parent.children:
            if child is widget:
                seen_self = True
                continue
            if not seen_self or not getattr(child, "select_one", None):
                continue
            if "elementor-widget-heading" in child.get("class", []):
                heading = child.select_one(".elementor-heading-title")
                if heading:
                    text = self._one_line(heading.get_text(" ", strip=True))
                    if self._parse_date(text):
                        return text
        return ""

    def _rank_math_schema(self, soup):
        for script in soup.select('script[type="application/ld+json"]'):
            text = script.string or script.get_text()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("@graph"):
                return data
        return {}

    def _schema_nodes(self, schema):
        graph = schema.get("@graph") if isinstance(schema, dict) else None
        return graph if isinstance(graph, list) else []

    def _schema_value(self, schema, key):
        for preferred_type in ("BlogPosting", "WebPage"):
            for node in self._schema_nodes(schema):
                node_type = node.get("@type")
                if node_type == preferred_type or (
                    isinstance(node_type, list) and preferred_type in node_type
                ):
                    value = node.get(key)
                    if value:
                        return value
        return None

    def _schema_author(self, schema):
        for node in self._schema_nodes(schema):
            node_type = node.get("@type")
            if node_type != "BlogPosting" and not (
                isinstance(node_type, list) and "BlogPosting" in node_type
            ):
                continue
            author = node.get("author")
            if isinstance(author, dict):
                return author.get("name") or author.get("@id")
            if isinstance(author, str):
                return author
        return None

    def _meta(self, soup, key):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return self._one_line(tag.get("content"))
        return None

    def _find_pdf(self, content):
        if content is None:
            return None, None
        for a_tag in content.select("a[href]"):
            href = a_tag.get("href") or ""
            if not re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                continue
            pdf_url = urljoin(self.base_url, href)
            return pdf_url, self._filename_from_url(pdf_url)
        return None, None

    def _filename_from_headers(self, headers):
        if not headers:
            return None
        msg = Message()
        for line in headers.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            msg[key.strip()] = value.strip()
        content_disposition = msg.get("Content-Disposition")
        if not content_disposition:
            return None
        match = re.search(r'filename\*=[^=]*?utf-8[\'"]*([^;\r\n]+)', content_disposition, re.I)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename=["\']?([^"\';\r\n]+)', content_disposition, re.I)
        if match:
            return unquote(match.group(1).strip())
        return None

    def _filename_from_url(self, url):
        if not url:
            return None
        path = urlparse(url).path
        tail = path.rstrip("/").split("/")[-1]
        if not tail:
            return None
        tail = unquote(tail)
        return tail if "." in tail and len(tail) <= 220 else None

    def _slug_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        return path.split("/")[-1] if path else None

    def _keywords_from_classes(self, classes):
        labels = []
        for cls in classes:
            cls = str(cls)
            if cls == "category-anakoinoseis-typoy":
                labels.append(self.CATEGORY)
            elif cls == "category-yetha":
                labels.append("ΥΕΘΑ")
            elif cls == "category-yfetha":
                labels.append("ΥΦΕΘΑ")
            elif cls == "category-teleytaia-nea":
                labels.append("Τελευταία Νέα")
        return ", ".join(dict.fromkeys(labels)) if labels else self.CATEGORY

    def _department_from_classes(self, classes):
        if "category-yfetha" in classes:
            return "ΥΦΕΘΑ"
        if "category-yetha" in classes:
            return "ΥΕΘΑ"
        return None

    def _clean_title(self, title):
        title = self._one_line(title)
        return re.sub(r"\s*[-–|]\s*Ελληνική Δημοκρατία.*$", "", title).strip()

    def _parse_date(self, text):
        if not text:
            return None
        iso = self._iso_date(text)
        if iso:
            return iso
        match = re.search(r"(\d{1,2})\s+([^\s,]+),?\s+(\d{4})", text, re.UNICODE)
        if not match:
            return None
        day = int(match.group(1))
        month_key = self._normalize_greek_token(match.group(2))
        month = _GREEK_MONTHS.get(month_key)
        if not month:
            return None
        year = int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    def _iso_date(self, text):
        if not text:
            return None
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(text))
        if not match:
            return None
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    def _normalize_greek_token(self, text):
        text = (text or "").strip().lower().replace("ς", "σ")
        decomposed = unicodedata.normalize("NFD", text)
        return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")

    def _one_line(self, text):
        return re.sub(r"\s+", " ", text or "").strip()

    def _normalize_text(self, text):
        text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()
