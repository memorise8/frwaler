# -*- coding: utf-8 -*-
"""WHO Publications crawler — https://www.who.int/publications/i"""

import json
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

# BeautifulSoup parser fallback chain
try:
    from bs4 import BeautifulSoup as _BS
    try:
        import html5lib  # noqa: F401
        _BS_PARSER = "html5lib"
    except ImportError:
        try:
            import lxml  # noqa: F401
            _BS_PARSER = "lxml"
        except ImportError:
            _BS_PARSER = "html.parser"
except ImportError:
    _BS = None
    _BS_PARSER = None


def _make_soup(raw):
    """Construct BeautifulSoup with fallback parsers; return None on total failure."""
    if _BS is None:
        return None
    for parser in [_BS_PARSER, "html5lib", "lxml", "html.parser"]:
        if parser is None:
            continue
        try:
            return _BS(raw, parser)
        except Exception:
            continue
    return None


class WHOPublicationsCrawler(BaseCrawler):
    """Crawler for WHO Publications (who.int/publications/i)."""

    site_id = "who-int-publications"
    site_name = "Custom: who-int-publications"
    base_url = "https://www.who.int"

    _LIST_API = (
        "https://www.who.int/api/hubs/publications"
        "?sf_site=15210d59-ad60-47ff-a542-7ed76645f0c7"
        "&sf_provider=OpenAccessProvider"
        "&sf_culture=en"
        "&$orderby=PublicationDateAndTime%20desc"
        "&$count=true"
        "&$select=Title,ItemDefaultUrl,FormatedDate,Tag,DownloadUrl"
    )
    _PAGE_SIZE = 20
    _MAX_PAGES = 200
    _WALL_CLOCK_BUDGET_S = 25 * 60  # 25 minutes
    _ABSTRACT_MIN_CHARS = 100       # skip items with shorter abstracts

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url, accept="application/json"):
        """GET via curl with exponential-backoff retry (1s, 3s, 9s)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Accept: {accept}",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                body = r.stdout.decode("utf-8", errors="replace").strip()
                if body:
                    return body
            except Exception as exc:
                print(f"[who-int-publications] curl error attempt {attempt + 1}: {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw):
        """Parse '7 May 2026' or ISO dates to 'YYYY-MM-DD'."""
        if not raw:
            return ""
        raw = raw.strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        try:
            return datetime.strptime(raw, "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
        return raw[:10] if len(raw) >= 10 else raw

    # ------------------------------------------------------------------
    # List-page fetch
    # ------------------------------------------------------------------

    def _fetch_list_page(self, skip):
        """Fetch one OData page from the list API; return parsed JSON or None."""
        url = f"{self._LIST_API}&$skip={skip}&$top={self._PAGE_SIZE}"
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Detail-page fetch + parse
    # ------------------------------------------------------------------

    def _fetch_detail(self, item_path):
        """Fetch a publication detail page and extract metadata.

        Returns a dict with keys: abstract, authors, doi, published_date,
        department, keywords.
        """
        item_id = item_path.lstrip("/")
        url = f"https://www.who.int/publications/i/item/{item_id}"
        raw = self._curl_get(url, accept="text/html,application/xhtml+xml,*/*;q=0.9")
        if not raw:
            return {}

        result = {
            "abstract": "",
            "authors": [],
            "doi": "",
            "published_date": "",
            "department": "",
            "keywords": [],
        }

        # Primary: og:description meta tag (fastest, no parser needed)
        m = re.search(
            r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
            raw,
        )
        if not m:
            m = re.search(
                r'<meta\s+content=["\']([^"\']{30,})["\'\s]+property=["\']og:description["\']',
                raw,
            )
        if m:
            result["abstract"] = m.group(1).strip()

        # JSON-LD structured data — author, doi, date, department
        for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            raw,
            re.S,
        ):
            try:
                data = json.loads(block)
                if isinstance(data, list):
                    data = data[0] if data else {}

                # Authors
                author = data.get("author")
                if author:
                    if isinstance(author, dict):
                        name = (author.get("name") or "").strip()
                        if name:
                            result["authors"] = [name]
                    elif isinstance(author, list):
                        result["authors"] = [
                            a.get("name", "").strip()
                            for a in author
                            if isinstance(a, dict) and a.get("name")
                        ]

                # DOI
                for key in ("identifier", "doi", "sameAs"):
                    val = data.get(key) or ""
                    if isinstance(val, str) and "10." in val:
                        result["doi"] = val
                        break

                # Published date
                for key in ("datePublished", "dateCreated"):
                    val = data.get(key) or ""
                    if val:
                        result["published_date"] = str(val)[:10]
                        break

                # Department / publisher
                pub = data.get("publisher") or {}
                if isinstance(pub, dict):
                    result["department"] = (pub.get("name") or "").strip()

                # Keywords
                kw = data.get("keywords") or []
                if isinstance(kw, str):
                    kw = [k.strip() for k in kw.split(",") if k.strip()]
                elif isinstance(kw, list):
                    kw = [str(k).strip() for k in kw if k]
                result["keywords"] = kw

            except (json.JSONDecodeError, AttributeError, TypeError):
                continue

        # Supplement abstract from body text if still short
        if len(result["abstract"]) < self._ABSTRACT_MIN_CHARS:
            soup = _make_soup(raw)
            if soup:
                # Look for known abstract container classes
                for tag in soup.find_all(
                    ["div", "p", "section"],
                    class_=re.compile(r"abstract|description|overview|content", re.I),
                ):
                    text = tag.get_text(separator=" ", strip=True)
                    if len(text) >= self._ABSTRACT_MIN_CHARS:
                        result["abstract"] = text
                        break

                # Fallback: any long paragraph
                if len(result["abstract"]) < self._ABSTRACT_MIN_CHARS:
                    for p in soup.find_all("p"):
                        text = p.get_text(separator=" ", strip=True)
                        if len(text) >= self._ABSTRACT_MIN_CHARS:
                            result["abstract"] = text
                            break

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl WHO publications with full pagination.

        Parameters
        ----------
        limit:
            Maximum number of papers to save. None means unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page_num in range(self._MAX_PAGES):
            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_BUDGET_S:
                print(
                    f"[who-int-publications] 25-min wall-clock budget reached "
                    f"({elapsed:.0f}s). Stopping."
                )
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety cap log
            if page_num == self._MAX_PAGES - 1:
                print(
                    f"[who-int-publications] Safety cap of {self._MAX_PAGES} "
                    f"pages reached. Stopping."
                )

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(
                    f"[who-int-publications] page {page_num}: "
                    f"saved {saved}/{limit_str}"
                )

            # Fetch list page with up to 3 retries
            data = None
            skip = page_num * self._PAGE_SIZE
            for attempt in range(3):
                data = self._fetch_list_page(skip)
                if data is not None:
                    break
                wait = [1, 3, 9][attempt]
                print(
                    f"[who-int-publications] list page {page_num} attempt "
                    f"{attempt + 1} failed, retry in {wait}s"
                )
                time.sleep(wait)

            if data is None:
                print(
                    f"[who-int-publications] Failed page {page_num} after 3 "
                    f"attempts. Stopping."
                )
                break

            items = data.get("value") or []
            if not items:
                print(f"[who-int-publications] No items at page {page_num}. Done.")
                break

            new_this_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_path = (item.get("ItemDefaultUrl") or "").strip()
                if not item_path:
                    continue

                # URL deduplication — stops silent paginator loops
                if item_path in seen_urls:
                    continue
                seen_urls.add(item_path)
                new_this_page += 1

                item_id = item_path.lstrip("/")
                item_url = f"https://www.who.int/publications/i/item/{item_id}"
                title = (item.get("Title") or "").strip()
                formatted_date = item.get("FormatedDate") or ""
                tag = item.get("Tag") or ""
                download_url = (item.get("DownloadUrl") or "").strip()

                published_date = self._parse_date(formatted_date)

                # Per-item isolation: fetch + parse + save wrapped in try/except
                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(item_path)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[who-int-publications] item {item_id} fetch failed: {exc}")
                    continue

                abstract = (detail.get("abstract") or "").strip()
                authors = detail.get("authors") or []
                doi = detail.get("doi") or ""
                detail_date = detail.get("published_date") or ""
                department = detail.get("department") or ""
                keywords = detail.get("keywords") or []

                # Prefer detail page date (more precise)
                if detail_date and len(detail_date) >= 7:
                    published_date = detail_date

                # Skip items with abstracts that are too short
                if len(abstract) < self._ABSTRACT_MIN_CHARS:
                    print(
                        f"[who-int-publications] skipping {item_id}: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                try:
                    self._save_paper({
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item_id,
                        "title": title or "(untitled)",
                        "authors": json.dumps(authors, ensure_ascii=False),
                        "abstract": abstract,
                        "category": tag,
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": published_date,
                        "url": item_url,
                        "pdf_url": download_url,
                        "doi": doi,
                        "department": department,
                        "metadata": json.dumps(
                            {
                                "tag": tag,
                                "itemDefaultUrl": item_path,
                                "formatedDate": formatted_date,
                            },
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    print(
                        f"[who-int-publications] saved {saved}/{limit_str}: "
                        f"{title[:70]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[who-int-publications] item {item_id} save failed: {exc}"
                    )
                    continue

            # If every URL on this page was already seen, paginator looped back
            if new_this_page == 0:
                print(
                    f"[who-int-publications] All items on page {page_num} "
                    f"already seen (paginator loop). Done."
                )
                break

        print(f"[who-int-publications] Done. Total saved: {saved}")
        return saved
