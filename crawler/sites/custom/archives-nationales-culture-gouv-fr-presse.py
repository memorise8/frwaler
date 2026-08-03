# -*- coding: utf-8 -*-
"""Archives nationales (France) — Communiqués de presse crawler.

Starting URL:
  https://www.archives-nationales.culture.gouv.fr/presse
  ?title=&field_type_publication_target_id_1%5B32%5D=32&created%5Bvalue%5D=

Bot mitigation: 3-step cookie handshake.
  Step 1: GET page  →  bot_mitigation_cookie + JS href with redirect token
  Step 2: GET redirect URL with cookie1  →  307 + new bot_mitigation_cookie
  Step 3: GET original page with cookie2  →  real HTML

Articles link directly to PDF files on S3; there are no separate HTML detail
pages.  The card description (~200 chars) is used as the abstract.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.archives-nationales.culture.gouv.fr"
_LIST_PATH = "/presse"
_LIST_PARAMS = "title=&field_type_publication_target_id_1%5B32%5D=32&created%5Bvalue%5D="

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_FRENCH_MONTHS = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}


class ArchivesNationalesPresseCrawler(BaseCrawler):
    site_id = "archives-nationales-culture-gouv-fr-presse"
    site_name = "Custom: archives-nationales-culture-gouv-fr-presse"
    base_url = "https://www.archives-nationales.culture.gouv.fr"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, cookie=None):
        """GET via curl. Returns (headers_str, body_str) or (None, None)."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-D", "-",
            "-H", f"User-Agent: {_UA}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en-US;q=0.8",
        ]
        if cookie:
            cmd += ["-H", f"Cookie: {cookie}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                raw = result.stdout.decode("utf-8", errors="replace")
                # Split response headers from body
                sep = raw.find("\r\n\r\n")
                if sep != -1:
                    return raw[:sep], raw[sep + 4:]
                sep = raw.find("\n\n")
                if sep != -1:
                    return raw[:sep], raw[sep + 2:]
                return "", raw
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl timeout (attempt {attempt + 1}/3), retrying in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s")
                    time.sleep(wait)
        return None, None

    def _fetch_page(self, url):
        """Fetch a page through the 3-step bot-mitigation handshake.

        Returns the rendered HTML body string, or None on failure.
        The cookie token changes on every request, so this handshake must
        be repeated for each page.
        """
        # Step 1: initial GET
        headers1, body1 = self._curl_get(url)
        if headers1 is None:
            return None

        redir_match = re.search(r"href='(/redirect_[^']+)'", body1 or "")
        if not redir_match:
            # No bot mitigation active; the body is already real content.
            return body1

        cookie1_match = re.search(r"bot_mitigation_cookie=([^\s;]+)", headers1)
        if not cookie1_match:
            return body1
        cookie1 = f"bot_mitigation_cookie={cookie1_match.group(1)}"
        redir_path = redir_match.group(1)

        # Step 2: follow the JS-redirect URL to get a fresh cookie (307)
        headers2, _body2 = self._curl_get(_BASE + redir_path, cookie=cookie1)
        if headers2 is None:
            return None
        cookie2_match = re.search(r"bot_mitigation_cookie=([^\s;]+)", headers2)
        cookie2 = (
            f"bot_mitigation_cookie={cookie2_match.group(1)}"
            if cookie2_match
            else cookie1
        )

        # Step 3: fetch the actual page with the validated cookie
        _headers3, body3 = self._curl_get(url, cookie=cookie2)
        return body3

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Parse HTML — html5lib → lxml → html.parser fallback chain."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_french_date(text):
        """'Publié(e) le 05 février 2026' → '2026-02-05'."""
        m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text.strip())
        if not m:
            return ""
        day, month_fr, year = m.group(1), m.group(2).lower(), m.group(3)
        month = _FRENCH_MONTHS.get(month_fr, "")
        if not month:
            return ""
        return f"{year}-{month}-{day.zfill(2)}"

    @staticmethod
    def _version_id(url):
        """Extract VersionId query param from an S3 URL (numeric string)."""
        m = re.search(r"[?&]VersionId=([^&]+)", url)
        return m.group(1) if m else None

    @staticmethod
    def _url_filename(url):
        """URL-decode and return the last path segment of a URL."""
        path = urllib.parse.urlparse(url).path
        return urllib.parse.unquote(path.rstrip("/").split("/")[-1])

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_display = str(limit) if limit is not None else "∞"

        for p in range(200):  # 200-page safety cap
            # Wall-clock budget
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute budget reached at page {p}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if p == 199:
                print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if p % 10 == 0 and p > 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_display}")

            # Build page URL (page=0 is the default; add &page=N for subsequent pages)
            page_url = (
                f"{_BASE}{_LIST_PATH}?{_LIST_PARAMS}"
                if p == 0
                else f"{_BASE}{_LIST_PATH}?{_LIST_PARAMS}&page={p}"
            )

            html = self._fetch_page(page_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {p}. Stopping.")
                break

            try:
                soup = self._make_soup(html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error on page {p}: {exc}. Stopping.")
                break

            if not soup:
                print(f"[{self.site_id}] Could not parse page {p}. Stopping.")
                break

            articles = soup.find_all("article", class_="node--type-detail-publication")
            if not articles:
                print(f"[{self.site_id}] No articles on page {p}. Done.")
                break

            new_on_page = 0

            for art in articles:
                if limit is not None and saved >= limit:
                    break

                try:
                    # Title
                    title_el = art.find("span", class_="field--name-title")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    if not title:
                        continue

                    # PDF URL — the card link href goes directly to an S3 PDF
                    link_el = art.find("a", class_="fr-card__link")
                    pdf_url = (link_el.get("href", "") or "").strip() if link_el else ""

                    # Deduplicate
                    dedup_key = pdf_url or title
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)
                    new_on_page += 1

                    # Abstract from card description (~200 truncated chars)
                    desc_el = art.find("p", class_="fr-card__desc")
                    abstract = desc_el.get_text(strip=True) if desc_el else ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Date — "Publié(e) le DD MMMM YYYY"
                    date_el = art.find("p", class_="fr-card__detail")
                    raw_date = date_el.get_text(strip=True) if date_el else ""
                    published_date = self._parse_french_date(raw_date)

                    # IDs — use S3 VersionId as numeric post_number when available
                    vid = self._version_id(pdf_url)
                    external_id = (
                        vid
                        if vid
                        else hashlib.sha256(dedup_key.encode()).hexdigest()[:20]
                    )
                    post_number = vid  # numeric string or None

                    # Filename from URL path
                    original_filename = self._url_filename(pdf_url) if pdf_url else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "authors": "",
                        "publisher": "Archives nationales",
                        "journal": "",
                        "url": pdf_url or page_url,
                        "pdf_url": pdf_url or None,
                        "doi": "",
                        "keywords": "",
                        "category": "Communiqué de presse",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_date,
                                "originalFilename": original_filename,
                                "version_id": vid,
                                "raw_date": raw_date,
                                "page_number": p,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new articles on page {p}. Done.")
                break

            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
