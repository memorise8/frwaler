# -*- coding: utf-8 -*-
"""Crawler for ANSES request-based opinions and reports."""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class AnsesFrFrCrawler(BaseCrawler):
    site_id = "anses-fr-fr"
    site_name = "Custom: anses-fr-fr"
    base_url = "https://www.anses.fr"

    START_URL = (
        "https://www.anses.fr/fr/content/"
        "avis-et-rapports-de-lanses-sur-saisine"
    )
    LIST_ENDPOINT = START_URL + "?page={page}"
    DETAIL_ENDPOINT = "https://www.anses.fr/system/files/<pdf>"

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 60
    PDF_TEXT_TIMEOUT = 45
    PDF_TEXT_PAGES = 6
    MIN_ABSTRACT_CHARS = 50
    MAX_ABSTRACT_CHARS = 6000
    _CURL_META_MARKER = "__ANSES_FR_FR_CURL_META__:"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    def crawl(self, limit=None):
        """Crawl the Drupal Views HTML list and linked PDF documents."""
        saved = 0
        page = 0
        seen_urls = set()

        while True:
            if limit is not None and saved >= limit:
                break

            list_url = self._list_url(page)
            raw, list_effective_url = self._curl_get_text(
                list_url,
                context=f"list page {page}",
                referer=self.base_url + "/fr",
            )
            if not raw:
                print(f"[{self.site_id}] list endpoint failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list page {page} could not be parsed; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] no records found at page {page}; stopping")
                break

            print(
                f"[{self.site_id}] page {page}: discovered {len(records)} records "
                "from Drupal Views HTML list endpoint"
            )

            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                item_label = f"page {page} item {idx}"
                try:
                    pdf_url = record.get("pdf_url") or ""
                    if not pdf_url:
                        raise RuntimeError("record has no PDF detail URL")
                    if pdf_url in seen_urls:
                        continue
                    seen_urls.add(pdf_url)

                    time.sleep(self.detail_delay)
                    pdf_bytes, effective_pdf_url = self._curl_get_bytes(
                        pdf_url,
                        context=f"item {item_label} PDF detail",
                        referer=list_effective_url or list_url,
                        accept="application/pdf,*/*;q=0.8",
                    )
                    if not pdf_bytes:
                        raise RuntimeError("PDF detail fetch failed after retries")

                    pdf_text = self._extract_pdf_text(
                        pdf_bytes,
                        context=f"item {item_label} PDF detail",
                    )
                    parsed = self._parse_detail(record, pdf_text, effective_pdf_url)
                    abstract = parsed.get("abstract") or ""
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item_label} skipped: "
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
                    print(f"[anses-fr-fr] item {item_label} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break
            if not self._has_next_page(soup):
                break
            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get_text(self, url, context="request", referer=None, accept=None):
        body, effective_url = self._curl_get_bytes(
            url,
            context=context,
            referer=referer,
            accept=accept
            or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json,*/*;q=0.8"
            ),
        )
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
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
            f"Accept: {accept or '*/*'}",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            "-w",
            "\n" + self._CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"",
                    url,
                )
                stderr = (result.stderr or b"").decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError(f"empty response for {effective_url}")
                return body, effective_url
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                wait = self.BACKOFF_SECONDS[attempt - 1]
                if attempt < 3:
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} curl failed after 3 attempts: {last_error}")
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self._CURL_META_MARKER).encode("ascii")
        marker_pos = raw.rfind(marker)
        if marker_pos == -1:
            return raw, "", fallback_url
        body = raw[:marker_pos]
        meta = raw[marker_pos + len(marker):].decode(
            "utf-8",
            errors="replace",
        ).strip()
        if "\t" not in meta:
            return body, "", fallback_url
        http_code, effective_url = meta.split("\t", 1)
        return body, http_code.strip(), effective_url.strip() or fallback_url

    # ------------------------------------------------------------------
    # Parsing helpers
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
        print(f"[{self.site_id}] all BeautifulSoup parsers failed for {context}: {last_exc}")
        return None

    def _parse_list(self, soup):
        records = []
        seen = set()
        for article in soup.select(".views-row article.document"):
            link = article.select_one(".know-more__title a[href]") or article.select_one(
                "h3 a[href]"
            )
            if link is None:
                continue

            title = self._normalize_text(link.get_text(" ", strip=True))
            pdf_url = self._clean_url(urljoin(self.base_url, link.get("href", "")))
            if not title or not pdf_url:
                continue
            if not self._is_pdf_url(pdf_url):
                continue

            download_link = article.select_one("a.download[href]")
            download_url = ""
            if download_link is not None:
                download_url = urljoin(self.base_url, download_link.get("href", ""))

            referral = self._field_text(article, ".document__referral")
            external_id = self._external_id(pdf_url, referral)
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)

            signature_date = self._date_from_time(
                article,
                ".document__signature_date time",
            )
            online_date = self._date_from_time(article, ".document__online_date time")
            keywords_raw = self._field_text(article, ".document__keywords")
            thematics = [
                self._normalize_text(node.get_text(" ", strip=True))
                for node in article.select(".document__thematique-item")
            ]
            thematics = [item for item in thematics if item]

            records.append(
                {
                    "external_id": external_id,
                    "title": title,
                    "url": pdf_url,
                    "pdf_url": pdf_url,
                    "download_url": download_url,
                    "doc_type": self._field_text(article, ".document__type_document"),
                    "expert_committee": self._field_text(
                        article,
                        ".document__expert_committee",
                    ),
                    "referral": referral,
                    "linked_referrals": self._field_text(
                        article,
                        ".document__linked_referrals",
                    ),
                    "signature_date": signature_date,
                    "online_date": online_date,
                    "keywords_raw": keywords_raw,
                    "keywords": self._split_keywords(keywords_raw),
                    "thematics": thematics,
                }
            )
        return records

    def _parse_detail(self, record, pdf_text, effective_pdf_url):
        cleaned_pdf_text = self._clean_pdf_text(pdf_text)
        abstract = self._build_abstract(cleaned_pdf_text)
        pdf_url = effective_pdf_url or record["pdf_url"]
        return {
            "external_id": record["external_id"],
            "title": record["title"],
            "authors": ["Anses"],
            "abstract": abstract,
            "category": record.get("doc_type") or "Avis et rapports",
            "keywords": record.get("keywords") or [],
            "published_date": record.get("signature_date") or record.get("online_date"),
            "url": pdf_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": record.get("expert_committee") or "Anses",
            "metadata": {
                "source": "Drupal Views HTML list + linked PDF",
                "listEndpoint": self.LIST_ENDPOINT,
                "detailEndpoint": self.DETAIL_ENDPOINT,
                "documentType": record.get("doc_type"),
                "expertCommittee": record.get("expert_committee"),
                "referral": record.get("referral"),
                "linkedReferrals": record.get("linked_referrals"),
                "signatureDate": record.get("signature_date"),
                "onlineDate": record.get("online_date"),
                "posted_date": record.get("online_date"),
                "keywordsRaw": record.get("keywords_raw"),
                "thematics": record.get("thematics") or [],
                "downloadUrl": record.get("download_url"),
                "pdfTextPages": self.PDF_TEXT_PAGES,
            },
        }

    def _extract_pdf_text(self, pdf_bytes, context="PDF"):
        if not pdf_bytes:
            return ""
        attempts = [
            [
                "pdftotext",
                "-layout",
                "-enc",
                "UTF-8",
                "-f",
                "1",
                "-l",
                str(self.PDF_TEXT_PAGES),
                "-",
                "-",
            ],
            [
                "pdftotext",
                "-raw",
                "-enc",
                "UTF-8",
                "-f",
                "1",
                "-l",
                str(self.PDF_TEXT_PAGES),
                "-",
                "-",
            ],
        ]
        last_error = ""
        for cmd in attempts:
            try:
                result = subprocess.run(
                    cmd,
                    input=pdf_bytes,
                    capture_output=True,
                    timeout=self.PDF_TEXT_TIMEOUT,
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                stderr = (result.stderr or b"").decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                last_error = stderr or f"pdftotext exit {result.returncode}"
            except FileNotFoundError as exc:
                last_error = str(exc)
                break
            except Exception as exc:
                last_error = str(exc)
        print(f"[{self.site_id}] {context} text extraction failed: {last_error}")
        return ""

    def _build_abstract(self, text):
        if not text:
            return ""

        patterns = (
            r"^\s*1[\.\s]+CONTEXTE[^\n]*",
            r"^\s*1\s+Contexte[^\n]*",
            r"^\s*CONTEXTE[^\n]*",
            r"^\s*RESUME[^\n]*",
            r"^\s*R.SUM.[^\n]*",
        )
        for pattern in patterns:
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
            for match in reversed(matches):
                candidate = self._compact_text(
                    text[match.start(): match.start() + self.MAX_ABSTRACT_CHARS * 2]
                )
                if len(candidate) >= self.MIN_ABSTRACT_CHARS:
                    return candidate[: self.MAX_ABSTRACT_CHARS]

        compact = self._compact_text(text)
        return compact[: self.MAX_ABSTRACT_CHARS]

    def _clean_pdf_text(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = text or ""
        text = text.replace("\x0c", "\n")
        lines = []
        for line in text.splitlines():
            line = self._normalize_text(line)
            if not line:
                if lines and lines[-1]:
                    lines.append("")
                continue
            upper = line.upper()
            folded_upper = unicodedata.normalize("NFKD", upper).encode(
                "ascii",
                errors="ignore",
            ).decode("ascii")
            if re.match(r"^page\s+\d+\s*/\s*\d+", line, flags=re.IGNORECASE):
                continue
            if folded_upper.startswith("AGENCE NATIONALE DE SECURITE SANITAIRE"):
                continue
            if upper.startswith("TEL ") or upper.startswith("TEL:"):
                continue
            if upper.startswith("ANSES/FGE/"):
                continue
            lines.append(line)
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _field_text(self, article, selector):
        node = article.select_one(selector)
        if node is None:
            return ""
        return self._normalize_text(node.get_text(" ", strip=True))

    def _date_from_time(self, article, selector):
        node = article.select_one(selector)
        if node is None:
            return ""
        return self._parse_date(node.get("datetime") or node.get_text(" ", strip=True))

    def _parse_date(self, raw):
        raw = self._normalize_text(raw)
        if not raw:
            return ""
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if match:
            return "-".join(match.groups())
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if match:
            day, month, year = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return ""

    def _split_keywords(self, raw):
        raw = self._normalize_text(raw)
        if not raw:
            return []
        parts = [self._normalize_text(part) for part in raw.split(",")]
        keywords = []
        seen = set()
        for part in parts:
            if not part:
                continue
            key = part.casefold()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(part)
        return keywords

    def _list_url(self, page):
        if page <= 0:
            return self.START_URL
        return self.LIST_ENDPOINT.format(page=page)

    def _has_next_page(self, soup):
        return soup.select_one("li.pager__item--next a[href]") is not None

    def _external_id(self, pdf_url, referral=""):
        path = urlparse(pdf_url).path
        filename = path.rstrip("/").rsplit("/", 1)[-1]
        if filename:
            return filename
        return self._normalize_text(referral)

    def _clean_url(self, url):
        url = (url or "").strip()
        if not url:
            return ""
        return url.split("?", 1)[0].split("#", 1)[0]

    def _is_pdf_url(self, url):
        return urlparse(url).path.lower().endswith(".pdf")

    def _normalize_text(self, text):
        if text is None:
            return ""
        text = unescape(str(text)).replace("\xa0", " ")
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\r", "\n")
        return re.sub(r"\s+", " ", text).strip()

    def _compact_text(self, text):
        text = self._normalize_text(text)
        return re.sub(r"\s+", " ", text).strip()
