# -*- coding: utf-8 -*-
"""Crawler for CAE (Conseil d'Analyse Economique) - Communiques de presse.

List pages: https://cae-eco.fr/Presse-CAE-0, /Presse-CAE-1, ...
Detail page: https://cae-eco.fr/<slug>
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

BASE = "https://cae-eco.fr"


def _bs4(raw):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl(url, retries=3):
    """Fetch URL via curl with retry + exponential backoff. Returns text or None."""
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url],
                capture_output=True, timeout=35,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[cae-eco-fr-presse-cae-0] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            print(f"[cae-eco-fr-presse-cae-0] retrying in {wait}s...")
            time.sleep(wait)
    return None


class CaeEcoFrPresseCae0Crawler(BaseCrawler):
    site_id = "cae-eco-fr-presse-cae-0"
    site_name = "Custom: cae-eco-fr-presse-cae-0"
    base_url = BASE

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        start_time = time.time()
        MAX_MINUTES = 25
        MAX_PAGES = 200

        page = 0
        while page < MAX_PAGES:
            if (time.time() - start_time) / 60 >= MAX_MINUTES:
                print(f"[cae-eco-fr-presse-cae-0] {MAX_MINUTES}min budget reached, stopping.")
                break
            if page == MAX_PAGES - 1:
                print(f"[cae-eco-fr-presse-cae-0] 200-page safety cap reached, stopping.")

            page_url = f"{BASE}/Presse-CAE-{page}"
            raw = _curl(page_url)
            if not raw:
                print(f"[cae-eco-fr-presse-cae-0] failed to fetch list page {page}, stopping.")
                break

            soup = _bs4(raw)
            if not soup:
                print(f"[cae-eco-fr-presse-cae-0] failed to parse list page {page}, stopping.")
                break

            articles = soup.select("article.portfolio-item")
            if not articles:
                print(f"[cae-eco-fr-presse-cae-0] no articles on page {page}, stopping.")
                break

            new_on_page = 0
            for art in articles:
                if saved >= limit_or_inf:
                    break

                # Slug from h3 > a, fallback to image link
                link_tag = art.select_one("h3 a") or art.select_one("div.entry-image > a")
                if not link_tag:
                    continue
                slug = link_tag.get("href", "").strip("'\" ")
                if not slug or slug.startswith("http"):
                    continue

                article_url = f"{BASE}/{slug}"
                if article_url in seen_urls:
                    continue
                seen_urls.add(article_url)
                new_on_page += 1

                list_title = link_tag.get_text(strip=True)
                list_authors = [a.get_text(strip=True) for a in art.select('a[href^="a="]')]

                try:
                    detail_raw = _curl(article_url)
                    if not detail_raw:
                        print(f"[cae-eco-fr-presse-cae-0] item {slug} fetch failed, skipping.")
                        continue

                    detail = _bs4(detail_raw)
                    if not detail:
                        print(f"[cae-eco-fr-presse-cae-0] item {slug} parse failed, skipping.")
                        continue

                    # Title
                    h1 = detail.select_one("h1")
                    title = h1.get_text(strip=True) if h1 else list_title

                    # Abstract: prefer og:description (article-specific)
                    abstract = ""
                    og = detail.select_one('meta[property="og:description"]')
                    if og:
                        abstract = og.get("content", "").strip()
                    if len(abstract) < 50:
                        # Fallback: second meta[name=description] with real content
                        for tag in detail.select('meta[name="description"]'):
                            c = tag.get("content", "").strip()
                            if len(c) > 50:
                                abstract = c
                                break
                    if len(abstract) < 50:
                        print(f"[cae-eco-fr-presse-cae-0] item {slug} abstract <50 chars, skipping.")
                        continue

                    # Date from schedule icon list item
                    published_date = None
                    for li in detail.select("li"):
                        text = li.get_text(strip=True)
                        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                        if m:
                            published_date = m.group(1)
                            break

                    # PDF URL and original filename
                    pdf_url = None
                    original_filename = None
                    pdf_tag = detail.select_one('a[href*="/static/pdf/"][href$=".pdf"]')
                    if pdf_tag:
                        pdf_path = pdf_tag.get("href", "")
                        pdf_url = f"{BASE}{pdf_path}" if pdf_path.startswith("/") else pdf_path
                        original_filename = pdf_path.split("/")[-1]

                    # post_number from PDF filename N{num} pattern
                    post_number = None
                    if original_filename:
                        m = re.search(r"[Nn](\d+)", original_filename)
                        if m:
                            post_number = m.group(1)
                    if not post_number:
                        m = re.search(r"(\d+)", slug)
                        if m:
                            post_number = m.group(1)

                    # Authors from tagcloud on detail page
                    authors_list = []
                    tagcloud = detail.select_one("div.tagcloud")
                    if tagcloud:
                        authors_list = [a.get_text(strip=True) for a in tagcloud.select("a")]
                    if not authors_list:
                        authors_list = list_authors

                    metadata = {"posted_date": published_date, "slug": slug}
                    if original_filename:
                        metadata["originalFilename"] = original_filename

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": post_number,
                        "title": title or list_title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": "; ".join(authors_list) if authors_list else None,
                        "publisher": "Conseil d'Analyse Economique",
                        "url": article_url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": "Communiques de presse",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[cae-eco-fr-presse-cae-0] saved {saved}: {title[:60]}")

                    time.sleep(1.0)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[cae-eco-fr-presse-cae-0] item {slug} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[cae-eco-fr-presse-cae-0] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[cae-eco-fr-presse-cae-0] no new articles on page {page}, stopping.")
                break

            if saved >= limit_or_inf:
                break

            page += 1

        return saved
