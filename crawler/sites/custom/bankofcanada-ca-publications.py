# -*- coding: utf-8 -*-
"""Crawler for Bank of Canada consumer expectations publications."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BankOfCanadaCaPublicationsCrawler(BaseCrawler):
    site_id = "bankofcanada-ca-publications"
    site_name = "Custom: bankofcanada-ca-publications"
    base_url = "https://www.bankofcanada.ca"

    START_URL = (
        "https://www.bankofcanada.ca/publications/"
        "canadian-survey-of-consumer-expectations/"
    )
    BROWSE_FALLBACK_URL = (
        "https://www.bankofcanada.ca/publications/browse/"
        "?content_type%5B%5D=21396&content_type%5B%5D=21396"
    )
    AJAX_FALLBACK_URL = (
        "https://www.bankofcanada.ca/wp-content/plugins/frontend-ajax.php"
    )
    WIDGET_FALLBACK_ID = "multitaxonomytaglist-cfct-module-c5d00e20fb0a4da3ad700b3e64b366e3"
    PAGE_ID_FALLBACK = "169645"
    CONTENT_TYPE_FALLBACK = ["21396", "21396"]

    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50
    CATEGORY = "Canadian Survey of Consumer Expectations"

    def crawl(self, limit=None):
        saved = 0

        start_raw = self._curl_get(self.START_URL, context="start page")
        if not start_raw:
            print(f"[{self.site_id}] start page fetch failed")
            return saved

        start_soup = self._make_soup(start_raw)
        if start_soup is None:
            print(f"[{self.site_id}] start page could not be parsed")
            return saved

        browse_url = self._discover_browse_url(start_soup) or self.BROWSE_FALLBACK_URL
        browse_raw = self._curl_get(browse_url, context="browse page")
        if not browse_raw:
            print(f"[{self.site_id}] browse page fetch failed: {browse_url}")
            return saved

        browse_soup = self._make_soup(browse_raw)
        if browse_soup is None:
            print(f"[{self.site_id}] browse page could not be parsed")
            return saved

        list_api = self._discover_list_api(browse_soup, browse_url)
        print(
            f"[{self.site_id}] list endpoint: {list_api['ajax_url']} "
            f"widget={list_api['widgetid']} pageid={list_api['pageid']}"
        )

        seen_urls = set()
        item_number = 0
        page = 1

        while True:
            if limit is not None and saved >= limit:
                break

            page_raw = self._fetch_list_page(list_api, page)
            if page_raw is None:
                if page == 1:
                    page_raw = browse_raw
                else:
                    print(f"[{self.site_id}] list page {page} failed; stopping")
                    break

            page_soup = self._make_soup(page_raw)
            if page_soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list_records(page_soup, list_api, page)
            if not records:
                print(f"[{self.site_id}] no records on list page {page}; stopping")
                break

            total = self._clean_text(page_soup.select_one(".num-results"))
            total_msg = f" of {total}" if total else ""
            print(
                f"[{self.site_id}] list page {page}: "
                f"discovered {len(records)} records{total_msg}"
            )

            for record in records:
                if limit is not None and saved >= limit:
                    break

                item_number += 1
                try:
                    url = record.get("url") or ""
                    if not url:
                        raise RuntimeError("list record has no detail URL")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    time.sleep(self._delay)
                    detail_raw = self._curl_get(url, context=f"item {item_number} detail")
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    detail_soup = self._make_soup(detail_raw)
                    if detail_soup is None:
                        raise RuntimeError("detail HTML could not be parsed")

                    parsed = self._parse_detail(detail_soup, record)
                    abstract = parsed["abstract"]
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_number} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": parsed["external_id"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": abstract,
                        "category": parsed["category"],
                        "keywords": json.dumps(parsed["keywords"], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": parsed["url"],
                        "pdf_url": parsed["pdf_url"],
                        "doi": parsed["doi"],
                        "department": parsed["department"],
                        "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
                except Exception as exc:
                    print(f"[bankofcanada-ca-publications] item {item_number} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(page_soup, page):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _fetch_list_page(self, list_api, page):
        params = {
            "action": list_api["action"],
            "pageid": list_api["pageid"],
            "widgetid": list_api["widgetid"],
            "mt_page": str(page),
        }
        for idx, value in enumerate(list_api["content_type"]):
            params[f"content_type[{idx}]"] = value

        url = f"{list_api['ajax_url']}?{urlencode(params)}"
        data = self._curl_json(
            url,
            context=f"list page {page}",
            referer=list_api.get("referer") or self.START_URL,
        )
        if not data:
            return None
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            print(f"[{self.site_id}] list page {page} JSON has no content")
            return None
        return content

    def _discover_browse_url(self, soup):
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = self._clean_text(link)
            if "content_type" in href and "21396" in href:
                return urljoin(self.START_URL, href)
            if text.lower() == "see more" and "/publications/browse/" in href:
                return urljoin(self.START_URL, href)
        return ""

    def _discover_list_api(self, soup, browse_url):
        pageid = self._page_id(soup) or self.PAGE_ID_FALLBACK
        widgetid = ""

        form = soup.find("form", attrs={"data-mtw": True})
        if form:
            widgetid = self._clean_text(form.get("data-mtw") or "")

        archive = None
        if widgetid:
            archive = soup.find(id=widgetid)
        if archive is None:
            archive = soup.select_one(".mtw.archive.paging-paged[data-ajax-url]")
        if archive is not None:
            widgetid = archive.get("id") or widgetid

        ajax_path = ""
        if archive is not None:
            ajax_path = archive.get("data-ajax-url") or ""
        ajax_url = urljoin(self.base_url + "/", ajax_path) if ajax_path else self.AJAX_FALLBACK_URL

        values = []
        if archive is not None:
            for hidden in archive.select('input[type="hidden"][name^="content_type"]'):
                value = self._clean_text(hidden.get("value") or "")
                if value:
                    values.append(value)
        if not values:
            parsed = urlparse(browse_url)
            for key, value in parse_qsl(parsed.query, keep_blank_values=False):
                if key.startswith("content_type") and value:
                    values.append(value)
        if not values:
            values = list(self.CONTENT_TYPE_FALLBACK)

        return {
            "ajax_url": ajax_url,
            "action": "BoCUtil_Widget_AjaxGetWidget",
            "pageid": pageid,
            "widgetid": widgetid or self.WIDGET_FALLBACK_ID,
            "content_type": values,
            "referer": browse_url,
        }

    def _parse_list_records(self, soup, list_api, page):
        records = []
        for article in soup.select("article.media"):
            link = article.select_one(".media-heading a[href]") or article.find("a", href=True)
            if link is None:
                continue

            title = self._clean_text(link)
            url = urljoin(self.base_url + "/", link.get("href", "").strip())
            if not title or not url:
                continue

            post_id = self._post_id(article.get("id") or "")
            date_raw = self._clean_text(article.select_one(".media-date"))
            excerpt = self._clean_text(article.select_one(".media-excerpt"))
            category = self._clean_text(link.get("data-content-type") or "") or self.CATEGORY
            tags = []
            for tag_link in article.select(".media-tags a"):
                tag_text = self._clean_text(tag_link)
                if tag_text:
                    tags.append(tag_text)

            records.append({
                "post_id": post_id,
                "title": title,
                "url": url,
                "published_date": self._normalize_date(date_raw),
                "date_raw": date_raw,
                "list_excerpt": excerpt,
                "category": category,
                "tags": self._dedupe(tags),
                "list_page": page,
                "list_endpoint": list_api["ajax_url"],
                "widgetid": list_api["widgetid"],
                "pageid": list_api["pageid"],
            })
        return records

    def _parse_detail(self, soup, record):
        main = soup.find("main") or soup
        self._remove_noise(main)

        canonical = self._canonical_url(soup)
        detail_url = canonical or record["url"]
        title = (
            self._meta_content(soup, "og:title")
            or self._meta_content(soup, "twitter:title")
            or self._clean_text(main.find("h1"))
            or record["title"]
        )
        title = self._site_title_cleanup(title)
        if not title:
            raise RuntimeError("detail page has no title")

        post_id = record.get("post_id") or self._post_id_from_detail(soup)
        external_id = post_id or self._external_id(detail_url)
        published_date = (
            self._normalize_date(self._meta_content(soup, "publication_date"))
            or record.get("published_date")
            or self._date_from_url(detail_url)
        )
        modified_date = self._normalize_date(self._meta_content(soup, "last_modified_date"))

        meta_description = (
            self._meta_content(soup, "description")
            or self._meta_content(soup, "og:description")
            or self._meta_content(soup, "twitter:description")
        )
        body_summary, body_source = self._extract_body_summary(main)
        abstract = self._join_limited(
            self._dedupe([meta_description, record.get("list_excerpt"), body_summary]),
            limit=2400,
        )

        data_links = self._data_links(main, detail_url)
        pdf_url = self._first_pdf_url(main, detail_url)
        keywords = self._dedupe(
            record.get("tags", [])
            + [
                self.CATEGORY,
                "consumer expectations",
                "inflation expectations",
                "household finances",
                "labour market",
            ]
        )

        metadata = {
            "source_format": "ajax-json-list+html-detail",
            "start_url": self.START_URL,
            "browse_url": record.get("list_referer") or self.BROWSE_FALLBACK_URL,
            "list_endpoint": record.get("list_endpoint"),
            "detail_endpoint": record.get("url"),
            "canonical_url": canonical,
            "post_id": post_id,
            "list_page": record.get("list_page"),
            "widgetid": record.get("widgetid"),
            "pageid": record.get("pageid"),
            "date_raw": record.get("date_raw"),
            "modified_date": modified_date,
            "abstract_source": body_source,
            "list_excerpt": record.get("list_excerpt"),
            "data_links": data_links,
        }

        return {
            "external_id": external_id,
            "title": title,
            "authors": ["Bank of Canada"],
            "abstract": abstract,
            "category": record.get("category") or self.CATEGORY,
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "Bank of Canada",
            "metadata": metadata,
        }

    def _curl_json(self, url, context="request", referer=None):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/javascript,*/*;q=0.8",
            referer=referer,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] {context} invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] {context} JSON root is not an object")
            return None
        return data

    def _curl_get(self, url, context="request", accept=None, referer=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            "45",
            "--connect-timeout",
            "15",
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept or 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'}",
            "-H",
            "Accept-Language: en-CA,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                stdout = result.stdout or b""
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                last_error = stderr or f"curl exit {result.returncode}; empty response"
            except subprocess.TimeoutExpired as exc:
                last_error = f"curl timeout after {exc.timeout}s"
            except Exception as exc:
                last_error = str(exc)

            wait = self.BACKOFF_SECONDS[attempt]
            print(
                f"[{self.site_id}] {context} curl failed "
                f"(attempt {attempt + 1}/3): {last_error}"
            )
            if attempt < 2:
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{self.site_id}] all BeautifulSoup parsers failed: {last_exc}")
        return None

    def _extract_body_summary(self, main):
        parts = []
        source = "detail body"
        for node in main.select(".cfct-mod-content p, .cfct-mod-content li, .post-content p"):
            text = self._clean_text(node)
            if not self._is_useful_abstract_part(text):
                continue
            parts.append(text)
            if len(" ".join(parts)) >= 1600:
                break
        return self._join_limited(self._dedupe(parts), limit=1800), source

    def _is_useful_abstract_part(self, text):
        if len(text) < 35:
            return False
        lowered = text.lower()
        skip_phrases = (
            "skip to content",
            "share this page",
            "data available as",
            "a modern browser with javascript enabled",
            "alternatively, the data is available",
            "enter page",
            "go to page",
            "on this page",
            "date modified",
            "subscribe to",
            "receive notification by email",
        )
        return not any(phrase in lowered for phrase in skip_phrases)

    def _remove_noise(self, root):
        for tag in root.find_all([
            "script",
            "style",
            "noscript",
            "svg",
            "img",
            "picture",
            "form",
            "nav",
            "footer",
            "header",
            "button",
            "select",
            "iframe",
        ]):
            tag.decompose()
        for selector in (
            ".bocss-share",
            ".bocss-toc",
            ".bocss-main-pagination",
            ".hidden-print",
            ".visible-print",
            ".sr-only",
        ):
            for tag in root.select(selector):
                tag.decompose()

    def _has_next_page(self, soup, current_page):
        if soup.select_one("a.next.page-numbers"):
            return True
        for link in soup.select(".pagination a[href]"):
            match = re.search(r"mt_page=([0-9]+)", link.get("href", ""))
            if match and int(match.group(1)) > current_page:
                return True
        return False

    def _data_links(self, root, base):
        links = []
        seen = set()
        for link in root.find_all("a", href=True):
            href = link.get("href", "").strip()
            if "/valet/observations/group/" not in href:
                continue
            url = urljoin(base, href)
            if url in seen:
                continue
            seen.add(url)
            fmt_match = re.search(r"/(csv|json|xml)(?:\?|$)", url, flags=re.IGNORECASE)
            links.append({
                "label": self._clean_text(link),
                "url": url,
                "format": fmt_match.group(1).lower() if fmt_match else "",
            })
        return links

    def _first_pdf_url(self, root, base):
        for link in root.find_all("a", href=True):
            href = link.get("href", "").strip()
            if ".pdf" in href.lower():
                return urljoin(base, href)
        return ""

    def _canonical_url(self, soup):
        link = soup.find("link", rel=lambda value: value and "canonical" in value)
        if link and link.get("href"):
            return urljoin(self.base_url + "/", link.get("href", "").strip())
        meta_url = self._meta_content(soup, "og:url")
        if meta_url:
            return urljoin(self.base_url + "/", meta_url)
        return ""

    def _meta_content(self, soup, name):
        for attrs in ({"name": name}, {"property": name}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                return self._clean_text(tag["content"])
        return ""

    def _page_id(self, soup):
        body = soup.find("body")
        classes = body.get("class", []) if body else []
        for cls in classes:
            match = re.search(r"(?:postid|page-id)-([0-9]+)", str(cls))
            if match:
                return match.group(1)
        return ""

    def _post_id_from_detail(self, soup):
        body_id = self._page_id(soup)
        if body_id:
            return body_id
        shortlink = soup.find("link", rel=lambda value: value and "shortlink" in value)
        if shortlink and shortlink.get("href"):
            match = re.search(r"[?&]p=([0-9]+)", shortlink.get("href", ""))
            if match:
                return match.group(1)
        return ""

    def _post_id(self, value):
        match = re.search(r"post-([0-9]+)", value or "")
        return match.group(1) if match else ""

    def _external_id(self, value):
        text = self._clean_text(value)
        parsed_path = urlparse(text).path.strip("/")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", parsed_path or text).strip("-").lower()
        if slug:
            return slug[:180]
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        return f"boc-{digest}"

    def _date_from_url(self, url):
        match = re.search(r"/([0-9]{4})/([0-9]{2})/", url or "")
        if match:
            year, month = match.groups()
            return f"{year}-{month}-01"
        return ""

    def _normalize_date(self, raw):
        text = self._clean_text(raw)
        if not text:
            return ""
        match = re.search(r"([0-9]{4})[-/]([0-9]{1,2})[-/]([0-9]{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        match = re.search(r"([A-Za-z]+)\s+([0-9]{1,2}),?\s+([0-9]{4})", text)
        if match:
            month_name, day, year = match.groups()
            for fmt in ("%B", "%b"):
                try:
                    month = datetime.strptime(month_name[:3], "%b").month if fmt == "%b" else datetime.strptime(month_name, fmt).month
                    return f"{year}-{month:02d}-{int(day):02d}"
                except ValueError:
                    continue
        return ""

    def _site_title_cleanup(self, value):
        text = self._clean_text(value)
        return re.sub(r"\s+-\s+Bank of Canada$", "", text).strip()

    def _dedupe(self, values):
        output = []
        seen = set()
        for value in values:
            text = self._clean_text(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(text)
        return output

    def _join_limited(self, parts, limit=1800):
        text = self._normalize_text(" ".join(part for part in parts if part))
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:")
        return f"{cut}."

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value)).replace("\xa0", " ")
        text = text.replace("\u200b", "").replace("\ufeff", "")
        return self._normalize_text(text)

    def _normalize_text(self, text):
        return re.sub(r"\s+", " ", str(text)).strip()
