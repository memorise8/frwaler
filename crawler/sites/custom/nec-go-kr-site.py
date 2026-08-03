# -*- coding: utf-8 -*-
"""중앙선거관리위원회 보도자료 crawler.

Starting URL: https://www.nec.go.kr/site/nec/ex/bbs/List.do?cbIdx=1090
"""

import json
import os
import re
import subprocess
import tempfile
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.nec.go.kr/site/nec/ex/bbs/List.do"
_CB_IDX = "1090"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _bs(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url, max_time=30):
    """GET via curl with Korean gov TLS workaround. Returns decoded text or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", str(max_time),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9",
        url,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            try:
                text = r.stdout.decode("utf-8")
            except UnicodeDecodeError:
                text = r.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception:
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
    return None


def _curl_download_binary(url, dest, max_time=60):
    """Download binary to dest path via curl. Returns True on success."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", str(max_time),
        "-H", f"User-Agent: {_UA}",
        "-o", dest,
        url,
    ]
    for attempt in range(3):
        try:
            subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
            if os.path.isfile(dest) and os.path.getsize(dest) > 100:
                return True
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
        except Exception:
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
    return False


def _pdf_to_text(pdf_url):
    """Download PDF and extract plain text with pdftotext. Returns text or ''."""
    if not pdf_url:
        return ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        if not _curl_download_binary(pdf_url, tmp):
            return ""
        r = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, timeout=60,
        )
        try:
            return r.stdout.decode("utf-8").strip()
        except UnicodeDecodeError:
            return r.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _parse_list_page(html):
    """Parse one list page. Returns list of item dicts."""
    try:
        soup = _bs(html)
    except Exception:
        return []
    if soup is None:
        return []

    items = []
    for li in soup.select("div.tableList ul li"):
        a = li.select_one("a.btn_bbsDetail")
        if not a:
            continue
        bc_idx = a.get("data-info1", "").strip()
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not href or not bc_idx:
            continue
        detail_url = (
            "https://www.nec.go.kr" + href if href.startswith("/") else href
        )

        num_span = li.select_one("span.num")
        post_number = num_span.get_text(strip=True) if num_span else None

        date_span = li.select_one("span.date")
        listed_date = date_span.get_text(strip=True) if date_span else ""

        # First PDF link in the row (img.nec.go.kr Download.do)
        pdf_a = li.select_one("span.fileDown a[href*='Download.do']")
        pdf_url = ""
        orig_fn = ""
        if pdf_a:
            pdf_url = pdf_a.get("href", "")
            t = pdf_a.get("title", "")
            orig_fn = t.replace(" 파일다운로드", "").strip() if t else ""

        items.append({
            "bc_idx": bc_idx,
            "post_number": post_number,
            "title": title,
            "detail_url": detail_url,
            "listed_date": listed_date,
            "pdf_url": pdf_url,
            "original_filename": orig_fn,
        })
    return items


def _parse_detail_page(html):
    """Parse detail page. Returns dict with published_date, department, pdf_url, orig_fn."""
    result = {
        "published_date": "",
        "department": "",
        "pdf_url": "",
        "original_filename": "",
    }
    try:
        soup = _bs(html)
    except Exception:
        return result
    if soup is None:
        return result

    # published_date from viewDetail li containing 등록일
    for li in soup.select("ul.viewDetail li"):
        text = li.get_text()
        if "등록일" in text:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                result["published_date"] = m.group(1)
            break

    # department from satisfaction footer section
    sat = soup.select_one("div.satisfaction")
    if sat:
        t = sat.get_text()
        m = re.search(r"담당부서[:\s]*([^/\n]+)", t)
        if m:
            result["department"] = m.group(1).strip()

    # First PDF download link
    for a in soup.select("a.fileLink"):
        href = a.get("href", "")
        if not href:
            continue
        if ".pdf" in href.lower() or "Download.do" in href:
            result["pdf_url"] = href
            t = a.get("title", "")
            orig = t.replace(" 파일다운로드", "").strip() if t else ""
            if not orig:
                orig = a.get_text(separator=" ", strip=True)
            result["original_filename"] = orig
            break

    return result


