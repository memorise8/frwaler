# -*- coding: utf-8 -*-
"""Spanish Ministry of Foreign Affairs (exteriores.gob.es/en) official publications crawler.

Starting URL: https://www.exteriores.gob.es/en/ServiciosAlCiudadano/Paginas/Publicaciones-oficiales.aspx
Pagination:   ?p=N  (12 items/page, ~5 pages, ~60 total)
Items link directly to PDFs; abstract comes from the list-page snippet.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import unquote

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_LIST_URL = (
    "https://www.exteriores.gob.es/en/ServiciosAlCiudadano"
    "/Paginas/Publicaciones-oficiales.aspx"
)
_BASE = "https://www.exteriores.gob.es"
_PUBLISHER = "Ministry of Foreign Affairs, European Union and Cooperation of Spain"
_SITE_ID = "exteriores-gob-es-en"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML; fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and exponential-backoff retries (1 s, 3 s, 9 s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9,es;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _parse_list_page(html: str) -> list[dict]:
    """Extract publication dicts from one list page."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] page parse error: {exc}")
        return []

    items = []
    for article in soup.find_all("article"):
        try:
            # Date: <time datetime="YYYY-MM-DD">
            time_tag = article.find("time")
            date_str = ""
            if time_tag:
                date_str = (time_tag.get("datetime") or "").strip().strip('"')

            # Link + title: <h2><a href="...PDF">title (X.XX MB)</a></h2>
            h2 = article.find("h2")
            a_tag = h2.find("a", href=True) if h2 else None
            if not a_tag:
                a_tag = article.find("a", href=True)
            if not a_tag:
                continue

            href = a_tag.get("href", "").strip()
            if not href:
                continue
            if href.startswith("/"):
                pdf_url = _BASE + href
            elif href.startswith("http"):
                pdf_url = href
            else:
                pdf_url = _BASE + "/" + href.lstrip("/")

            # Strip "(X.XX MB)" file-size annotations and icon text from link text
            raw_text = a_tag.get_text(separator=" ", strip=True)
            title = re.sub(
                r'\(\s*\d+[\.,]\d+\s*[KMG]B\s*\)', '', raw_text, flags=re.IGNORECASE
            )
            title = re.sub(r'\s+', ' ', title).strip()

            # Abstract: <div class="cardType4__text"><p>...</p></div>
            text_div = article.find(class_=re.compile(r'cardType4__text', re.IGNORECASE))
            abstract = ""
            if text_div:
                p_tag = text_div.find("p")
                if p_tag:
                    abstract = p_tag.get_text(strip=True)

            filename = (
                unquote(href.rstrip("/").split("/")[-1].split("?")[0])
                if href else None
            )

            items.append({
                "date": date_str,
                "title": title,
                "pdf_url": pdf_url,
                "abstract": abstract,
                "filename": filename,
            })

        except Exception as exc:
            print(f"[{_SITE_ID}] article parse error: {exc}")
            continue

    return items


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ExterioresGobEsEnCrawler(BaseCrawler):
    """Crawler for Spanish MFA official publications (exteriores.gob.es/en)."""

    site_id = "exteriores-gob-es-en"
    site_name = "Custom: exteriores-gob-es-en"
    base_url = "https://www.exteriores.gob.es"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        max_wall = 25 * 60
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(1, max_pages + 1):
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            url = _LIST_URL if page_num == 1 else f"{_LIST_URL}?p={page_num}"

            raw = _curl_get(url)
            if not raw:
                print(f"[{_SITE_ID}] page {page_num}: failed to fetch. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no articles found. Done.")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    title = item["title"]
                    abstract = item["abstract"]

                    if len(abstract) < 100:
                        print(
                            f"[{_SITE_ID}] skipping (abstract {len(abstract)} chars): "
                            f"{title[:60]}"
                        )
                        continue

                    date_str = item["date"]
                    filename = item["filename"]
                    external_id = filename or pdf_url.split("/")[-1]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": None,
                        "title": title,
                        "abstract": abstract,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": _PUBLISHER,
                        "journal": "",
                        "category": "Official Publications",
                        "keywords": "",
                        "published_date": date_str,
                        "listed_date": date_str,
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "original_filename": filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "originalFilename": filename,
                                "list_page": page_num,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{_SITE_ID}] item {item.get('filename', '?')} failed: {exc}"
                    )
                    continue

            if new_on_page == 0:
                print(
                    f"[{_SITE_ID}] page {page_num}: all results already seen. Done."
                )
                break

        if page_num >= max_pages:
            print(f"[{_SITE_ID}] safety cap of {max_pages} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
