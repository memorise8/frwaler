# -*- coding: utf-8 -*-
"""NHLBI Budget and Legislative Information crawler.

Starting URL: https://www.nhlbi.nih.gov/about/budget-and-legislative-information

Sources:
  1. Main page   → Congressional Justification entries (FY 2019-2027) and Significant Items
  2. science-advances (2025) → section cards with descriptive text + PDF anchors
  3. science-advances-2023   → article sections with longer text
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# BeautifulSoup with fallback chain: html5lib → lxml → html.parser
# ---------------------------------------------------------------------------
try:
    from bs4 import BeautifulSoup as _BS
    _BS_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None  # type: ignore[assignment]
    _BS_PARSERS: list[str] = []


def _make_soup(html: str):
    """Try BS parsers in priority order; return None if all fail."""
    if _BS is None:
        return None
    for parser in _BS_PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip(text: str) -> str:
    """Remove HTML tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _curl(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS workaround; retry with exponential backoff."""
    backoff = (1, 3, 9)
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", "30",
                    "-A", (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            body = r.stdout.decode("utf-8", errors="replace")
            if body.strip():
                return body
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(backoff[attempt])
    return None


class NHLBIAboutCrawler(BaseCrawler):
    site_id = "nhlbi-nih-gov-about"
    site_name = "Custom: nhlbi-nih-gov-about"
    base_url = "https://www.nhlbi.nih.gov"

    START_URL = "https://www.nhlbi.nih.gov/about/budget-and-legislative-information"
    SA_2025_URL = "https://www.nhlbi.nih.gov/about/budget-and-legislative-information/science-advances"
    SA_2023_URL = "https://www.nhlbi.nih.gov/about/budget-and-legislative-information/science-advances-2023"

    WALL_CLOCK_MINUTES = 25
    MAX_PAGES = 200
    MIN_ABSTRACT_CHARS = 100  # must match test assertion

    _CJ_DESC = (
        "The National Heart, Lung, and Blood Institute (NHLBI) Congressional Justification "
        "is submitted annually when the President sends a budget request to Congress. "
        "Federal agencies including NIH/NHLBI prepare this document to help justify "
        "the President's request by detailing past investments and advances in research, "
        "active research programs, evolving scientific priorities, and proposed budget "
        "for heart, lung, blood, and sleep disorders and diseases research."
    )

    def crawl(self, limit=None):
        deadline = time.time() + self.WALL_CLOCK_MINUTES * 60
        saved = 0
        seen_urls: set[str] = set()

        # Each source is (name, fetcher_callable); treated as one "page" each
        sources = [
            ("main-cj", self._items_from_main_page),
            ("science-advances-2025", self._items_from_sa_2025),
            ("science-advances-2023", self._items_from_sa_2023),
        ]

        p = 0
        for src_name, fetcher in sources:
            if limit is not None and saved >= limit:
                break
            if time.time() > deadline:
                print(f"[{self.site_id}] wall-clock budget exhausted; stopping")
                break

            p += 1
            if p % 10 == 0:
                limit_or_inf = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")
            if p >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break

            try:
                items = fetcher()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] source {src_name} failed: {exc}")
                items = []

            if not items:
                print(f"[{self.site_id}] source {src_name} returned 0 items")
                continue

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() > deadline:
                    break

                url = item.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    abstract = item.get("abstract", "")
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipped (abstract {len(abstract)} chars): "
                            f"{item.get('title', '')[:50]}"
                        )
                        continue

                    time.sleep(self._delay)
                    ext_id = (
                        item.get("external_id")
                        or hashlib.md5(url.encode()).hexdigest()[:16]
                    )
                    paper = {
                        "id": ext_id,
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "title": item.get("title") or "(untitled)",
                        "authors": json.dumps([]),
                        "abstract": abstract,
                        "category": item.get("category", "Budget and Legislative Information"),
                        "keywords": json.dumps(item.get("keywords", [])),
                        "published_date": item.get("date", ""),
                        "url": url,
                        "pdf_url": item.get("pdf_url", ""),
                        "doi": "",
                        "department": "National Heart, Lung, and Blood Institute",
                        "metadata": json.dumps({
                            "source": src_name,
                            "section": item.get("section", ""),
                        }),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}: {item.get('title', '')[:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] source {src_name} had no new items")

        limit_or_inf = str(limit) if limit is not None else "inf"
        print(f"[{self.site_id}] done. Total saved: {saved}/{limit_or_inf}")
        return saved

    # -----------------------------------------------------------------------
    # Source 1: main budget page → Congressional Justification entries
    # -----------------------------------------------------------------------

    def _items_from_main_page(self) -> list[dict]:
        html = _curl(self.START_URL)
        if not html:
            print(f"[{self.site_id}] could not fetch main page")
            return []

        items: list[dict] = []
        seen_ext_ids: set[str] = set()

        # Match anchors whose text starts with "FY XXXX Congressional Justification"
        # or "FY XXXX Significant Items"
        pattern = re.compile(
            r'<a\s[^>]*href=[\"\']([^\"\']+)[\"\']\s*[^>]*>\s*'
            r'(FY\s+\d{4}[^<]*(?:Congressional\s+Justification|Significant\s+Items)[^<]*)'
            r'\s*</a>',
            re.IGNORECASE | re.DOTALL,
        )

        for m in pattern.finditer(html):
            href = m.group(1).strip()
            title = _strip(m.group(2))
            if not title:
                continue

            # Make absolute
            if href.startswith("/"):
                href = self.base_url + href

            yr_m = re.search(r"\b(\d{4})\b", title)
            year = yr_m.group(1) if yr_m else ""

            if "Congressional Justification" in title:
                category = "Congressional Justification"
            else:
                category = "Significant Items"

            pdf_url = href if ".pdf" in href.lower() else ""
            doc_url = href if not pdf_url else f"{self.START_URL}#fy{year}-cj"

            abstract = (
                f"The {title}, published by the National Heart, Lung, and Blood "
                f"Institute (NHLBI), provides Congress with detailed information on "
                f"NHLBI research priorities, investments, and outcomes for the fiscal "
                f"year. {self._CJ_DESC}"
            )

            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            ext_id = f"nhlbi-{slug}"
            if ext_id in seen_ext_ids:
                continue
            seen_ext_ids.add(ext_id)

            items.append({
                "external_id": ext_id,
                "title": title,
                "abstract": abstract,
                "category": category,
                "keywords": ["NHLBI", "budget", "congressional justification", year],
                "date": f"{year}-01-01" if year else "",
                "url": doc_url,
                "pdf_url": pdf_url,
                "section": "Congressional Budget Justifications",
            })

        print(f"[{self.site_id}] main page: {len(items)} items")
        return items

    # -----------------------------------------------------------------------
    # Source 2: science advances 2025 → section cards
    # -----------------------------------------------------------------------

    def _items_from_sa_2025(self) -> list[dict]:
        html = _curl(self.SA_2025_URL)
        if not html:
            return []
        return self._parse_sa_page(html, 2025, self.SA_2025_URL)

    # -----------------------------------------------------------------------
    # Source 3: science advances 2023 → article sections
    # -----------------------------------------------------------------------

    def _items_from_sa_2023(self) -> list[dict]:
        html = _curl(self.SA_2023_URL)
        if not html:
            return []
        return self._parse_sa_page(html, 2023, self.SA_2023_URL)

    # -----------------------------------------------------------------------
    # Shared: parse a science-advances page into item dicts
    # -----------------------------------------------------------------------

    def _parse_sa_page(self, html: str, year: int, page_url: str) -> list[dict]:
        items: list[dict] = []

        soup = _make_soup(html)
        if soup:
            items = self._parse_sa_with_soup(soup, year, page_url)

        if not items:
            items = self._parse_sa_with_regex(html, year, page_url)

        # Final fallback: whole main content as one document
        if not items:
            m = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL)
            text = _strip(m.group(1)) if m else _strip(html)
            h1 = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
            page_title = _strip(h1.group(1)) if h1 else f"NHLBI Science Advances {year}"
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                items.append({
                    "external_id": f"sa-{year}-full",
                    "title": page_title,
                    "abstract": text[:3000],
                    "category": "Science Advances",
                    "keywords": ["NHLBI", "research", str(year)],
                    "date": f"{year}-01-01",
                    "url": page_url,
                    "pdf_url": "",
                    "section": f"Science Advances {year}",
                })

        print(f"[{self.site_id}] science-advances-{year}: {len(items)} items")
        return items

    def _parse_sa_with_soup(self, soup, year: int, page_url: str) -> list[dict]:
        """Extract section cards using BeautifulSoup."""
        items: list[dict] = []
        seen_titles: set[str] = set()

        # 1. Component basic cards (used on the 2025 page style)
        cards = soup.find_all(
            class_=lambda c: c and "component_basic_card_item" in c
        )
        for card in cards:
            title_el = card.find(
                class_=lambda c: c and "component_basic_card_title" in c
            )
            desc_el = card.find(
                class_=lambda c: c and "component_basic_card_desc" in c
            )

            title = title_el.get_text(separator=" ", strip=True) if title_el else ""
            desc = desc_el.get_text(separator=" ", strip=True) if desc_el else ""

            if not title or not desc:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)

            # PDF link (may include a #page=N anchor)
            pdf_url = ""
            link_el = card.find("a", href=re.compile(r"\.pdf", re.I))
            if link_el:
                href = link_el.get("href", "")
                if href.startswith("/"):
                    href = self.base_url + href
                pdf_url = href.split("#")[0]

            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            items.append({
                "external_id": f"sa-{year}-{slug}",
                "title": f"{year}: Advancing Research — {title}",
                "abstract": desc,
                "category": "Science Advances",
                "keywords": ["NHLBI", "research", "science advances", str(year)],
                "date": f"{year}-01-01",
                "url": f"{page_url}#{slug}",
                "pdf_url": pdf_url,
                "section": f"Science Advances {year}",
            })

        # 2. Heading-based sections (used on the 2023 page)
        if not items:
            main = soup.find("main") or soup.find(id="main-content") or soup.body
            if main:
                for heading in main.find_all(["h2", "h3"]):
                    title = heading.get_text(strip=True)
                    if not title or title in seen_titles:
                        continue
                    # Skip nav-like short headings
                    if len(title) < 5:
                        continue

                    parts: list[str] = []
                    for sibling in heading.find_next_siblings():
                        if sibling.name in ["h2", "h3"]:
                            break
                        text = sibling.get_text(separator=" ", strip=True)
                        if text:
                            parts.append(text)
                        if sum(len(x) for x in parts) > 2000:
                            break

                    abstract = " ".join(parts).strip()
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        continue

                    seen_titles.add(title)
                    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    items.append({
                        "external_id": f"sa-{year}-{slug}",
                        "title": f"{year}: {title}",
                        "abstract": abstract[:2000],
                        "category": "Science Advances",
                        "keywords": ["NHLBI", "research", str(year)],
                        "date": f"{year}-01-01",
                        "url": f"{page_url}#{slug}",
                        "pdf_url": "",
                        "section": f"Science Advances {year}",
                    })

                # If headings gave nothing, use all paragraphs as one document
                if not items:
                    paras = main.find_all("p")
                    full_text = " ".join(
                        p.get_text(separator=" ", strip=True)
                        for p in paras
                        if len(p.get_text(strip=True)) > 30
                    )
                    h1 = main.find("h1") or soup.find("h1")
                    page_title = (
                        h1.get_text(strip=True) if h1
                        else f"NHLBI Science Advances {year}"
                    )
                    if len(full_text) >= self.MIN_ABSTRACT_CHARS:
                        items.append({
                            "external_id": f"sa-{year}-main",
                            "title": page_title,
                            "abstract": full_text[:3000],
                            "category": "Science Advances",
                            "keywords": ["NHLBI", "research", str(year)],
                            "date": f"{year}-01-01",
                            "url": page_url,
                            "pdf_url": "",
                            "section": f"Science Advances {year}",
                        })

        return items

    def _parse_sa_with_regex(self, html: str, year: int, page_url: str) -> list[dict]:
        """Regex fallback for parsing science-advances pages."""
        items: list[dict] = []
        seen_titles: set[str] = set()

        # Component basic card blocks
        card_re = re.compile(
            r'class=["\'][^"\']*component_basic_card_item[^"\']*["\']'
            r'(.*?)(?=class=["\'][^"\']*component_basic_card_item|</ul|</section|</div>\s*</div>)',
            re.DOTALL,
        )
        for m in card_re.finditer(html):
            block = m.group(1)

            # Title: inside component_basic_card_title
            tm = re.search(
                r'component_basic_card_title[^>]*>.*?<a[^>]*>([^<]+)</a>'
                r'|component_basic_card_title[^>]*>\s*<[^>]+>([^<]+)',
                block, re.DOTALL,
            )
            title = _strip((tm.group(1) or tm.group(2)) if tm else "")

            # Desc: inside component_basic_card_desc; may be nested
            dm = re.search(
                r'component_basic_card_desc[^>]*>(.*?)</div\s*>',
                block, re.DOTALL,
            )
            desc = _strip(dm.group(1)) if dm else ""

            # Also try string-long field pattern
            if not desc:
                sl = re.search(
                    r'field--type-string-long[^>]*>\s*<[^>]+>\s*<p>([^<]+)',
                    block,
                )
                if sl:
                    desc = sl.group(1).strip()

            # PDF URL
            pdf_url = ""
            pm = re.search(r'href=["\'](https?://[^"\']+\.pdf)[^"\']*["\']', block, re.I)
            if pm:
                pdf_url = pm.group(1).split("#")[0]

            if not title or not desc:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)

            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            items.append({
                "external_id": f"sa-{year}-{slug}",
                "title": f"{year}: Advancing Research — {title}",
                "abstract": desc,
                "category": "Science Advances",
                "keywords": ["NHLBI", "research", "science advances", str(year)],
                "date": f"{year}-01-01",
                "url": f"{page_url}#{slug}",
                "pdf_url": pdf_url,
                "section": f"Science Advances {year}",
            })

        # Heading-based sections for article-style pages (2023)
        if not items:
            m_main = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL)
            body = m_main.group(1) if m_main else html

            heading_re = re.compile(
                r'<h[23][^>]*>([^<]+)</h[23]>(.*?)(?=<h[23]|</main|</article)',
                re.DOTALL | re.IGNORECASE,
            )
            for hm in heading_re.finditer(body):
                title = _strip(hm.group(1))
                section_html = hm.group(2)
                abstract = _strip(section_html)[:2000]

                if not title or len(title) < 5:
                    continue
                if len(abstract) < self.MIN_ABSTRACT_CHARS:
                    continue
                if title in seen_titles:
                    continue
                seen_titles.add(title)

                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                items.append({
                    "external_id": f"sa-{year}-{slug}",
                    "title": f"{year}: {title}",
                    "abstract": abstract,
                    "category": "Science Advances",
                    "keywords": ["NHLBI", "research", str(year)],
                    "date": f"{year}-01-01",
                    "url": f"{page_url}#{slug}",
                    "pdf_url": "",
                    "section": f"Science Advances {year}",
                })

        return items