class NecGoKrSiteCrawler(BaseCrawler):
    """중앙선거관리위원회 보도자료 (NEC press releases) crawler."""

    site_id = "nec-go-kr-site"
    site_name = "Custom: nec-go-kr-site"
    base_url = "https://www.nec.go.kr"

    def crawl(self, limit=None):
        """Crawl NEC 보도자료 list → detail → PDF text.

        Parameters
        ----------
        limit:
            Maximum saved items. None means unlimited.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute hard budget
        page = 1
        max_pages = 200

        while True:
            # Wall-clock budget guard
            if time.time() - start_time > max_wall:
                print(
                    f"[nec-go-kr-site] 25-minute budget reached at page {page}. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > max_pages:
                print(f"[nec-go-kr-site] Safety cap of {max_pages} pages reached. Stopping.")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[nec-go-kr-site] page {page}: saved {saved}/{lim_str}")

            # ── Fetch list page ──────────────────────────────────────────
            list_url = f"{_LIST_URL}?cbIdx={_CB_IDX}&pageIndex={page}"
            raw = None
            for attempt in range(3):
                raw = _curl_get(list_url)
                if raw:
                    break
                wait = 2 ** attempt
                print(
                    f"[nec-go-kr-site] list page {page} fetch failed "
                    f"(attempt {attempt + 1}/3), retry in {wait}s"
                )
                time.sleep(wait)

            if not raw:
                print(f"[nec-go-kr-site] list page {page} failed after 3 retries, stopping.")
                break

            try:
                items = _parse_list_page(raw)
            except Exception as e:
                print(f"[nec-go-kr-site] page {page} parse error: {e}, skipping page.")
                page += 1
                continue

            # URL dedup — detect looping or end-of-pagination
            new_items = [it for it in items if it["detail_url"] not in seen_urls]
            for it in new_items:
                seen_urls.add(it["detail_url"])

            if not new_items:
                print(f"[nec-go-kr-site] page {page}: no new items. Pagination end.")
                break

            # ── Per-item detail fetch + PDF extraction ────────────────────
            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > max_wall:
                    print("[nec-go-kr-site] 25-minute budget reached mid-page. Stopping.")
                    break

                bc_idx = item["bc_idx"]
                try:
                    # Fetch detail page
                    detail_raw = None
                    for attempt in range(3):
                        detail_raw = _curl_get(item["detail_url"])
                        if detail_raw:
                            break
                        wait = (attempt + 1) * 3
                        print(
                            f"[nec-go-kr-site] detail {bc_idx} fetch failed "
                            f"(attempt {attempt + 1}/3), retry in {wait}s"
                        )
                        time.sleep(wait)

                    # Merge detail-page data over list-page defaults
                    published_date = item["listed_date"]
                    department = ""
                    pdf_url = item["pdf_url"]
                    original_filename = item["original_filename"]

                    if detail_raw:
                        d = _parse_detail_page(detail_raw)
                        if d["published_date"]:
                            published_date = d["published_date"]
                        if d["department"]:
                            department = d["department"]
                        if not pdf_url and d["pdf_url"]:
                            pdf_url = d["pdf_url"]
                        if not original_filename and d["original_filename"]:
                            original_filename = d["original_filename"]

                    # Fallback filename from URL streFileNm param
                    if not original_filename and pdf_url:
                        m = re.search(r"streFileNm=([^&]+)", pdf_url)
                        if m:
                            original_filename = m.group(1)

                    # Extract abstract from PDF
                    abstract = _pdf_to_text(pdf_url) if pdf_url else ""

                    if len(abstract) < 50:
                        print(
                            f"[nec-go-kr-site] item {bc_idx} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": bc_idx,
                        "url": item["detail_url"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": item["listed_date"],
                        "publisher": "중앙선거관리위원회",
                        "authors": "",
                        "keywords": "",
                        "category": "보도자료",
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "doi": "",
                        "metadata": json.dumps(
                            {
                                "bcIdx": bc_idx,
                                "cbIdx": _CB_IDX,
                                "posted_date": item["listed_date"],
                                "originalFilename": original_filename,
                                "department": department,
                            },
                            ensure_ascii=False,
                        ),
                    }
                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[nec-go-kr-site] saved {saved}/{lim_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nec-go-kr-site] item {bc_idx} failed: {exc}")
                    continue

                time.sleep(self._delay)

            page += 1

        print(f"[nec-go-kr-site] Done. Total saved: {saved}")
        return saved
