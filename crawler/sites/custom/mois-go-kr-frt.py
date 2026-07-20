# -*- coding: utf-8 -*-
"""행정안전부 보도자료 crawler (mois.go.kr)."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BBS_ID = "BBSMSTR_000000000008"
_LIST_URL = "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardList.do"
_DETAIL_URL = "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do"
_PAGE_SIZE = 10
_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60


class MoisGoKrFrtCrawler(BaseCrawler):
    site_id = "mois-go-kr-frt"
    site_name = "Custom: mois-go-kr-frt"
    base_url = "https://www.mois.go.kr"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl(self, method, url, form_data=None):
        """Run curl; return decoded text or None on failure."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
        ]
        if method.upper() == "POST" and form_data:
            cmd += ["-X", "POST", "-H", "Content-Type: application/x-www-form-urlencoded"]
            for k, v in form_data.items():
                cmd += ["--data-urlencode", f"{k}={v}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[mois-go-kr-frt] empty response, retry in {wait}s…")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    time.sleep([1, 3, 9][attempt])
                else:
                    print(f"[mois-go-kr-frt] curl timeout after 3 attempts")
            except Exception as exc:
                if attempt < 2:
                    time.sleep([1, 3, 9][attempt])
                else:
                    print(f"[mois-go-kr-frt] curl error: {exc}")
        return None

    def _fetch_list(self, page):
        return self._curl(
            "GET",
            f"{_LIST_URL}?bbsId={_BBS_ID}&pageIndex={page}",
        )

    def _fetch_detail(self, ntt_id):
        return self._curl("GET", f"{_DETAIL_URL}?bbsId={_BBS_ID}&nttId={ntt_id}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Return a BeautifulSoup object using the best available parser."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        raise RuntimeError("No HTML parser available")

    def _parse_list(self, html):
        """Return list of item dicts from the board list page."""
        items = []
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[mois-go-kr-frt] list parse (soup init): {exc}")
            return items

        try:
            tbody = soup.find("tbody")
            if not tbody:
                return items

            for row in tbody.find_all("tr"):
                tds = row.find_all("td")
                if not tds:
                    continue

                link = row.find("a", onclick=re.compile(r"fn_egov_inqire_notice"))
                if not link:
                    continue

                m = re.search(r"fn_egov_inqire_notice\('(\d+)'", link.get("onclick", ""))
                if not m:
                    continue

                ntt_id = m.group(1)
                post_number = tds[0].get_text(strip=True) if tds else ""
                title = link.get_text(strip=True)

                # Columns: 번호(0), 제목(1), 첨부(2), 작성자(3), 등록일(4), 조회수(5)
                department = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                date_str = tds[4].get_text(strip=True) if len(tds) > 4 else ""

                items.append({
                    "ntt_id": ntt_id,
                    "post_number": post_number,
                    "title": title,
                    "department": department,
                    "date": date_str,
                })
        except Exception as exc:
            print(f"[mois-go-kr-frt] list parse error: {exc}")

        return items

    @staticmethod
    def _extract_hwp_texts(obj, depth=0):
        """Recursively pull text-run values (key "t") from HWP JSON."""
        if depth > 20:
            return []
        texts = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "t" and isinstance(v, str) and v.strip():
                    texts.append(v.strip())
                elif isinstance(v, (dict, list)):
                    texts.extend(MoisGoKrFrtCrawler._extract_hwp_texts(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                texts.extend(MoisGoKrFrtCrawler._extract_hwp_texts(item, depth + 1))
        return texts

    def _parse_detail(self, html, ntt_id):
        """Extract abstract and file metadata from a detail page."""
        result = {
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
            "atch_file_id": None,
            "files": [],
        }
        if not html:
            return result

        # --- Abstract: try HWP JSON first ---
        try:
            m = re.search(r"<!--\[data-hwpjson\](.*?)\]-->", html, re.DOTALL)
            if m:
                raw_json = m.group(1).strip()
                try:
                    data = json.loads(raw_json)
                except json.JSONDecodeError:
                    # "Extra data" case: HTML comment contains `-->` inside a JSON
                    # string value; use raw_decode to consume just the first object.
                    data, _ = json.JSONDecoder().raw_decode(raw_json)
                texts = self._extract_hwp_texts(data)
                result["abstract"] = "\n".join(t for t in texts if t.strip())
        except Exception as exc:
            print(f"[mois-go-kr-frt] HWP JSON parse error (nttId={ntt_id}): {exc}")

        # --- Abstract fallback: BeautifulSoup text extraction ---
        if len(result["abstract"]) < 50:
            try:
                soup = self._make_soup(html)
                for cls in ("board_content", "table_detail_area", "bbs_content", "view_con"):
                    div = soup.find(class_=cls) or soup.find(id=cls)
                    if div:
                        txt = div.get_text(separator="\n", strip=True)
                        if len(txt) > len(result["abstract"]):
                            result["abstract"] = txt
                        break
            except Exception as exc:
                print(f"[mois-go-kr-frt] BS4 abstract fallback error (nttId={ntt_id}): {exc}")

        # --- atchFileId ---
        m_fid = re.search(r'name="atchFileId"[^>]+value="([^"]+)"', html)
        if m_fid:
            result["atch_file_id"] = m_fid.group(1)

        # --- File list: extract href + filename from <a> anchors ---
        # Pattern: <a href="/cmm/fms/FileDown.do?atchFileId=...&fileSn=N">
        #              <img ...> FILENAME &nbsp;[ SIZE ]
        #          </a>
        file_pattern = re.compile(
            r'href="(/cmm/fms/FileDown\.do\?atchFileId=([^&"]+)&(?:amp;)?fileSn=(\d+))"'
            r'[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        seen_sns = set()
        for fm in file_pattern.finditer(html):
            rel_url, atch_id, file_sn, inner = fm.groups()
            if file_sn in seen_sns:
                continue
            seen_sns.add(file_sn)

            # Extract file name from the anchor inner HTML (strip tags + whitespace)
            raw_name = re.sub(r"<[^>]+>", " ", inner)
            raw_name = re.sub(r"&nbsp;", " ", raw_name)
            raw_name = re.sub(r"&[a-zA-Z]+;", "", raw_name)
            # Trim trailing size info like "[ 147.5 KB ]"
            raw_name = re.sub(r"\[.*?\]", "", raw_name)
            fname = re.sub(r"\s+", " ", raw_name).strip()

            file_url = f"{self.base_url}{rel_url.replace('&amp;', '&')}"
            result["files"].append({
                "url": file_url,
                "fileSn": file_sn,
                "name": fname,
            })

        # Prefer PDF as the primary download, fallback to first file
        pdf_files = [f for f in result["files"]
                     if f["name"].lower().endswith(".pdf")]
        all_files = result["files"]

        primary = (pdf_files or all_files or [None])[0]
        if primary:
            result["pdf_url"] = primary["url"]
            result["original_filename"] = primary["name"] or None

        return result

    @staticmethod
    def _parse_date(raw):
        """Convert '2026.05.13.' → '2026-05-13'."""
        if not raw:
            return None
        m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", raw.strip())
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return raw.strip().rstrip(".")

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_ntt_ids = set()
        limit_or_inf = limit if limit is not None else "∞"
        start_time = time.time()

        while True:
            # Limit / time / page-cap checks
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[mois-go-kr-frt] Safety cap: {_MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > _MAX_SECONDS:
                print(f"[mois-go-kr-frt] Time budget ({_MAX_SECONDS}s) exceeded. Stopping.")
                break

            if page % 10 == 0:
                print(f"[mois-go-kr-frt] page {page}: saved {saved}/{limit_or_inf}")

            # Fetch list page
            html = self._fetch_list(page)
            if not html:
                print(f"[mois-go-kr-frt] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list(html)
            if not items:
                print(f"[mois-go-kr-frt] No items on page {page}. Done.")
                break

            # URL deduplication to detect silent pagination loops
            new_items = [it for it in items if it["ntt_id"] not in seen_ntt_ids]
            if not new_items:
                print(f"[mois-go-kr-frt] All items on page {page} already seen. Done.")
                break
            for it in new_items:
                seen_ntt_ids.add(it["ntt_id"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                ntt_id = item["ntt_id"]
                detail_url = (
                    f"{self.base_url}/frt/bbs/type010/commonSelectBoardArticle.do"
                    f"?bbsId={_BBS_ID}&nttId={ntt_id}"
                )

                try:
                    time.sleep(self._delay)

                    # Fetch detail with per-item retry
                    detail_html = None
                    for attempt in range(3):
                        detail_html = self._fetch_detail(ntt_id)
                        if detail_html:
                            break
                        wait = [1, 3, 9][attempt]
                        print(
                            f"[mois-go-kr-frt] detail {ntt_id} retry {attempt+1}/3 in {wait}s"
                        )
                        time.sleep(wait)

                    if not detail_html:
                        print(f"[mois-go-kr-frt] item {ntt_id} failed: no response")
                        continue

                    detail = self._parse_detail(detail_html, ntt_id)
                    abstract = detail.get("abstract", "").strip()

                    if len(abstract) < 100:
                        print(
                            f"[mois-go-kr-frt] item {ntt_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    listed_date = self._parse_date(item.get("date", ""))

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ntt_id,
                        "post_number": item.get("post_number") or ntt_id,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": listed_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "department": item.get("department", ""),
                        "publisher": "행정안전부",
                        "authors": "",
                        "keywords": "",
                        "category": "보도자료",
                        "doi": "",
                        "journal": "",
                        "metadata": json.dumps({
                            "nttId": ntt_id,
                            "bbsId": _BBS_ID,
                            "posted_date": item.get("date", ""),
                            "post_number": item.get("post_number", ""),
                            "atchFileId": detail.get("atch_file_id", ""),
                            "files": detail.get("files", []),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[mois-go-kr-frt] saved {saved}/{limit_or_inf}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mois-go-kr-frt] item {ntt_id} failed: {exc}")
                    continue

            page += 1

        print(f"[mois-go-kr-frt] Done. Total saved: {saved}")
        return saved
