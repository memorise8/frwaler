# -*- coding: utf-8 -*-
"""Crawler for the SEER "Annual Report to the Nation" archive.

Source: https://seer.cancer.gov/report_to_nation/previous.html

The archive is a single static HTML page listing every historical
"Annual Report to the Nation on the Status of Cancer" entry as an
<h2> heading followed by one or more citation <p> blocks (author list,
journal citation, DOI, HTML/PDF links, "Materials to Share" link).
There is no further server-side pagination on this site — everything
lives on the one page — so the crawl loop below fetches it once and
then naturally terminates on the (nonexistent) next page.
"""

import json
import re
import subprocess
import sys
import time as time_mod
from urllib.parse import urljoin, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "seer-cancer-gov-report_to_nation"
_BASE_URL = "https://seer.cancer.gov"
_LIST_URL = "https://seer.cancer.gov/report_to_nation/previous.html"
_MAX_PAGES = 200
_MIN_ABSTRACT = 50
_MAX_WALL_SEC = 25 * 60
_PUBLISHER = "National Cancer Institute; SEER Program"
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _curl(url, retries=3):
    """Fetch a URL via curl with TLS-max 1.3 and exponential-backoff retry."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L", url],
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", "replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            time_mod.sleep(delays[attempt])
    return None


def _make_soup(html):
    """Parse HTML with a fallback parser chain; never raises."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_listed_date(html):
    """Return (iso_date, raw_str) from the page's <meta http-equiv="last_modified">."""
    m = re.search(r'http-equiv="last_modified"\s+content="([^"]+)"', html)
    if not m:
        return None, None
    raw = m.group(1)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
        try:
            import datetime
            dt = datetime.datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), raw
        except Exception:
            continue
    return None, raw


def _extract_year_slug(title):
    """Return a native slug like '1975-2019' or '1973-1995' from the h2 title."""
    m = re.search(r'(\d{4})\s*[-–—]\s*(\d{4})', title)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r'(\d{4})', title)
    if m:
        return m.group(1)
    return None


def _extract_doi(text):
    m = re.search(r'10\.\d{4,9}/[^\s\]\)"<>,]+', text)
    if not m:
        return None
    return m.group(0).rstrip(".,;")


def _extract_published_date(citation_text):
    """Best-effort ISO date from a citation string; prefers explicit 'Month Year'."""
    m = re.search(
        r'(' + "|".join(_MONTHS) + r')\.?\s+(\d{4})',
        citation_text,
    )
    if m:
        mm = _MONTHS.index(m.group(1)) + 1
        return f"{m.group(2)}-{mm:02d}-01"
    years = re.findall(r'(?:19|20)\d{2}', citation_text)
    if years:
        return f"{years[-1]}-01-01"
    return None


def _extract_journal(ps, citation_text):
    for p in ps:
        em = p.find(["em", "i"])
        if em:
            txt = em.get_text(strip=True)
            if txt:
                return txt
    m = re.search(r',\s*([A-Z][A-Za-z:&\s]{3,80}?),?\s*Volume\s+\d+', citation_text)
    if m:
        return m.group(1).strip()
    return None


def _extract_volume_issue(citation_text):
    m = re.search(r'Volume\s+(\d+),\s*Issue\s+(\d+)', citation_text)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r';\s*(\d+)\((\d+)\)', citation_text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _extract_authors(citation_text):
    m = re.search(r'^(.*?)annual\s+report\s+to\s+the\s+nation', citation_text, re.IGNORECASE)
    if m:
        raw = m.group(1).strip(" ,.")
        return raw or None
    return None


def _filename_from_url(url):
    if not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    if tail and "." in tail and len(tail) <= 200:
        return tail
    return None


def _extract_entries(html):
    """Group the archive page's <h2>/<p> siblings into entry dicts."""
    soup = _make_soup(html)
    if not soup:
        return []
    main = soup.find("main", id="content") or soup.find("main") or soup
    entries = []
    current = None
    try:
        for tag in main.find_all(["h2", "p"], recursive=True):
            if tag.name == "h2":
                if current is not None:
                    entries.append(current)
                current = {"title": tag.get_text(" ", strip=True), "ps": []}
            elif tag.name == "p":
                if current is not None:
                    current["ps"].append(tag)
        if current is not None:
            entries.append(current)
    except Exception as exc:
        print(f"[{_SITE_ID}] entry grouping error: {exc}")
    return entries


