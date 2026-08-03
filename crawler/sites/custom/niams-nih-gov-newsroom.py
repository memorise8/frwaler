# -*- coding: utf-8 -*-
"""NIAMS NIH Newsroom press-releases crawler.

Discovery: sitemap.xml → /newsroom/press-releases/* URLs.
Detail: each press-release page via HTML parsing.
"""

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from crawler.base_crawler import BaseCrawler


class NiamsNihNewsroomCrawler(BaseCrawler):
    site_id = "niams-nih-gov-newsroom"
    site_name = "Custom: niams-nih-gov-newsroom"
    base_url = "https://www.niams.nih.gov"

    _SITEMAP_URL = "https://www.niams.nih.gov/sitemap.xml"
    _LIST_URL = "https://www.niams.nih.gov/newsroom/press-releases"
    _PR_PREFIX = "/newsroom/press-releases/"
    _PAGE_SIZE = 10   # logical page size for chunked processing
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, max_attempts=3):
        """GET via curl with retry / exponential backoff. Returns text or None."""
        delays = [1, 3, 9]
        for attempt in range(max_attempts):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
                print(f"[{self.site_id}] Empty response attempt {attempt + 1} for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")
            if attempt < max_attempts - 1:
                wait = delays[attempt]
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        print(f"[{self.site_id}] Failed after {max_attempts} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Parse HTML with html5lib → lxml → html.parser fallback."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # URL discovery
    # ------------------------------------------------------------------

    def _get_pr_urls_from_sitemap(self):
        """Return list of press-release URLs from sitemap.xml."""
        raw = self._curl_get(self._SITEMAP_URL)
        if not raw:
            return []
        urls = []
        try:
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            root = ET.fromstring(raw)
            for loc in root.findall(".//sm:loc", ns):
                url = (loc.text or "").strip()
                if (self._PR_PREFIX in url
                        and url.startswith(self.base_url)
                        and not url.endswith(self._LIST_URL.rstrip("/"))):
                    urls.append(url)
        except ET.ParseError:
            # Fallback: regex extraction
            urls = re.findall(
                r"<loc>(https://www\.niams\.nih\.gov/newsroom/press-releases/[^<]+)</loc>",
                raw,
            )
        return urls

    def _get_pr_urls_from_listing(self):
        """Scrape the listing landing page for additional press-release URLs."""
        raw = self._curl_get(self._LIST_URL)
        if not raw:
            return []
        urls = []
        try:
            soup = self._make_soup(raw)
            if not soup:
                return []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if self._PR_PREFIX in href:
                    if not href.startswith("http"):
                        href = self.base_url + href
                    # Only keep niams.nih.gov press-release URLs
                    if href.startswith(self.base_url + self._PR_PREFIX):
                        urls.append(href.split("?")[0].rstrip("/"))
        except Exception as exc:
            print(f"[{self.site_id}] listing parse error: {exc}")
        return list(dict.fromkeys(urls))

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _extract_abstract_from_html(self, soup):
        """Extract best available abstract text from a BeautifulSoup page."""
        # Primary: schema:text div (NIAMS article body)
        body_div = soup.find(attrs={"property": "schema:text"})
        if body_div:
            text = body_div.get_text(separator=" ", strip=True)
            if len(text) >= 50:
                return re.sub(r"\s+", " ", text).strip()

        # Secondary: og:description meta
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            text = og_desc.get("content", "").strip()
            if len(text) >= 50:
                return text

        # Tertiary: collect long paragraph text (for nih.gov stub targets)
        paras = soup.find_all("p")
        chunks = []
        skip_patterns = re.compile(
            r"official website|https://|locked padlock|follow this link",
            re.I,
        )
        for p in paras:
            text = p.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text)
            if len(text) > 80 and not skip_patterns.search(text):
                chunks.append(text)
            if len(chunks) >= 5:
                break
        return " ".join(chunks).strip()

    def _parse_detail(self, url):
        """
        Fetch and parse one press-release detail page.

        If the NIAMS page is a stub that links to nih.gov, follows that link
        to retrieve the full abstract.

        Returns dict with title/abstract/published_date or None on failure.
        """
        raw = self._curl_get(url)
        if not raw:
            return None

        try:
            soup = self._make_soup(raw)
            if not soup:
                return None

            # --- title ---
            title = ""
            h1 = soup.find("h1", attrs={"itemprop": "name"})
            if h1:
                title = h1.get_text(separator=" ", strip=True)
            if not title:
                og = soup.find("meta", property="og:title")
                if og:
                    title = og.get("content", "").strip()
            if not title:
                t = soup.find("title")
                if t:
                    title = t.get_text(strip=True).split("|")[0].strip()

            # --- published date (YYYY-MM-DD) ---
            published_date = ""
            pub_meta = soup.find("meta", property="article:published_time")
            if pub_meta:
                published_date = pub_meta.get("content", "").strip()[:10]

            # --- abstract ---
            abstract = self._extract_abstract_from_html(soup)

            # Stub pages say "Follow this link for the original release"
            # and link out to nih.gov — follow that link for the real body.
            if len(abstract) < 50:
                page_text = soup.get_text(separator=" ")
                if re.search(r"follow this link.*original release", page_text, re.I):
                    # Find the nih.gov link in the page
                    nih_url = None
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if "nih.gov/news" in href and "niams" not in href:
                            nih_url = href
                            break
                    if nih_url:
                        print(f"[{self.site_id}] Stub page — following to {nih_url}")
                        time.sleep(self._delay)
                        raw2 = self._curl_get(nih_url)
                        if raw2:
                            soup2 = self._make_soup(raw2)
                            if soup2:
                                abstract = self._extract_abstract_from_html(soup2)
                                # Also pick up date if missing
                                if not published_date:
                                    pm2 = soup2.find("meta", property="article:published_time")
                                    if pm2:
                                        published_date = pm2.get("content", "").strip()[:10]

            abstract = re.sub(r"\s+", " ", abstract).strip()

            return {
                "title": title,
                "abstract": abstract,
                "published_date": published_date,
            }
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error ({url}): {exc}")
            return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.monotonic()
        saved = 0
        seen_urls = set()

        # --- Discover all press-release URLs ---
        print(f"[{self.site_id}] Discovering URLs from sitemap...")
        sitemap_urls = self._get_pr_urls_from_sitemap()
        print(f"[{self.site_id}] Sitemap: {len(sitemap_urls)} press-release URLs")

        print(f"[{self.site_id}] Discovering URLs from listing page...")
        listing_urls = self._get_pr_urls_from_listing()
        print(f"[{self.site_id}] Listing page: {len(listing_urls)} press-release URLs")

        # Merge — sitemap first, deduplicate, strip trailing slashes
        merged = []
        for u in sitemap_urls + listing_urls:
            u = u.split("?")[0].rstrip("/")
            if u not in seen_urls:
                seen_urls.add(u)
                merged.append(u)
        seen_urls.clear()  # reset for per-item deduplication during fetch

        print(f"[{self.site_id}] Total unique URLs: {len(merged)}")

        if not merged:
            print(f"[{self.site_id}] No press-release URLs found. Done.")
            return 0

        # --- Process in logical pages of PAGE_SIZE ---
        total_pages = (len(merged) + self._PAGE_SIZE - 1) // self._PAGE_SIZE

        for page_num in range(total_pages):
            if page_num >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            elapsed = time.monotonic() - start_time
            if elapsed > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute wall-clock budget reached. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            page_urls = merged[page_num * self._PAGE_SIZE:(page_num + 1) * self._PAGE_SIZE]
            if not page_urls:
                break

            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            new_on_page = 0
            for url in page_urls:
                if limit is not None and saved >= limit:
                    break

                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                if time.monotonic() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                    print(f"[{self.site_id}] Budget exhausted mid-page. Stopping.")
                    break

                try:
                    time.sleep(self._delay)

                    detail = self._parse_detail(url)
                    if not detail:
                        print(f"[{self.site_id}] item {url} failed: could not parse detail")
                        continue

                    title = detail.get("title", "").strip()
                    abstract = detail.get("abstract", "").strip()
                    published_date = detail.get("published_date", "")

                    if not title:
                        print(f"[{self.site_id}] item {url} failed: no title, skipping")
                        continue

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {url} skipped: abstract too short "
                              f"({len(abstract)} chars)")
                        continue

                    slug = url.rstrip("/").split("/")[-1]

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "Press Release",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": url,
                        "pdf_url": "",
                        "doi": "",
                        "department": (
                            "National Institute of Arthritis and "
                            "Musculoskeletal and Skin Diseases"
                        ),
                        "metadata": json.dumps(
                            {"source": "NIAMS Newsroom"},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new URLs on page {page_num}. Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
