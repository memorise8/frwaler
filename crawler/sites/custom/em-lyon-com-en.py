# -*- coding: utf-8 -*-
"""emlyon business school English newsroom crawler.

Starting point: https://em-lyon.com/en/newsroom

The site is a Drupal rendered HTML view.  The real list endpoint is the
paginated newsroom view (``/en/newsroom?page=N``); each card links to a detail
HTML page whose Drupal settings expose the native ``node/<id>`` identifier.
"""

import html
import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://em-lyon.com"
_START_URL = f"{_BASE_URL}/en/newsroom"
_MAX_PAGES = 200
_MAX_WALL_SECONDS = 25 * 60
_BACKOFF_SECONDS = (1, 3, 9)


def _curl_get(url, site_id="em-lyon-com-en", retries=3, max_time=45):
    """Fetch a URL with curl and exponential backoff."""
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--compressed",
        "--connect-timeout",
        "15",
        "--max-time",
        str(max_time),
        "-A",
        BaseCrawler.USER_AGENT,
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=max_time + 10,
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode == 0 and body.strip():
                return body
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[{site_id}] curl failed (attempt {attempt + 1}/{retries}) "
                f"for {url}: rc={result.returncode} {stderr[:200]}"
            )
        except Exception as exc:
            print(f"[{site_id}] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")

        if attempt < retries - 1:
            time.sleep(_BACKOFF_SECONDS[attempt])

    return None


def _make_soup(raw):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup

    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw or "", parser)
        except Exception as exc:
            print(f"[em-lyon-com-en] BeautifulSoup parser {parser} failed: {exc}")
    return None


def _clean_text(value):
    """Decode entities and normalize whitespace."""
    if value is None:
        return None
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _element_text(element):
    if element is None:
        return None
    return _clean_text(element.get_text(" ", strip=True))


def _iso_date(raw):
    """Best-effort conversion to YYYY-MM-DD."""
    if not raw:
        return None
    raw = _clean_text(raw)
    if not raw:
        return None

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        return match.group(0)

    raw = re.sub(r"^Published\s+in\s+", "", raw, flags=re.I)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _absolute_url(href, base_url=_BASE_URL):
    if not href:
        return None
    href = html.unescape(href.strip())
    if href.startswith(("mailto:", "tel:", "#")):
        return None
    return urljoin(base_url, href)


