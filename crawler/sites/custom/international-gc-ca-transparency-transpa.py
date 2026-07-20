# -*- coding: utf-8 -*-
"""Global Affairs Canada access-to-information/privacy reports crawler."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

SITE_ID = "international-gc-ca-transparency-transpa"

# The original URL redirects to this canonical one
INDEX_URL = (
    "https://www.international.gc.ca/transparency-transparence/"
    "information-privacy-information-protection/reports-rapport/"
    "index.aspx?lang=eng"
)
BACKOFF_SECONDS = (1, 3, 9)
MIN_ABSTRACT = 50
MAX_PAGES = 200  # safety cap (this site is a single list page)


class InternationalGCTransparencyTranspaCrawler(BaseCrawler):
    site_id = "international-gc-ca-transparency-transpa"
    site_name = "Custom: international-gc-ca-transparency-transpa"
    base_url = "https://www.international.gc.ca"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page_num = 1

        # Fetch the index / list page (follows redirect automatically via -L)
        raw = self._curl_get(INDEX_URL, context="list")
        if not raw:
            print(f"[{SITE_ID}] list endpoint failed")
            return saved

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{SITE_ID}] list endpoint could not be parsed")
            return saved

        records = self._parse_list(soup)
        total = len(records)
        print(f"[{SITE_ID}] discovered {total} report links from HTML list endpoint")
        print(f"[{SITE_ID}] page {page_num}: saved {saved}/{limit if limit is not None else 'inf'}")

        for idx, record in enumerate(records, start=1):
            if limit is not None and saved >= limit:
                break

            # Wall-clock budget: 25 minutes
            if time.time() - start_time > 25 * 60:
                print(f"[{SITE_ID}] wall-clock budget exceeded after {idx - 1} items; stopping")
                break

            detail_url = record["url"]
            if not detail_url or detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            try:
                time.sleep(self._delay)
                detail_raw = self._curl_get(detail_url, context=f"item {idx}")
                if not detail_raw:
                    raise RuntimeError("detail fetch failed after retries")

                detail_soup = self._make_soup(detail_raw)
                if detail_soup is None:
                    raise RuntimeError("detail HTML could not be parsed")

                parsed = self._parse_detail(detail_soup, record)

                abstract = parsed["abstract"]
                if len(abstract) < MIN_ABSTRACT:
                    print(
                        f"[{SITE_ID}] item {idx} skipped: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": parsed["external_id"],
                    "post_number": parsed["post_number"],
                    "title": parsed["title"],
                    "abstract": abstract,
                    "published_date": parsed["published_date"],
                    "listed_date": parsed["listed_date"],
                    "authors": parsed["authors"],
                    "publisher": parsed["publisher"],
                    "department": parsed["department"],
                    "url": parsed["url"],
                    "pdf_url": parsed["pdf_url"],
                    "keywords": parsed["keywords"],
                    "category": parsed["category"],
                    "doi": None,
                    "original_filename": parsed["original_filename"],
                    "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                label = f"{saved}/{limit}" if limit is not None else str(saved)
                print(f"[{SITE_ID}] saved {label}: {parsed['title'][:80]}")

                if saved % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{SITE_ID}] page {page_num}: saved {saved}/{lim_str}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{SITE_ID}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list(self, soup: BeautifulSoup) -> list[dict]:
        """Extract all report entries from the index page."""
        # The redirected canonical URL is embedded in the page
        list_base = self._canonical_url(soup) or "https://international.canada.ca"
        main = soup.find("main") or soup
        records: list[dict] = []
        seen: set[str] = set()

        for heading in main.find_all("h2"):
            category_text = self._clean_text(heading)
            if "Access to Information Act" in category_text:
                category = "Access to Information Act"
            elif "Privacy Act" in category_text:
                category = "Privacy Act"
            else:
                continue

            section_list = heading.find_next_sibling("ul")
            if section_list is None:
                continue

            for link in section_list.find_all("a", href=True):
                title = self._clean_text(link)
                href = link.get("href", "").strip()
                if not title or not href:
                    continue

                url = href if href.startswith("http") else urljoin(list_base, href)
                if url in seen:
                    continue
                seen.add(url)

                archived = bool(link.find_next_sibling(class_="label-warning"))
                records.append({
                    "title": title,
                    "category": category,
                    "url": url,
                    "archived": archived,
                })

        return records

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, soup: BeautifulSoup, record: dict) -> dict:
        main = soup.find("main") or soup
        self._remove_noise(main)

        canonical = self._canonical_url(soup) or record["url"]

        title = (
            self._meta_content(soup, "dcterms.title")
            or self._clean_text(soup.find("h1"))
            or record["title"]
        )
        title = self._normalize_text(title)

        issued = self._meta_content(soup, "dcterms.issued")
        modified = self._meta_content(soup, "dcterms.modified") or self._date_modified(main)
        published_date = self._normalize_date(issued) or self._date_from_title(title)
        listed_date = self._normalize_date(modified) or published_date

        creator = self._meta_content(soup, "dcterms.creator") or "Global Affairs Canada"
        subject = self._meta_content(soup, "dcterms.subject") or ""

        # Refine category from title
        category = record.get("category", "Access to Information and Privacy Reports")
        if "privacy act" in title.lower():
            category = "Privacy Act"
        elif "access to information act" in title.lower():
            category = "Access to Information Act"

        # external_id from the canonical/original URL path
        original_url = self._archive_original_url(canonical) or self._archive_original_url(record["url"])
        id_source = original_url or canonical or record["url"]
        external_id = self._external_id(id_source)

        # post_number: fiscal year from title (e.g. "2024" from "2024-2025"),
        # with a suffix to differentiate ATI vs Privacy reports
        post_number = self._post_number(title, category, canonical)

        abstract, abstract_source = self._extract_abstract(main)
        pdf_url, original_filename = self._find_pdf(main, canonical)

        # keywords as comma-separated string
        kw_parts: list[str] = []
        for part in re.split(r"[,;]", subject):
            p = self._normalize_text(part)
            if p and p not in kw_parts:
                kw_parts.append(p)
        if category not in kw_parts:
            kw_parts.append(category)
        if "Global Affairs Canada" not in kw_parts:
            kw_parts.append("Global Affairs Canada")
        keywords = ",".join(kw_parts)

        metadata = {
            "posted_date": listed_date,
            "originalFilename": original_filename,
            "source_format": "html",
            "list_endpoint": INDEX_URL,
            "detail_endpoint": record["url"],
            "canonical_url": canonical,
            "original_url": original_url,
            "list_title": record["title"],
            "list_category": record["category"],
            "archived": record.get("archived", False),
            "issued_date": issued,
            "modified_date": self._normalize_date(modified),
            "abstract_source": abstract_source,
            "dcterms_subject": subject,
        }

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": creator,          # semicolon-separated (single org here)
            "publisher": "Global Affairs Canada",
            "department": "Global Affairs Canada",
            "url": canonical,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "original_filename": original_filename or None,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Abstract extraction
    # ------------------------------------------------------------------

    def _extract_abstract(self, main: BeautifulSoup) -> tuple[str, str]:
        """Extract abstract preferring the Introduction section."""
        # Look for an Introduction heading
        intro_heading = None
        for h in main.find_all(["h2", "h3"]):
            if self._clean_text(h).lower() == "introduction":
                intro_heading = h
                break

        parts: list[str] = []
        intro_level = intro_heading.name if intro_heading is not None else "h2"
        if intro_heading is not None:
            for node in intro_heading.find_all_next():
                # Stop when we hit a sibling heading of the same or higher level
                if node.name in ("h1", "h2") and node is not intro_heading:
                    break
                if node.name in ("p", "li"):
                    # Skip nodes whose parent is also a p/li (avoid double-counting)
                    if node.parent and node.parent.name in ("p", "li"):
                        continue
                    text = self._clean_text(node)
                    if self._is_meaningful_text(text):
                        parts.append(text)
                if sum(len(p) for p in parts) >= 1600:
                    break
            source = "introduction_section"
        else:
            for node in main.find_all(["p", "li"]):
                if node.parent and node.parent.name in ("p", "li"):
                    continue
                text = self._clean_text(node)
                if self._is_meaningful_text(text):
                    parts.append(text)
                if sum(len(p) for p in parts) >= 1600:
                    break
            source = "main_content"

        abstract = self._normalize_text(" ".join(parts))
        if len(abstract) > 2500:
            abstract = abstract[:2500].rsplit(" ", 1)[0].strip()
        return abstract, source

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _remove_noise(self, root: BeautifulSoup) -> None:
        for tag_name in ("script", "style", "noscript", "nav", "form"):
            for node in root.find_all(tag_name):
                node.decompose()
        for cls in (".pagedetails", ".wb-inv"):
            try:
                for node in root.select(cls):
                    node.decompose()
            except Exception:
                pass

    def _find_pdf(self, root: BeautifulSoup, base_url: str) -> tuple[str, str]:
        for link in root.find_all("a", href=True):
            href = link.get("href", "").strip()
            if ".pdf" in href.lower():
                full = href if href.startswith("http") else urljoin(base_url, href)
                filename = full.rstrip("/").split("/")[-1].split("?")[0]
                return full, filename
        return "", ""

    def _post_number(self, title: str, category: str, url: str) -> str | None:
        # Extract fiscal year range, e.g. "2024-2025" → use start year "2024"
        m = re.search(r"(20\d{2})\s*[-–—]\s*(20\d{2}|\d{2})", title + " " + url)
        if m:
            start_year = m.group(1)
        else:
            m2 = re.search(r"(20\d{2})", title + " " + url)
            start_year = m2.group(1) if m2 else None

        if not start_year:
            return None

        suffix = "privacy" if "privacy" in category.lower() else "atia"
        return f"{start_year}-{suffix}"

    def _external_id(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        slug = path.rsplit("/", 1)[-1] if path else parsed.netloc
        slug = re.sub(r"\.(aspx|html?)$", "", slug, flags=re.IGNORECASE)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-")
        if slug:
            return slug[:200]
        fallback = re.sub(r"[^A-Za-z0-9_.-]+", "-", url).strip("-")
        return fallback[:200] or "global-affairs-report"

    def _archive_original_url(self, url: str) -> str:
        m = re.search(r"/web/\d+/(https?:/.+)$", url or "")
        if m:
            return m.group(1)
        return ""

    def _canonical_url(self, soup: BeautifulSoup) -> str:
        link = soup.find("link", rel=lambda v: v and "canonical" in v)
        if link and link.get("href"):
            return link["href"].strip()
        return ""

    def _meta_content(self, soup: BeautifulSoup, name: str) -> str:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") is not None:
            return self._normalize_text(tag.get("content", ""))
        tag = soup.find("meta", attrs={"property": name})
        if tag and tag.get("content") is not None:
            return self._normalize_text(tag.get("content", ""))
        return ""

    def _date_modified(self, root: BeautifulSoup) -> str:
        tag = root.find("time", attrs={"property": "dateModified"})
        if tag:
            return self._normalize_date(self._clean_text(tag))
        return ""

    def _normalize_date(self, raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", raw)
        if m:
            y, mo, d = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return ""

    def _date_from_title(self, title: str) -> str:
        m = re.search(r"(20\d{2})\s*[-–—]\s*(20\d{2})", title or "")
        if m:
            return f"{m.group(2)}-01-01"
        m = re.search(r"(20\d{2})\s*[-–—]\s*(\d{2})", title or "")
        if m:
            return f"20{m.group(2)}-01-01"
        return ""

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--max-time", "45",
            "--connect-timeout", "15",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-CA,en;q=0.9",
        ]
        # Wayback archive returns a stub page when a User-Agent is present
        if "wayback" not in url and "webarchiveweb" not in url:
            cmd.extend(["-A", self.USER_AGENT])
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

            wait = BACKOFF_SECONDS[attempt]
            print(f"[{SITE_ID}] {context} curl failed (attempt {attempt + 1}/3): {last_error}")
            if attempt < 2:
                print(f"[{SITE_ID}] retrying in {wait}s...")
                time.sleep(wait)

        print(f"[{SITE_ID}] {context} curl failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw: str) -> BeautifulSoup | None:
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        print(f"[{SITE_ID}] all parsers failed: {last_exc}")
        return None

    def _clean_text(self, node) -> str:
        if node is None:
            return ""
        try:
            for hidden in node.select(".wb-inv"):
                hidden.decompose()
        except Exception:
            pass
        if hasattr(node, "get_text"):
            text = node.get_text(" ", strip=True)
        else:
            text = str(node)
        return self._normalize_text(text)

    def _normalize_text(self, text) -> str:
        text = unescape(str(text or ""))
        text = text.replace("\xa0", " ").replace("​", "")
        return re.sub(r"\s+", " ", text).strip()

    def _is_meaningful_text(self, text: str) -> bool:
        if len(text) < 25:
            return False
        lowered = text.lower()
        if lowered in {"table of contents", "date modified"}:
            return False
        skip_prefixes = ("skip to ", "we have archived this page")
        return not any(lowered.startswith(p) for p in skip_prefixes)
