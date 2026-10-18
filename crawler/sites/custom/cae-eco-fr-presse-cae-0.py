# -*- coding: utf-8 -*-
"""Crawler for CAE (Conseil d'Analyse Economique) - Communiques de presse.

List pages: https://cae-eco.fr/Presse-CAE-0, /Presse-CAE-1, ...
Detail page: https://cae-eco.fr/<slug>
"""

import json
import re
import subprocess
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from crawler.base_crawler import BaseCrawler

BASE = "https://cae-eco.fr"


def _pdf_request_url(href):
    """Encode source URL characters without guessing at path/query boundaries."""
    parts = urlsplit(urljoin(BASE + "/", href))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("invalid source PDF URL")
    safe = "/%:@!$&'()*+,;=-._~"
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe=safe),
                      quote(parts.query, safe=safe + "?"), quote(parts.fragment, safe=safe + "?")))


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

    def _save_document(self, document):
        # A legacy upsert would delete existing files when pdf_url changes.
        # Historical raw URLs need a separate repair preserving those files.
        rows = self._conn.execute(
            "SELECT pdf_url FROM documents WHERE site_id=? AND external_id=?",
            (self.site_id, document.get("external_id")),
        ).fetchall()
        if len(rows) > 1 or rows and (rows[0][0] or "") != (document.get("pdf_url") or ""):
            self._report_source_failure("invalid_response")
            raise ValueError("existing attachment identity differs; preserve files pending repair review")
        return super()._save_document(document)

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        start_time = time.time()
        MAX_MINUTES = 25
        MAX_PAGES = 200

        page = 0
        while page < MAX_PAGES:
            self._check_cancelled()
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
                self._check_cancelled()
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
                    self._check_cancelled()
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
                    abstract_source = "original source metadata"
                    if len(abstract) < 50 and h1:
                        body = h1.parent.parent.select_one(":scope > div.p-3")
                        if body:
                            body_text = body.get_text(" ", strip=True)
                            if body_text:
                                abstract = body_text
                                abstract_source = "original HTML press body"

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
                    pdf_url_raw = None
                    original_filename = None
                    pdf_tag = detail.select_one('a[href*="/static/pdf/"][href$=".pdf"]')
                    if pdf_tag:
                        pdf_path = pdf_tag.get("href", "")
                        pdf_url_raw = urljoin(BASE + "/", pdf_path)
                        pdf_url = _pdf_request_url(pdf_path)
                        original_filename = pdf_path.split("/")[-1]

                    # The original press archive includes PDF-only entries.
                    # Keep their actual attachment without inventing a summary.
                    if not abstract and not pdf_url:
                        self._report_source_failure("invalid_response")
                        raise ValueError("source press entry has neither summary nor PDF")
                    pdf_urls = []
                    pdf_urls_raw = []
                    for link in detail.select('a[href*="/static/pdf/"][href$=".pdf"]'):
                        href = link["href"]
                        raw_url = urljoin(BASE + "/", href)
                        if raw_url not in pdf_urls_raw:
                            pdf_urls_raw.append(raw_url)
                        url = _pdf_request_url(href)
                        if url not in pdf_urls:
                            pdf_urls.append(url)

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
                    metadata["source_pdf_urls"] = pdf_urls
                    metadata["source_pdf_urls_raw"] = pdf_urls_raw
                    metadata["primary_pdf_url_raw"] = pdf_url_raw
                    metadata["abstract_source"] = abstract_source if abstract else "source PDF full text pending"
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

                    self._check_cancelled()
                    self._save_paper(paper)
                    if self._last_save_created:
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

    # ------------------------------------------------------------------
    # Pure enumeration entry points (framework adapter). crawl() and
    # _save_document() stay unchanged; no fetching or storage happens here.
    # ------------------------------------------------------------------
    def _enumeration_items(self, html, url):
        """Article rows of one /Presse-CAE-N page, exactly as crawl() reads them."""
        from crawler.enumeration.http import PermanentFailure
        if not isinstance(html, str):
            raise PermanentFailure("listing_parse_error:expected_html")
        soup = _bs4(html)
        if not soup:
            raise PermanentFailure("listing_parse_error:unparseable_html")
        rows = []
        for art in soup.select("article.portfolio-item"):
            link_tag = art.select_one("h3 a") or art.select_one("div.entry-image > a")
            if not link_tag:
                continue
            slug = link_tag.get("href", "").strip("'\" ")
            if not slug or slug.startswith("http"):
                continue
            rows.append({
                "slug": slug,
                "url": f"{BASE}/{slug}",
                "list_title": link_tag.get_text(strip=True),
                "list_authors": [a.get_text(strip=True) for a in art.select('a[href^="a="]')],
            })
        return rows

    def _enumeration_paper(self, html, item):
        """Copy of crawl()'s per-article conversion; None where crawl() skips or fails."""
        slug = item["slug"]
        article_url = item["url"]
        list_title = item.get("list_title")
        list_authors = item.get("list_authors") or []
        detail = _bs4(html)
        if not detail:
            return None
        h1 = detail.select_one("h1")
        title = h1.get_text(strip=True) if h1 else list_title
        abstract = ""
        og = detail.select_one('meta[property="og:description"]')
        if og:
            abstract = og.get("content", "").strip()
        if len(abstract) < 50:
            for tag in detail.select('meta[name="description"]'):
                c = tag.get("content", "").strip()
                if len(c) > 50:
                    abstract = c
                    break
        abstract_source = "original source metadata"
        if len(abstract) < 50 and h1:
            body = h1.parent.parent.select_one(":scope > div.p-3")
            if body:
                body_text = body.get_text(" ", strip=True)
                if body_text:
                    abstract = body_text
                    abstract_source = "original HTML press body"
        published_date = None
        for li in detail.select("li"):
            text = li.get_text(strip=True)
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                published_date = m.group(1)
                break
        pdf_url = None
        pdf_url_raw = None
        original_filename = None
        pdf_tag = detail.select_one('a[href*="/static/pdf/"][href$=".pdf"]')
        if pdf_tag:
            pdf_path = pdf_tag.get("href", "")
            pdf_url_raw = urljoin(BASE + "/", pdf_path)
            pdf_url = _pdf_request_url(pdf_path)
            original_filename = pdf_path.split("/")[-1]
        if not abstract and not pdf_url:
            return None  # crawl(): "source press entry has neither summary nor PDF" (item failed)
        pdf_urls = []
        pdf_urls_raw = []
        for link in detail.select('a[href*="/static/pdf/"][href$=".pdf"]'):
            href = link["href"]
            raw_url = urljoin(BASE + "/", href)
            if raw_url not in pdf_urls_raw:
                pdf_urls_raw.append(raw_url)
            url = _pdf_request_url(href)
            if url not in pdf_urls:
                pdf_urls.append(url)
        post_number = None
        if original_filename:
            m = re.search(r"[Nn](\d+)", original_filename)
            if m:
                post_number = m.group(1)
        if not post_number:
            m = re.search(r"(\d+)", slug)
            if m:
                post_number = m.group(1)
        authors_list = []
        tagcloud = detail.select_one("div.tagcloud")
        if tagcloud:
            authors_list = [a.get_text(strip=True) for a in tagcloud.select("a")]
        if not authors_list:
            authors_list = list_authors
        metadata = {"posted_date": published_date, "slug": slug}
        metadata["source_pdf_urls"] = pdf_urls
        metadata["source_pdf_urls_raw"] = pdf_urls_raw
        metadata["primary_pdf_url_raw"] = pdf_url_raw
        metadata["abstract_source"] = abstract_source if abstract else "source PDF full text pending"
        if original_filename:
            metadata["originalFilename"] = original_filename
        return {
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