def _parse_entry(entry):
    """Turn one grouped {title, ps} entry into a fields dict, or None on failure."""
    title = entry["title"]
    ps = entry["ps"]
    if not title or not ps:
        return None

    citation_text = " ".join(p.get_text(" ", strip=True) for p in ps).strip()

    html_links, pdf_links, materials_link = [], [], None
    for p in ps:
        for a in p.find_all("a", href=True):
            cls = a.get("class") or []
            if "extlink" in cls:
                continue
            text = a.get_text(strip=True)
            if not text:
                continue
            full = urljoin(_LIST_URL, a["href"])
            low = text.lower()
            if "pdf" in low:
                pdf_links.append(full)
            elif "html" in low or "abstract" in low:
                html_links.append(full)
            elif "materials" in low or "supplement" in low:
                materials_link = materials_link or full

    slug = _extract_year_slug(title) or re.sub(r'\W+', '-', title.lower())[:40].strip("-")
    url = html_links[0] if html_links else (materials_link or f"{_LIST_URL}#{slug}")
    pdf_url = pdf_links[0] if pdf_links else None
    doi = _extract_doi(citation_text)
    journal = _extract_journal(ps, citation_text)
    volume, issue = _extract_volume_issue(citation_text)
    authors = _extract_authors(citation_text)
    published_date = _extract_published_date(citation_text)
    original_filename = _filename_from_url(pdf_url)

    subtitle = ""
    if ":" in title:
        subtitle = title.split(":", 1)[1].strip()

    return {
        "slug": slug,
        "title": title,
        "abstract": citation_text,
        "url": url,
        "pdf_url": pdf_url,
        "doi": doi,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "authors": authors,
        "published_date": published_date,
        "original_filename": original_filename,
        "keywords": subtitle,
        "html_links": html_links,
        "pdf_links": pdf_links,
        "materials_url": materials_link,
    }


class SeerCancerGovReportToNationCrawler(BaseCrawler):
    site_id = "seer-cancer-gov-report_to_nation"
    site_name = "Custom: seer-cancer-gov-report_to_nation"
    base_url = "https://seer.cancer.gov"

    def crawl(self, limit=None):
        start_t = time_mod.time()
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        seen_slugs = set()

        for p in range(1, _MAX_PAGES + 1):
            if time_mod.time() - start_t > _MAX_WALL_SEC:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached, stopping.")
                break
            if saved >= limit_val:
                break

            html = _curl(_LIST_URL) if p == 1 else None
            if not html:
                print(f"[{_SITE_ID}] page {p}: no HTML / no further pages, stopping pagination")
                break

            listed_date, listed_date_raw = _extract_listed_date(html)
            entries = _extract_entries(html)
            new_count = 0

            for entry in entries:
                if saved >= limit_val:
                    break
                try:
                    parsed = _parse_entry(entry)
                    if not parsed:
                        print(f"[{_SITE_ID}] item: could not parse entry, skipping")
                        continue

                    slug = parsed["slug"]
                    url = parsed["url"]
                    if slug in seen_slugs or url in seen_urls:
                        continue

                    abstract = parsed["abstract"]
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] item {slug}: abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    seen_slugs.add(slug)
                    seen_urls.add(url)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": parsed["title"],
                        "abstract": abstract,
                        "published_date": parsed["published_date"],
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": parsed["pdf_url"],
                        "original_filename": parsed["original_filename"],
                        "authors": parsed["authors"],
                        "publisher": _PUBLISHER,
                        "department": None,
                        "journal": parsed["journal"],
                        "keywords": parsed["keywords"],
                        "category": "Cancer Statistics Report",
                        "doi": parsed["doi"],
                        "metadata": json.dumps({
                            "posted_date": listed_date_raw,
                            "originalFilename": parsed["original_filename"],
                            "journal_raw": parsed["journal"],
                            "series": None,
                            "volume": parsed["volume"],
                            "issue": parsed["issue"],
                            "slug": slug,
                            "html_links": parsed["html_links"],
                            "pdf_links": parsed["pdf_links"],
                            "materials_url": parsed["materials_url"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    new_count += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {entry.get('title', '?')} failed: {exc}")
                    continue

                time_mod.sleep(1.0)

            if p % 10 == 0:
                lim_str = str(limit_val) if limit_val != float("inf") else "inf"
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{lim_str}")

            if new_count == 0:
                print(f"[{_SITE_ID}] page {p}: 0 new records, stopping pagination")
                break

            if p >= _MAX_PAGES:
                print(f"[{_SITE_ID}] reached {_MAX_PAGES}-page safety cap, stopping")
                break

        print(f"[{_SITE_ID}] done: saved {saved} items")
        return saved
