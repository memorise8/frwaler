# -*- coding: utf-8 -*-
"""한국철도공사(코레일) 보도자료 BBS crawler.

Starting URL: https://info.korail.com/info/selectBbsNttList.do?bbsNo=199&key=911
"""

import json
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, __file__.split("/crawler/")[0])
from crawler.base_crawler import BaseCrawler

_BASE = "https://info.korail.com"
_LIST_URL = _BASE + "/info/selectBbsNttList.do"
_DETAIL_URL = _BASE + "/info/selectBbsNttView.do"
_DOWNLOAD_BASE = _BASE + "/info/downloadBbsFile.do"
_BBS_NO = "199"
_KEY = "911"


def _try_bs4(content):
    """Parse HTML with html5lib → lxml → html.parser fallback. Returns soup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(content, parser)
        except Exception:
            continue
    return None


def _strip_tags(html):
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class InfoKorailComInfoCrawler(BaseCrawler):
    site_id = "info-korail-com-info"
    site_name = "Custom: info-korail-com-info"
    base_url = _BASE

    _MAX_PAGES = 200

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None):
        """GET via curl with TLS workaround. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}): {exc}")
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
        return None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_list(self, content):
        """Return list of {ntt_no, title, listed_date} dicts from a list-page HTML."""
        items = []
        soup = _try_bs4(content)

        if soup:
            for a in soup.find_all("a", href=re.compile(r"selectBbsNttView")):
                href = a.get("href", "")
                m = re.search(r"nttNo=(\d+)", href)
                if not m:
                    continue
                ntt_no = m.group(1)

                # Title: strip any badge text (새글 etc.)
                for badge in a.find_all("span"):
                    badge.decompose()
                title = a.get_text(" ", strip=True)
                if not title:
                    continue

                # Date from sibling tds in the same tr
                listed_date = ""
                tr = a.find_parent("tr")
                if tr:
                    for td in tr.find_all("td"):
                        t = td.get_text(strip=True)
                        if re.match(r"\d{4}-\d{2}-\d{2}", t):
                            listed_date = t[:10]
                            break

                items.append({
                    "ntt_no": ntt_no,
                    "title": title,
                    "listed_date": listed_date,
                })
        else:
            # Regex fallback when bs4 unavailable
            for href, title_raw, date_raw in re.findall(
                r'href="[^"]*selectBbsNttView[^"]*nttNo=(\d+)[^"]*"[^>]*>'
                r'(.*?)</a>.*?(\d{4}-\d{2}-\d{2})',
                content, re.S
            ):
                title = _strip_tags(title_raw).strip()
                if title:
                    items.append({
                        "ntt_no": href,
                        "title": title,
                        "listed_date": date_raw,
                    })

        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, content, ntt_no):
        """Return dict with abstract, published_date, pdf_url, original_filename."""
        result = {
            "abstract": "",
            "published_date": "",
            "pdf_url": None,
            "original_filename": None,
            "attachments": [],
        }
        soup = _try_bs4(content)

        if soup:
            # Title (cross-check, may override list title)
            title_div = soup.find(class_="detail_title")
            result["title"] = title_div.get_text(" ", strip=True) if title_div else ""

            # Published date
            info_ul = soup.find(class_="detail_info")
            if info_ul:
                date_li = info_ul.find("li", class_="date")
                if date_li:
                    span = date_li.find("span")
                    if span:
                        result["published_date"] = span.get_text(strip=True)

            # Body content from detail_content td
            content_td = soup.find("td", class_="detail_content")
            if content_td:
                for tag in content_td.find_all(["script", "style"]):
                    tag.decompose()
                result["abstract"] = content_td.get_text(separator=" ", strip=True)

            # Fallback if empty
            if not result["abstract"]:
                for cls in ("view-content", "bbs_content", "cont_area", "article_content"):
                    el = soup.find(class_=cls)
                    if el:
                        result["abstract"] = el.get_text(separator=" ", strip=True)
                        break

            # Attached files: downloadBbsFile.do links
            for a_tag in soup.find_all("a", href=re.compile(r"downloadBbsFile")):
                href = a_tag.get("href", "")
                # Resolve relative
                if href.startswith("./"):
                    href = _BASE + "/info/" + href[2:]
                elif href.startswith("/"):
                    href = _BASE + href
                elif not href.startswith("http"):
                    href = _BASE + "/info/" + href

                # Filename: prefer file_name span, else anchor text
                fname = ""
                parent = (a_tag.find_parent(class_="attach_file")
                          or a_tag.find_parent("li")
                          or a_tag.find_parent("td"))
                if parent:
                    fn_span = parent.find(class_="file_name")
                    if fn_span:
                        fname = fn_span.get_text(strip=True)
                if not fname:
                    fname = a_tag.get_text(strip=True)
                fname = re.sub(r"\s+", " ", fname).strip()

                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                result["attachments"].append({"url": href, "filename": fname, "ext": ext})

                # Prefer PDF, else take first
                if result["pdf_url"] is None or ext == "pdf":
                    result["pdf_url"] = href
                    result["original_filename"] = fname or None

        else:
            # Regex fallback
            title_m = re.search(r'class="detail_title"[^>]*>(.*?)</div>', content, re.S)
            if title_m:
                result["title"] = _strip_tags(title_m.group(1))

            date_m = re.search(r'class="date"[^>]*>.*?<span>([\d-]+)</span>', content, re.S)
            if date_m:
                result["published_date"] = date_m.group(1).strip()

            body_m = re.search(r'class="detail_content"[^>]*>(.*?)</td>', content, re.S)
            if body_m:
                result["abstract"] = _strip_tags(body_m.group(1))

            for href, in re.findall(
                r'href="([^"]*downloadBbsFile[^"]*)"', content
            ):
                if href.startswith("./"):
                    href = _BASE + "/info/" + href[2:]
                elif href.startswith("/"):
                    href = _BASE + href
                if result["pdf_url"] is None:
                    result["pdf_url"] = href

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Korail press releases BBS (info.korail.com, bbsNo=199)."""
        saved = 0
        seen_ids = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"
        list_referer = f"{_LIST_URL}?bbsNo={_BBS_NO}&key={_KEY}"

        for page in range(1, self._MAX_PAGES + 1):
            # 25-minute wall-clock budget
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-min budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = (
                f"{_LIST_URL}?bbsNo={_BBS_NO}&key={_KEY}"
                f"&searchCtgry=&searchCnd=all&searchKrwd=&integrDeptCode="
                f"&pageIndex={page}"
            )

            content = None
            for attempt in range(3):
                content = self._curl_get(list_url, referer=list_referer)
                if content:
                    break
                wait = (attempt + 1) * 3
                print(f"[{self.site_id}] list page {page} fetch failed, retry in {wait}s")
                time.sleep(wait)

            if not content:
                print(f"[{self.site_id}] Cannot fetch list page {page}. Stopping.")
                break

            items = self._parse_list(content)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # End-of-pagination: all items already seen (loop-back guard)
            new_items = [it for it in items if it["ntt_no"] not in seen_ids]
            if not new_items:
                print(f"[{self.site_id}] Page {page}: all items already seen. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                ntt_no = item["ntt_no"]
                if ntt_no in seen_ids:
                    continue
                seen_ids.add(ntt_no)

                try:
                    time.sleep(self._delay)

                    detail_url = (
                        f"{_DETAIL_URL}?key={_KEY}&bbsNo={_BBS_NO}&nttNo={ntt_no}"
                        f"&searchCtgry=&searchCnd=all&searchKrwd=&integrDeptCode="
                        f"&pageIndex={page}"
                    )

                    detail_content = None
                    for attempt in range(3):
                        detail_content = self._curl_get(detail_url, referer=list_url)
                        if detail_content:
                            break
                        wait = (attempt + 1) * 3
                        print(f"[{self.site_id}] detail {ntt_no} retry {attempt + 1} in {wait}s")
                        time.sleep(wait)

                    if not detail_content:
                        print(f"[{self.site_id}] item {ntt_no} failed: no response")
                        continue

                    detail = self._parse_detail(detail_content, ntt_no)
                    abstract = detail["abstract"]

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {ntt_no} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    title = detail.get("title") or item["title"]
                    published_date = detail.get("published_date") or item["listed_date"]
                    listed_date = item["listed_date"]

                    canonical_url = (
                        f"{_DETAIL_URL}?key={_KEY}&bbsNo={_BBS_NO}&nttNo={ntt_no}"
                    )

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ntt_no,
                        "post_number": ntt_no,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": "",
                        "publisher": "한국철도공사",
                        "department": "",
                        "url": canonical_url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "keywords": "",
                        "category": "보도자료",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "nttNo": ntt_no,
                                "bbsNo": _BBS_NO,
                                "posted_date": listed_date,
                                "originalFilename": detail["original_filename"],
                                "attachments": detail["attachments"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_label}: "
                        f"{title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {ntt_no} failed: {exc}")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
