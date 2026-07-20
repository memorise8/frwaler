# -*- coding: utf-8 -*-
"""中华人민共和国公安部 政府信息公开 crawler (app.mps.gov.cn/gdnps)."""

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_API_URL = "https://app.mps.gov.cn/gdnps/searchIndex.jsp"
_DETAIL_URL_TPL = "https://app.mps.gov.cn/gdnps/pc/content.jsp?id={}&mtype=4"
_PAGE_SIZE = 15
_MAX_PAGES = 200
_WALL_CLOCK_BUDGET_S = 25 * 60  # 25 minutes


def _strip_tags(html: str) -> str:
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&[a-zA-Z0-9#]+;", "", html)
    return re.sub(r"\s+", " ", html).strip()


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw


def _encode_params(params: dict) -> str:
    s = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    return urllib.parse.quote(urllib.parse.quote(s))


def _curl_fetch(url: str, user_agent: str) -> str | None:
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {user_agent}",
        "-H", "Accept: application/json, */*",
        "-H", "Referer: https://app.mps.gov.cn/gdnps/pc/index.jsp?mtype=4",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=35,
                                errors="replace", encoding="utf-8")
        out = result.stdout.strip()
        return out if out else None
    except Exception:
        return None


def _parse_jsonp(raw: str) -> dict | None:
    raw = raw.strip()
    raw = re.sub(r"^[a-zA-Z_$][a-zA-Z0-9_$]*\(", "", raw)
    raw = re.sub(r"\);?\s*$", "", raw)
    return json.loads(raw)


def _fetch_with_retry(url: str, user_agent: str, retries: int = 3) -> dict | None:
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            raw = _curl_fetch(url, user_agent)
            if raw:
                return _parse_jsonp(raw)
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delays[attempt])
    return None


class AppMpsGovCnGdnpsCrawler(BaseCrawler):
    """中华人民共和国公安部 政府信息公开 crawler."""

    site_id = "app-mps-gov-cn-gdnps"
    site_name = "Custom: app-mps-gov-cn-gdnps"
    base_url = "https://app.mps.gov.cn"

    def _list_url(self, page: int) -> str:
        params = {
            "goPage": page,
            "orderBy": [{"orderBy": "scrq", "reverse": True}],
            "pageSize": _PAGE_SIZE,
            "queryParam": [],
            "doRepeated": "0",
        }
        return f"{_API_URL}?params={_encode_params(params)}&callback=cb"

    def _detail_url(self, item_id: str) -> str:
        try:
            id_int = int(item_id)
        except (ValueError, TypeError):
            id_int = item_id
        params = {
            "goPage": 1,
            "orderBy": [{"orderBy": "scrq", "reverse": True}],
            "pageSize": 20,
            "queryParam": [{"shortName": "id", "value": id_int}],
        }
        return f"{_API_URL}?params={_encode_params(params)}&callback=cb"

    def _fetch_detail(self, item_id: str) -> dict | None:
        url = self._detail_url(item_id)
        data = _fetch_with_retry(url, self.USER_AGENT)
        if not data:
            return None
        items = data.get("resultMap", [])
        return items[0] if items else None

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _WALL_CLOCK_BUDGET_S:
                print(f"[{self.site_id}] 25-min wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")

            data = _fetch_with_retry(self._list_url(page), self.USER_AGENT)
            if data is None:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            items = data.get("resultMap", [])
            if not items:
                print(f"[{self.site_id}] No more items at page {page}. Done.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    item_id = str(item.get("id", "")).strip()
                    if not item_id:
                        continue

                    page_url = _DETAIL_URL_TPL.format(item_id)
                    if page_url in seen_urls:
                        continue
                    seen_urls.add(page_url)
                    new_on_page += 1

                    time.sleep(self._delay)

                    detail = None
                    for attempt in range(3):
                        detail = self._fetch_detail(item_id)
                        if detail is not None:
                            break
                        wait = [1, 3, 9][attempt]
                        print(f"[{self.site_id}] detail retry {attempt+1}/3 for {item_id} in {wait}s")
                        time.sleep(wait)

                    if detail is None:
                        print(f"[{self.site_id}] item {item_id} failed: no detail. Skipping.")
                        continue

                    title = (detail.get("title") or item.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] item {item_id} has no title. Skipping.")
                        continue

                    # Abstract: prefer htmlContent from detail (full content), fall back to nrgs
                    html_content = detail.get("htmlContent") or ""
                    nrgs = detail.get("nrgs") or item.get("nrgs") or ""
                    abstract = ""
                    if html_content and html_content.strip() not in ("", "&nbsp;"):
                        try:
                            abstract = _strip_tags(html_content)
                        except Exception:
                            abstract = ""
                    if len(abstract) < 50 and nrgs:
                        try:
                            abstract = _strip_tags(nrgs)
                        except Exception:
                            abstract = ""

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {item_id} abstract too short "
                              f"({len(abstract)} chars). Skipping.")
                        continue

                    # Dates
                    publish_time = detail.get("publishTime") or item.get("publishTime") or ""
                    scrq = detail.get("scrq") or item.get("scrq") or ""
                    published_date = _parse_date(scrq) or _parse_date(publish_time)
                    listed_date = _parse_date(publish_time) or published_date

                    # Authors / Publisher
                    zz = (detail.get("zz") or item.get("zz") or "").strip()
                    fbjg = (detail.get("fbjg") or item.get("fbjg") or "").strip()

                    # Category
                    ztfl = detail.get("ztfl") or item.get("ztfl") or ""
                    zpfl = detail.get("zpfl") or item.get("zpfl") or ""
                    subject_name = detail.get("subjectName") or item.get("subjectName") or ""
                    category = ztfl or zpfl or subject_name

                    # Metadata
                    syh = detail.get("syh") or item.get("syh") or ""
                    own_subject_dn = detail.get("ownSubjectDn") or item.get("ownSubjectDn") or ""
                    yxx = (detail.get("yxxval") or detail.get("yxx") or
                           item.get("yxxval") or item.get("yxx") or "")
                    mc = detail.get("mc") or item.get("mc") or ""
                    dexbt = detail.get("dexbt") or item.get("dexbt") or ""

                    metadata_dict = {
                        "posted_date": publish_time,
                        "syh": syh,
                        "mc": mc,
                        "dexbt": dexbt,
                        "ownSubjectDn": own_subject_dn,
                        "ownSubjectId": (detail.get("ownSubjectId") or item.get("ownSubjectId") or ""),
                        "yxx": yxx,
                        "zpfl": zpfl,
                        "ztfl": ztfl,
                        "subjectName": subject_name,
                        "category": category,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item_id,
                        "post_number": item_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "listed_date": listed_date,
                        "authors": zz,
                        "publisher": fbjg,
                        "department": "",
                        "journal": "",
                        "url": page_url,
                        "pdf_url": None,
                        "keywords": "",
                        "category": category,
                        "doi": "",
                        "original_filename": None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] Page {page} had no new items (all duplicates). Stopping.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
