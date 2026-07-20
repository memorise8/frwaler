# -*- coding: utf-8 -*-
"""Crawler for Earlham Institute Newsroom — https://www.earlham.ac.uk/newsroom

Pagination: Drupal 10 infinite-scroll via AJAX endpoint (/views/ajax).
Each AJAX page returns up to 7 article cards; page 0 is the first batch.
The latest article is pinned/featured and repeats at the top of every page;
seen_urls deduplication handles it transparently.
"""

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402  absolute import

_SITE_ID = "earlham-ac-uk-newsroom"

# Map month names → zero-padded numbers for date parsing.
_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

# Drupal views AJAX endpoint — view_dom_id is stable for the newsroom page.
_AJAX_URL_TMPL = (
    "https://www.earlham.ac.uk/views/ajax"
    "?_wrapper_format=drupal_ajax"
    "&view_name=news_stories"
    "&view_display_id=block_news_stories"
    "&view_args=all"
    "&view_path=%2Fnewsroom"
    "&view_dom_id=8b6c3df7388cc406a4463a9345695be7d5d653fd5a0df3a32e3e1305e811c391"
    "&pager_element=0"
    "&page={page}"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with BS4; fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_display_date(raw: str) -> str:
    """Convert '22 April 2026' → '2026-04-22'; return '' on failure."""
    raw = raw.strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if not m:
        return ""
    day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
    mon = _MONTH_MAP.get(month_name, "")
    if not mon:
        return ""
    return f"{year}-{mon}-{int(day):02d}"


def _parse_iso_date(raw: str) -> str:
    """Extract YYYY-MM-DD from an ISO-8601 string; return '' on failure."""
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


def _strip_tags(html_text: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str, extra_headers: dict | None = None, retries: int = 3) -> str | None:
    """HTTP GET via curl with exponential-backoff retries; returns text or None."""
    cmd = [
        "curl", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
        "-H", "Accept-Language: en-US,en;q=0.9",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
            # Empty response — retry
            if attempt < retries - 1:
                wait = (attempt + 1) * 3
                print(f"[{_SITE_ID}] Empty response (attempt {attempt+1}), retry in {wait}s")
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                wait = (attempt + 1) * 3
                print(f"[{_SITE_ID}] Timeout (attempt {attempt+1}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = (attempt + 1) * 3
                print(f"[{_SITE_ID}] curl error: {exc} (attempt {attempt+1}), retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class EarlhamAcUkNewsroomCrawler(BaseCrawler):
    """Crawler for Earlham Institute Newsroom."""

    site_id = "earlham-ac-uk-newsroom"
    site_name = "Custom: earlham-ac-uk-newsroom"
    base_url = "https://www.earlham.ac.uk"

    # ------------------------------------------------------------------
    # List-page fetching
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Fetch one Drupal AJAX page; return list of article stub dicts."""
        url = _AJAX_URL_TMPL.format(page=page)
        raw = _curl_get(
            url,
            extra_headers={"Accept": "application/json, text/javascript, */*"},
        )
        if not raw:
            return []

        # Drupal sometimes wraps the AJAX JSON in <textarea>…</textarea>
        # when the client doesn't send the right XHR content-type header.
        textarea_m = re.search(r"<textarea[^>]*>(.*?)</textarea>", raw, re.DOTALL)
        if textarea_m:
            raw = textarea_m.group(1)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        # Find the insert command for the main view block.
        insert_html = ""
        for item in data:
            if item.get("command") != "insert":
                continue
            selector = item.get("selector") or ""
            if "js-view-dom-id-8b6c3d" not in selector:
                continue
            html_chunk = item.get("data") or ""
            if html_chunk:
                insert_html = html_chunk
                break

        if not insert_html:
            return []

        try:
            soup = _make_soup(insert_html)
        except Exception:
            soup = None

        if not soup:
            return []

        articles = []
        for link_tag in soup.find_all("a", class_="earlham-card-link"):
            href = (link_tag.get("href") or "").strip()
            if not href.startswith("/news/"):
                continue

            article_tag = link_tag.find("article")
            if not article_tag:
                continue

            # Node ID from aria-labelledby="card-label-NNNNN"
            labelledby = article_tag.get("aria-labelledby") or ""
            node_id = ""
            mid = re.search(r"card-label-(\d+)", labelledby)
            if mid:
                node_id = mid.group(1)

            date_div = link_tag.find(class_="card-date")
            date_raw = date_div.get_text(strip=True) if date_div else ""

            excerpt_div = link_tag.find(class_="card-excerpt")
            excerpt = excerpt_div.get_text(strip=True) if excerpt_div else ""

            title_span = link_tag.find(class_="field--name-title")
            title = title_span.get_text(strip=True) if title_span else ""

            articles.append({
                "url": self.base_url + href,
                "slug": href,
                "node_id": node_id,
                "listed_date_raw": date_raw,
                "listed_date": _parse_display_date(date_raw),
                "excerpt": excerpt,
                "title": title,
            })

        return articles

    # ------------------------------------------------------------------
    # Detail-page fetching
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict | None:
        """Fetch and parse a news article detail page; return dict or None."""
        raw = _curl_get(url)
        if not raw:
            return None

        result: dict = {
            "title": "",
            "abstract": "",
            "published_date": "",
            "listed_date": "",
            "node_id": "",
            "author": "",
            "ld_description": "",
            "notes_to_editors": "",
        }

        # JSON-LD — most reliable source for title, dates, description.
        ld_m = re.search(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            raw,
            re.DOTALL,
        )
        if ld_m:
            try:
                ld = json.loads(ld_m.group(1))
                graph = ld if isinstance(ld, list) else ld.get("@graph", [ld])
                for node in graph:
                    if not isinstance(node, dict):
                        continue
                    if node.get("@type") not in ("NewsArticle", "Article"):
                        continue
                    result["title"] = node.get("headline") or node.get("name") or ""
                    result["ld_description"] = node.get("description") or ""
                    result["published_date"] = _parse_iso_date(
                        node.get("datePublished") or ""
                    )
                    auth = node.get("author") or {}
                    if isinstance(auth, dict):
                        result["author"] = auth.get("name") or ""
                    elif isinstance(auth, list):
                        result["author"] = "; ".join(
                            a.get("name", "") for a in auth
                            if isinstance(a, dict) and a.get("name")
                        )
                    break
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass

        # Shortlink → node ID (more reliable than parsing from URL).
        sl_m = re.search(
            r'rel="shortlink"\s+href="https://www\.earlham\.ac\.uk/node/(\d+)"',
            raw,
        )
        if sl_m:
            result["node_id"] = sl_m.group(1)

        # BeautifulSoup for body content.
        try:
            soup = _make_soup(raw)
        except Exception:
            soup = None

        if soup:
            # Displayed date from node-date div.
            node_date_div = soup.find(class_="node-date")
            if node_date_div:
                result["listed_date"] = _parse_display_date(
                    node_date_div.get_text(strip=True)
                )

            # Title fallback from <h1>.
            if not result["title"]:
                h1 = soup.find("h1")
                if h1:
                    result["title"] = h1.get_text(strip=True)

            # Excerpt paragraph (short intro shown under headline).
            excerpt_p = soup.find(class_="node-excerpt")
            excerpt_text = excerpt_p.get_text(strip=True) if excerpt_p else ""

            # Body paragraphs from the main content columns.
            body_parts: list[str] = []
            for div in soup.find_all(
                lambda tag: (
                    tag.name == "div"
                    and "vsc-wysiwyg" in (tag.get("class") or [])
                    and "main-content-text" in (tag.get("class") or [])
                )
            ):
                for p_tag in div.find_all("p"):
                    txt = p_tag.get_text(strip=True)
                    if txt and len(txt) > 20:
                        body_parts.append(txt)

            # Notes to editors (contains institutional boilerplate — useful context).
            notes_div = soup.find(
                lambda tag: (
                    tag.name
                    and "field--name-field-notes-to-editors" in " ".join(
                        tag.get("class") or []
                    )
                )
            )
            if notes_div:
                result["notes_to_editors"] = notes_div.get_text(
                    separator=" ", strip=True
                )[:600]

            # Assemble abstract: excerpt first, then body paragraphs (dedup).
            parts: list[str] = []
            seen_parts: set[str] = set()
            if excerpt_text:
                parts.append(excerpt_text)
                seen_parts.add(excerpt_text)
            for bt in body_parts:
                if bt not in seen_parts:
                    parts.append(bt)
                    seen_parts.add(bt)
            result["abstract"] = "\n\n".join(parts)

        else:
            # Regex fallback when BS4 is unavailable.
            exc_m = re.search(
                r'class="node-excerpt"[^>]*>\s*(.*?)\s*</p>', raw, re.DOTALL
            )
            if exc_m:
                result["abstract"] = _strip_tags(exc_m.group(1)).strip()

        # If abstract is still short, supplement with JSON-LD description.
        if len(result["abstract"]) < 100 and result["ld_description"]:
            if result["abstract"]:
                result["abstract"] = (
                    result["ld_description"] + "\n\n" + result["abstract"]
                )
            else:
                result["abstract"] = result["ld_description"]

        return result

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Paginate the Earlham newsroom and save articles.

        Parameters
        ----------
        limit:
            Maximum number of articles to save. None = unlimited.
        """
        limit_val = float("inf") if limit is None else limit
        saved = 0
        seen_urls: set[str] = set()
        page = 0
        max_pages = 200
        start_time = time.time()
        max_wall_seconds = 25 * 60  # 25-minute budget

        while page <= max_pages:
            # Wall-clock safety valve.
            if time.time() - start_time > max_wall_seconds:
                print(
                    f"[{_SITE_ID}] Wall-clock budget reached at page {page}. Stopping."
                )
                break

            if saved >= limit_val:
                break

            if page > 0 and page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(
                    f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}"
                )

            articles = self._fetch_list_page(page)
            new_articles = [a for a in articles if a["url"] not in seen_urls]
            for a in articles:
                seen_urls.add(a["url"])

            if not new_articles:
                print(f"[{_SITE_ID}] No new articles at page {page}. Done.")
                break

            if page == max_pages:
                print(f"[{_SITE_ID}] Safety cap of {max_pages} pages reached.")
                break

            for article in new_articles:
                if saved >= limit_val:
                    break

                url = article["url"]
                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(url)
                    if not detail:
                        print(f"[{_SITE_ID}] Failed to fetch detail: {url}")
                        continue

                    title = detail.get("title") or article.get("title", "")
                    abstract = detail.get("abstract", "") or article.get("excerpt", "")

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] Abstract too short (<50 chars) for {url}, skipping"
                        )
                        continue

                    node_id = detail.get("node_id") or article.get("node_id", "")
                    external_id = node_id or article["slug"].lstrip("/news/")
                    published_date = (
                        detail.get("published_date") or article.get("listed_date", "")
                    )
                    listed_date = (
                        detail.get("listed_date") or article.get("listed_date", "")
                    )
                    author = detail.get("author") or "Earlham Institute"

                    metadata = {
                        "posted_date": article.get("listed_date_raw", ""),
                        "node_id": node_id,
                        "slug": article["slug"],
                        "notes_to_editors": detail.get("notes_to_editors", ""),
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id if node_id else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "authors": author,
                        "publisher": "Earlham Institute",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "News",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{_SITE_ID}] Saved {counter}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
