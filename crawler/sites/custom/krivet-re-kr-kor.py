# -*- coding: utf-8 -*-
"""KRIVET's four delivered menus: journals, conference papers, press and reports.

Source publication and registration dates stay separate and retain precision.
Report metadata uses the publisher's public ``fileType=all`` filter; a listing
is not evidence that an attachment can be downloaded. Catalogue-only records
remain explicit body gaps until a source body or a supported policy is available.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_SITE_ID = 'krivet-re-kr-kor'
_BASE_URL = 'https://www.krivet.re.kr'
_LIST_URL = f'{_BASE_URL}/kor/sub.do'
# Preserve the previously collected menu first and existing publication IDs.
_MENU_SCOPES = ('15', '19', '23', '12')
_MAX_PAGES = int(os.environ.get('LIBERTREE_MAX_PAGES', '200'))  # Unknown-boundary fallback only.
_WALL_CLOCK_LIMIT = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25 * 60)))


def _curl_get(url, params=None, retries=3, check_cancelled=None):
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    cmd = ['curl', '--tls-max', '1.3', '-sSL', '--max-time', '30',
           '-H', 'User-Agent: Mozilla/5.0', '-H', 'Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8',
           '--write-out', '\n%{http_code}', url]
    for attempt in range(retries):
        if check_cancelled:
            check_cancelled()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if check_cancelled:
                check_cancelled()
            body, separator, raw_status = result.stdout.rpartition(b'\n')
            status = int(raw_status) if separator and raw_status.isdigit() else 0
            if status in (401, 403, 429):
                raise PermissionError(f'HTTP {status}')
            if result.returncode == 0 and status == 200 and body:
                return body.decode('utf-8', errors='replace')
            # Do not turn a denial/not-found/error page into an empty list.
            if status and status < 500:
                return None
        except PermissionError:
            raise
        except (subprocess.TimeoutExpired, OSError):
            pass
        if attempt < retries - 1:
            if check_cancelled:
                check_cancelled()
            time.sleep(3 ** attempt)
    return None


def _make_soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def _text(node):
    return node.get_text(' ', strip=True) if node else ''


class KrivetReKrKorCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = 'Custom: krivet-re-kr-kor'
    base_url = _BASE_URL

    def _fetch_list_page(self, page_index):
        params = {'menuSn': self._active_menu, 'pageIndex': str(page_index)}
        if self._active_menu == '12':
            params['fileType'] = 'all'
        return _curl_get(_LIST_URL, params, check_cancelled=self._check_cancelled)

    def _fetch_detail_page(self, pst_no):
        return _curl_get(_LIST_URL, {'menuSn': self._active_menu, 'pstNo': pst_no},
                         check_cancelled=self._check_cancelled)

    def _parse_list_page(self, html):
        soup = _make_soup(html)
        items = []
        for table in soup.find_all('table'):
            headers = [_text(th) for th in table.select('thead th')]
            if '제목' not in headers:
                continue
            for row in table.select('tbody tr'):
                cells = row.find_all('td', recursive=False)
                if len(cells) != len(headers):
                    if any(re.search(r'select(?:Detail|View)\(', a.get('onclick', '')) for a in row.find_all('a')):
                        raise ValueError('changed publication columns')
                    continue
                values = dict(zip(headers, cells))
                title_cell = values['제목']
                links = [(a, re.search(r"(?:selectDetail|selectView)\(['\"]([^'\"]+)['\"]\)", a.get('onclick', '')))
                         for a in title_cell.find_all('a')]
                links = [(a, match) for a, match in links if match]
                if len(links) != 1:
                    raise ValueError('unrecognized publication link')
                anchor, match = links[0]
                author_cell = values.get('저자')
                if author_cell:
                    for button in author_cell.find_all('button'):
                        button.decompose()
                items.append({'pst_no': match.group(1), 'title': _text(anchor),
                              'source_ordinal': int(_text(values.get('번호')).replace(',', '')),
                              'eng_title': _text(title_cell.select_one('.pub_sub_tit')),
                              'publication': _text(title_cell.select_one('.pub_sub_tit2')),
                              'authors_raw': _text(author_cell),
                              'published_raw': _text(values.get('발행일')),
                              'listed_raw': _text(values.get('등록일')),
                              'category': _text(values.get('분류'))})
        return items

    def _list_boundary(self, html, page):
        soup = _make_soup(html)
        form = soup.find('form', attrs={'name': 'searchForm'})
        if not form:
            raise ValueError('missing source search form')
        fields = {n.get('name'): n.get('value', '') for n in form.find_all('input') if n.get('name')}
        if fields.get('menuSn') != self._active_menu:
            raise ValueError('source menu mismatch')
        if self._active_menu == '12' and fields.get('fileType') != 'all':
            raise ValueError('source report filter mismatch')
        if not any('제목' in [_text(th) for th in table.select('thead th')] for table in soup.find_all('table')):
            raise ValueError('missing publication table')
        selected = soup.select_one('a.on[title="선택됨"]')
        if selected and _text(selected) != str(page):
            raise ValueError('source page mismatch')
        last = soup.select_one('a.page_last')
        match = re.search(r'selectList\((\d+)\)', last.get('onclick', '')) if last else None
        # A structurally valid empty result is different from missing selectors.
        empty = any(token in soup.get_text(' ', strip=True) for token in
                    ('검색된 결과가 없습니다', '조회된 결과가 없습니다', '등록된 게시물이 없습니다'))
        return int(match.group(1)) if match else None, empty

    def _attachments(self, root, menu):
        attachments = []
        for button in root.select('[onclick]'):
            call = button.get('onclick', '')
            match = re.search(r'\.fileDown\((.*?)\)', call)
            if not match:
                continue
            args = re.findall(r"'((?:\\.|[^'])*)'", match.group(1))
            if len(args) < 2:
                continue
            # Publication detail callbacks take (pstNo, attachment, ...).
            # Press callbacks take (classification, pstNo, attachment).
            if menu == '23':
                if len(args) < 3:
                    continue
                classification, uid, attachment = args[:3]
            else:
                uid, attachment = args[:2]
                classification = 'DA' if menu in ('12', '19') else None
            if not attachment.isdigit():
                continue
            params = dict(fileDownDiv='NOTICOMM', pstNo=uid, pstgAtchNo=attachment)
            if classification:
                params['pstgClsfCd'] = classification
            name = button.get('title') or _text(button)
            classes = button.get('class', [])
            pdf_named = '.pdf' in name.lower() or 'icon_file_pdf' in classes
            known_other = bool(re.search(r'\.(?:hwpx?|docx?|xlsx?|zip|pptx?)(?:\s|$)', name, re.I)) or any(
                c in classes for c in ('icon_file_hwp', 'icon_file_hwpx', 'icon_file_doc', 'icon_file_xls', 'icon_file_zip'))
            attachments.append(dict(url=f'{_BASE_URL}/kor/com/fileDown.do?'+urlencode(params),
                                    pst_no=uid, attachment=attachment, name=name,
                                    source_call=call, pdf_named=pdf_named,
                                    publication_download_candidate='btn_down' in classes and not known_other))
        return attachments

    def _parse_detail_page(self, html):
        soup = _make_soup(html)
        menu = getattr(self, '_active_menu', '15')
        title_tag = soup.select_one('.pub_tit') if menu != '23' else soup.select_one('th.tb_tit')
        if not title_tag or not _text(title_tag):
            return None
        fields, authors = {}, []
        if menu == '23':
            container = title_tag.find_parent('table')
            if not container:
                return None
            for tr in container.select('tr'):
                th, td = tr.find('th'), tr.find('td')
                if th and td and _text(th) in ('등록일', '발행일'):
                    fields[_text(th)] = _text(td)
            abstract = '\n\n'.join(n.get_text('\n', strip=True) for n in container.select('tbody tr > td[colspan="2"]'))
        else:
            container = soup
            for dl in soup.select('dl.sub_dl'):
                for dt, dd in zip(dl.find_all('dt'), dl.find_all('dd')):
                    label = _text(dt)
                    for button in dd.find_all('button'):
                        button.decompose()
                    fields[label] = _text(dd)
                    if label in ('저자', '저자명'):
                        authors = [_text(n) for n in dd.find_all('span') if _text(n)] or ([_text(dd)] if _text(dd) else [])
            abstract = '\n\n'.join(n.get_text(strip=True) for n in soup.select('#tl2 pre,#tl4 pre'))
        keywords = []
        for heading in soup.select('h5.sub_tit4'):
            if '주제어' in _text(heading):
                node = heading.find_next('div', class_='rel_box')
                keywords = [k.strip() for k in _text(node).split(',') if k.strip()]
                break
        attachments = self._attachments(container, menu)
        # Prefer explicitly labelled PDFs. Publication full-text buttons without
        # a filename remain unverified references, never downloaded-file evidence.
        primary = next((a for a in attachments if a['pdf_named']), None)
        if not primary:
            primary = next((a for a in attachments if a['publication_download_candidate']), None)
        return dict(title=_text(title_tag), eng_title=_text(soup.select_one('h5.pub_sub_tit')),
                    authors=authors, category=fields.get('분류정보', ''), publisher=fields.get('발행기관', ''),
                    published_date=fields.get('발행일', ''), reg_date=fields.get('등록일', ''),
                    doi=fields.get('DOI', ''), keywords=keywords, abstract=abstract,
                    attachments=attachments, primary_attachment=primary,
                    contents='\n\n'.join(n.get_text(strip=True) for n in soup.select('#tl1 pre,#tl3 pre')))

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        start = time.monotonic()
        saved = 0
        seen_across = {}
        self.delivery_body_gaps = []
        for menu in _MENU_SCOPES:
            self._active_menu = menu
            previous_ordinal = None
            boundary = None
            page = 1
            while True:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    return saved
                if time.monotonic() - start > _WALL_CLOCK_LIMIT:
                    self._report_source_failure('invalid_response')
                    return saved
                if boundary is None and page > _MAX_PAGES:
                    self._report_source_failure('invalid_response')
                    return saved
                time.sleep(self._delay)
                self._check_cancelled()
                try:
                    raw = self._fetch_list_page(page)
                    self._check_cancelled()
                    if not raw:
                        self._report_source_failure('fetch_failed')
                        return saved
                    current_boundary, explicit_empty = self._list_boundary(raw, page)
                    items = self._parse_list_page(raw)
                    if items and current_boundary is not None and current_boundary < page:
                        raise ValueError('invalid last page')
                    if page == 1:
                        boundary = current_boundary
                    elif current_boundary != boundary:
                        raise ValueError('changed page boundary')
                    if not items:
                        if not explicit_empty or (page > 1 and boundary and page <= boundary):
                            raise ValueError('unexpected empty source page')
                        break
                    # The publisher can list the same pstNo more than once.
                    # Row positions, not new publication IDs, prove page progress.
                    ordinals = [i['source_ordinal'] for i in items]
                    if min(ordinals) < 1 or ordinals != list(range(ordinals[0], ordinals[0] - len(items), -1)):
                        raise ValueError('invalid source row positions')
                    if previous_ordinal is not None and ordinals[0] != previous_ordinal - 1:
                        raise ValueError('source row positions repeated or skipped')
                    if boundary is not None and page == boundary and ordinals[-1] != 1:
                        raise ValueError('source ended before final row position')
                    previous_ordinal = ordinals[-1]
                except PermissionError as exc:
                    self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                    return saved
                except Exception:
                    self._report_source_failure('invalid_response')
                    return saved
                for item in items:
                    self._check_cancelled()
                    if limit is not None and saved >= limit:
                        return saved
                    uid = item['pst_no']
                    if uid in seen_across:
                        if seen_across[uid] != item['title']:
                            self._report_source_failure('item_failed')
                        continue
                    # Source identity is pstNo, preserving existing DB/file links.
                    seen_across[uid] = item['title']
                    try:
                        time.sleep(self._delay)
                        self._check_cancelled()
                        raw_detail = self._fetch_detail_page(uid)
                        self._check_cancelled()
                        detail = self._parse_detail_page(raw_detail) if raw_detail else None
                        if not detail or not detail['title'] or detail['title'] != item['title']:
                            self._report_source_failure('item_failed')
                            continue
                        if any(a['pst_no'] != uid for a in detail['attachments']):
                            self._report_source_failure('item_failed')
                            continue
                        if len(detail['abstract']) < 50:
                            self.delivery_body_gaps.append(dict(menu=menu, pst_no=uid, contents_present=bool(detail['contents'])))
                            self._report_source_failure('item_failed')
                            continue
                        authors = detail['authors'] or ([re.sub(r'\s*외\s*$', '', item['authors_raw']).strip()] if item['authors_raw'] else [])
                        primary = detail['primary_attachment']
                        paper = dict(site_id=self.site_id, external_id=uid, title=detail['title'],
                                     authors=authors, abstract=detail['abstract'], category=detail['category'] or item['category'],
                                     keywords=detail['keywords'], published_date=detail['published_date'] or item['published_raw'] or None,
                                     posted_date=detail['reg_date'] or item['listed_raw'] or None,
                                     url=_LIST_URL+'?'+urlencode(dict(menuSn=menu, pstNo=uid)),
                                     pdf_url=primary['url'] if primary else None, doi=detail['doi'],
                                     publisher=detail['publisher'], department=detail['publisher'],
                                     metadata=json.dumps(dict(menu=menu, eng_title=detail['eng_title'] or item['eng_title'],
                                                              publication=item['publication'], reg_date=detail['reg_date'],
                                                              attachments=detail['attachments'],
                                                              primary_attachment_type_verified=False), ensure_ascii=False))
                        self._save_paper(paper)
                        if self._last_save_created:
                            saved += 1
                        self._check_cancelled()
                    except PermissionError as exc:
                        self._report_source_failure('rate_limited' if '429' in str(exc) else 'authentication_failed')
                        return saved
                    except Exception:
                        self._report_source_failure('item_failed')
                if boundary is not None and page >= boundary:
                    break
                page += 1
        self._check_cancelled()
        return saved
