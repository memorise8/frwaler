# -*- coding: utf-8 -*-
"""Crawler for mohrss.gov.cn — Government Information Disclosure Annual Reports
(政府信息公开年报).

Starting URL: https://www.mohrss.gov.cn/xxgk2020/zfxxgknb/
"""

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_SITE_ID  = "mohrss-gov-cn-xxgk2020"
_BASE_URL = "https://www.mohrss.gov.cn"
_START    = "https://www.mohrss.gov.cn/xxgk2020/zfxxgknb/"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

# TencentEdgeOne challenge: the sum WTKkN+bOYDu+wyeCN is always 3652130274 for
# this server; only the EO_Bot_Ssid inner constant changes per response.
_TST_CONSTANT = "3652130274"


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (curl-based, no requests dependency)
# ---------------------------------------------------------------------------

def _curl(url, cookies=None, referer=None, timeout=30):
    cmd = [
        "curl", "-sk", "--tls-max", "1.3",
        "-A", _UA,
        "-H", "Accept-Language: zh-CN,zh;q=0.9",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if cookies:
        cmd += ["-b", cookies]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _solve_challenge(raw):
    """Return EO_Bot_Ssid value from TencentEdgeOne JS challenge, or None."""
    m = re.search(r'\]\(t,(\d+)\)', raw)
    return m.group(1) if m else None


def _fetch(url, referer=None, max_retries=3):
    """Fetch URL with TencentEdgeOne challenge resolution and exponential backoff."""
    last = ""
    for attempt in range(max_retries):
        backoff = [1, 3, 9][attempt]
        raw = _curl(url, referer=referer)
        if not raw:
            if attempt < max_retries - 1:
                time.sleep(backoff)
            continue
        # Attempt to resolve bot challenge (up to 2 rounds)
        for _ in range(2):
            if 'WTKkN' not in raw:
                break
            ssid = _solve_challenge(raw)
            if not ssid:
                break
            ck = f"__tst_status={_TST_CONSTANT}#; EO_Bot_Ssid={ssid}"
            time.sleep(0.5)
            raw = _curl(url, cookies=ck, referer=referer)
            if not raw:
                raw = last
                break
        last = raw
        if raw and 'WTKkN' not in raw:
            return raw
        if attempt < max_retries - 1:
            time.sleep(backoff)
    return last


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
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


def _html_to_text(html_fragment):
    """Strip tags, decode common entities, normalise whitespace."""
    text = re.sub(r'<[^>]+>', ' ', html_fragment or "")
    text = text.replace('\xa0', ' ').replace('&nbsp;', ' ')
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&[a-z]+;', '', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_listing(html):
    """Return list of (rel_url, title, date_str) from a listing page."""
    items = []
    soup = _make_soup(html)
    if soup:
        ul = soup.find("ul", class_="rsb_con_rightUl")
        if ul:
            for li in ul.find_all("li"):
                a = li.find("a", href=True)
                span = li.find("span")
                if not a:
                    continue
                href = a["href"].strip()
                title = a.get_text(strip=True)
                date_str = span.get_text(strip=True) if span else ""
                if href and title:
                    items.append((href, title, date_str))
            return items
    # Regex fallback
    for m in re.finditer(
        r'<li>\s*<a\s+href=["\']([^"\']+\.html)["\'][^>]*>\s*([^<]{5,}?)\s*</a>'
        r'\s*<span>\s*([^<]*?)\s*</span>',
        html, re.S | re.I,
    ):
        items.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    return items


def _parse_detail(html, fallback_title="", fallback_date=""):
    """Return (title, pub_date, abstract) from a detail article page."""
    title = fallback_title
    pub_date = fallback_date
    abstract = ""

    soup = _make_soup(html)
    if soup:
        t = soup.find(class_="artT")
        if t:
            title = t.get_text(strip=True) or title

        info = soup.find(class_="art_infos")
        if info:
            dm = re.search(r'(\d{4}-\d{2}-\d{2})', info.get_text())
            if dm:
                pub_date = dm.group(1)

        content = soup.find(class_="TRS_Editor") or soup.find(class_="art_p")
        if content:
            abstract = content.get_text(separator=" ", strip=True)

        if not abstract:
            main = (soup.find(class_="rsb_mainc")
                    or soup.find(class_="art_det")
                    or soup.find("article"))
            if main:
                abstract = main.get_text(separator=" ", strip=True)
    else:
        # Pure-regex fallback
        tm = re.search(r'class=["\']artT["\'][^>]*>\s*([^<]+)', html)
        if tm:
            title = tm.group(1).strip() or title
        dm = re.search(r'发布时间[：:]\s*(\d{4}-\d{2}-\d{2})', html)
        if dm:
            pub_date = dm.group(1)
        cm = re.search(r'class=["\']?TRS_Editor["\']?\s*>(.*?)</div', html, re.S | re.I)
        if cm:
            abstract = _html_to_text(cm.group(1))

    return title, pub_date, abstract


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MohrssCrawler(BaseCrawler):
    site_id   = _SITE_ID
    site_name = "Custom: mohrss-gov-cn-xxgk2020"
    base_url  = _BASE_URL

    def crawl(self, limit=None):
        start_ts   = time.time()
        MAX_PAGES  = 200
        MAX_MINS   = 25
        RATE       = self._delay  # seconds between detail fetches

        saved      = 0
        seen_urls  = set()
        limit_val  = limit if limit is not None else float("inf")

        page_idx = 0  # 0 → index.html, N → index_N.html

        while saved < limit_val:
            # Wall-clock budget
            if (time.time() - start_ts) / 60 >= MAX_MINS:
                print(f"[{_SITE_ID}] {MAX_MINS}min wall-clock limit reached, stopping")
                break
            if page_idx >= MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {MAX_PAGES} pages reached, stopping")
                break

            page_url = _START if page_idx == 0 else _START + f"index_{page_idx}.html"

            if page_idx % 10 == 0:
                lim_str = str(int(limit_val)) if limit_val != float("inf") else "∞"
                print(f"[{_SITE_ID}] page {page_idx}: saved {saved}/{lim_str}")

            page_html = _fetch(page_url, referer=_START)
            if not page_html:
                print(f"[{_SITE_ID}] page {page_idx}: empty response, stopping")
                break

            # Detect end-of-pagination: pages beyond the last one redirect to ./
            if ('meta http-equiv="refresh"' in page_html
                    or "meta http-equiv='refresh'" in page_html):
                if re.search(r'url\s*=\s*\./', page_html, re.I):
                    print(f"[{_SITE_ID}] page {page_idx}: meta-refresh redirect → end of pagination")
                    break

            items = _parse_listing(page_html)
            if not items:
                print(f"[{_SITE_ID}] page {page_idx}: no items found, stopping")
                break

            new_on_page = 0
            for rel_url, list_title, list_date in items:
                if saved >= limit_val:
                    break

                abs_url = urljoin(page_url, rel_url)
                if abs_url in seen_urls:
                    continue
                seen_urls.add(abs_url)
                new_on_page += 1

                # post_number: numeric ID from URL like t20260128_566265.html
                pn_m = re.search(r't\d+_(\d+)\.html', rel_url)
                post_number = pn_m.group(1) if pn_m else None

                try:
                    detail_html = _fetch(abs_url, referer=page_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item {abs_url}: empty detail, skipping")
                        continue

                    title, pub_date, abstract = _parse_detail(
                        detail_html,
                        fallback_title=list_title,
                        fallback_date=list_date,
                    )

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] item {abs_url}: abstract too short "
                              f"({len(abstract)} chars), skipping")
                        continue

                    # PDF attachments
                    pdf_url = None
                    original_filename = None
                    pdf_links = re.findall(r'href=["\']([^"\']*\.pdf)["\']',
                                           detail_html, re.I)
                    if pdf_links:
                        pdf_url = urljoin(abs_url, pdf_links[0])
                        original_filename = (
                            pdf_links[0].rstrip("/").split("/")[-1].split("?")[0]
                        )

                    paper_dict = {
                        "site_id":           _SITE_ID,
                        "external_id":       post_number,
                        "url":               abs_url,
                        "title":             title,
                        "abstract":          abstract[:8000],
                        "published_date":    pub_date,
                        "posted_date":       list_date,
                        "publisher":         "人力资源社会保障部",
                        "department":        "中华人民共和国人力资源和社会保障部",
                        "pdf_url":           pdf_url,
                        "original_filename": original_filename,
                        "keywords":          None,
                        "category":          "政府信息公开年报",
                        "metadata":          json.dumps(
                            {
                                "posted_date":  list_date,
                                "list_title":   list_title,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper_dict)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}: {title[:60]}")

                    time.sleep(RATE)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {abs_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page_idx}: all items already seen, stopping")
                break

            page_idx += 1

        lim_str = str(int(limit_val)) if limit_val != float("inf") else "∞"
        print(f"[{_SITE_ID}] finished: saved {saved}/{lim_str} across {page_idx+1} page(s)")
        return saved
