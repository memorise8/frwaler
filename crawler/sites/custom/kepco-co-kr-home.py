# -*- coding: utf-8 -*-
"""
Crawler for KEPCO (한국전력공사) 보도·설명자료.

List AJAX  : POST /home/media/newsroom/pr/boardListAjax.do  (page=N)
Detail AJAX: POST /home/media/newsroom/pr/boardViewAjax.do  (boardMngNo=15&boardNo=N)
PDF DL     : POST /c2r/FileDownload.do  (fileNo=<enc>&fileSeq=<enc>)
"""

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


class KepcoCoKrHomeCrawler(BaseCrawler):
    site_id = "kepco-co-kr-home"
    site_name = "Custom: kepco-co-kr-home"
    base_url = "https://www.kepco.co.kr"

    _LIST_AJAX_URL = "https://www.kepco.co.kr/home/media/newsroom/pr/boardListAjax.do"
    _VIEW_AJAX_URL = "https://www.kepco.co.kr/home/media/newsroom/pr/boardViewAjax.do"
    _DOWNLOAD_URL = "https://www.kepco.co.kr/c2r/FileDownload.do"
    _BOARD_MNG_NO = "15"

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                         #
    # ------------------------------------------------------------------ #

    def _curl_raw(self, url, method="GET", post_data=None, retries=3):
        """Fetch URL with curl (TLS-safe). Returns bytes or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L",
            "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "-H", f"Referer: {self.base_url}/home/media/newsroom/pr/boardList.do",
        ]
        if method == "POST":
            cmd += [
                "-X", "POST",
                "-H", "Content-Type: application/x-www-form-urlencoded",
            ]
            if post_data:
                cmd += ["--data", post_data]
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                print(f"[{self.site_id}] curl rc={result.returncode} on {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt+1}/{retries}: {exc}")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1 s → 3 s → 9 s
                print(f"[{self.site_id}] retrying in {wait}s…")
                time.sleep(wait)
        return None

    def _parse_soup(self, raw_bytes):
        """Decode bytes and parse HTML with parser fallback chain."""
        if not raw_bytes:
            return None
        text = raw_bytes.decode("utf-8", errors="replace")
        if not _HAS_BS4:
            return None
        for parser in ["html5lib", "lxml", "html.parser"]:
            try:
                return BeautifulSoup(text, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Date helpers                                                          #
    # ------------------------------------------------------------------ #

    def _parse_date(self, date_str):
        """Normalize any date string to ISO YYYY-MM-DD, or None."""
        if not date_str:
            return None
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", date_str)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        return None

    # ------------------------------------------------------------------ #
    # List page                                                             #
    # ------------------------------------------------------------------ #

    def _fetch_list_page(self, page_no):
        """Return list of (board_no, title, date_str) for one AJAX list page."""
        raw = self._curl_raw(
            self._LIST_AJAX_URL,
            method="POST",
            post_data=f"boardMngNo={self._BOARD_MNG_NO}&page={page_no}",
        )
        soup = self._parse_soup(raw)
        if not soup:
            return []

        items = []
        for item_div in soup.find_all("div", class_="media-list-item"):
            try:
                link = item_div.find("a")
                if not link:
                    continue
                m = re.search(r"fn_Detail\('(\d+)','(\d+)'\)", link.get("href", ""))
                if not m:
                    continue
                board_no = m.group(2)

                title_tag = item_div.find("strong", class_="tit")
                title = title_tag.get_text(strip=True) if title_tag else ""

                date_tag = item_div.find("span", class_="date")
                date_str = date_tag.get_text(strip=True) if date_tag else ""

                items.append((board_no, title, date_str))
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
        return items

    # ------------------------------------------------------------------ #
    # Detail page                                                           #
    # ------------------------------------------------------------------ #

    def _fetch_detail(self, board_no):
        """
        Fetch boardViewAjax and return::

            {
                "title": str,
                "date_str": str,           # 작성일 raw
                "pdf_info": (enc_no, enc_seq, fname) | None,
                "hwp_info": (enc_no, enc_seq, fname) | None,
            }
        """
        raw = self._curl_raw(
            self._VIEW_AJAX_URL,
            method="POST",
            post_data=f"boardMngNo={self._BOARD_MNG_NO}&boardNo={board_no}",
        )
        soup = self._parse_soup(raw)
        if not soup:
            return None

        title_tag = soup.find("h4", class_="detail-top-title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Published date: span.label "작성일" → sibling span.txt
        date_str = ""
        for label_span in soup.find_all("span", class_="label"):
            if "작성일" in label_span.get_text():
                txt_span = label_span.find_next_sibling("span", class_="txt")
                if txt_span:
                    date_str = txt_span.get_text(strip=True)
                break

        # File attachments — first PDF and first HWP found
        pdf_info = None
        hwp_info = None

        for file_item in soup.find_all("div", class_="file-list-item"):
            icon_img = file_item.find("img", src=re.compile(r"file_icon"))
            if not icon_img:
                continue
            icon_src = icon_img.get("src", "")

            fname_tag = file_item.find("span", class_="file-name")
            fname = fname_tag.get_text(strip=True) if fname_tag else ""

            dl_link = file_item.find("a", href=re.compile(r"downloadFile"))
            if not dl_link:
                continue
            m_dl = re.search(r"downloadFile\('([^']+)','([^']+)'\)", dl_link.get("href", ""))
            if not m_dl:
                continue
            enc_no, enc_seq = m_dl.group(1), m_dl.group(2)

            if "file_icon_pdf" in icon_src and pdf_info is None:
                pdf_info = (enc_no, enc_seq, fname)
            elif "file_icon_hwp" in icon_src and hwp_info is None:
                hwp_info = (enc_no, enc_seq, fname)

        return {
            "title": title,
            "date_str": date_str,
            "pdf_info": pdf_info,
            "hwp_info": hwp_info,
        }

    # ------------------------------------------------------------------ #
    # PDF download + text extraction                                        #
    # ------------------------------------------------------------------ #

    def _download_pdf_text(self, enc_no, enc_seq):
        """Download PDF via POST (encrypted params) and extract text. Returns str or None."""
        post_data = (
            f"fileNo={urllib.parse.quote(enc_no)}"
            f"&fileSeq={urllib.parse.quote(enc_seq)}"
        )
        raw = self._curl_raw(self._DOWNLOAD_URL, method="POST", post_data=post_data)
        if not raw or len(raw) < 512:
            return None

        # Sanity: must look like a PDF
        if not raw[:4].startswith(b"%PDF"):
            return None

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(raw)
            tmp_path = f.name
        try:
            result = subprocess.run(
                ["pdftotext", "-enc", "UTF-8", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
            return None
        except FileNotFoundError:
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Main crawl loop                                                       #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        MAX_PAGES = 200
        MAX_MINUTES = 24
        start_time = time.time()

        saved = 0
        seen_urls = set()
        limit_val = limit if limit is not None else float("inf")

        page = 1
        while saved < limit_val:
            # Wall-clock budget
            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min >= MAX_MINUTES:
                print(f"[{self.site_id}] {MAX_MINUTES}-min budget reached at page {page}, stopping")
                break
            if page > MAX_PAGES:
                print(f"[{self.site_id}] safety cap {MAX_PAGES} pages reached, stopping")
                break

            if page % 10 == 1 and page > 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_val}")

            # ── Fetch list page ──────────────────────────────────────────
            try:
                items = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] page {page} list fetch failed: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] page {page} returned 0 items, done")
                break

            # Dedup by canonical URL
            new_items = []
            for board_no, title, date_str in items:
                url = (
                    f"{self.base_url}/home/media/newsroom/pr/boardView.do"
                    f"?boardMngNo={self._BOARD_MNG_NO}&boardNo={board_no}"
                )
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_items.append((board_no, title, date_str, url))

            if not new_items:
                print(f"[{self.site_id}] page {page} all items already seen, stopping")
                break

            # ── Process each item ────────────────────────────────────────
            for board_no, list_title, list_date_str, url in new_items:
                if saved >= limit_val:
                    break

                try:
                    detail = self._fetch_detail(board_no)
                    if not detail:
                        print(f"[{self.site_id}] boardNo={board_no}: detail fetch failed, skipping")
                        continue

                    title = detail["title"] or list_title
                    published_date = self._parse_date(detail["date_str"] or list_date_str)
                    listed_date = self._parse_date(list_date_str)

                    # ── Abstract from PDF text ───────────────────────────
                    abstract = None
                    original_filename = None
                    pdf_info = detail.get("pdf_info")
                    hwp_info = detail.get("hwp_info")

                    if pdf_info:
                        enc_no, enc_seq, pdf_fname = pdf_info
                        pdf_text = self._download_pdf_text(enc_no, enc_seq)
                        if pdf_text:
                            abstract = " ".join(pdf_text.split())
                        if pdf_fname:
                            original_filename = pdf_fname + ".pdf"

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[{self.site_id}] boardNo={board_no}: "
                            f"abstract too short ({len(abstract or '')}<50), skipping"
                        )
                        continue

                    # ── Metadata ─────────────────────────────────────────
                    meta = {
                        "boardMngNo": self._BOARD_MNG_NO,
                        "boardNo": board_no,
                        "posted_date": list_date_str,
                    }
                    if hwp_info:
                        meta["hwp_filename"] = hwp_info[2]

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": board_no,
                        "post_number": board_no,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "original_filename": original_filename,
                        "publisher": "한국전력공사",
                        "category": "보도자료",
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] saved boardNo={board_no}: {title[:50]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] boardNo={board_no} failed: {exc}")
                    continue

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_val}")

            page += 1

        print(f"[{self.site_id}] crawl done: saved={saved} pages_fetched={page-1}")
        return saved
