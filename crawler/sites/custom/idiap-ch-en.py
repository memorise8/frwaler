# -*- coding: utf-8 -*-
"""Crawler for Idiap Research Institute – Scientific Data catalogue.

Target: https://www.idiap.ch/en/scientific-research/data
Backend: Plone CMS with eea.facetednavigation (@@faceted_query endpoint).
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.idiap.ch/en/scientific-research/data/@@faceted_query"
_PAGE_SIZE = 20
_MAX_PAGES = 200
_MAX_WALL_SECS = 25 * 60  # 25 minutes


def _make_soup(html: str):
    """Parse HTML with fallback chain; returns soup or None on complete failure."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl (bypasses TLS issues). Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(3 ** attempt)
            else:
                print(f"[idiap-ch-en] curl failed for {url}: {exc}")
    return None


def _parse_list_page(html: str) -> list[dict]:
    """Return list of {url, title, short_desc, slug} from a @@faceted_query page."""
    soup = _make_soup(html)
    if soup is None:
        return []
    entries = []
    for item in soup.find_all("div", class_="photoAlbumEntry"):
        a = item.find("a")
        if not a:
            continue
        href = a.get("href", "")
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.idiap.ch" + href

        title_el = a.find("span", class_="photoAlbumEntryTitle")
        desc_el = a.find("span", class_="photoAlbumEntryDescription")
        title = title_el.get_text(strip=True) if title_el else ""
        short_desc = desc_el.get_text(strip=True) if desc_el else ""

        # Extract slug: URL ends with /slug/index_html or /slug
        parts = href.rstrip("/").split("/")
        if parts and parts[-1] == "index_html":
            slug = parts[-2] if len(parts) >= 2 else parts[-1]
        else:
            slug = parts[-1]

        entries.append({"url": href, "title": title, "short_desc": short_desc, "slug": slug})
    return entries


def _section_texts(text_div) -> dict[str, str]:
    """
    Walk children of div#parent-fieldname-text and group text by h3 section.
    Returns {section_name: combined_text, ...}.
    Also returns key "__preamble__" for text before the first h3.
    """
    sections: dict[str, list[str]] = {"__preamble__": []}
    current = "__preamble__"
    for tag in text_div.children:
        if not hasattr(tag, "name"):
            # NavigableString
            txt = str(tag).strip()
            if txt:
                sections[current].append(txt)
            continue
        if tag.name == "h3":
            header = tag.get_text(strip=True)
            current = header
            sections.setdefault(current, [])
        elif tag.name in ("p", "ul", "ol", "div", "table"):
            txt = tag.get_text(separator=" ", strip=True)
            if txt:
                sections[current].append(txt)
        elif tag.name == "hr":
            pass  # ignore horizontal rules
    return {k: " ".join(v).strip() for k, v in sections.items()}


def _parse_authors_from_citation(citation: str) -> tuple[str | None, str | None]:
    """
    Try to extract (authors_str, year) from a citation like:
      "Alice Foo and Bob Bar, Title of Thing, Idiap Technical Report, 2026."
    Returns (None, None) if parsing fails.
    """
    # Match: everything before first standalone ", YYYY" at end
    m = re.search(r"(.+?),\s*.+?,\s*.+?,\s*(\d{4})\.?\s*$", citation)
    if not m:
        # Simpler: just grab author portion before first comma and year
        m2 = re.search(r"^(.+?),", citation)
        yr = re.search(r"\b(19|20)\d{2}\b", citation)
        if m2 and yr:
            raw_authors = m2.group(1).strip()
        else:
            return None, None
        year = yr.group(0) if yr else None
        raw_authors = m2.group(1).strip() if m2 else None
    else:
        raw_authors = m.group(1).strip()
        year = m.group(2)

    if not raw_authors:
        return None, None

    # Split on " and " first, then on "," for multi-author
    parts = re.split(r"\s+and\s+", raw_authors)
    # Each part may still have commas (last-name, first-name) — keep as-is if short
    authors_list = []
    for part in parts:
        part = part.strip()
        if part:
            authors_list.append(part)
    authors_str = "; ".join(authors_list) if authors_list else None
    return authors_str, year


