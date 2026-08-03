# -*- coding: utf-8 -*-
"""Crawler for 성평등가족부 보도자료 (mogef.go.kr /nw/rpd/nw_rpd_s001.do?mid=news405)."""

import json
import re
import subprocess
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.mogef.go.kr"
_LIST_URL = _BASE + "/nw/rpd/nw_rpd_s001.do?mid=news405"
_DETAIL_BASE = _BASE + "/nw/rpd/nw_rpd_s001d.do?mid=news405&bbtSn="
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_PAGE_SIZE = 10


def _bs4(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No BS4 parser available")


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str) -> str | None:
    delays = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", "-L", url],
                capture_output=True, timeout=35,
            )
            if result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < 2:
                print(f"[mogef-go-kr-nw] Empty GET response, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
        except Exception as exc:
            if attempt < 2:
                print(f"[mogef-go-kr-nw] curl GET error: {exc}, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
            else:
                print(f"[mogef-go-kr-nw] curl GET failed: {exc}")
    return None


def _curl_post(url: str, data: str) -> str | None:
    delays = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                 "-X", "POST", "-d", data, url],
                capture_output=True, timeout=35,
            )
            if result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < 2:
                print(f"[mogef-go-kr-nw] Empty POST response, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
        except Exception as exc:
            if attempt < 2:
                print(f"[mogef-go-kr-nw] curl POST error: {exc}, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
            else:
                print(f"[mogef-go-kr-nw] curl POST failed: {exc}")
    return None


def _parse_list_page(html: str) -> list[dict]:
    """Parse list page HTML; return [{bbt_sn, title, dept, date, post_number}]."""
    items = []
    try:
        soup = _bs4(html)
    except Exception as exc:
        print(f"[mogef-go-kr-nw] BS4 failed on list page: {exc}")
        # Regex fallback
        for m in re.finditer(r"fn_selectView\('(\d+)'\)", html):
            items.append({"bbt_sn": m.group(1), "title": "", "dept": "",
                          "date": "", "post_number": None})
        return items

    tbody = soup.find("table", class_="brdList01")
    if not tbody:
        return items
    rows = tbody.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        # Find bbtSn from fn_selectView call in any anchor
        link = row.find("a", href=re.compile(r"fn_selectView"))
        if not link:
            continue
        m = re.search(r"fn_selectView\('(\d+)'\)", link.get("href", ""))
        if not m:
            continue
        bbt_sn = m.group(1)

        # Post number: the tnum cell (first td)
        tnum_td = row.find("td", class_="tnum")
        post_number = tnum_td.get_text(strip=True) if tnum_td else None

        # Title from link title attr or span text
        title = (link.get("title") or "").strip()
        if not title:
            span = link.find("span")
            title = span.get_text(strip=True) if span else link.get_text(strip=True)

        # Department: td without moDpNone that has department text (3rd td overall)
        dept = ""
        date = ""
        regular_cells = [c for c in cells if "tnum" not in (c.get("class") or [])]
        # cells order: [tnum(hidden), title(newtitle/title), dept(hidden), date, attach(hidden), views(hidden)]
        # Find date: YYYY-MM-DD pattern
        for td in cells:
            txt = td.get_text(strip=True)
            if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                date = txt
            elif td.get("class") and "newtitle" not in td.get("class") and \
                    "title" not in td.get("class") and "tnum" not in td.get("class") and \
                    not re.match(r"\d{4}-\d{2}-\d{2}", txt) and \
                    not re.match(r"^\d+$", txt) and txt and len(txt) < 60:
                dept = txt

        items.append({
            "bbt_sn": bbt_sn,
            "title": title,
            "dept": dept,
            "date": date,
            "post_number": post_number,
        })
    return items


def _parse_detail(html: str, bbt_sn: str) -> dict | None:
    """Parse detail page; return field dict or None on failure."""
    try:
        soup = _bs4(html)
    except Exception as exc:
        print(f"[mogef-go-kr-nw] BS4 failed on detail {bbt_sn}: {exc}")
        return None

    result: dict = {}

    # Title from brdViewTit
    tit_th = soup.find("th", class_="brdViewTit")
    if tit_th:
        result["title"] = tit_th.get_text(strip=True)
    else:
        cap = soup.find("caption")
        if cap:
            strong = cap.find("strong")
            result["title"] = (strong or cap).get_text(strip=True)

    # Metadata table: department, phone, date, views
    dept = phone = reg_date = ""
    for th in soup.find_all("th"):
        th_id = th.get("id", "")
        td = th.find_next_sibling("td")
        if not td:
            continue
        txt = td.get_text(strip=True)
        if th_id == "W_th01":
            dept = txt
        elif th_id == "W_th02":
            phone = txt
        elif th_id == "W_th03":
            reg_date = txt
        elif th_id == "W_th04":
            pass  # views — not stored

    result["dept"] = dept
    result["phone"] = phone
    result["date"] = reg_date

    # Attachments: parse fn_fileDownload calls
    pdf_filename = None
    pdf_atfile_sn = None
    pdf_atfile_seq = None
    attachments = []

    add_file_td = soup.find("td", class_="addFile")
    if add_file_td:
        for a in add_file_td.find_all("a"):
            onclick = a.get("onclick", "")
            m = re.search(r"fn_fileDownload\('(\d+)',\s*'(\d+)'\)", onclick)
            if not m:
                continue
            at_sn = m.group(1)
            at_seq = m.group(2)
            fname = a.get_text(strip=True)
            attachments.append({"atfileSn": at_sn, "atfileSeq": at_seq, "filename": fname})
            # Prefer PDF; fall back to first attachment
            if fname.lower().endswith(".pdf") and pdf_filename is None:
                pdf_filename = fname
                pdf_atfile_sn = at_sn
                pdf_atfile_seq = at_seq
        if pdf_filename is None and attachments:
            pdf_filename = attachments[0]["filename"]
            pdf_atfile_sn = attachments[0]["atfileSn"]
            pdf_atfile_seq = attachments[0]["atfileSeq"]

    result["pdf_filename"] = pdf_filename
    result["pdf_atfile_sn"] = pdf_atfile_sn
    result["pdf_atfile_seq"] = pdf_atfile_seq
    result["attachments"] = attachments

    # Body content → abstract
    body_td = soup.find("td", class_="brdViewCont")
    abstract = ""
    if body_td:
        abstract = _strip_html(str(body_td))
        abstract = re.sub(r"\s+", " ", abstract).strip()
    result["abstract"] = abstract

    return result


class MogefGoKrNwCrawler(BaseCrawler):
    """성평등가족부 보도자료 crawler."""

    site_id = "mogef-go-kr-nw"
    site_name = "Custom: mogef-go-kr-nw"
    base_url = "https://www.mogef.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        page = 1

        while saved < limit_or_inf:
            # Wall-clock budget
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[mogef-go-kr-nw] 25-minute budget reached; stopping at page {page}.")
                break

            if page > _MAX_PAGES:
                print(f"[mogef-go-kr-nw] Safety cap of {_MAX_PAGES} pages reached; stopping.")
                break

            if page % 10 == 0:
                print(f"[mogef-go-kr-nw] page {page}: saved {saved}/{limit_or_inf}")

            # Fetch list page
            post_data = f"pageIndex={page}&pageUnit={_PAGE_SIZE}&searchKeyword=&order_gubun="
            html = _curl_post(_LIST_URL, post_data)
            if not html:
                print(f"[mogef-go-kr-nw] Failed to fetch list page {page}; stopping.")
                break

            items = _parse_list_page(html)
            if not items:
                print(f"[mogef-go-kr-nw] No items on page {page}; end of pagination.")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_or_inf:
                    break

                bbt_sn = item["bbt_sn"]
                detail_url = _DETAIL_BASE + bbt_sn

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # Fetch detail page
                try:
                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[mogef-go-kr-nw] item {bbt_sn} failed: empty response; skipping.")
                        continue

                    detail = _parse_detail(detail_html, bbt_sn)
                    if not detail:
                        print(f"[mogef-go-kr-nw] item {bbt_sn} failed: parse error; skipping.")
                        continue

                    title = detail.get("title") or item.get("title") or ""
                    abstract = detail.get("abstract", "")
                    if len(abstract) < 50:
                        print(f"[mogef-go-kr-nw] item {bbt_sn}: abstract too short ({len(abstract)} chars); skipping.")
                        continue

                    reg_date = detail.get("date") or item.get("date") or ""
                    dept = detail.get("dept") or item.get("dept") or ""
                    pdf_filename = detail.get("pdf_filename")
                    pdf_atfile_sn = detail.get("pdf_atfile_sn")
                    pdf_atfile_seq = detail.get("pdf_atfile_seq")
                    attachments = detail.get("attachments", [])

                    metadata = {
                        "bbtSn": bbt_sn,
                        "bbid": "news405",
                        "phone": detail.get("phone", ""),
                        "posted_date": reg_date,
                    }
                    if pdf_atfile_sn:
                        metadata["atfileSn"] = pdf_atfile_sn
                        metadata["atfileSeq"] = pdf_atfile_seq
                        metadata["downloadFormAction"] = "/news/down.do?mid=news405"
                    if attachments:
                        metadata["attachments"] = attachments
                    if item.get("post_number"):
                        metadata["tnum"] = item["post_number"]

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": bbt_sn,
                        "post_number": item.get("post_number"),
                        "title": title,
                        "abstract": abstract,
                        "published_date": reg_date,
                        "listed_date": reg_date,
                        "url": detail_url,
                        "pdf_url": None,
                        "original_filename": pdf_filename,
                        "publisher": "성평등가족부",
                        "department": dept,
                        "authors": None,
                        "keywords": None,
                        "category": "보도자료",
                        "doi": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mogef-go-kr-nw] item {bbt_sn} failed: {exc}; skipping.")
                    continue

                time.sleep(1.0)

            if new_on_page == 0:
                print(f"[mogef-go-kr-nw] All items on page {page} already seen; stopping.")
                break

            page += 1

        print(f"[mogef-go-kr-nw] Done. Total saved: {saved}")
        return saved
