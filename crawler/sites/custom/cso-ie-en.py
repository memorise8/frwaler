# -*- coding: utf-8 -*-
"""CSO Ireland (cso-ie-en) press release crawler.

Target: https://www.cso.ie/en/csolatestnews/pressreleases/2025pressreleases/
"""

import json
import html as htmlmod
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler
from crawler.document_identity import is_cso_press_release_url

_SITE_ID = "cso-ie-en"
_BASE_URL = "https://www.cso.ie"
_LIST_URL_TMPL = "https://www.cso.ie/en/csolatestnews/pressreleases/{year}pressreleases/"
_PUBLISHER = "Central Statistics Office"
_EARLIEST_YEAR = 2010
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url, retries=3):
    """Fetch URL via curl with retry and exponential backoff."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
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
            if result.returncode == 0 and result.stdout:
                raw = result.stdout
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) {url}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            time.sleep(wait)
    print(f"[{_SITE_ID}] all {retries} attempts failed for {url}")
    return None


# ---------------------------------------------------------------------------
# BeautifulSoup wrapper with fallback parsers
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Try html5lib → lxml → html.parser; return None on total failure."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_date_str(s):
    """Parse '- 23 December 2025' or ' - 24 November 2025' → 'YYYY-MM-DD'."""
    if not s:
        return None
    s = s.strip().lstrip("- ").strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", s)
    if not m:
        return None
    day, month_word, year = m.group(1), m.group(2).lower(), m.group(3)
    month_num = _MONTH_MAP.get(month_word)
    if not month_num:
        return None
    return f"{year}-{month_num}-{int(day):02d}"


def _parse_list_page(html):
    """Return list of {url, title, listed_date, node_id} from a year list page."""
    soup = _make_soup(html)
    if soup is not None:
        return _parse_list_bs4(soup)
    return _parse_list_regex(html)


def _module_link_item(href, label, anchor):
    """Read the current monthly modules without inventing a document number."""
    if not re.fullmatch(r'\d+_link_\d+', anchor or ''):
        return None
    url = urljoin(_BASE_URL, href)
    if not is_cso_press_release_url(url):
        return None
    match = re.fullmatch(r'(.+?)\s+-\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', label.strip())
    if not match:
        return None
    listed_date = _parse_date_str(match[2])
    try:
        datetime.strptime(listed_date or '', '%Y-%m-%d')
    except ValueError:
        return None
    # The prefix identifies a monthly CMS module; the suffix is its mutable
    # link position. Neither is the release's old numeric node ID.
    return dict(url=url, title=match[1], listed_date=listed_date,
                node_id=None, listing_anchor=anchor)


def _parse_list_bs4(soup):
    items = []
    seen = set()

    # Each press release item is preceded by an anchor span identifying the
    # node. The site's CMS migrated the anchor from name="d.en.NNNNNN" to
    # id="d.en.NNNNNN" at some point; match either for robustness.
    # It's followed by <ul class="links"><li><a href="...">title</a>
    #              <span class="externalSource">- DD Month YYYY</span></li></ul>
    anchor_re = re.compile(r"^d\.en\.\d+$")
    anchor_spans = [
        span
        for span in soup.find_all("span")
        if anchor_re.match(span.get("id") or "") or anchor_re.match(span.get("name") or "")
    ]
    for span in anchor_spans:
        raw_name = span.get("id") or span.get("name") or ""
        id_match = re.search(r"d\.en\.(\d+)", raw_name)
        if not id_match:
            continue
        node_id = id_match.group(1)

        ul = span.find_next_sibling("ul")
        if ul is None:
            continue
        ul_classes = ul.get("class") or []
        if "links" not in ul_classes:
            continue

        a_tag = ul.find("a", href=re.compile(r"/pressreleases/\d{4}pressreleases/"))
        if a_tag is None:
            continue

        href = a_tag.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        title = a_tag.get_text(" ", strip=True)

        date_span = ul.find("span", class_="externalSource")
        listed_date = None
        if date_span:
            listed_date = _parse_date_str(date_span.get_text(strip=True))

        url = (_BASE_URL + href) if not href.startswith("http") else href
        items.append({"url": url, "title": title, "listed_date": listed_date, "node_id": node_id})

    for link in soup.select('.conferencePapers .moduleBody a[href]'):
        item = _module_link_item(link['href'], link.get_text(' ', strip=True), link.get('id'))
        if item and item['url'] not in {row['url'] for row in items}:
            items.append(item)
    return items


def _parse_list_regex(html):
    """Regex fallback for list parsing when BS4 unavailable."""
    items = []
    seen = set()
    pat = re.compile(
        r'(?:id|name)="d\.en\.(\d+)"[^>]*>.*?'
        r'<ul[^>]*class="links"[^>]*>.*?'
        r'<a\s+href="(/en/csolatestnews/pressreleases/\d{4}pressreleases/[^"]+)"[^>]*>'
        r'([^<]+)</a>.*?'
        r'<span[^>]*class="externalSource"[^>]*>([^<]+)</span>',
        re.DOTALL,
    )
    for m in pat.finditer(html):
        node_id, href, title, date_str = m.groups()
        if href in seen:
            continue
        seen.add(href)
        items.append({
            "url": _BASE_URL + href,
            "title": title.strip(),
            "listed_date": _parse_date_str(date_str.strip()),
            "node_id": node_id,
        })
    for match in re.finditer(r'<a\b([^>]+)>(.*?)</a>', html, re.DOTALL | re.IGNORECASE):
        attrs = dict((m[1].lower(), htmlmod.unescape(m[3])) for m in re.finditer(
            r'\b(href|id)\s*=\s*([\"\'])(.*?)\2', match[1], re.DOTALL | re.IGNORECASE))
        label = htmlmod.unescape(re.sub(r'<[^>]+>', ' ', match[2]))
        item = _module_link_item(attrs.get('href', ''), re.sub(r'\s+', ' ', label).strip(), attrs.get('id'))
        if item and item['url'] not in {row['url'] for row in items}:
            items.append(item)
    return items


def _extract_detail(html):
    """Extract published_date, abstract, pdf_url from a press release detail page."""
    if not html:
        return {}

    # published_date from <meta property="article:published_time" content="YYYY-MM-DD">
    published_date = None
    m = re.search(r'published_time[^>]+content="(\d{4}-\d{2}-\d{2})"', html)
    if m:
        published_date = m.group(1)

    # Abstract: main body paragraphs from <div class="copy"> up to pressReleaseEnds
    abstract = _extract_abstract_bs4(html)
    if len(abstract) < 50:
        abstract = _extract_abstract_regex(html)

    # PDF URL — skip the placeholder UAT link cso.ie uses as a template
    pdf_url = None
    for raw in re.findall(r'href="(https?://[^"]+\.pdf[^"]*)"', html, re.I):
        if "uat" not in raw.lower() and "test_press_release" not in raw.lower():
            pdf_url = raw
            break

    return {"published_date": published_date, "abstract": abstract, "pdf_url": pdf_url}


def _extract_abstract_bs4(html):
    soup = _make_soup(html)
    if soup is None:
        return ""
    copy_div = soup.find("div", class_="copy")
    if not copy_div:
        return ""

    parts = []
    for elem in copy_div.find_all(["p", "h2", "h3", "h4"]):
        # Stop at the "-- ENDS --" marker
        cls = " ".join(elem.get("class") or [])
        if "pressReleaseEnds" in cls:
            break
        # Skip contact/sidebar sections
        parent = elem.find_parent(class_=re.compile(r"contact|pub-comment|related-viz"))
        if parent is not None:
            continue
        text = elem.get_text(" ", strip=True)
        if text:
            parts.append(text)

    return " ".join(parts)


def _extract_abstract_regex(html):
    """Regex fallback for abstract extraction."""
    idx_start = html.find('class="copy"')
    idx_end = html.find("pressReleaseEnds")
    if idx_start < 0 or idx_end <= idx_start:
        return ""
    chunk = html[idx_start:idx_end]
    paras = re.findall(r"<p[^>]*>(.*?)</p>", chunk, re.DOTALL)
    parts = []
    for p in paras:
        clean = re.sub(r"<[^>]+>", "", p).strip()
        if clean:
            parts.append(clean)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class CsoIeEnCrawler(BaseCrawler):
    site_id = "cso-ie-en"
    site_name = "Custom: cso-ie-en"
    base_url = "https://www.cso.ie"

    def crawl(self, limit=None):
        """Crawl CSO press releases, newest-first, walking back through years.

        Returns the number of documents saved.
        """
        self._check_cancelled()
        saved = 0
        limit_eff = limit if limit is not None else float("inf")
        seen_urls = set()
        start_time = time.time()
        page_count = 0

        current_year = datetime.now().year

        try:
            for year in range(current_year, _EARLIEST_YEAR - 1, -1):
                self._check_cancelled()
                if saved >= limit_eff:
                    break
                if time.time() - start_time > _MAX_SECONDS:
                    print(f"[{_SITE_ID}] wall-clock budget reached, stopping cleanly")
                    break
                if page_count >= _MAX_PAGES:
                    print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached")
                    break

                list_url = _LIST_URL_TMPL.format(year=year)
                html = _curl_get(list_url)
                page_count += 1

                if not html:
                    self._report_source_failure("fetch_failed")
                    print(f"[{_SITE_ID}] failed to fetch list for year {year}, stopping")
                    break

                items = _parse_list_page(html)
                if not items:
                    # An empty archive year does not prove older years are empty.
                    print(f"[{_SITE_ID}] no items found for year {year}")
                    continue

                new_items = [it for it in items if it["url"] not in seen_urls]
                if not new_items:
                    continue

                for it in new_items:
                    self._check_cancelled()
                    if saved >= limit_eff:
                        break
                    if time.time() - start_time > _MAX_SECONDS:
                        print(f"[{_SITE_ID}] wall-clock budget reached mid-page, stopping cleanly")
                        break

                    seen_urls.add(it["url"])

                    try:
                        time.sleep(self._delay)
                        detail_html = _curl_get(it["url"])
                        if not detail_html:
                            raise RuntimeError("detail fetch failed")
                        detail = _extract_detail(detail_html)

                        abstract = detail.get("abstract", "")
                        if len(abstract) < 50:
                            print(
                                f"[{_SITE_ID}] skip short abstract "
                                f"(len={len(abstract)}): {it['title'][:60]}"
                            )
                            continue

                        published_date = detail.get("published_date") or it.get("listed_date")
                        listed_date = it.get("listed_date")

                        metadata = json.dumps(
                            {
                                "node_id": it["node_id"],
                                "listing_anchor": it.get("listing_anchor"),
                                "listed_date": listed_date,
                                "year": year,
                            },
                            ensure_ascii=False,
                        )

                        paper = {
                            "site_id": self.site_id,
                            "external_id": it["node_id"],
                            "post_number": it["node_id"],
                            "url": it["url"],
                            "title": it["title"],
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": listed_date,
                            "listed_date": listed_date,
                            "publisher": _PUBLISHER,
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": None,
                            "authors": None,
                            "department": None,
                            "journal": None,
                            "doi": None,
                            "category": "Press Release",
                            "original_filename": None,
                            "metadata": metadata,
                        }

                        self._save_paper(paper)
                        if self._last_save_created:
                            saved += 1

                        if saved % 10 == 0:
                            lim_str = str(limit) if limit is not None else "inf"
                            print(
                                f"[{_SITE_ID}] page {page_count}: "
                                f"saved {saved}/{lim_str}"
                            )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        self._report_source_failure("item_failed")
                        print(f"[{_SITE_ID}] item failed ({it.get('url', '?')}): {exc}")
                        continue

                if page_count % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{_SITE_ID}] page {page_count}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{_SITE_ID}] interrupted — saved {saved} so far")
            raise

        print(f"[{_SITE_ID}] done: saved {saved} documents")
        return saved
