# -*- coding: utf-8 -*-
"""Crawler for Portugal's Diplomatic Portal press releases (English)."""

import json
import re
import subprocess
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "portaldiplomatico-mne-gov-pt-en"
_BASE_URL = "https://portaldiplomatico.mne.gov.pt"
_LIST_URL = f"{_BASE_URL}/en/communication-and-media/press-releases"
_PAGE_SIZE = 6
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3):
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            wait = [1, 3, 9][min(attempt, 2)]
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/{retries}): {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
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


def _strip_tags(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(dt_str: str) -> str:
    if not dt_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str.strip())
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list:
    items = []
    soup = _make_soup(html)

    if soup:
        for row in soup.find_all(class_=re.compile(r"\bitems-row\b")):
            blog_post = row.find(attrs={"itemprop": "blogPost"})
            if not blog_post:
                continue

            h2 = blog_post.find("h2")
            if not h2:
                continue
            a_tag = h2.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            if not href:
                continue
            url = href if href.startswith("http") else _BASE_URL + href

            time_tag = blog_post.find("time")
            listed_date = _parse_date(time_tag.get("datetime", "")) if time_tag else ""

            intro_div = blog_post.find(class_=re.compile(r"autoblog-intro"))
            intro = intro_div.get_text(separator=" ", strip=True) if intro_div else ""

            if title and url:
                items.append({
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "intro": intro,
                })
    else:
        # Regex fallback
        for row_html in re.findall(
            r'itemprop=["\']?blogPost["\']?[^>]*>(.*?)(?=itemprop=["\']?blogPost|class=["\']pagination)',
            html, re.S
        ):
            m_link = re.search(
                r'<h2[^>]*><a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([^<]+?)\s*</a>',
                row_html
            )
            if not m_link:
                continue
            href = m_link.group(1)
            title = m_link.group(2).strip()
            url = href if href.startswith("http") else _BASE_URL + href

            m_dt = re.search(r'datetime=["\']([^"\']+)["\']', row_html)
            listed_date = _parse_date(m_dt.group(1)) if m_dt else ""

            m_intro = re.search(r'autoblog-intro[^>]*>(.*?)</div>', row_html, re.S)
            intro = _strip_tags(m_intro.group(1)) if m_intro else ""

            if title and url:
                items.append({
                    "title": title,
                    "url": url,
                    "listed_date": listed_date,
                    "intro": intro,
                })

    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail_page(html: str) -> dict:
    result = {"title": "", "abstract": "", "published_date": "", "pdf_url": None}
    soup = _make_soup(html)

    if soup:
        h1 = soup.find("h1")
        if h1:
            result["title"] = h1.get_text(strip=True)

        time_tag = soup.find("time", attrs={"itemprop": "datePublished"})
        if not time_tag:
            time_tag = soup.find("time")
        if time_tag:
            result["published_date"] = _parse_date(time_tag.get("datetime", ""))

        body_div = soup.find(class_=re.compile(r"\bcom-content-article__body\b"))
        if body_div:
            result["abstract"] = body_div.get_text(separator=" ", strip=True)

        for a in soup.find_all("a", href=re.compile(r"\.pdf", re.I)):
            pdf_href = a.get("href", "")
            if pdf_href:
                result["pdf_url"] = (
                    pdf_href if pdf_href.startswith("http") else _BASE_URL + pdf_href
                )
                break
    else:
        # Regex fallback
        m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        if m_h1:
            result["title"] = _strip_tags(m_h1.group(1))

        m_dt = re.search(r'itemprop=["\']?datePublished["\']?[^>]*datetime=["\']([^"\']+)', html)
        if not m_dt:
            m_dt = re.search(r'datetime=["\']([^"\']+)["\']', html)
        if m_dt:
            result["published_date"] = _parse_date(m_dt.group(1))

        # Body ends at first </div> after the opening tag (nav is outside the div)
        m_body = re.search(r"com-content-article__body[^>]*>(.*?)</div>", html, re.S)
        if m_body:
            result["abstract"] = _strip_tags(m_body.group(1))

        m_pdf = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.I)
        if m_pdf:
            pdf_href = m_pdf.group(1)
            result["pdf_url"] = (
                pdf_href if pdf_href.startswith("http") else _BASE_URL + pdf_href
            )

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class PortalDiplomaticoEnCrawler(BaseCrawler):
    """Crawler for Portugal's Diplomatic Portal — English press releases."""

    site_id = "portaldiplomatico-mne-gov-pt-en"
    site_name = "Custom: portaldiplomatico-mne-gov-pt-en"
    base_url = "https://portaldiplomatico.mne.gov.pt"

    def crawl(self, limit=None):
        saved = 0
        page_num = 0
        seen_urls = set()
        start_time = time.time()
        limit_display = limit if limit is not None else "∞"

        while True:
            # Time-budget guard
            elapsed = time.time() - start_time
            if elapsed >= _MAX_SECONDS:
                print(f"[{_SITE_ID}] Time budget reached ({elapsed:.0f}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            start_offset = page_num * _PAGE_SIZE
            list_url = _LIST_URL if page_num == 0 else f"{_LIST_URL}?start={start_offset}"

            html = _curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch list page {page_num}. Stopping.")
                break

            try:
                items = _parse_list_page(html)
            except Exception as exc:
                print(f"[{_SITE_ID}] List-page parse error at page {page_num}: {exc}")
                page_num += 1
                continue

            if not items:
                print(f"[{_SITE_ID}] No items on page {page_num}. Done.")
                break

            # Loop-back guard: if every URL was already seen, stop
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page_num} already seen. Done.")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                slug = url.rstrip("/").rsplit("/", 1)[-1]

                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] Failed to fetch detail: {url}")
                        continue

                    detail = _parse_detail_page(detail_html)

                    title = detail.get("title") or item["title"]
                    abstract = detail.get("abstract") or item["intro"] or ""
                    abstract = re.sub(r"\s+", " ", abstract).strip()

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] Skipping — abstract too short "
                            f"({len(abstract)} chars): {title[:60]}"
                        )
                        continue

                    published_date = detail.get("published_date") or item["listed_date"]
                    listed_date = item["listed_date"]
                    pdf_url = detail.get("pdf_url")

                    original_filename = None
                    if pdf_url:
                        original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "authors": None,
                        "publisher": "Portuguese Ministry of Foreign Affairs",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Press Release",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["listed_date"],
                                "slug": slug,
                                "intro": item["intro"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

            page_num += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
