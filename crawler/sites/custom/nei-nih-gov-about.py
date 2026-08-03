# -*- coding: utf-8 -*-
"""Crawler for NEI (National Eye Institute) Budget & Congress page.

Source: https://www.nei.nih.gov/about/budget-and-congress

The landing page is a single Drupal "Budget and Congress" page that lists:

* Congressional Justifications — links to FY-numbered PDFs (and one external
  NIH OD page for FY 2026).
* Congressional Testimony — links to per-FY testimony sub-pages on nei.nih.gov.

There is no real list/detail JSON API and no server-side pagination — every
record lives on a single HTML page. Treat the page as page 1; subsequent
"pages" yield zero new records and the loop exits cleanly.
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


class NEINihGovAboutCrawler(BaseCrawler):
    site_id = "nei-nih-gov-about"
    site_name = "Custom: nei-nih-gov-about"
    base_url = "https://www.nei.nih.gov"

    _START_URL = "https://www.nei.nih.gov/about/budget-and-congress"
    _SAFETY_PAGE_CAP = 200
    _WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # HTTP via curl (TLS issues are common on .gov hosts)
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3, max_time: int = 30) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", str(max_time),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            url,
        ]
        last_exc = None
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                last_exc = exc
            wait = 3 ** attempt  # 1s, 3s, 9s
            print(f"[{self.site_id}] GET {url} failed (attempt {attempt + 1}/{retries}); "
                  f"sleeping {wait}s. err={last_exc}")
            time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
                continue
        return None

    @staticmethod
    def _norm_ws(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def _section_text(heading) -> str:
        """Concatenate text of all siblings after `heading` until the next h2/h3."""
        parts: list[str] = []
        for sib in heading.next_siblings:
            if getattr(sib, "name", None) in ("h2", "h3"):
                break
            if hasattr(sib, "get_text"):
                t = sib.get_text(" ", strip=True)
                if t:
                    parts.append(t)
            elif isinstance(sib, str):
                t = sib.strip()
                if t:
                    parts.append(t)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    @staticmethod
    def _section_links(heading) -> list:
        anchors = []
        for sib in heading.next_siblings:
            if getattr(sib, "name", None) in ("h2", "h3"):
                break
            if hasattr(sib, "find_all"):
                anchors.extend(sib.find_all("a", href=True))
        return anchors

    # ------------------------------------------------------------------
    # Detail-page fetch (sub-pages on nei.nih.gov)
    # ------------------------------------------------------------------

    def _fetch_detail_text(self, url: str) -> tuple[str, str]:
        """Return (title, body_text) from a nei.nih.gov sub-page."""
        raw = self._curl_get(url)
        if not raw:
            return "", ""
        soup = self._make_soup(raw)
        if soup is None:
            return "", ""

        title = ""
        if soup.title and soup.title.string:
            title = self._norm_ws(soup.title.get_text(" ", strip=True))
            title = re.sub(r"\s*\|\s*National Eye Institute\s*$", "", title)

        # Try a few main content containers; fall back to <main> or <body>.
        for sel in ("main#main-content", "main", "article", "div.l-page-content"):
            container = soup.select_one(sel)
            if container is not None:
                break
        if container is None:
            container = soup.body or soup

        for tag in list(container.find_all(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"])):
            tag.decompose()

        text = self._norm_ws(container.get_text(" ", strip=True))
        return title, text

    # ------------------------------------------------------------------
    # Item extraction from the landing page
    # ------------------------------------------------------------------

    def _extract_items(self, raw: str) -> list[dict]:
        soup = self._make_soup(raw)
        if soup is None:
            return []

        main = soup.find("main") or soup
        for tag in list(main.find_all(["script", "style", "noscript"])):
            tag.decompose()

        article = main.find("article") or main

        # Walk the article's direct children in document order. The page has
        # heading-bearing ``div.c-section`` blocks followed by orphan
        # ``div.c-text`` blocks that logically belong to the most recent
        # section (the FY 2026..2017 PDF list lives in such an orphan).
        items: list[dict] = []
        current_section = ""
        current_intro = ""

        for child in article.children:
            if not hasattr(child, "find_all"):
                continue

            heading = child.find(["h2", "h3"]) if hasattr(child, "find") else None
            if heading is not None:
                title = self._norm_ws(heading.get_text(" ", strip=True))
                if title:
                    current_section = title
                    # Section intro = the c-section's text minus the heading
                    intro_parts = []
                    for sib in heading.next_siblings:
                        if hasattr(sib, "get_text"):
                            t = sib.get_text(" ", strip=True)
                            if t:
                                intro_parts.append(t)
                    current_intro = self._norm_ws(" ".join(intro_parts))

            section_low = (current_section or "").lower()
            if not any(k in section_low for k in ("justification", "testimony")):
                continue

            for a in child.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#") or href.startswith("mailto:"):
                    continue

                label = self._norm_ws(a.get_text(" ", strip=True))
                if not label:
                    continue
                # Skip generic / archive / "explore" links
                if label.lower() in ("explore our archive", "explore", "read more"):
                    continue

                abs_url = urljoin(self._START_URL, href)
                low = abs_url.lower()

                category = "Congressional Justification" if "justification" in section_low else "Congressional Testimony"

                is_pdf = low.endswith(".pdf")
                # Whitelist: NEI sub-pages OR PDFs on nei.nih.gov OR FY-2026 NIH OD link
                accept = False
                if is_pdf and "nei.nih.gov" in low:
                    accept = True
                elif "nei.nih.gov/about/budget-and-congress/" in low and abs_url != self._START_URL:
                    accept = True
                elif category == "Congressional Justification" and "officeofbudget.od.nih.gov" in low:
                    accept = True

                if not accept:
                    continue

                items.append({
                    "url": abs_url,
                    "label": label,
                    "category": category,
                    "section_intro": current_intro,
                    "is_pdf": is_pdf,
                })

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_ts = time.monotonic()
        saved = 0
        seen_urls: set[str] = set()

        page = 1
        while True:
            if limit is not None and saved >= limit:
                break
            if page > self._SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] safety page cap ({self._SAFETY_PAGE_CAP}) reached; exiting.")
                break
            if time.monotonic() - start_ts > self._WALL_CLOCK_BUDGET_SEC:
                print(f"[{self.site_id}] wall-clock budget exceeded; exiting cleanly.")
                break

            # Site has no real ?page= pagination — only page 1 yields items.
            if page == 1:
                list_url = self._START_URL
            else:
                # Try a paginator query just to be safe; will return 0 new items.
                list_url = f"{self._START_URL}?page={page - 1}"

            raw = self._curl_get(list_url)
            if raw is None:
                print(f"[{self.site_id}] page {page}: list fetch failed after retries.")
                break

            items = self._extract_items(raw)
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page}: 0 new records — done.")
                break

            limit_str = str(limit) if limit is not None else "inf"

            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start_ts > self._WALL_CLOCK_BUDGET_SEC:
                    print(f"[{self.site_id}] wall-clock budget exceeded mid-page; exiting cleanly.")
                    break

                url = it["url"]
                seen_urls.add(url)

                try:
                    paper = self._build_paper(it)
                    if paper is None:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] short abstract ({len(abstract)} chars), skipping: {url}")
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-item record assembly
    # ------------------------------------------------------------------

    def _build_paper(self, it: dict) -> dict | None:
        url = it["url"]
        label = it["label"]
        section_intro = it["section_intro"]
        category = it["category"]
        is_pdf = it["is_pdf"]

        # External_id: stable, derived from URL path
        external_id = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("nei.nih.gov/", 1)[-1]).strip("-")
        if not external_id:
            external_id = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-")
        external_id = external_id[:200]

        # Try to extract a year from the label (e.g. "FY 2025") for published_date
        year_match = re.search(r"(?:FY|fy)\s*(\d{4})", label) or re.search(r"\b(20\d{2})\b", label)
        published_date = f"{year_match.group(1)}-01-01" if year_match else ""

        title = label
        # Promote the section name into the title for clarity (it's vague otherwise)
        if not re.search(r"justification|testimony|budget|remarks|statement", title, re.I):
            title = f"{label} — {category}"

        keywords = ["NEI", "NIH", "budget", category]

        if is_pdf:
            abstract_parts = [
                f"{label} ({category}).",
                section_intro,
            ]
            abstract = self._norm_ws(" ".join(p for p in abstract_parts if p))
            pdf_url = url
            page_url = self._START_URL  # listing page is the canonical web URL
        elif "officeofbudget.od.nih.gov" in url.lower():
            abstract = self._norm_ws(
                f"{label} ({category}). External link to the NIH Office of Budget congressional "
                f"justification index. {section_intro}"
            )
            pdf_url = ""
            page_url = url
        else:
            # Sub-page on nei.nih.gov — fetch & parse for body text
            page_title, body = self._fetch_detail_text(url)
            if page_title:
                title = page_title
            abstract = body or self._norm_ws(f"{label} ({category}). {section_intro}")
            pdf_url = ""
            page_url = url

        # Truncate runaway abstracts (some NEI pages dump >50k chars of menus
        # even after stripping; cap at 20k for sanity).
        if len(abstract) > 20000:
            abstract = abstract[:20000]

        metadata = {
            "source_listing_url": self._START_URL,
            "category": category,
            "label": label,
            "is_pdf": is_pdf,
            "section_intro": section_intro[:1000],
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps(["National Eye Institute"], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": page_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": "National Eye Institute",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
