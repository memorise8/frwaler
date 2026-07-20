# -*- coding: utf-8 -*-
"""UN Geneva press releases crawler (https://www.ungeneva.org/en/news-media/press-release)."""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT = 50
_WALL_CLOCK_MIN = 25
_MAX_PAGES = 200


def _curl_get(url: str, timeout: int = 30) -> str | None:
    """GET via curl with TLS workaround; returns decoded body or None on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "-L",
        "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        url,
    ]
    last_exc = None
    for delay in _BACKOFF:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            body = result.stdout.decode("utf-8", errors="replace")
            if body.strip():
                return body
        except Exception as exc:
            last_exc = exc
        time.sleep(delay)
    if last_exc:
        print(f"[ungeneva-org-en] curl failed for {url}: {last_exc}")
    return None


def _make_soup(html: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class UNGenevaEnCrawler(BaseCrawler):
    site_id = "ungeneva-org-en"
    site_name = "Custom: ungeneva-org-en"
    base_url = "https://www.ungeneva.org"

    _LIST_URL = "https://www.ungeneva.org/en/news-media/press-release"
    _SITE_SUFFIX = r"\s*\|\s*The United Nations Office at Geneva\s*$"

    def crawl(self, limit=None):
        deadline = time.time() + _WALL_CLOCK_MIN * 60
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(0, _MAX_PAGES):
            if limit is not None and saved >= limit:
                break

            if time.time() > deadline:
                print(f"[ungeneva-org-en] 25-minute wall-clock budget reached at page {page}. Stopping.")
                break

            if page == _MAX_PAGES - 1:
                print(f"[ungeneva-org-en] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            if page > 0 and page % 10 == 0:
                print(f"[ungeneva-org-en] page {page}: saved {saved}/{limit_str}")

            list_url = f"{self._LIST_URL}?page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[ungeneva-org-en] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[ungeneva-org-en] BeautifulSoup failed on list page {page}: {exc}. Skipping.")
                continue

            if soup is None:
                print(f"[ungeneva-org-en] Could not parse list page {page}. Stopping.")
                break

            rows = soup.select("div.views-row a.un-listings-box")
            if not rows:
                print(f"[ungeneva-org-en] No articles on page {page}. End of pagination.")
                break

            new_on_page = 0
            for row in rows:
                if limit is not None and saved >= limit:
                    break

                href = row.get("href", "")
                if not href:
                    continue
                full_url = (
                    f"https://www.ungeneva.org{href}"
                    if href.startswith("/")
                    else href
                )

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                new_on_page += 1

                # Date hint from list row (overridden by detail page when available)
                time_tag = row.find("time")
                list_date = ""
                if time_tag and time_tag.get("datetime"):
                    list_date = time_tag["datetime"][:10]

                external_id = href.rstrip("/").split("/")[-1]

                try:
                    ok = self._fetch_and_save(full_url, external_id, list_date)
                    if ok:
                        saved += 1
                        print(f"[ungeneva-org-en] saved {saved}/{limit_str}: {external_id[:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[ungeneva-org-en] item {full_url} failed: {exc}. Continuing.")
                    continue

                time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[ungeneva-org-en] All items on page {page} already seen. Stopping.")
                break

            # End of pagination: no "Next" link in pager
            next_link = soup.select_one("li.pager__item--next a")
            if not next_link:
                print(f"[ungeneva-org-en] No next-page link after page {page}. Done.")
                break

        print(f"[ungeneva-org-en] Done. Total saved: {saved}")
        return saved

    def _fetch_and_save(self, url: str, external_id: str, fallback_date: str) -> bool:
        """Fetch detail page, parse, and persist. Returns True if saved."""
        raw = _curl_get(url)
        if not raw:
            print(f"[ungeneva-org-en] Failed to fetch detail {url}")
            return False

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[ungeneva-org-en] Parse error for {url}: {exc}")
            return False

        if soup is None:
            return False

        # Title: <title> tag, strip site-name suffix
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        title = re.sub(self._SITE_SUFFIX, "", title).strip()
        if not title:
            og = soup.find("meta", property="og:title")
            title = (og.get("content", "") if og else "").strip()

        # Published date: first <time datetime="...">
        time_tag = soup.find("time", attrs={"datetime": True})
        published_date = fallback_date
        if time_tag:
            raw_dt = time_tag.get("datetime", "")
            if raw_dt:
                published_date = raw_dt[:10]

        # Abstract: full text from div.text-long (Drupal body field)
        body_div = soup.find("div", class_="text-long")
        abstract = ""
        if body_div:
            abstract = body_div.get_text(separator=" ", strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        # Fallback to og:description if body is missing or too short
        if len(abstract) < _MIN_ABSTRACT:
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                candidate = og_desc.get("content", "").strip()
                if len(candidate) > len(abstract):
                    abstract = candidate

        if len(abstract) < _MIN_ABSTRACT:
            print(
                f"[ungeneva-org-en] Abstract too short ({len(abstract)} chars) "
                f"for {url}. Skipping."
            )
            return False

        self._save_paper({
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "Press Release",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": "",
            "doi": "",
            "department": "United Nations Office at Geneva",
            "metadata": json.dumps({"source_url": url}, ensure_ascii=False),
        })
        return True
