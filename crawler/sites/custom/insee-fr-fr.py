# -*- coding: utf-8 -*-
"""Crawler for INSEE (France) press releases — communiqués de presse.

List source: POST https://www.insee.fr/fr/solr/consultation
  filters: typeProduit=communiquesDePresse, sorted by dateDiffusion desc
Detail:    https://www.insee.fr/fr/information/{id}
Abstract:  extracted from the PDF attached to each detail page
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class InseeFrFrCrawler(BaseCrawler):
    site_id = "insee-fr-fr"
    site_name = "Custom: insee-fr-fr"
    base_url = "https://www.insee.fr"

    SOLR_URL = "https://www.insee.fr/fr/solr/consultation"
    ROWS_PER_PAGE = 20
    MAX_PAGES = 200
    BACKOFF = (1, 3, 9)
    CURL_TIMEOUT = 60
    PDF_PAGES = 4
    PDF_TIMEOUT = 45
    MIN_ABSTRACT = 100
    MAX_ABSTRACT = 6000
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MARKER = "__INSEE_FR_FR_CURL_META__:"

    def crawl(self, limit=None):
        saved = 0
        list_start = 0
        seen_urls = set()
        page = 0
        t0 = time.time()

        while True:
            if time.time() - t0 > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget exceeded; stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                lim = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim}")

            payload = {
                "q": "*:*",
                "start": list_start,
                "rows": self.ROWS_PER_PAGE,
                "sortFields": [{"field": "dateDiffusion", "order": "desc"}],
                "filters": [{"field": "typeProduit", "values": ["communiquesDePresse"]}],
                "facetsField": [{"field": "codeGeo", "tag": "tagCodeGeo"}],
                "facetsQuery": [],
            }

            raw = self._solr_post(payload, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Solr API failed at page {page}; stopping")
                break

            try:
                data = json.loads(raw)
            except Exception as exc:
                print(f"[{self.site_id}] JSON parse error at page {page}: {exc}")
                break

            docs = data.get("documents", [])
            if not docs:
                print(f"[{self.site_id}] no records at page {page}; stopping")
                break

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                doc_id = str(doc.get("id", "")).strip()
                if not doc_id:
                    continue

                detail_url = f"{self.base_url}/fr/information/{doc_id}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    time.sleep(self._delay)

                    html, _ = self._curl_get_text(
                        detail_url, context=f"detail {doc_id}",
                        referer=f"{self.base_url}/fr/information/2008400",
                    )
                    if not html:
                        print(f"[{self.site_id}] item {doc_id} detail fetch failed; skipping")
                        continue

                    soup = self._make_soup(html, context=f"detail {doc_id}")
                    if soup is None:
                        print(f"[{self.site_id}] item {doc_id} soup parse failed; skipping")
                        continue

                    pdf_rel = self._extract_pdf_url(soup)
                    full_pdf_url = urljoin(self.base_url, pdf_rel) if pdf_rel else None

                    abstract = ""
                    if full_pdf_url:
                        pdf_bytes, _ = self._curl_get_bytes(
                            full_pdf_url,
                            context=f"PDF {doc_id}",
                            accept="application/pdf,*/*;q=0.8",
                        )
                        if pdf_bytes:
                            raw_text = self._extract_pdf_text(pdf_bytes, context=f"PDF {doc_id}")
                            abstract = self._clean_text(raw_text)

                    if len(abstract) < self.MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] item {doc_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    title = self._normalize_text(doc.get("titre") or "")
                    if not title:
                        print(f"[{self.site_id}] item {doc_id} skipped: no title")
                        continue

                    pub_date = self._parse_date(doc.get("dateDiffusion") or "")
                    famille = doc.get("famille") or {}
                    if not isinstance(famille, dict):
                        famille = {}
                    region = doc.get("libelleGeographique") or ""
                    collection = doc.get("collection") or "Communiqués de presse"
                    famille_libelle = famille.get("libelleFr") or ""
                    sous_titre = self._normalize_text(doc.get("sousTitre") or "")

                    orig_filename = None
                    if full_pdf_url:
                        orig_filename = urlparse(full_pdf_url).path.rsplit("/", 1)[-1] or None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": doc_id,
                        "title": title,
                        "authors": None,
                        "abstract": abstract[: self.MAX_ABSTRACT],
                        "category": collection,
                        "keywords": None,
                        "published_date": pub_date,
                        "url": detail_url,
                        "pdf_url": full_pdf_url,
                        "doi": None,
                        "department": region or famille_libelle or "INSEE",
                        "original_filename": orig_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": pub_date,
                                "familleId": famille.get("id"),
                                "familleLibelle": famille_libelle,
                                "libelleGeographique": region,
                                "originalFilename": orig_filename,
                                "dateDiffusion": doc.get("dateDiffusion"),
                                "sousTitre": sous_titre or None,
                                "solrId": doc_id,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] saved {saved}/{lim}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc_id} failed: {exc}")
                    continue

            page += 1
            list_start += self.ROWS_PER_PAGE

            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _solr_post(self, payload, context="Solr"):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Content-Type: application/json; charset=utf-8",
            "-H", "Accept: application/json",
            "-H", f"Referer: {self.base_url}/fr/information/2117832",
            "-X", "POST",
            "-d", json.dumps(payload),
            self.SOLR_URL,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        (result.stderr or b"").decode("utf-8", errors="replace").strip()
                        or f"curl exit {result.returncode}"
                    )
                body = result.stdout or b""
                if not body:
                    raise RuntimeError("empty response")
                return body.decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} failed (attempt {attempt}/3): {last_error}")
                if attempt < 3:
                    w = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {w}s...")
                    time.sleep(w)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _curl_get_text(self, url, context="request", referer=None):
        body, effective_url = self._curl_get_bytes(url, context=context, referer=referer)
        if body is None:
            return None, effective_url
        return body.decode("utf-8", errors="replace"), effective_url

    def _curl_get_bytes(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,*/*;q=0.8'}",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
            "-w", "\n" + self._MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                body, http_code, effective_url = self._split_curl_output(
                    result.stdout or b"", url
                )
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                if result.returncode != 0:
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if http_code and http_code.isdigit() and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code} for {effective_url}")
                if not body:
                    raise RuntimeError("empty response")
                return body, effective_url
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{self.site_id}] {context} curl failed "
                    f"(attempt {attempt}/3): {last_error}"
                )
                if attempt < 3:
                    w = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {w}s...")
                    time.sleep(w)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None, url

    def _split_curl_output(self, raw, fallback_url):
        marker = ("\n" + self._MARKER).encode("ascii")
        pos = raw.rfind(marker)
        if pos == -1:
            return raw, "", fallback_url
        body = raw[:pos]
        meta = raw[pos + len(marker):].decode("utf-8", errors="replace").strip()
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
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    def _extract_pdf_url(self, soup):
        """Return the first PDF href from the detail page, or None."""
        link = soup.select_one("div.donnees-telechargeables a[href]")
        if link:
            href = link.get("href", "")
            if href.lower().endswith(".pdf"):
                return href
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf"):
                return href
        return None

    def _extract_pdf_text(self, pdf_bytes, context="PDF"):
        if not pdf_bytes:
            return ""
        cmds = [
            [
                "pdftotext", "-layout", "-enc", "UTF-8",
                "-f", "1", "-l", str(self.PDF_PAGES), "-", "-",
            ],
            [
                "pdftotext", "-raw", "-enc", "UTF-8",
                "-f", "1", "-l", str(self.PDF_PAGES), "-", "-",
            ],
        ]
        last_error = ""
        for cmd in cmds:
            try:
                r = subprocess.run(
                    cmd, input=pdf_bytes, capture_output=True, timeout=self.PDF_TIMEOUT
                )
                text = (r.stdout or b"").decode("utf-8", errors="replace")
                if r.returncode == 0 and text.strip():
                    return text
                last_error = (r.stderr or b"").decode("utf-8", errors="replace").strip()
            except FileNotFoundError:
                last_error = "pdftotext not found"
                break
            except Exception as exc:
                last_error = str(exc)
        print(f"[{self.site_id}] {context} PDF extraction failed: {last_error}")
        return ""

    def _clean_text(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = text or ""
        text = text.replace("\x0c", "\n")
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _parse_date(self, raw):
        if not raw:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(raw))
        if m:
            day, month, year = m.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return None

    def _normalize_text(self, text):
        if text is None:
            return ""
        text = unescape(str(text)).replace("\xa0", " ")
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\r", "\n")
        return re.sub(r"\s+", " ", text).strip()
