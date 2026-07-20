# -*- coding: utf-8 -*-
"""소방청 소방정책연구자료 crawler (nfa-go-kr-nfa).

Target : https://www.nfa.go.kr/nfa/publicrelations/policyarchive/policyresearch/
Board  : bbs_0000000000000613
List   : GET ?boardId=bbs_0000000000000613&mode=list&pageIdx={n}
Detail : GET ?boardId=bbs_0000000000000613&mode=view&cntId={id}
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.nfa.go.kr"
_LIST_PATH = "/nfa/publicrelations/policyarchive/policyresearch/"
_BOARD_ID = "bbs_0000000000000613"
_SAFETY_CAP = 200
_BUDGET_SECS = 25 * 60
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl; --tlsv1.0 works around Korean gov TLS quirks."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tlsv1.0", "-sk", "-L",
                    "--max-time", "30",
                    "-H", f"User-Agent: {_UA}",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = 3 ** attempt
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[nfa-go-kr-nfa] curl error (attempt {attempt+1}/{retries}): {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[nfa-go-kr-nfa] curl failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _bs4(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _clean(tag) -> str:
    """Strip tags and normalise whitespace from a BeautifulSoup tag."""
    if tag is None:
        return ""
    return re.sub(r"\s+", " ", tag.get_text(separator=" ")).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NfaGoKrNfaCrawler(BaseCrawler):
    site_id = "nfa-go-kr-nfa"
    site_name = "Custom: nfa-go-kr-nfa"
    base_url = "https://www.nfa.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        t0 = time.time()

        for page in range(1, _SAFETY_CAP + 1):
            # Wall-clock budget guard
            if time.time() - t0 > _BUDGET_SECS:
                print(f"[nfa-go-kr-nfa] 25-min budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 1:
                lim_s = str(limit) if limit is not None else "∞"
                print(f"[nfa-go-kr-nfa] page {page}: saved {saved}/{lim_s}")

            list_url = (
                _BASE + _LIST_PATH + "?"
                + urlencode({"boardId": _BOARD_ID, "mode": "list", "pageIdx": str(page)})
            )

            list_html = _curl_get(list_url)
            if not list_html:
                print(f"[nfa-go-kr-nfa] list page {page} fetch failed. Stopping.")
                break

            try:
                list_soup = _bs4(list_html)
            except Exception as exc:
                print(f"[nfa-go-kr-nfa] parse error on list page {page}: {exc}")
                continue

            if list_soup is None:
                print(f"[nfa-go-kr-nfa] failed to parse list page {page}. Stopping.")
                break

            tbody = list_soup.find("tbody")
            if not tbody:
                print(f"[nfa-go-kr-nfa] no tbody on page {page}. Done.")
                break

            rows = tbody.find_all("tr")
            if not rows:
                print(f"[nfa-go-kr-nfa] no rows on page {page}. Done.")
                break

            new_this_page = 0

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                title_td = row.find("td", class_="title")
                if not title_td:
                    continue

                link = title_td.find("a", href=True)
                if not link:
                    continue

                href = link["href"]
                m = re.search(r"cntId=(\d+)", href)
                if not m:
                    continue

                cnt_id = m.group(1)
                detail_url = (
                    _BASE + _LIST_PATH + "?"
                    + urlencode({
                        "boardId": _BOARD_ID,
                        "mode": "view",
                        "cntId": cnt_id,
                        "category": "",
                        "pageIdx": str(page),
                    })
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_this_page += 1

                # List-level fallbacks
                list_title = _clean(link)
                created_td = row.find("td", class_="created")
                list_date = _clean(created_td) if created_td else ""

                time.sleep(self._delay)

                try:
                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[nfa-go-kr-nfa] item cntId={cnt_id} failed: empty response")
                        continue

                    det = _bs4(detail_html)
                    if det is None:
                        print(f"[nfa-go-kr-nfa] item cntId={cnt_id} failed: parse returned None")
                        continue

                    # --- Title ---
                    h3 = det.find("h3", class_="detail-title")
                    title = _clean(h3) if h3 else list_title
                    if not title:
                        title = list_title

                    # --- Date + Writer ---
                    listed_date = list_date
                    writer = ""
                    view_header = det.find("div", class_="view-board-header")
                    if view_header:
                        for dl in view_header.find_all("dl"):
                            dt_tag = dl.find("dt")
                            dd_tag = dl.find("dd")
                            if not (dt_tag and dd_tag):
                                continue
                            lbl = _clean(dt_tag)
                            val = _clean(dd_tag)
                            if "작성일" in lbl:
                                listed_date = val
                            elif "작성자" in lbl:
                                writer = val

                    # --- Body content (strip hidden display:none divs first) ---
                    content_div = det.find("div", class_="board_content")
                    body_text = ""
                    if content_div:
                        for hidden in content_div.find_all(
                            True, style=re.compile(r"display\s*:\s*none", re.I)
                        ):
                            hidden.decompose()
                        body_text = _clean(content_div)

                    # --- Attachments ---
                    filenames: list[str] = []
                    file_urls: list[str] = []
                    pdf_url = None
                    original_filename = None

                    for li in det.find_all("li", class_="file"):
                        fn_span = li.find("span", class_="fileOnm")
                        fn = _clean(fn_span) if fn_span else ""
                        if fn:
                            filenames.append(fn)
                            if original_filename is None:
                                original_filename = fn

                        dl_a = li.find("a", href=True)
                        if dl_a:
                            dl_href = dl_a["href"]
                            pm = re.search(
                                r"Jnit_boardDownload\s*\(\s*['\"]?\s*(/[^;'\"]+)", dl_href
                            )
                            if pm:
                                raw = pm.group(1).split(";")[0].strip()
                                file_urls.append(_BASE + raw)

                        for btn in li.find_all("button"):
                            oc = btn.get("onclick", "")
                            pm2 = re.search(
                                r"Jnit_boardDownload\s*\(\s*['\"]?\s*(/[^;'\"]+pdfFileDownload[^;'\"]*)",
                                oc,
                            )
                            if pm2 and pdf_url is None:
                                raw2 = pm2.group(1).split(";")[0].strip()
                                pdf_url = _BASE + raw2
                                break

                    if pdf_url is None and file_urls:
                        pdf_url = file_urls[0]

                    # --- Build abstract ---
                    # Primary: title + body; pad with metadata when body is short
                    parts = [title]
                    if body_text and body_text != title:
                        parts.append(body_text)
                    abstract = "\n\n".join(p for p in parts if p)

                    if len(abstract) < 100:
                        meta_lines = []
                        if writer:
                            meta_lines.append(f"작성자: {writer}")
                        if listed_date:
                            meta_lines.append(f"작성일: {listed_date}")
                        if filenames:
                            meta_lines.append("첨부파일: " + ", ".join(filenames))
                        if meta_lines:
                            abstract = abstract + "\n" + "\n".join(meta_lines)

                    if len(abstract) < 50:
                        print(
                            f"[nfa-go-kr-nfa] cntId={cnt_id} abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    # --- Date normalisation ---
                    date_m = re.search(r"\d{4}-\d{2}-\d{2}", listed_date)
                    iso_date = date_m.group(0) if date_m else (listed_date or None)

                    paper = {
                        "site_id": self.site_id,
                        "external_id": cnt_id,
                        "post_number": cnt_id,
                        "title": title,
                        "abstract": abstract,
                        "authors": writer or None,
                        "publisher": "소방청",
                        "department": writer or None,
                        "published_date": iso_date,
                        "posted_date": iso_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "keywords": None,
                        "category": "소방정책연구자료",
                        "journal": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "cntId": cnt_id,
                                "boardId": _BOARD_ID,
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "filenames": filenames,
                                "fileUrls": file_urls,
                                "writer": writer,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_s = str(limit) if limit is not None else "∞"
                    print(f"[nfa-go-kr-nfa] saved {saved}/{lim_s}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nfa-go-kr-nfa] item cntId={cnt_id} failed: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[nfa-go-kr-nfa] page {page}: no new items. Done.")
                break

            if page == _SAFETY_CAP:
                print(f"[nfa-go-kr-nfa] safety cap of {_SAFETY_CAP} pages reached. Stopping.")

        print(f"[nfa-go-kr-nfa] done. Total saved: {saved}")
        return saved
