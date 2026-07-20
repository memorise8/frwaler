# -*- coding: utf-8 -*-
"""
Crawler for molit.go.kr press releases (보도자료).
List URL: https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp
Detail URL: https://www.molit.go.kr/USR/NEWS/m_71/dtl.jsp?lcmspage=1&id={id}

Auth: site sets TMOSHCooKie via 307 redirect on first visit; requests.Session
handles it automatically. SSL cert is self-signed/unknown-CA, so verify=False.

Abstract strategy:
  1. Download PDF attachment → pdfminer text extraction → first 800 chars.
  2. Fallback: title + og:title + bullet points from HTML.
  If combined abstract < 50 chars, skip the item.
"""

import io
import json
import re
import time
import urllib.parse
import warnings

import requests
import urllib3

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_html(raw):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return BeautifulSoup("", "html.parser")


def _extract_pdf_text(pdf_bytes):
    """Extract text from PDF bytes via pdfminer. Returns '' on any failure."""
    try:
        from pdfminer.high_level import extract_text as _ext
        text = _ext(io.BytesIO(pdf_bytes)) or ""
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return ""


def _fetch_with_retry(session, url, max_tries=3, **kwargs):
    """GET url via session with exponential backoff. Returns Response or None."""
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("verify", False)
    for attempt in range(max_tries):
        try:
            r = session.get(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            wait = [1, 3, 9][attempt]
            print(f"[molit-go-kr-usr] fetch {url} attempt {attempt+1}/{max_tries}: {exc}")
            if attempt < max_tries - 1:
                time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MolitGoKrUsrCrawler(BaseCrawler):
    site_id = "molit-go-kr-usr"
    site_name = "Custom: molit-go-kr-usr"
    base_url = "https://www.molit.go.kr"

    _LIST_URL = "https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp"
    _MAX_PAGES = 200
    _WALL_SECS = 25 * 60

    def crawl(self, limit=None):
        """Crawl molit.go.kr press-release list pages and persist each item."""
        # Korean gov sites use self-signed / unknown-CA certs; skip verification.
        self._session.verify = False

        saved = 0
        seen_urls = set()
        page = 1
        t0 = time.time()

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self._MAX_PAGES:
                print(f"[molit-go-kr-usr] hit page cap {self._MAX_PAGES}, stopping")
                break
            if time.time() - t0 > self._WALL_SECS:
                print("[molit-go-kr-usr] approaching 25-min wall clock, stopping")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[molit-go-kr-usr] page {page}: saved {saved}/{lim_str}")

            # ---- Fetch list page ----
            resp = self._request(
                self._LIST_URL,
                params={"psize": "10", "lcmspage": str(page)},
                verify=False,
            )
            if resp is None:
                print(f"[molit-go-kr-usr] list page {page} failed, stopping")
                break

            try:
                soup = _parse_html(resp.content.decode("utf-8", errors="replace"))
            except Exception as exc:
                print(f"[molit-go-kr-usr] parse error page {page}: {exc}")
                break

            rows = soup.select("table.bd_tbl tbody tr")
            if not rows:
                print(f"[molit-go-kr-usr] no rows on page {page}, stopping")
                break

            new_on_page = 0
            for row in rows:
                if limit is not None and saved >= limit:
                    break

                num_td = row.select_one("td.bd_num")
                title_td = row.select_one("td.bd_title")
                field_td = row.select_one("td.bd_field")
                date_td = row.select_one("td.bd_date")

                if not title_td:
                    continue
                a_tag = title_td.find("a")
                if not a_tag:
                    continue

                href = a_tag.get("href", "")
                try:
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    item_id = qs.get("id", [None])[0]
                except Exception:
                    item_id = None
                if not item_id:
                    continue

                detail_url = (
                    f"{self.base_url}/USR/NEWS/m_71/dtl.jsp?lcmspage=1&id={item_id}"
                )
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # List-level metadata
                list_title = re.sub(
                    r"\s+(새글|공지)\s*$",
                    "",
                    a_tag.get_text(separator=" ", strip=True),
                ).strip()
                category = field_td.get_text(strip=True) if field_td else None
                listed_date = date_td.get_text(strip=True) if date_td else None
                post_number = num_td.get_text(strip=True) if num_td else None

                try:
                    n = self._process_item(
                        item_id=item_id,
                        detail_url=detail_url,
                        list_title=list_title,
                        category=category,
                        listed_date=listed_date,
                        post_number=post_number,
                    )
                    if n:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[molit-go-kr-usr] item {item_id} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[molit-go-kr-usr] no new items on page {page}, stopping")
                break

            page += 1
            time.sleep(0.3)  # brief inter-page pause

        return saved

    # ------------------------------------------------------------------

    def _process_item(self, *, item_id, detail_url, list_title,
                      category, listed_date, post_number):
        """Fetch detail page, extract all fields, save. Returns 1 on save, 0 on skip."""
        time.sleep(self._delay)

        resp = _fetch_with_retry(self._session, detail_url)
        if resp is None:
            print(f"[molit-go-kr-usr] detail {item_id}: all retries failed, skipping")
            return 0

        html = resp.content.decode("utf-8", errors="replace")

        try:
            soup = _parse_html(html)
        except Exception as exc:
            print(f"[molit-go-kr-usr] HTML parse error {item_id}: {exc}, skipping")
            return 0

        bd_view = soup.find("div", class_="bd_view")
        if not bd_view:
            print(f"[molit-go-kr-usr] no bd_view for {item_id}, skipping")
            return 0

        # Title
        h4 = bd_view.find("h4")
        title = h4.get_text(strip=True) if h4 else list_title

        # og:title (contains subtitle / key-point summary)
        og_meta = soup.find("meta", {"property": "og:title"})
        og_title = og_meta.get("content", "").strip() if og_meta else ""

        # Bullet key-points
        bullets = []
        ul_lst = bd_view.find("ul", class_="bd_view_ul_lst")
        if ul_lst:
            bullets = [
                li.get_text(strip=True)
                for li in ul_lst.find_all("li")
                if li.get_text(strip=True)
            ]

        # Info: department, published_date (등록일)
        department = None
        published_date = None
        ul_info = bd_view.find("ul", class_="bd_view_ul_info")
        if ul_info:
            for li in ul_info.find_all("li"):
                strong = li.find("strong")
                span = li.find("span")
                if strong and span:
                    key = strong.get_text(strip=True)
                    val = span.get_text(strip=True)
                    if key == "담당부서":
                        department = val
                    elif key == "등록일":
                        published_date = val.split()[0] if val else None

        # Attachments
        pdf_url = None
        original_filename = None
        file_li = bd_view.find("li", class_="file")
        if file_li:
            for a in file_li.find_all("a"):
                href = a.get("href", "")
                title_attr = a.get("title", "")
                if "DWN.jsp" in href and ".pdf" in href.lower():
                    pdf_url = (
                        self.base_url + href
                        if href.startswith("/")
                        else href
                    )
                    # Extract original filename from query string
                    try:
                        qp = urllib.parse.parse_qs(
                            urllib.parse.urlparse(href).query
                        )
                        fn = qp.get("fileName", [None])[0]
                        if fn:
                            original_filename = urllib.parse.unquote_plus(fn)
                    except Exception:
                        pass
                    if not original_filename and title_attr:
                        # title_attr like "filename.pdf 파일 미리보기 새창열림"
                        original_filename = (
                            title_attr.split(" 파일")[0].strip()
                        )
                    break  # take first PDF

        # Build abstract
        abstract = self._build_abstract(
            item_id=item_id,
            title=title,
            og_title=og_title,
            bullets=bullets,
            pdf_url=pdf_url,
        )

        if len(abstract) < 50:
            print(
                f"[molit-go-kr-usr] abstract <50 chars ({len(abstract)}) for "
                f"{item_id}, skipping"
            )
            return 0

        metadata = {
            "item_id": item_id,
            "og_title": og_title,
            "bullets": bullets,
        }
        if department:
            metadata["department"] = department

        self._save_paper({
            "site_id": self.site_id,
            "external_id": item_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "department": department,
            "publisher": "국토교통부",
            "category": category,
            "keywords": None,
            "doi": None,
            "authors": None,
            "journal": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        })
        return 1

    def _build_abstract(self, *, item_id, title, og_title, bullets, pdf_url):
        """
        Primary: download PDF → pdfminer text → first 800 chars.
        Fallback: title + og_title + bullets combined.
        """
        if pdf_url:
            try:
                r = _fetch_with_retry(self._session, pdf_url)
                if r is not None and len(r.content) > 200:
                    text = _extract_pdf_text(r.content)
                    if len(text) >= 100:
                        return text[:800]
            except Exception as exc:
                print(f"[molit-go-kr-usr] PDF extract failed {item_id}: {exc}")

        # HTML fallback: combine unique text segments
        seen = set()
        parts = []
        for seg in [title, og_title] + bullets:
            if seg and seg not in seen:
                seen.add(seg)
                parts.append(seg)

        return " | ".join(parts)
