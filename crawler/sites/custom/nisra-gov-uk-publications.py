# -*- coding: utf-8 -*-
"""NISRA (Northern Ireland Statistics and Research Agency) publications crawler.

Starting URL: https://www.nisra.gov.uk/publications
Pagination:   ?page=0, ?page=1, ... (20 cards per page)
Detail pages: https://www.nisra.gov.uk/publications/<slug>
"""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from crawler.base_crawler import BaseCrawler

_BACKOFF = [1, 3, 9]


class NisraGovUkPublicationsCrawler(BaseCrawler):
    site_id = "nisra-gov-uk-publications"
    site_name = "Custom: nisra-gov-uk-publications"
    base_url = "https://www.nisra.gov.uk"

    _LIST_URL = "https://www.nisra.gov.uk/publications"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl; returns decoded text or None."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                        "-H", "Accept-Language: en-GB,en;q=0.9",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                pass
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {url}: {exc}")

            if attempt < retries - 1:
                wait = _BACKOFF[attempt]
                print(f"[{self.site_id}] Retrying {url} in {wait}s (attempt {attempt + 1}/{retries})...")
                time.sleep(wait)
            else:
                print(f"[{self.site_id}] Failed to fetch {url} after {retries} attempts.")
        return None

    def _make_soup(self, raw):
        """Parse HTML with html5lib → lxml → html.parser fallback chain."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Abstract builder
    # ------------------------------------------------------------------

    def _build_abstract(self, soup_main, title):
        """Extract a meaningful abstract from the detail page.

        Always includes: title, field-summary, topics, document labels,
        publisher — so even bare-bones pages reach ≥100 chars.
        """
        parts = []

        # 1. Title as lead sentence
        if title:
            parts.append(title + ".")

        # 2. field-summary
        summary_el = soup_main.find(class_="field-summary")
        if summary_el:
            txt = summary_el.get_text(" ", strip=True)
            if txt:
                parts.append(txt)

        # 3. Statistics topics
        for topic_block in soup_main.find_all(class_="field--name-field-site-topics"):
            label = topic_block.find(class_="site-topics--label")
            items = topic_block.find_all(class_="site-topics--item")
            label_txt = label.get_text(strip=True).rstrip(":") if label else "Topics"
            items_txt = ", ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))
            if items_txt:
                parts.append(f"{label_txt}: {items_txt}.")

        # 4. Research support topics
        for topic_block in soup_main.find_all(class_="field--name-field-research-support"):
            label = topic_block.find(class_="site-topics--label")
            items = topic_block.find_all(class_="site-topics--item")
            label_txt = label.get_text(strip=True).rstrip(":") if label else "Research"
            items_txt = ", ".join(i.get_text(strip=True) for i in items if i.get_text(strip=True))
            if items_txt:
                parts.append(f"{label_txt}: {items_txt}.")

        # 5. Document labels (descriptive names like "2024 Bulletin", "Press release")
        article = soup_main.find("article") or soup_main
        doc_labels = []
        for a in article.find_all("a", class_=lambda c: c and "file-link" in c if c else False):
            lbl = a.get_text(" ", strip=True)
            # strip trailing file-meta like "Adobe PDF (2 MB)"
            if "Adobe" in lbl or "Microsoft" in lbl or "Open" in lbl:
                lbl = lbl.split("Adobe")[0].split("Microsoft")[0].split("Open")[0].strip()
            if lbl and len(lbl) > 5:
                doc_labels.append(lbl)
        if doc_labels:
            parts.append("Documents: " + "; ".join(doc_labels) + ".")

        # 6. Any other <p> paragraphs in the article body (exclude nav cruft)
        for p in article.find_all("p"):
            cls = " ".join(p.get("class", []))
            if "published-date" in cls or "help-viewing" in cls or "field-summary" in cls:
                continue
            txt = p.get_text(" ", strip=True)
            if txt and len(txt) > 20 and txt not in parts:
                parts.append(txt)

        # 7. Always append publisher
        parts.append("Published by the Northern Ireland Statistics and Research Agency (NISRA).")

        combined = " ".join(parts)
        return combined

    # ------------------------------------------------------------------
    # Detail page processor
    # ------------------------------------------------------------------

    def _process_detail(self, card_href, listed_date, card_pub_type):
        """Fetch and parse one detail page. Returns paper dict or None."""
        detail_url = urljoin(self.base_url, card_href)
        slug = card_href.rstrip("/").split("/")[-1]

        raw = self._curl_get(detail_url)
        if not raw:
            print(f"[{self.site_id}] Could not fetch detail: {detail_url}")
            return None

        soup = self._make_soup(raw)
        if soup is None:
            print(f"[{self.site_id}] Could not parse detail: {detail_url}")
            return None

        main = soup.find(id="main-content") or soup.find("main") or soup

        # Title
        h1 = main.find("h1", class_="page-title") or main.find("h1")
        title = h1.get_text(strip=True) if h1 else slug.replace("-", " ").title()

        # Published date from detail page (authoritative)
        time_el = main.find("time")
        published_date = ""
        if time_el:
            dt = time_el.get("datetime", "")
            published_date = dt[:10] if dt else ""

        # Abstract
        abstract = self._build_abstract(main, title)

        if len(abstract) < 50:
            print(f"[{self.site_id}] Skipping {slug}: abstract too short ({len(abstract)} chars)")
            return None

        # Keywords from all site-topic items on detail page
        topic_items = main.find_all(class_="site-topics--item")
        keywords_list = [t.get_text(strip=True) for t in topic_items if t.get_text(strip=True)]
        keywords = ", ".join(dict.fromkeys(keywords_list))  # deduplicate order-preserving

        # Publication type (from detail, or fall back to card)
        pub_type = card_pub_type
        type_block = main.find(class_="field--name-field-publication-type")
        if type_block:
            type_item = type_block.find(class_="site-topics--item")
            if type_item:
                pub_type = type_item.get_text(strip=True) or pub_type

        # PDF URL — first .pdf link in document list
        pdf_url = None
        original_filename = None
        for a in main.find_all("a", href=True):
            href_a = a.get("href", "")
            cls_a = " ".join(a.get("class", []))
            if ".pdf" in href_a.lower() or "file--ico__pdf" in cls_a:
                if href_a.startswith("/"):
                    pdf_url = f"{self.base_url}{href_a}"
                elif href_a.startswith("http"):
                    pdf_url = href_a
                if pdf_url:
                    original_filename = unquote(Path(href_a).name)
                    break

        # All document links for metadata
        docs = []
        for a in main.find_all("a", class_=lambda c: c and "file-link" in c if c else False):
            lbl = a.get_text(" ", strip=True)
            docs.append({"label": lbl, "url": a.get("href", "")})

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": pub_type,
            "keywords": keywords,
            "publisher": "Northern Ireland Statistics and Research Agency",
            "authors": "",
            "doi": None,
            "department": None,
            "journal": None,
            "metadata": json.dumps(
                {
                    "posted_date": listed_date,
                    "originalFilename": original_filename,
                    "publication_type": pub_type,
                    "documents": docs,
                    "slug": slug,
                },
                ensure_ascii=False,
            ),
        }
        return paper

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        page = 0
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page
            list_url = f"{self._LIST_URL}?page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            soup = self._make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] Could not parse list page {page}, skipping.")
                page += 1
                continue

            # Parse cards (each card is an <a class="card ...">)
            cards = soup.find_all(
                "a",
                class_=lambda c: c and "card" in c.split() if c else False,
            )
            if not cards:
                print(f"[{self.site_id}] No cards on page {page}, stopping.")
                break

            # Filter to new (unseen) publication URLs
            new_cards = []
            for card in cards:
                href = card.get("href", "")
                if not href or not href.startswith("/publications/"):
                    continue
                # Skip facet/filter URLs (e.g. /publications/type/...)
                parts = href.strip("/").split("/")
                if len(parts) < 2 or parts[1] in ("type", "topic", "series", "date"):
                    continue
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                new_cards.append(card)

            if not new_cards:
                print(f"[{self.site_id}] No new cards on page {page} (all seen), stopping.")
                break

            # Process each card
            for card in new_cards:
                if limit is not None and saved >= limit:
                    break

                href = card.get("href", "")
                try:
                    # Extract card-level metadata
                    time_el = card.find("time")
                    listed_date = ""
                    if time_el:
                        dt = time_el.get("datetime", "")
                        listed_date = dt[:10] if dt else ""

                    type_el = card.find(class_="site-topics--item")
                    card_pub_type = type_el.get_text(strip=True) if type_el else ""

                    time.sleep(self._delay)
                    paper = self._process_detail(href, listed_date, card_pub_type)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {href} failed: {exc}")
                    continue

            # Check for next page
            next_link = soup.find("a", rel="next")
            if not next_link:
                print(f"[{self.site_id}] No 'next' link on page {page}, done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
