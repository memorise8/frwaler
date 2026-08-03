# -*- coding: utf-8 -*-
"""국가보훈부 보도자료 crawler — mpva-go-kr-mpva.

Starting URL: https://www.mpva.go.kr/mpva/selectBbsNttList.do?bbsNo=16&key=77
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE = "https://www.mpva.go.kr"
_LIST_URL = f"{_BASE}/mpva/selectBbsNttList.do"
_VIEW_URL = f"{_BASE}/mpva/selectBbsNttView.do"
_BBS_NO = "16"
_KEY = "77"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class MpvaGovKrMpvaCrawler(BaseCrawler):
    """국가보훈부 보도자료 crawler."""

    site_id = "mpva-go-kr-mpva"
    site_name = "Custom: mpva-go-kr-mpva"
    base_url = "https://www.mpva.go.kr"

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with TLS 1.3 workaround; returns decoded text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_LIST_URL}?bbsNo={_BBS_NO}&key={_KEY}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=40)
                if result.stdout:
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
                wait = 1 * (3 ** attempt)
                print(f"[{self.site_id}] Empty response (attempt {attempt+1}/3), retry in {wait}s...")
                time.sleep(wait)
            except Exception as exc:
                wait = 1 * (3 ** attempt)
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}, retry in {wait}s...")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}")
        return None

    @staticmethod
    def _make_soup(html: str):
        """Parse HTML; fallback chain html5lib → lxml → html.parser."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_list_page(self, html: str) -> list:
        """Return list of {ntt_no, post_number, title, dept, date} dicts."""
        items = []
        try:
            soup = self._make_soup(html)
            if not soup:
                return items
            for row in soup.select("table.p-table tr"):
                subj_td = row.select_one("td.p-subject")
                if not subj_td:
                    continue
                a_tag = subj_td.select_one("a[href]")
                if not a_tag:
                    continue
                href = a_tag.get("href", "")
                m = re.search(r"nttNo=(\d+)", href)
                if not m:
                    continue
                ntt_no = m.group(1)

                # Strip child span text (e.g. 새글) from title
                title = a_tag.get_text(separator=" ", strip=True)
                for span in a_tag.find_all("span"):
                    title = title.replace(span.get_text(strip=True), "")
                title = re.sub(r"\s+", " ", title).strip()

                # Column layout: [번호, 제목, 파일, 부서, 작성일]
                tds = row.find_all("td")
                post_number = re.sub(r"[^\d]", "", tds[0].get_text()) if tds else ""
                dept = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                date_text = tds[4].get_text(strip=True) if len(tds) > 4 else ""
                date = date_text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", date_text) else ""

                items.append({
                    "ntt_no": ntt_no,
                    "post_number": post_number,
                    "title": title,
                    "dept": dept,
                    "date": date,
                })
        except Exception as exc:
            print(f"[{self.site_id}] list parse error: {exc}")
        return items

    def _parse_detail_page(self, html: str, ntt_no: str) -> dict:
        """Parse detail page; returns enriched dict."""
        result = {
            "title": "",
            "date": "",
            "dept": "",
            "contact": "",
            "body": "",
            "pdf_url": None,
            "original_filename": None,
            "file_list": [],
        }
        try:
            soup = self._make_soup(html)
            if not soup:
                return result

            # Title
            subj_span = soup.select_one("span.p-table__subject_text")
            if subj_span:
                result["title"] = subj_span.get_text(strip=True)

            # Date
            time_el = soup.select_one("time")
            if time_el:
                result["date"] = time_el.get_text(strip=True)[:10]

            # Department / contact from th→td pairs
            for row in soup.select("table.p-table tr"):
                th = row.select_one("th")
                td = row.select_one("td")
                if not th or not td:
                    continue
                th_text = th.get_text(strip=True)
                td_text = td.get_text(strip=True)
                if "부서" in th_text:
                    result["dept"] = td_text
                elif "연락처" in th_text:
                    result["contact"] = td_text

            # Body text
            body_div = soup.select_one("div.p-table__text")
            if body_div:
                body_text = body_div.get_text(separator="\n", strip=True)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text)
                result["body"] = body_text.strip()

            # Attachments
            for li in soup.select("ul.p-attach li.p-attach__item"):
                dl_a = li.select_one("a.p-attach__link")
                if not dl_a:
                    continue
                href = dl_a.get("href", "")
                # Make absolute URL
                if href.startswith("./"):
                    href_abs = f"{_BASE}/mpva/{href[2:]}"
                elif href.startswith("/"):
                    href_abs = f"{_BASE}{href}"
                else:
                    href_abs = href

                # Filename: last non-icon span
                fname = ""
                for sp in dl_a.find_all("span"):
                    cls = " ".join(sp.get("class") or [])
                    if "p-icon" not in cls:
                        fname = sp.get_text(strip=True)
                        break

                result["file_list"].append({"url": href_abs, "name": fname})

                # First PDF becomes pdf_url
                if result["pdf_url"] is None and (
                    fname.lower().endswith(".pdf") or "pdf" in href_abs.lower()
                ):
                    result["pdf_url"] = href_abs
                    result["original_filename"] = fname or None

        except Exception as exc:
            print(f"[{self.site_id}] detail parse error (nttNo={ntt_no}): {exc}")
        return result

    def crawl(self, limit=None):
        """Crawl 국가보훈부 보도자료 paginated list + detail pages."""
        start_time = time.time()
        saved = 0
        seen_urls: set = set()
        page = 1
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-min budget reached at page {page}. Stopping.")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break
            if limit is not None and saved >= limit:
                break

            list_url = (
                f"{_LIST_URL}?bbsNo={_BBS_NO}&key={_KEY}"
                f"&searchCtgry=&searchCnd=all&searchKrwd=&integrDeptCode="
                f"&pageIndex={page}"
            )
            if list_url in seen_urls:
                print(f"[{self.site_id}] Duplicate list URL (infinite loop guard). Stopping.")
                break
            seen_urls.add(list_url)

            raw_list = self._curl_get(list_url)
            if not raw_list:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] 25-min budget reached inside page {page}. Stopping.")
                    break

                ntt_no = item["ntt_no"]
                detail_url = (
                    f"{_VIEW_URL}?key={_KEY}&bbsNo={_BBS_NO}&nttNo={ntt_no}"
                    f"&searchCtgry=&searchCnd=all&searchKrwd=&integrDeptCode="
                    f"&pageIndex={page}"
                )
                if detail_url in seen_urls:
                    print(f"[{self.site_id}] Duplicate detail URL nttNo={ntt_no}, skipping.")
                    continue
                seen_urls.add(detail_url)

                try:
                    time.sleep(self._delay)
                    raw_detail = self._curl_get(detail_url)
                    if not raw_detail:
                        print(f"[{self.site_id}] Failed to fetch detail nttNo={ntt_no}, skipping.")
                        continue

                    detail = self._parse_detail_page(raw_detail, ntt_no)

                    # Prefer detail-level values; fall back to list-level
                    title = detail["title"] or item["title"]
                    dept = detail["dept"] or item["dept"]
                    date = detail["date"] or item["date"]
                    contact = detail["contact"]
                    body = detail["body"]

                    # Build abstract from all available parts to ensure >= 100 chars
                    abstract_parts = []
                    if title:
                        abstract_parts.append(f"[제목] {title}")
                    if dept:
                        abstract_parts.append(f"[부서] {dept}")
                    if date:
                        abstract_parts.append(f"[작성일] {date}")
                    if contact:
                        abstract_parts.append(f"[연락처] {contact}")
                    if body:
                        abstract_parts.append("")
                        abstract_parts.append(body)
                    if detail["file_list"]:
                        fnames = [f["name"] for f in detail["file_list"] if f.get("name")]
                        if fnames:
                            abstract_parts.append("")
                            abstract_parts.append("[첨부파일]")
                            abstract_parts.extend(f"- {n}" for n in fnames[:5])

                    abstract = "\n".join(abstract_parts).strip()

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Abstract too short ({len(abstract)} chars) "
                            f"for nttNo={ntt_no}, skipping."
                        )
                        continue

                    # PDF URL
                    pdf_url = detail["pdf_url"]
                    original_filename = detail["original_filename"]
                    if not pdf_url and detail["file_list"]:
                        first = detail["file_list"][0]
                        pdf_url = first["url"]
                        original_filename = first["name"] or None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ntt_no,
                        "post_number": item["post_number"] or None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date,
                        "posted_date": date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "department": dept,
                        "publisher": dept,
                        "doi": None,
                        "keywords": None,
                        "category": "보도자료",
                        "original_filename": original_filename,
                        "authors": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": date,
                                "nttNo": ntt_no,
                                "bbsNo": _BBS_NO,
                                "contact": contact,
                                "post_number": item["post_number"],
                                "attached_files": detail["file_list"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item nttNo={ntt_no} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
