# -*- coding: utf-8 -*-
"""MFDS (식품의약품안전처) 보도자료 crawler.

Crawls https://www.mfds.go.kr/brd/m_99/list.do — Korean Food and Drug
Safety Administration press releases.

List page: /brd/m_99/list.do?page=N  (10 items/page)
Detail:    /brd/m_99/view.do?seq=XXXXX&page=1
Files:     /brd/m_99/down.do?brd_id=ntc0021&seq=XXXXX&data_tp=A&file_seq=N

Abstract is extracted from the attached PDF via pdftotext. The inline HTML body
contains only the title, so the PDF is the only reliable source of rich text.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler


class MFDSBoardCrawler(BaseCrawler):
    """Crawler for MFDS (식품의약품안전처) 보도자료 board m_99."""

    site_id = "mfds-go-kr-brd"
    site_name = "Custom: mfds-go-kr-brd"
    base_url = "https://www.mfds.go.kr"

    _LIST_URL = "https://www.mfds.go.kr/brd/m_99/list.do"
    _VIEW_URL = "https://www.mfds.go.kr/brd/m_99/view.do"
    _DOWN_BASE = "https://www.mfds.go.kr/brd/m_99/down.do"
    _BRD_ID = "ntc0021"
    _PAGE_SIZE = 10    # items per list page
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))   # safety pagination cap
    _RATE_LIMIT = 1.0  # seconds between detail fetches
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
    _MIN_ABSTRACT_LEN = 50  # items shorter than this are skipped (not saved)

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url, params=None):
        """HTTP GET via curl (TLS-max 1.3, no cert verify). Returns bytes or None."""
        full_url = url
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{qs}"

        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8",
            "-H", f"Referer: {self._LIST_URL}",
            full_url,
        ]
        delays = [1, 3, 9]
        for attempt, delay in enumerate(delays):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.stdout:
                    return result.stdout
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response (attempt {attempt + 1}/3), "
                          f"retrying in {delay}s…")
                    time.sleep(delay)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    print(f"[{self.site_id}] Timeout (attempt {attempt + 1}/3), "
                          f"retrying in {delay}s…")
                    time.sleep(delay)
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}, "
                          f"retrying in {delay}s…")
                    time.sleep(delay)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _decode(self, raw_bytes):
        """Decode bytes with encoding fallback; never raises."""
        if not raw_bytes:
            return ""
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return raw_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw_bytes.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html_text):
        """Parse HTML with parser fallback chain. Never raises."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html_text, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return [(seq, title, date_str), ...] from a list page HTML."""
        items = []
        soup = self._make_soup(html)

        if soup:
            for a in soup.find_all("a", class_="title"):
                href = a.get("href", "")
                # Match ?seq=DIGITS — not itm_seq_1 etc.
                m = re.search(r'[?&]seq=(\d+)', href)
                if not m:
                    continue
                seq = m.group(1)
                title = a.get_text(separator=" ", strip=True)

                # Find date in the closest winfo container
                date_str = ""
                node = a.parent
                for _ in range(8):
                    if node is None:
                        break
                    winfo = node.find(class_=re.compile(r'winfo|date'))
                    if winfo:
                        dm = re.search(r'(\d{4}-\d{2}-\d{2})', winfo.get_text())
                        if dm:
                            date_str = dm.group(1)
                            break
                    node = node.parent

                items.append((seq, title, date_str))
        else:
            # Regex fallback when BS4 is unavailable / fails
            for m in re.finditer(
                r'view\.do\?seq=(\d+)[^"\']*["\'][^>]*class="title"[^>]*>(.*?)</a>',
                html, re.DOTALL
            ):
                seq = m.group(1)
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                items.append((seq, title, ""))

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, seq):
        """Fetch detail page. Returns (date_str, pdf_url_or_None, inline_text)."""
        raw = self._curl_get(self._VIEW_URL, {"seq": seq, "page": "1"})
        if not raw:
            return "", None, ""

        html = self._decode(raw)

        # Registration date
        date_str = ""
        dm = re.search(r'등록일\s*</span>\s*(\d{4}-\d{2}-\d{2})', html)
        if dm:
            date_str = dm.group(1)

        # File links — prefer PDF; fall back to any download link
        pdf_url = None
        any_url = None

        soup = self._make_soup(html)
        if soup:
            for li in soup.select("ul.bbs_file_view_list li"):
                strong = li.find("strong")
                link = li.find("a", class_="bbs_icon_filedown")
                if not (strong and link):
                    continue
                filename = strong.get_text(strip=True).lower()
                href = link.get("href", "")
                # Resolve relative URL (BS4 does NOT decode &amp; → & in attrs)
                href = href.replace("&amp;", "&")
                if href.startswith("./"):
                    href = f"https://www.mfds.go.kr/brd/m_99/{href[2:]}"
                elif href.startswith("/"):
                    href = f"https://www.mfds.go.kr{href}"
                if any_url is None:
                    any_url = href
                if ".pdf" in filename and pdf_url is None:
                    pdf_url = href
        else:
            # Regex fallback
            for m in re.finditer(
                r'<strong>([^<]+)</strong>.*?'
                r'href=["\'](\./down\.do\?[^"\']+)["\'].*?class="bbs_icon_filedown"',
                html, re.DOTALL | re.IGNORECASE
            ):
                fname = m.group(1).lower()
                href = (m.group(2)
                        .replace("./", "https://www.mfds.go.kr/brd/m_99/")
                        .replace("&amp;", "&"))
                if any_url is None:
                    any_url = href
                if ".pdf" in fname and pdf_url is None:
                    pdf_url = href

            if pdf_url is None and any_url is None:
                m2 = re.search(r'href=["\'](\./down\.do\?[^"\']+)["\']', html)
                if m2:
                    any_url = (m2.group(1)
                               .replace("./", "https://www.mfds.go.kr/brd/m_99/")
                               .replace("&amp;", "&"))

        # Inline content (usually just the title, kept as last-resort fallback)
        inline_text = ""
        m3 = re.search(r'class="bv_cont"[^>]*>(.*?)</div>', html, re.DOTALL)
        if m3:
            raw_inner = m3.group(1)
            inline_text = re.sub(r"<[^>]+>", " ", raw_inner)
            inline_text = re.sub(r"&[a-zA-Z#0-9]+;", " ", inline_text)
            inline_text = re.sub(r"\s+", " ", inline_text).strip()

        return date_str, pdf_url or any_url, inline_text

    # ------------------------------------------------------------------
    # PDF text extraction
    # ------------------------------------------------------------------

    def _extract_pdf_text(self, url):
        """Download a PDF and return plain text via pdftotext. Returns '' on failure."""
        raw = self._curl_get(url)
        if not raw or len(raw) < 200:
            return ""

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(raw)
                tmp = f.name

            result = subprocess.run(
                ["pdftotext", tmp, "-"],
                capture_output=True, timeout=30
            )
            text = result.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
            return text
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext failed for {url}: {exc}")
            return ""
        finally:
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MFDS 보도자료 board m_99 and save up to ``limit`` records.

        Walks pages until (a) ``saved >= limit``, (b) a page returns no new
        items, or (c) the 200-page / 25-minute safety caps are reached.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_disp = str(limit) if limit is not None else "∞"

        while True:
            # --- Safety guards ---
            elapsed = time.time() - start_time
            if elapsed > self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL_SECONDS}s) reached. "
                      f"Stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            # --- Fetch list page ---
            raw = self._curl_get(self._LIST_URL, {"page": str(page)})
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            html = self._decode(raw)
            items = self._parse_list_page(html)

            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_disp}")

            new_on_page = 0

            for seq, title, list_date in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = f"{self._VIEW_URL}?seq={seq}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._RATE_LIMIT)

                    date_str, pdf_url, inline_text = self._fetch_detail(seq)
                    published_date = date_str or list_date or ""

                    # Build abstract: PDF text first, inline HTML as fallback
                    abstract = ""
                    if pdf_url:
                        abstract = self._extract_pdf_text(pdf_url)
                    if not abstract:
                        abstract = inline_text

                    if len(abstract) < self._MIN_ABSTRACT_LEN:
                        print(f"[{self.site_id}] seq={seq} skipped: abstract too short "
                              f"({len(abstract)} chars)")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": seq,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "보도자료",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": f"{self._VIEW_URL}?seq={seq}&page=1",
                        "pdf_url": pdf_url or "",
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps({
                            "board_id": self._BRD_ID,
                            "seq": seq,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_disp}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item seq={seq} failed: {exc}")
                    continue

            # Stop if every item on this page was a duplicate (paginator looped back)
            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
