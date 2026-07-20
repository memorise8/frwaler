# -*- coding: utf-8 -*-
"""Federal Budget archive crawler for budget.canada.ca."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BudgetCanadaCaHomeAccueilEnhtmlCrawler(BaseCrawler):
    site_id = "budget-canada-ca-home-accueil-enhtml"
    site_name = "Custom: budget-canada-ca-home-accueil-enhtml"
    base_url = "https://budget.canada.ca"

    START_URL = "https://budget.canada.ca/home-accueil-en.html"
    BACKOFF_SECONDS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

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

        list_url = self._discover_list_endpoint(start_soup, start_raw)
        list_data = self._curl_json(list_url, context="budget.json list endpoint")
        if not list_data:
            print(f"[{self.site_id}] list endpoint failed: {list_url}")
            return saved

        records = list_data.get("data") if isinstance(list_data.get("data"), list) else []
        if not records:
            print(f"[{self.site_id}] list endpoint returned no records: {list_url}")
            return saved

        print(f"[{self.site_id}] discovered {len(records)} records from {list_url}")

        seen_urls = set()
        for idx, item in enumerate(records, start=1):
            if limit is not None and saved >= limit:
                break

            try:
                record = self._parse_list_record(item, idx, list_url)
                if not record["url"]:
                    raise RuntimeError("list record has no English detail URL")
                if record["url"] in seen_urls:
                    continue
                seen_urls.add(record["url"])

                time.sleep(self._delay)
                detail_raw = self._curl_get(record["url"], context=f"item {idx} detail")
                if not detail_raw:
                    raise RuntimeError("detail fetch failed after retries")

                detail_soup = self._make_soup(detail_raw)
                if detail_soup is None:
                    raise RuntimeError("detail HTML could not be parsed")

                parsed = self._parse_detail(detail_soup, record)
                abstract = parsed["abstract"]
                if len(abstract) < self.MIN_ABSTRACT_CHARS:
                    print(
                        f"[{self.site_id}] item {idx} skipped: "
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
                    "doi": "",
                    "department": parsed["department"],
                    "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                counter = f"{saved}/{limit}" if limit else str(saved)
                print(f"[{self.site_id}] saved {counter}: {parsed['title'][:90]}")
            except Exception as exc:
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _discover_list_endpoint(self, soup, start_raw):
        page_title = self._clean_text(soup.find("title")).lower()
        prefer_budget = "budget" in page_title
        candidates = []

        for script in soup.find_all("script", src=True):
            script_url = urljoin(self.START_URL, script.get("src", "").strip())
            if not script_url or "archives" not in script_url.lower():
                continue

            js_raw = self._curl_get(
                script_url,
                context="archives.js discovery",
                accept="application/javascript,text/javascript,*/*;q=0.8",
            )
            if not js_raw:
                continue

            for match in re.finditer(r"['\"]([^'\"]+\.json)['\"]", js_raw):
                candidates.append(urljoin(script_url, match.group(1)))

        for match in re.finditer(r"['\"]([^'\"]+\.json)['\"]", start_raw):
            candidates.append(urljoin(self.START_URL, match.group(1)))

        if prefer_budget:
            for candidate in candidates:
                if candidate.rstrip("/").endswith("/budget.json"):
                    return candidate

        if candidates:
            return candidates[0]

        return urljoin(self.START_URL, "budget.json")

    def _parse_list_record(self, item, idx, list_url):
        if not isinstance(item, dict):
            raise RuntimeError("list record is not an object")

        title = self._localized(item.get("title")) or f"Budget record {idx}"
        title = self._strip_archived_prefix(title)
        date_raw = self._clean_text(item.get("date") or "")
        published_date = self._normalize_date(date_raw)

        website = item.get("website") if isinstance(item.get("website"), dict) else {}
        detail_path = website.get("en") or website.get("eng") or ""
        detail_url = urljoin(self.base_url + "/", detail_path.lstrip("/")) if detail_path else ""

        publications = item.get("publications") if isinstance(item.get("publications"), dict) else {}
        english_pubs = publications.get("en") if isinstance(publications.get("en"), list) else []
        pdf_url = self._select_pdf_url(english_pubs)

        external_id = self._external_id(detail_url or f"{title}-{date_raw}")
        keywords = self._dedupe(["Federal budget", title, "Budget", "Public finance"])

        return {
            "external_id": external_id,
            "title": title,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "category": "Federal Budget",
            "keywords": keywords,
            "authors": ["Department of Finance Canada"],
            "department": "Department of Finance Canada",
            "metadata": {
                "source_format": "json+html",
                "start_url": self.START_URL,
                "list_endpoint": list_url,
                "detail_endpoint": detail_url,
                "list_index": idx,
                "date_raw": date_raw,
                "prime_minister": self._clean_text(item.get("pMin") or ""),
                "finance_minister": self._clean_text(self._localized(item.get("fMin")) or item.get("fMin") or ""),
                "website": website,
                "publications": publications,
            },
        }

    def _parse_detail(self, soup, record):
        main = soup.find("main") or soup
        self._remove_noise(main)

        title = (
            self._meta_content(soup, "dcterms.title")
            or self._schema_value(soup, "name")
            or self._clean_text(soup.find("h1"))
            or record["title"]
        )
        title = self._strip_archived_prefix(title)
        if not title or title.lower() in {"homepage", "home"}:
            title = record["title"]
        if "budget" not in title.lower() and "budget" in record["title"].lower():
            title = record["title"]

        published_date = (
            self._normalize_date(self._meta_content(soup, "dcterms.issued"))
            or self._normalize_date(self._schema_value(soup, "datePublished"))
            or record["published_date"]
        )

        detail_url = self._canonical_url(soup) or record["url"]
        abstract, abstract_source = self._extract_abstract(main, soup)
        pdf_url = record["pdf_url"] or self._first_pdf_url(main, detail_url)

        keywords = self._dedupe(
            record["keywords"]
            + self._split_terms(self._meta_content(soup, "keywords"))
            + self._split_terms(self._meta_content(soup, "dcterms.subject"))
        )

        metadata = dict(record["metadata"])
        metadata.update({
            "canonical_url": self._canonical_url(soup),
            "abstract_source": abstract_source,
            "dcterms_description": self._meta_content(soup, "dcterms.description"),
            "meta_description": self._meta_content(soup, "description"),
            "modified_date": self._normalize_date(
                self._meta_content(soup, "dcterms.modified") or self._date_modified(main)
            ),
        })

        return {
            "external_id": record["external_id"],
            "title": title,
            "authors": record["authors"],
            "abstract": abstract,
            "category": record["category"],
            "keywords": keywords,
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "department": record["department"],
            "metadata": metadata,
        }

    def _curl_json(self, url, context="request"):
        raw = self._curl_get(
            url,
            context=context,
            accept="application/json,text/javascript,*/*;q=0.8",
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

    def _curl_get(self, url, context="request", accept=None):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
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
            url,
        ]

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

    def _extract_abstract(self, main, soup):
        priority_parts = []
        for selector in (".planContent p", ".hero-copy p", ".lead"):
            for node in main.select(selector):
                text = self._clean_text(node)
                if self._is_useful_abstract_part(text):
                    priority_parts.append(text)

        if priority_parts:
            return self._join_limited(priority_parts), "priority body text"

        parts = []
        seen = set()
        for node in main.find_all(["p", "li"]):
            text = self._clean_text(node)
            if not self._is_useful_abstract_part(text):
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            parts.append(text)
            if len(" ".join(parts)) >= 1200:
                break

        body_abstract = self._join_limited(parts)
        if len(body_abstract) >= self.MIN_ABSTRACT_CHARS:
            return body_abstract, "body text"

        for meta_name in ("description", "dcterms.description", "og:description", "twitter:description"):
            text = self._meta_content(soup, meta_name)
            if self._is_useful_abstract_part(text):
                return text, meta_name

        return body_abstract, "body text"

    def _is_useful_abstract_part(self, text):
        if len(text) < 40:
            return False
        lowered = text.lower()
        skip_phrases = (
            "skip to main content",
            "we have archived this page",
            "you can use it for research",
            "date modified",
            "available pdf",
            "pdf downloads",
            "download the budget",
            "view the budget",
            "table of contents",
            "this link opens in a new tab",
        )
        return not any(phrase in lowered for phrase in skip_phrases)

    def _join_limited(self, parts, limit=1800):
        text = self._normalize_text(" ".join(part for part in parts if part))
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:")
        return f"{cut}."

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
        ]):
            tag.decompose()

    def _select_pdf_url(self, publications):
        if not publications:
            return ""

        def score(pub):
            title = self._clean_text(pub.get("title") if isinstance(pub, dict) else "")
            path = pub.get("path") if isinstance(pub, dict) else ""
            value = 0
            if str(path).lower().endswith(".pdf"):
                value += 10
            if title.lower() in {"budget", "budget plan"}:
                value += 20
            elif "budget" in title.lower():
                value += 8
            return value

        best = max(publications, key=score)
        path = best.get("path", "") if isinstance(best, dict) else ""
        if not path:
            return ""
        return urljoin(self.base_url + "/", str(path).lstrip("/"))

    def _first_pdf_url(self, root, base):
        for link in root.find_all("a", href=True):
            href = link.get("href", "").strip()
            if ".pdf" in href.lower():
                return urljoin(base, href)
        return ""

    def _canonical_url(self, soup):
        link = soup.find("link", rel=lambda value: value and "canonical" in value)
        if link and link.get("href"):
            return urljoin(self.base_url + "/", link["href"].strip())
        meta_url = self._meta_content(soup, "og:url")
        if meta_url:
            return urljoin(self.base_url + "/", meta_url)
        return ""

    def _schema_value(self, soup, key):
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            value = self._schema_find(data, key)
            if value:
                return self._clean_text(value)
        return ""

    def _schema_find(self, data, key):
        if isinstance(data, dict):
            if data.get(key):
                return data[key]
            for value in data.values():
                found = self._schema_find(value, key)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._schema_find(item, key)
                if found:
                    return found
        return ""

    def _meta_content(self, soup, name):
        for attrs in ({"name": name}, {"property": name}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                return self._clean_text(tag["content"])
        return ""

    def _date_modified(self, root):
        time_tag = root.find("time", attrs={"property": "dateModified"})
        if time_tag:
            return self._clean_text(time_tag)
        return ""

    def _localized(self, value):
        if isinstance(value, dict):
            return self._clean_text(value.get("en") or value.get("eng") or next(iter(value.values()), ""))
        return self._clean_text(value)

    def _split_terms(self, value):
        text = self._clean_text(value)
        if not text:
            return []
        return [part.strip() for part in re.split(r"[;,|]", text) if part.strip()]

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

    def _external_id(self, value):
        text = self._clean_text(value)
        parsed_path = urlparse(text).path.strip("/")
        basis = parsed_path or text
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", basis).strip("-").lower()
        if slug:
            return slug[:180]
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        return f"budget-{digest}"

    def _normalize_date(self, raw):
        text = self._clean_text(raw)
        match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(\d{4})(\d{2})(\d{2})", text)
        if match:
            year, month, day = match.groups()
            return f"{year}-{month}-{day}"
        return ""

    def _strip_archived_prefix(self, value):
        text = self._clean_text(value)
        text = re.sub(r"^Archived\s*[-:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*\|\s*Budget\s+\d{4}$", "", text).strip()
        return text

    def _clean_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = unescape(str(value)).replace("\xa0", " ")
        return self._normalize_text(text)

    def _normalize_text(self, text):
        return re.sub(r"\s+", " ", str(text)).strip()
