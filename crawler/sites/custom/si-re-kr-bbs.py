# -*- coding: utf-8 -*-
"""서울연구원 연구보고서 BBS crawler (si.re.kr).

List page:  GET /bbs/list.do?key=2024100039&pageIndex=N&orderBy=bbsOrdr+desc
Detail page: GET /bbs/view.do?key=2024100039&pstSn=<ID>
PDF:         /atch/fileDown.do?cnncSn=<pstSn>&cnncTy=bbs&ordr=2
"""

import html as html_module
import json
import re
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.si.re.kr"
_KEY = "2024100039"
_LIST_URL = f"{_BASE}/bbs/list.do"
_VIEW_URL = f"{_BASE}/bbs/view.do"
_DOWNLOAD_URL = f"{_BASE}/atch/fileDown.do"

_BS_PARSERS = ("html5lib", "lxml", "html.parser")


def _bs(html_text):
    """Parse HTML with html5lib → lxml → html.parser fallback. Never raises."""
    from bs4 import BeautifulSoup
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html_text, parser)
        except Exception:
            continue
    return None


def _txt(node):
    """Extract and normalize whitespace from a BS4 node or string."""
    if node is None:
        return ""
    raw = node.get_text(separator=" ") if hasattr(node, "get_text") else str(node)
    raw = html_module.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


