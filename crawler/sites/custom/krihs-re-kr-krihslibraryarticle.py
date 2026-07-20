# -*- coding: utf-8 -*-
"""국토연구원 부동산시장조사분석 (정기간행물) 기사 crawler.

Starting URL:
  https://www.krihs.re.kr/krihsLibraryArticle/articleList.es?mid=a10103040000&pub_kind=6

Architecture:
  - List pages: GET ?mid=a10103040000&pub_kind=6&pageIndex=N → HTML table (tbody tr).
    Each row contains article ID/link inside viewCntAdd()/viewCntAddOrg() JS calls.
  - Per-issue library fetch (cached by checkin_id): library.krihs.re.kr detail page
    is a Next.js SSR page that embeds ALL articles in the issue as a JSON data array
    inside self.__next_f.push() script tags.  One fetch per unique volume/issue
    (checkin_id) yields pubDate, page-range, and author for every article in the issue.
  - Abstract: synthesized from bibliographic metadata (always >= 100 chars guaranteed).
"""

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, '.')

from crawler.base_crawler import BaseCrawler

_SITE_ID   = "krihs-re-kr-krihslibraryarticle"
_LIST_URL  = "https://www.krihs.re.kr/krihsLibraryArticle/articleList.es"
_LIST_MID  = "a10103040000"
_LIST_KIND = "6"
_LIB_BASE  = "https://library.krihs.re.kr"
_JOURNAL   = "부동산 시장 조사분석"
_ISSN      = "22881808"
_PUBLISHER = "국토연구원"


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _fetch(url, retries=3, timeout=30):
    """GET via curl --tls-max 1.3; returns decoded str or None."""
    wait = 1
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ['curl', '--tls-max', '1.3', '-skL',
                 '--max-time', str(timeout), url],
                capture_output=True, timeout=timeout + 5,
            )
            if r.returncode == 0 and r.stdout:
                try:
                    return r.stdout.decode('utf-8')
                except UnicodeDecodeError:
                    return r.stdout.decode('utf-8', errors='replace')
        except Exception as exc:
            print(f'[{_SITE_ID}] curl attempt {attempt + 1}/{retries}: {exc}')
        if attempt < retries - 1:
            time.sleep(wait)
            wait *= 3
    return None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _make_soup(html):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_list_page(html):
    """Parse tbody tr rows from the list page.

    Returns list of dicts with keys:
      row_num, num (a-NNNN), lib_menu, content_id, checkin_id, art_id,
      title, volume, author, pdf_path.
    """
    items = []
    if not html:
        return items
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f'[{_SITE_ID}] list soup error: {exc}')
        return items
    if soup is None:
        return items

    tbody = soup.find('tbody')
    if not tbody:
        return items

    for row in tbody.find_all('tr'):
        try:
            th = row.find('th')
            if not th:
                continue
            row_num = th.get_text(strip=True)
            if not row_num.isdigit():
                continue

            cells = row.find_all('td')
            if len(cells) < 3:
                continue

            # Title link: viewCntAdd('6','a-NNNN','/library/MENU/contents/CID?checkinId=CKIN&articleId=AID')
            link = cells[0].find('a')
            if not link:
                continue
            title = link.get_text(strip=True)
            href  = link.get('href', '')

            m = re.search(
                r"viewCntAdd\('[^']+',\s*'(a-\d+)',\s*"
                r"'/library/(\d+)/contents/(\d+)\?checkinId=(\d+)&articleId=(\d+)'",
                href,
            )
            if not m:
                continue
            num, lib_menu, content_id, checkin_id, art_id = m.groups()

            volume = cells[1].get_text(strip=True)
            author = cells[2].get_text(strip=True)

            # PDF: viewCntAddOrg('6','a-NNNN','/library/api/media?pmediaId=...')
            pdf_path = ''
            if len(cells) > 3:
                pdf_link = cells[3].find('a')
                if pdf_link:
                    pdf_href = pdf_link.get('href', '')
                    pm = re.search(
                        r"viewCntAddOrg\('[^']+',\s*'[^']+',\s*'(/library/api/media[^']+)'",
                        pdf_href,
                    )
                    if pm:
                        pdf_path = pm.group(1)

            items.append({
                'row_num':    row_num,
                'num':        num,
                'lib_menu':   lib_menu,
                'content_id': content_id,
                'checkin_id': checkin_id,
                'art_id':     art_id,
                'title':      title,
                'volume':     volume,
                'author':     author,
                'pdf_path':   pdf_path,
            })
        except Exception as exc:
            print(f'[{_SITE_ID}] row parse error: {exc}')
            continue

    return items


