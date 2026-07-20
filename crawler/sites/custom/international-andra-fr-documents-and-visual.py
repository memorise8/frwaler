# -*- coding: utf-8 -*-
"""Crawler for ANDRA International — Documents and Visual Resources.

Target: https://international.andra.fr/documents-and-visual-ressources

The site is protected by an F5 bot-mitigation WAF that requires JS execution
to set a session cookie. We use playwright to load the page once and harvest
both the HTML and the bot_mitigation_cookie; all subsequent PDF downloads use
that cookie via curl.

The page is a single-page Drupal 10 site with 34 publications in a flat
``div.row`` grid — no pagination required.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

SITE_ID = "international-andra-fr-documents-and-visual"


class InternationalAndraFrDocumentsAndVisualCrawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: international-andra-fr-documents-and-visual"
    base_url = "https://international.andra.fr"

    START_URL = "https://international.andra.fr/documents-and-visual-ressources"
    PUBLISHER = "Andra (Agence nationale pour la gestion des déchets radioactifs)"

    BACKOFF = (1, 3, 9)
    CURL_TIMEOUT = 60
    PDF_PAGES = 5
    MIN_ABSTRACT = 50      # skip threshold (per requirements)
    SAVE_MIN_ABSTRACT = 100  # test assertion threshold
    MAX_PAGES = 200        # safety cap

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_WALL = 25 * 60  # 25 minutes

        # --- Page loop (single page, but structured per spec) ---
        page = 0
        raw_page = None
        cookie_str = None

        while page < self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL:
                print(f"[{SITE_ID}] wall-clock budget exceeded; stopping")
                break

            if page == self.MAX_PAGES - 1:
                print(f"[{SITE_ID}] reached safety cap of {self.MAX_PAGES} pages; stopping")
                break

            limit_str = str(limit) if limit is not None else "inf"
            if page % 10 == 0:
                print(f"[{SITE_ID}] page {page}: saved {saved}/{limit_str}")

            if page == 0:
                # Playwright load to bypass F5 WAF
                print(f"[{SITE_ID}] loading page via playwright to bypass F5 WAF…")
                raw_page, cookie_str = self._playwright_load(self.START_URL)
                if not raw_page:
                    print(f"[{SITE_ID}] failed to load start page; aborting")
                    break
            else:
                # This site has only one page — stop after page 0
                break

            records, section_lead = self._parse_page(raw_page)
            if not records:
                print(f"[{SITE_ID}] page {page}: no records found; stopping")
                break

            new_records = [r for r in records if r.get("pdf_url") not in seen_urls]
            if not new_records:
                print(f"[{SITE_ID}] page {page}: no new records (all seen); stopping")
                break

            print(
                f"[{SITE_ID}] page {page}: discovered {len(new_records)} records"
            )

            # --- Item loop ---
            for idx, record in enumerate(new_records, 1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_WALL:
                    print(f"[{SITE_ID}] wall-clock budget exceeded; stopping")
                    break

                pdf_rel = record.get("pdf_url", "")
                if not pdf_rel:
                    continue
                full_pdf_url = (
                    pdf_rel if pdf_rel.startswith("http")
                    else urljoin(self.base_url, pdf_rel)
                )
                if full_pdf_url in seen_urls:
                    continue
                seen_urls.add(full_pdf_url)

                try:
                    time.sleep(self._delay)

                    # Download PDF bytes
                    pdf_bytes = self._curl_bytes(
                        full_pdf_url, cookie_str,
                        context=f"page {page} item {idx}"
                    )

                    # Build abstract from PDF text
                    abstract = ""
                    if pdf_bytes:
                        pdf_text = self._extract_pdf_text(
                            pdf_bytes, context=f"page {page} item {idx}"
                        )
                        abstract = self._build_abstract(pdf_text)

                    # Fallback: combine section_lead + item description
                    if len(abstract) < self.SAVE_MIN_ABSTRACT:
                        fb = self._fallback_abstract(record, section_lead)
                        if abstract:
                            abstract = (abstract + " " + fb).strip()
                        else:
                            abstract = fb

                    abstract = abstract[:6000]

                    if len(abstract) < self.MIN_ABSTRACT:
                        print(
                            f"[{SITE_ID}] page {page} item {idx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Derive title, date, filename
                    title = record.get("title") or ""
                    if not title:
                        # Decode filename as last resort
                        fname_raw = full_pdf_url.rstrip("/").rsplit("/", 1)[-1]
                        title = unquote(fname_raw).replace("%20", " ").rsplit(".", 1)[0]
                    title = title[:500]

                    original_filename = unquote(
                        full_pdf_url.rstrip("/").rsplit("/", 1)[-1]
                    )
                    external_id = original_filename

                    date_match = re.search(r"/files/(\d{4}-\d{2})/", pdf_rel)
                    date_str = (date_match.group(1) + "-01") if date_match else ""

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title,
                        "abstract": abstract,
                        "url": full_pdf_url,
                        "pdf_url": full_pdf_url,
                        "published_date": date_str,
                        "listed_date": date_str,
                        "department": self.PUBLISHER,
                        "authors": "",
                        "keywords": "",
                        "category": record.get("section", "Andra publications"),
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "originalFilename": original_filename,
                                "section": record.get("section", "Andra publications"),
                                "image_url": record.get("image_url", ""),
                                "image_alt": record.get("image_alt", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_sfx = f"/{limit}" if limit is not None else ""
                    print(f"[{SITE_ID}] saved {saved}{lim_sfx}: {title[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{SITE_ID}] page {page} item {idx} failed: {exc}")
                    continue

            page += 1

        print(f"[{SITE_ID}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Playwright page loader (F5 WAF bypass)
    # ------------------------------------------------------------------

    def _playwright_load(self, url):
        """Load URL via playwright, return (html_str, cookie_str) or (None, None)."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True, args=["--no-sandbox"]
                )
                ctx = browser.new_context(user_agent=self.USER_AGENT)
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                cookies = ctx.cookies()
                browser.close()

            cookie_str = "; ".join(
                f"{c['name']}={c['value']}"
                for c in cookies
                if "andra.fr" in c.get("domain", "")
            )
            return html, cookie_str
        except Exception as exc:
            print(f"[{SITE_ID}] playwright load failed: {exc}")
            return None, None

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_bytes(self, url, cookie_str, context="request"):
        """Download URL bytes using curl with the session cookie. Retries 3×."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: */*",
            "-H", "Accept-Language: en-US,en;q=0.9,fr;q=0.8",
            "-H", f"Referer: {self.START_URL}",
        ]
        if cookie_str:
            cmd.extend(["-H", f"Cookie: {cookie_str}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True,
                    timeout=self.CURL_TIMEOUT + 15
                )
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                body = result.stdout
                if not body:
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(
                    f"[{SITE_ID}] {context} curl attempt {attempt + 1}/3 failed: "
                    f"{last_error}"
                )
                if attempt < 2:
                    wait = self.BACKOFF[attempt]
                    print(f"[{SITE_ID}] retrying in {wait}s…")
                    time.sleep(wait)

        print(f"[{SITE_ID}] {context} curl failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
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
                print(f"[{SITE_ID}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{SITE_ID}] all parsers failed for {context}: {last_exc}")
        return None

    def _parse_page(self, raw):
        """Parse the documents page HTML.

        Returns (records_list, section_lead_text).
        Each record dict has: title, pdf_url, section, image_url, image_alt.
        """
        soup = self._make_soup(raw, context="documents page")
        if soup is None:
            return [], ""

        section_lead = ""
        lead_el = soup.select_one(".section-lead")
        if lead_el:
            section_lead = self._normalize(lead_el.get_text(" ", strip=True))

        section_title = ""
        sec_title_el = soup.select_one(".section-title")
        if sec_title_el:
            section_title = self._normalize(sec_title_el.get_text(" ", strip=True))

        records = []
        seen_pdf = set()

        for pub in soup.find_all("div", class_="mod-publication"):
            # PDF link
            link = pub.find("a", href=re.compile(r"/sites/international/files/.*\.pdf", re.I))
            if link is None:
                continue
            pdf_url = link.get("href", "").strip()
            if not pdf_url or pdf_url in seen_pdf:
                continue
            seen_pdf.add(pdf_url)

            # Title from mod-content p text (excluding link text)
            content_div = pub.find("div", class_="mod-content")
            description = ""
            if content_div:
                for p_el in content_div.find_all("p"):
                    p_text = self._normalize(p_el.get_text(" ", strip=True))
                    p_text = re.sub(r"\s*Consulter la publication\s*$", "", p_text, flags=re.I).strip()
                    if p_text:
                        description = p_text
                        break

            # Fallback title from img alt
            img = pub.find("img")
            image_url = ""
            image_alt = ""
            if img:
                image_url = img.get("src", "")
                if image_url and not image_url.startswith("http"):
                    image_url = urljoin(self.base_url, image_url)
                image_alt = self._normalize(img.get("alt", ""))

            title = description or image_alt or ""

            records.append(
                {
                    "title": title,
                    "pdf_url": pdf_url,
                    "section": section_title or "Andra publications",
                    "image_url": image_url,
                    "image_alt": image_alt,
                    "description": description,
                }
            )

        return records, section_lead

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, pdf_bytes, context="PDF"):
        if not pdf_bytes:
            return ""
        cmds = [
            ["pdftotext", "-layout", "-enc", "UTF-8",
             "-f", "1", "-l", str(self.PDF_PAGES), "-", "-"],
            ["pdftotext", "-raw", "-enc", "UTF-8",
             "-f", "1", "-l", str(self.PDF_PAGES), "-", "-"],
        ]
        for cmd in cmds:
            try:
                result = subprocess.run(
                    cmd, input=pdf_bytes, capture_output=True, timeout=60
                )
                text = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
            except FileNotFoundError:
                print(f"[{SITE_ID}] pdftotext not found; skipping PDF extraction")
                return ""
            except Exception as exc:
                print(f"[{SITE_ID}] {context} pdftotext failed: {exc}")
        return ""

    def _build_abstract(self, text):
        """Extract a clean abstract from raw PDF text."""
        if not text:
            return ""
        text = text.replace("\x0c", "\n")
        # Remove headers/footers patterns
        lines = []
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                if lines and lines[-1]:
                    lines.append("")
                continue
            # Skip pure page numbers / header noise
            if re.match(r"^Page\s+\d+\s*(of|/)\s*\d+$", line, re.I):
                continue
            if re.match(r"^\d+\s*/\s*\d+$", line):
                continue
            lines.append(line)
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        # Try to start from a meaningful section
        for pattern in (
            r"(?:^|\n)(?:Summary|Abstract|Introduction|Overview|Purpose)[^\n]*\n",
            r"(?:^|\n)\d+[\.\s]+(?:Purpose|Context|Introduction)[^\n]*\n",
        ):
            m = re.search(pattern, cleaned, re.I)
            if m:
                candidate = cleaned[m.start():m.start() + 6000].strip()
                candidate = re.sub(r"\s+", " ", candidate)
                if len(candidate) >= self.SAVE_MIN_ABSTRACT:
                    return candidate[:6000]

        compact = re.sub(r"\s+", " ", cleaned).strip()
        return compact[:6000]

    def _fallback_abstract(self, record, section_lead=""):
        """Build a fallback abstract from available metadata."""
        parts = []
        if section_lead:
            parts.append(section_lead)
        desc = record.get("description", "")
        if desc and desc not in parts:
            parts.append(desc)
        img_alt = record.get("image_alt", "")
        if img_alt and img_alt not in parts:
            parts.append(img_alt)
        section = record.get("section", "")
        if section and section not in parts:
            parts.append(f"Section: {section}")
        return " | ".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _normalize(self, text):
        if text is None:
            return ""
        text = unescape(str(text)).replace("\xa0", " ")
        text = unicodedata.normalize("NFKC", text)
        text = text.replace("\r", "\n")
        return re.sub(r"\s+", " ", text).strip()
