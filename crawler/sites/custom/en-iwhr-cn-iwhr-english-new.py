# -*- coding: utf-8 -*-
"""Crawler for IWHR English New — Publications / Papers section.

Starting URL: http://en.iwhr.cn/IWHR-English-New/Publications/AnnualReports/A110403index_1.htm
Primary content: Publications > Papers — 464 papers across 24 pages.
List URL pattern: /IWHR-English-New/Publications/Papers/A110402index_{page}.htm
Detail pages:    /IWHR-English-New/Publications/Papers/webinfo/{yyyy}/{mm}/{id}.htm
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    try:
        import html5lib  # noqa: F401
        _PARSER = "html5lib"
    except ImportError:
        try:
            import lxml  # noqa: F401
            _PARSER = "lxml"
        except ImportError:
            _PARSER = "html.parser"
except ImportError:
    _BS = None
    _PARSER = None


def _make_soup(raw):
    """Parse HTML with fallback chain; return None on total failure."""
    if _BS is None or raw is None:
        return None
    parsers = [_PARSER] + [p for p in ("html5lib", "lxml", "html.parser") if p != _PARSER]
    for parser in parsers:
        try:
            return _BS(raw, parser)
        except Exception:
            continue
    return None


def _curl(url, retries=3):
    """GET url via curl with retries + exponential backoff. Returns text or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url],
                capture_output=True,
                timeout=45,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[en-iwhr-cn-iwhr-english-new] curl rc={result.returncode} for {url}")
        except Exception as exc:
            print(f"[en-iwhr-cn-iwhr-english-new] curl error attempt {attempt+1}/{retries}: {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            print(f"[en-iwhr-cn-iwhr-english-new] retrying in {wait}s...")
            time.sleep(wait)
    return None


_BASE = "http://en.iwhr.cn"
_PAPERS_TPL = _BASE + "/IWHR-English-New/Publications/Papers/A110402index_{page}.htm"


def _abs_url(href):
    """Resolve href to absolute URL."""
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE + href
    return _BASE + "/" + href


class IWHREnglishNewCrawler(BaseCrawler):
    """Crawler for IWHR English New site (Publications > Papers section)."""

    site_id = "en-iwhr-cn-iwhr-english-new"
    site_name = "Custom: en-iwhr-cn-iwhr-english-new"
    base_url = "http://en.iwhr.cn"

    def crawl(self, limit=None):
        """Paginate through Papers list, fetch each detail page, save to DB.

        Returns count of saved records.
        """
        saved = 0
        seen_urls = set()
        limit_val = limit if limit is not None else float("inf")
        limit_str = str(limit) if limit is not None else "∞"
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget
        page = 1
        max_pages = 200

        while True:
            if time.time() - start_time > max_seconds:
                print("[en-iwhr-cn-iwhr-english-new] wall-clock budget exceeded, stopping")
                break

            if saved >= limit_val:
                break

            if page > max_pages:
                print(f"[en-iwhr-cn-iwhr-english-new] safety cap {max_pages} pages reached")
                break

            list_url = _PAPERS_TPL.format(page=page)
            html = _curl(list_url)
            if not html:
                print(f"[en-iwhr-cn-iwhr-english-new] failed to fetch page {page}, stopping")
                break

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[en-iwhr-cn-iwhr-english-new] parse error page {page}: {exc}")
                page += 1
                continue

            if soup is None:
                break

            items = soup.select(".PapersList li")
            if not items:
                print(f"[en-iwhr-cn-iwhr-english-new] no items on page {page}, stopping")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_val:
                    break

                link = item.find("a")
                if not link:
                    continue
                href = link.get("href", "")
                if not href:
                    continue

                detail_url = _abs_url(href)
                if not detail_url:
                    continue

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                title_from_list = link.get_text(strip=True)
                date_p = item.find("p", class_="date")
                listed_date = date_p.get_text(strip=True) if date_p else None

                try:
                    paper_dict = self._parse_detail(detail_url, title_from_list, listed_date)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[en-iwhr-cn-iwhr-english-new] item {detail_url} failed: {exc}")
                    continue

                if paper_dict is None:
                    continue

                try:
                    self._save_paper(paper_dict)
                    saved += 1
                except Exception as exc:
                    print(f"[en-iwhr-cn-iwhr-english-new] save error {detail_url}: {exc}")
                    continue

                time.sleep(1.0)

            if page % 10 == 0:
                print(
                    f"[en-iwhr-cn-iwhr-english-new] page {page}: saved {saved}/{limit_str}"
                )

            if new_on_page == 0:
                print(f"[en-iwhr-cn-iwhr-english-new] no new URLs on page {page}, stopping")
                break

            # Next-page detection: look for 下一页 link or explicit next-page href
            has_next = bool(
                soup.find("a", string=lambda s: s and "下一页" in s)
            ) or bool(
                soup.find("a", href=lambda h: h and f"index_{page + 1}.htm" in str(h))
            )
            if not has_next:
                print(f"[en-iwhr-cn-iwhr-english-new] no next page after page {page}")
                break

            page += 1

        print(f"[en-iwhr-cn-iwhr-english-new] done: saved {saved} total")
        return saved

    def _parse_detail(self, url, title_fallback, listed_date):
        """Fetch paper detail page. Returns paper_dict or None (skip)."""
        html = _curl(url)
        if not html:
            return None

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[en-iwhr-cn-iwhr-english-new] detail parse error {url}: {exc}")
            return None
        if soup is None:
            return None

        # Title from <h2>
        h2 = soup.find("h2")
        title = (h2.get_text(strip=True) if h2 else None) or title_fallback

        # Published date (full ISO: 2023-11-28) from <div class="date">
        date_div = soup.find("div", class_="date")
        published_date = date_div.get_text(strip=True) if date_div else listed_date

        # External ID: 10–16 digit numeric segment in URL
        id_m = re.search(r"/(\d{10,16})\.htm", url)
        external_id = id_m.group(1) if id_m else None

        # Main content lives in <div class="zwbox"> > <DIV id="BodyLabel">
        zwbox = soup.find("div", class_="zwbox")
        if not zwbox:
            print(f"[en-iwhr-cn-iwhr-english-new] skip (no zwbox): {url}")
            return None

        body_div = zwbox
        for tag in zwbox.find_all(True):
            if tag.get("id", "").lower() == "bodylabel":
                body_div = tag
                break

        paragraphs = [p for p in body_div.find_all("p") if p.get_text(strip=True)]

        authors = None
        abstract_parts = []
        keywords = None
        pdf_url = None

        for i, p in enumerate(paragraphs):
            text = p.get_text(separator=" ", strip=True)
            if not text:
                continue

            # Keywords paragraph: "Keywords: ..." or "Key words: ..."
            kw_m = re.match(r"^Key\s*words?\s*[:：]\s*(.+)", text, re.IGNORECASE)
            if kw_m:
                keywords = kw_m.group(1).strip()
                continue

            # Collect PDF links within this paragraph
            for a_tag in p.find_all("a", href=re.compile(r"\.pdf", re.I)):
                href = a_tag.get("href", "")
                if href and pdf_url is None:
                    pdf_url = _abs_url(href)

            # First non-empty paragraph: author list detection.
            # Pattern: "SURNAME Firstname[, SURNAME Firstname, ...]"
            # At least one UPPERCASE word followed by a capitalised word.
            if (
                i == 0
                and re.search(r"\b[A-Z]{2,}\s+[A-Z][a-z]", text)
                and len(text) < 300
            ):
                authors = text
                continue

            abstract_parts.append(text)

        abstract = " ".join(abstract_parts).strip()

        if len(abstract) < 50:
            print(
                f"[en-iwhr-cn-iwhr-english-new] skip (abstract {len(abstract)} chars): {url}"
            )
            return None

        # Broader PDF search if not found inside paragraphs
        if pdf_url is None:
            for a_tag in zwbox.find_all("a", href=re.compile(r"\.pdf", re.I)):
                href = a_tag.get("href", "")
                if href:
                    pdf_url = _abs_url(href)
                    break

        original_filename = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "url": url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": authors,
            "keywords": keywords,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "IWHR (China Institute of Water Resources and Hydropower Research)",
            "metadata": json.dumps(
                {"posted_date": listed_date, "external_id": external_id},
                ensure_ascii=False,
            ),
        }
