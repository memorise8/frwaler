# -*- coding: utf-8 -*-
"""Crawler for NTP/NIEHS publications.

The starting page is a publications landing page. Its real report list is the
Drupal ``study_report`` view rendered at /publications/reports. The view emits a
server-side DataTables HTML table with abstract detail links (/go/*abs) and PDF
links (/go/*).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse, unquote

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_MONTHS = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}


class NtpNiehsNihGovPublicationsCrawler(BaseCrawler):
    """Crawler for National Toxicology Program study reports."""

    site_id = "ntp-niehs-nih-gov-publications"
    site_name = "Custom: ntp-niehs-nih-gov-publications"
    base_url = "https://ntp.niehs.nih.gov"

    START_URL = "https://ntp.niehs.nih.gov/publications"
    LIST_URL = "https://ntp.niehs.nih.gov/publications/reports"
    PAGE_CAP = 200
    MAX_SECONDS = 25 * 60
    RETRY_WAITS = (1, 3, 9)
    MIN_ABSTRACT_CHARS = 50

    def crawl(self, limit=None):
        """Crawl NTP publication reports and save parsed records."""
        saved = 0
        start = time.monotonic()
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(1, self.PAGE_CAP + 1):
            if limit is not None and saved >= limit:
                break
            if self._time_exhausted(start):
                print(f"[{self.site_id}] crawl time budget nearly exhausted; saved {saved}")
                break

            list_url = self._list_url(page)
            fetched = self._curl_get(list_url, context=f"list page {page}", timeout=75)
            if not fetched or not fetched.get("body"):
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            soup = self._parse_html(fetched["body"], context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] page {page}: list HTML parse failed; stopping")
                break

            items = self._parse_list_items(soup, fetched.get("final_url") or list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                detail_url = item.get("url")
                if not detail_url:
                    continue
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                item_label = item.get("post_number") or item.get("external_id") or detail_url
                try:
                    if self._time_exhausted(start):
                        print(f"[{self.site_id}] crawl time budget nearly exhausted; saved {saved}")
                        return saved

                    time.sleep(getattr(self, "_delay", 1.0))
                    detail = self._curl_get(detail_url, context=f"item {item_label}", timeout=120)
                    if not detail or not detail.get("body"):
                        print(f"[{self.site_id}] item {item_label} failed: empty detail response")
                        continue

                    detail_soup = self._parse_html(
                        detail["body"], context=f"item {item_label}"
                    )
                    if detail_soup is None:
                        print(f"[{self.site_id}] item {item_label} failed: detail parse failed")
                        continue

                    paper = self._build_paper(item, detail_soup, detail)
                    abstract = self._clean(paper.get("abstract"))
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_label} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: only duplicate records; stopping")
                break
            if not self._has_next_page(soup):
                break
        else:
            print(f"[{self.site_id}] reached safety page cap {self.PAGE_CAP}; stopping")

        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, context="request", timeout=60):
        marker = "__NTP_CURL_META__"
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
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
            "-w",
            f"\n{marker}%{{http_code}} %{{url_effective}}\n",
            url,
        ]
        last_error = "unknown error"
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    timeout=timeout + 15,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body, http_code, final_url = self._split_curl_output(text, marker)
                if result.returncode == 0 and body.strip() and 200 <= http_code < 400:
                    return {"body": body, "http_code": http_code, "final_url": final_url}
                last_error = (
                    f"exit={result.returncode} http={http_code}"
                    if result.returncode
                    else f"http={http_code} empty={not bool(body.strip())}"
                )
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if stderr:
                    last_error = f"{last_error}: {stderr}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < 3:
                wait = self.RETRY_WAITS[attempt - 1]
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None

    @staticmethod
    def _split_curl_output(text, marker):
        idx = text.rfind(marker)
        if idx == -1:
            return text, 0, ""
        body = text[:idx]
        meta = text[idx + len(marker):].strip().split(None, 1)
        try:
            http_code = int(meta[0])
        except Exception:
            http_code = 0
        final_url = meta[1].strip() if len(meta) > 1 else ""
        return body, http_code, final_url

    # ------------------------------------------------------------------
    # HTML and text helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw, *, context="html"):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    @staticmethod
    def _clean(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _meta(soup, *names):
        for name in names:
            for attr in ("name", "property"):
                node = soup.find("meta", attrs={attr: name})
                if node and node.get("content"):
                    return node.get("content", "").strip()
        return ""

    @classmethod
    def _parse_date(cls, value):
        text = cls._one_line(value)
        if not text:
            return None
        iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if iso:
            return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
        us_month = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(\d{1,2}),\s*(\d{4})",
            text,
            re.I,
        )
        if us_month:
            return (
                f"{us_month.group(3)}-"
                f"{_MONTHS[us_month.group(1).lower()]}-"
                f"{us_month.group(2).zfill(2)}"
            )
        month_year = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(\d{4})",
            text,
            re.I,
        )
        if month_year:
            return f"{month_year.group(2)}-{_MONTHS[month_year.group(1).lower()]}-01"
        month_slash_year = re.fullmatch(r"(\d{1,2})/(\d{4})", text)
        if month_slash_year:
            return f"{month_slash_year.group(2)}-{month_slash_year.group(1).zfill(2)}-01"
        year = re.fullmatch(r"(\d{4})", text)
        if year:
            return f"{year.group(1)}-01-01"
        return None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urlparse(url).path.rstrip("/")
        if not path:
            return None
        name = unquote(path.rsplit("/", 1)[-1])
        if "." in name and len(name) <= 220:
            return name
        return None

    def _time_exhausted(self, started):
        return time.monotonic() - started >= self.MAX_SECONDS - 30

    def _list_url(self, page):
        if page <= 1:
            return self.LIST_URL
        parsed = urlparse(self.LIST_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page - 1)
        return urlunparse(parsed._replace(query=urlencode(query)))

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list_items(self, soup, page_url):
        rows = soup.select("table#datatable tbody tr")
        items = []
        for row in rows:
            try:
                cells = row.find_all("td", recursive=False)
                if len(cells) < 5:
                    continue

                abs_link = cells[0].find("a", href=True)
                if not abs_link:
                    continue
                pdf_link = cells[1].find("a", href=True)

                label = self._one_line(abs_link.get_text(" ", strip=True))
                detail_url = urljoin(page_url, abs_link.get("href", ""))
                pdf_alias = urljoin(page_url, pdf_link.get("href", "")) if pdf_link else None
                title = self._one_line(cells[2].get_text(" ", strip=True))
                listed_raw = self._one_line(cells[3].get_text(" ", strip=True))
                category = (
                    cells[4].get("data-title")
                    or self._one_line(cells[4].get_text(" ", strip=True))
                    or None
                )
                category = self._one_line(category)
                slug = self._slug_from_detail_url(detail_url, label)
                post_number = self._post_number(label, slug)

                items.append(
                    {
                        "url": self._normalize_url(detail_url),
                        "external_id": slug,
                        "post_number": post_number,
                        "report_label": label,
                        "title": title,
                        "listed_date_raw": listed_raw,
                        "listed_date": self._parse_date(listed_raw),
                        "category": category,
                        "pdf_alias": self._normalize_url(pdf_alias) if pdf_alias else None,
                        "pdf_size": self._one_line(cells[1].get_text(" ", strip=True)),
                        "list_endpoint": self.LIST_URL,
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] list item parse failed: {exc}")
                continue
        return items

    @staticmethod
    def _normalize_url(url):
        if not url:
            return url
        parsed = urlparse(url)
        return urlunparse(parsed._replace(fragment=""))

    @staticmethod
    def _slug_from_detail_url(url, label):
        path_tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"abs$", "", path_tail, flags=re.I)
        if slug:
            return slug.lower()
        return re.sub(r"[^a-z0-9]+", "", (label or "").lower()) or url

    @staticmethod
    def _post_number(label, slug):
        for value in (label, slug):
            if not value:
                continue
            match = re.search(r"(\d+)", value)
            if match:
                return match.group(1)
        return slug or None

    def _has_next_page(self, soup):
        if soup.select_one('a[rel="next"], li.pager__item--next a, a.pager-next'):
            return True
        for link in soup.find_all("a", href=True):
            text = self._one_line(link.get_text(" ", strip=True)).lower()
            if text in {"next", "next page", ">"}:
                return True
        return False

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _build_paper(self, list_item, soup, fetched):
        meta = self._collect_meta(soup)
        settings = self._drupal_settings(soup)

        final_url = fetched.get("final_url") or list_item.get("url")
        detail_url = self._normalize_url(final_url)
        go_link = self._meta(soup, "goLink") or urlparse(list_item.get("url", "")).path

        book_id = self._text_first(soup, ".field--name-field-book-id") or list_item.get("report_label")
        post_number = self._post_number(book_id, list_item.get("external_id"))
        external_id = (
            self._meta(soup, "SearchReport")
            or list_item.get("external_id")
            or re.sub(r"[^a-z0-9]+", "", (book_id or "").lower())
            or detail_url
        )

        title = (
            self._meta(soup, "citation_title")
            or self._publication_title(soup)
            or list_item.get("title")
            or self._text_first(soup, "h1")
        )
        abstract = self._extract_abstract(soup) or self._meta(soup, "description")

        published_raw = (
            self._meta(soup, "article:published_time")
            or self._meta(soup, "citation_publication_date")
            or self._text_first(soup, ".field--name-field-publish-date")
            or list_item.get("listed_date_raw")
        )
        published_date = self._parse_date(published_raw)
        listed_raw = list_item.get("listed_date_raw") or published_raw
        listed_date = list_item.get("listed_date") or published_date

        authors = self._extract_authors(soup)
        affiliations = self._extract_affiliations(soup)
        publisher = self._text_first(soup, ".field--name-field_publisher") or "National Toxicology Program"
        department = self._meta(soup, "Org") or None
        journal_raw = self._meta(soup, "citation_journal_title") or self._meta(soup, "NTPReports")
        category = list_item.get("category") or self._meta(soup, "ContentType") or journal_raw

        pdf_url = self._extract_pdf_url(soup) or list_item.get("pdf_alias")
        original_filename = self._filename_from_url(pdf_url)
        doi = self._extract_doi(soup)
        keywords = self._extract_keywords(title, category, book_id)

        metadata = {
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": journal_raw,
            "volume": post_number,
            "issue": None,
            "node_id": self._node_id(settings),
            "go_link": go_link,
            "SearchReport": self._meta(soup, "SearchReport"),
            "book_id": book_id,
            "report_label": list_item.get("report_label"),
            "report_type": category,
            "list_endpoint": list_item.get("list_endpoint"),
            "detail_effective_url": detail_url,
            "pdf_alias": list_item.get("pdf_alias"),
            "pdf_size": list_item.get("pdf_size"),
            "published_date_raw": published_raw,
            "citation_publication_date": self._meta(soup, "citation_publication_date"),
            "article_published_time": self._meta(soup, "article:published_time"),
            "article_modified_time": self._meta(soup, "article:modified_time"),
            "affiliations": affiliations,
            "meta": meta,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else detail_url,
            "post_number": str(post_number) if post_number is not None else None,
            "title": self._one_line(title),
            "abstract": self._clean(abstract),
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": department,
            "journal": journal_raw,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _collect_meta(self, soup):
        data = {}
        for node in soup.find_all("meta"):
            key = node.get("name") or node.get("property")
            content = node.get("content")
            if key and content and key not in data:
                data[key] = content
        return data

    def _drupal_settings(self, soup):
        node = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if not node:
            return {}
        try:
            return json.loads(node.string or node.get_text() or "{}")
        except Exception:
            return {}

    @staticmethod
    def _node_id(settings):
        current_path = ((settings or {}).get("path") or {}).get("currentPath")
        match = re.search(r"node/(\d+)", current_path or "")
        return match.group(1) if match else current_path

    def _text_first(self, soup, selector):
        node = soup.select_one(selector)
        if not node:
            return None
        return self._one_line(node.get_text(" ", strip=True))

    def _publication_title(self, soup):
        details = soup.select_one(".publication-details")
        if details:
            h2 = details.find_next("h2")
            if h2:
                return self._one_line(h2.get_text(" ", strip=True))
        article = soup.find("article")
        if article:
            h2 = article.find("h2")
            if h2:
                return self._one_line(h2.get_text(" ", strip=True))
        return None

    def _extract_abstract(self, soup):
        panel = soup.select_one("#Abstract-x") or soup.select_one(".field--name-field-abstract")
        if panel is None:
            return None

        # Keep prose, discard navigation controls and large summary tables.
        for selector in ("script", "style", "button", ".anchor-top", ".reveal", "table"):
            for node in panel.select(selector):
                node.decompose()

        parts = []
        for node in panel.find_all(["p", "li"], recursive=True):
            text = self._one_line(node.get_text(" ", strip=True))
            if not text:
                continue
            if text.lower() in {"back to top", "abstract"}:
                continue
            parts.append(text)

        if not parts:
            return self._one_line(panel.get_text(" ", strip=True))
        return "\n\n".join(parts)

    def _extract_authors(self, soup):
        authors = []
        for node in soup.select(".publication-authors .field--name-field-authors li"):
            for citation in node.select(".citation, .has-tip"):
                citation.decompose()
            text = self._one_line(node.get_text(" ", strip=True))
            if text:
                authors.append(text)
        return "; ".join(dict.fromkeys(authors)) if authors else None

    def _extract_affiliations(self, soup):
        affiliations = []
        for node in soup.select(".field--name-field-author-information li"):
            for span in node.find_all("span"):
                span.decompose()
            text = self._one_line(node.get_text(" ", strip=True))
            if text:
                affiliations.append(text)
        return affiliations

    def _extract_pdf_url(self, soup):
        for link in soup.select(".publication-download a[href]"):
            href = link.get("href", "")
            text = self._one_line(link.get_text(" ", strip=True)).lower()
            if ".pdf" in href.lower() and "summary" not in text and "full" in text:
                return self._normalize_url(urljoin(self.base_url, href))
        for link in soup.select('a[href$=".pdf"], a[href*=".pdf?"]'):
            href = link.get("href", "")
            text = self._one_line(link.get_text(" ", strip=True)).lower()
            if "summary" in text or "citation" in href.lower():
                continue
            if "full report" in text or "pdf version" in text or "508.pdf" in href.lower():
                return self._normalize_url(urljoin(self.base_url, href))
        return None

    def _extract_doi(self, soup):
        for area in (soup.select_one(".publication-download"), soup):
            if area is None:
                continue
            for link in area.find_all("a", href=True):
                href = link.get("href", "")
                text = self._one_line(link.get_text(" ", strip=True))
                combined = f"{href} {text}"
                match = re.search(r"10\.\d{4,9}/[^\s\"'<>)]+", combined, re.I)
                if match:
                    return match.group(0).rstrip(".,;")
        return None

    def _extract_keywords(self, title, category, book_id):
        values = []
        for value in (category, book_id):
            if value:
                values.append(self._one_line(value))
        for cas in re.findall(r"\bCAS(?:RN| No\.)?\s*([0-9-]+)", title or "", re.I):
            values.append(f"CASRN {cas}")
        return ", ".join(dict.fromkeys(v for v in values if v)) or None