def _filename_from_url(url):
    if not url:
        return None
    path = urlparse(url).path
    tail = unquote(path.rstrip("/").split("/")[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _load_drupal_settings(soup):
    script = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
    if not script:
        return {}
    raw = script.string or script.get_text()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError) as exc:
        print(f"[em-lyon-com-en] drupal settings JSON failed: {exc}")
        return {}


def _meta_content(soup, *, name=None, prop=None):
    attrs = {}
    if name:
        attrs["name"] = name
    if prop:
        attrs["property"] = prop
    element = soup.find("meta", attrs=attrs)
    if not element:
        return None
    return _clean_text(element.get("content"))


def _parse_node_id(settings):
    current_path = (settings.get("path") or {}).get("currentPath")
    if not current_path:
        return None, None
    match = re.search(r"\bnode/(\d+)\b", current_path)
    return (match.group(1) if match else None), current_path


def _slug_from_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else url


def _parse_list_items(soup, list_url):
    """Extract newsroom cards from one list page."""
    items = []
    for article in soup.select("article.emlyon-big-card-content"):
        link = None
        for anchor in article.find_all("a", href=True):
            href = _absolute_url(anchor.get("href"))
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.netloc == "em-lyon.com" and parsed.path.startswith("/en/"):
                link = href
                break
        if not link:
            continue

        title = _element_text(article.select_one("h4.card-titre"))
        tracking = article.get("data-boryl-tracking-news")
        tracking_name = None
        if tracking:
            try:
                tracking_obj = json.loads(html.unescape(tracking))
                tracking_name = _clean_text(tracking_obj.get("name"))
            except (TypeError, ValueError):
                tracking_name = None
        if not title:
            title = tracking_name

        time_el = article.find("time")
        listed_raw = _element_text(time_el)
        listed_datetime = time_el.get("datetime") if time_el else None
        listed_date = _iso_date(listed_datetime or listed_raw)
        category = _element_text(article.select_one(".badge, .sub-category"))
        text_longs = [
            text
            for text in (_element_text(el) for el in article.select(".field-type--text_long"))
            if text
        ]

        authors = None
        department = None
        if category and category.lower() == "testimonials" and text_longs:
            authors = text_longs[0]
            if len(text_longs) > 1:
                department = text_longs[1]

        items.append(
            {
                "url": link,
                "title": title,
                "listed_date": listed_date,
                "listed_date_raw": listed_raw,
                "listed_datetime": listed_datetime,
                "category": category,
                "authors": authors,
                "department": department,
                "list_author_texts": text_longs,
                "tracking_name": tracking_name,
                "list_url": list_url,
            }
        )
    return items


def _next_page_url(soup):
    link = soup.find("link", rel=lambda value: value and "next" in value)
    if link and link.get("href"):
        return _absolute_url(link.get("href"))

    pager = soup.select_one('li.pager__item--next a[href], a[rel="next"][href]')
    if pager:
        return _absolute_url(pager.get("href"))
    return None


def _article_text_blocks(article, category):
    blocks = []
    for element in article.select(".field-type--text_long"):
        text = _element_text(element)
        if text:
            blocks.append(text)

    if category and category.lower() == "testimonials" and len(blocks) > 2:
        blocks = blocks[2:]

    seen = set()
    unique_blocks = []
    for block in blocks:
        key = block.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_blocks.append(block)
    return unique_blocks


def _extract_pdf(article):
    pdf_links = []
    for anchor in article.find_all("a", href=True):
        href = _absolute_url(anchor.get("href"))
        if not href:
            continue
        text = _element_text(anchor) or ""
        if ".pdf" in href.lower():
            pdf_links.append({"text": text, "url": href})

    if not pdf_links:
        return None, None, []

    pdf_url = pdf_links[0]["url"]
    return pdf_url, _filename_from_url(pdf_url), pdf_links


def _parse_detail(detail_html, item):
    soup = _make_soup(detail_html)
    if soup is None:
        raise ValueError("could not parse detail HTML")

    settings = _load_drupal_settings(soup)
    node_id, current_path = _parse_node_id(settings)
    article = soup.select_one("article.emlyon-news.full") or soup.find("article")
    if article is None:
        raise ValueError("detail article not found")

    canonical = None
    canonical_el = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical_el:
        canonical = _absolute_url(canonical_el.get("href"))

    title = _element_text(article.find("h1")) or _meta_content(soup, prop="og:title") or item.get("title")
    if not title:
        raise ValueError("missing title")

    time_el = article.find("time")
    detail_date_raw = _element_text(time_el)
    detail_datetime = time_el.get("datetime") if time_el else None
    published_date = _iso_date(detail_datetime or detail_date_raw) or item.get("listed_date")

    category = _element_text(article.select_one(".sub-category, .badge")) or item.get("category")
    meta_description = _meta_content(soup, name="description")
    og_description = _meta_content(soup, prop="og:description")

    text_blocks = _article_text_blocks(article, category)
    abstract = _clean_text("\n\n".join(text_blocks))
    if (not abstract or len(abstract) < 50) and og_description:
        abstract = og_description
    if (not abstract or len(abstract) < 50) and meta_description:
        abstract = meta_description

    if not abstract:
        abstract = ""

    pdf_url, original_filename, pdf_links = _extract_pdf(article)
    external_id = node_id or _slug_from_url(item["url"])
    post_number = node_id or _slug_from_url(item["url"])
    listed_date = item.get("listed_date") or published_date

    metadata = {
        "posted_date": item.get("listed_date_raw") or item.get("listed_datetime") or listed_date,
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "node_id": node_id,
        "drupal_current_path": current_path,
        "post_number": post_number,
        "source_path": urlparse(item["url"]).path,
        "canonical_url": canonical,
        "detail_date_raw": detail_date_raw,
        "detail_datetime": detail_datetime,
        "listed_date": listed_date,
        "listed_datetime": item.get("listed_datetime"),
        "list_url": item.get("list_url"),
        "list_title": item.get("title"),
        "list_category": item.get("category"),
        "list_author_texts": item.get("list_author_texts") or [],
        "tracking_name": item.get("tracking_name"),
        "meta_description": meta_description,
        "og_description": og_description,
        "pdf_links": pdf_links,
    }

    authors = item.get("authors")
    department = item.get("department")

    return {
        "id": f"em-lyon-com-en:{external_id}",
        "site_id": "em-lyon-com-en",
        "external_id": str(external_id),
        "post_number": str(post_number) if post_number is not None else None,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": listed_date,
        "posted_date": listed_date,
        "authors": authors,
        "publisher": "emlyon business school",
        "department": department,
        "journal": None,
        "url": canonical or item["url"],
        "pdf_url": pdf_url,
        "keywords": None,
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


class EMLyonComEnCrawler(BaseCrawler):
    site_id = "em-lyon-com-en"
    site_name = "Custom: em-lyon-com-en"
    base_url = "https://em-lyon.com"

    def crawl(self, limit=None):
        saved = 0
        page = 0
        list_url = _START_URL
        seen_urls = set()
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                break

            if page and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            raw = _curl_get(list_url, site_id=self.site_id)
            if not raw:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] page {page}: parse failed; stopping")
                break

            items = _parse_list_items(soup, list_url)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found; done")
                break

            new_items = []
            for item in items:
                item_url = item.get("url")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] page {page}: no new records; done")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget nearly reached; stopping cleanly")
                    return saved

                item_url = item.get("url")
                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(item_url, site_id=self.site_id)
                    if not detail_html:
                        print(f"[{self.site_id}] item {item_url} failed: empty response")
                        continue

                    paper = _parse_detail(detail_html, item)
                    abstract_len = len(paper.get("abstract") or "")
                    if abstract_len < 50:
                        print(f"[{self.site_id}] item {item_url} skipped: abstract too short ({abstract_len})")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:80]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            next_url = _next_page_url(soup)
            if not next_url:
                print(f"[{self.site_id}] page {page}: next page absent; done")
                break
            list_url = next_url
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
