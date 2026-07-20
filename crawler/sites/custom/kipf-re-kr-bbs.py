# -*- coding: utf-8 -*-
"""KIPF 보도자료 (Press Release) BBS crawler.

Target: https://www.kipf.re.kr/bbs/kor_Plaza_PressRelease.do
Uses eGovFrame BBS — GET pagination via ?pageIndex=N, detail via /view.do?nttId=X.
curl-based due to TLS quirks on Korean government sites.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.kipf.re.kr/bbs/kor_Plaza_PressRelease.do"
_DETAIL_URL = "https://www.kipf.re.kr/bbs/kor_Plaza_PressRelease/view.do"
_DOWNLOAD_BASE = "https://www.kipf.re.kr/cmm/fms/FileDown.do"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, max_time: int = 30) -> bytes | None:
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(max_time),
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=max_time + 5)
            if result.stdout:
                return result.stdout
        except Exception as exc:
            print(f"[kipf-re-kr-bbs] curl error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _make_soup(raw: bytes):
    text = _decode(raw)
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


class KipfBbsCrawler(BaseCrawler):
    site_id = "kipf-re-kr-bbs"
    site_name = "Custom: kipf-re-kr-bbs"
    base_url = "https://www.kipf.re.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_val = limit if limit is not None else float("inf")
        start_time = time.time()
        MAX_WALL = 25 * 60  # 25 minutes
        MAX_PAGES = 200

        for page in range(1, MAX_PAGES + 1):
            if time.time() - start_time > MAX_WALL:
                print(f"[kipf-re-kr-bbs] 25-min wall-clock budget reached at page {page}, stopping.")
                break

            if saved >= limit_val:
                break

            if page == MAX_PAGES:
                print(f"[kipf-re-kr-bbs] Safety cap of {MAX_PAGES} pages reached, stopping.")

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[kipf-re-kr-bbs] page {page}: saved {saved}/{lim_str}")

            # Fetch list page
            raw = _curl_get(f"{_LIST_URL}?pageIndex={page}")
            if raw is None:
                print(f"[kipf-re-kr-bbs] Failed to fetch list page {page}, stopping.")
                break

            text = _decode(raw)

            # Extract nttIds from fn_search_detail('...') onclick attributes
            ntt_ids = re.findall(r"fn_search_detail\(['\"]([A-Za-z0-9]+)['\"]", text)

            if not ntt_ids:
                print(f"[kipf-re-kr-bbs] No items on page {page}, done.")
                break

            # Deduplicate while preserving order
            seen_on_page = []
            for nid in ntt_ids:
                url = f"{_DETAIL_URL}?nttId={nid}"
                if url not in seen_urls:
                    seen_on_page.append((nid, url))

            if not seen_on_page:
                print(f"[kipf-re-kr-bbs] All items on page {page} already seen, done.")
                break

            for ntt_id, detail_url in seen_on_page:
                if saved >= limit_val:
                    break

                seen_urls.add(detail_url)

                try:
                    paper = self._fetch_detail(ntt_id, detail_url)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[kipf-re-kr-bbs] Skipping {ntt_id}: abstract too short "
                            f"({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kipf-re-kr-bbs] item {ntt_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

        print(f"[kipf-re-kr-bbs] Done. Saved {saved} items.")
        return saved

    def _fetch_detail(self, ntt_id: str, detail_url: str) -> dict | None:
        raw = None
        for attempt in range(3):
            raw = _curl_get(detail_url)
            if raw:
                break
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[kipf-re-kr-bbs] retry {attempt+1}/3 for {ntt_id} in {wait}s")
                time.sleep(wait)

        if not raw:
            print(f"[kipf-re-kr-bbs] Failed to fetch detail {ntt_id} after 3 attempts, skipping.")
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[kipf-re-kr-bbs] Parse error for {ntt_id}: {exc}")
            return None

        if soup is None:
            return None

        # --- Title ---
        tit_tag = soup.find(class_="ui bbs--view--tit")
        if tit_tag is None:
            # fallback: look for bbs--view--tit anywhere
            tit_tag = soup.select_one("[class*='bbs--view--tit']")
        title = tit_tag.get_text(" ", strip=True) if tit_tag else ""

        # --- Date ---
        pub_date = ""
        date_tag = soup.find(class_="date")
        if date_tag:
            m = re.search(r"\d{4}-\d{2}-\d{2}", date_tag.get_text())
            if m:
                pub_date = m.group(0)

        # --- Department (작성자) ---
        dept = ""
        for span in soup.find_all("span"):
            i_tag = span.find("i")
            if i_tag and "작성자" in i_tag.get_text():
                dept = span.get_text(" ", strip=True).replace("작성자", "").strip()
                break

        # --- Content / abstract ---
        content_tag = soup.find(class_="ui bbs--view--content")
        if content_tag is None:
            content_tag = soup.select_one("[class*='bbs--view--content']")

        abstract = ""
        if content_tag:
            abstract = content_tag.get_text(separator="\n", strip=True)
            abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

        # --- PDF URL ---
        text = _decode(raw)
        pdf_url = ""
        # Each file reference: fn_egov_downFile('FILE_ID', 'SN')
        file_refs = re.findall(
            r"fn_egov_downFile\(['\"]([A-Za-z0-9_]+)['\"],\s*['\"](\d+)['\"]",
            text,
        )
        for file_id, file_sn in file_refs:
            # Check if associated with a PDF (acrobat icon class nearby in raw text)
            pos = text.find(file_id)
            nearby = text[max(0, pos - 20):pos + 500] if pos >= 0 else ""
            if "acrobat" in nearby or ".pdf" in nearby.lower():
                pdf_url = f"{_DOWNLOAD_BASE}?atchFileId={file_id}&fileSn={file_sn}"
                break
        if not pdf_url and file_refs:
            file_id, file_sn = file_refs[0]
            pdf_url = f"{_DOWNLOAD_BASE}?atchFileId={file_id}&fileSn={file_sn}"

        return {
            "site_id": self.site_id,
            "external_id": ntt_id,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "보도자료",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": pub_date,
            "url": detail_url,
            "pdf_url": pdf_url or None,
            "doi": "",
            "department": dept,
            "metadata": json.dumps({"nttId": ntt_id}, ensure_ascii=False),
        }
