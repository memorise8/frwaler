# -*- coding: utf-8 -*-
"""Crawler for Canada West Foundation publications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class CwfCaPublicationsCrawler(BaseCrawler):
    site_id = "cwf-ca-publications"
    site_name = "Custom: cwf-ca-publications"
    base_url = "https://cwf.ca"

    START_URL = (
        "https://cwf.ca/publications/"
        "#series=reports@what-now-policy-briefs@partner-publications@what-the-west"
        "@fiscal-federalism-policy-network@50th-anniversary"
    )
    AJAX_URL = "https://cwf.ca/wp-admin/admin-ajax.php"
    SERIES_FILTER = (
        "reports@what-now-policy-briefs@partner-publications@what-the-west"
        "@fiscal-federalism-policy-network@50th-anniversary"
    )
    SERIES_LABELS = {
        "reports": "Reports",
        "what-now-policy-briefs": "What Now? Policy Briefs",
        "partner-publications": "Partner Publications",
        "what-the-west": "What the West?",
        "fiscal-federalism-policy-network": "Fiscal Federalism Policy Network (FFPN)",
        "50th-anniversary": "50th Anniversary",
    }

    BACKOFF_SECONDS = (1, 3, 9)
    SAFETY_CAP_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        started_at = time.monotonic()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while page <= self.SAFETY_CAP_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._wall_budget_nearly_spent(started_at):
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_data = self._fetch_list_page(page)
            if not list_data:
                print(f"[{self.site_id}] page {page}: empty or invalid list response")
                break

            items = self._parse_list_items(list_data, page)
            if not items:
                print(f"[{self.site_id}] page {page}: no list items; stopping")
                break

            new_urls_on_page = 0
            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                if self._wall_budget_nearly_spent(started_at):
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                    return saved

                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_urls_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(item_url, f"detail page {item_url}")
                    if not detail_html:
                        raise RuntimeError("empty detail page response")

                    paper = self._parse_detail_page(detail_html, item_url, item)
                    if not paper:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        title = paper.get("title") or item_url
                        print(
                            f"[{self.site_id}] skipping '{title[:80]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page}.{idx} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: all item URLs already seen; stopping")
                break

            has_next = self._has_next_page(list_data, page)
            if not has_next:
                break
            if page >= self.SAFETY_CAP_PAGES:
                print(f"[{self.site_id}] safety cap of {self.SAFETY_CAP_PAGES} pages reached")
                break
            page += 1

        if page > self.SAFETY_CAP_PAGES:
            print(f"[{self.site_id}] safety cap of {self.SAFETY_CAP_PAGES} pages reached")
        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _fetch_list_page(self, page):
        payload = {
            "action": "publication_filter",
            "series_term_id": self.SERIES_FILTER,
            "centre_term_id": "",
            "topic_term_id": "",
            "page": str(page),
        }
        raw = self._curl_post(self.AJAX_URL, payload, f"list page {page}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] list page {page}: JSON parse failed: {exc}")
            return None
        if not isinstance(data, dict) or data.get("status") is not True:
            return None
        return data

    def _parse_list_items(self, data, page):
        html = data.get("html") or ""
        soup = self._make_soup(html)
        if soup is None:
            return []

        items = []
        seen = set()
        containers = soup.select(".blog-masonry-item")
        if not containers:
            containers = soup.find_all("div")

        for node in containers:
            link = node.select_one(".post-title h2 a[href]")
            if link is None:
                link = node.find("a", href=True)
            if link is None:
                continue

            href = urljoin(self.base_url, link.get("href", "").strip())
            if "/research/publications/" not in href:
                continue
            href = href.split("#", 1)[0]
            if href in seen:
                continue
            seen.add(href)

            title = self._clean_text(link.get_text(" ", strip=True))
            if not title:
                h2 = node.find(["h1", "h2", "h3"])
                title = self._clean_text(h2.get_text(" ", strip=True)) if h2 else ""

            img = node.find("img")
            image_url = urljoin(self.base_url, img.get("src", "")) if img and img.get("src") else None
            items.append({
                "url": href,
                "title": title,
                "list_page": page,
                "image_url": image_url,
            })

        if items:
            return items

        for href in re.findall(r'href=["\']([^"\']*/research/publications/[^"\']+)["\']', html):
            url = urljoin(self.base_url, href).split("#", 1)[0]
            if url not in seen:
                seen.add(url)
                items.append({"url": url, "title": "", "list_page": page, "image_url": None})
        return items

    def _has_next_page(self, data, current_page):
        page_html = data.get("page_container") or ""
        if not page_html.strip():
            return False

        soup = self._make_soup(page_html)
        if soup is None:
            return False

        for li in soup.find_all("li"):
            p_val = li.get("p")
            classes = li.get("class") or []
            if not p_val or "active" not in classes:
                continue
            try:
                if int(p_val) > int(current_page):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _parse_detail_page(self, html, url, list_item):
        soup = self._make_soup(html)
        if soup is None:
            raise RuntimeError("BeautifulSoup could not parse detail HTML")

        metas = self._collect_metas(soup)
        canonical = self._canonical_url(soup) or url
        title = self._extract_title(soup, metas, list_item)
        if not title:
            raise RuntimeError("missing title")

        post_id = self._extract_post_id(soup, html)
        slug = self._slug_from_url(canonical)
        external_id = post_id or slug or self._stable_hash(canonical)
        post_number = post_id if post_id and post_id.isdigit() else (slug or None)

        article_body = soup.select_one(".article-body .wysiwyg") or soup.select_one(".article-body")
        raw_date = self._extract_raw_date(soup, article_body, metas)
        published_date = self._parse_date(raw_date)
        listed_date = published_date

        pdf_urls = self._extract_pdf_urls(article_body or soup)
        pdf_url = pdf_urls[0] if pdf_urls else None
        original_filename = self._filename_from_url(pdf_url)

        authors = self._extract_authors(article_body, title)
        abstract = self._extract_abstract(article_body, metas, title)
        if not abstract or len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._meta_description(metas)

        series_slugs = self.SERIES_FILTER.split("@")
        series_names = [self.SERIES_LABELS.get(slug, slug) for slug in series_slugs]
        keywords = ", ".join(["Canada West Foundation", "publication"] + series_names)

        metadata = {
            "posted_date": raw_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": series_names,
            "series_slugs": series_slugs,
            "volume": None,
            "issue": None,
            "post_id": post_id,
            "node_id": post_id,
            "slug": slug,
            "list_page": list_item.get("list_page"),
            "list_title": list_item.get("title"),
            "list_image_url": list_item.get("image_url"),
            "source_api": self.AJAX_URL,
            "ajax_action": "publication_filter",
            "series_filter": self.SERIES_FILTER,
            "pdf_urls": pdf_urls,
            "og_updated_time": metas.get("og:updated_time"),
            "twitter_description": metas.get("twitter:description"),
        }

        return {
            "id": f"{self.site_id}-{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": "Canada West Foundation",
            "department": "Canada West Foundation",
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": "Publication",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _curl_get(self, url, label):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "45",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        return self._run_curl(cmd, label)

    def _curl_post(self, url, payload, label):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "45",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/javascript, */*; q=0.01",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", "Origin: https://cwf.ca",
            "-H", "Referer: https://cwf.ca/publications/",
        ]
        for key, value in payload.items():
            cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(url)
        return self._run_curl(cmd, label)

    def _run_curl(self, cmd, label):
        last_error = None
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=55)
                body = result.stdout or b""
                if result.returncode == 0 and body:
                    return body.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = stderr or f"curl return code {result.returncode}"
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout: {exc}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < len(self.BACKOFF_SECONDS):
                print(f"[{self.site_id}] {label} failed (attempt {attempt}/3): {last_error}; retrying in {wait}s")
                time.sleep(wait)

        print(f"[{self.site_id}] {label} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, html):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _collect_metas(self, soup):
        metas = {}
        for meta in soup.find_all("meta"):
            key = meta.get("property") or meta.get("name")
            value = meta.get("content")
            if key and value and key not in metas:
                metas[key] = self._clean_text(value)
        return metas

    def _canonical_url(self, soup):
        link = soup.find("link", rel=lambda val: val and "canonical" in val)
        if link and link.get("href"):
            return urljoin(self.base_url, link["href"])
        return None

    def _extract_title(self, soup, metas, list_item):
        header = soup.select_one("header.page-header h1")
        if header:
            title = self._clean_text(header.get_text(" ", strip=True))
            if title:
                return title
        if list_item.get("title"):
            return self._clean_text(list_item["title"])
        raw = metas.get("og:title") or metas.get("twitter:title") or ""
        raw = re.sub(r"\s+Canada West Foundation\s*$", "", raw).strip()
        return self._clean_text(raw)

    def _extract_post_id(self, soup, html):
        body = soup.find("body")
        if body:
            classes = body.get("class") or []
            for cls in classes:
                match = re.match(r"postid-(\d+)", str(cls))
                if match:
                    return match.group(1)

        shortlink = soup.find("link", rel=lambda val: val and "shortlink" in val)
        if shortlink and shortlink.get("href"):
            match = re.search(r"[?&]p=(\d+)", shortlink["href"])
            if match:
                return match.group(1)

        match = re.search(r"\bpostid-(\d+)\b", html)
        return match.group(1) if match else None

    def _extract_raw_date(self, soup, article_body, metas):
        date_node = soup.select_one("header.page-header span.sub")
        if date_node:
            raw = self._clean_text(date_node.get_text(" ", strip=True))
            if raw:
                return raw

        if article_body:
            text = article_body.get_text("\n", strip=True)
            match = re.search(
                r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                r"\s+\d{1,2},\s+\d{4}\b",
                text,
            )
            if match:
                return match.group(0)
            match = re.search(
                r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                r"\s+\d{4}\b",
                text,
            )
            if match:
                return match.group(0)

        return metas.get("article:published_time") or metas.get("og:updated_time") or ""

    def _parse_date(self, raw):
        if not raw:
            return None
        text = self._clean_text(str(raw))
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if iso:
            return iso.group(0)
        text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
            try:
                dt = datetime.strptime(text, fmt)
                if fmt in ("%B %Y", "%b %Y"):
                    return f"{dt.year:04d}-{dt.month:02d}-01"
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _extract_pdf_urls(self, root):
        if root is None:
            return []
        urls = []
        seen = set()
        for link in root.find_all("a", href=True):
            href = urljoin(self.base_url, link["href"].strip())
            href_no_fragment = href.split("#", 1)[0]
            path = urlparse(href_no_fragment).path.lower()
            link_text = link.get_text(" ", strip=True).lower()
            if not path.endswith(".pdf") and "pdf" not in link_text:
                continue
            if href_no_fragment not in seen:
                seen.add(href_no_fragment)
                urls.append(href_no_fragment)
        return urls

    def _filename_from_url(self, url):
        if not url:
            return None
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        if not tail:
            return None
        return unquote(tail)

    def _extract_authors(self, article_body, title):
        if article_body is None:
            return []

        authors = []
        staff_links = []
        for link in article_body.find_all("a", href=True):
            href = link.get("href", "")
            if "/about-us/staff/" in href:
                name = self._clean_author_name(link.get_text(" ", strip=True))
                if name:
                    staff_links.append(name)
        if staff_links:
            return self._dedupe(staff_links)

        lines = []
        for node in article_body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"], recursive=True):
            for line in node.get_text("\n", strip=True).splitlines():
                line = self._clean_text(line)
                if line:
                    lines.append(line)
            if len(lines) >= 12:
                break

        title_norm = self._norm_for_compare(title)
        for i, line in enumerate(lines):
            lowered = line.lower()
            if "read the" in lowered or "download" in lowered:
                continue
            if self._norm_for_compare(line) == title_norm:
                continue

            match = re.match(r"author\s*:\s*(.+)$", line, flags=re.IGNORECASE)
            if match:
                authors.extend(self._split_author_names(match.group(1)))
                continue

            pipe_match = re.match(r"(.+?)\s*\|\s*(.+)$", line)
            if pipe_match and self._parse_date(pipe_match.group(2)):
                authors.extend(self._split_author_names(pipe_match.group(1)))
                continue

            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if self._parse_date(next_line) and self._looks_like_author_line(line):
                authors.extend(self._split_author_names(line))

        return self._dedupe(authors)

    def _looks_like_author_line(self, line):
        if not line or len(line) > 100:
            return False
        if re.search(r"\d", line):
            return False
        lowered = line.lower()
        blocked = (
            "report", "brief", "download", "read", "problem", "solution",
            "what we heard", "policy", "roundtable", "housing", "mineral",
        )
        if any(word in lowered for word in blocked):
            return False
        words = re.findall(r"[A-Z][A-Za-z.'-]+", line)
        return len(words) >= 2

    def _split_author_names(self, raw):
        raw = re.sub(r"\s+", " ", self._clean_text(raw)).strip(" :-")
        raw = re.sub(r"\b(and|with)\b", ";", raw, flags=re.IGNORECASE)
        raw = raw.replace("&", ";")
        parts = re.split(r";|,\s*(?=[A-Z][a-z])", raw)
        return [self._clean_author_name(part) for part in parts if self._clean_author_name(part)]

    def _clean_author_name(self, value):
        value = self._clean_text(value)
        value = re.sub(r"\b(author|by)\b\s*:?", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"\s+", " ", value).strip(" ,;:-")
        if not value or len(value) > 80:
            return None
        return value

    def _extract_abstract(self, article_body, metas, title):
        if article_body is None:
            return self._meta_description(metas)

        texts = []
        title_norm = self._norm_for_compare(title)
        stop_patterns = (
            "media contact",
            "for more information contact",
            "about the canada west foundation",
            "support us",
            "join us",
        )

        for node in article_body.find_all(["h1", "h2", "h3", "h4", "p", "li"], recursive=True):
            text = self._clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            lowered = text.lower()
            if any(pattern in lowered for pattern in stop_patterns):
                break
            if self._norm_for_compare(text) == title_norm:
                continue
            if self._is_boilerplate_or_meta_line(text):
                continue
            if texts and text == texts[-1]:
                continue
            texts.append(text)

        abstract = self._clean_text(" ".join(texts))
        if len(abstract) > 5000:
            abstract = abstract[:5000].rsplit(" ", 1)[0].strip()
        return abstract

    def _is_boilerplate_or_meta_line(self, text):
        lowered = text.lower()
        if lowered in {"read the report (pdf)", "download pdf", "read the full brief"}:
            return True
        if ("read the" in lowered or "download" in lowered) and self._contains_month_year(text) and len(text) < 260:
            return True
        if lowered.startswith("read the report") or lowered.startswith("read the full"):
            return True
        if lowered.startswith("download "):
            return True
        if self._parse_date(text):
            return True
        if re.match(r"author\s*:", text, flags=re.IGNORECASE):
            return True
        return False

    def _contains_month_year(self, text):
        return bool(re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"(?:\s+\d{1,2},)?\s+\d{4}\b",
            text,
        ))

    def _meta_description(self, metas):
        return self._clean_text(
            metas.get("description")
            or metas.get("og:description")
            or metas.get("twitter:description")
            or ""
        )

    def _slug_from_url(self, url):
        path = urlparse(url).path.rstrip("/")
        return path.split("/")[-1] if path else None

    def _stable_hash(self, value):
        return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _wall_budget_nearly_spent(self, started_at):
        return time.monotonic() - started_at >= self.MAX_WALL_SECONDS - 30

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _norm_for_compare(self, value):
        value = self._clean_text(value).lower()
        return re.sub(r"[^a-z0-9]+", "", value)

    def _dedupe(self, values):
        result = []
        seen = set()
        for value in values:
            cleaned = self._clean_author_name(value)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result