class SiReKrBbsCrawler(BaseCrawler):
    site_id = "si-re-kr-bbs"
    site_name = "Custom: si-re-kr-bbs"
    base_url = _BASE

    # ------------------------------------------------------------------ helpers

    def _get(self, url, params=None, referer=None):
        """GET via self._session (maintains cookies). Retries 3×, backoff 1/3/9 s."""
        headers = {}
        if referer:
            headers["Referer"] = referer
        for attempt in range(3):
            try:
                time.sleep(self._delay)
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=30)
                resp.raise_for_status()
                return resp.content.decode(resp.apparent_encoding or "utf-8",
                                           errors="replace")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                wait = (attempt + 1) * 3
                print(f"[{self.site_id}] GET {url} attempt {attempt+1} error: {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    def _fetch_list(self, page):
        return self._get(_LIST_URL, params={
            "key": _KEY,
            "pageIndex": page,
            "orderBy": "bbsOrdr desc",
        })

    def _fetch_detail(self, pst_sn):
        return self._get(_VIEW_URL,
                         params={"key": _KEY, "pstSn": pst_sn},
                         referer=_LIST_URL)

    # ------------------------------------------------------------------ parsers

    def _parse_list(self, html_text):
        """Return list of dicts: {pst_sn, title, authors, listed_date, category, pdf_url, abstract_preview}."""
        soup = _bs(html_text)
        if soup is None:
            return []

        items = []
        for a_tag in soup.find_all("a", onclick=True):
            m = re.search(r"goView\('([^']+)'\)", a_tag.get("onclick", ""))
            if not m:
                continue
            pst_sn = m.group(1)

            # Walk up to the enclosing <li>
            li = a_tag
            for _ in range(8):
                if li is None:
                    break
                if getattr(li, "name", None) == "li":
                    break
                li = li.parent
            if li is None or getattr(li, "name", None) != "li":
                continue

            title = _txt(a_tag)
            if not title:
                continue

            # Authors (저자)
            authors = ""
            for em in li.find_all("em"):
                if "저자" in _txt(em):
                    span = em.find_next_sibling("span")
                    if span is None and em.parent:
                        span = em.parent.find("span")
                    if span:
                        authors = _txt(span)
                    break

            # Registration date (등록일) — listed_date
            listed_date = ""
            for i_tag in li.find_all("i", class_=re.compile(r"\bdate\b")):
                span = i_tag.find_next_sibling("span")
                if span:
                    listed_date = _txt(span)
                    break

            # Category (주제)
            category = ""
            for em in li.find_all("em"):
                if "주제" in _txt(em):
                    span = em.find_next_sibling("span")
                    if span is None and em.parent:
                        span = em.parent.find("span")
                    if span:
                        category = _txt(span)
                    break

            # PDF download link
            pdf_url = None
            for a in li.find_all("a", href=True):
                href = a["href"]
                if "fileDown.do" in href:
                    pdf_url = (_BASE + href) if href.startswith("/") else href
                    break

            # Abstract preview (in list HTML, the full text is present behind CSS clip)
            abstract_preview = ""
            p = li.find("p", class_=re.compile(r"txt-over"))
            if p:
                abstract_preview = _txt(p)

            items.append({
                "pst_sn": pst_sn,
                "title": title,
                "authors": authors,
                "listed_date": listed_date,
                "category": category,
                "pdf_url": pdf_url,
                "abstract_preview": abstract_preview,
            })

        return items

    def _parse_detail(self, html_text):
        """Return enrichment dict from a detail page."""
        soup = _bs(html_text)
        if soup is None:
            return {}

        result = {}

        # Full abstract from bbs-cont
        bbs_cont = soup.find("div", class_="bbs-cont")
        if bbs_cont:
            result["abstract"] = _txt(bbs_cont)

        # Keywords
        kw_ul = soup.find("ul", class_="keyword")
        if kw_ul:
            kws = [_txt(li) for li in kw_ul.find_all("li")]
            kws = [k for k in kws if k]
            result["keywords"] = ", ".join(kws)

        # All <em>LABEL</em><span>VALUE</span> pairs across mid-txt lists
        raw_meta = {}
        for mid_ul in soup.find_all("ul", class_="mid-txt"):
            for li in mid_ul.find_all("li"):
                em = li.find("em")
                span = li.find("span")
                if em and span:
                    label = _txt(em)
                    value = _txt(span)
                    if label and value:
                        raw_meta[label] = value

        result["raw_meta"] = raw_meta
        result["published_date"] = raw_meta.get("발행일", "")
        result["authors_detail"] = raw_meta.get("저자", "")
        result["category"] = raw_meta.get("주제", "")
        result["department"] = raw_meta.get("부서명", "")
        task_code = raw_meta.get("과제코드", "")
        result["task_code"] = task_code
        result["pub_type"] = raw_meta.get("발간유형", "")
        if task_code:
            result["original_filename"] = task_code + ".pdf"

        return result

    # ------------------------------------------------------------------ crawl

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SEC = 25 * 60
        MAX_PAGES = 200

        seen_urls = set()
        saved = 0
        page = 1
        limit_str = str(limit) if limit is not None else "∞"

        while page <= MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > MAX_WALL_SEC:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping at page {page}")
                break

            # Fetch and parse list page
            list_html = self._fetch_list(page)
            if not list_html:
                print(f"[{self.site_id}] page {page}: failed to fetch list, stopping")
                break

            items = self._parse_list(list_html)
            if not items:
                print(f"[{self.site_id}] page {page}: no items parsed, stopping")
                break

            new_count = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                pst_sn = item["pst_sn"]
                view_url = f"{_BASE}/bbs/view.do?key={_KEY}&pstSn={pst_sn}"

                if view_url in seen_urls:
                    continue
                seen_urls.add(view_url)
                new_count += 1

                try:
                    detail_html = self._fetch_detail(pst_sn)
                    if not detail_html:
                        print(f"[{self.site_id}] item {pst_sn}: detail fetch failed, skipping")
                        continue

                    detail = self._parse_detail(detail_html)

                    # Abstract: prefer detail page (full HTML→text), fall back to list preview
                    abstract = detail.get("abstract") or item.get("abstract_preview", "")
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {pst_sn}: abstract too short "
                              f"({len(abstract)} chars), skipping")
                        continue

                    # Authors: detail page may have same or richer value
                    authors_raw = detail.get("authors_detail") or item.get("authors", "")

                    # listed_date (등록일 from list page)
                    listed_date = item.get("listed_date", "") or ""

                    # published_date (발행일 from detail page, fall back to listed_date)
                    published_date = detail.get("published_date") or listed_date

                    # PDF URL (from list page; detail page has same link)
                    pdf_url = item.get("pdf_url")

                    # Category
                    category = detail.get("category") or item.get("category", "")

                    # Keywords, department, original_filename, task_code
                    keywords = detail.get("keywords", "")
                    department = detail.get("department", "")
                    original_filename = detail.get("original_filename")
                    task_code = detail.get("task_code", "")
                    pub_type = detail.get("pub_type", "")
                    raw_meta = detail.get("raw_meta", {})

                    # Metadata JSON
                    meta_dict = {
                        "posted_date": listed_date,
                        "pstSn": pst_sn,
                        "pub_type": pub_type,
                        "task_code": task_code,
                        "department": department,
                    }
                    if original_filename:
                        meta_dict["originalFilename"] = original_filename
                    # Include all raw label→value pairs for completeness
                    for k, v in raw_meta.items():
                        if k not in meta_dict:
                            meta_dict[k] = v

                    paper = {
                        "site_id": self.site_id,
                        "external_id": pst_sn,
                        "url": view_url,
                        "title": item["title"],
                        "abstract": abstract,
                        "authors": authors_raw,
                        "publisher": "서울연구원",
                        "keywords": keywords,
                        "category": category,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "metadata": json.dumps(meta_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pst_sn} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping (pagination loop)")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page += 1

        if page > MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {MAX_PAGES} pages reached")

        return saved