def _parse_detail(html: str, url: str) -> dict | None:
    """Parse a detail page and return a paper dict, or None on hard failure."""
    soup = _make_soup(html)
    if soup is None:
        return None

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Short description
    desc_el = soup.find("div", class_="documentDescription")
    short_desc = desc_el.get_text(strip=True) if desc_el else ""

    # Slug from URL for external_id
    parts = url.rstrip("/").split("/")
    slug = parts[-2] if (parts and parts[-1] == "index_html") else parts[-1]
    slug = slug or url.split("/")[-1]

    # Main content block
    text_div = soup.find("div", id="parent-fieldname-text")
    abstract = ""
    authors = None
    published_date = None
    pub_url = None
    get_data_url = None

    if text_div:
        # Get Data link (could be Zenodo, GitHub, etc.)
        for a in text_div.find_all("a"):
            atxt = a.get_text(strip=True).lower()
            if "get data" in atxt or "download" in atxt:
                href = a.get("href", "")
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = "https://www.idiap.ch" + href
                if href:
                    get_data_url = href
                    break

        # Section-based parsing
        secs = _section_texts(text_div)

        # Abstract = Description section; fallback to preamble or short_desc
        if "Description" in secs and secs["Description"]:
            abstract = secs["Description"]
        else:
            # Try first non-empty section
            for k, v in secs.items():
                if k != "__preamble__" and v and len(v) >= 50:
                    abstract = v
                    break
            if not abstract:
                abstract = secs.get("__preamble__", "") or short_desc

        # Reference section → authors + year
        ref_text = secs.get("Reference", "")
        if ref_text:
            # Find the citation line: after "please cite the following publication:"
            lines = [ln.strip() for ln in ref_text.split("  ") if ln.strip()]
            for line in lines:
                if re.search(r"\b(19|20)\d{2}\b", line) and "," in line:
                    a_str, yr = _parse_authors_from_citation(line)
                    if a_str:
                        authors = a_str
                    if yr:
                        published_date = yr
                    break

        # Publication URL on publications.idiap.ch
        for a in text_div.find_all("a"):
            href = a.get("href", "")
            if "publications.idiap.ch" in href:
                pub_url = href
                break

    metadata_dict: dict = {}
    if short_desc:
        metadata_dict["short_description"] = short_desc
    if pub_url:
        metadata_dict["publication_url"] = pub_url
    if get_data_url:
        metadata_dict["get_data_url"] = get_data_url

    return {
        "external_id": slug,
        "post_number": slug,
        "title": title or short_desc,
        "abstract": abstract,
        "authors": authors,
        "published_date": published_date,
        "url": url,
        "pdf_url": get_data_url,
        "publisher": "Idiap Research Institute",
        "metadata": metadata_dict,
        "short_desc": short_desc,
    }


class IdiapDataCrawler(BaseCrawler):
    site_id = "idiap-ch-en"
    site_name = "Custom: idiap-ch-en"
    base_url = "https://www.idiap.ch"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        b_start = 0
        page_num = 0

        while page_num < _MAX_PAGES:
            if time.time() - start_time > _MAX_WALL_SECS:
                print(f"[idiap-ch-en] 25-min wall-clock budget reached at page {page_num}; stopping.")
                break

            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[idiap-ch-en] page {page_num}: saved {saved}/{limit_str}")

            list_html = _curl_get(
                f"{_LIST_URL}?b_size={_PAGE_SIZE}&b_start={b_start}"
            )
            if not list_html:
                print(f"[idiap-ch-en] Failed to fetch list page b_start={b_start}; stopping.")
                break

            entries = _parse_list_page(list_html)
            if not entries:
                print(f"[idiap-ch-en] No entries at b_start={b_start}; done.")
                break

            for entry in entries:
                if limit is not None and saved >= limit:
                    break

                item_url = entry["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[idiap-ch-en] item {item_url} failed: no response")
                        continue

                    detail = _parse_detail(detail_html, item_url)
                    if not detail:
                        print(f"[idiap-ch-en] item {item_url} failed: parse returned None")
                        continue

                    abstract = detail.get("abstract", "")
                    if not abstract or len(abstract) < 50:
                        print(f"[idiap-ch-en] item {item_url}: abstract too short ({len(abstract)} chars), skipping")
                        continue

                    title = detail["title"] or entry["title"]
                    if not title:
                        title = entry["short_desc"] or "(untitled)"

                    paper = {
                        "site_id": self.site_id,
                        "external_id": detail["external_id"],
                        "post_number": detail["post_number"],
                        "title": title,
                        "abstract": abstract,
                        "authors": detail.get("authors"),
                        "published_date": detail.get("published_date"),
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "publisher": detail.get("publisher", "Idiap Research Institute"),
                        "keywords": None,
                        "category": "research-datasets",
                        "doi": None,
                        "original_filename": None,
                        "metadata": detail.get("metadata", {}),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[idiap-ch-en] item {item_url} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            b_start += _PAGE_SIZE
            page_num += 1

        if page_num >= _MAX_PAGES:
            print(f"[idiap-ch-en] Safety cap of {_MAX_PAGES} pages reached; stopping.")

        return saved