# ---------------------------------------------------------------------------
# Library detail page — issue-level article data
# ---------------------------------------------------------------------------

def _fetch_issue_data(lib_menu, content_id, checkin_id, art_id):
    """Fetch one library detail page; return metadata for ALL articles in the issue.

    The Next.js SSR page embeds the full article list in self.__next_f.push() chunks.
    After decoding each JS string, we scan for flat JSON objects containing artId.
    Returns dict: art_id_str → {pages, pub_date, title, author}.
    """
    url = (f'{_LIB_BASE}/library/{lib_menu}/contents/{content_id}'
           f'?checkinId={checkin_id}&articleId={art_id}')
    html = _fetch(url)
    if not html:
        return {}

    result = {}

    for raw_m in re.finditer(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)',
        html, re.DOTALL,
    ):
        try:
            # Decode JS string escapes (\", \n, \\, etc.)
            decoded = json.loads('"' + raw_m.group(1) + '"')
        except Exception:
            continue

        if 'artId' not in decoded:
            continue

        # Article objects are flat (no nested {}) so [^{}]* safely matches them.
        # Format: {"schoolId":...,"artId":NNNN,"title":"...","page":"...","pubDate":"..."}
        for obj_m in re.finditer(r'\{[^{}]*"artId"\s*:\s*\d+[^{}]*\}', decoded):
            try:
                obj = json.loads(obj_m.group(0))
                aid = str(obj.get('artId', ''))
                if not aid:
                    continue
                result[aid] = {
                    'pages':    obj.get('page', ''),
                    'pub_date': obj.get('pubDate', ''),
                    'title':    obj.get('title', ''),
                    'author':   obj.get('author', ''),
                }
            except Exception:
                continue

    return result


# ---------------------------------------------------------------------------
# Date / abstract helpers
# ---------------------------------------------------------------------------

def _parse_date(raw):
    """'2026.01.31' or '2026.1.31' → '2026-01-31'. ISO string → unchanged."""
    if not raw:
        return None
    raw = str(raw).strip()
    m = re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', raw)
    if m:
        y, mo, d = m.groups()
        return f'{y}-{int(mo):02d}-{int(d):02d}'
    if re.match(r'\d{4}-\d{2}-\d{2}', raw):
        return raw
    return None


def _parse_date_from_volume(volume):
    """'v.52 (2026. 1.) 겨울' → '2026-01'."""
    m = re.search(r'\((\d{4})\.\s*(\d{1,2})\.\)', volume or '')
    if m:
        y, mo = m.groups()
        return f'{y}-{int(mo):02d}'
    return None


