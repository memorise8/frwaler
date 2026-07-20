# -*- coding: utf-8 -*-
"""Crawler for the Estonian Academy Publishers archive.

The kirj.ee article archive is rendered by a custom WordPress plugin. The
useful endpoints are HTML pages:

* /archive/ seeds journal issue URLs.
* <journal publications page>?filter[year]=YYYY&filter[issue]=ID lists articles.
* The same URL with filter[publication]=ID renders article detail HTML.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class KirjEeArchiveCrawler(BaseCrawler):
    site_id = "kirj-ee-archive"
    site_name = "Custom: kirj-ee-archive"
    base_url = "https://kirj.ee"

    _START_URL = "https://kirj.ee/archive/?v=38dd815e66db"
    _MIN_ABSTRACT_CHARS = 50
    _CURL_TIMEOUT = 45

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network and parsing helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=None):
        timeout = timeout or self._CURL_TIMEOUT
        cmd = [
            "curl",
            "-g",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] curl failed {attempt + 1}/3 for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[kirj-ee-archive] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\r", "\n").replace("\f", "\n")
        text = re.sub(r"[ \t\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value):
        return re.sub(r"\s+", " ", cls._clean_text(value)).strip()

    @classmethod
    def _tag_text(cls, tag, separator=" "):
        if not tag:
            return ""
        return cls._one_line(tag.get_text(separator, strip=True))

    @staticmethod
    def _dedupe(items):
        seen = set()
        result = []
        for item in items:
            value = str(item or "").strip()
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @classmethod
    def _split_authors(cls, raw):
        text = cls._one_line(raw)
        if not text:
            return []
        parts = [p.strip() for p in re.split(r"\s*,\s*", text) if p.strip()]
        return cls._dedupe(parts)

    @staticmethod
    def _query_value(url, key):
        parsed = urlparse(url)
        values = parse_qs(parsed.query, keep_blank_values=True).get(key) or []
        return values[0] if values else ""

    @classmethod
    def _year_from_text(cls, *values):
        for value in values:
            match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
            if match:
                return match.group(0)
        return ""

    @staticmethod
    def _extract_doi(value):
        text = str(value or "")
        match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, re.I)
        if not match:
            return ""
        return match.group(0).rstrip(".,);")

    @staticmethod
    def _pdf_timestamp(pdf_url):
        match = re.search(r"_(\d{4})(\d{2})(\d{2})\d{6}\.pdf(?:$|[?#])", pdf_url or "")
        if not match:
            return ""
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    @classmethod
    def _split_title_pages(cls, raw_title):
        title = cls._one_line(raw_title)
        match = re.search(r"\s*;\s*((?:p|pp)\.?\s+.+)$", title, re.I)
        if not match:
            return title, ""
        clean_title = title[: match.start()].strip()
        return clean_title or title, cls._one_line(match.group(1))

    def _canonical_url(self, url, *, include_publication):
        absolute = urljoin(self.base_url, url)
        parsed = urlparse(absolute)
        params = parse_qs(parsed.query, keep_blank_values=True)

        keep = {}
        for key in ("filter[year]", "filter[issue]"):
            value = (params.get(key) or [""])[0]
            if value:
                keep[key] = value
        publication_id = (params.get("filter[publication]") or [""])[0]
        if include_publication and publication_id:
            keep["filter[publication]"] = publication_id

        query = urlencode(keep) if keep else ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))

    def _is_internal_content_url(self, url):
        parsed = urlparse(urljoin(self.base_url, url))
        if parsed.netloc and parsed.netloc != "kirj.ee":
            return False
        path = parsed.path or ""
        if path.startswith("/wp-content/") or path.startswith("/wp-admin/"):
            return False
        if path in ("", "/"):
            return False
        return True

    # ------------------------------------------------------------------
    # Archive, issue, and article parsing
    # ------------------------------------------------------------------

    def _parse_archive_links(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return []

        scope = soup.select_one(".sisu") or soup
        entries = []
        for link in scope.select("a[href]"):
            href = link.get("href") or ""
            if not self._is_internal_content_url(href):
                continue
            absolute = urljoin(self.base_url, href)
            parsed = urlparse(absolute)
            if parsed.path.rstrip("/") == "/archive":
                continue
            journal = self._tag_text(link)
            if not journal:
                continue
            entries.append(
                {
                    "url": self._canonical_url(absolute, include_publication=False),
                    "journal_hint": journal,
                    "source": "archive",
                }
            )

        deduped = []
        seen = set()
        for entry in entries:
            if entry["url"] in seen:
                continue
            seen.add(entry["url"])
            deduped.append(entry)
        return deduped

    def _parse_issue_links(self, soup, current_url, journal_hint):
        if soup is None:
            return []

        entries = []
        for link in soup.select(".issues_menu a[href], a[href*='filter%5Bissue%5D'], a[href*='filter[issue]']"):
            href = link.get("href") or ""
            if "filter" not in href:
                continue
            issue_url = self._canonical_url(urljoin(current_url, href), include_publication=False)
            if not self._query_value(issue_url, "filter[issue]"):
                continue
            entries.append(
                {
                    "url": issue_url,
                    "journal_hint": journal_hint,
                    "source": "issue-menu",
                }
            )

        deduped = []
        seen = set()
        for entry in entries:
            if entry["url"] in seen:
                continue
            seen.add(entry["url"])
            deduped.append(entry)
        return deduped

    def _page_journal_name(self, soup, journal_hint):
        title = self._tag_text(soup.title if soup else None)
        title = re.sub(r"^EAP\s*-\s*", "", title)
        title = re.sub(r"\s+Publications\.?$", "", title, flags=re.I)
        title = title.replace(" - ", " ").strip()
        return title or journal_hint or "Estonian Academy Publishers"

    def _parse_issue_records(self, raw, issue_url, journal_hint):
        soup = self._parse_html(raw)
        if soup is None:
            return [], []

        issue_links = self._parse_issue_links(soup, issue_url, journal_hint)
        issue_title = self._tag_text(soup.select_one(".issue_title"))
        issue_special_title = self._tag_text(soup.select_one(".issue_spetitle"))
        journal = self._page_journal_name(soup, journal_hint)
        year = self._year_from_text(issue_title, self._query_value(issue_url, "filter[year]"))
        issue_id = self._query_value(issue_url, "filter[issue]")

        records = []
        for link in soup.select("a.publication_list[href*='publication'], a[href*='filter%5Bpublication%5D'], a[href*='filter[publication]']"):
            href = link.get("href") or ""
            detail_url = self._canonical_url(urljoin(issue_url, href), include_publication=True)
            publication_id = self._query_value(detail_url, "filter[publication]") or link.get("stats_obj_id") or ""
            if not publication_id:
                continue

            title_node = link.find_previous(class_="artlist_title")
            raw_title = link.get("stats_title") or self._tag_text(title_node)
            title, pages = self._split_title_pages(raw_title)
            if not title:
                continue

            authors_node = link.find_previous(class_="pubauthorsdiv")
            type_node = link.find_previous(class_="art_type")
            pdf_node = link.find_previous("a", class_="pdf-link")
            pdf_url = urljoin(self.base_url, pdf_node.get("href")) if pdf_node and pdf_node.get("href") else ""

            records.append(
                {
                    "external_id": publication_id,
                    "url": detail_url,
                    "title": title,
                    "pages": pages,
                    "authors": self._split_authors(self._tag_text(authors_node)),
                    "category": self._tag_text(type_node),
                    "published_date": year,
                    "issue_id": issue_id,
                    "issue_title": issue_title,
                    "issue_special_title": issue_special_title,
                    "journal": journal,
                    "journal_hint": journal_hint,
                    "pdf_url": pdf_url,
                    "source_issue_url": issue_url,
                }
            )

        deduped = []
        seen = set()
        for record in records:
            if record["external_id"] in seen:
                continue
            seen.add(record["external_id"])
            deduped.append(record)
        return deduped, issue_links

    def _parse_keywords(self, soup):
        if soup is None:
            return []

        labels = soup.find_all(string=re.compile(r"^\s*Keywords?\s*$", re.I))
        for label in labels:
            parent = label.parent
            if not parent:
                continue
            sibling = parent.find_next_sibling()
            if sibling:
                text = self._tag_text(sibling)
                if text and not re.match(r"^(Abstract|References?)$", text, re.I):
                    return self._dedupe(re.split(r"\s*[,;]\s*", text))
        return []

    def _parse_detail_record(self, raw, list_record):
        soup = self._parse_html(raw)
        if soup is None:
            raise RuntimeError("detail HTML could not be parsed")

        title_node = soup.select_one(".articleview_title")
        title, pages = self._split_title_pages(self._tag_text(title_node) or list_record.get("title"))
        authors_text = self._tag_text(soup.select_one("#authorsdiv"))
        authors = self._split_authors(authors_text) or list_record.get("authors", [])
        abstract = self._tag_text(soup.select_one(".abstractdiv"), separator="\n")

        pdf_node = soup.select_one("a.pdf-link")
        pdf_url = ""
        if pdf_node and pdf_node.get("href"):
            pdf_url = urljoin(self.base_url, pdf_node.get("href"))
        if not pdf_url:
            pdf_url = list_record.get("pdf_url", "")

        doi_text = ""
        doi_node = soup.select_one("a.doi_article")
        if doi_node:
            doi_text = " ".join([doi_node.get("href") or "", self._tag_text(doi_node)])
        doi = self._extract_doi(doi_text)

        category = self._tag_text(soup.select_one(".art_type")) or list_record.get("category", "")
        keywords = self._parse_keywords(soup)
        reference_count = len(soup.select(".refsdiv p"))
        pdf_posted_date = self._pdf_timestamp(pdf_url)

        metadata = {
            "endpointDiscovery": {
                "archive": self._START_URL,
                "list": "HTML issue page with filter[year] and filter[issue]",
                "detail": "HTML article page with filter[publication]",
            },
            "journal": list_record.get("journal") or list_record.get("journal_hint"),
            "journalHint": list_record.get("journal_hint"),
            "issueId": list_record.get("issue_id"),
            "issueTitle": list_record.get("issue_title"),
            "issueSpecialTitle": list_record.get("issue_special_title"),
            "pages": pages or list_record.get("pages"),
            "sourceIssueUrl": list_record.get("source_issue_url"),
            "pdfPostedDate": pdf_posted_date,
            "referenceCount": reference_count,
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": list_record.get("external_id"),
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": list_record.get("published_date", ""),
            "url": list_record.get("url"),
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "Estonian Academy Publishers",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        print(
            f"[{self.site_id}] endpoints: archive HTML -> issue HTML "
            "filter[year]/filter[issue] -> detail HTML filter[publication]"
        )

        archive_raw = self._curl(self._START_URL)
        if not archive_raw:
            print(f"[{self.site_id}] start URL failed; nothing saved")
            return 0

        queue = self._parse_archive_links(archive_raw)
        queued_urls = {entry["url"] for entry in queue}
        seen_issue_urls = set()
        seen_publications = set()
        saved = 0

        print(f"[{self.site_id}] discovered {len(queue)} archive issue/journal links")

        index = 0
        while index < len(queue):
            if limit is not None and saved >= limit:
                break

            entry = queue[index]
            index += 1
            issue_url = entry["url"]
            if issue_url in seen_issue_urls:
                continue
            seen_issue_urls.add(issue_url)

            issue_raw = self._curl(issue_url, referer=self._START_URL)
            if not issue_raw:
                print(f"[{self.site_id}] issue page failed: {issue_url}")
                continue

            records, issue_links = self._parse_issue_records(
                issue_raw,
                issue_url,
                entry.get("journal_hint", ""),
            )
            for issue_entry in issue_links:
                if issue_entry["url"] in queued_urls or issue_entry["url"] in seen_issue_urls:
                    continue
                queued_urls.add(issue_entry["url"])
                queue.append(issue_entry)

            if not records:
                print(f"[{self.site_id}] no article records found: {issue_url}")
                continue

            print(f"[{self.site_id}] {len(records)} records found: {issue_url}")
            for item_number, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = record.get("external_id") or f"{item_number}"
                if item_label in seen_publications:
                    continue
                seen_publications.add(item_label)

                try:
                    time.sleep(self.detail_delay)
                    detail_raw = self._curl(record["url"], referer=issue_url)
                    if not detail_raw:
                        raise RuntimeError("detail fetch failed after retries")

                    paper = self._parse_detail_record(detail_raw, record)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[kirj-ee-archive] item {item_label} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
