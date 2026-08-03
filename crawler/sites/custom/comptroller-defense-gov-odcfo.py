# -*- coding: utf-8 -*-
"""Crawler for DoD ODCFO Agency Financial Reports.

Discovery notes:
  - Start URL:
    https://comptroller.defense.gov/ODCFO/afr/
  - The page is a DNN/LiveHTML page, not a JSON list API. With a browser
    user-agent, curl returns a static HTML year list whose detail pages are
    /odcfo/afrYYYY.aspx.
  - Each detail page carries the AFR PDF links in a LiveHTML module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

# Absolute import: spec_from_file_location has no package context.
from crawler.base_crawler import BaseCrawler


class ComptrollerDefenseGovOdcfoCrawler(BaseCrawler):
    site_id = "comptroller-defense-gov-odcfo"
    site_name = "Custom: comptroller-defense-gov-odcfo"
    base_url = "https://comptroller.defense.gov"

    START_URL = "https://comptroller.defense.gov/ODCFO/afr/"
    SAFETY_CAP_PAGES = 200
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    MIN_ABSTRACT_CHARS = 50
    BACKOFF_SECONDS = (1, 3, 9)

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = 1.0 if detail_delay is None else detail_delay

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

            list_url = self._list_url(page)
            raw = self._curl_get(list_url, f"list page {page}", referer=self.START_URL)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response")
                break

            items, has_next = self._parse_list_page(raw, page)
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
                if not item_url:
                    continue
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_urls_on_page += 1

                try:
                    time.sleep(self._detail_delay)
                    detail_raw = self._curl_get(item_url, f"detail {item_url}", referer=list_url)
                    if not detail_raw:
                        raise RuntimeError("empty detail page response")

                    paper = self._parse_detail_page(detail_raw, item_url, item)
                    if not paper:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping item {page}.{idx}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:90]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page}.{idx} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: all item URLs already seen; stopping")
                break
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

    def _list_url(self, page):
        if page <= 1:
            return self.START_URL
        return f"{self.START_URL}?page={page}"

    def _parse_list_page(self, raw, page):
        soup = self._make_soup(raw)
        if soup is None:
            return self._parse_list_page_regex(raw, page), False

        items = []
        seen_years = set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            match = re.search(r"/?odcfo/afr((?:19|20)\d{2})\.aspx\b", href, flags=re.IGNORECASE)
            if not match:
                continue

            year = match.group(1)
            if year in seen_years:
                continue
            seen_years.add(year)

            detail_url = self._canonical_site_url(urljoin(self.base_url, href))
            items.append({
                "url": detail_url,
                "year": year,
                "post_number": year,
                "external_id": f"afr{year}",
                "title": self._default_title(year),
                "list_page": page,
                "list_text": self._clean_text(link.get_text(" ", strip=True)),
            })

        has_next = self._has_next_page(soup, page)
        return items, has_next

    def _parse_list_page_regex(self, raw, page):
        items = []
        seen_years = set()
        for match in re.finditer(r'["\']([^"\']*?/odcfo/afr((?:19|20)\d{2})\.aspx)["\']', raw or "", re.I):
            href, year = match.groups()
            if year in seen_years:
                continue
            seen_years.add(year)
            detail_url = self._canonical_site_url(urljoin(self.base_url, unescape(href)))
            items.append({
                "url": detail_url,
                "year": year,
                "post_number": year,
                "external_id": f"afr{year}",
                "title": self._default_title(year),
                "list_page": page,
                "list_text": year,
            })
        return items

    def _has_next_page(self, soup, page):
        next_link = soup.find("a", rel=lambda value: value and "next" in value)
        if next_link and next_link.get("href"):
            return True

        for link in soup.find_all("a", href=True):
            text = self._clean_text(link.get_text(" ", strip=True)).lower()
            if text in {"next", ">", "next page"}:
                return True
            href = link.get("href", "")
            match = re.search(r"[?&](?:page|p)=(\d+)", href, flags=re.I)
            if match:
                try:
                    if int(match.group(1)) > int(page):
                        return True
                except (TypeError, ValueError):
                    continue
        return False

    def _parse_detail_page(self, raw, url, item):
        soup = self._make_soup(raw)
        if soup is None:
            raise RuntimeError("BeautifulSoup could not parse detail HTML")

        year = item.get("year") or self._year_from_url(url)
        if not year:
            raise RuntimeError("missing fiscal year")

        metas = self._collect_metas(soup)
        canonical = self._canonical_site_url(self._canonical_from_soup(soup) or url)
        title = self._extract_title(soup, year, item)
        report_module = self._find_report_module(soup, year)
        module_text = self._clean_text(report_module.get_text(" ", strip=True)) if report_module else ""
        pdf_links = self._extract_pdf_links(report_module or soup)
        main_pdf = self._select_main_pdf(pdf_links, year)
        pdf_url = main_pdf.get("url") if main_pdf else None
        original_filename = self._filename_from_url(pdf_url)

        date_raw = self._extract_raw_date(soup, module_text, year)
        published_date = self._parse_date(date_raw)
        listed_date = published_date
        abstract = self._build_abstract(title, year, module_text, pdf_links)

        module_id = self._module_id(report_module)
        external_id = item.get("external_id") or f"afr{year}"
        post_number = item.get("post_number") or year
        series = "DoD Agency Financial Report / Performance and Accountability Report"
        publisher = "U.S. Department of Defense; Office of the Under Secretary of Defense (Comptroller)"
        department = "Office of the Deputy Chief Financial Officer (ODCFO)"

        metadata = {
            "posted_date": date_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": series,
            "volume": None,
            "issue": None,
            "native_id": external_id,
            "node_id": module_id,
            "module_id": module_id,
            "detail_slug": self._slug_from_url(canonical),
            "fiscal_year": year,
            "post_number": post_number,
            "date_basis": "fiscal year inferred from AFR page navigation",
            "list_page": item.get("list_page"),
            "list_text": item.get("list_text"),
            "list_title": item.get("title"),
            "start_url": self.START_URL,
            "source_endpoint": self.START_URL,
            "detail_endpoint": canonical,
            "pdf_links": pdf_links,
            "main_pdf_link": main_pdf,
            "page_title": self._clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None,
            "meta_description": metas.get("description"),
            "meta_keywords": metas.get("keywords"),
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
            "authors": None,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": (
                f"Agency Financial Report, Performance and Accountability Report, "
                f"DoD, ODCFO, financial statements, fiscal year {year}"
            ),
            "category": "Agency Financial Report",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _curl_get(self, url, label, referer=None, timeout=45):
        marker = "\n__COMPTROLLER_STATUS__:"
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-H",
            f"Referer: {referer or self.START_URL}",
            "-w",
            marker + "%{http_code}",
            url,
        ]

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout + 10,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                status = None
                if marker in body:
                    body, status_text = body.rsplit(marker, 1)
                    try:
                        status = int(status_text.strip()[:3])
                    except ValueError:
                        status = None

                if result.returncode == 0 and status is not None and 200 <= status < 300 and body.strip():
                    if self._looks_like_access_denied(body):
                        last_error = "access denied body"
                    else:
                        return body
                else:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    last_error = stderr or f"curl={result.returncode} status={status}"
            except KeyboardInterrupt:
                raise
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout: {exc}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = self.BACKOFF_SECONDS[attempt]
                print(
                    f"[{self.site_id}] {label} failed attempt {attempt + 1}/3: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {label} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
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

    def _canonical_from_soup(self, soup):
        link = soup.find("link", rel=lambda value: value and "canonical" in value)
        if link and link.get("href"):
            return urljoin(self.base_url, link.get("href"))
        return None

    def _extract_title(self, soup, year, item):
        year_pattern = re.compile(rf"\bFY\s*{re.escape(str(year))}\b.*Agency Financial Report", re.I)
        for node in soup.find_all(["h1", "h2", "h3"]):
            text = self._clean_text(node.get_text(" ", strip=True))
            if text and year_pattern.search(text):
                return text

        page_title = self._clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        if page_title and f"afr{year}" not in page_title.lower():
            return page_title
        return item.get("title") or self._default_title(year)

    def _find_report_module(self, soup, year):
        candidates = []
        year_key = f"fy{year}".lower()
        for node in soup.select("div.livehtml, div.DNNModuleContent, div.DnnModule"):
            html = str(node)
            html_lower = html.lower()
            if ".pdf" not in html_lower:
                continue
            if year_key not in html_lower and str(year) not in html_lower:
                continue
            text = self._clean_text(node.get_text(" ", strip=True))
            score = html_lower.count(".pdf") * 10
            if "agency financial report" in text.lower():
                score += 100
            if "dod afr" in text.lower():
                score += 50
            candidates.append((score, node))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _extract_pdf_links(self, root):
        links = []
        by_url = {}
        if root is None:
            return links

        for link in root.find_all("a", href=True):
            href = link.get("href", "").strip()
            url = self._canonical_site_url(urljoin(self.base_url, href)).split("#", 1)[0]
            parsed = urlparse(url)
            if not parsed.path.lower().endswith(".pdf"):
                continue

            text = self._clean_text(link.get_text(" ", strip=True))
            if not text:
                image = link.find("img")
                if image:
                    text = self._clean_text(image.get("alt"))
            filename = self._filename_from_url(url)
            entry = by_url.get(url)
            if entry is None:
                entry = {
                    "url": url,
                    "title": text or filename,
                    "originalFilename": filename,
                }
                links.append(entry)
                by_url[url] = entry
            elif text and (not entry.get("title") or entry.get("title") == entry.get("originalFilename")):
                entry["title"] = text

        return links

    def _select_main_pdf(self, pdf_links, year):
        if not pdf_links:
            return None

        preferred_patterns = (
            "agency financial report",
            "agency_financial_report",
            f"dod_fy{str(year)[-2:]}",
            f"fy{year}",
        )
        for link in pdf_links:
            haystack = f"{link.get('title') or ''} {link.get('originalFilename') or ''}".lower()
            if any(pattern in haystack for pattern in preferred_patterns):
                return link

        return pdf_links[0]

    def _build_abstract(self, title, year, module_text, pdf_links):
        pdf_titles = [link.get("title") for link in pdf_links if link.get("title")]
        pdf_summary = "; ".join(pdf_titles[:10])
        parts = [
            title,
            (
                f"This ODCFO page provides the Department of Defense Agency Financial Report "
                f"and Performance and Accountability Report materials for fiscal year {year}."
            ),
        ]
        if module_text:
            parts.append(f"Page text: {module_text}")
        if pdf_summary:
            parts.append(f"Linked report files include: {pdf_summary}.")
        return self._clean_text(" ".join(parts))

    def _extract_raw_date(self, soup, module_text, year):
        text = module_text or self._clean_text(soup.get_text(" ", strip=True))
        date_match = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},\s+(?:19|20)\d{2}\b",
            text,
            flags=re.I,
        )
        if date_match:
            return date_match.group(0)
        return f"FY {year}"

    def _parse_date(self, raw):
        text = self._clean_text(raw)
        if not text:
            return None

        iso_match = re.search(r"((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])", text)
        if iso_match:
            year, month, day = iso_match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                from datetime import datetime

                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue

        year_match = re.search(r"(?:19|20)\d{2}", text)
        if year_match:
            return f"{year_match.group(0)}-01-01"
        return None

    def _default_title(self, year):
        return f"FY {year} DoD Agency Financial Report (AFR) / DoD Performance and Accountability Report (PAR)"

    def _year_from_url(self, url):
        match = re.search(r"afr((?:19|20)\d{2})\.aspx", url or "", flags=re.I)
        return match.group(1) if match else None

    def _slug_from_url(self, url):
        path = urlparse(url or "").path.rstrip("/")
        return os.path.basename(path) if path else None

    def _filename_from_url(self, url):
        if not url:
            return None
        name = os.path.basename(urlparse(url).path.rstrip("/"))
        return unquote(name) if name else None

    def _module_id(self, node):
        if node is None:
            return None
        current = node
        while current is not None:
            node_id = current.get("id")
            if node_id:
                match = re.search(r"(?:LiveHTMLWrapper|dnn_ctr)(\d+)", node_id)
                if match:
                    return match.group(1)
            classes = current.get("class") or []
            for cls in classes:
                match = re.search(r"DnnModule-(\d+)", str(cls))
                if match:
                    return match.group(1)
            current = current.parent
        return None

    def _canonical_site_url(self, url):
        if not url:
            return url
        parsed = urlparse(urljoin(self.base_url, url))
        host = parsed.netloc.lower()
        if host in {"comptroller.war.gov", "www.comptroller.war.gov"}:
            parsed = parsed._replace(scheme="https", netloc="comptroller.defense.gov")
        elif host in {"www.comptroller.defense.gov", "comptroller.defense.gov"}:
            parsed = parsed._replace(scheme="https", netloc="comptroller.defense.gov")
        return urlunparse(parsed)

    def _looks_like_access_denied(self, body):
        sample = (body or "")[:1000].lower()
        return "access denied" in sample and "edgesuite.net" in sample

    def _wall_budget_nearly_spent(self, started_at):
        return time.monotonic() - started_at >= self.MAX_WALL_SECONDS - 30

    def _clean_text(self, value):
        if value is None:
            return ""
        text = unescape(str(value)).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