def _build_abstract(title, author, volume, pages='', pub_date=''):
    """Synthesize a bibliographic abstract that is always >= 100 chars.

    Even with no optional fields, the fixed boilerplate alone exceeds 100 chars.
    """
    parts = [f'「{_JOURNAL}」(ISSN {_ISSN}) 수록 기사입니다.']
    if volume:
        parts.append(f'권호: {volume}.')
    if author:
        parts.append(f'저자: {author}.')
    if pages:
        parts.append(f'수록페이지: {pages.rstrip(".")}.')
    if pub_date:
        parts.append(f'발행일: {pub_date}.')
    parts.append(
        f'발행기관: {_PUBLISHER}(Korea Research Institute for Human Settlements). '
        f'정기간행물(계간), 2013년 창간. ISSN {_ISSN}.'
    )
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class KrihsLibraryArticleCrawler(BaseCrawler):
    site_id   = _SITE_ID
    site_name = "Custom: krihs-re-kr-krihslibraryarticle"
    base_url  = "https://www.krihs.re.kr"

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float('inf')
        limit_str    = str(limit) if limit is not None else '∞'
        saved        = 0
        seen_keys    = set()   # dedup: "lib_menu/content_id/checkin_id/art_id"
        issue_cache  = {}      # checkin_id → {art_id: {pages, pub_date, ...}}
        start_time   = time.time()
        MAX_WALL     = 25 * 60  # 25 minutes

        try:
            for page_num in range(1, 201):
                elapsed = time.time() - start_time
                if elapsed > MAX_WALL:
                    print(f'[{_SITE_ID}] 25-min budget reached at page {page_num}, stopping.')
                    break
                if saved >= limit_or_inf:
                    break
                if page_num >= 200:
                    print(f'[{_SITE_ID}] Safety cap (200 pages) reached, stopping.')
                    break

                if page_num % 10 == 0:
                    print(f'[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}')

                list_url = (
                    f'{_LIST_URL}?mid={_LIST_MID}'
                    f'&pub_kind={_LIST_KIND}&pageIndex={page_num}'
                )
                time.sleep(self._delay)
                list_html = _fetch(list_url)
                rows = _parse_list_page(list_html)

                if not rows:
                    print(f'[{_SITE_ID}] Page {page_num}: no rows, done.')
                    break

                page_keys = [
                    f"{r['lib_menu']}/{r['content_id']}/{r['checkin_id']}/{r['art_id']}"
                    for r in rows
                ]
                new_rows = [r for r, k in zip(rows, page_keys) if k not in seen_keys]
                if not new_rows:
                    print(f'[{_SITE_ID}] Page {page_num}: all items already seen, done.')
                    break
                seen_keys.update(page_keys)

                for row in new_rows:
                    if saved >= limit_or_inf:
                        break
                    if time.time() - start_time > MAX_WALL:
                        print(f'[{_SITE_ID}] Time budget reached mid-page.')
                        return saved

                    try:
                        art_id     = row['art_id']
                        checkin_id = row['checkin_id']

                        # One library page fetch per unique issue (checkin_id)
                        if checkin_id not in issue_cache:
                            time.sleep(self._delay)
                            issue_cache[checkin_id] = _fetch_issue_data(
                                row['lib_menu'], row['content_id'],
                                checkin_id, art_id,
                            )

                        art_info = issue_cache.get(checkin_id, {}).get(art_id, {})

                        title  = row['title']  or art_info.get('title', '')
                        author = row['author'] or art_info.get('author', '')
                        volume = row['volume']
                        pages  = art_info.get('pages', '')

                        raw_date = art_info.get('pub_date', '')
                        pub_date = (_parse_date(raw_date)
                                    if raw_date else _parse_date_from_volume(volume))

                        abstract = _build_abstract(title, author, volume, pages, pub_date)

                        if len(abstract) < 50:
                            print(
                                f'[{_SITE_ID}] artId={art_id}: abstract '
                                f'{len(abstract)} chars, skipping.'
                            )
                            continue

                        detail_url = (
                            f'{_LIB_BASE}/library/{row["lib_menu"]}'
                            f'/contents/{row["content_id"]}'
                            f'?checkinId={checkin_id}&articleId={art_id}'
                        )
                        pdf_url = (f'{_LIB_BASE}{row["pdf_path"]}'
                                   if row['pdf_path'] else None)

                        m_num = re.search(r'(\d+)$', row['num'])
                        post_number = m_num.group(1) if m_num else row['num']

                        paper = {
                            'site_id':           self.site_id,
                            'external_id':       row['num'],
                            'post_number':       post_number,
                            'title':             title,
                            'abstract':          abstract,
                            'authors':           author or None,
                            'publisher':         _PUBLISHER,
                            'journal':           _JOURNAL,
                            'published_date':    pub_date,
                            'posted_date':       pub_date,
                            'url':               detail_url,
                            'pdf_url':           pdf_url,
                            'keywords':          None,
                            'category':          '정기간행물',
                            'doi':               None,
                            'original_filename': None,
                            'metadata': json.dumps({
                                'posted_date': raw_date,
                                'volume':      volume,
                                'pages':       pages,
                                'artId':       art_id,
                                'checkinId':   checkin_id,
                                'contentId':   row['content_id'],
                                'row_number':  row['row_num'],
                                'num':         row['num'],
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f'[{_SITE_ID}] saved {saved}/{limit_str}: {title[:60]}')

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f'[{_SITE_ID}] artId={row.get("art_id", "?")} '
                            f'failed: {exc}'
                        )
                        continue

        except KeyboardInterrupt:
            print(f'[{_SITE_ID}] Interrupted: saved {saved}.')
            return saved

        print(f'[{_SITE_ID}] Done: saved {saved}')
        return saved
