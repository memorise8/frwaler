# -*- coding: utf-8 -*-
"""Crawler for BMWSB Gesetzgebungsverfahren (Federal Ministry for Housing).

Target: https://www.bmwsb.bund.de/DE/ministerium/gesetzgebungsverfahren/gesetzgebungsverfahren_node.html
CMS: Government Site Builder (GSB / Bund)
Pagination: ?gtp=122226_Dokumente%3D{page} for page >= 2
            (HTML href has %253D = double-encoded; effective URL needs %3D)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class BmwsbBundDeDeCrawler(BaseCrawler):
    site_id = "bmwsb-bund-de-de"
    site_name = "Custom: bmwsb-bund-de-de"
    base_url = "https://www.bmwsb.bund.de"

    START_URL = (
        "https://www.bmwsb.bund.de/DE/ministerium/gesetzgebungsverfahren/"
        "gesetzgebungsverfahren_node.html"
    )
    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 60
    MIN_ABSTRACT_CHARS = 50
    PREFERRED_ABSTRACT_CHARS = 100
    MAX_PAGES = 200
    PUBLISHER = "Bundesministerium für Wohnen, Stadtentwicklung und Bauwesen (BMWSB)"

    def crawl(self, limit=None):
        """Crawl Gesetzgebungsverfahren from BMWSB, saving via self._save_paper()."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        max_wall_seconds = 25 * 60

        page_url: str | None = self.START_URL
        page_num = 0

        try:
            while page_url:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > max_wall_seconds:
                    print(f"[{self.site_id}] wall-clock budget reached, stopping")
                    break

                page_num += 1
                if page_num > self.MAX_PAGES:
                    print(
                        f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached"
                    )
                    break

                raw = self._curl_get(page_url, context=f"list page {page_num}")
                if not raw:
                    print(f"[{self.site_id}] list page {page_num} fetch failed, stopping")
                    break

                soup = self._make_soup(raw, context=f"list page {page_num}")
                if soup is None:
                    print(f"[{self.site_id}] list page {page_num} parse failed, stopping")
                    break

                items = soup.select("div.c-teaser-search-result")
                if not items:
                    print(f"[{self.site_id}] page {page_num}: no items, end of pagination")
                    break

                if page_num % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                for item_el in items:
                    if limit is not None and saved >= limit:
                        break

                    try:
                        link_el = item_el.select_one("a.c-teaser-search-result__link-main")
                        if not link_el:
                            continue
                        href = (link_el.get("href") or "").strip()
                        if not href:
                            continue
                        detail_url = urljoin(self.base_url, href)

                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)

                        # --- List-page fields ---
                        title_el = item_el.select_one("h3.c-teaser-search-result__headline")
                        list_title = _clean_text(
                            title_el.get_text(" ", strip=True) if title_el else ""
                        )

                        date_el = item_el.select_one("span.c-topline__element.is-date")
                        list_date_raw = date_el.get_text(strip=True) if date_el else ""
                        list_date = _parse_german_date(list_date_raw)

                        cat_el = item_el.select_one("p.c-teaser-search-result__category")
                        category = ""
                        if cat_el:
                            for span in cat_el.find_all("span"):
                                span.decompose()
                            category = _clean_text(cat_el.get_text(" ", strip=True))

                        abs_el = item_el.select_one("p.c-teaser-search-result__text")
                        list_abstract = _clean_text(
                            abs_el.get_text(" ", strip=True) if abs_el else ""
                        )

                        # --- Fetch detail page ---
                        time.sleep(self._delay)
                        detail_raw = self._curl_get(
                            detail_url,
                            context=f"detail {len(seen_urls)}",
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(
                            detail_raw, context=f"detail {detail_url}"
                        )
                        if detail_soup is None:
                            raise RuntimeError("detail HTML could not be parsed")

                        parsed = self._parse_detail(detail_soup, detail_url)

                        title = parsed.get("title") or list_title
                        abstract = parsed.get("abstract") or list_abstract
                        published_date = parsed.get("published_date") or list_date
                        pdf_url = parsed.get("pdf_url") or ""
                        original_filename = parsed.get("original_filename") or ""

                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item skipped: abstract too short "
                                f"({len(abstract)} chars): {title[:60]}"
                            )
                            continue
                        if len(abstract) < self.PREFERRED_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] item skipped: abstract below preferred "
                                f"({len(abstract)} chars): {title[:60]}"
                            )
                            continue

                        external_id = _external_id_from_url(detail_url)

                        metadata: dict = {
                            "list_page": page_url,
                            "list_date_raw": list_date_raw,
                            "list_abstract_preview": list_abstract[:200],
                            "category": category,
                            "page_num": page_num,
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": external_id,
                            "title": title,
                            "abstract": abstract,
                            "category": category,
                            "keywords": category,
                            "published_date": published_date,
                            "listed_date": list_date,
                            "url": detail_url,
                            "pdf_url": pdf_url or None,
                            "doi": "",
                            "department": self.PUBLISHER,
                            "publisher": self.PUBLISHER,
                            "authors": "",
                            "journal": "",
                            "original_filename": original_filename or None,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] saved {saved}/{lim_str}: {title[:80]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item failed: {exc}")
                        continue

                if limit is not None and saved >= limit:
                    break

                page_url = self._next_page_url(soup)

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted, saved so far: {saved}")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _next_page_url(self, soup: BeautifulSoup) -> str | None:
        """Return the next list-page URL, or None at end of pagination."""
        next_a = soup.select_one("li.c-pagination__item--next a[href]")
        if not next_a:
            return None
        href = (next_a.get("href") or "").strip()
        if not href:
            return None
        # CMS writes %253D (double-encoded =) in href; working URL needs %3D
        href = href.replace("%253D", "%3D")
        full = urljoin(self.base_url, href)
        if "gesetzgebungsverfahren_node" not in full:
            return None
        return full

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_detail(self, soup: BeautifulSoup, url: str) -> dict:
        # Title
        title = ""
        title_el = soup.select_one("h1.c-intro__headline")
        if title_el:
            title = _clean_text(title_el.get_text(" ", strip=True))
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            if og and og.get("content"):
                title = _clean_text(og["content"])

        # Published date — span in intro, then JSON-LD fallback
        published_date = ""
        date_el = soup.select_one("span.c-intro__label-element.is-date")
        if date_el:
            published_date = _parse_german_date(date_el.get_text(strip=True))
        if not published_date:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    if isinstance(data, dict) and data.get("datePublished"):
                        published_date = str(data["datePublished"])[:10]
                        break
                except Exception:
                    pass

        # Abstract — GSB structure:
        #   outer l-article__content
        #     l-article
        #       l-article__intro   ← skip: title/date/image
        #       row
        #         column
        #           l-article__content  ← body text here
        abstract = ""
        body_el = soup.select_one("div.l-article div.row div.l-article__content")
        if not body_el:
            # Fallback: find l-article and strip its intro
            l_article = soup.select_one("div.l-article")
            if l_article:
                intro = l_article.select_one("div.l-article__intro")
                if intro:
                    intro.decompose()
                body_el = l_article

        if body_el:
            for unwanted in body_el.find_all(
                ["script", "style", "noscript", "nav", "figure", "figcaption"]
            ):
                unwanted.decompose()
            pieces = []
            for node in body_el.find_all(["p", "li", "h2", "h3", "h4"]):
                t = _clean_text(node.get_text(" ", strip=True))
                if t and len(t) > 5:
                    pieces.append(t)
            abstract = re.sub(r"\s+", " ", " ".join(pieces)).strip()

        # Fallback: meta description
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            meta = soup.select_one('meta[name="description"]')
            if meta and meta.get("content"):
                candidate = _clean_text(meta["content"])
                if len(candidate) > len(abstract):
                    abstract = candidate

        # PDF URL (prefer __blob=publicationFile links)
        pdf_url = ""
        original_filename = ""
        for a_tag in soup.find_all("a", href=True):
            raw_href = a_tag.get("href") or ""
            if "__blob=publicationFile" in raw_href or re.search(
                r"\.pdf(\?|$)", raw_href, re.IGNORECASE
            ):
                clean_href = raw_href.replace("&amp;", "&")
                pdf_url = urljoin(self.base_url, clean_href)
                path_only = urlparse(pdf_url.split("?")[0]).path
                original_filename = path_only.rstrip("/").split("/")[-1]
                break

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "request") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", str(self.CURL_TIMEOUT),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.7",
            url,
        ]
        for attempt, wait in enumerate(self.BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                msg = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt}/3): rc={result.returncode} {msg[:120]}"
                )
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] {context} timeout (attempt {attempt}/3)")
            except Exception as exc:
                print(f"[{self.site_id}] {context} error (attempt {attempt}/3): {exc}")
            if attempt < len(self.BACKOFF_SECONDS):
                time.sleep(wait)
        return None

    def _make_soup(self, raw: str, context: str = "HTML") -> BeautifulSoup | None:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}"
                )
        return None


# ------------------------------------------------------------------
# Module-level helpers (used by crawler and importable for tests)
# ------------------------------------------------------------------

def _clean_text(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()


def _parse_german_date(raw: str) -> str:
    """DD.MM.YYYY → YYYY-MM-DD; also passes through ISO dates."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw or "")
    if m:
        day, month, year = m.groups()
        return f"{year}-{month}-{day}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw or "")
    if m:
        return m.group(1)
    return ""


def _external_id_from_url(url: str) -> str:
    """Return a stable external_id from the last two path segments (without .html)."""
    path = urlparse(url).path
    path = re.sub(r"\.html$", "", path)
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    if parts:
        return parts[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", url)[-60:]
