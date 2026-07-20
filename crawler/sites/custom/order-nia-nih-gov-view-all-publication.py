# -*- coding: utf-8 -*-
"""NIA Publication Ordering System — All Publications crawler.

Starting URL: https://order.nia.nih.gov/view-all-publications

The site is a Drupal 11 views listing behind an AWS WAF ("Human
Verification" JS challenge). The challenge is keyed off session
continuity: a cookie-less, one-shot request (e.g. a bare ``curl`` call
per page) gets served the challenge page instead of real content, which
silently yields zero results. Using a single persistent
``requests.Session`` (``self._session``, already provided by
``BaseCrawler``) plus a ``Referer`` header avoids the challenge in
practice.

The view has no real pager (``?page=N`` returns the same 57 items
regardless of ``N``), so the pagination loop below is defensive: it
walks pages until a page yields zero new links, which in practice means
it stops right after the first page.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:  # pragma: no cover - bs4 is a hard dependency of this repo
    _BS = None
    _PARSERS = []


def _make_soup(raw_html):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:  # noqa: BLE001 - malformed markup fallback chain
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class OrderNiaNihGovViewAllPublicationCrawler(BaseCrawler):
    """Crawler for order.nia.nih.gov/view-all-publications."""

    site_id = "order-nia-nih-gov-view-all-publication"
    site_name = "Custom: order-nia-nih-gov-view-all-publication"
    base_url = "https://order.nia.nih.gov"

    _LIST_URL = "https://order.nia.nih.gov/view-all-publications"
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60
    _PUBLISHER = "National Institute on Aging (NIA)"
    _RETRY_WAITS = (1, 3, 9)
    _WAF_RETRY_WAITS = (10, 20, 40)

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _fetch(self, url):
        """GET a URL through the shared session with retry/backoff.

        Uses ``self._session`` (a persistent ``requests.Session``) rather
        than one-off subprocess calls so AWS WAF sees a continuous,
        cookie-bearing browsing session instead of isolated anonymous
        requests — the latter gets served a "Human Verification" JS
        challenge page instead of real content. Returns decoded text, or
        ``None`` if every attempt fails.

        The WAF also applies a short-lived, IP-based burst-rate challenge
        (HTTP 202 + ``x-amzn-waf-action: challenge``) independent of
        session/cookies; empirically it clears within ~15s, so that case
        gets a longer backoff than plain network errors.
        """
        for attempt in range(3):
            try:
                resp = self._session.get(
                    url,
                    timeout=30,
                    headers={"Referer": self._LIST_URL},
                )
                waf_challenged = resp.headers.get("x-amzn-waf-action") in ("challenge", "captcha")
                try:
                    text = resp.content.decode(resp.encoding or "utf-8")
                except (UnicodeDecodeError, LookupError):
                    text = resp.content.decode("utf-8", errors="replace")

                if not waf_challenged and ("captcha-container" in text and "gokuProps" in text):
                    waf_challenged = True

                if waf_challenged:
                    raise RuntimeError(f"WAF challenge served (HTTP {resp.status_code})")
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")

                return text
            except Exception as exc:  # noqa: BLE001 - network/WAF errors of any shape
                is_waf = "WAF challenge" in str(exc)
                if attempt < 2:
                    wait = self._WAF_RETRY_WAITS[attempt] if is_waf else self._RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] fetch error ({exc}) for {url}, retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _list_page_links(self, page):
        """Return ordered, deduped list of /publication/<slug> hrefs for a list page."""
        url = self._LIST_URL if page == 0 else f"{self._LIST_URL}?page={page}"
        raw = self._fetch(url)
        if not raw:
            return []
        try:
            soup = _make_soup(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] failed to parse list page {page}: {exc}")
            return []

        links = []
        seen_on_page = set()
        for a in soup.select('a[href^="/publication/"]'):
            href = a.get("href") or ""
            href = href.split("?")[0].split("#")[0]
            if not href or href == "/publication/" or href in seen_on_page:
                continue
            seen_on_page.add(href)
            links.append(href)
        return links

    @staticmethod
    def _parse_pub_date(raw_text):
        """Parse a 'Month YYYY' string into an ISO YYYY-MM-01 date, or None."""
        if not raw_text:
            return None
        raw_text = raw_text.strip()
        for fmt in ("%B %Y", "%b %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(raw_text, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _parse_detail(self, href):
        """Fetch and parse a single publication detail page. Returns a paper dict or None."""
        url = self.base_url + href
        raw = self._fetch(url)
        if not raw:
            print(f"[{self.site_id}] no response for {url}")
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] failed to parse detail page {url}: {exc}")
            return None

        title_el = soup.select_one("h1.pub_title")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            meta_title = soup.find("meta", attrs={"property": "og:title"})
            title = meta_title.get("content").strip() if meta_title and meta_title.get("content") else None
        if not title:
            print(f"[{self.site_id}] no title found for {url}, skipping")
            return None

        desc_el = soup.select_one(".pub_text--description")
        abstract = desc_el.get_text(" ", strip=True) if desc_el else ""
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if not abstract:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            abstract = (meta_desc.get("content") or "").strip() if meta_desc else ""
        if not abstract:
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            abstract = (og_desc.get("content") or "").strip() if og_desc else ""

        pub_date_raw = None
        m = re.search(r"Publication Date:</strong>&nbsp;([^<]+)", raw)
        if m:
            pub_date_raw = m.group(1).strip()
        published_date = self._parse_pub_date(pub_date_raw)

        pdf_a = soup.select_one("a.btn-pdf")
        pdf_url = None
        if pdf_a and pdf_a.get("href"):
            pdf_href = pdf_a.get("href")
            pdf_url = pdf_href if pdf_href.startswith("http") else self.base_url + pdf_href

        original_filename = None
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if tail and "." in tail:
                original_filename = tail

        slug = href.strip("/").split("/")[-1]

        metadata = {
            "posted_date": pub_date_raw,
            "slug": slug,
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "listed_date": published_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": None,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }
        return paper

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        seen_urls = set()
        saved = 0
        page = 0
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while page < self._MAX_PAGES:
                elapsed = time.monotonic() - start_time
                if elapsed > self._MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget ({self._MAX_WALL_SECONDS}s) reached, stopping.")
                    break

                hrefs = self._list_page_links(page)
                new_hrefs = [h for h in hrefs if h not in seen_urls]

                if not new_hrefs:
                    print(f"[{self.site_id}] page {page}: 0 new records, stopping pagination.")
                    break

                for href in new_hrefs:
                    if limit is not None and saved >= limit:
                        break
                    seen_urls.add(href)

                    elapsed = time.monotonic() - start_time
                    if elapsed > self._MAX_WALL_SECONDS:
                        print(f"[{self.site_id}] wall-clock budget ({self._MAX_WALL_SECONDS}s) reached, stopping.")
                        break

                    try:
                        paper = self._parse_detail(href)
                        if paper is None:
                            continue
                        if len(paper["abstract"]) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] abstract too short "
                                f"({len(paper['abstract'])} chars) for {href}, skipping"
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:60]}")
                    except Exception as exc:  # noqa: BLE001 - one bad item must not kill the run
                        print(f"[{self.site_id}] item {href} failed: {exc}")
                        continue

                    time.sleep(self._delay)

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                if limit is not None and saved >= limit:
                    break

                page += 1

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages.")
        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted by user, saved {saved}/{limit_str} so far.")
            raise

        print(f"[{self.site_id}] crawl complete: saved {saved}/{limit_str}")
        return saved
