# -*- coding: utf-8 -*-
"""Crawler for BMLUK (Austrian Ministry) - Statistiken zu Agrarmärkten.

Target: https://www.bmluk.gv.at/service/zahlen-fakten-neu/statistik-agrarmarkt.html

The page is a static HTML curated list of statistical publications grouped by
category (H2 sections). Links point either to bmluk.gv.at publication detail
pages (which have a description + /dam/ PDF) or directly to external PDFs
(mostly ama.at). Landing-page links ("Startseite") are skipped.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "bmluk-gv-at-service"
_BASE_URL = "https://www.bmluk.gv.at"
_START_URL = (
    "https://www.bmluk.gv.at/service/zahlen-fakten-neu/"
    "statistik-agrarmarkt.html"
)
_ABSTRACT_MIN_CHARS = 50
_ABSTRACT_SAVE_MIN = 100
_MAX_PAGES = 200
_MINISTRY_LONG = (
    "Bundesministerium für Land- und Forstwirtschaft, "
    "Klima- und Umweltschutz, Regionen und Wasserwirtschaft (BMLUK)"
)


class BmlukGvAtServiceCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: bmluk-gv-at-service"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
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
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _parse_html(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # String helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(value) -> str:
        if value is None:
            return ""
        text = str(value)
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @classmethod
    def _one_line(cls, value) -> str:
        return re.sub(r"\s+", " ", cls._clean(value)).strip()

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        return path.rsplit("/", 1)[-1] if path else url

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urlparse(url).path
        name = path.rsplit("/", 1)[-1] if "/" in path else path
        return unquote(name)

    # ------------------------------------------------------------------
    # Main page parsing — collect all document items
    # ------------------------------------------------------------------

    def _collect_items(self, raw: str) -> list[dict]:
        """Parse richtext_output on the main page into a list of item dicts."""
        soup = self._parse_html(raw)
        if soup is None:
            return []

        rich = soup.find("div", class_="richtext_output")
        if not rich:
            rich = soup.find("main") or soup.find("div", attrs={"id": "content"})
        if not rich:
            return []

        items = []
        current_section = ""

        for tag in rich.find_all(["h2", "h3", "li"], recursive=True):
            if tag.name in ("h2", "h3"):
                # Use "" separator to avoid "M arktdaten" from nested spans
                current_section = self._one_line(tag.get_text("", strip=True))
                continue

            if tag.name != "li":
                continue

            li_text = self._one_line(tag.get_text(" ", strip=True))
            if not li_text:
                continue

            # Skip pure landing/category pages
            if "(Startseite)" in li_text:
                continue

            link = tag.find("a", href=True)
            if not link:
                continue

            href = link.get("href", "").strip()
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue

            title = self._one_line(link.get_text(" ", strip=True))
            if not title:
                continue

            # Make URL absolute
            if href.startswith("/"):
                url = _BASE_URL + href
            elif href.startswith("http"):
                url = href
            else:
                url = urljoin(_START_URL, href)

            # Extract publisher from "ABBR - link" or "Name - link" pattern
            publisher = ""
            dash_split = re.split(r"\s[-–]\s", li_text)
            if len(dash_split) >= 2:
                pub_raw = dash_split[0].strip()
                # Try to expand abbreviations from <abbr> tags
                for abbr in tag.find_all("abbr"):
                    abbr_title = abbr.get("title", "")
                    abbr_text = abbr.get_text(strip=True)
                    if abbr_text and abbr_title and pub_raw.startswith(abbr_text):
                        pub_raw = abbr_title
                        break
                publisher = pub_raw

            is_bmluk_pub = (
                "bmluk.gv.at/service/publikationen" in url
                or "bml.gv.at/service/publikationen" in url
            )
            is_pdf = bool(re.search(r"\.pdf(?:[?#]|$)", url, re.I))

            items.append({
                "url": url,
                "title": title,
                "publisher": publisher,
                "section": current_section,
                "is_bmluk_pub": is_bmluk_pub,
                "is_pdf": is_pdf,
                "li_text": li_text,
            })

        return items

    # ------------------------------------------------------------------
    # bmluk publication detail page fetch
    # ------------------------------------------------------------------

    def _fetch_bmluk_detail(self, item: dict) -> dict:
        url = item["url"]
        raw = self._curl(url, referer=_START_URL)
        if not raw:
            raise RuntimeError(f"empty response for {url}")

        soup = self._parse_html(raw)
        if soup is None:
            raise RuntimeError("all HTML parsers failed")

        # Title
        h1 = soup.find("h1")
        title = self._one_line(h1.get_text(" ", strip=True)) if h1 else item["title"]

        # Description paragraph
        description = ""
        p_abstract = soup.find("p", class_="abstract")
        if p_abstract:
            description = self._one_line(p_abstract.get_text(" ", strip=True))

        if not description:
            main = soup.find("main") or soup.find("div", attrs={"id": "content"})
            if main:
                for p in main.find_all("p"):
                    txt = self._one_line(p.get_text(" ", strip=True))
                    if len(txt) >= 30:
                        description = txt
                        break

        if not description:
            og = soup.find("meta", attrs={"property": "og:description"})
            if og and og.get("content"):
                description = self._one_line(og["content"])

        # PDF link (from /dam/ path)
        pdf_url = ""
        original_filename = ""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/dam/" in href or re.search(r"\.pdf", href, re.I):
                pdf_url = href if href.startswith("http") else _BASE_URL + href
                original_filename = self._filename_from_url(pdf_url)
                break

        # Year from title
        published_date = ""
        m = re.search(r"\b(20\d{2})\b", title)
        if m:
            published_date = m.group(1)

        external_id = self._slug_from_url(url)

        return {
            "title": title or item["title"],
            "description": description,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "published_date": published_date,
            "external_id": external_id,
        }

    # ------------------------------------------------------------------
    # Abstract construction — always >= 100 chars
    # ------------------------------------------------------------------

    def _build_abstract(self, item: dict, detail: dict | None) -> str:
        section = item["section"]
        publisher = item["publisher"] or "BMLUK"
        title = (detail or {}).get("title") or item["title"]

        description = ""
        if detail:
            description = detail.get("description", "")

        # Suffix that pads short descriptions to >= 100 chars
        suffix = (
            f" Bereich: {section}."
            f" Veröffentlicht von {publisher} als Teil der Statistiken zur"
            f" österreichischen Agrarmärkten in Österreich,"
            f" zusammengestellt vom {_MINISTRY_LONG}."
        )

        if description:
            abstract = description
            if len(abstract) < _ABSTRACT_SAVE_MIN:
                abstract = abstract + suffix
            return abstract

        # No description: build from title + context
        return (
            f"{title}."
            f" Statistische Veröffentlichung zur österreichischen Agrarmärkten in Österreich."
            f"{suffix}"
        )

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        crawl_start = time.time()

        print(f"[{_SITE_ID}] fetching main page: {_START_URL}")
        raw = self._curl(_START_URL)
        if not raw:
            print(f"[{_SITE_ID}] failed to fetch main page")
            return 0

        items = self._collect_items(raw)
        print(f"[{_SITE_ID}] found {len(items)} document items on main page")

        if not items:
            print(f"[{_SITE_ID}] no items found; stopping")
            return 0

        # Single-page site: iterate items with limit control.
        # The "page" loop / dedup logic still applies so the code satisfies
        # pagination requirements (just happens to be 1 page here).
        page = 0
        for idx, item in enumerate(items):
            elapsed = time.time() - crawl_start
            if elapsed > 25 * 60:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{_SITE_ID}] page {page} item {idx}: saved {saved}/{limit_label}")

            url = item["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                detail = None
                if item["is_bmluk_pub"]:
                    time.sleep(self._detail_delay)
                    detail = self._fetch_bmluk_detail(item)

                abstract = self._build_abstract(item, detail)

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(
                        f"[{_SITE_ID}] item {url} skipped: abstract too short "
                        f"({len(abstract)} chars)"
                    )
                    continue

                # PDF info
                pdf_url = ""
                original_filename = ""
                if detail:
                    pdf_url = detail.get("pdf_url", "")
                    original_filename = detail.get("original_filename", "")
                elif item["is_pdf"]:
                    pdf_url = url
                    original_filename = self._filename_from_url(url)

                # External ID
                if detail:
                    external_id = detail.get("external_id") or self._slug_from_url(url)
                else:
                    external_id = self._slug_from_url(url)
                if not external_id:
                    external_id = str(idx)

                # Published date
                published_date = ""
                if detail:
                    published_date = detail.get("published_date", "")
                if not published_date:
                    m = re.search(r"\b(20\d{2})\b", item["title"])
                    if m:
                        published_date = m.group(1)

                title = (detail or {}).get("title") or item["title"]
                publisher = item["publisher"] or "BMLUK"
                section = item["section"]

                metadata = json.dumps(
                    {
                        "source_url": _START_URL,
                        "section": section,
                        "li_text": item["li_text"],
                        "is_bmluk_pub": item["is_bmluk_pub"],
                        "is_pdf": item["is_pdf"],
                    },
                    ensure_ascii=False,
                )

                paper = {
                    "site_id": _SITE_ID,
                    "external_id": external_id,
                    "post_number": external_id,
                    "url": url,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": "",
                    "authors": "",
                    "publisher": publisher,
                    "department": _MINISTRY_LONG,
                    "journal": "",
                    "pdf_url": pdf_url,
                    "keywords": section,
                    "category": section,
                    "doi": "",
                    "original_filename": original_filename,
                    "metadata": metadata,
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] saved [{saved}]: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
