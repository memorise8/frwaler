# -*- coding: utf-8 -*-
"""Crawler for HARC Research — harcresearch.org/research.

All 68+ items render on a single HTML listing page (no server-side pagination).
Each internal /research/{slug}/ detail page contains full content.
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler


def _make_soup(html, url=""):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class HarcResearchCrawler(BaseCrawler):
    site_id = "harcresearch-org-research"
    site_name = "Custom: harcresearch-org-research"
    base_url = "https://harcresearch.org"

    _LIST_URL = "https://harcresearch.org/research/"

    def _curl_get(self, url, retries=3):
        """GET via curl with TLS workaround and exponential backoff retry."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
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
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] Empty response from {url}, retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error: {exc}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    def _extract_abstract(self, soup):
        """Extract abstract text from detail page; modifies soup in place."""
        # Remove non-content tags
        for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # Remove WordPress hero banner
        hero = soup.find("section", id="image-banner")
        if hero:
            hero.decompose()

        # Remove sidebar metadata lists
        for el in soup.find_all(class_=re.compile(r"\blists\b")):
            el.decompose()

        # Remove "related research" section (contains PHP debug output on this site)
        for el in soup.find_all(class_=re.compile(r"\brelated\b")):
            el.decompose()

        # Collect substantial paragraphs, skipping CSS/code artifacts
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(separator=" ", strip=True)
            if not text or len(text) < 40:
                continue
            # Skip CSS/code artifacts that leak through parser
            if "{" in text and "}" in text:
                continue
            if text.startswith(("//", "/*", "var ", "function ", "document.", "window.")):
                continue
            paragraphs.append(text)

        if paragraphs:
            return " ".join(paragraphs[:20])

        # Fallback: all remaining body text
        body = soup.find("body")
        if body:
            text = re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True)).strip()
            return text[:3000]

        return ""

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "∞"

        # Fetch listing page — all items load at once, no pagination
        print(f"[{self.site_id}] Fetching listing page: {self._LIST_URL}")
        raw = self._curl_get(self._LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch listing page.")
            return 0

        try:
            list_soup = _make_soup(raw, self._LIST_URL)
        except Exception as exc:
            print(f"[{self.site_id}] Failed to parse listing page: {exc}")
            return 0

        if list_soup is None:
            print(f"[{self.site_id}] Failed to parse listing page HTML.")
            return 0

        item_divs = list_soup.find_all("div", class_="item")
        print(f"[{self.site_id}] Found {len(item_divs)} items on listing page.")

        # Build item list from listing HTML
        item_list = []
        for item_div in item_divs:
            a_tag = item_div.find("a", href=True)
            if not a_tag:
                continue

            href = a_tag.get("href", "").strip()
            if not href:
                continue

            # Title from <span class="h3"> inside <div class="title">
            title = ""
            title_span = item_div.find("span", class_="h3")
            if title_span:
                title = title_span.get_text(strip=True)
            if not title:
                for h in item_div.find_all(["h3", "h2", "h4", "h1"]):
                    title = h.get_text(strip=True)
                    if title:
                        break

            # Short description from <p> inside the card
            p_tag = item_div.find("p")
            short_desc = p_tag.get_text(strip=True) if p_tag else ""

            # Categories from CSS classes like cat-air, cat-data-downloads, etc.
            classes = item_div.get("class", [])
            categories = [
                c.replace("cat-", "").replace("-", " ")
                for c in classes if c.startswith("cat-")
            ]

            item_list.append({
                "url": href,
                "title": title,
                "short_desc": short_desc,
                "categories": categories,
            })

        print(f"[{self.site_id}] Parsed {len(item_list)} items from listing.")

        # Process items — the whole listing is one logical "page"
        p = 1
        for idx, item_info in enumerate(item_list):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping.")
                break

            url = item_info["url"]
            title = item_info["title"]
            short_desc = item_info["short_desc"]
            categories = list(item_info["categories"])

            if url in seen_urls:
                continue
            seen_urls.add(url)

            parsed_url = urlparse(url)
            netloc = parsed_url.netloc or ""
            path = parsed_url.path or ""

            is_internal = (
                "harcresearch.org" in netloc
                and "/research/" in path
                and not url.lower().endswith(".pdf")
                and len(path.rstrip("/").split("/")) >= 3  # /research/slug/
            )
            is_pdf = url.lower().endswith(".pdf")

            try:
                if is_internal:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(url)

                    if not detail_html:
                        print(f"[{self.site_id}] item {idx+1} failed: could not fetch {url}")
                        continue

                    try:
                        detail_soup = _make_soup(detail_html, url)
                    except Exception as exc:
                        print(f"[{self.site_id}] item {idx+1} failed: parse error {exc}")
                        continue

                    if detail_soup is None:
                        print(f"[{self.site_id}] item {idx+1} failed: could not parse {url}")
                        continue

                    # --- Extract all metadata BEFORE modifying soup ---

                    # WP post ID from body class (e.g. "postid-4646")
                    post_id = None
                    body_tag = detail_soup.find("body")
                    if body_tag:
                        for cls in body_tag.get("class", []):
                            m = re.match(r"postid-(\d+)", cls)
                            if m:
                                post_id = m.group(1)
                                break

                    # Title from h1 (inside image-banner hero section)
                    h1 = detail_soup.find("h1")
                    if h1:
                        title = h1.get_text(strip=True)

                    # Resource types and program tags from hero section
                    resource_types = []
                    hero_sec = detail_soup.find("section", id="image-banner")
                    if hero_sec:
                        for h3 in hero_sec.find_all("h3"):
                            rt = h3.get_text(strip=True)
                            if rt:
                                resource_types.append(rt)
                        for h4 in hero_sec.find_all("h4"):
                            pt = h4.get_text(strip=True).lower()
                            if pt and pt not in categories:
                                categories.append(pt)

                    # Sidebar metadata: Researchers, Client/Funder, Partners, Location
                    authors_list = []
                    publisher = None
                    partners = []
                    location = None

                    for lists_div in detail_soup.find_all("div", class_="lists"):
                        for h5 in lists_div.find_all("h5"):
                            label = h5.get_text(strip=True).lower()
                            nxt = h5.find_next_sibling()
                            if not nxt:
                                continue
                            if "researcher" in label:
                                for li in nxt.find_all("li"):
                                    name = li.get_text(strip=True)
                                    if name:
                                        authors_list.append(name)
                            elif "client" in label or "funder" in label:
                                publisher = nxt.get_text(separator=" ", strip=True)
                            elif "partner" in label:
                                for a_el in nxt.find_all("a"):
                                    pname = a_el.get_text(strip=True)
                                    if pname:
                                        partners.append(pname)
                                if not partners:
                                    partners.append(nxt.get_text(separator=" ", strip=True))
                            elif "location" in label:
                                location = nxt.get_text(separator=" ", strip=True)

                    # Date from og:updated_time (in <head>, unaffected by body decompose)
                    published_date = None
                    og_meta = detail_soup.find("meta", attrs={"property": "og:updated_time"})
                    if og_meta:
                        dt_raw = og_meta.get("content", "")
                        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_raw)
                        if m:
                            published_date = m.group(1)

                    # PDF links anywhere in page
                    pdf_url = None
                    original_filename = None
                    for a_el in detail_soup.find_all("a", href=True):
                        href_val = a_el.get("href", "")
                        if href_val.lower().endswith(".pdf"):
                            pdf_url = href_val
                            original_filename = urlparse(href_val).path.split("/")[-1]
                            break

                    # --- Abstract extraction LAST (modifies soup) ---
                    abstract_text = self._extract_abstract(detail_soup)

                    # Fallback to listing short description
                    if len(abstract_text) < 50 and short_desc:
                        abstract_text = short_desc

                    if len(abstract_text) < 100:
                        print(
                            f"[{self.site_id}] item {idx+1} skipped "
                            f"(abstract {len(abstract_text)} chars): {title[:50]}"
                        )
                        continue

                    slug = path.rstrip("/").split("/")[-1]
                    external_id = post_id or slug

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_id or slug,
                        "title": title,
                        "abstract": abstract_text,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": "; ".join(authors_list) if authors_list else None,
                        "publisher": publisher,
                        "department": location,
                        "journal": None,
                        "url": url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": "; ".join(categories) if categories else None,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": published_date,
                            "originalFilename": original_filename,
                            "resource_types": resource_types,
                            "partners": partners,
                            "location": location,
                            "post_id": post_id,
                            "slug": slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                elif is_pdf:
                    # Direct PDF item — use listing description as abstract
                    if len(short_desc) < 100:
                        print(
                            f"[{self.site_id}] item {idx+1} PDF skipped "
                            f"(desc {len(short_desc)} chars): {title[:50]}"
                        )
                        continue
                    original_filename = url.split("/")[-1]
                    slug = original_filename[:-4] if original_filename.lower().endswith(".pdf") else original_filename

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": short_desc,
                        "published_date": None,
                        "listed_date": None,
                        "authors": None,
                        "publisher": None,
                        "url": url,
                        "pdf_url": url,
                        "keywords": None,
                        "category": "; ".join(categories) if categories else None,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": None,
                            "originalFilename": original_filename,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]} [PDF]")

                else:
                    # External URL (ArcGIS StoryMaps, etc.) — no crawlable detail page
                    print(f"[{self.site_id}] item {idx+1} skipped (external URL): {title[:50]}")
                    continue

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx+1} failed: {exc}")
                continue

            if (idx + 1) % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_or_inf}")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
